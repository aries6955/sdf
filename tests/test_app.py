"""页面、鉴权、示例数据与导出。"""

import app.db as db
from tests.conftest import (
    _add,
    _add_meter,
    _basic,
    _checkout,
    _hid,
    _mid,
    _move_in,
    _one,
    _q,
    _with_period,
)
from tests.htmlq import block, block_of, heading_pos


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
    # 只看抄表表单:筛选与记录表里也会出现租户名
    form = block(html, "新增抄表")
    # 甲上次 2025-01-01,乙 2025-03-01;甲的 select option 应更靠前
    assert form.find("甲") < form.find("乙")


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
    from urllib.parse import unquote
    r = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert "密码不正确" in unquote(r.headers["location"])


def test_change_password(client):
    """页面不再写密码文件:提交后引导去改 .env,登录仍用旧密码。"""
    from urllib.parse import unquote

    from tests.conftest import TEST_PASSWORD
    r = client.post("/init/password", follow_redirects=False)
    assert "SDF_PASSWORD" in unquote(r.headers["location"])
    ok = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert ok.headers["location"] == "/"


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


def test_household_page_has_new_household_form(client):
    from tests.conftest import _add_meter
    _with_period(client)
    # 新建租户是名单上方的按钮 + 弹窗,不再是独立卡片
    _add_meter(client, "分电三相", kind="electricity", phase="three", base="100/100/100")
    html = client.get("/households").text
    assert 'data-open="newHousehold"' in html
    assert 'id="newHousehold"' in html
    # 三相表入住读数三个框;单价框用 value= 而不是 placeholder=
    assert 'name="base_' in html and '_1"' in html
    assert 'name="price_' in html and 'value="' in html
    assert 'placeholder="留空=沿用上次"' not in html
    assert "退租" in html


def test_tenant_can_only_pick_sub_meters(client):
    """总表不参与租户计费,候选表里不出现;即使硬塞也拒收。"""
    _with_period(client)
    _add_meter(client, "分水甲", kind="water", level="sub", base="0")
    html = client.get("/households").text
    assert "总水" not in html       # 总表不在候选表清单里
    main_id = _mid("总水")
    r = client.post("/households/add", data={
        "name": "偷选总表", "start_date": "2026-01-01", "meter_ids": main_id,
        f"base_{main_id}": "0"}, follow_redirects=False)
    assert "location" in r.headers, "应带错误提示跳回租户页"


def test_mislabeled_meter_with_children_is_not_offerable(client):
    """总表被误存成分表时,只要下面挂着分表,也不能给租户用。

    正常入口拦得住(meter_add 要求父表是总表),这里直接改库模拟历史脏数据。
    """
    import sqlite3

    import app.db as db
    from tests.conftest import _add_meter, _hid, _mid

    _add_meter(client, "汇总表", level="sub", base="0")
    _add_meter(client, "真总表", level="main", base="")
    _add_meter(client, "分表A", level="sub", base="0", main="真总表")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE meters SET main_meter_id=? WHERE name='分表A'",
                 (_mid("汇总表"),))   # 硬造:分表挂在一张 sub 表下面
    conn.commit()
    conn.close()

    html = client.get("/households").text
    assert "汇总表" not in html, "有子表的汇总表不该出现在候选清单"
    before = _hid("硬塞")
    client.post("/households/add", data={
        "name": "硬塞", "start_date": "2026-01-01",
        "meter_ids": [_mid("汇总表")], f"base_{_mid('汇总表')}": "0"},
        follow_redirects=False)
    assert before is None and _hid("硬塞") is None, "应被拒绝,不留半个租户"


def test_edit_household_modal_lists_name_and_current_lines(client):
    """修改弹窗:名称/入住日期只读,可补挂空表。"""
    _with_period(client)
    html = client.get("/households").text
    assert 'id="editHousehold"' in html
    assert 'data-edit-hh="' in html
    assert 'id="hh-edit-name"' in html and "disabled" in html
    assert 'action="/households/edit"' in html


def test_reading_form_groups_by_household_and_batch_saves(client):
    """新增抄表先选租户,下方按该户的表逐块填,一次提交全部保存。"""
    from tests.conftest import _one

    _basic(client)                                    # 甲 入住 分水A 100
    html = client.get("/readings").text
    assert 'name="household_id"' in html
    # 只显示选中租户的那一组
    assert 'data-hh="' in html
    html.count("data-hh=") >= 1

    lid = _one("select id from lines where move_out_date is null")["id"]
    mid = _mid("分水A")
    r = client.post("/readings/add", data={
        "household_id": "1", "reading_date": "2025-02-01",
        "value_%d" % lid: "130", "meter_%d" % lid: str(mid),
        "price_%d" % lid: "10"}, follow_redirects=False)
    loc = r.headers["location"]
    assert "ok=" in loc, loc
    assert _one("select count(*) as n from readings where kind='periodic'")["n"] == 1


