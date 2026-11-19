"""各记录页面的分页与合计测试。"""

import re
import sqlite3

import app.db as db
from tests.conftest import _add, _ids, _setup, _table_rows


def _make_payments(client, meter_id, n=25):
    """按新接口造缴费:日期递增、读数递增,金额 = 增量 × 单价。"""
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    for i in range(1, n + 1):
        d = base + timedelta(days=i)
        client.post("/payments/add", data={
            "meter_id": str(meter_id),
            "payment_date": d.isoformat(),
            "reading_value": str(1000 + 10 * i),   # 每次增 10
            "unit_price": "1",
            "amount": "",
            "period_start": d.isoformat(),
            "period_end": d.isoformat(),
        })


def _make_switches(client, old_id, new_id, n=25):
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    start = conn.execute(
        "select value from readings where meter_id=? order by reading_date desc limit 1",
        (old_id,)).fetchone()[0]
    conn.close()
    for i in range(1, n + 1):
        client.post("/switches/add", data={
            "old_meter_id": str(old_id), "new_meter_id": str(new_id),
            "switch_date": f"2025-{(i - 1) % 12 + 1:02d}-{(i - 1) // 12 * 10 + 1:02d}",
            "old_final_value": str(start + i), "new_base_value": "0",
        })


def _make_adjustments(client, reading_id, household_id, n=25):
    for i in range(1, n + 1):
        client.post("/adjustments/add", data={
            "reading_id": str(reading_id),
            "household_id": str(household_id),
            "amount": str(i),
            "adjust_date": f"2025-{(i - 1) % 12 + 1:02d}-{(i - 1) // 12 * 10 + 1:02d}",
            "note": f"测试 {i}",
        })


def _assert_paginated(html, label, max_rows=20):
    rows = _table_rows(html).count("<tr>") - 1
    assert rows <= max_rows, f"{label} 单页渲染了 {rows} 行,应分页"


def test_payments_page_is_paginated(client):
    _setup(client)
    meters, _ = _ids(client)
    _make_payments(client, meters["总水"])
    html = client.get("/payments").text
    _assert_paginated(html, "缴费")
    assert "筛选金额合计" in html
    # 合计应覆盖全部 25 笔,而不是当前页
    total = 10 * 24   # 首笔无历史(增量 0),其余 24 笔各增 10
    assert f"{total:,.2f}" in html
    assert _table_rows(client.get("/payments?page=2").text) != _table_rows(html)


def test_switches_page_is_paginated(client):
    _setup(client)
    meters, _ = _ids(client)
    client.post("/meters/add", data={"name": "新水表", "meter_type": "water",
                                     "level": "sub", "main_meter_id": str(meters["总水"]),
                                     "base_date": "2025-01-01", "base_value": "0"})
    conn = sqlite3.connect(db.DB_PATH)
    new_id = conn.execute("select id from meters where name='新水表'").fetchone()[0]
    conn.close()
    _make_switches(client, meters["分水A"], new_id)
    html = client.get("/switches").text
    _assert_paginated(html, "换表")
    assert "共" in html and "页" in html


def test_adjustments_page_is_paginated(client):
    _setup(client)
    meters, households = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and value=130",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    client.post("/readings/bill", data={"id": str(rid)})
    _make_adjustments(client, rid, households["租户A"])
    html = client.get("/adjustments").text
    _assert_paginated(html, "冲正")
    # 合计 1+2+...+25 = 325
    assert "325.00" in html


def test_statistics_detail_is_paginated(client):
    _setup(client, houses="房东\n租户A", meters="总水,水,总,,\n分水A,水,分,总水,租户A")
    meters, _ = _ids(client)
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    for i in range(1, 26):
        _add(client, meters["分水A"], (base + timedelta(days=i)).isoformat(), 100 + i)
    html = client.get("/statistics").text
    _assert_paginated(html, "统计明细")
    # 合计必须覆盖全部 25 个账期
    assert "明细(共 25 条" in html
    page2 = client.get("/statistics?page=2").text
    assert _table_rows(page2) != _table_rows(html)


def test_pagination_keeps_filters(client):
    """翻页时筛选条件不能丢。"""
    _setup(client, houses="房东\n租户A", meters="总水,水,总,,\n分水A,水,分,总水,租户A")
    meters, _ = _ids(client)
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    for i in range(1, 26):
        _add(client, meters["分水A"], (base + timedelta(days=i)).isoformat(), 100 + i)
    html = client.get("/readings?billed=0").text
    m = re.search(r'href="(\?[^"]*page=2[^"]*)"', html)
    assert m, "未找到翻页链接"
    assert "billed=0" in m.group(1), f"翻页链接丢了筛选条件: {m.group(1)}"
