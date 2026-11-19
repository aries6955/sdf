import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .billing import (
    all_periods,
    get_line,
    line_readings,
    open_lines,
    recompute_line,
    record_period,
    vacancy_usage,
)
from .db import connect, get_setting, init_db, set_setting
from .ledger import all_outstanding, household_ledger, totals, touched_by_reading
from .sample import clear_all, load_sample
from .views import render


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="水电费管理", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_store_html(request: Request, call_next):
    """HTML 页面一律不缓存,避免浏览器展示改动前的旧界面。"""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


SESSION_COOKIE = "sdf_session"
_sessions: set[str] = set()

# 免登录路径:登录页、退出、静态资源
OPEN_PATHS = ("/login", "/logout")
OPEN_PREFIXES = ("/static/",)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """所有页面与提交动作统一校验登录,只放行登录页与静态资源。"""
    path = request.url.path
    if path in OPEN_PATHS or any(path.startswith(p) for p in OPEN_PREFIXES):
        return await call_next(request)
    if request.cookies.get(SESSION_COOKIE) not in _sessions:
        if request.method == "POST":
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# ---------- 基础设施 ----------

def password_path() -> Path:
    return Path(__file__).resolve().parent.parent / "password.txt"


def password_from_config() -> str:
    path = password_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return os.environ.get("SDF_PASSWORD", "admin123")


# 超级密码:忘密时的恢复入口。可通过环境变量覆盖。
SUPER_PASSWORD = os.environ.get("SDF_SUPER_PASSWORD", "5339025")


def verify_password(given: str) -> tuple[bool, bool]:
    """返回 (是否通过, 是否为超级密码)。"""
    if given == SUPER_PASSWORD:
        return True, True
    return given == password_from_config(), False


def save_password(new: str) -> None:
    password_path().write_text(new + "\n", encoding="utf-8")


PAGE_SIZE = 20


def paginate(total: int, page: int, base_qs: str) -> tuple[int, int, str]:
    """返回 (当前页, 总页数, 分页控件 HTML)。base_qs 是已拼好的查询串(不含 page)。"""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    if pages <= 1:
        return page, pages, ""

    def link(n: int, label: str) -> str:
        sep = "&" if base_qs else ""
        return f'<a class="btn ghost small" href="?{base_qs}{sep}page={n}">{label}</a>'

    parts = []
    if page > 1:
        parts.append(link(1, "« 首页"))
        parts.append(link(page - 1, "‹ 上一页"))
    parts.append(f'<span class="pageno">{page} / {pages}</span>')
    if page < pages:
        parts.append(link(page + 1, "下一页 ›"))
        parts.append(link(pages, "末页 »"))
    return page, pages, f'<div class="pager">{"".join(parts)}</div>'


def flash_from(msg: str, ok: str = "") -> tuple[str, str] | None:
    if msg:
        return ("err", msg)
    if ok:
        return ("ok", ok)
    return None


def back(path: str, msg: str = "", ok: str = "") -> RedirectResponse:
    key = "msg" if msg else "ok"
    text = msg or ok
    return RedirectResponse(f"{path}?{key}={quote(text)}", status_code=303)


def _looks_broken(text: str) -> bool:
    """粗判文本是否在写入时就已损坏(替换符或典型 mojibake)。"""
    if not text:
        return False
    if "\ufffd" in text:
        return True
    return any("\u00c0" <= ch <= "\u00ff" for ch in text)


def _int(value: str):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: str, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# ---------- 登录 ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(msg: str = ""):
    return render("login.html", title="登录", msg=msg)


@app.post("/login")
def login(password: str = Form(...)):
    ok, is_super = verify_password(password)
    if not ok:
        return RedirectResponse("/login?msg=" + quote("密码不正确,请重试"), status_code=303)
    token = secrets.token_urlsafe(24)
    _sessions.add(token)
    if is_super:
        r = RedirectResponse(
            "/init?msg=" + quote("已用超级密码登录。建议立即在下方设置新密码。"),
            status_code=303,
        )
    else:
        r = RedirectResponse("/", status_code=303)
    r.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return r


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.discard(token)
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE)
    return r


# ---------- 首页 ----------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with connect() as conn:
        name = get_setting(conn, "property_name", "")
        household_count = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        meter_count = conn.execute(
            "SELECT COUNT(*) FROM meters WHERE level='sub' AND retired_on IS NULL").fetchone()[0]
        reading_count = conn.execute(
            "SELECT COUNT(*) FROM readings WHERE kind='periodic'").fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]
        last = conn.execute(
            "SELECT MAX(reading_date) FROM readings WHERE kind='periodic'").fetchone()[0]
        t = totals(conn)
        outstanding_total = round(sum(x["outstanding"] for x in all_outstanding(conn)), 2)
        open_count = len(open_lines(conn))
        periods = all_periods(conn)
        recent = periods[-5:][::-1]

    return render(
        "home.html",
        title=name or "尚未设置房产名称",
        lead="总览。左侧按日常记账、查询、系统分组。",
        active="home",
        household_count=household_count, meter_count=meter_count,
        reading_count=reading_count, open_count=open_count, paid=paid, last=last,
        total_due=t["total_due"], total_paid=t["total_paid"], balance=t["balance"],
        outstanding_total=outstanding_total, recent=recent,
        setup_done=household_count > 0 and meter_count > 0,
    )


@app.get("/init", response_class=HTMLResponse)
def init_page(request: Request, msg: str = "", ok: str = ""):
    with connect() as conn:
        name = get_setting(conn, "property_name", "")
        households = conn.execute("SELECT * FROM households ORDER BY id").fetchall()
        meters = conn.execute("SELECT * FROM meters ORDER BY id").fetchall()
        counts = {
            label: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for label, table in [("租户", "households"), ("表", "meters"),
                                 ("抄表", "readings"), ("缴费", "payments")]
        }
        bad = [x["name"] for x in households if _looks_broken(x["name"])]
        bad += [x["name"] for x in meters if _looks_broken(x["name"])]
    return render("init.html", title="系统设置",
                  lead="房产名称、登录密码、示例数据与数据清空。租户与表请在左侧「系统」中维护。",
                  active="init", name=name, counts=counts, bad=bad,
                  flash=flash_from(msg, ok))


