"""锁判据的 interface 测试:已付款区间必须与完整台账逐分一致。"""

import pytest

import app.db as db
from app.ledger import household_ledger, paid_periods


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lock.sqlite3")
    db.init_db()
    c = db.connect()
    c.execute("INSERT INTO households(name) VALUES ('甲')")
    hh = c.execute("SELECT id FROM households").fetchone()["id"]
    c.execute("INSERT INTO meters(name,meter_type,level,phase,base_value) "
              "VALUES ('水','water','sub','single',0)")
    mid = c.execute("SELECT id FROM meters WHERE name='水'").fetchone()["id"]
    line = c.execute("INSERT INTO lines(household_id,meter_type,move_in_date,move_in_value) "
                     "VALUES (?,?,?,?)", (hh, "water", "2025-01-01", 0)).lastrowid
    c.execute("UPDATE meters SET line_id=? WHERE id=?", (line, mid))
    c.commit()
    yield c, hh, line
    c.close()


def _period(c, hh, line, start, end, amount):
    c.execute("INSERT INTO receivables(line_id,household_id,period_start,period_end,"
              "usage_amount,unit_price,amount,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (line, hh, start, end, int(amount), 1.0, amount, "2025-01-01"))


def _receipt(c, hh, kind, amount):
    c.execute("INSERT INTO receipts(household_id,entry_date,amount,kind) VALUES (?,?,?,?)",
              (hh, "2025-06-01", amount, kind))


def test_lock_matches_ledger_with_partial_payment(env):
    c, hh, line = env
    _period(c, hh, line, "2025-01-01", "2025-02-01", 300)
    _period(c, hh, line, "2025-02-01", "2025-03-01", 200)
    _receipt(c, hh, "receipt", 350)
    c.commit()
    led = household_ledger(c, hh)
    locked = paid_periods(c, hh)
    assert [p["end"] for p in locked] == [p["period_end"] for p in led["rows"] if p["paid"] > 0]
    assert [p["paid"] for p in locked] == [300.0, 50.0]


def test_lock_matches_ledger_with_balance(env):
    c, hh, line = env
    _period(c, hh, line, "2025-01-01", "2025-02-01", 300)
    _period(c, hh, line, "2025-02-01", "2025-03-01", 200)
    _receipt(c, hh, "receipt", 900)
    c.commit()
    led = household_ledger(c, hh)
    assert led["balance"] == 400.0
    locked = paid_periods(c, hh)
    assert [p["end"] for p in locked] == ["2025-02-01", "2025-03-01"]
    assert all(p["paid"] > 0 for p in locked)


def test_lock_matches_ledger_with_refund(env):
    c, hh, line = env
    _period(c, hh, line, "2025-01-01", "2025-02-01", 300)
    _receipt(c, hh, "receipt", 900)
    _receipt(c, hh, "refund", 800)
    c.commit()
    led = household_ledger(c, hh)
    assert led["balance"] == 0.0
    locked = paid_periods(c, hh)
    assert [p["paid"] for p in locked] == [100.0]
    assert [p["end"] for p in locked] == [p["period_end"] for p in led["rows"] if p["paid"] > 0]
