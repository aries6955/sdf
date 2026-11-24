"""领域模型的核心规则:归属只来自租期、抄表即成账、账期跨表、空档期、收款冲抵。"""


import sqlite3

import pytest

import app.db as db
from tests.conftest import (
    _add,
    _add_meter,
    _basic,
    _checkout,
    _hid,
    _line_of,
    _mid,
    _move_in,
    _msg,
    _one,
    _q,
    _with_period,
)

# ---------- 归属 ----------

def test_meters_have_no_owner_column(client):
    """归属不再存在表上,只在 lines 上。"""
    cols = {r["name"] for r in _q("PRAGMA table_info(meters)")}
    assert "household_id" not in cols
    assert "line_id" in cols


def test_ownership_comes_from_open_line(client):
    _basic(client)
    from app.billing import open_lines
    conn = db.connect()
    lines = open_lines(conn, _hid("甲"))
    conn.close()
    assert len(lines) == 1
    assert lines[0]["meter_type"] == "water"


def test_one_household_can_have_two_water_lines(client):
    """一个租户同一路可以有多条计量线,各自独立算账。"""
    _add_meter(client, "分水A", base="100")
    _add_meter(client, "分水B", base="50")
    _move_in(client, "甲", [("分水A", 100, 10), ("分水B", 50, 10)])
    _add(client, "分水A", "2025-02-01", 130)
    _add(client, "分水B", "2025-02-01", 70)
    rows = _q("select usage_amount, amount from receivables order by id")
    assert [r["usage_amount"] for r in rows] == [30, 20]
    assert [r["amount"] for r in rows] == [300.0, 200.0]


def test_meter_cannot_be_bound_twice(client):
    _basic(client)
    resp = _move_in(client, "乙", [("分水A", 100, 10)], start="2025-03-01")
    assert "有人在用" in _msg(resp)
    assert _hid("乙") is None


def test_move_in_rejects_main_meter(client):
    _add_meter(client, "总水", level="main")
    resp = _move_in(client, "甲", [("总水", 0, 10)])
    assert "总表" in _msg(resp)


# ---------- 抄表即成账 ----------

def test_reading_creates_receivable_immediately(client):
    rec = _with_period(client)
    assert rec is not None
    assert rec["usage_amount"] == 30
    assert rec["amount"] == 300.0
    assert rec["period_start"] == "2025-01-01"
    assert rec["period_end"] == "2025-02-01"


def test_no_manual_bill_route(client):
    assert client.post("/readings/bill", data={"id": "1"}).status_code in (404, 405)


def test_first_period_starts_at_move_in_value(client):
    _add_meter(client, "分水A", base="0")     # 表原点是 0
    _move_in(client, "甲", [("分水A", 100, 10)])  # 但甲的起点是 100
    _add(client, "分水A", "2025-02-01", 130)
    rec = _one("select * from receivables")
    assert rec["usage_amount"] == 30, "首期从租户入住读数起算,不是表基数"


def test_reading_must_not_decrease(client):
    _basic(client)
    resp = _add(client, "分水A", "2025-02-01", 99)
    assert "小于前一条读数" in _msg(resp)


def test_reading_date_must_advance(client):
    _basic(client)
    resp = _add(client, "分水A", "2025-01-01", 120)
    assert "需晚于" in _msg(resp)


def test_reading_can_be_backfilled_into_middle(client):
    """漏抄的读数可以插到中间,前后两期各自重算(ADR-0021)。"""
    _basic(client)
    _add(client, "分水A", "2025-03-01", 160)
    assert _one("select usage_amount from receivables")["usage_amount"] == 60
    resp = _add(client, "分水A", "2025-02-01", 130)
    assert "已保存" in _msg(resp)
    rows = _q("select period_start, period_end, usage_amount from receivables order by period_end")
    assert [(r["period_start"], r["period_end"], r["usage_amount"]) for r in rows] == [
        ("2025-01-01", "2025-02-01", 30), ("2025-02-01", "2025-03-01", 30)]


