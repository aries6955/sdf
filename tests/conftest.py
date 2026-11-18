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


def _setup(client, houses="房东*\n租户A\n租户B", meters=None, bases=None):
    """通过真实页面接口建数据。

    houses / meters / bases 用逗号分隔的字符串,便于测试读懂。
    """
    if meters is None:
        meters = "总水,水,总,,房东\n分水A,水,分,总水,租户A\n分水B,水,分,总水,租户B"
    if bases is None:
        bases = "总水,2025-01-01,1000,5,10\n分水A,2025-01-01,100,5,10\n分水B,2025-01-01,200,5,10"

    for line in houses.strip().splitlines():
        name = line.strip().rstrip("*").strip()
        if name:
            client.post("/households/add", data={"name": name, "note": "",
                                                 "start_date": "2025-01-01"})

    houses_ids = _ids(client)[1]
    meter_ids: dict[str, int] = {}
    rows = [[p.strip() for p in l.split(",")] for l in meters.strip().splitlines() if l.strip()]
    for row in rows:
        if len(row) >= 3 and row[2] in ("总", "main"):
            client.post("/meters/add", data={
                "name": row[0],
                "meter_type": "water" if row[1] in ("水", "water") else "electricity",
                "level": "main",
            })
            meter_ids[row[0]] = _ids(client)[0][row[0]]
    for row in rows:
        if len(row) >= 3 and row[2] not in ("总", "main"):
            main_name = row[3] if len(row) > 3 else ""
            hh_name = row[4] if len(row) > 4 else ""
            client.post("/meters/add", data={
                "name": row[0],
                "meter_type": "water" if row[1] in ("水", "water") else "electricity",
                "level": "sub",
                "main_meter_id": str(meter_ids.get(main_name, "")),
                "household_id": str(houses_ids.get(hh_name, "")),
            })
            meter_ids[row[0]] = _ids(client)[0][row[0]]

    for line in bases.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        mid = _ids(client)[0].get(parts[0])
        if not mid:
            continue
        # 基数走 /meters/base:写入 is_initial 读数并建立租期
        client.post("/meters/base", data={
            "meter_id": str(mid), "reading_date": parts[1], "value": parts[2],
            "tenant_price": parts[4] if len(parts) > 4 else "",
        })


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
