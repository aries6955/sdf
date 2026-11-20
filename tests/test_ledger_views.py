"""界面上新增的账务展示:台账卡片、上期读数、统计页的已收/未收。

这三处都是把已有事实摆到页面上,口径必须与台账同一处推导,不能另算一套。
"""


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
    # 新表那条的上期读数应是 0(同表上一条),不是旧表的 130
    i = html.find("<td>分水B</td>")
    assert i > 0, "表里应有新表的读数行"
    row = html[i:i + 900]
    assert '<td class="num">0</td>' in row, row


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