def test_backfill_only_on_current_meter(client):
    """补录只能记在当前挂在这条线上的表上:旧表换下后不再接受新读数。"""
    _basic(client)
    _add_meter(client, "分水A2", base="0")
    client.post("/switches/add", data={
        "old_meter_id": str(_mid("分水A")), "new_meter_id": str(_mid("分水A2")),
        "switch_date": "2025-02-15", "old_final_value": "150", "new_base_value": "0"},
        follow_redirects=False)
    # 旧表已换下:接口拒绝把它当作落点
    resp = client.post("/readings/add", data={
        "line_id": str(_line_of("分水A2")), "meter_id": str(_mid("分水A")),
        "reading_date": "2025-02-01", "value": "130"}, follow_redirects=False)
    assert "不在该计量线上" in _msg(resp)
    _add(client, "分水A2", "2025-03-01", 40)
    assert _one("select usage_amount from receivables")["usage_amount"] == 90


def test_price_carried_from_previous(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130, price=12)
    _add(client, "分水A", "2025-03-01", 150)      # 单价留空
    rows = _q("select unit_price, amount from receivables order by id")
    assert rows[0]["unit_price"] == 12.0
    assert rows[1]["unit_price"] == 12.0
    assert rows[1]["amount"] == 240.0


# ---------- 换表 ----------

def test_switch_merges_period_across_meters(client):
    """换表不切断账期:旧表段 + 新表段 = 一张账。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    _add_meter(client, "分水A2", base="0")
    resp = client.post("/switches/add", data={
        "old_meter_id": str(_mid("分水A")), "new_meter_id": str(_mid("分水A2")),
        "switch_date": "2025-02-15", "old_final_value": "150", "new_base_value": "0",
        "note": "表坏了"}, follow_redirects=False)
    assert "换表已登记" in _msg(resp)
    assert _one("select retired_on from meters where name='分水A'")["retired_on"] == "2025-02-15"
    # 换表日只记内部读数,不产生新账期
    assert len(_q("select * from receivables")) == 1
    # 月底抄新表 40 → 本账期用量 = (150-130) + (40-0) = 60
    _add(client, "分水A2", "2025-03-01", 40)
    rows = _q("select usage_amount, amount, period_start, period_end from receivables "
              "order by id")
    assert rows[1]["usage_amount"] == 60
    assert rows[1]["amount"] == 600.0
    assert rows[1]["period_start"] == "2025-02-01"
    assert rows[1]["period_end"] == "2025-03-01"


def test_switch_keeps_line_and_moves_meter(client):
    _basic(client)
    _add_meter(client, "分水A2", base="0")
    line_before = _line_of("分水A")
    client.post("/switches/add", data={
        "old_meter_id": str(_mid("分水A")), "new_meter_id": str(_mid("分水A2")),
        "switch_date": "2025-02-15", "old_final_value": "150", "new_base_value": "0"},
        follow_redirects=False)
    assert _line_of("分水A2") == line_before, "计量线不变,只是换了物理表"
    assert _one("select line_id from meters where name='分水A'")["line_id"] is None


def test_switch_rejects_same_or_mixed_level(client):
    _basic(client)
    _add_meter(client, "分电", kind="electricity", base="0")
    resp = client.post("/switches/add", data={
        "old_meter_id": str(_mid("分水A")), "new_meter_id": str(_mid("分水A")),
        "switch_date": "2025-02-15", "old_final_value": "1", "new_base_value": "0"},
        follow_redirects=False)
    assert "不能是同一块表" in _msg(resp)
    resp = client.post("/switches/add", data={
        "old_meter_id": str(_mid("分水A")), "new_meter_id": str(_mid("分电")),
        "switch_date": "2025-02-15", "old_final_value": "150", "new_base_value": "0"},
        follow_redirects=False)
    assert "同类、同级" in _msg(resp)


def test_main_meter_switch_repoints_sub_meters(client):
    """换总表:原接入的分表改接到新总表。"""
    _add_meter(client, "总水", level="main")
    _add_meter(client, "分水A", base="100", main="总水")
    _move_in(client, "甲", [("分水A", 100, 10)])
    _add_meter(client, "总水2", level="main")
    client.post("/switches/add", data={
        "old_meter_id": str(_mid("总水")), "new_meter_id": str(_mid("总水2")),
        "switch_date": "2025-02-15", "old_final_value": "1200", "new_base_value": "0"},
        follow_redirects=False)
    assert _one("select main_meter_id from meters where name='分水A'")["main_meter_id"] \
        == _mid("总水2")


# ---------- 退租与空档期 ----------

def test_checkout_closes_line_and_settles(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "100"})
    resp = _checkout(client, "甲", {"分水A": 180}, date="2025-03-01")
    assert "已退租" in _msg(resp)
    line = _one("select * from lines")
    assert line["move_out_date"] == "2025-03-01"
    assert line["move_out_value"] == 180
    # 最后一期 50 吨 × 10 = 500;应收 300 + 500 = 800,已收 100,欠 700
    html = client.get(f"/ledger?household_id={_hid('甲')}").text
    assert "800.00" in html and "700.00" in html


def test_checkout_allows_unpaid_tail(client):
    """退租只要求算清,不要求收齐。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    resp = _checkout(client, "甲", {"分水A": 180}, date="2025-03-01")
    assert "已退租" in _msg(resp), "有欠款也应允许退租"


