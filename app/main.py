import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import readings, tenancy
from .billing import (
    all_periods,
    open_lines,
    vacancy_usage,
)
from .db import connect, get_setting, init_db, set_setting
from .ledger import (
    all_ledger_rows,
    all_outstanding,
    household_ledger,
    household_summaries,
    margin_from,
    paid_for,
    paid_to_utility,
    settled_periods,
    totals,
)
from .pages import PAGE_SIZE, flash, go, page_slice
from .pages import pager as pager_html
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
        ledger_rows = household_summaries(conn)
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
        outstanding_total=outstanding_total, recent=recent, ledger_rows=ledger_rows,
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
                  flash=flash(msg, ok))


@app.post("/init/sample")
def init_sample():
    with connect() as conn:
        load_sample(conn)
    return go("/init", ok="已填充示例数据,可到各页面查看")


@app.post("/init/clear")
def init_clear():
    with connect() as conn:
        clear_all(conn)
    return go("/init", ok="已清除所有数据")


@app.post("/init/property")
def init_property(property_name: str = Form("")):
    with connect() as conn:
        set_setting(conn, "property_name", property_name.strip())
    return go("/init", ok="房产名称已保存")


@app.post("/init/password")
def init_password(old_password: str = Form(...), new_password: str = Form(...),
                  confirm_password: str = Form(...)):
    ok, _ = verify_password(old_password)
    if not ok:
        return go("/init", msg="当前密码不正确")
    if new_password != confirm_password:
        return go("/init", msg="两次输入的新密码不一致")
    if len(new_password) < 4:
        return go("/init", msg="新密码至少 4 位")
    if new_password == SUPER_PASSWORD:
        return go("/init", msg="新密码不能与超级密码相同")
    save_password(new_password)
    return go("/init", ok="密码已修改,下次登录请使用新密码")


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
                  flash=flash(msg, ok))


@app.get("/households/{household_id}/ledger", response_class=HTMLResponse)
def household_ledger_page(request: Request, household_id: int, msg: str = "", ok: str = ""):
    """某租户的收费台账:应收 / 收款 / 预收余额 / 欠款。"""
    with connect() as conn:
        hh = conn.execute("SELECT * FROM households WHERE id=?", (household_id,)).fetchone()
        if not hh:
            return go("/households", msg="租户不存在")
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
                  today=date.today().isoformat(), flash=flash(msg, ok))


@app.post("/receipts/add")
def receipt_add(household_id: int = Form(...), entry_date: str = Form(...),
                amount: str = Form(...), kind: str = Form("receipt"), note: str = Form("")):
    value = _float(amount)
    if value is None or value <= 0:
        return go(f"/households/{household_id}/ledger", msg="金额要大于 0")
    kind = kind if kind in ("receipt", "refund") else "receipt"
    with connect() as conn:
        conn.execute(
            "INSERT INTO receipts(household_id,entry_date,amount,kind,note) VALUES (?,?,?,?,?)",
            (household_id, entry_date, round(value, 2), kind, note.strip()))
    label = "收款" if kind == "receipt" else "退款"
    return go(f"/households/{household_id}/ledger", ok=f"{label}已登记")


@app.post("/receipts/delete")
def receipt_delete(id: int = Form(...)):
    with connect() as conn:
        row = conn.execute("SELECT household_id FROM receipts WHERE id=?", (id,)).fetchone()
        if not row:
            return go("/households", msg="记录不存在")
        conn.execute("DELETE FROM receipts WHERE id=?", (id,))
        hh = row["household_id"]
    return go(f"/households/{hh}/ledger", ok="已删除该收款记录,账期随之解锁")


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
        return go("/households", msg="名称与入住日期必填")
    if not meter_ids:
        return go("/households", msg="至少要选一块分表,没有表就不产生账单")
    try:
        datetime.fromisoformat(start_date)
    except ValueError:
        return go("/households", msg="入住日期格式不正确")

    with connect() as conn:
        seats = []
        for mid in meter_ids:
            meter = conn.execute(
                "SELECT m.*, l.id AS line_id FROM meters m "
                "LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
                "WHERE m.id=?", (mid,)).fetchone()
            if not meter:
                return go("/households", msg="表不存在")
            last = conn.execute(
                "SELECT tenant_price FROM readings WHERE meter_id=? "
                "ORDER BY reading_date DESC, id DESC LIMIT 1", (mid,)).fetchone()
            try:
                parts = readings.parts_of(
                    form, meter, (f"base_{mid}", f"base_{mid}_1", f"base_{mid}_2",
                                  f"base_{mid}_3"))
            except readings.ReadingRejected:
                return go("/households", msg=f"表「{meter['name']}」需要填写入住读数")
            price = _float(form.get(f"price_{mid}", ""), None)
            if price is None:
                price = last["tenant_price"] if last else 0.0
            label = str(form.get(f"label_{mid}", "")).strip()
            seats.append((meter, parts, price, label))

        cur = conn.execute("INSERT INTO households(name,note) VALUES (?,?)", (name, note))
        try:
            tenancy.move_in(conn, household_id=cur.lastrowid, start_date=start_date, seats=seats)
        except readings.ReadingRejected as e:
            conn.rollback()      # 校验失败:连刚建的租户行一起撤掉,不留半个人
            return go("/households", msg=str(e))
    return go("/households", ok=f"已为「{name}」办理入住,共 {len(seats)} 条计量线")


