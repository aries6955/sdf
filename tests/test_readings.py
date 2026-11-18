"""抄表页筛选/统计/分页,以及首页导航入口的测试。"""

import re
import sqlite3

import app.db as db
from tests.test_app import _add, _ids, _setup, _table_rows


def test_home_link_exists_on_every_page(client):
    """登录后任意页面都能回到首页概览。"""
    for path in ["/readings", "/payments", "/switches", "/adjustments",
                 "/statistics", "/households", "/meters", "/init"]:
        html = client.get(path).text
        assert 'href="/"' in html, f"{path} 缺少回首页入口"
        assert "首页概览" in html, f"{path} 缺少首页概览链接"


def test_home_nav_is_active_on_index(client):
    html = client.get("/").text
    assert re.search(r'class="navlink active" href="/"', html)


def test_entry_form_is_above_records(client):
    """录入表单必须排在抄表记录表格之前。"""
    _setup(client)
    html = client.get("/readings").text
    form_pos = html.find('action="/readings/add"')
    table_pos = html.find("抄表记录 ·")
    assert form_pos != -1 and table_pos != -1
    assert form_pos < table_pos, "录入抄表应位于抄表记录上方"


def test_records_are_paginated_not_300(client):
    """记录必须分页,不能一次渲染几百条把页面拉长。"""
    _setup(client, meters="总水,水,总,,\n分水A,水,分,总水,租客A",
           houses="房东\n租客A")
    meters, _ = _ids(client)
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    for i in range(1, 26):
        _add(client, meters["分水A"], (base + timedelta(days=i)).isoformat(), 100 + i * 10)
    html = client.get("/readings").text
    rows = _table_rows(html).count("<tr>") - 1  # 扣除表头
    assert rows <= 20, f"单页渲染了 {rows} 行,应分页"
    assert "页" in html
    # 第二页应存在且内容不同
    page2 = client.get("/readings?page=2").text
    assert _table_rows(page2) != _table_rows(html)


def test_filter_by_meter(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    _add(client, meters["分水B"], "2025-02-01", 260)
    html = client.get(f"/readings?meter_id={meters['分水A']}").text
    rows = _table_rows(html)
    assert "分水A" in rows
    assert "分水B" not in rows


def test_filter_by_status(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute("select id from readings where meter_id=? and is_initial=0",
                       (meters["分水A"],)).fetchone()[0]
    conn.close()
    client.post("/readings/bill", data={"id": str(rid)})

    billed_rows = _table_rows(client.get("/readings?billed=1").text)
    assert "分水A" in billed_rows
    unbilled_rows = _table_rows(client.get("/readings?billed=0").text)
    assert "分水A" not in unbilled_rows


def test_filter_by_date_range(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    _add(client, meters["分水A"], "2025-04-01", 160)
    assert "分水A" in _table_rows(client.get("/readings?date_from=2025-03-01").text)
    html = _table_rows(client.get("/readings?date_to=2025-02-28").text)
    assert "2025-04-01" not in html


def test_totals_reflect_filter_not_page(client):
    """合计必须基于全部命中记录,而不是当前页。"""
    _setup(client, houses="房东\n租客A",
           meters="总水,水,总,,\n分水A,水,分,总水,租客A")
    meters, _ = _ids(client)
    # 用递进的唯一日期,避免重复日期被校验拦掉
    from datetime import date, timedelta
    base = date(2025, 1, 1)
    values = []
    for i in range(1, 26):
        d = base + timedelta(days=i)
        v = 100 + i
        values.append(v)
        _add(client, meters["分水A"], d.isoformat(), v)
    html = client.get("/readings?billed=0").text
    m = re.search(r"命中记录</div><div class=\"v\">(\d+)", html)
    total = re.search(r"读数合计</div><div class=\"v\">([\d,]+)", html)
    assert m and int(m.group(1)) == 25, m.group(1) if m else "未找到命中记录"
    assert total and int(total.group(1).replace(",", "")) == sum(values), (
        total.group(1) if total else "未找到读数合计")