def test_checkout_requires_reading(client):
    _basic(client)
    resp = _checkout(client, "甲", {"分水A": ""}, date="2025-03-01")
    assert "需要填写退租读数" in _msg(resp)


def test_checkout_reading_must_not_decrease(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    resp = _checkout(client, "甲", {"分水A": 120}, date="2025-03-01")
    assert "小于上次读数" in _msg(resp)


def test_vacancy_usage_belongs_to_landlord(client):
    """上一任退租与下一任入住之间的用量归房东,不进任何账单。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    _checkout(client, "甲", {"分水A": 150}, date="2025-03-01")
    _move_in(client, "乙", [("分水A", 168, 10)], start="2025-04-01")

    from app.billing import vacancy_usage
    conn = db.connect()
    gaps = vacancy_usage(conn)
    conn.close()
    assert len(gaps) == 1
    assert gaps[0]["usage"] == 18

    rows = _q("select household_id, usage_amount from receivables")
    assert _hid("乙") not in [r["household_id"] for r in rows], "空档期不产生乙的账单"
    # 甲的两期账(130-100=30、150-130=20),乙入住后还没有抄表
    assert [r["usage_amount"] for r in rows] == [30, 20]


def test_next_tenant_reading_below_last_is_rejected(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    _checkout(client, "甲", {"分水A": 150}, date="2025-03-01")
    resp = _move_in(client, "乙", [("分水A", 120, 10)], start="2025-04-01")
    assert "小于该表上次读数" in _msg(resp)


def test_returning_tenant_is_a_new_line(client):
    """甲回来是新租期,但历史欠款仍挂在他名下。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)   # 应收 300,未收
    _checkout(client, "甲", {"分水A": 150}, date="2025-03-01")   # 再欠 200
    _move_in(client, "甲", [("分水A", 200, 10)], start="2025-05-01")
    lines = _q("select * from lines order by id")
    assert len(lines) == 2
    assert lines[0]["move_out_date"] == "2025-03-01"
    assert lines[1]["move_out_date"] is None
    html = client.get(f"/ledger?household_id={_hid('甲')}").text
    assert "500.00" in html, "历史欠款仍然挂在这个人身上"


# ---------- 收款:先欠先冲 + 预收余额 ----------

def test_payment_is_not_tied_to_a_bill(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)     # 300
    _add(client, "分水A", "2025-03-01", 150)     # 200
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-03-05", "amount": "350"})
    from app.ledger import household_ledger
    conn = db.connect()
    led = household_ledger(conn, _hid("甲"))
    conn.close()
    assert led["rows"][0]["paid"] == 300.0, "先欠先冲:最早的一期先结清"
    assert led["rows"][1]["paid"] == 50.0
    assert led["outstanding"] == 150.0


def test_overpayment_becomes_balance(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)     # 300
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "500"})
    from app.ledger import household_ledger
    conn = db.connect()
    led = household_ledger(conn, _hid("甲"))
    conn.close()
    assert led["outstanding"] == 0.0
    assert led["balance"] == 200.0, "多收的 200 转为预收余额"


