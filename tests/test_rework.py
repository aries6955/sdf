"""本轮改动的测试:日期默认、缴费自动算金额、合计、筛选、入口归位。"""

import re
import sqlite3

import app.db as db
from tests.conftest import _add, _ids, _setup, _table_rows


def test_dates_default_to_today(client):
    """所有录入类日期框默认当天;筛选类日期框保持空(不筛选)。"""
    from datetime import date
    today = date.today().isoformat()
    # 录入类:必须默认今天
    assert f'name="reading_date" value="{today}"' in client.get("/readings").text
    assert f'name="payment_date" value="{today}"' in client.get("/payments").text
    assert f'name="reading_date" value="{today}"' in client.get("/meters").text
    assert f'name="switch_date" value="{today}"' in client.get("/switches").text
    assert f'name="adjust_date" value="{today}"' in client.get("/adjustments").text


def test_every_date_input_has_a_value(client):
    """不允许出现没有任何 value 的日期输入框(会显示空白)。"""
    import re
    for path in ["/readings", "/payments", "/meters", "/switches",
                 "/adjustments", "/statistics"]:
        html = client.get(path).text
        for tag in re.findall(r'<input[^>]*type="date"[^>]*>', html):
            assert "value=" in tag, f"{path} 有无默认值的日期框: {tag}"


def test_reading_form_has_no_department_price(client):
    """部门单价不应在抄表时录入;也不再有「作为起始读数」勾选。"""
    html = client.get("/readings").text
    add_form = re.search(r'action="/readings/add".*?</form>', html, re.S).group(0)
    assert "department_price" not in add_form
    assert "tenant_price" in add_form
    assert "is_initial" not in add_form
    assert "作为起始读数" not in add_form


def test_base_entry_lives_in_meters_page(client):
    _setup(client)
    html = client.get("/meters").text
    assert 'action="/meters/base"' in html
    assert "设置起始读数" in html


def test_payment_auto_computes_amount(client):
    """金额 = (本次读数 - 该总表上次缴费读数) × 单价。"""
    _setup(client)
    meters, _ = _ids(client)
    # 第一次缴费没有历史,只记读数与单价,金额为 0
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-01-05",
        "reading_value": "1000", "unit_price": "4.5", "amount": "",
        "period_start": "2025-01-01", "period_end": "2025-01-31",
    })
    # 第二次:1120 - 1000 = 120 × 4.5 = 540
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-02-05",
        "reading_value": "1120", "unit_price": "4.5", "amount": "",
        "period_start": "2025-02-01", "period_end": "2025-02-28",
    })
    conn = sqlite3.connect(db.DB_PATH)
    amount, rv, up = conn.execute(
        "select amount, reading_value, unit_price from payments order by id desc limit 1"
    ).fetchone()
    conn.close()
    assert (amount, rv, up) == (540.0, 1120, 4.5)


def test_payment_last_reading_ignores_meter_readings(client):
    """缴费的上次读数只取缴费历史,与抄表页的分表/总表读数无关。"""
    _setup(client)
    meters, _ = _ids(client)
    # 抄表页把总表读到 1120
    _add(client, meters["总水"], "2025-02-01", 1120)
    # 缴费第一次:没有缴费历史,所以增量为 0
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-02-10",
        "reading_value": "900", "unit_price": "4", "amount": "",
        "period_start": "2025-01-01", "period_end": "2025-02-01",
    })
    conn = sqlite3.connect(db.DB_PATH)
    amount = conn.execute("select amount from payments order by id desc limit 1").fetchone()[0]
    conn.close()
    # 若错误地取自抄表读数(1120),900 会被拒;这里应接受且增量为 0
    assert amount == 0.0


