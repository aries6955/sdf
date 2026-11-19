"""账务:应收、实收、部分收款、欠款、退租结算。"""

import re
import sqlite3

import app.db as db
from tests.conftest import _add, _ids, _setup, _table_rows


def _bill(client, reading_id):
    return client.post("/readings/bill", data={"id": str(reading_id)}, follow_redirects=False)


def _reading_id(client, meter_name, value):
    meters, _ = _ids(client)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and value=?",
        (meters[meter_name], value)).fetchone()
    conn.close()
    return rid[0] if rid else None


def _setup_with_bill(client):
    """建一个已出账的租户:分水A 基数 100 → 抄表 130,30 吨 × 10 = 300 应收。"""
    _setup(client)
    meters, households = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    rid = _reading_id(client, "分水A", 130)
    _bill(client, rid)
    return meters, households, rid


def test_bill_creates_receivable(client):
    """出账后生成一条应收,金额 = 用量 × 租户单价。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    row = conn.execute(
        "select household_id, meter_id, usage_amount, unit_price, amount, period_end "
        "from receivables where reading_id=?", (rid,)).fetchone()
    conn.close()
    assert row is not None, "出账应生成应收"
    assert row[0] == households["租户A"]
    assert row[2] == 30 and row[4] == 300.0


def test_partial_receipt_creates_outstanding(client):
    """部分收款:收 100,应收 300,欠 200。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                       "receipt_date": "2025-02-10", "amount": "100"})
    html = client.get(f"/households/{households['租户A']}/ledger").text
    assert "300.00" in html   # 应收
    assert "100.00" in html   # 实收
    assert "200.00" in html   # 欠款
    assert "部分收款" in html


def test_multiple_receipts_until_settled(client):
    """多次收款累计到结清。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    for amt in ("100", "150", "50"):
        client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                           "receipt_date": "2025-02-10", "amount": amt})
    html = client.get(f"/households/{households['租户A']}/ledger").text
    assert "已结清" in html
    conn = sqlite3.connect(db.DB_PATH)
    total = conn.execute("select coalesce(sum(amount),0) from receipts where receivable_id=?",
                         (rec_id,)).fetchone()[0]
    conn.close()
    assert total == 300.0


def test_receipt_cannot_exceed_receivable(client):
    """收款不能超过应收。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    r = client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                           "receipt_date": "2025-02-10", "amount": "500"},
                    follow_redirects=False)
    from urllib.parse import unquote
    assert "超额" in unquote(r.headers["location"])
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("select count(*) from receipts").fetchone()[0]
    conn.close()
    assert n == 0


def test_checkout_blocked_when_outstanding(client):
    """有欠款不能退租。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                       "receipt_date": "2025-02-10", "amount": "100"})
    from urllib.parse import unquote
    r = client.post("/households/checkout", data={"id": str(households["租户A"])},
                    follow_redirects=False)
    assert "欠款" in unquote(r.headers["location"])
    conn = sqlite3.connect(db.DB_PATH)
    holder = conn.execute("select household_id from meters where name='分水A'").fetchone()[0]
    conn.close()
    assert holder == households["租户A"], "未结清不应解绑"


def test_checkout_after_settlement_unbinds_meter(client):
    """结清后退租:解绑表,台账历史保留。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                       "receipt_date": "2025-02-10", "amount": "300"})
    client.post("/households/checkout", data={"id": str(households["租户A"])})
    conn = sqlite3.connect(db.DB_PATH)
    holder = conn.execute("select household_id from meters where name='分水A'").fetchone()[0]
    n_rec = conn.execute("select count(*) from receivables").fetchone()[0]
    conn.close()
    assert holder is None, "退租应解绑表"
    assert n_rec == 1, "历史应收应保留"


def test_household_list_shows_outstanding(client):
    """租户列表显示应收/实收/欠款。"""
    meters, households, rid = _setup_with_bill(client)
    conn = sqlite3.connect(db.DB_PATH)
    rec_id = conn.execute("select id from receivables where reading_id=?", (rid,)).fetchone()[0]
    conn.close()
    client.post("/receipts/add", data={"receivable_id": str(rec_id),
                                       "receipt_date": "2025-02-10", "amount": "120"})
    html = client.get("/households").text
    assert "应收" in html and "实收" in html and "欠款" in html
    assert "180.00" in html  # 300 - 120


def test_next_tenant_base_must_not_be_below_last_reading(client):
    """新租户基数不得小于该表上次读数。"""
    _setup(client)
    meters, households = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    from urllib.parse import unquote
    r = client.post("/households/add", data={
        "name": "王五", "note": "", "start_date": "2025-03-01",
        "meter_ids": [str(meters["分水A"])],
        f"base_{meters['分水A']}": "120",
    }, follow_redirects=False)
    assert "小于该表上次读数" in unquote(r.headers["location"])
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("select count(*) from households where name='王五'").fetchone()[0]
    conn.close()
    assert n == 0, "校验失败不应留下半成品租户"


def test_tenant_cannot_take_main_meter(client):
    """租户不能绑定总表。"""
    _setup(client)
    meters, households = _ids(client)
    from urllib.parse import unquote
    r = client.post("/households/add", data={
        "name": "王五", "note": "", "start_date": "2025-03-01",
        "meter_ids": [str(meters["总水"])],
        f"base_{meters['总水']}": "2000",
    }, follow_redirects=False)
    assert "总表" in unquote(r.headers["location"])


def test_first_period_uses_tenant_base(client):
    """首期 = 抄表 − 租户基数;第二期 = 本期 − 上期。"""
    _setup(client, houses="房东\n租户A", meters="总水,水,总,,\n分水A,水,分,总水,租户A")
    meters, households = _ids(client)
    # setup 交接基数 100
    _add(client, meters["分水A"], "2025-02-01", 130)   # 首期 30
    _add(client, meters["分水A"], "2025-03-01", 180)   # 第二期 50
    rows = _table_rows(client.get("/statistics").text)
    assert ">30<" in rows.replace(" ", "") or "30" in rows
    assert "300.00" in rows   # 30 × 10
    assert "500.00" in rows   # 50 × 10