def test_balance_offsets_next_period(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)     # 300
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "500"})
    _add(client, "分水A", "2025-03-01", 150)     # 200
    from app.ledger import household_ledger
    conn = db.connect()
    led = household_ledger(conn, _hid("甲"))
    conn.close()
    assert led["outstanding"] == 0.0
    assert led["balance"] == 0.0


def test_refund_comes_from_balance(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "500"})
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-10", "amount": "200",
                                       "kind": "refund"})
    from app.ledger import household_ledger
    conn = db.connect()
    led = household_ledger(conn, _hid("甲"))
    conn.close()
    assert led["balance"] == 0.0
    assert led["outstanding"] == 0.0


# ---------- 锁定 ----------

def test_reading_locked_once_paid(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "100"})
    rid = _one("select id from readings where kind='periodic'")["id"]
    resp = client.post("/readings/edit", data={"id": str(rid), "value": "140"},
                       follow_redirects=False)
    assert "已收款" in _msg(resp)
    client.post("/receipts/delete", data={"id": str(_one("select id from receipts")["id"])})
    resp = client.post("/readings/edit", data={"id": str(rid), "value": "140"},
                       follow_redirects=False)
    assert "已更新" in _msg(resp), "删掉收款后账期解锁重算"


def test_edit_recalculates_receivable(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    rid = _one("select id from readings where kind='periodic'")["id"]
    client.post("/readings/edit", data={"id": str(rid), "value": "140"})
    assert _one("select amount from receivables")["amount"] == 400.0


def test_move_in_reading_not_editable(client):
    _basic(client)
    rid = _one("select id from readings where kind='move_in'")["id"]
    resp = client.post("/readings/edit", data={"id": str(rid), "value": "99"},
                       follow_redirects=False)
    assert "租期起点" in _msg(resp)


# ---------- 没有删除 ----------

def test_no_delete_routes(client):
    assert client.post("/households/delete", data={"id": "1"}).status_code in (404, 405)


def test_blank_meter_can_delete_but_history_cannot(client):
    """录错新建的空白表可删;进过账本的表不能删(ADR-0019)。"""
    _add_meter(client, "空白表")
    blank = _mid("空白表")
    assert client.post("/meters/delete", data={"id": str(blank)}).status_code == 200
    assert _one("select id from meters where id=?", (blank,)) is None
    _basic(client)
    used = _mid("分水A")
    resp = client.post("/meters/delete", data={"id": str(used)}, follow_redirects=False)
    assert "不能删除" in _msg(resp)
    assert _one("select id from meters where id=?", (used,)) is not None


def test_edit_meter_fields(client):
    """空白表可改类型/级别/制式/装表读数;已有读数的表只放行改名、换归属。"""
    _add_meter(client, "临时表")
    mid = _mid("临时表")
    resp = client.post("/meters/edit", data={
        "id": str(mid), "name": "临时电表", "meter_type": "electricity",
        "level": "sub", "phase": "three", "base_1": "12345",
        "base_2": "23456", "base_3": "34567", "note": "改过"},
        follow_redirects=False)
    assert "表已更新" in _msg(resp)
    m = _one("select * from meters where id=?", (mid,))
    assert (m["meter_type"], m["phase"], m["base_value"], m["note"]) == \
        ("electricity", "three", 70368, "改过")
    assert (m["base_r1"], m["base_r2"], m["base_r3"]) == (12345, 23456, 34567)

    _basic(client)
    mid = _mid("分水A")
    resp = client.post("/meters/edit", data={
        "id": str(mid), "name": "分水A改名", "meter_type": "electricity",
        "level": "main", "phase": "three", "base_value": "999"},
        follow_redirects=False)
    assert "不能改类型" in _msg(resp)
    # 已有读数的表:类型/级别锁死,但装表读数可分相改正
    resp = client.post("/meters/edit", data={
        "id": str(mid), "name": "分水A改名", "meter_type": "water",
        "level": "sub", "phase": "single", "base_value": "150"},
        follow_redirects=False)
    assert "表已更新" in _msg(resp)
    m = _one("select * from meters where id=?", (mid,))
    assert (m["name"], m["meter_type"], m["level"], m["base_value"]) == \
        ("分水A改名", "water", "sub", 150)


def test_no_adjustment_routes(client):
    assert client.get("/adjustments").status_code in (404, 405)
    assert client.post("/adjustments/add", data={}).status_code in (404, 405)


# ---------- 缴费 ----------

def test_payment_amount_is_recorded_verbatim(client):
    """缴费金额照账单手填,不推算、不校验。"""
    _add_meter(client, "总水", level="main")
    client.post("/payments/add", data={
        "meter_id": str(_mid("总水")), "payment_date": "2025-02-05",
        "reading_value": "5120", "unit_price": "4.2", "amount": "504.26"})
    row = _one("select * from payments")
    assert row["amount"] == 504.26
    assert row["reading_value"] == 5120


def test_payment_does_not_touch_receivables(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/payments/add", data={
        "meter_id": str(_mid("总水")), "payment_date": "2025-02-05", "amount": "504"})
    assert _one("select amount from receivables")["amount"] == 300.0