def test_payment_rejects_reading_below_last(client):
    _setup(client)
    meters, _ = _ids(client)
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-01-05",
        "reading_value": "1120", "unit_price": "4.5", "amount": "",
        "period_start": "2025-01-01", "period_end": "2025-01-31",
    })
    # 第二次填报 900,小于上次缴费读数 1120,应被拒绝
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-02-05",
        "reading_value": "900", "unit_price": "4.5", "amount": "",
        "period_start": "2025-02-01", "period_end": "2025-02-28",
    })
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("select count(*) from payments").fetchone()[0]
    conn.close()
    assert n == 1, "读数小于上次缴费读数应被拒绝"


def test_reading_records_show_amount_and_total(client):
    """抄表记录要显示金额,并在筛选卡给出金额合计。"""
    _setup(client)
    meters, _ = _ids(client)
    # 分水A 基数 100 → 130,增量 30,单价沿用 10 → 300
    _add(client, meters["分水A"], "2025-02-01", 130)
    html = client.get("/readings?billed=0").text
    assert "金额(元)" in html
    assert "金额合计" in html
    assert "300.00" in html


def test_payment_records_show_reading_unit_price(client):
    _setup(client)
    meters, _ = _ids(client)
    client.post("/payments/add", data={
        "meter_id": str(meters["总水"]), "payment_date": "2025-02-01",
        "reading_value": "1120", "unit_price": "4.5", "amount": "",
        "period_start": "2025-01-01", "period_end": "2025-02-01",
    })
    html = client.get("/payments").text
    assert "本次读数" in html and "单价" in html
    assert "1,120" in html or "1120" in html


def test_payment_filter_by_meter_and_date(client):
    _setup(client)
    meters, _ = _ids(client)
    for meter, rv in [("总水", 1120), ("总电", 5100) if "总电" in meters else (None, None)]:
        if not meter:
            continue
        client.post("/payments/add", data={
            "meter_id": str(meters[meter]), "payment_date": "2025-02-01",
            "reading_value": str(rv), "unit_price": "1", "amount": "",
            "period_start": "2025-01-01", "period_end": "2025-02-01",
        })
    rows = _table_rows(client.get(f"/payments?meter_id={meters['总水']}").text)
    assert "总水" in rows
    later = _table_rows(client.get("/payments?date_from=2025-03-01").text)
    assert "总水" not in later


def test_readings_filter_by_household(client):
    _setup(client)
    meters, households = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    _add(client, meters["分水B"], "2025-02-01", 260)
    html = client.get(f"/readings?household_id={households['租户A']}").text
    rows = _table_rows(html)
    assert "分水A" in rows
    assert "分水B" not in rows


def test_switches_and_adjustments_not_in_nav(client):
    """换表与冲正不再是独立导航项。"""
    html = client.get("/").text
    nav = re.search(r'<aside class="sidebar".*?</aside>', html, re.S).group(0)
    assert 'href="/switches"' not in nav
    assert 'href="/adjustments"' not in nav
    # 但仍可从各自所属页面进入
    assert 'href="/switches"' in client.get("/meters").text
    assert 'href="/adjustments"' in client.get("/statistics").text


def test_switch_entry_is_a_visible_button(client):
    """换表入口应是按钮,而不是埋在文字里的链接。"""
    html = client.get("/meters").text
    btn = re.search(r'<a class="btn" href="/switches">([^<]*)</a>', html)
    assert btn, "换表入口应为按钮样式"
    assert "换表" in btn.group(1)
    # 必须出现在表清单之前(顶部可见)
    assert html.find('href="/switches"') < html.find("表清单")


def test_adjustment_entry_is_in_filter_toolbar(client):
    """冲正入口应在筛选卡的动作行内,而不是页面最底部。"""
    html = client.get("/statistics").text
    assert 'href="/adjustments"' in html
    # 与查询/导出按钮同在一个 .actions 里
    actions = re.search(r'<div class="actions">.*?</div>', html, re.S)
    assert actions and 'href="/adjustments"' in actions.group(0), \
        "冲正入口应与查询按钮同排,而非页面底部"
    # 位置应明显靠前(在明细表之前)
    assert html.find('href="/adjustments"') < html.find("明细(")