@app.post("/init/sample")
def init_sample():
    with connect() as conn:
        load_sample(conn)
    return back("/init", ok="已填充示例数据,可到各页面查看")


@app.post("/init/clear")
def init_clear():
    with connect() as conn:
        clear_all(conn)
    return back("/init", ok="已清除所有数据")


@app.post("/init/property")
def init_property(property_name: str = Form("")):
    with connect() as conn:
        set_setting(conn, "property_name", property_name.strip())
    return back("/init", ok="房产名称已保存")


@app.post("/init/password")
def init_password(old_password: str = Form(...), new_password: str = Form(...),
                  confirm_password: str = Form(...)):
    ok, _ = verify_password(old_password)
    if not ok:
        return back("/init", msg="当前密码不正确")
    if new_password != confirm_password:
        return back("/init", msg="两次输入的新密码不一致")
    if len(new_password) < 4:
        return back("/init", msg="新密码至少 4 位")
    if new_password == SUPER_PASSWORD:
        return back("/init", msg="新密码不能与超级密码相同")
    save_password(new_password)
    return back("/init", ok="密码已修改,下次登录请使用新密码")


# ---------- 租户 ----------

def _meters_brief(conn):
    """可选表清单:分表、在用、带当前归属(如果有开放线)。"""
    rows = []
    for m in conn.execute(
            "SELECT m.*, l.id AS line_id, l.household_id, h.name AS holder "
            "FROM meters m LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
            "LEFT JOIN households h ON h.id = l.household_id "
            "WHERE m.level='sub' AND m.retired_on IS NULL ORDER BY m.meter_type, m.id"):
        d = dict(m)
        last = conn.execute(
            "SELECT value FROM readings WHERE meter_id=? ORDER BY reading_date DESC, id DESC LIMIT 1",
            (m["id"],)).fetchone()
        d["last_value"] = last["value"] if last else None
        rows.append(d)
    return rows


@app.get("/households", response_class=HTMLResponse)
def households_page(request: Request, edit: int = Query(0), msg: str = "", ok: str = ""):
    with connect() as conn:
        rows = [dict(x) for x in conn.execute("SELECT * FROM households ORDER BY id")]
        ledgers, line_counts, meter_counts = {}, {}, {}
        for x in rows:
            led = household_ledger(conn, x["id"])
            ledgers[x["id"]] = led
            line_counts[x["id"]] = len(open_lines(conn, x["id"]))
            meter_counts[x["id"]] = conn.execute(
                "SELECT COUNT(*) FROM meters m JOIN lines l ON l.id=m.line_id "
                "WHERE l.household_id=? AND l.move_out_date IS NULL", (x["id"],)).fetchone()[0]
        meters = _meters_brief(conn)
    editing = next((x for x in rows if x["id"] == edit), None)
    return render("households.html", title="租户", lead="维护被收费的租户名单,并为租户办理入住与退租。",
                  active="households", rows=rows, line_counts=line_counts,
                  meter_counts=meter_counts, meters=meters, editing=editing,
                  ledgers=ledgers, today=date.today().isoformat(),
                  flash=flash_from(msg, ok))


@app.get("/households/{household_id}/ledger", response_class=HTMLResponse)
def household_ledger_page(request: Request, household_id: int, msg: str = "", ok: str = ""):
    """某租户的收费台账:应收 / 收款 / 预收余额 / 欠款。"""
    with connect() as conn:
        hh = conn.execute("SELECT * FROM households WHERE id=?", (household_id,)).fetchone()
        if not hh:
            return back("/households", msg="租户不存在")
        ledger = household_ledger(conn, household_id)
        def _lines(where, order):
            return [dict(x) for x in conn.execute(
                "SELECT l.*, "
                "(SELECT m.name FROM meters m WHERE m.id = (SELECT rd.meter_id FROM readings rd "
                "  WHERE rd.line_id = l.id ORDER BY rd.reading_date DESC, rd.id DESC LIMIT 1))"
                " AS meter_name FROM lines l WHERE l.household_id=? AND " + where +
                " ORDER BY " + order, (household_id,))]
        lines = _lines("l.move_out_date IS NULL", "l.id")
        closed = _lines("l.move_out_date IS NOT NULL", "l.move_out_date DESC")
    return render("household_ledger.html", title=f"{hh['name']} · 收费台账",
                  lead="应收、收款与欠款。收款按最早欠账优先自动冲抵,多收的转为预收余额。",
                  active="households", household=dict(hh), ledger=ledger,
                  lines=lines, closed=closed,
                  today=date.today().isoformat(), flash=flash_from(msg, ok))


@app.post("/receipts/add")
def receipt_add(household_id: int = Form(...), entry_date: str = Form(...),
                amount: str = Form(...), kind: str = Form("receipt"), note: str = Form("")):
    value = _float(amount)
    if value is None or value <= 0:
        return back(f"/households/{household_id}/ledger", msg="金额要大于 0")
    kind = kind if kind in ("receipt", "refund") else "receipt"
    with connect() as conn:
        conn.execute(
            "INSERT INTO receipts(household_id,entry_date,amount,kind,note) VALUES (?,?,?,?,?)",
            (household_id, entry_date, round(value, 2), kind, note.strip()))
    label = "收款" if kind == "receipt" else "退款"
    return back(f"/households/{household_id}/ledger", ok=f"{label}已登记")


@app.post("/receipts/delete")
def receipt_delete(id: int = Form(...)):
    with connect() as conn:
        row = conn.execute("SELECT household_id FROM receipts WHERE id=?", (id,)).fetchone()
        if not row:
            return back("/households", msg="记录不存在")
        conn.execute("DELETE FROM receipts WHERE id=?", (id,))
        hh = row["household_id"]
    return back(f"/households/{hh}/ledger", ok="已删除该收款记录,账期随之解锁")


