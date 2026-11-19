"""读数录入模块的 interface 测试:直接跨 module 的 seam,不经 HTTP,不查库表。

四个拒绝(三相缺相 / 读数倒退 / 日期撞位 / 已收款锁定)与三个放行(常规抄表 / 补录到中间 / 三相读数)。
"""

import pytest

import app.db as db
from app import readings
from app.billing import all_periods


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reading.sqlite3")
    db.init_db()
    c = db.connect()
    c.execute("INSERT INTO households(name) VALUES ('甲')")
    hh = c.execute("SELECT id FROM households").fetchone()["id"]
    c.execute("INSERT INTO meters(name,meter_type,level,phase,base_value) "
              "VALUES ('单相','water','sub','single',100)")
    c.execute("INSERT INTO meters(name,meter_type,level,phase,base_value) "
              "VALUES ('三相','electricity','sub','three',0)")
    single = c.execute("SELECT id FROM meters WHERE name='单相'").fetchone()["id"]
    three = c.execute("SELECT id FROM meters WHERE name='三相'").fetchone()["id"]
    line_s = c.execute("INSERT INTO lines(household_id,meter_type,move_in_date,move_in_value) "
                       "VALUES (?,?,?,?)", (hh, "water", "2025-01-01", 100)).lastrowid
    line_t = c.execute("INSERT INTO lines(household_id,meter_type,move_in_date,move_in_value) "
                       "VALUES (?,?,?,?)", (hh, "electricity", "2025-01-01", 0)).lastrowid
    c.execute("UPDATE meters SET line_id=? WHERE id=?", (line_s, single))
    c.execute("UPDATE meters SET line_id=? WHERE id=?", (line_t, three))
    for lid, mid, parts in ((line_s, single, (100, None, None, None)),
                            (line_t, three, (0, 0, 0, 0))):
        readings.insert(c, kind="move_in", line_id=lid, meter_id=mid,
                        reading_date="2025-01-01", parts=parts, price=10.0)
    c.commit()
    yield c, {"hh": hh, "single": single, "three": three, "line_s": line_s, "line_t": line_t}
    c.close()


def test_three_phase_missing_a_part_is_rejected(conn):
    with pytest.raises(readings.ReadingRejected):
        readings.parts_of({"value_1": "10", "value_2": "20"}, {"phase": "three"})


def test_reading_below_previous_is_rejected(conn):
    c, ids = conn
    readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                    reading_date="2025-02-01", parts=(130, None, None, None))
    with pytest.raises(readings.ReadingRejected) as err:
        readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                        reading_date="2025-03-01", parts=(120, None, None, None))
    assert "小于前一条读数" in str(err.value)


def test_reading_date_collision_is_rejected(conn):
    c, ids = conn
    with pytest.raises(readings.ReadingRejected) as err:
        readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                        reading_date="2025-01-01", parts=(110, None, None, None))
    assert "需晚于前一条读数" in str(err.value)


def test_locked_period_rejects_backfill(conn):
    c, ids = conn
    readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                    reading_date="2025-03-01", parts=(160, None, None, None))
    c.execute("INSERT INTO receipts(household_id,entry_date,amount,kind) VALUES (?,?,?,?)",
              (ids["hh"], "2025-03-05", 600.0, "receipt"))
    with pytest.raises(readings.ReadingRejected) as err:
        readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                        reading_date="2025-02-01", parts=(130, None, None, None))
    assert "已收款" in str(err.value)


def test_plain_reading_records_a_period(conn):
    c, ids = conn
    readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                    reading_date="2025-02-01", parts=(130, None, None, None))
    periods = all_periods(c)
    assert [(p["start"], p["end"], p["usage"], p["amount"]) for p in periods] == [
        ("2025-01-01", "2025-02-01", 30, 300.0)]


def test_backfill_into_middle_is_accepted(conn):
    c, ids = conn
    readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                    reading_date="2025-03-01", parts=(160, None, None, None))
    readings.insert(c, kind="periodic", line_id=ids["line_s"], meter_id=ids["single"],
                    reading_date="2025-02-01", parts=(130, None, None, None))
    usages = [p["usage"] for p in all_periods(c)]
    assert usages == [30, 30]


def test_three_phase_parts_are_summed(conn):
    c, ids = conn
    meter = c.execute("SELECT * FROM meters WHERE name='三相'").fetchone()
    parts = readings.parts_of({"value_1": "10", "value_2": "20", "value_3": "40"}, meter)
    assert parts == (70, 10, 20, 40)
    readings.insert(c, kind="periodic", line_id=ids["line_t"], meter_id=ids["three"],
                    reading_date="2025-02-01", parts=parts)
    row = c.execute("SELECT value, r1, r2, r3 FROM readings WHERE kind='periodic'").fetchone()
    assert (row["value"], row["r1"], row["r2"], row["r3"]) == (70, 10, 20, 40)
    assert all_periods(c, meter_type="electricity")[0]["usage"] == 70