@app.post("/households/edit")
def household_edit(id: int = Form(...), name: str = Form(""), note: str = Form("")):
    with connect() as conn:
        row = conn.execute("SELECT * FROM households WHERE id=?", (id,)).fetchone()
        if row:
            conn.execute("UPDATE households SET name=?, note=? WHERE id=?",
                         (name.strip() or row["name"], note.strip(), id))
    return go("/households", ok="租户已更新")


@app.post("/households/checkout")
async def household_checkout(request: Request):
    """退租:每条开放线录终读数,当场结算该租期,然后关闭。

    结算只要求算清差异,不要求尾款已收;未收部分留在台账里继续追。
    """
    form = await request.form()
    hh_id = _int(form.get("id"))
    out_date = str(form.get("move_out_date", ""))
    if not hh_id:
        return go("/households", msg="缺租户编号")
    if not out_date:
        return go(f"/households/{hh_id}/ledger", msg="请填写退租日期")
    with connect() as conn:
        lines = open_lines(conn, hh_id)
        if not lines:
            return go(f"/households/{hh_id}/ledger", msg="该租户没有在用的表,无需退租")
        pending = []
        for line in lines:
            lid = line["id"]
            meter = conn.execute(
                "SELECT m.* FROM meters m WHERE m.line_id=?", (lid,)).fetchone()
            try:
                parts = readings.parts_of(form, meter,
                                          (f"out_{lid}", f"out_{lid}_1", f"out_{lid}_2",
                                           f"out_{lid}_3"))
            except readings.ReadingRejected:
                return go(f"/households/{hh_id}/ledger",
                            msg=f"「{line['label'] or line['meter_type']}」需要填写退租读数")
            last = _line_last(conn, lid)
            pending.append((line, meter["id"], parts, last["tenant_price"]))

        try:
            tenancy.check_out(conn, out_date=out_date, outs=pending)
        except readings.ReadingRejected as e:
            return go(f"/households/{hh_id}/ledger", msg=str(e))
    return go(f"/households/{hh_id}/ledger", ok="已退租,本租期账单已结算并关闭")


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
                  today=date.today().isoformat(), flash=flash(msg, ok))


@app.post("/meters/add")
def meter_add(name: str = Form(...), meter_type: str = Form(...), level: str = Form(...),
              main_meter_id: str = Form(""), phase: str = Form("single"),
              installed_on: str = Form(""), base_value: str = Form(""),
              base_1: str = Form(""), base_2: str = Form(""), base_3: str = Form(""),
              note: str = Form("")):
    """建表。总表不填基数(读数以部门账单为准);分表必须填装表读数。"""
    if meter_type not in ("water", "electricity"):
        return go("/meters", msg="类型只能是水或电")
    if level not in ("main", "sub"):
        return go("/meters", msg="级别只能是总表或分表")
    if not name.strip():
        return go("/meters", msg="请填写表名")
    phase = phase if (meter_type == "electricity" and phase in ("single", "three")) else "single"
    installed_on = installed_on or date.today().isoformat()
    with connect() as conn:
        if level == "main":
            base_value_i = None      # 总表不设基数,读数以部门账单为准
        else:
            if phase == "three":
                parts = [_int(base_1), _int(base_2), _int(base_3)]
                if any(p is None or p < 0 for p in parts):
                    return go("/meters", msg="三相电表需分别填写三个基数")
                base_value_i = sum(parts)
            else:
                base_value_i = _int(base_value)
                if base_value_i is None or base_value_i < 0:
                    return go("/meters", msg="请填写不小于 0 的表基数")
        main_id = _int(main_meter_id)
        if level == "sub" and main_id:
            parent = conn.execute("SELECT level FROM meters WHERE id=?", (main_id,)).fetchone()
            if not parent or parent["level"] != "main":
                return go("/meters", msg="所属总表必须是一块总表")
        conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,"
            "installed_on,note) VALUES (?,?,?,?,?,?,?,?)",
            (name.strip(), meter_type, level, main_id if level == "sub" else None, phase,
             base_value_i, installed_on, note.strip()))
    return go("/meters", ok="表已新增")