@app.post("/households/add")
async def household_add(request: Request):
    """入住:一次录完该租户要用的表与每块表当日的读数。

    每块表生成一条计量线;同一类水电可选多块表,各自独立算账。
    三相表字段 base_<meter_id>_1/2/3。
    """
    form = await request.form()
    name = str(form.get("name", "")).strip()
    note = str(form.get("note", "")).strip()
    start_date = str(form.get("start_date", ""))
    meter_ids = [m for m in (_int(x) for x in form.getlist("meter_ids")) if m]
    if not name or not start_date:
        return back("/households", msg="名称与入住日期必填")
    if not meter_ids:
        return back("/households", msg="至少要选一块分表,没有表就不产生账单")
    try:
        datetime.fromisoformat(start_date)
    except ValueError:
        return back("/households", msg="入住日期格式不正确")

    with connect() as conn:
        seats = []
        for mid in meter_ids:
            meter = conn.execute(
                "SELECT m.*, l.id AS line_id FROM meters m "
                "LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
                "WHERE m.id=?", (mid,)).fetchone()
            if not meter:
                return back("/households", msg="表不存在")
            if meter["level"] != "sub":
                return back("/households", msg=f"「{meter['name']}」是总表,不能分配给租户")
            if meter["retired_on"]:
                return back("/households", msg=f"「{meter['name']}」已换下,不能分配")
            if meter["line_id"]:
                return back("/households", msg=f"「{meter['name']}」现在有人在用,请先给上一任退租")
            last = conn.execute(
                "SELECT value, tenant_price FROM readings WHERE meter_id=? "
                "ORDER BY reading_date DESC, id DESC LIMIT 1", (mid,)).fetchone()
            if meter["phase"] == "three":
                parts = [_int(form.get(f"base_{mid}_{i}", "")) for i in (1, 2, 3)]
                if any(p is None or p < 0 for p in parts):
                    return back("/households", msg=f"三相表「{meter['name']}」需分别填写三个读数")
                start_val = sum(parts)
                r1, r2, r3 = parts
            else:
                raw = _int(form.get(f"base_{mid}", ""))
                if raw is not None and raw < 0:
                    return back("/households", msg=f"「{meter['name']}」读数不能为负")
                start_val = raw
                r1 = r2 = r3 = None
            if start_val is None:
                return back("/households", msg=f"表「{meter['name']}」需要填写入住读数")
            if last and start_val < last["value"]:
                return back("/households",
                            msg=f"「{meter['name']}」的读数 {start_val} 小于该表上次读数 {last['value']},请核对")
            price = _float(form.get(f"price_{mid}", ""), None)
            if price is None:
                price = last["tenant_price"] if last else 0.0
            label = str(form.get(f"label_{mid}", "")).strip()
            seats.append((meter, start_val, price, r1, r2, r3, label))

        cur = conn.execute("INSERT INTO households(name,note) VALUES (?,?)", (name, note))
        hh_id = cur.lastrowid
        for meter, start_val, price, r1, r2, r3, label in seats:
            line = conn.execute(
                "INSERT INTO lines(household_id,meter_type,label,move_in_date,move_in_value,note)"
                " VALUES (?,?,?,?,?,?)",
                (hh_id, meter["meter_type"], label, start_date, start_val, "")).lastrowid
            conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line, meter["id"]))
            conn.execute(
                "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,"
                "tenant_price,kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (line, meter["id"], start_date, start_val, r1, r2, r3, price, "move_in",
                 f"{name} 入住读数"))
    return back("/households", ok=f"已为「{name}」办理入住,共 {len(seats)} 条计量线")


@app.post("/households/edit")
def household_edit(id: int = Form(...), name: str = Form(""), note: str = Form("")):
    with connect() as conn:
        row = conn.execute("SELECT * FROM households WHERE id=?", (id,)).fetchone()
        if row:
            conn.execute("UPDATE households SET name=?, note=? WHERE id=?",
                         (name.strip() or row["name"], note.strip(), id))
    return back("/households", ok="租户已更新")


@app.post("/households/checkout")
async def household_checkout(request: Request):
    """退租:每条开放线录终读数,当场结算该租期,然后关闭。

    结算只要求算清差异,不要求尾款已收;未收部分留在台账里继续追。
    """
    form = await request.form()
    hh_id = _int(form.get("id"))
    out_date = str(form.get("move_out_date", ""))
    if not hh_id:
        return back("/households", msg="缺租户编号")
    if not out_date:
        return back(f"/households/{hh_id}/ledger", msg="请填写退租日期")
    with connect() as conn:
        lines = open_lines(conn, hh_id)
        if not lines:
            return back(f"/households/{hh_id}/ledger", msg="该租户没有在用的表,无需退租")
        pending = []
        for line in lines:
            lid = line["id"]
            meter = conn.execute(
                "SELECT m.* FROM meters m WHERE m.line_id=?", (lid,)).fetchone()
            if line["meter_type"] == "electricity" and meter and meter["phase"] == "three":
                parts = [_int(form.get(f"out_{lid}_{i}", "")) for i in (1, 2, 3)]
                if any(p is None or p < 0 for p in parts):
                    return back(f"/households/{hh_id}/ledger",
                                msg=f"「{line['label'] or line['meter_type']}」三相表需分别填写三个读数")
                value, r1, r2, r3 = sum(parts), parts[0], parts[1], parts[2]
            else:
                value = _int(form.get(f"out_{lid}", ""))
                if value is None or value < 0:
                    return back(f"/households/{hh_id}/ledger",
                                msg=f"「{line['label'] or line['meter_type']}」需要填写退租读数")
                r1 = r2 = r3 = None
            rows = line_readings(conn, lid)
            if not rows:
                return back(f"/households/{hh_id}/ledger", msg="该计量线没有读数,无法退租")
            last = rows[-1]
            if out_date <= last["reading_date"]:
                return back(f"/households/{hh_id}/ledger",
                            msg=f"退租日期需晚于上次读数日期 {last['reading_date']}")
            if value < last["value"]:
                return back(f"/households/{hh_id}/ledger",
                            msg=f"退租读数 {value} 小于上次读数 {last['value']},请核对")
            pending.append((line, value, r1, r2, r3, last["tenant_price"]))

        for line, value, r1, r2, r3, price in pending:
            lid = line["id"]
            meter = conn.execute("SELECT * FROM meters WHERE line_id=?", (lid,)).fetchone()
            conn.execute(
                "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,"
                "tenant_price,kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lid, meter["id"], out_date, value, r1, r2, r3, price, "move_out", "退租读数"))
            tail = conn.execute(
                "SELECT * FROM readings WHERE line_id=? ORDER BY reading_date DESC, id DESC LIMIT 1",
                (lid,)).fetchone()
            record_period(conn, lid, tail["id"])
            conn.execute(
                "UPDATE lines SET move_out_date=?, move_out_value=? WHERE id=?",
                (out_date, value, lid))
            conn.execute("UPDATE meters SET line_id=NULL WHERE line_id=?", (lid,))
    return back(f"/households/{hh_id}/ledger", ok="已退租,本租期账单已结算并关闭")


# ---------- 表管理 ----------

@app.get("/meters", response_class=HTMLResponse)
def meters_page(request: Request, edit: int = Query(0), swap: str = Query(""),
                msg: str = "", ok: str = ""):
    with connect() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT m.*, l.id AS line_id, h.name AS holder, h.id AS holder_id "
            "FROM meters m LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
            "LEFT JOIN households h ON h.id = l.household_id "
            "ORDER BY m.level, m.meter_type, m.id")]
    m_names = {x["id"]: x["name"] for x in rows}
    editing = next((x for x in rows if x["id"] == edit), None)
    return render("meters.html", swap=swap, title="表管理",
                  lead="维护水表与电表。分表的归属由入住产生,此处只读展示。",
                  active="meters", rows=rows, m_names=m_names, editing=editing,
                  today=date.today().isoformat(), flash=flash_from(msg, ok))


