"""租期交接与三相电表的功能测试。"""

import re
import sqlite3

import app.db as db
from tests.conftest import _add, _ids, _setup, _table_rows


def _create_tenant_with_meters(client, name, meters_with_bases, date="2025-06-01"):
    """绑定表的新协议:单相表字段 base_<id>;三相表 base_<id>_1/2/3。

    meters_with_bases: (meter_id, value) 或 (meter_id, (v1, v2, v3))。
    """
    data = {"name": name, "note": "", "start_date": date}
    for mid, val in meters_with_bases:
        data["meter_ids"] = data.get("meter_ids", []) + [str(mid)]
        if isinstance(val, tuple):
            for i, v in enumerate(val, 1):
                data[f"base_{mid}_{i}"] = str(v)
        else:
            data[f"base_{mid}"] = str(val)
    return client.post("/households/add", data=data)


def test_new_tenant_binds_meters_with_base_readings(client):
    """新增租户选表 + 起始读数;表转给新租户并产生租期记录。"""
    _setup(client)
    meters, _ = _ids(client)
    r = _create_tenant_with_meters(client, "王五", [(meters["分水A"], 500)])
    assert r.status_code == 200
    conn = sqlite3.connect(db.DB_PATH)
    holder = conn.execute(
        "select h.name from meters m join households h on m.household_id=h.id "
        "where m.name='分水A'").fetchone()[0]
    n = conn.execute("select count(*) from takeovers").fetchone()[0]
    conn.close()
    assert holder == "王五"
    # 房东/租户A/租户B 各 1 条基数租期 + 本次交接 1 条
    assert n == 4


def test_tenant_takeover_splits_billing(client):
    """交接后:新租户只从交接读数起计费;上一任的历史账单保留。"""
    _setup(client, houses="房东\n租户A", meters="总水,水,总,,\n分水A,水,分,总水,租户A")
    meters, households = _ids(client)
    # A 的第一期:100 → 130,30 吨 × 10 = 300
    _add(client, meters["分水A"], "2025-02-01", 130)
    # A 搬走,B 入住,交接读数 160(同时是 A 的终读数)
    _create_tenant_with_meters(client, "租户B", [(meters["分水A"], 160)], date="2025-03-01")
    # B 的第一期:160 → 200,40 吨 × 10 = 400
    _add(client, meters["分水A"], "2025-04-01", 200)

    stats = client.get("/statistics").text
    rows = _table_rows(stats)
    # A 只有一段:300
    assert "300.00" in rows
    # B 只有一段:400;B 不应看到交接前的用量
    assert "400.00" in rows
    # 三段:A(100→130)=300, A(130→160)=300 —— 交接读数 160 是 A 的终读数
    # B(160→200)=400。交接读数只结一次,不重复计量。
    assert rows.count("<tr>") - 1 == 3
    assert rows.count("300.00") == 2
    # B 的账单起点是交接日
    b_row = [r for r in rows.split("<tr>") if "400.00" in r][0]
    assert "2025-03-01" in b_row


def test_old_tenant_history_survives_takeover(client):
    """表转给新租户后,统计页仍能看到上一任的历史账期。"""
    _setup(client, houses="房东\n租户A", meters="总水,水,总,,\n分水A,水,分,总水,租户A")
    meters, households = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    hh_a = households["租户A"]
    rows_a = _table_rows(client.get(f"/statistics?household_id={hh_a}").text)
    assert "300.00" in rows_a
    _create_tenant_with_meters(client, "租户B", [(meters["分水A"], 160)], date="2025-03-01")
    # 交接后,A 的历史仍在
    rows_a = _table_rows(client.get(f"/statistics?household_id={hh_a}").text)
    assert "300.00" in rows_a


def test_three_phase_meter_sums_readings(client):
    """三相电表:交接分相建基,抄表三相相加计费。"""
    client.post("/households/add", data={"name": "房东", "note": "", "start_date": "2025-01-01"})
    client.post("/meters/add", data={"name": "总电", "meter_type": "electricity", "level": "main"})
    client.post("/meters/add", data={"name": "分电A", "meter_type": "electricity",
                                     "level": "sub", "main_meter_id": "2", "phase": "three"})
    meters, _ = _ids(client)
    # 交接:三相 100/100/100 → 基数 300,单价 1
    _create_tenant_with_meters(client, "租户A", [(meters["分电A"], (100, 100, 100))],
                               date="2025-01-01")
    client.post("/households/add", data={"name": "租户A占位2", "note": "", "start_date": "2025-01-02"})
    # 抄表:三相 120/130/110 → 合计 360,用量 60 × 1 = 60 元
    client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-02-01",
        "value_1": "120", "value_2": "130", "value_3": "110", "tenant_price": "1",
    })
    conn = sqlite3.connect(db.DB_PATH)
    value, r1, r2, r3 = conn.execute(
        "select value, r1, r2, r3 from readings where meter_id=? and reading_date='2025-02-01'",
        (meters["分电A"],)).fetchone()
    conn.close()
    assert (value, r1, r2, r3) == (360, 120, 130, 110)
    rows = _table_rows(client.get("/statistics").text)
    assert "60.00" in rows