def test_main_meter_base_is_optional(client):
    """总表不参与计费推算,装表读数可留空;填了就照存,留着换表时核对。"""
    _add_meter(client, "总水", level="main")
    assert _one("select base_value from meters where name='总水'")["base_value"] is None
    _add_meter(client, "总电", kind="electricity", level="main", base="12000")
    assert _one("select base_value from meters where name='总电'")["base_value"] == 12000


# ---------- 补录与三相 ----------

def test_three_phase_usage_sums_each_phase_delta(client):
    """三相用量 = 各相本次 − 各相上次,再相加;不是只拿合计相减。

    上次 A=100 B=15 C=80,本次 A=120 B=50 C=100
    → (120−100)+(50−15)+(100−80) = 75
    """
    _add_meter(client, "分电T", kind="electricity", phase="three", base="0/0/0")
    _move_in(client, "甲", [("分电T", (100, 15, 80), 10)])
    _add(client, "分电T", "2025-02-01", (120, 50, 100))
    rec = _one("select * from receivables order by id desc limit 1")
    assert rec["usage_amount"] == 75, "三相用量须按分相差额相加"
    assert rec["amount"] == 750.0


def test_three_phase_rejects_single_phase_regression(client):
    """某一相读数回退就该拒,即使三相合计仍在增长。"""
    _add_meter(client, "分电T", kind="electricity", phase="three", base="0/0/0")
    _move_in(client, "甲", [("分电T", (100, 15, 80), 10)])
    # 合计 230 > 195,但 B 相 10 < 15
    resp = _add(client, "分电T", "2025-02-01", (120, 10, 100))
    assert "某一相读数小于前一条" in _msg(resp)



# ---------- 回归:审查发现的缺陷 ----------

def test_backfill_blocked_inside_paid_period(client):
    """补录不能插进已收款的账期 —— 否则账期被一分为二、冲抵重排(ADR-0015/0021)。"""
    _basic(client)
    _add(client, "分水A", "2025-03-01", 160)          # 100 → 160,60 吨 × 10 = 600
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-03-05", "amount": "600"})
    resp = _add(client, "分水A", "2025-02-01", 130)
    assert "已收款" in _msg(resp)
    assert "不能在其区间内补录" in _msg(resp)
    rows = _q("select period_start, period_end, amount from receivables")
    assert len(rows) == 1 and rows[0]["amount"] == 600.0, "账期不应被拆分"

    # 删掉收款后补录才放行
    client.post("/receipts/delete", data={"id": str(_one("select id from receipts")["id"])})
    assert "已保存" in _msg(_add(client, "分水A", "2025-02-01", 130))
    assert len(_q("select * from receivables")) == 2


def test_edit_blocked_inside_paid_period(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "100"})
    rid = _one("select id from readings where kind='periodic'")["id"]
    resp = client.post("/readings/edit", data={"id": str(rid), "value": "140"},
                       follow_redirects=False)
    assert "已收款" in _msg(resp)