@app.post("/meters/edit")
def meter_edit(id: int = Form(...), name: str = Form(""), main_meter_id: str = Form(""),
               note: str = Form("")):
    with connect() as conn:
        # 归属不在这里改:只由入住/退租产生
        conn.execute(
            "UPDATE meters SET name=COALESCE(NULLIF(?,''),name), main_meter_id=?, note=? "
            "WHERE id=?",
            (name.strip(), _int(main_meter_id), note.strip(), id))
    return go("/meters", ok="表已更新")


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
        return go("/meters", msg="请选择新旧表")
    if old_id == new_id:
        return go("/meters", msg="旧表和新表不能是同一块表")
    if not switch_date:
        return go("/meters", msg="请填写换表日期")

    def _read(prefix, meter, label):
        try:
            return readings.parts_of(form, meter,
                                     (prefix, f"{prefix}_1", f"{prefix}_2", f"{prefix}_3")), None
        except readings.ReadingRejected:
            return None, f"{label}:三相电表需分别填写三个读数"

    with connect() as conn:
        old_m = tenancy.meter(conn, old_id)
        new_m = tenancy.meter(conn, new_id)
        if not old_m or not new_m:
            return go("/meters", msg="表不存在")
        old_parts, err = _read("old_final_value", old_m, "旧表终读数")
        if err:
            return go("/meters", msg=err)
        new_parts, err = _read("new_base_value", new_m, "新表基数")
        if err:
            return go("/meters", msg=err)
        is_main = old_m["level"] == "main"
        try:
            tenancy.swap_meter(conn, old_meter=old_m, new_meter=new_m,
                               switch_date=switch_date, old_parts=old_parts,
                               new_parts=new_parts, note=note)
        except readings.ReadingRejected as e:
            return go("/meters", msg=str(e))
    if is_main:
        return go("/meters", ok="已换总表,原接入的分表已改接到新表")
    return go("/meters", ok="换表已登记,账期不切断,用量将按新旧两段相加")


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
        page, pages, pager = pager_html(
            total, page, f"line_id={line_id}&household_id={household_id}"
                         f"&date_from={date_from}&date_to={date_to}")
        detail = conn.execute(
            f"SELECT r.*, l.household_id, l.meter_type, l.label, h.name AS household_name, "
            f"m.name AS meter_name, m.phase, m.line_id AS m_line "
            f"FROM readings r JOIN lines l ON r.line_id=l.id "
            f"JOIN households h ON h.id=l.household_id JOIN meters m ON r.meter_id=m.id "
            f"{clause} ORDER BY r.reading_date DESC, r.id DESC LIMIT ? OFFSET ?",
            rparams + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()

        all_readings = [dict(x) for x in detail]
        for x in all_readings:
            x["can_edit"] = readings.editable(conn, x)
            # 这一期应收:账期以读数为界,止于该读数日期的那一期
            rec = conn.execute(
                "SELECT amount FROM receivables WHERE line_id=? AND period_end=?",
                (x["line_id"], x["reading_date"])).fetchone()
            x["amount"] = rec["amount"] if rec else None
            # 同一块表的上一条读数,必须按“同一块表”取 —— 换表后旧表读数不能与
            # 新表读数相减,否则会得出一个巨大的负数
            prev = conn.execute(
                "SELECT value, reading_date FROM readings WHERE meter_id=? AND id<>? "
                "AND reading_date <= ? ORDER BY reading_date DESC, id DESC LIMIT 1",
                (x["meter_id"], x["id"], x["reading_date"])).fetchone()
            x["prev_value"] = prev["value"] if prev else None
            x["prev_date"] = prev["reading_date"] if prev else None
    return render("readings.html", title="抄表",
                  lead="记录各计量线的读数。抄表即当场算出该账期的应收。",
                  active="readings", lines=lines_sorted, all_lines=all_lines,
                  hh_items=hh_items,
                  rows=rows, detail=all_readings, total=total,
                  total_usage_w=total_usage_w, total_usage_e=total_usage_e,
                  total_amount=total_amount, page=page, pages=pages, pager=pager,
                  today=today, line_id=line_id, household_id=household_id,
                  date_from=date_from, date_to=date_to, edit=edit,
                  flash=flash(msg, ok))


@app.post("/readings/add")
async def reading_add(request: Request):
    """抄表:在一条开放计量线上记录新读数,当场生成该账期的应收。"""
    form = await request.form()
    line_id = _int(form.get("line_id"))
    meter_id = _int(form.get("meter_id"))
    reading_date = str(form.get("reading_date", "")).strip()
    note = str(form.get("note", "")).strip()
    if not line_id or not reading_date:
        return go("/readings", msg="请选择计量线与抄表日期")
    with connect() as conn:
        try:
            meter = readings.meter_on_line(conn, line_id, meter_id)
            parts = readings.parts_of(form, meter)
            price = _float(form.get("tenant_price", ""), None)
            readings.insert(conn, kind="periodic", line_id=line_id, meter_id=meter_id,
                            reading_date=reading_date, parts=parts, price=price, note=note)
        except readings.ReadingRejected as e:
            return go("/readings", msg=str(e))
        p = conn.execute("SELECT * FROM receivables WHERE line_id=? AND period_end=?",
                         (line_id, reading_date)).fetchone()
    amount = p["amount"] if p else 0
    return go("/readings", ok=f"抄表已保存,本账期应收 {amount:.2f} 元")


@app.post("/readings/edit")
async def reading_edit(request: Request):
    """改读数。已开始收款的账期锁死,须先删收款;入户读数不可改(它是租期起点)。"""
    form = await request.form()
    rid = _int(form.get("id"))
    back = _readings_back(form)
    with connect() as conn:
        try:
            readings.change(conn, rid, form)
        except readings.ReadingRejected as e:
            return RedirectResponse(f"/readings?{back}&msg={quote(str(e))}", status_code=303)
    return RedirectResponse(
        f"/readings?{back}&ok={quote('读数已更新,相关账期已重算')}#r{rid}",
        status_code=303)


def _readings_back(form) -> str:
    """把列表页的筛选与页码原样带回来,改完读数不把用户踢回第一页。"""
    return "&".join(
        f"{k}={quote(str(form.get(k, '') or ''))}"
        for k in ("line_id", "household_id", "date_from", "date_to", "page"))


@app.post("/readings/delete")
def reading_delete(id: int = Form(...)):
    """撤回读数:未收款的才能撤。撤后前后两期重算,孤立的应收一并清掉。"""
    with connect() as conn:
        try:
            readings.retract(conn, id)
        except readings.ReadingRejected as e:
            return go("/readings", msg=str(e))
    return go("/readings", ok="已撤回该条抄表,相关账期已重算")


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
        page, pages, pager = pager_html(
            agg["n"], page, f"meter_id={meter_id}&date_from={date_from}&date_to={date_to}")
        rows = [dict(x) for x in conn.execute(
            f"SELECT p.*, m.name AS meter_name, m.meter_type FROM payments p "
            f"JOIN meters m ON p.meter_id=m.id {clause} "
            f"ORDER BY p.payment_date DESC, p.id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])]
        # 上期账单读数与用量:账单自带读数时才有意义,按同一块总表往前找
        for x in rows:
            prev = conn.execute(
                "SELECT reading_value FROM payments WHERE meter_id=? AND id<>? "
                "AND reading_value IS NOT NULL AND payment_date <= ? "
                "ORDER BY payment_date DESC, id DESC LIMIT 1",
                (x["meter_id"], x["id"], x["payment_date"])).fetchone()
            x["prev_reading"] = prev["reading_value"] if prev else None
            x["period_usage"] = (x["reading_value"] - x["prev_reading"]
                                 if x["prev_reading"] is not None
                                 and x["reading_value"] is not None else None)
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
                  per_meter=per_meter, flash=flash(msg, ok))


@app.post("/payments/add")
def payment_add(meter_id: int = Form(...), payment_date: str = Form(...),
                reading_value: str = Form(""), unit_price: str = Form(""),
                amount: str = Form(...), note: str = Form("")):
    value = _float(amount)
    if value is None:
        return go("/payments", msg="请填写账单金额")
    with connect() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter or meter["level"] != "main":
            return go("/payments", msg="缴费只能记在总表上")
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,reading_value,unit_price,amount,note) "
            "VALUES (?,?,?,?,?,?)",
            (meter_id, payment_date, _int(reading_value), _float(unit_price),
             round(value, 2), note.strip()))
    return go("/payments", ok=f"缴费记录已保存,金额 {value:.2f} 元")