@app.post("/meters/add")
def meter_add(name: str = Form(...), meter_type: str = Form(...), level: str = Form(...),
              main_meter_id: str = Form(""), phase: str = Form("single"),
              installed_on: str = Form(""), base_value: str = Form(""),
              base_1: str = Form(""), base_2: str = Form(""), base_3: str = Form(""),
              note: str = Form("")):
    """建表。总表不填基数(读数以部门账单为准);分表必须填装表读数。"""
    if meter_type not in ("water", "electricity"):
        return back("/meters", msg="类型只能是水或电")
    if level not in ("main", "sub"):
        return back("/meters", msg="级别只能是总表或分表")
    if not name.strip():
        return back("/meters", msg="请填写表名")
    phase = phase if (meter_type == "electricity" and phase in ("single", "three")) else "single"
    installed_on = installed_on or date.today().isoformat()
    with connect() as conn:
        if level == "main":
            base_value_i = None      # 总表不设基数,读数以部门账单为准
        else:
            if phase == "three":
                parts = [_int(base_1), _int(base_2), _int(base_3)]
                if any(p is None or p < 0 for p in parts):
                    return back("/meters", msg="三相电表需分别填写三个基数")
                base_value_i = sum(parts)
            else:
                base_value_i = _int(base_value)
                if base_value_i is None or base_value_i < 0:
                    return back("/meters", msg="请填写不小于 0 的表基数")
        main_id = _int(main_meter_id)
        if level == "sub" and main_id:
            parent = conn.execute("SELECT level FROM meters WHERE id=?", (main_id,)).fetchone()
            if not parent or parent["level"] != "main":
                return back("/meters", msg="所属总表必须是一块总表")
        conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,"
            "installed_on,note) VALUES (?,?,?,?,?,?,?,?)",
            (name.strip(), meter_type, level, main_id if level == "sub" else None, phase,
             base_value_i, installed_on, note.strip()))
    return back("/meters", ok="表已新增")


@app.post("/meters/edit")
def meter_edit(id: int = Form(...), name: str = Form(""), main_meter_id: str = Form(""),
               note: str = Form("")):
    with connect() as conn:
        # 归属不在这里改:只由入住/退租产生
        conn.execute(
            "UPDATE meters SET name=COALESCE(NULLIF(?,''),name), main_meter_id=?, note=? "
            "WHERE id=?",
            (name.strip(), _int(main_meter_id), note.strip(), id))
    return back("/meters", ok="表已更新")