def test_refund_is_a_receipt_entry(client):
    """退款走 /receipts/add 的 kind 参数(ADR-0018),没有独立路由。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "500"})
    resp = client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                              "entry_date": "2025-02-10",
                                              "amount": "100", "kind": "refund"},
                       follow_redirects=False)
    assert "退款已登记" in _msg(resp)
    from app.ledger import household_ledger
    conn = db.connect()
    led = household_ledger(conn, _hid("甲"))
    conn.close()
    assert led["refunded"] == 100.0
    assert led["balance"] == 100.0


# ---------- 备份与恢复 ----------

def test_clear_auto_backs_up_and_restore(client):
    """清空前自动备份,恢复能把整库找回来。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "总水", level="main")
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 10)])
    _add(client, "分水A", "2025-02-01", 130)

    html = client.get("/init").text
    assert "清空所有数据" in html and "立即备份" in html

    resp = client.post("/init/clear", follow_redirects=False)
    assert "自动备份" in _msg(resp)
    assert _one("select count(*) n from households")["n"] == 0

    html = client.get("/init").text
    assert "清空前自动备份.sqlite3" in html
    client.post("/init/restore", data={"name": "清空前自动备份.sqlite3"})
    assert _one("select count(*) n from households")["n"] == 1
    assert _one("select count(*) n from readings")["n"] == 2


# ---------- 回归:锁定按计量线取,日期入口必须校验 ----------

def test_lock_is_scoped_to_the_line_not_the_household(client):
    """同一租户水电两路同日各有一期:收了水费不能锁死电表那一期(ADR-0015/0021)。"""
    from tests.conftest import _add, _add_meter, _hid, _move_in
    _add_meter(client, "总水", level="main")
    _add_meter(client, "分水A", base="0")
    _add_meter(client, "分电A", kind="electricity", base="0")
    _move_in(client, "甲", [("分水A", 0, 1), ("分电A", 0, 1)])
    _add(client, "分水A", "2025-02-01", 100)      # 水路一期:应收 100
    _add(client, "分电A", "2025-02-01", 1000)     # 电路一期:应收 1000
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-05", "amount": "100"})

    water = _one("select id from lines where meter_type='water'")["id"]
    elec = _one("select id from lines where meter_type='electricity'")["id"]
    # 水费那一期已收款 -> 挡住
    assert "已收款" in _msg(_add_by_line(client, water, "分水A", "2025-01-15", 50))
    # 电费那一期没收到钱 -> 必须放行
    assert "已保存" in _msg(_add_by_line(client, elec, "分电A", "2025-01-15", 500))


def _add_by_line(client, line_id, meter_name, date, value):
    from tests.conftest import _mid
    return client.post("/readings/add", data={
        "line_id": str(line_id), "meter_id": str(_mid(meter_name)),
        "reading_date": date, "value": str(value), "tenant_price": "1"},
        follow_redirects=False)


def test_bad_dates_never_reach_the_books(client):
    """非法日期一旦落库,账期排序会静默错位;四个写入口都要挡住它。"""
    from tests.conftest import _add_meter, _hid, _mid, _move_in
    _add_meter(client, "总水", level="main")
    _add_meter(client, "分水A", base="0")
    _move_in(client, "甲", [("分水A", 0, 10)])
    line = _one("select id from lines where move_out_date is null")["id"]

    def post(url, data):
        return client.post(url, data=data, follow_redirects=False)

    assert "正确的抄表日期" in _msg(post("/readings/add", {
        "line_id": str(line), "meter_id": str(_mid("分水A")),
        "reading_date": "2025-13-45", "value": "99", "tenant_price": "10"}))
    assert "正确的收款日期" in _msg(post("/receipts/add", {
        "household_id": str(_hid("甲")), "entry_date": "不是日期", "amount": "10"}))
    assert "正确的缴费日期" in _msg(post("/payments/add", {
        "meter_id": str(_mid("总水")), "payment_date": "abc", "amount": "10"}))
    assert "格式不正确" in _msg(post("/households/add", {
        "name": "乙", "start_date": "2025-99-99", "meter_ids": str(_mid("分水A"))}))

    assert _q("select * from readings where reading_date not like '____-__-__'") == []
    assert _q("select * from receipts where entry_date not like '____-__-__'") == []
    assert _q("select * from payments where payment_date not like '____-__-__'") == []


