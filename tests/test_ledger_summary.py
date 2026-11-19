"""台账汇总的 interface 测试:毛差恒等于应收减缴出,且按水/电分型正确。"""

import pytest

import app.db as db
from app.ledger import margin, paid_to_utility, tenant_due


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "summary.sqlite3")
    db.init_db()
    c = db.connect()
    c.execute("INSERT INTO households(name) VALUES ('甲')")
    hh = c.execute("SELECT id FROM households").fetchone()["id"]
    ids = {}
    for name, kind in (("总水", "water"), ("总电", "electricity")):
        c.execute("INSERT INTO meters(name,meter_type,level) VALUES (?,?,'main')", (name, kind))
        ids[kind] = c.execute("SELECT id FROM meters WHERE name=?", (name,)).fetchone()["id"]
    for kind, amount in (("water", 504.0), ("electricity", 340.0)):
        c.execute("INSERT INTO payments(meter_id,payment_date,amount) VALUES (?,?,?)",
                  (ids[kind], "2025-02-05", amount))
    from app import readings

    # 应收由读数派生:一条入户线 + 一次抄表 = 一个账期
    for kind, usage, price in (("water", 30, 10.0), ("electricity", 50, 5.0)):
        c.execute("INSERT INTO meters(name,meter_type,level,phase) VALUES (?,?,'sub','single')",
                  (f"分{kind}", kind))
        mid = c.execute("SELECT id FROM meters WHERE name=?", (f"分{kind}",)).fetchone()["id"]
        line = c.execute("INSERT INTO lines(household_id,meter_type,move_in_date,move_in_value) "
                         "VALUES (?,?,?,?)", (hh, kind, "2025-01-01", 0)).lastrowid
        c.execute("UPDATE meters SET line_id=? WHERE id=?", (line, mid))
        for d, v in (("2025-01-01", 0), ("2025-02-01", usage)):
            readings.insert(c, kind="move_in" if d == "2025-01-01" else "periodic",
                            line_id=line, meter_id=mid, reading_date=d,
                            parts=(v, None, None, None), price=price)
    c.commit()
    yield c
    c.close()


def test_margin_is_due_minus_paid(env):
    assert tenant_due(env) == 550.0
    assert paid_to_utility(env) == {"water": 504.0, "electricity": 340.0}
    assert margin(env) == 550.0 - 844.0


def test_margin_per_meter_type(env):
    assert margin(env, "water") == 300.0 - 504.0
    assert margin(env, "electricity") == 250.0 - 340.0
    assert margin(env) == margin(env, "water") + margin(env, "electricity")