def test_three_phase_rejects_per_phase_decrease(client):
    _setup(client, houses="房东\n租户A",
           meters="总电,电,总,,\n分电A,电,分,总电,租户A")
    meters, _ = _ids(client)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("update meters set phase='three' where id=?", (meters["分电A"],))
    conn.commit()
    conn.close()
    client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-02-01",
        "value_1": "100", "value_2": "100", "value_3": "100",
    })
    # 第二次某相倒退(90 < 100)应被拒
    from urllib.parse import unquote
    r = client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-03-01",
        "value_1": "90", "value_2": "110", "value_3": "110",
    }, follow_redirects=False)
    assert "某一相" in unquote(r.headers["location"])
    # 日期/相序都合法的读数应被接受
    r2 = client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-03-01",
        "value_1": "110", "value_2": "110", "value_3": "110",
    }, follow_redirects=False)
    assert "msg=" not in (r2.headers.get("location") or "")


def test_meter_delete_cleans_dependencies(client):
    """删除有读数/租期的表,应连带清理而不是 500。"""
    _setup(client)
    meters, _ = _ids(client)
    _add(client, meters["分水A"], "2025-02-01", 130)
    r = client.post("/meters/delete", data={"id": str(meters["分水A"])}, follow_redirects=False)
    assert r.status_code == 303
    conn = sqlite3.connect(db.DB_PATH)
    n = conn.execute("select count(*) from meters where name='分水A'").fetchone()[0]
    conn.close()
    assert n == 0


def test_bind_form_lists_meters_with_holders(client):
    """新增租户表单应列出可选的表、当前持有人与最近读数。"""
    _setup(client)
    html = client.get("/households").text
    assert "交接起始读数" in html
    assert "当前持有人" in html
    assert 'name="meter_ids"' in html
    assert 'name="base_' in html


def test_three_phase_tenant_handover_stores_components(client):
    """新增租户绑定三相表:分相录入,存三相之和并留档 r1/r2/r3。"""
    _setup(client, houses="房东\n租户A", meters="总电,电,总,,\n分电A,电,分,总电,租户A")
    meters, _ = _ids(client)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("update meters set phase='three' where id=?", (meters["分电A"],))
    conn.commit()
    conn.close()
    r = _create_tenant_with_meters(client, "租户B", [(meters["分电A"], (100, 110, 120))])
    assert r.status_code == 200
    conn = sqlite3.connect(db.DB_PATH)
    value, r1, r2, r3, note = conn.execute(
        "select value, r1, r2, r3, note from readings where meter_id=? "
        "order by id desc limit 1", (meters["分电A"],)).fetchone()
    t = conn.execute("select start_value from takeovers where meter_id=? "
                     "order by id desc limit 1", (meters["分电A"],)).fetchone()[0]
    conn.close()
    assert (value, r1, r2, r3) == (330, 100, 110, 120)
    assert t == 330


def test_three_phase_base_via_meters_base(client):
    """未绑定租户的三相表:表设置接口用 b_1/b_2/b_3 设基数,分相留档。"""
    client.post("/meters/add", data={"name": "备用电表", "meter_type": "electricity",
                                     "level": "sub", "phase": "three"})
    meters, _ = _ids(client)
    client.post("/meters/base", data={"meter_id": str(meters["备用电表"]),
                                      "reading_date": "2025-01-01",
                                      "b_1": "50", "b_2": "60", "b_3": "70",
                                      "tenant_price": "2"})
    conn = sqlite3.connect(db.DB_PATH)
    row = conn.execute(
        "select value, r1, r2, r3, is_initial from readings where meter_id=?",
        (meters["备用电表"],)).fetchone()
    conn.close()
    assert row == (180, 50, 60, 70, 1)


def test_bind_form_lists_meters_with_holders(client):
    """新增租户表单应列出可选的表、当前持有人与最近读数。"""
    _setup(client)
    html = client.get("/households").text
    assert "交接起始读数" in html
    assert "当前持有人" in html
    assert 'name="meter_ids"' in html
    assert 'name="base_' in html


def test_three_phase_tenant_handover_stores_components(client):
    """新增租户绑定三相表:分相录入,存三相之和并留档 r1/r2/r3。"""
    _setup(client, houses="房东\n租户A", meters="总电,电,总,,\n分电A,电,分,总电,租户A")
    meters, _ = _ids(client)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("update meters set phase='three' where id=?", (meters["分电A"],))
    conn.commit()
    conn.close()
    r = _create_tenant_with_meters(client, "租户B", [(meters["分电A"], (100, 110, 120))])
    assert r.status_code == 200
    conn = sqlite3.connect(db.DB_PATH)
    value, r1, r2, r3, note = conn.execute(
        "select value, r1, r2, r3, note from readings where meter_id=? "
        "order by id desc limit 1", (meters["分电A"],)).fetchone()
    t = conn.execute("select start_value from takeovers where meter_id=? "
                     "order by id desc limit 1", (meters["分电A"],)).fetchone()[0]
    conn.close()
    assert (value, r1, r2, r3) == (330, 100, 110, 120)
    assert t == 330