def test_reading_record_renamed_and_nav_flattened(client):
    """读数记录改叫抄表记录;侧栏平铺,徒步入口改名。"""
    _basic(client)
    html = client.get("/readings").text
    assert "<h2>抄表记录" in html
    assert "<h2>读数记录" not in html
    assert ">日常管理</span>" in html
    assert ">总览</span>" not in html and ">日常记账</span>" not in html
    assert ">查询</span>" not in html
    assert ">账务管理</span>" in client.get("/").text


def test_payment_amount_autofills_from_reading_and_price(client):
    """缴费金额默认由 (账单读数 - 上次读数) × 单价 算出,且可手改。"""
    _add_meter(client, "总水", level="main")
    client.post("/payments/add", data={"meter_id": str(_mid("总水")),
                                       "payment_date": "2025-01-01",
                                       "reading_value": "100", "unit_price": "2",
                                       "amount": "200"})
    html = client.get("/payments").text
    # 上次账单读数交给前端算用量(渲染成 meter_id -> 上次读数 的映射)
    assert 'var prev = {' in html
    assert '"%s": 100' % _mid("总水") in html
    assert "本期用量" in html
    assert "可直接改" in html
    assert 'id="p-calc"' in html


def test_three_phase_last_reading_falls_back_to_meter_base(client):
    """三相表没存分相时,上次读数退回装表读数,不显示「— / — / —」。"""
    from tests.conftest import _mid, _move_in
    _add_meter(client, "三相电", kind="electricity", phase="three", base="10/10/5")
    _move_in(client, "甲", [("三相电", (10, 10, 5), 1)])
    html = client.get("/readings").text
    form = html[html.index('action="/readings/add"'):]
    assert "10 / 10 / 5" in form
    assert "— / — / —" not in form
    assert _mid("三相电")


def test_readings_filter_lives_in_record_card(client):
    """筛选条挪进「抄表记录」卡片,不再挂在账期合计上。"""
    _basic(client)
    html = client.get("/readings").text
    assert 'class="filterbar"' in html
    assert "filterbar" not in block(html, "账期合计")
    assert "filterbar" in block(html, "抄表记录")


def test_new_tenant_requires_unit_price(client):
    """单价必须填:留空会让第一次抄表按 0 计费。"""
    from tests.conftest import _hid, _mid
    _add_meter(client, "分水A", base="100")
    # 不填 price_<id>
    r = client.post("/households/add", data={
        "name": "没单价", "start_date": "2025-01-01",
        "meter_ids": [_mid("分水A")], f"base_{_mid('分水A')}": "100"},
        follow_redirects=False)
    from urllib.parse import unquote
    loc = unquote(r.headers["location"])
    assert "请填写租户单价" in loc, loc
    assert _hid("没单价") is None, "该拒绝,不留半个租户"

    # 表单里单价框标记必填
    html = client.get("/households").text
    assert 'placeholder="必填"' in html


def test_meter_type_tags_have_water_and_electricity_styles(client):
    """水=浅蓝底黑字,电=淡红底白字。"""
    _add_meter(client, "总分水", level="main", kind="water")
    _add_meter(client, "总分电", level="main", kind="electricity")
    html = client.get("/meters").text
    assert '<span class="tag water">水</span>' in html
    assert '<span class="tag electricity">电</span>' in html


def test_statistics_filter_sits_in_detail_card(client):
    """统计页的筛选在「账期明细」卡片里,不与汇总卡同块。"""
    _with_period(client)
    html = client.get("/statistics").text
    detail = block(html, "账期明细")
    assert 'class="filterbar"' in detail
    # 汇总卡在前、筛选在后:筛选属于明细,不属于汇总
    assert html.index("filterbar") > html.index('class="stats"')


def test_home_next_steps_are_cards(client):
    """首页「下一步」用卡片展示状态,不再是项目符号列表。"""
    _with_period(client)
    html = client.get("/").text
    assert 'class="stepcards"' in html
    assert html.count('class="stepcard"') == 4
    assert 'class="tasklist"' not in html
    for key in ("新增抄表", "待收账款", "预收余额", "抄表记录"):
        assert key in html, key