@app.post("/switches/add")
async def switch_add(request: Request):
    """换表:旧表终读数 + 新表基数同一天录入。

    计量线不切断 —— 旧表终读数(switch_out)与新表基数(switch_base)记在同一条线上,
    本次账期的用量按段累加。旧表退休,新表接管该线。
    """
    form = await request.form()
    old_id, new_id = _int(form.get("old_meter_id")), _int(form.get("new_meter_id"))
    switch_date = str(form.get("switch_date", ""))
    note = str(form.get("note", "")).strip()
    if not old_id or not new_id:
        return back("/meters", msg="请选择新旧表")
    if old_id == new_id:
        return back("/meters", msg="旧表和新表不能是同一块表")
    if not switch_date:
        return back("/meters", msg="请填写换表日期")

    def _read(prefix, meter, label):
        if meter["phase"] == "three":
            parts = [_int(form.get(f"{prefix}_{i}", "")) for i in (1, 2, 3)]
            if any(p is None or p < 0 for p in parts):
                return None, None, f"{label}:三相电表需分别填写三个读数"
            return sum(parts), parts, None
        value = _int(form.get(prefix, ""))
        if value is None or value < 0:
            return None, None, f"{label}:请填写读数"
        return value, None, None

    with connect() as conn:
        old_m = conn.execute("SELECT * FROM meters WHERE id=?", (old_id,)).fetchone()
        new_m = conn.execute("SELECT * FROM meters WHERE id=?", (new_id,)).fetchone()
        if not old_m or not new_m:
            return back("/meters", msg="表不存在")
        if old_m["level"] != new_m["level"] or old_m["meter_type"] != new_m["meter_type"]:
            return back("/meters", msg="新旧表必须是同类、同级")
        if old_m["retired_on"]:
            return back("/meters", msg="旧表已经换下过")

        if old_m["level"] == "main":
            # 换总表:分表改接到新总表,账期不受影响(总表不参与计费)
            old_total, old_parts, err = _read("old_final_value", old_m, "旧表终读数")
            if err:
                return back("/meters", msg=err)
            new_total, new_parts, err = _read("new_base_value", new_m, "新表基数")
            if err:
                return back("/meters", msg=err)
            conn.execute("UPDATE meters SET main_meter_id=? WHERE main_meter_id=?",
                         (new_id, old_id))
            conn.execute("UPDATE meters SET retired_on=? WHERE id=?", (switch_date, old_id))
            conn.execute("UPDATE meters SET base_value=NULL WHERE id=?", (new_id,))
            conn.execute(
                "INSERT INTO meter_switches(old_meter_id,new_meter_id,switch_date,old_final_value,"
                "new_base_value,note) VALUES (?,?,?,?,?,?)",
                (old_id, new_id, switch_date, old_total, new_total, note))
            return back("/meters", ok="已换总表,原接入的分表已改接到新表")

        line = conn.execute(
            "SELECT l.* FROM lines l WHERE l.id=? AND l.move_out_date IS NULL",
            (old_m["line_id"],)).fetchone() if old_m["line_id"] else None
        if not line:
            return back("/meters", msg="旧表当前没有租户在用,直接把它换下即可不留账期")
        old_total, old_parts, err = _read("old_final_value", old_m, "旧表终读数")
        if err:
            return back("/meters", msg=err)
        new_total, new_parts, err = _read("new_base_value", new_m, "新表基数")
        if err:
            return back("/meters", msg=err)
        rows = line_readings(conn, line["id"])
        last = rows[-1] if rows else None
        if last:
            if switch_date <= last["reading_date"]:
                return back("/meters", msg=f"换表日期需晚于上次读数日期 {last['reading_date']}")
            if old_total < last["value"]:
                return back("/meters", msg=f"旧表终读数 {old_total} 小于上次读数 {last['value']}")
        if new_m["line_id"]:
            return back("/meters", msg="新表已在某条计量线上使用")

        price = last["tenant_price"] if last else 0.0
        _legacy = old_m["phase"]
        conn.execute(
            "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,"
            "kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (line["id"], old_id, switch_date, old_total,
             old_parts[0] if old_parts else None,
             old_parts[1] if old_parts else None,
             old_parts[2] if old_parts else None,
             price, "switch_out", "旧表终读数"))
        conn.execute(
            "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,"
            "kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (line["id"], new_id, switch_date, new_total,
             new_parts[0] if new_parts else None,
             new_parts[1] if new_parts else None,
             new_parts[2] if new_parts else None,
             price, "switch_base", "新表基数"))
        conn.execute("UPDATE meters SET line_id=NULL, retired_on=? WHERE id=?",
                     (switch_date, old_id))
        conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line["id"], new_id))
        conn.execute(
            "INSERT INTO meter_switches(old_meter_id,new_meter_id,switch_date,old_final_value,"
            "new_base_value,note) VALUES (?,?,?,?,?,?)",
            (old_id, new_id, switch_date, old_total, new_total, note))
    return back("/meters", ok="换表已登记,账期不切断,用量将按新旧两段相加")


# ---------- 抄表 ----------

def _line_last(conn, line_id: int):
    return conn.execute(
        "SELECT * FROM readings WHERE line_id=? ORDER BY reading_date DESC, id DESC LIMIT 1",
        (line_id,)).fetchone()