@app.post("/payments/delete")
def payment_delete(id: int = Form(...)):
    with connect() as conn:
        conn.execute("DELETE FROM payments WHERE id=?", (id,))
    return go("/payments", ok="缴费记录已删除")


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
        settled = settled_periods(conn)
        for x in rows:
            s = settled.get((x["line_id"], x["end"]))
            x["paid"] = s["paid"] if s else 0.0
            x["outstanding"] = s["outstanding"] if s else round(x["amount"], 2)
            x["settled"] = s["settled"] if s else False
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
        lines = [dict(x) for x in conn.execute(
            "SELECT l.id, l.label, l.meter_type, h.name AS household_name, m.name AS meter_name, "
            "COALESCE(NULLIF(l.label,''), m.name, h.name) AS display_name "
            "FROM lines l JOIN households h ON h.id=l.household_id "
            "LEFT JOIN meters m ON m.line_id=l.id ORDER BY l.id")]
        paid_by_type = paid_to_utility(conn, date_from, date_to)
        paid_total = paid_for(conn, None if type == "all" else type, date_from, date_to)
        gaps = vacancy_usage(conn, date_from, date_to)

    total_water = sum(x["usage"] for x in rows if x["meter_type"] == "water")
    total_elec = sum(x["usage"] for x in rows if x["meter_type"] == "electricity")
    total_amount = round(sum(x["amount"] for x in rows), 2)
    total_paid = round(sum(x["paid"] for x in rows), 2)
    total_outstanding = round(sum(x["outstanding"] for x in rows), 2)
    # 应收与缴出取自同一组筛选条件(租户、计量线、账期区间),毛差才恒等
    margin_value = margin_from(total_amount, paid_total)
    gap_usage = sum(g["usage"] for g in gaps)

    qs = (f"type={type}&household_id={household_id}&line_id={line_id}"
          f"&date_from={date_from}&date_to={date_to}")
    page_rows, page, pages, pager = page_slice(rows, len(rows), page, qs)

    return render("statistics.html", title="统计与查询",
                  lead="按租户、计量线、账期查询用量与费用;并对比向部门缴出与向租户收取。",
                  active="statistics", type=type, household_id=household_id,
                  line_id=line_id, date_from=date_from, date_to=date_to,
                  households=households, lines=lines, rows=rows, page_rows=page_rows,
                  page=page, pages=pages, pager=pager, qs=qs,
                  total_water=total_water, total_elec=total_elec,
                  total_amount=total_amount, total_paid=total_paid,
                  total_outstanding=total_outstanding,
                  paid_total=paid_total,
                  paid_by_type=paid_by_type, margin=margin_value,
                  gaps=gaps, gap_usage=gap_usage)


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, household_id: str = "", type: str = "",
                status: str = "", date_from: str = "", date_to: str = "",
                page: int = 1):
    """台账:全部租户的逐期应收/已收/未收,加收款流水。"""
    with connect() as conn:
        households = [dict(x) for x in conn.execute(
            "SELECT id, name FROM households ORDER BY id")]
        rows = all_ledger_rows(conn)
        entries = [dict(x) for x in conn.execute(
            "SELECT r.*, h.name AS household_name FROM receipts r "
            "JOIN households h ON h.id = r.household_id "
            "ORDER BY r.entry_date DESC, r.id DESC")]

    if household_id:
        hh = _int(household_id)
        rows = [x for x in rows if x["household_id"] == hh]
        entries = [x for x in entries if x["household_id"] == hh]
    if type:
        rows = [x for x in rows if x["meter_type"] == type]
    if date_from:
        rows = [x for x in rows if x["period_end"] >= date_from]
        entries = [x for x in entries if x["entry_date"] >= date_from]
    if date_to:
        rows = [x for x in rows if x["period_end"] <= date_to]
        entries = [x for x in entries if x["entry_date"] <= date_to]
    if status == "unpaid":
        rows = [x for x in rows if x["paid"] <= 0.005]
    elif status == "partial":
        rows = [x for x in rows if x["paid"] > 0.005 and not x["settled"]]
    elif status == "settled":
        rows = [x for x in rows if x["settled"]]
    rows.sort(key=lambda x: (x["period_end"], x["household_id"], x["line_id"]),
              reverse=True)

    sum_due = round(sum(x["amount"] for x in rows), 2)
    sum_paid = round(sum(x["paid"] for x in rows), 2)
    sum_outstanding = round(sum(x["outstanding"] for x in rows), 2)
    net_receipts = round(sum(x["amount"] if x["kind"] == "receipt" else -x["amount"]
                             for x in entries), 2)

    qs = (f"household_id={household_id}&type={type}&status={status}"
          f"&date_from={date_from}&date_to={date_to}")
    page_rows, page, pages, pager = page_slice(rows, len(rows), page, qs)

    return render("ledger.html", title="台账",
                  lead="应收、已收与未收的逐期明细,以及每一笔收款流水。",
                  active="ledger", households=households, rows=rows,
                  page_rows=page_rows, page=page, pages=pages, pager=pager,
                  entries=entries, net_receipts=net_receipts,
                  sum_due=sum_due, sum_paid=sum_paid,
                  sum_outstanding=sum_outstanding,
                  household_id=household_id, type=type, status=status,
                  date_from=date_from, date_to=date_to)