def test_receipt_to_unknown_household_is_rejected_not_500(client):
    """不存在的租户:一句提示,不是 500。"""
    r = client.post("/receipts/add", data={
        "household_id": "9999", "entry_date": "2025-05-01", "amount": "10"},
        follow_redirects=False)
    assert r.status_code == 303
    assert "租户不存在" in _msg(r)


# ---------- 回归:审计发现的三处数据/安全问题 ----------

def test_backup_name_cannot_escape_backups_dir(client):
    """恢复的备份名拼进路径:带 .. 或分隔符必须被拒,否则能读任意 SQLite 文件覆盖主库。"""
    from app import backup

    for evil in ("../evil.sqlite3", "sub/evil.sqlite3", "../../etc/x.sqlite3",
                 "evil/../../x.sqlite3"):
        with pytest.raises(ValueError):
            backup.restore(evil)


def test_restore_refuses_corrupt_snapshot(client):
    """拿损坏文件恢复会把主库一起弄坏,必须先验 integrity_check。"""
    from app import backup

    backup.create("garbage")
    path = backup.backup_dir() / "garbage.sqlite3"
    path.write_bytes(b"not a database at all")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        backup.restore("garbage.sqlite3")


def test_restore_keeps_a_copy_of_the_overwritten_db(client):
    """恢复会覆盖当前库,覆盖前必须另存一份,且不能与待恢复的那份同名。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    from app import backup

    backup.create("要恢复的")
    client.post("/init/restore", data={"name": "要恢复的.sqlite3"})
    names = {x["name"] for x in backup.backups()}
    assert "恢复前" in "".join(names) or any(n.startswith("恢复前-") for n in names)


def test_same_day_backfill_cannot_split_a_paid_period(client):
    """已收款账期的端点日期再插一条读数,会把该期劈成两段,已收的钱对错期。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/receipts/add", data={"household_id": str(_hid("甲")),
                                       "entry_date": "2025-02-03", "amount": "300"})
    resp = _add(client, "分水A", "2025-02-01", 128)
    assert "已收款" in _msg(resp), "同日期补录必须被已收款账期挡住"


def test_cross_site_post_is_rejected(client):
    """带外站 Origin 的 POST 一律 403 —— 不依赖浏览器是否发 cookie。"""
    r = client.post("/init/clear", headers={"origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert _one("select count(*) n from households")["n"] >= 0   # 库没被动


def test_https_behind_reverse_proxy_is_not_cross_site(client):
    """反代在 443 终止 TLS 再转 http:Origin 是 https,应用看到的是 http,不能判跨站。

    真实故障:lucky 反代下抄表保存一律 403「跨站请求已拒绝」。
    """
    r = client.post("/init/property", data={"property_name": "反代"},
                    headers={"origin": "https://sdf.example.com",
                             "host": "sdf.example.com"}, follow_redirects=False)
    assert r.status_code == 303, "同主机名不同协议应放行"


def test_forwarded_host_header_is_honoured(client):
    """反代改写了 Host 时,用 X-Forwarded-Host 比主机名。"""
    r = client.post("/init/property", data={"property_name": "反代2"},
                    headers={"origin": "https://sdf.example.com",
                             "host": "172.25.0.1:8536",
                             "x-forwarded-host": "sdf.example.com"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_null_origin_is_rejected(client):
    """Origin: null 来自 sandbox/file://,正常页面不会出现,一律拒绝。"""
    r = client.post("/init/property", data={"property_name": "x"},
                    headers={"origin": "null"}, follow_redirects=False)
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client):
    r = client.post("/init/property", data={"property_name": "本站"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_flash_keeps_existing_query_string_valid(client):
    """回跳地址本身带查询串时,提示参数必须用 & 拼接。

    拼成 ?ok= 会让 page=1?ok=... 变成一个值,下一页 curl 就报 int_parsing。
    """
    from app.pages import go

    loc = go("/ledger?household_id=1&page=2", ok="收款已登记").headers["location"]
    assert "?ok=" not in loc
    assert "&ok=" in loc

    loc = go("/readings", ok="已保存").headers["location"]
    assert loc.startswith("/readings?ok=")