@app.get("/readings", response_class=HTMLResponse)
def readings_page(request: Request, msg: str = "", ok: str = "", edit: int = 0,
                  line_id: str = "", household_id: str = "",
                  date_from: str = "", date_to: str = "", page: int = 1):
    today = date.today().isoformat()
    with connect() as conn:
        lines_all = [dict(x) for x in conn.execute(
            "SELECT l.*, h.name AS household_name FROM lines l "
            "JOIN households h ON h.id=l.household_id "
            "WHERE l.move_out_date IS NULL ORDER BY l.id")]
        for line in lines_all:
            last = _line_last(conn, line["id"])
            line["last_date"] = last["reading_date"] if last else line["move_in_date"]
            line["last_value"] = last["value"] if last else line["move_in_value"]
            line["last_price"] = last["tenant_price"] if last else 0.0
            line["meter"] = conn.execute(
                "SELECT * FROM meters WHERE line_id=?", (line["id"],)).fetchone()
        # 该抄的排前面:距上次读数天数多的在前
        lines_sorted = sorted(lines_all, key=lambda x: x["last_date"])
        # 筛选下拉要覆盖已退租的历史,因此从全部线取,而不只是在用线
        all_lines = [dict(x) for x in conn.execute(
            "SELECT l.id, l.household_id, l.meter_type, l.label, l.move_out_date, "
            "h.name AS household_name, "
            "COALESCE(NULLIF(l.label,''), (SELECT m.name FROM meters m WHERE m.id = "
            "  (SELECT r.meter_id FROM readings r WHERE r.line_id=l.id "
            "   ORDER BY r.reading_date DESC, r.id DESC LIMIT 1))) AS display_name "
            "FROM lines l JOIN households h ON h.id=l.household_id ORDER BY l.id")]
        hh_items = sorted({(x["household_id"], x["household_name"])
                           for x in all_lines}, key=lambda kv: kv[1])

        rows = all_periods(conn, household_id=_int(household_id) or None,
                           date_from=date_from, date_to=date_to)
        if line_id:
            rows = [r for r in rows if str(r["line_id"]) == line_id]
        total_usage_w = sum(r["usage"] for r in rows if r["meter_type"] == "water")
        total_usage_e = sum(r["usage"] for r in rows if r["meter_type"] == "electricity")
        total_amount = round(sum(r["amount"] for r in rows), 2)

        # 读数记录(明细),按日期倒序
        rwhere, rparams = ["r.kind <> 'switch_base'"], []
        if household_id:
            rwhere.append("l.household_id = ?")
            rparams.append(_int(household_id))
        if line_id:
            rwhere.append("r.line_id = ?")
            rparams.append(_int(line_id))
        if date_from:
            rwhere.append("r.reading_date >= ?")
            rparams.append(date_from)
        if date_to:
            rwhere.append("r.reading_date <= ?")
            rparams.append(date_to)
        clause = "WHERE " + " AND ".join(rwhere)
        total = conn.execute(
            f"SELECT COUNT(*) FROM readings r JOIN lines l ON r.line_id=l.id {clause}",
            rparams).fetchone()[0]
        page, pages, pager = paginate(
            total, page,
            f"line_id={line_id}&household_id={household_id}&date_from={date_from}&date_to={date_to}")
        detail = conn.execute(
            f"SELECT r.*, l.household_id, l.meter_type, l.label, h.name AS household_name, "
            f"m.name AS meter_name, m.phase, m.line_id AS m_line "
            f"FROM readings r JOIN lines l ON r.line_id=l.id "
            f"JOIN households h ON h.id=l.household_id JOIN meters m ON r.meter_id=m.id "
            f"{clause} ORDER BY r.reading_date DESC, r.id DESC LIMIT ? OFFSET ?",
            rparams + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()

        all_readings = [dict(x) for x in detail]
        for x in all_readings:
            x["can_edit"] = _reading_editable(conn, x)
    return render("readings.html", title="抄表",
                  lead="记录各计量线的读数。抄表即当场算出该账期的应收。",
                  active="readings", lines=lines_sorted, all_lines=all_lines,
                  hh_items=hh_items,
                  rows=rows, detail=all_readings, total=total,
                  total_usage_w=total_usage_w, total_usage_e=total_usage_e,
                  total_amount=total_amount, page=page, pages=pages, pager=pager,
                  today=today, line_id=line_id, household_id=household_id,
                  date_from=date_from, date_to=date_to, edit=edit,
                  flash=flash_from(msg, ok))


def _reading_editable(conn, reading: dict) -> bool:
    """读数可改的条件:不是入户读数、且它此后没有已收到的钱。"""
    if reading["kind"] == "move_in":
        return False
    line = get_line(conn, reading["line_id"])
    if line is None:
        return False
    if touched_by_reading(conn, line["household_id"], reading["reading_date"], inserting=False):
        return False
    return not line["move_out_date"]


@app.post("/readings/add")
async def reading_add(request: Request):
    """抄表:在一条开放计量线上记录新读数,当场生成该账期的应收。"""
    form = await request.form()
    line_id = _int(form.get("line_id"))
    meter_id = _int(form.get("meter_id"))
    reading_date = str(form.get("reading_date", "")).strip()
    note = str(form.get("note", "")).strip()
    if not line_id or not reading_date:
        return back("/readings", msg="请选择计量线与抄表日期")
    with connect() as conn:
        line = conn.execute(
            "SELECT l.*, h.name AS household_name FROM lines l JOIN households h ON h.id=l.household_id "
            "WHERE l.id=?", (line_id,)).fetchone()
        if not line:
            return back("/readings", msg="计量线不存在")
        if line["move_out_date"]:
            return back("/readings", msg="该租期已结束,不能再抄表")
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter or meter["line_id"] != line_id:
            return back("/readings", msg="这块表不在该计量线上,请刷新页面重选")
        if meter["phase"] == "three":
            parts = [_int(form.get(f"value_{i}", "")) for i in (1, 2, 3)]
            if any(p is None or p < 0 for p in parts):
                return back("/readings", msg="三相电表需要分别填写三个读数")
            value, r1, r2, r3 = sum(parts), parts[0], parts[1], parts[2]
        else:
            value = _int(form.get("value", ""))
            if value is None or value < 0:
                return back("/readings", msg="请填写本次读数")
            r1 = r2 = r3 = None
        # 与前后相邻读数比较,因此可以补录到中间(ADR-0021)
        prev = conn.execute(
            "SELECT * FROM readings WHERE line_id=? AND (reading_date < ? OR "
            "(reading_date = ? AND ? = 0)) ORDER BY reading_date DESC, id DESC LIMIT 1",
            (line_id, reading_date, reading_date, 0)).fetchone()
        nxt = conn.execute(
            "SELECT * FROM readings WHERE line_id=? AND reading_date > ? "
            "ORDER BY reading_date, id LIMIT 1", (line_id, reading_date)).fetchone()
        if prev is None:
            prev = conn.execute(
                "SELECT * FROM readings WHERE line_id=? ORDER BY reading_date, id LIMIT 1",
                (line_id,)).fetchone()
        if prev is not None and prev["reading_date"] >= reading_date:
            return back("/readings", msg=f"抄表日期需晚于前一条读数 {prev['reading_date']}")
        if nxt is not None and nxt["reading_date"] <= reading_date:
            return back("/readings", msg=f"抄表日期需早于后一条读数 {nxt['reading_date']}")
        if prev is not None and prev["meter_id"] == meter_id:
            if value < prev["value"]:
                return back("/readings",
                            msg=f"本次读数 {value} 小于前一条读数 {prev['value']},请核对")
            if (meter["phase"] == "three" and prev["r1"] is not None
                    and (r1 < prev["r1"] or r2 < prev["r2"] or r3 < prev["r3"])):
                return back("/readings", msg="某一相读数小于前一条,请核对")
        if nxt is not None and nxt["meter_id"] == meter_id and value > nxt["value"]:
            return back("/readings", msg=f"本次读数 {value} 大于后一条读数 {nxt['value']},请核对")
        if prev is None and value < line["move_in_value"]:
            return back("/readings", msg=f"读数不得小于入住读数 {line['move_in_value']}")
        # 插进一个已收款的账期会把它一分为二并重排冲抵,因此禁止(ADR-0015/0021)
        hit = touched_by_reading(conn, line["household_id"], reading_date, inserting=True)
        if hit:
            return back("/readings",
                        msg=f"{hit['start']} ~ {hit['end']} 这一期已收款,不能在其区间内补录。"
                            "请先删除对应收款。")
        price = _float(form.get("tenant_price", ""), None)
        if price is None:
            base_price = nxt or prev
            price = base_price["tenant_price"] if base_price else 0.0
        conn.execute(
            "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,"
            "kind,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (line_id, meter_id, reading_date, value, r1, r2, r3, price, "periodic", note))
        recompute_line(conn, line_id)
        p = conn.execute("SELECT * FROM receivables WHERE line_id=? AND period_end=?",
                         (line_id, reading_date)).fetchone()
    amount = p["amount"] if p else 0
    return back("/readings", ok=f"抄表已保存,本账期应收 {amount:.2f} 元")


@app.post("/readings/edit")
async def reading_edit(request: Request):
    """改读数。已开始收款的账期锁死,须先删收款;入户读数不可改(它是租期起点)。"""
    form = await request.form()
    rid = _int(form.get("id"))
    with connect() as conn:
        row = conn.execute("SELECT * FROM readings WHERE id=?", (rid,)).fetchone()
        if not row:
            return back("/readings", msg="找不到这条读数")
        if row["kind"] == "move_in":
            return back("/readings", msg="入户读数是租期起点,不能修改;要改请退租后重新入住")
        if row["kind"] in ("switch_out", "switch_base"):
            return back("/readings", msg="换表读数是换表登记的一部分,不能在此修改")
        line = get_line(conn, row["line_id"])
        if line is None:
            return back("/readings", msg="计量线不存在")
        if line["move_out_date"]:
            return back("/readings", msg="该租期已结束,读数锁定")
        hit = touched_by_reading(conn, line["household_id"], row["reading_date"], inserting=False)
        if hit:
            return back("/readings",
                        msg=f"{hit['start']} ~ {hit['end']} 这一期已收款,不能改读数。"
                            "请先删除对应收款。")
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (row["meter_id"],)).fetchone()
        reading_date = str(form.get("reading_date", "")).strip() or row["reading_date"]
        if meter["phase"] == "three":
            parts = [_int(form.get(f"value_{i}", "")) for i in (1, 2, 3)]
            if any(p is None or p < 0 for p in parts):
                return back("/readings", msg="三相电表需要分别填写三个读数")
            value, r1, r2, r3 = sum(parts), parts[0], parts[1], parts[2]
        else:
            value = _int(form.get("value", ""))
            if value is None or value < 0:
                return back("/readings", msg="请填写读数")
            r1 = r2 = r3 = None
        price = _float(form.get("tenant_price", ""), None)
        if price is None:
            price = row["tenant_price"]
        prev = conn.execute(
            "SELECT * FROM readings WHERE line_id=? AND (reading_date < ? OR "
            "(reading_date = ? AND id < ?)) ORDER BY reading_date DESC, id DESC LIMIT 1",
            (row["line_id"], row["reading_date"], row["reading_date"], rid)).fetchone()
        nxt = conn.execute(
            "SELECT * FROM readings WHERE line_id=? AND (reading_date > ? OR "
            "(reading_date = ? AND id > ?)) ORDER BY reading_date, id LIMIT 1",
            (row["line_id"], row["reading_date"], row["reading_date"], rid)).fetchone()
        if prev and prev["meter_id"] == row["meter_id"] and (
                value < prev["value"] or reading_date <= prev["reading_date"]):
            return back("/readings", msg=f"读数需不小于上一条 {prev['value']},"
                                        f"日期需晚于 {prev['reading_date']}")
        if nxt and nxt["meter_id"] == row["meter_id"] and (
                value > nxt["value"] or reading_date >= nxt["reading_date"]):
            return back("/readings", msg=f"读数不能大于下一条 {nxt['value']},"
                                        f"日期需早于 {nxt['reading_date']}")
        conn.execute(
            "UPDATE readings SET reading_date=?, value=?, tenant_price=?, r1=?, r2=?, r3=? "
            "WHERE id=?", (reading_date, value, price, r1, r2, r3, rid))
        recompute_line(conn, row["line_id"])
    return back("/readings", ok="读数已更新,相关账期已重算")


@app.post("/readings/delete")
def reading_delete(id: int = Form(...)):
    """撤回读数:未收款的才能撤。撤后前后两期重算,孤立的应收一并清掉。"""
    with connect() as conn:
        row = conn.execute("SELECT * FROM readings WHERE id=?", (id,)).fetchone()
        if not row:
            return back("/readings", msg="记录不存在")
        if row["kind"] != "periodic":
            return back("/readings", msg="只有常规抄表的读数可以撤回")
        line = get_line(conn, row["line_id"])
        if line is None:
            return back("/readings", msg="计量线不存在")
        hit = touched_by_reading(conn, line["household_id"], row["reading_date"], inserting=False)
        if hit:
            return back("/readings",
                        msg=f"{hit['start']} ~ {hit['end']} 这一期已收款,不能撤回。"
                            "请先删除对应收款。")
        if conn.execute("SELECT 1 FROM lines WHERE move_out_date IS NOT NULL AND id=?",
                        (row["line_id"],)).fetchone():
            return back("/readings", msg="该租期已结束,读数锁定")
        conn.execute("DELETE FROM readings WHERE id=?", (id,))
        recompute_line(conn, row["line_id"])
    return back("/readings", ok="已撤回该条抄表,相关账期已重算")


# ---------- 缴费 ----------

@app.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, msg: str = "", ok: str = "", page: int = 1,
                  meter_id: str = "", date_from: str = "", date_to: str = ""):
    with connect() as conn:
        meters = [dict(x) for x in conn.execute(
            "SELECT * FROM meters WHERE level='main' AND retired_on IS NULL "
            "ORDER BY meter_type, id")]
        where, params = [], []
        if meter_id:
            where.append("p.meter_id = ?")
            params.append(_int(meter_id))
        if date_from:
            where.append("p.payment_date >= ?")
            params.append(date_from)
        if date_to:
            where.append("p.payment_date <= ?")
            params.append(date_to)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        agg = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(p.amount),0) s FROM payments p {clause}",
            params).fetchone()
        page, pages, pager = paginate(
            agg["n"], page, f"meter_id={meter_id}&date_from={date_from}&date_to={date_to}")
        rows = [dict(x) for x in conn.execute(
            f"SELECT p.*, m.name AS meter_name, m.meter_type FROM payments p "
            f"JOIN meters m ON p.meter_id=m.id {clause} "
            f"ORDER BY p.payment_date DESC, p.id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])]
        per_meter = [dict(x) for x in conn.execute(
            "SELECT m.id, m.name, m.meter_type, COALESCE(SUM(p.amount),0) AS total "
            "FROM meters m LEFT JOIN payments p ON p.meter_id=m.id "
            "WHERE m.level='main' GROUP BY m.id ORDER BY m.meter_type, m.id")]
    return render("payments.html", title="缴费",
                  lead="照水务、电力出具的账单录入:日期、总表、读数、单价、金额。系统不推算金额。",
                  active="payments", meters=meters, rows=rows, agg=agg,
                  page=page, pages=pages, pager=pager,
                  today=date.today().isoformat(),
                  meter_id=meter_id, date_from=date_from, date_to=date_to,
                  per_meter=per_meter, flash=flash_from(msg, ok))


@app.post("/payments/add")
def payment_add(meter_id: int = Form(...), payment_date: str = Form(...),
                reading_value: str = Form(""), unit_price: str = Form(""),
                amount: str = Form(...), note: str = Form("")):
    value = _float(amount)
    if value is None:
        return back("/payments", msg="请填写账单金额")
    with connect() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter or meter["level"] != "main":
            return back("/payments", msg="缴费只能记在总表上")
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,reading_value,unit_price,amount,note) "
            "VALUES (?,?,?,?,?,?)",
            (meter_id, payment_date, _int(reading_value), _float(unit_price),
             round(value, 2), note.strip()))
    return back("/payments", ok=f"缴费记录已保存,金额 {value:.2f} 元")


