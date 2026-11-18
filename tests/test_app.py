import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as app_main
from app.db import connect
from app.main import SUPER_PASSWORD, app
from tests.conftest import (TEST_PASSWORD, _add, _ids, _setup, _table_rows)


def test_requires_login():
    with TestClient(app) as c:
        r = c.get("/households", follow_redirects=False)
        assert r.headers["location"] == "/login"


def test_all_pages_return_html_not_json(client):
    """每个页面路由必须声明 HTMLResponse,否则 str 会被序列化成 JSON 显示为源码。"""
    for path in ["/", "/households", "/meters", "/readings", "/switches",
                 "/payments", "/adjustments", "/statistics", "/init"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("text/html"), (path, r.headers["content-type"])
        assert "<!doctype html>" in r.text, path


def test_bad_password_rejected(client):
    r = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login")


def test_main_meter_never_charged_to_household(client):
    _setup(client)
    meters, _ = _ids(client)
    for name, value in [("总水", 1100), ("分水A", 130), ("分水B", 260)]:
        _add(client, meters[name], "2025-02-01", value)
    rows = _table_rows(client.get("/statistics").text)
    # 总表不产生任何租户账单行(既无整笔计费,也无差额行)
    assert "差额" not in rows
    assert "总水" not in rows
    # 只有两个分表各自计费:30×10 与 60×10
    assert rows.count("</tr>") - 1 == 2  # 扣除表头行
    assert "300.0" in rows and "600.0" in rows


def test_price_carried_from_previous_reading(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    dp, tp = conn.execute(
        "select department_price, tenant_price from readings where meter_id=? and is_initial=0",
        (meters["分水A"],),
    ).fetchone()
    conn.close()
    # 部门单价已移到缴费时录入(ADR-0009),抄表只继承租户单价
    assert tp == 10


def test_reading_must_increase(client):
    _setup(client)
    meters, _ = _ids(client)
    assert "msg=" in _add(client, meters["分水A"], "2025-02-01", 90).headers["location"]
    assert "msg=" in _add(client, meters["分水A"], "2024-12-01", 120).headers["location"]
    assert "msg=" not in (_add(client, meters["分水A"], "2025-02-01", 130).headers.get("location") or "")


def test_base_cannot_be_reset_after_readings(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    r = _add(client, meters["分水A"], "2025-03-01", 5, is_initial="1")
    assert "msg=" in r.headers["location"]


def test_clear_works_with_dependent_data(client):
    """有缴费/抄表等依赖数据时,清空也能正常完成(外键顺序正确)。"""
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["总水"], "2025-02-01", 1100)
    client.post("/payments/add", data={"meter_id": str(meters["总水"]),
                                       "payment_date": "2025-02-05", "amount": "500",
                                       "period_start": "2025-01-01", "period_end": "2025-02-01"})
    assert client.post("/init/clear").status_code == 200
    conn = sqlite3.connect(db.DB_PATH)
    for table in ["households", "meters", "readings", "payments", "adjustments", "settings"]:
        assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
    conn.close()


def test_billed_period_is_locked(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and value=130",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    client.post("/readings/bill", data={"id": str(rid)})
    client.post("/readings/edit", data={"id": str(rid), "value": "9999"})
    client.post("/readings/delete", data={"id": str(rid)})
    conn = sqlite3.connect(db.DB_PATH)
    value = conn.execute("select value from readings where id=?", (rid,)).fetchone()[0]
    conn.close()
    assert value == 130


def test_payment_does_not_lock_period(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and value=130",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    client.post("/payments/add", data={"meter_id": str(meters["总水"]),
                                       "payment_date": "2025-02-05", "amount": "500",
                                       "period_start": "2025-01-01", "period_end": "2025-02-01"})
    client.post("/readings/edit", data={"id": str(rid), "value": "140"})
    conn = sqlite3.connect(db.DB_PATH)
    value = conn.execute("select value from readings where id=?", (rid,)).fetchone()[0]
    conn.close()
    assert value == 140


def test_switch_keeps_usage_and_prices(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    client.post("/meters/add", data={"name": "分水A新", "meter_type": "water", "level": "sub",
                                    "main_meter_id": str(meters["总水"]), "household_id": "2"})
    conn = sqlite3.connect(db.DB_PATH)
    new_id = conn.execute("select id from meters where name='分水A新'").fetchone()[0]
    conn.close()
    client.post("/switches/add", data={"old_meter_id": str(meters["分水A"]), "new_meter_id": str(new_id),
                                       "switch_date": "2025-03-01", "old_final_value": "150",
                                       "new_base_value": "0"})
    _add(client, new_id, "2025-04-01", 60)
    # 旧表 130->150 与 新表 0->60 都应计入,且单价沿用 10
    rows = _table_rows(client.get("/statistics?date_from=2025-03-01").text)
    assert "分水A新" in rows
    # 新表的账期行单价应为沿用的 10.0(而非 0),金额 60×10=600
    new_row = next(r for r in rows.split("<tr>") if "分水A新" in r)
    assert "<td class=\"num\">10.0</td>" in new_row
    assert "600.00" in new_row
    # 旧表历史用量仍要能被查到,不能被换表丢掉
    assert "分水A" in client.get("/statistics").text


def test_switch_rejects_same_meter(client):
    _setup(client)
    meters, _ = _ids(client)
    before = sqlite3.connect(db.DB_PATH).execute("select count(*) from readings").fetchone()[0]
    client.post("/switches/add", data={"old_meter_id": str(meters["分水A"]),
                                       "new_meter_id": str(meters["分水A"]),
                                       "switch_date": "2025-03-01", "old_final_value": "150",
                                       "new_base_value": "0"})
    after = sqlite3.connect(db.DB_PATH).execute("select count(*) from readings").fetchone()[0]
    assert before == after


def _table_rows(html: str) -> str:
    """The result table only, excluding filter dropdown options."""
    start = html.find("<table>")
    end = html.find("</table>", start)
    return html[start:end]


def test_statistics_filters(client):
    _setup(client, meters="总水,水,总,,房东\n分水A,水,分,总水,租户A\n分水B,水,分,总水,租户B")
    meters, households = _ids(client)
    for name, value in [("总水", 1100), ("分水A", 130), ("分水B", 260)]:
        _add(client, meters[name], "2025-02-01", value)
    only_a = _table_rows(client.get(f"/statistics?household_id={households['租户A']}").text)
    assert "分水A" in only_a and "总水" not in only_a
    only_b = _table_rows(client.get(f"/statistics?meter_id={meters['分水B']}").text)
    assert "分水B" in only_b and "分水A" not in only_b
    later = _table_rows(client.get("/statistics?date_from=2025-03-01").text)
    assert "分水A" not in later
    billed_only = _table_rows(client.get("/statistics?billed=1").text)
    assert "分水A" not in billed_only


def test_unbilled_reading_is_editable_inline(client):
    """需求 9:未出账抄表必须可以在界面上修改。"""
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and reading_date>'2025-01-01'",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    # 行内编辑表单要能打开
    page = client.get(f"/readings?edit={rid}").text
    assert 'action="/readings/edit"' in page
    # 改读数与单价应生效
    client.post("/readings/edit", data={"id": str(rid), "value": "145",
                                        "department_price": "5", "tenant_price": "12"})
    conn = sqlite3.connect(db.DB_PATH)
    value, tp = conn.execute("select value, tenant_price from readings where id=?", (rid,)).fetchone()
    conn.close()
    assert (value, tp) == (145, 12)


def test_edit_rejects_crossing_neighbours(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    _add(client, meters["分水A"], "2025-03-01", 160)
    conn = sqlite3.connect(db.DB_PATH)
    first = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and value=130",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    # 把前一条改成大于后一条,应被拒绝
    client.post("/readings/edit", data={"id": str(first), "value": "999"})
    conn = sqlite3.connect(db.DB_PATH)
    value = conn.execute("select value from readings where id=?", (first,)).fetchone()[0]
    conn.close()
    assert value == 130


def test_billed_reading_cannot_be_edited(client):
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    conn = sqlite3.connect(db.DB_PATH)
    rid = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and value=130",
        (meters["分水A"],)).fetchone()[0]
    conn.close()
    client.post("/readings/bill", data={"id": str(rid)})
    client.post("/readings/edit", data={"id": str(rid), "value": "999"})
    conn = sqlite3.connect(db.DB_PATH)
    value = conn.execute("select value from readings where id=?", (rid,)).fetchone()[0]
    conn.close()
    assert value == 130
    # 已出账记录不应显示修改入口
    assert f"/readings?edit={rid}" not in client.get("/readings").text


def test_sidebar_layout_present(client):
    """导航必须在左侧栏,而不是顶部平铺。"""
    html = client.get("/statistics").text
    assert '<aside class="sidebar" id="sidebar">' in html
    assert 'class="shell"' in html
    assert 'class="topbar"' not in html
    # 侧栏只保留高频入口;换表与冲正已并入表设置/统计页
    for href in ["/", "/readings", "/payments",
                 "/statistics", "/households", "/meters", "/init"]:
        assert f'href="{href}"' in html, href


def test_nav_grouping(client):
    """租户/表设置/设置收入可折叠的系统面板;日常记账不含低频项。"""
    html = client.get("/").text
    sysnav = re.search(r'<nav class="sidegroup sysnav.*?</nav>', html, re.S).group(0)
    # 收起状态:首页时系统面板不含 open
    assert 'sysnav open' not in html
    for href in ["/households", "/meters", "/init"]:
        assert f'href="{href}"' in sysnav, f"{href} 应在系统面板"
    assert "表设置" in sysnav
    assert ">表具<" not in html
    # 系统面板在侧栏底部(位于退出登录之前)
    assert html.find('class="sysnav') < html.find('href="/logout"')
    # 日常记账只剩抄表与缴费
    plain = re.findall(r'<nav class="sidegroup"[^>]*aria-label="([^"]+)".*?</nav>', html, re.S)
    daily = re.search(r'aria-label="日常记账".*?</nav>', html, re.S).group(0)
    assert "/readings" in daily and "/payments" in daily
    assert "/switches" not in daily and "/adjustments" not in daily
    # 查询组仍在
    query = re.search(r'aria-label="查询".*?</nav>', html, re.S).group(0)
    assert "/statistics" in query
    # 展开按钮存在且默认 aria-expanded=false
    assert 'id="sysNavToggle"' in html
    assert 'aria-expanded="false"' in html


def test_sysnav_auto_expands_on_system_pages(client):
    """当前页属于系统组时,面板自动展开并高亮。"""
    html = client.get("/init").text
    assert 'sysnav open' in html
    assert 'aria-current="page"' in html
    assert 'aria-expanded="true"' in html


def test_sample_data_billing_is_correct(client):
    """示例数据的计费逻辑必须可核对。"""
    client.post("/init/sample")
    rows = _table_rows(client.get("/statistics").text)
    # 张三水 1 月: 232-200=32 × 5.0 = 160.00
    assert "160.00" in rows
    # 张三水 2 月: 268-232=36 × 5.5 = 198.00(已调价)
    assert "198.00" in rows
    # 李四水 2 月: 205-176=29 × 5.5 = 159.50
    assert "159.50" in rows
    # 张三电 2 月: 1260-1120=140 × 1.1 = 154.00
    assert "154.00" in rows
    # 总表不得出现在租户账单里
    assert "总水表" not in rows and "总电表" not in rows
    # 李四没有电表,不应有电费行
    assert not any("李四" in r and "电" in r for r in rows.split("<tr>"))


def test_sample_arithmetic_totals(client):
    client.post("/init/sample")
    html = client.get("/statistics").text
    stats = dict(re.findall(r'<div class="k">(.*?)</div><div class="v">(.*?)</div>', html))
    assert "161" in stats["用水量合计"]      # 32+36+26+29+18+20
    assert "260" in stats["用电量合计"]      # 120+140
    assert "1,121.50" in stats["账单金额"]   # 各笔之和
    assert "25.00" in stats["冲正合计"]
    assert "1,146.50" in stats["应收净额"]


def test_clear_all_data(client):
    client.post("/init/sample")
    assert sqlite3.connect(db.DB_PATH).execute("select count(*) from households").fetchone()[0] > 0
    client.post("/init/clear")
    conn = sqlite3.connect(db.DB_PATH)
    for table in ["households", "meters", "readings", "payments", "adjustments", "settings"]:
        assert conn.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
    conn.close()


def test_mojibake_is_detected(client):
    """写入时就已损坏的数据要在初始化页被指出。"""
    with connect() as conn:
        conn.execute("insert into households(name) values (?)", ("\u00e5\u00bc\u00a0\u00e4\u00b8\u0089",))
    html = client.get("/init").text
    assert "可能是乱码" in html


def test_inline_edit_colspan_matches_header(client):
    """行内编辑的 colspan 必须等于表头列数,否则右侧会缺一块。"""
    client.post("/init/sample")
    conn = sqlite3.connect(db.DB_PATH)
    meter_id = conn.execute("select id from meters where name='101水表'").fetchone()[0]
    reading_id = conn.execute(
        "select id from readings where meter_id=? and is_initial=0 and billed=0",
        (meter_id,)).fetchone()[0]
    conn.close()

    for path, marker in [(f"/meters?edit={meter_id}", "/meters/edit"),
                         (f"/readings?edit={reading_id}", "/readings/edit")]:
        html = client.get(path).text
        heads = re.search(r"<thead><tr>(.*?)</tr></thead>", html, re.S).group(1)
        cols = heads.count("<th")
        row = re.search(r'<tr class="editrow">.*?<td colspan="(\d+)"', html, re.S)
        assert row, f"{path} 未找到编辑行"
        assert int(row.group(1)) == cols, f"{path} colspan={row.group(1)} 但表头 {cols} 列"
        assert marker in html


def test_edit_fields_align_in_one_row(client):
    """表具编辑行的 4 个字段必须同处一行(左右位置递增、top 相同)。"""
    client.post("/init/sample")
    conn = sqlite3.connect(db.DB_PATH)
    meter_id = conn.execute("select id from meters where name='101水表'").fetchone()[0]
    conn.close()
    html = client.get(f"/meters?edit={meter_id}").text
    row = re.search(r'<tr class="editrow">.*?</tr>', html, re.S).group(0)
    assert row.count('class="field"') == 3  # 归属租户已移除
    # 只允许「所属总表」带说明文字,其余不带
    assert row.count('class="assist"') == 1


def test_only_one_clear_button_in_init(client):
    """初始化页只能有一个清空数据按钮,避免重复。"""
    html = client.get("/init").text
    assert html.count('action="/init/clear"') == 1
    assert html.count('<button class="danger" type="submit">清空所有数据</button>') == 1
    assert "清除所有数据" not in html


def test_clear_requires_confirmation(client):
    """清空按钮必须带 confirm 确认框,不能直接提交。"""
    html = client.get("/init").text
    block = re.search(r'<form method="post" action="/init/clear"(.*?)</form>', html, re.S).group(1)
    assert "confirm(" in block
    assert "无法撤销" in block


def test_init_page_has_no_bulk_textareas(client):
    """批量录入文本框已移除,改用租户/表设置页面与抄表页。"""
    html = client.get("/init").text
    for action in ["/init/households", "/init/meters", "/init/bases"]:
        assert action not in html, action
    assert html.count("<textarea") == 0
    # 仍保留示例数据与房产名称
    assert 'action="/init/sample"' in html
    assert 'action="/init/property"' in html


def test_change_password(client):
    """界面改密码:验证旧密码、新密码写入文件。"""
    pw_file = app_main.password_path()
    # 旧密码错误应被拒
    client.post("/init/password", data={"old_password": "wrong",
                                        "new_password": "newpass1",
                                        "confirm_password": "newpass1"})
    assert "newpass1" not in pw_file.read_text(encoding="utf-8")
    # 两次不一致应被拒
    client.post("/init/password", data={"old_password": TEST_PASSWORD,
                                        "new_password": "newpass1",
                                        "confirm_password": "newpass2"})
    assert "newpass1" not in pw_file.read_text(encoding="utf-8")
    # 正确修改
    client.post("/init/password", data={"old_password": TEST_PASSWORD,
                                        "new_password": "newpass1",
                                        "confirm_password": "newpass1"})
    assert pw_file.read_text(encoding="utf-8").strip() == "newpass1"


def test_new_password_works_for_login(client):
    client.post("/init/password", data={"old_password": TEST_PASSWORD,
                                        "new_password": "changed9",
                                        "confirm_password": "changed9"})
    fresh = TestClient(app)
    r = fresh.post("/login", data={"password": "changed9"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_reject_password_equal_to_super(client):
    pw_file = app_main.password_path()
    client.post("/init/password", data={"old_password": TEST_PASSWORD,
                                        "new_password": SUPER_PASSWORD,
                                        "confirm_password": SUPER_PASSWORD})
    assert pw_file.read_text(encoding="utf-8").strip() != SUPER_PASSWORD


def test_super_password_recovers_access():
    """忘记密码时,用超级密码能登录并直达改密码页。"""
    fresh = TestClient(app)
    r = fresh.post("/login", data={"password": SUPER_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/init")
    assert "set-cookie" in {k.lower() for k in r.headers}


def test_sidebar_has_collapse_control(client):
    html = client.get("/").text
    assert 'id="collapseBtn"' in html
    assert "sdf_sidebar_collapsed" in html
    assert 'body.collapsed' in client.get("/static/style.css").text


def test_html_pages_are_not_cached(client):
    """页面不得被浏览器缓存,否则改动后仍展示旧界面。"""
    for path in ["/", "/init", "/readings", "/statistics"]:
        r = client.get(path)
        assert r.headers["content-type"].startswith("text/html")
        cache = r.headers.get("cache-control", "")
        assert "no-store" in cache, f"{path} 缺少 no-store: {cache!r}"


def test_pdf_export(client):
    _setup(client)
    meters, _ = _ids(client)
    for name, value in [("总水", 1100), ("分水A", 130)]:
        _add(client, meters[name], "2025-02-01", value)
    for query in ["type=water", "type=electricity", "type=all", "type=water&billed=1"]:
        r = client.get("/statistics/pdf?" + query)
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