@app.get("/statistics/pdf")
def statistics_pdf(type: str = Query("all"), household_id: str = Query(""),
                   line_id: str = Query(""), date_from: str = Query(""),
                   date_to: str = Query("")):
    from fpdf import FPDF

    with connect() as conn:
        rows = _query_rows(conn, type, household_id, line_id, date_from, date_to)
        settled = settled_periods(conn)
        for x in rows:
            s_ = settled.get((x["line_id"], x["end"]))
            x["paid"] = s_["paid"] if s_ else 0.0
            x["outstanding"] = s_["outstanding"] if s_ else round(x["amount"], 2)

    font_path = os.environ.get("SDF_PDF_FONT", "C:/Windows/Fonts/simhei.ttf")
    has_cjk = Path(font_path).exists()

    pdf = FPDF(orientation="L")
    pdf.add_page()
    if has_cjk:
        pdf.add_font("cjk", fname=font_path)
        pdf.set_font("cjk", size=10)
        title = "水电费统计报表"
        headers = ["租户", "计量线", "账期起", "账期止", "上期读数", "本期读数", "用量",
                   "单价", "应收(元)", "已收(元)", "未收(元)"]
    else:
        pdf.set_font("Helvetica", size=9)
        title = "Utility Bill Report"
        headers = ["Household", "Line", "Start", "End", "Prev", "Curr", "Usage",
                   "Price", "Due", "Paid", "Unpaid"]
    widths = [34, 46, 24, 24, 22, 22, 18, 20, 26, 26, 26]

    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font_size(9)
    for head, w in zip(headers, widths):
        pdf.cell(w, 8, head, border=1, align="C")
    pdf.ln()
    for x in rows:
        name = x.get("display_name") or x.get("meter_name") or ""
        vals = [x["household_name"], name, x.get("start", ""), x["end"],
                f"{x['start_value']:,}", f"{x['end_value']:,}", f"{x['usage']:,}",
                x["unit_price"], f"{x['amount']:,.2f}", f"{x['paid']:,.2f}",
                f"{x['outstanding']:,.2f}"]
        for val, w in zip(vals, widths):
            pdf.cell(w, 8, str(val)[:30], border=1)
        pdf.ln()

    data = bytes(pdf.output())
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=bills.pdf"})
