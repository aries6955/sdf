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
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    with TestClient(app) as c:
        r = c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        c.cookies.set("sdf_session", r.cookies.get("sdf_session"))
        yield c


# ---------- 共享辅助 ----------

def _q(sql, params=()):
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def _one(sql, params=()):
    rows = _q(sql, params)
    return rows[0] if rows else None


def _mid(name):
    row = _one("select id from meters where name=?", (name,))
    return row["id"] if row else None


def _hid(name):
    row = _one("select id from households where name=?", (name,))
    return row["id"] if row else None


def _add_meter(client, name, kind="water", level="sub", main=None, base=None,
               base_date="2025-01-01", phase="single", **kw):
    """建表。分表必须给装表读数,总表不给就留空。"""
    if base is None:
        base = "0" if level == "sub" else ""
    data = {"name": name, "meter_type": kind, "level": level,
            "installed_on": base_date, "phase": phase, **kw}
    if base:
        if phase == "three":
            parts = base.split("/")
            data.update({"base_1": parts[0], "base_2": parts[1], "base_3": parts[2]})
        else:
            data["base_value"] = base
    if main:
        data["main_meter_id"] = str(_mid(main))
    return client.post("/meters/add", data=data, follow_redirects=False)


def _move_in(client, name, seats, start="2025-01-01", note=""):
    """入住。seats: [(表名, 读数[, 单价])]"""
    data = {"name": name, "note": note, "start_date": start}
    for seat in seats:
        mname, value = seat[0], seat[1]
        price = seat[2] if len(seat) > 2 else ""
        mid = _mid(mname)
        data["meter_ids"] = data.get("meter_ids", []) + [str(mid)]
        if isinstance(value, tuple):
            for i, v in enumerate(value, 1):
                data[f"base_{mid}_{i}"] = str(v)
        else:
            data[f"base_{mid}"] = str(value)
        if price != "":
            data[f"price_{mid}"] = str(price)
    return client.post("/households/add", data=data, follow_redirects=False)


def _line_of(meter_name):
    row = _one("select id from lines where id = (select line_id from meters where name=?)",
               (meter_name,))
    return row["id"] if row else None


def _add(client, meter_name, date, value, price=None):
    line_id = _line_of(meter_name)
    data = {"line_id": str(line_id), "meter_id": str(_mid(meter_name)),
            "reading_date": date}
    if isinstance(value, tuple):
        for i, v in enumerate(value, 1):
            data[f"value_{i}"] = str(v)
    else:
        data["value"] = str(value)
    if price is not None:
        data["tenant_price"] = str(price)
    return client.post("/readings/add", data=data, follow_redirects=False)


def _msg(resp):
    from urllib.parse import unquote
    return unquote(resp.headers.get("location", ""))


def _checkout(client, name, outs, date="2025-06-01"):
    hid = _hid(name)
    data = {"id": str(hid), "move_out_date": date}
    for meter_name, value in outs.items():
        lid = _one("select id from lines where household_id=? and move_out_date is null "
                   "and id=(select line_id from meters where name=?)", (hid, meter_name))
        if isinstance(value, tuple):
            for i, v in enumerate(value, 1):
                data[f"out_{lid['id']}_{i}"] = str(v)
        elif lid:
            data[f"out_{lid['id']}"] = str(value)
    return client.post("/households/checkout", data=data, follow_redirects=False)


def _basic(client):
    """最小场景:一块总水表 + 一块分水表,甲入住 100。"""
    _add_meter(client, "总水", level="main")
    _add_meter(client, "分水A", base="100")
    _move_in(client, "甲", [("分水A", 100, 10)])


def _with_period(client):
    """甲 100 → 130,30 吨 × 10 = 300 应收。"""
    _basic(client)
    _add(client, "分水A", "2025-02-01", 130)
    return _one("select * from receivables order by id desc limit 1")
