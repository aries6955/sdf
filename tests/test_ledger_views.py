"""界面上新增的账务展示:台账卡片、上期读数、统计页的已收/未收。

这三处都是把已有事实摆到页面上,口径必须与台账同一处推导,不能另算一套。
"""

from tests.htmlq import block_of


def _home(client):
    return client.get("/").text


def test_home_has_ledger_card_not_a_jump_to_households(client):
    """首页的"租户欠款"不该只是跳到租户列表,要直接给出台账。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5.5)])
    _add(client, "分水A", "2025-02-01", 130)          # 30 × 5.5 = 165
    html = _home(client)
    assert "收款台账" in html
    assert "应收(元)" in html and "已收(元)" in html and "未收(元)" in html
    assert "165.00" in html


def test_readings_show_previous_value_same_meter(client):
    """上期读数必须按同一块表取,换表后不能拿旧表读数相减。"""
    from tests.conftest import _add, _add_meter, _mid, _move_in
    _add_meter(client, "分水A", base="100")
    _add_meter(client, "分水B", base="0")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)
    # 换表:旧表终读数 130,新表从 0 起
    client.post("/switches/add", data={"old_meter_id": str(_mid("分水A")),
                                       "new_meter_id": str(_mid("分水B")),
                                       "switch_date": "2025-02-15",
                                       "old_final_value": "130",
                                       "new_base_value": "0"})
    _add(client, "分水B", "2025-03-01", 20)
    html = client.get("/readings").text
    # 只在抄表记录表里找,新增抄表表单也会出现表名
    table = block_of(html, 'id="readingsTable"')
    i = table.find("<td>分水B</td>")
    assert i > 0, "表里应有新表的读数行"
    row = table[i:i + 1400]
    assert '<span class="view">0</span>' in row, row


def test_edit_reading_returns_to_same_page_and_row(client):
    """改完读数要停在原页原筛选,并以锚点回到那一行,不能甩回页顶。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)
    html = client.get("/readings").text
    rid = html.split('name="id" value="')[1].split('"')[0]
    r = client.post("/readings/edit", data={
        "id": rid, "reading_date": "2025-02-01", "value": "131",
        "tenant_price": "5", "date_from": "2025-01-01", "page": "2"},
        follow_redirects=False)
    loc = r.headers["location"]
    assert "date_from=2025-01-01" in loc and "page=2" in loc
    assert loc.endswith(f"#r{rid}")


def test_statistics_shows_paid_and_reading_columns(client):
    """统计页要能直接看出收费与缴费情况,含抄表读数。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)
    html = client.get("/statistics").text
    for col in ("上期读数", "本期读数", "已收(元)", "未收(元)", "向租户已收"):
        assert col in html, col
    assert "100" in html and "130" in html


def test_statistics_paid_matches_ledger(client):
    """统计页的已收总额必须等于台账里的已收,不能另算一套。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)          # 150
    client.post("/receipts/add", data={"household_id": "1", "entry_date": "2025-02-10",
                                       "kind": "receipt", "amount": "100"})
    stats = client.get("/statistics").text
    i = stats.find("向租户已收")
    assert "100.00" in stats[i:i + 120]


def test_ledger_page_records_receipt_without_leaving_it(client):
    """收款入口在台账页:登记后回到同一组筛选,不用先去某户台账。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)          # 150
    r = client.post("/receipts/add", data={
        "household_id": "1", "entry_date": "2025-02-10", "kind": "receipt",
        "amount": "100", "back": "/ledger?status=unpaid&type="},
        follow_redirects=False)
    assert r.headers["location"].startswith("/ledger?status=unpaid")
    html = client.get("/ledger").text
    assert "100.00" in html
    assert "登记收款" in html and "收款流水" in html


def test_readings_edit_is_inline_not_a_reload(client):
    """修改必须在行内展开,不能再生成一次整页请求(否则页面跳动)。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)
    html = client.get("/readings").text
    assert "data-edit" in html, "行内编辑按钮"
    assert 'class="editrow"' not in html, "不该再有整行替换的编辑态"
    assert 'form="ed-' in html, "输入框用 form 属性挂到行尾表单"


def test_ledger_has_quick_receipt_per_row(client):
    """台账明细每行带快捷收款:金额框默认填未收金额,提交即登记。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)          # 150
    html = client.get("/ledger").text
    assert 'class="quickpay"' in html
    assert 'value="150.00"' in html, "默认填未收金额"
    # 收齐后不再给入口
    client.post("/receipts/add", data={"household_id": "1", "entry_date": "2025-02-10",
                                       "amount": "150", "back": "/ledger"})
    html2 = client.get("/ledger").text
    assert "已收齐" in html2


def test_readings_show_three_phase_previous(client):
    """三相表的上期读数要带分相,不能只给合计。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分电A", kind="electricity", phase="three", base="100/100/100")
    _move_in(client, "甲", [("分电A", (100, 100, 100), 1)])
    _add(client, "分电A", "2025-02-01", (120, 130, 140))     # 90 × 1
    _add(client, "分电A", "2025-03-01", (150, 160, 170))
    html = client.get("/readings").text
    # 第二条的上期应是 120/130/140
    assert "120 / 130 / 140" in html


def test_statistics_shows_three_phase_readings(client):
    """统计页的三相账期要显示分相读数,与抄表页一致。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分电A", kind="electricity", phase="three", base="100/100/100")
    _move_in(client, "甲", [("分电A", (100, 100, 100), 1)])
    _add(client, "分电A", "2025-02-01", (120, 130, 140))
    html = client.get("/statistics").text
    assert "100 / 100 / 100" in html and "120 / 130 / 140" in html


def test_confirm_is_a_card_not_browser_dialog(client):
    """确认走页面内的卡片,不再用浏览器原生 confirm。"""
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)
    html = client.get("/readings").text
    assert 'data-confirm="撤回这条抄表?前后账期会重算。"' in html
    assert "return confirm(" not in html
    assert "/static/confirm.js" in html


def test_partial_payment_keeps_receipt_entry_on_every_page(client):
    """部分收款不算收齐:三个页面都要留收款入口,默认金额是剩余未收。

    这是实测出来的真回归 —— 抄表页曾用 paid_periods 判定,它的语义是"收到过钱",
    部分收款也命中,于是快捷收款消失、余款只能绕去台账页收。
    """
    from tests.conftest import _add, _add_meter, _move_in
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 5)])
    _add(client, "分水A", "2025-02-01", 130)          # 应收 150
    client.post("/receipts/add", data={"household_id": "1", "entry_date": "2025-02-05",
                                       "amount": "50"})   # 只收 50

    readings = client.get("/readings").text
    assert 'class="quickpay"' in readings, "抄表页仍要能收余款"
    assert 'value="100.00"' in readings, "默认金额是剩余未收 150-50"

    ledger = client.get("/ledger").text
    assert 'class="quickpay"' in ledger
    assert "已收齐" not in ledger, "部分收款不是收齐"

    stats = client.get("/statistics").text
    assert "部分收款" in stats
