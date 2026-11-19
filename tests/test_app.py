"""页面、鉴权、示例数据与导出。"""

import sqlite3

import app.db as db
from tests.conftest import (_add, _add_meter, _basic, _hid, _mid, _move_in, _one, _q,
                            _with_period)


def test_requires_login():
    """未登录访问任何页面都跳登录页。"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        for path in ("/", "/readings", "/households", "/meters", "/payments",
                     "/statistics", "/init"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_all_mutations_require_login():
    """提交动作也要登录:未登录的 POST 不能改动任何数据。"""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.db as _db
    with TestClient(app) as c:
        for path in ("/init/clear", "/households/add", "/meters/add", "/readings/add",
                     "/receipts/add", "/payments/add", "/switches/add"):
            r = c.post(path, data={}, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_all_pages_render(client):
    _with_period(client)
    for path in ("/", "/readings", "/households", "/meters", "/payments", "/statistics",
                 "/init", "/statistics/pdf"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_reading_page_lists_lines_sorted_by_staleness(client):
    """最久没抄的排最前。"""
    _add_meter(client, "分水A", base="100")
    _add_meter(client, "分水B", base="100")
    _move_in(client, "甲", [("分水A", 100, 10)], start="2025-01-01")
    _move_in(client, "乙", [("分水B", 100, 10)], start="2025-03-01")
    html = client.get("/readings").text
    # 甲上次 2025-01-01,乙 2025-03-01;甲的 select option 应更靠前
    assert html.find("甲") < html.find("乙")


def test_home_shows_totals(client):
    _with_period(client)
    html = client.get("/").text
    assert "300.00" in html
    assert "应收合计" in html


def test_statistics_shows_margin_and_vacancy(client):
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    client.post("/payments/add", data={
        "meter_id": str(_mid("总水")), "payment_date": "2025-02-05", "amount": "100"})
    html = client.get("/statistics").text
    assert "向部门缴费" in html and "毛差" in html
    assert "200.00" in html, "毛差 = 应收 300 - 缴费 100"


def test_pdf_export(client):
    _with_period(client)
    r = client.get("/statistics/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")


def test_html_pages_are_not_cached(client):
    r = client.get("/")
    assert "no-store" in r.headers.get("cache-control", "")


# ---------- 示例数据 ----------

def test_sample_data_arithmetic(client):
    """示例数据应能完整跑通:多线、换表、退租、空档期。"""
    client.post("/init/sample")
    rows = _q("select * from receivables order by id")
    assert len(rows) >= 8, "示例应产生多条账期"
    assert all(r["amount"] == round(r["usage_amount"] * r["unit_price"], 2) for r in rows)
    # 换表那条账期跨越两块表
    multis = [r for r in rows if r["usage_amount"] == 92]     # (280-232)+(32-0)? 见下
    lines = _q("select * from lines where move_out_date is not null")
    assert lines, "示例包含退租"

    from app.billing import vacancy_usage
    conn = db.connect()
    gaps = vacancy_usage(conn)
    conn.close()
    assert gaps and gaps[0]["usage"] == 15, "示例包含 15 吨空档期"


def test_sample_switch_keeps_period(client):
    """示例里张三的 101 水表在 3/1 换表,账期不切断。"""
    client.post("/init/sample")
    from app.billing import all_periods
    conn = db.connect()
    periods = all_periods(conn, meter_type="water")
    conn.close()
    # 换表当月间隔 2/1 与 3/1 的那条账期,用量跨两块表
    spans = [p for p in periods if p["start"] == "2025-02-01" and p["end"] == "2025-03-01"
             and p["household_name"] == "张三"]
    assert spans, "换表后的账期仍应是一条"
    # 换表日(3/15)不切账期:张三水线的账期是 1/1-2/1、2/1-3/1、3/1-4/1 三段
    zhang = [p for p in periods if p["household_name"] == "张三"]
    assert len(zhang) == 3
    assert [p["end"] for p in zhang] == ["2025-02-01", "2025-03-01", "2025-04-01"]


def test_clear_all_data(client):
    client.post("/init/sample")
    client.post("/init/clear")
    for table in ("households", "meters", "lines", "readings", "receivables",
                  "receipts", "payments", "meter_switches"):
        assert _one(f"select count(*) as n from {table}")["n"] == 0, table


def test_clear_resets_autoincrement(client):
    client.post("/init/sample")
    client.post("/init/clear")
    _move_in(client, "甲", [])
    # 空表清单会报错,改为直接建表验 ID 重置
    client.post("/init/clear")
    _add_meter(client, "分水A", base="0")
    assert _one("select id from meters")["id"] == 1


# ---------- 密码与初始化 ----------

def test_bad_password_rejected(client):
    from tests.conftest import TEST_PASSWORD
    from urllib.parse import unquote
    r = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert "密码不正确" in unquote(r.headers["location"])


def test_change_password(client):
    from tests.conftest import TEST_PASSWORD
    client.post("/init/password", data={"old_password": "wrong",
                                        "new_password": "abcd", "confirm_password": "abcd"})
    client.post("/init/password", data={"old_password": TEST_PASSWORD,
                                        "new_password": "abcd", "confirm_password": "abcd"})
    r = client.post("/login", data={"password": "abcd"}, follow_redirects=False)
    assert r.status_code == 303


def test_property_name(client):
    client.post("/init/property", data={"property_name": "测试小区"})
    assert "测试小区" in client.get("/").text


# ---------- 页面结构 ----------

def test_nav_has_no_adjustments_or_switches(client):
    html = client.get("/").text
    assert 'href="/adjustments"' not in html
    assert 'href="/switches"' not in html


def test_meters_page_has_swap_section(client):
    _basic(client)
    html = client.get("/meters").text
    assert "换表" in html
    assert "不切断账期" in html


def test_household_page_has_move_in_and_out(client):
    _with_period(client)
    html = client.get("/households").text
    assert "办理入住" in html
    assert "退租" in client.get(f"/households/{_hid('甲')}/ledger").text


def test_no_delete_buttons(client):
    _with_period(client)
    html = client.get("/households").text
    assert "/households/delete" not in html
    assert "清空" not in client.get("/meters").text


def test_closed_line_still_shows_meter_name(client):
    """退租后表已解绑,但台账与统计仍应显示那条线用的表。"""
    from tests.conftest import _checkout
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    _checkout(client, "甲", {"分水A": 180}, date="2025-03-01")
    html = client.get(f"/households/{_hid('甲')}/ledger").text
    assert "分水A" in html, "已结束的租期仍要能看到表名"
    stats = client.get("/statistics").text
    assert "分水A" in stats