def test_ledger_totals_on_top_filter_in_detail_and_receipt_shows_bills(client):
    """汇总卡在上、筛选进台账明细、收款选租户带出账单与欠款合计。"""
    _with_period(client)      # 甲 应收 300
    html = client.get("/ledger").text
    # 1) 汇总卡在「登记收款」之前(页面顶部)
    # 汇总卡在「登记收款」卡片之上
    assert html.index("应收合计") < heading_pos(html, "登记收款")
    # 2) 筛选在台账明细卡片里,不在独立区域
    assert 'class="filterbar"' in block(html, "台账明细")
    # 3) 收款表单带租户 --> 账单清单与欠款合计
    assert 'id="rc-bills"' in html
    assert "var owed=" in html and "totals=" in html
    assert "自动带出欠款合计" in html


def test_errors_show_in_modal_not_top_banner(client):
    """操作失败用弹窗讲清楚;成功只留顶部一条提示,不打断。"""
    from tests.conftest import _mid
    _basic(client)                                  # 甲 入住 分水A 100
    lid = _one("select id from lines where move_out_date is null")["id"]
    # 读数小于上次:应拒绝并弹窗
    r = client.get(client.post("/readings/add", data={
        "household_id": "1", "reading_date": "2025-02-01",
        "value_%d" % lid: "50", "meter_%d" % lid: str(_mid("分水A")),
        "price_%d" % lid: "10"}, follow_redirects=False).headers["location"]
    )
    assert 'id="flashModal"' in r.text and "操作没有成功" in r.text
    assert 'class="flash' not in r.text, "不该再有顶部横幅"

    # 成功:顶部提示,不弹窗
    loc = client.post("/readings/add", data={
        "household_id": "1", "reading_date": "2025-02-01",
        "value_%d" % lid: "130", "meter_%d" % lid: str(_mid("分水A")),
        "price_%d" % lid: "10"}, follow_redirects=False).headers["location"]
    ok = client.get(loc)
    assert 'id="flashModal"' in ok.text and "操作已完成" in ok.text
    assert 'role="alertdialog"' not in ok.text, "成功不该走告警弹窗"


def test_all_filter_pages_use_the_same_compact_bar(client):
    """筛选统一走 .filterbar:条件与按钮同一行,没有独立「查询条件」卡片,也不再恒显清除条件。"""
    _with_period(client)
    for path in ("/readings", "/payments", "/ledger", "/statistics"):
        html = client.get(path).text
        assert 'class="filterbar"' in html, path
        assert "清除条件" not in html, path
        # 按钮在同一行的 actions 里
        assert '<div class="actions">' in block_of(html, 'class="filterbar"'), path
        # 未筛选时不显示「重置」,避免无意义的按钮
        assert "重置" not in html, path


def test_payments_shows_per_meter_totals_as_cards_and_footer(client):
    """各总表累计缴费在页面顶部用卡片展示;命中记录/金额合计贴表尾。"""
    _add_meter(client, "总水", level="main")
    client.post("/payments/add", data={"meter_id": str(_mid("总水")),
                                       "payment_date": "2025-01-01",
                                       "amount": "100"})
    html = client.get("/payments").text
    cards = block(html, "各总表累计缴费")
    assert 'class="stats compact"' in cards
    assert "100.00" in cards
    assert "<table" not in cards, "累计缴费不该再用表格"
    foot = html[html.index('class="tablefoot"'):]
    assert "命中记录" in foot and "金额合计" in foot


def test_readings_and_payments_share_daily_tabs(client):
    _basic(client)
    for path, tab in (("/readings", "抄表"), ("/payments", "缴费")):
        html = client.get(path).text
        assert 'class="tabs"' in html
        assert f'>{tab}</a>' in html


def test_system_pages_share_tabs(client):
    """租户 / 表管理 / 设置 在同一套顶部 tab 下,侧栏只留一个系统入口。"""
    _basic(client)
    for path, tab in (("/households", "租户"), ("/meters", "表管理"), ("/init", "设置")):
        html = client.get(path).text
        assert 'class="tabs"' in html, path
        assert f'>{tab}</a>' in html
        # 侧栏只保留一个「系统设置」入口,没有旧的三个子项
        assert html.count("系统设置</span>") == 1, path
        assert ">租户</span>" not in html
        assert ">表管理</span>" not in html


def test_new_household_without_free_meter(client):
    """没有空余表时只给一句提示,不再逐块列出「正在被谁使用」。"""
    _basic(client)   # 分水A 已被甲占用
    html = client.get("/households").text
    assert "当前无空余表" in html
    assert "正在被" not in html
    assert "退租后才可选" not in html


