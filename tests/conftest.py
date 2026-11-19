import sqlite3

import pytest
from fastapi.testclient import TestClient

import app.db as db
import app.main as app_main
from app.main import app

TEST_PASSWORD = "test-pass-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 测试用独立的密码文件,不碰真实 password.txt
    pw = tmp_path / "password.txt"
    pw.write_text(TEST_PASSWORD + "\n", encoding="utf-8")
    monkeypatch.setattr(app_main, "password_path", lambda: pw)
    db.DB_PATH = tmp_path / "test.sqlite3"
    with TestClient(app) as c:
        r = c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        c.cookies.set("sdf_session", r.cookies.get("sdf_session"))
        yield c


# ---------- 共享辅助 ----------

def _ids(client):
    conn = sqlite3.connect(db.DB_PATH)
    meters = dict(conn.execute("select name, id from meters"))
    households = dict(conn.execute("select name, id from households"))
    conn.close()
    return meters, households


def _mid(client, name):
    return _ids(client)[0][name]


def _setup(client, houses="房东*\n租户A\n租户B", meters=None, bases=None):
    """通过真实页面接口建数据(绑定只在新增租户时发生)。

    houses: 每行一户,行尾 * 忽略。
    meters: 每行 `名称,水/电,总/分,所属总表,租户名`。
    bases:  每行 `表名,日期,读数,部门单价,租户单价`。
    """
    if meters is None:
        meters = "总水,水,总,,房东\n分水A,水,分,总水,租户A\n分水B,水,分,总水,租户B"
    if bases is None:
        bases = "总水,2025-01-01,1000,5,10\n分水A,2025-01-01,100,5,10\n分水B,2025-01-01,200,5,10"

    base_map = {}
    for line in bases.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            base_map[parts[0]] = parts

    # 先建表(基数在建表时录入;归属只在新增租户时发生)
    for line in meters.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        b = base_map.get(parts[0], [None, "2025-01-01", "0"])
        payload = {"name": parts[0],
                   "meter_type": "water" if parts[1] in ("水", "water") else "electricity",
                   "level": "main" if parts[2] in ("总", "main") else "sub",
                   "base_date": b[1], "base_value": b[2]}
        if len(parts) > 3 and parts[3]:
            payload["main_meter_id"] = str(_mid(client, parts[3]))
        client.post("/meters/add", data=payload)

    # 解析每户的表与基数
    house_meters: dict[str, list[str]] = {}
    unbound = []
    for line in meters.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        name, kind, level = parts[0], parts[1], parts[2]
        hh = parts[4] if len(parts) > 4 else ""
        if hh:
            house_meters.setdefault(hh, []).append(name)
        else:
            unbound.append(name)

    # 逐户新增租户并绑定其表(交接读数即基数,单价随交接录入)
    for line in houses.strip().splitlines():
        hh_name = line.strip().rstrip("*").strip()
        if not hh_name:
            continue
        data = {"name": hh_name, "note": "", "start_date": "2025-01-01"}
        for mname in house_meters.get(hh_name, []):
            mid = _mid(client, mname)
            b = base_map.get(mname, [None, "2025-01-01", "0", "", "0"])
            data["meter_ids"] = data.get("meter_ids", []) + [str(mid)]
            data[f"base_{mid}"] = b[2]
            data[f"price_{mid}"] = b[4] if len(b) > 4 else ""
        client.post("/households/add", data=data)

    # 未绑定表的基数走 /meters/base(基数接口保留为 API)
    for mname in unbound:
        b = base_map.get(mname)
        if b:
            client.post("/meters/base", data={
                "meter_id": _mid(client, mname), "reading_date": b[1],
                "value": b[2], "tenant_price": b[4] if len(b) > 4 else ""})


def _add(client, meter, date, value, **kw):
    # 单价传空串 = 沿用上次单价(传 "0" 会被当作真实的 0 元单价)
    data = {"meter_id": str(meter), "reading_date": date, "value": str(value),
            "department_price": "", "tenant_price": ""}
    data.update(kw)
    return client.post("/readings/add", data=data, follow_redirects=False)


def _table_rows(html: str) -> str:
    """结果表格(去掉筛选下拉选项里的干扰文本)。"""
    start = html.find("<table>")
    end = html.find("</table>", start)
    return html[start:end]
