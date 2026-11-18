"""租期交接与三相电表的功能测试。"""

import re
import sqlite3

import app.db as db
from tests.conftest import _add, _ids, _setup, _table_rows


def _create_tenant_with_meters(client, name, meters_with_bases, date="2025-06-01"):
    data = {"name": name, "note": "", "start_date": date}
    data["meter_ids"] = [str(mid) for mid, _ in meters_with_bases]
    data["base_values"] = [str(v) for _, v in meters_with_bases]
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
    # conftest 只给分表建基数租期(2 条)+ 本次交接 1 条
    assert n == 3


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
    """三相电表:三个读数相加作为一个读数计费。"""
    _setup(client, houses="房东\n租户A",
           meters="总电,电,总,,\n分电A,电,分,总电,租户A")
    meters, _ = _ids(client)
    # 把分电A 设为三相
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("update meters set phase='three' where id=?", (meters["分电A"],))
    conn.commit()
    conn.close()
    # 基数:三相之和 300(用 /meters/base)
    client.post("/meters/base", data={"meter_id": str(meters["分电A"]),
                                      "reading_date": "2025-01-01", "value": "300",
                                      "tenant_price": "1"})
    # 抄表:三相 120/130/110 → 合计 360,用量 60 × 1 = 60 元
    client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-02-01",
        "value_1": "120", "value_2": "130", "value_3": "110", "tenant_price": "1",
    })
    conn = sqlite3.connect(db.DB_PATH)
    value, r1, r2, r3 = conn.execute(
        "select value, r1, r2, r3 from readings where meter_id=? and is_initial=0",
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
        "meter_id": str(meters["分电A"]), "reading_date": "2025-01-01",
        "value_1": "100", "value_2": "100", "value_3": "100",
    })
    # 第二次某相倒退(90 < 100)应被拒
    from urllib.parse import unquote
    r = client.post("/readings/add", data={
        "meter_id": str(meters["分电A"]), "reading_date": "2025-02-01",
        "value_1": "90", "value_2": "110", "value_3": "110",
    }, follow_redirects=False)
    assert "某一相" in unquote(r.headers["location"])


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
    assert 'name="base_values"' in html