def test_three_phase_base_shows_each_phase(client):
    """三相表装表读数/表基数分别显示三相(含全 0),且列顺序正确。"""
    _basic(client)
    _add_meter(client, "分电三相", kind="electricity", phase="three",
               base="100/200/300")
    meters_html = client.get("/meters").text
    assert '<div class="base-parts">100 / 200 / 300</div>' in meters_html
    assert '合计 600' in meters_html
    households_html = client.get("/households").text
    # 三相表的当前读数只显示 A / B / C,合计(600)不是真实读数,不作主值
    assert '100 / 200 / 300' in households_html
    assert '<div class="last-reading">600</div>' not in households_html

    # 装表读数列位于当前租户与状态列中间
    head = meters_html[meters_html.index('当前租户'):meters_html.index('</tr></thead>')]
    assert head.index('当前租户') < head.index('装表读数') < head.index('状态')


def test_three_phase_zero_base_shows_three_zeros(client):
    """三相表三相均为 0 时,装表读数与最近读数都要显示 0 / 0 / 0。"""
    _basic(client)
    _add_meter(client, "三相零表", kind="electricity", phase="three", base="0/0/0")
    meters_html = client.get("/meters").text
    assert '<div class="base-parts">0 / 0 / 0</div>' in meters_html
    households_html = client.get("/households").text
    assert '0 / 0 / 0' in households_html


def test_new_household_meter_last_reading(client):
    """可选表清单:退租后显示退租读数;新表显示装表基数;标题已改。"""
    from app.db import connect
    from app.main import _meters_brief

    _with_period(client)  # 分水A 已抄到 130,但还被甲占用
    with connect() as conn:
        brief = {m["name"]: m for m in _meters_brief(conn)}
    assert brief["分水A"]["last_value"] == 130
    assert brief["分水A"]["last_source"] == "最近抄表"

    _checkout(client, "甲", {"分水A": 150})
    html = client.get("/households").text
    assert "该住户使用哪几块表" in html
    assert '<div class="last-reading">150</div>' in html
    assert "退租读数" in html

    _add_meter(client, "新表B", base="88")
    html = client.get("/households").text
    assert '<div class="last-reading">88</div>' in html
    assert "表基数" in html


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
    html = client.get(f"/ledger?household_id={_hid('甲')}").text
    assert "分水A" in html, "已结束的租期仍要能看到表名"
    stats = client.get("/statistics").text
    assert "分水A" in stats


def test_stats_margin_respects_type_filter(client):
    """毛差必须与应收同口径:只看水时,缴费也只算水费。"""
    from tests.conftest import _add, _add_meter, _mid, _move_in
    _add_meter(client, "总水", level="main")
    _add_meter(client, "总电", kind="electricity", level="main")
    _add_meter(client, "分水A", base="100")
    _add_meter(client, "分电A", kind="electricity", base="0")
    _move_in(client, "甲", [("分水A", 100, 10), ("分电A", 0, 2)])
    _add(client, "分水A", "2025-02-01", 130)      # 300
    _add(client, "分电A", "2025-02-01", 50)       # 100
    client.post("/payments/add", data={"meter_id": str(_mid("总水")),
                                       "payment_date": "2025-02-05", "amount": "120"})
    client.post("/payments/add", data={"meter_id": str(_mid("总电")),
                                       "payment_date": "2025-02-05", "amount": "40"})

    def margin(q):
        t = client.get(f"/statistics?type={q}").text
        i = t.find("向部门缴费")
        seg = t[i:i + 200]
        import re
        m = re.search(r"毛差</div><div class=\"v\">\s*([-\d,.]+)", seg)
        return m.group(1) if m else None

    assert margin("water") == "180.00"          # 300 − 120
    assert margin("electricity") == "60.00"     # 100 − 40
    assert margin("all") == "240.00"            # 400 − 160


def test_connection_is_closed_when_block_exits(client):
    """`with connect()` 既是事务也是生命周期:退出后连接必须已关闭。

    sqlite3 自带的 __exit__ 只提交,靠 GC 回收;长时间运行会堆积文件句柄。
    """
    import sqlite3

    import pytest

    with db.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_failed_block_rolls_back_and_closes(client):
    """异常退出时回滚,且连接关闭。"""
    import sqlite3

    import pytest

    try:
        with db.connect() as conn:
            conn.execute("INSERT INTO households(name) VALUES ('不该留下')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert _one("select id from households where name='不该留下'") is None


def test_version_shown_on_login_and_shell(client):
    """版本号只由 app.__init__ 定义,登录页与工作台侧栏都要显示。"""
    from app import __version__

    assert f"v{__version__}" in client.get("/login").text
    assert f"v{__version__}" in client.get("/").text