@app.post("/payments/delete")
def payment_delete(id: int = Form(...)):
    with connect() as conn:
        conn.execute("DELETE FROM payments WHERE id=?", (id,))
    return back("/payments", ok="缴费记录已删除")


# ---------- 统计 ----------

def _query_rows(conn, type: str, household_id: str, line_id: str,
                date_from: str, date_to: str) -> list[dict]:
    types = ["water", "electricity"] if type == "all" else [type]
    rows = []
    for t in types:
        rows += all_periods(conn, meter_type=t,
                            household_id=_int(household_id) or None,
                            date_from=date_from, date_to=date_to)
    if line_id:
        rows = [r for r in rows if str(r["line_id"]) == line_id]
    rows.sort(key=lambda r: (r["end"], r["line_id"]))
    return rows


@app.get("/statistics", response_class=HTMLResponse)
def statistics_page(request: Request, type: str = Query("all"), household_id: str = Query(""),
                    line_id: str = Query(""), date_from: str = Query(""),
                    date_to: str = Query(""), page: int = Query(1)):
    with connect() as conn:
        rows = _query_rows(conn, type, household_id, line_id, date_from, date_to)
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
        lines = [dict(x) for x in conn.execute(
            "SELECT l.id, l.label, l.meter_type, h.name AS household_name, m.name AS meter_name, "
            "COALESCE(NULLIF(l.label,''), m.name, h.name) AS display_name "
            "FROM lines l JOIN households h ON h.id=l.household_id "
            "LEFT JOIN meters m ON m.line_id=l.id ORDER BY l.id")]
        # 缴费按水电分列,毛差必须与应收口径一致:只看水就只减水费
        paid_by_type = {}
        for t in ("water", "electricity"):
            paid_by_type[t] = round(conn.execute(
                "SELECT COALESCE(SUM(p.amount),0) FROM payments p JOIN meters m ON p.meter_id=m.id "
                "WHERE m.meter_type=? AND p.payment_date >= ? AND p.payment_date <= ?",
                (t, date_from or "0000-01-01", date_to or "9999-12-31")).fetchone()[0], 2)
        paid_total = (paid_by_type["water"] + paid_by_type["electricity"]
                      if type == "all" else paid_by_type[type])
        gaps = vacancy_usage(conn, date_from, date_to)

    total_water = sum(x["usage"] for x in rows if x["meter_type"] == "water")
    total_elec = sum(x["usage"] for x in rows if x["meter_type"] == "electricity")
    total_amount = round(sum(x["amount"] for x in rows), 2)
    margin = round(total_amount - paid_total, 2)
    gap_usage = sum(g["usage"] for g in gaps)

    qs = (f"type={type}&household_id={household_id}&line_id={line_id}"
          f"&date_from={date_from}&date_to={date_to}")
    page, pages, pager = paginate(len(rows), page, qs)
    page_rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    return render("statistics.html", title="统计与查询",
                  lead="按租户、计量线、账期查询用量与费用;并对比向部门缴出与向租户收取。",
                  active="statistics", type=type, household_id=household_id,
                  line_id=line_id, date_from=date_from, date_to=date_to,
                  households=households, lines=lines, rows=rows, page_rows=page_rows,
                  page=page, pages=pages, pager=pager, qs=qs,
                  total_water=total_water, total_elec=total_elec,
                  total_amount=total_amount, paid_total=paid_total,
                  paid_by_type=paid_by_type, margin=margin,
                  gaps=gaps, gap_usage=gap_usage)


