"""台账汇总的 interface 测试:毛差恒等于应收减缴出,且与统计页同一组筛选条件。"""

import pytest

import app.db as db
from app.ledger import margin_from, paid_for, paid_to_utility, tenant_due


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "summary.sqlite3")
    db.init_db()
    from app import readings

    c = db.connect()
    for name in ("甲", "乙"):
        c.execute("INSERT INTO households(name) VALUES (?)", (name,))
    hh = {r["name"]: r["id"] for r in c.execute("SELECT id,name FROM households")}
    ids = {}
    for name, kind in (("总水", "water"), ("总电", "electricity")):
        c.execute("INSERT INTO meters(name,meter_type,level) VALUES (?,?,'main')", (name, kind))
        ids[kind] = c.execute("SELECT id FROM meters WHERE name=?", (name,)).fetchone()["id"]
    for kind, amount in (("water", 504.0), ("electricity", 340.0)):
        c.execute("INSERT INTO payments(meter_id,payment_date,amount) VALUES (?,?,?)",
                  (ids[kind], "2025-02-05", amount))

    # 两个租户各一条水线:甲用 30,乙用 50。应收由读数派生。
    for owner, usage in (("甲", 30), ("乙", 50)):
        c.execute("INSERT INTO meters(name,meter_type,level,phase) VALUES (?,?,'sub','single')",
                  (f"表{owner}", "water"))
        mid = c.execute("SELECT id FROM meters WHERE name=?", (f"表{owner}",)).fetchone()["id"]
        line = c.execute("INSERT INTO lines(household_id,meter_type,move_in_date,move_in_value) "
                         "VALUES (?,?,?,?)", (hh[owner], "water", "2025-01-01", 0)).lastrowid
        c.execute("UPDATE meters SET line_id=? WHERE id=?", (line, mid))
        for d, v in (("2025-01-01", 0), ("2025-02-01", usage)):
            readings.insert(c, kind="move_in" if d == "2025-01-01" else "periodic",
                            line_id=line, meter_id=mid, reading_date=d,
                            parts=(v, None, None, None), price=10.0)
    c.commit()
    yield c, hh
    c.close()


def test_paid_for_adds_both_types_or_one(env):
    c, _ = env
    assert paid_to_utility(c) == {"water": 504.0, "electricity": 340.0}
    assert paid_for(c) == 844.0
    assert paid_for(c, "water") == 504.0


def test_margin_is_due_minus_paid(env):
    c, _ = env
    assert tenant_due(c) == 800.0
    assert margin_from(tenant_due(c), paid_for(c)) == 800.0 - 844.0
    assert margin_from(tenant_due(c, "water"), paid_for(c, "water")) == 800.0 - 504.0


def test_margin_uses_the_same_filter_as_the_page(env):
    """统计页按租户筛选时,应收是过滤后的值,毛差必须用同一份,不能全量相减。"""
    c, hh = env
    due_jia = tenant_due(c, household_id=hh["甲"])
    assert due_jia == 300.0
    assert margin_from(due_jia, paid_for(c)) == 300.0 - 844.0


def test_view_agrees_with_per_household_ledger(env):
    """全量账期视图与逐户台账逐分一致 —— 五个页面共用同一份事实。"""
    from app.ledger import household_ledger, ledger_view

    c, _hh = env
    view = ledger_view(c)
    per = {x["household_name"]: household_ledger(c, x["household_id"])
           for x in view["households"]}
    for row in view["rows"]:
        same = per[row["household_name"]]
        match = [x for x in same["rows"]
                 if x["line_id"] == row["line_id"] and x["period_end"] == row["period_end"]]
        assert match and match[0]["paid"] == row["paid"]
        assert match[0]["outstanding"] == row["outstanding"]
    assert view["totals"]["total_due"] == round(sum(x["total_due"] for x in per.values()), 2)


def test_paid_spans_matches_paid_periods(env):
    """抄表页的批量锁判定与逐户锁判定给同一组区间。"""
    from app.ledger import paid_periods, paid_spans

    c, hh = env
    for x in paid_spans(c):
        assert x in [{"household_id": hh, **p} for p in paid_periods(c, hh[x["household_id"]])] \
            if False else True
    # 简化:两边算出的区间集合必须相等
    spans = {(x["household_id"], x["start"], x["end"]) for x in paid_spans(c)}
    per = {(k, p["start"], p["end"]) for k in hh.values() for p in paid_periods(c, k)}
    assert spans == per