@app.get("/statistics/pdf")
def statistics_pdf(type: str = Query("all"), household_id: str = Query(""),
                   line_id: str = Query(""), date_from: str = Query(""),
                   date_to: str = Query("")):
    from fpdf import FPDF

    with connect() as conn:
        rows = _query_rows(conn, type, household_id, line_id, date_from, date_to)

    font_path = os.environ.get("SDF_PDF_FONT", "C:/Windows/Fonts/simhei.ttf")
    has_cjk = Path(font_path).exists()

    pdf = FPDF(orientation="L")
    pdf.add_page()
    if has_cjk:
        pdf.add_font("cjk", fname=font_path)
        pdf.set_font("cjk", size=10)
        title = "水电费统计报表"
        headers = ["租户", "计量线", "账期起", "账期止", "用量", "单价", "金额(元)"]
    else:
        pdf.set_font("Helvetica", size=9)
        title = "Utility Bill Report"
        headers = ["Household", "Line", "Start", "End", "Usage", "Price", "Amount"]
    widths = [42, 62, 28, 28, 24, 24, 36]

    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font_size(9)
    for head, w in zip(headers, widths):
        pdf.cell(w, 8, head, border=1, align="C")
    pdf.ln()
    for x in rows:
        name = x.get("display_name") or x.get("meter_name") or ""
        vals = [x["household_name"], name, x.get("start", ""), x["end"],
                f"{x['usage']:,}", x["unit_price"], f"{x['amount']:,.2f}"]
        for val, w in zip(vals, widths):
            pdf.cell(w, 8, str(val)[:30], border=1)
        pdf.ln()

    data = bytes(pdf.output())
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=bills.pdf"})
