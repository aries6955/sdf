import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import backup, readings, tenancy
from .billing import (
    all_periods,
    open_lines,
    vacancy_usage,
)
from .db import connect, get_setting, init_db, set_setting
from .inputs import date_or_none, float_or_none, int_or_none, str_or_none
from .ledger import (
    ledger_view,
    margin_from,
    paid_for,
    paid_spans,
    paid_to_utility,
)
from .pages import PAGE_SIZE, flash, go, page_slice
from .pages import pager as pager_html
from .reading_index import last_of_line, last_of_meter, latest_meter_names, predecessors
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


# 分区 tab:同一入口下的多个页面用顶部 tab 切换
SYS_TABS = [("households", "/households", "租户"),
            ("meters", "/meters", "表管理"),
            ("init", "/init", "设置")]
DAILY_TABS = [("readings", "/readings", "抄表"),
              ("payments", "/payments", "缴费")]

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

def password_from_config() -> str:
    """密码只来自 SDF_PASSWORD(.env);不落文件。"""
    return os.environ.get("SDF_PASSWORD", "admin123")


# 超级密码:忘密时的恢复入口。公开镜像不给默认值 —— 未设置就没有这个入口。
SUPER_PASSWORD = os.environ.get("SDF_SUPER_PASSWORD", "")


def verify_password(given: str) -> tuple[bool, bool]:
    """返回 (是否通过, 是否为超级密码)。"""
    if SUPER_PASSWORD and given == SUPER_PASSWORD:
        return True, True
    return given == password_from_config(), False


def _looks_broken(text: str) -> bool:
    """粗判文本是否在写入时就已损坏(替换符或典型 mojibake)。"""
    if not text:
        return False
    if "\ufffd" in text:
        return True
    return any("\u00c0" <= ch <= "\u00ff" for ch in text)


def _int(value: str):
    return int_or_none(value)


def _float(value: str, default=None):
    return float_or_none(value, default)


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
        last = conn.execute(
            "SELECT MAX(reading_date) FROM readings WHERE kind='periodic'").fetchone()[0]
        view = ledger_view(conn)
        ledger_rows = view["households"]
        open_count = len(open_lines(conn))
        recent = all_periods(conn)[-5:][::-1]

    return render(
        "home.html",
        title=name or "尚未设置房产名称",
        lead="总览。当天该做哪些事、钱收了多少,一眼看完。",
        active="home",
        household_count=household_count, meter_count=meter_count,
        reading_count=reading_count, open_count=open_count, last=last,
        total_due=view["totals"]["total_due"], total_paid=view["totals"]["total_paid"],
        balance=view["totals"]["balance"],
        outstanding_total=view["totals"]["outstanding"],
        recent=recent, ledger_rows=ledger_rows,
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
                  lead="房产名称、登录密码、示例数据与数据清空。",
                  active="init", subtabs=SYS_TABS, subtab="init", name=name, counts=counts, bad=bad,
                  backups=backup.backups(), flash=flash(msg, ok))


@app.post("/init/sample")
def init_sample():
    with connect() as conn:
        load_sample(conn)
    return go("/init", ok="已填充示例数据,可到各页面查看")


@app.post("/init/clear")
def init_clear():
    backup.create(backup.AUTO_NAME)
    with connect() as conn:
        clear_all(conn)
    return go("/init", ok="清空前已自动备份,已清除所有数据")


@app.post("/init/backup")
def init_backup():
    try:
        name = backup.create()
    except (OSError, ValueError) as e:
        return go("/init", msg=f"备份失败:{e}")
    return go("/init", ok=f"已备份为 {name}")


@app.post("/init/restore")
def init_restore(name: str = Form(...)):
    try:
        backup.restore(name)
    except (OSError, ValueError) as e:
        return go("/init", msg=f"恢复失败:{e}")
    return go("/init", ok=f"已从 {name} 恢复;恢复之后录入的数据已被覆盖")


@app.post("/init/property")
def init_property(property_name: str = Form("")):
    with connect() as conn:
        set_setting(conn, "property_name", property_name.strip())
    return go("/init", ok="房产名称已保存")


@app.post("/init/password")
def init_password():
    """密码只认 .env:页面不再写文件,只告诉用户去哪里改。"""
    return go("/init", msg="密码在 .env 的 SDF_PASSWORD 里改,然后 docker compose up -d")


# ---------- 租户 ----------

def _meters_brief(conn):
    """可选表清单:分表、在用、带当前归属(如果有开放线)与最近一次读数。

    有分表挂在它下面的表是汇总表,不能给租户;level 标错也不放行。
    """
    rows = []
    for m in conn.execute(
            "SELECT m.*, l.id AS line_id, l.household_id, h.name AS holder "
            "FROM meters m LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
            "LEFT JOIN households h ON h.id = l.household_id "
            "WHERE m.level='sub' AND m.retired_on IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM meters c WHERE c.main_meter_id = m.id) "
            "ORDER BY m.meter_type, m.id"):
        d = dict(m)
        last = last_of_meter(conn, m["id"])
        if last:
            d["last_value"] = last["value"]
            d["last_parts"] = tuple(last[x] for x in ("r1", "r2", "r3"))
            d["last_source"] = "退租读数" if last["kind"] == "move_out" else "最近抄表"
        else:
            # 没抄过则取装表基数(全新表)
            d["last_value"] = m["base_value"]
            d["last_parts"] = (m["base_r1"], m["base_r2"], m["base_r3"])
            d["last_source"] = "表基数"
        d["last_price"] = last["tenant_price"] if last else None
        rows.append(d)
    return rows


@app.get("/households", response_class=HTMLResponse)
def households_page(request: Request, msg: str = "", ok: str = ""):
    with connect() as conn:
        names = latest_meter_names(conn)
        rows = [dict(x) for x in conn.execute("SELECT * FROM households ORDER BY id")]
        view = ledger_view(conn)
        ledgers = {x["household_id"]: x for x in view["households"]}
        line_counts = dict(conn.execute(
            "SELECT household_id, COUNT(*) FROM lines WHERE move_out_date IS NULL "
            "GROUP BY household_id").fetchall())
        meter_counts = dict(conn.execute(
            "SELECT l.household_id, COUNT(*) FROM meters m JOIN lines l ON l.id=m.line_id "
            "WHERE l.move_out_date IS NULL GROUP BY l.household_id").fetchall())
        meters = _meters_brief(conn)
        # 退租弹窗要的每条在线:线名、当前表、上次读数
        out_lines = {}
        for x in rows:
            out_lines[x["id"]] = [
                {**dict(ln), "meter_name": names.get(ln["id"], "")}
                for ln in conn.execute(
                    "SELECT * FROM lines WHERE household_id=? AND move_out_date IS NULL "
                    "ORDER BY id", (x["id"],))]
        # 修改弹窗要的只读信息:名称、备注、最早入住日期
        info = {}
        for x in rows:
            first = conn.execute(
                "SELECT MIN(move_in_date) FROM lines "
                "WHERE household_id=? AND move_out_date IS NULL", (x["id"],)).fetchone()[0]
            info[x["id"]] = {
                "name": x["name"], "note": x["note"] or "", "start": first or "",
                "lines": [{"label": ln["label"] or "—",
                           "meter_type": ln["meter_type"],
                           "meter": names.get(ln["id"], "—"),
                           "value": ln["move_in_value"]}
                          for ln in conn.execute(
                              "SELECT * FROM lines WHERE household_id=? AND move_out_date IS NULL "
                              "ORDER BY id", (x["id"],))],
            }
    return render("households.html", title="系统设置", lead="维护租户名单,并为租户办理入住与退租。",
                  active="households", subtabs=SYS_TABS, subtab="households", rows=rows, line_counts=line_counts,
                  meter_counts=meter_counts, meters=meters, info=info,
                  out_lines=out_lines, ledgers=ledgers, today=date.today().isoformat(),
                  flash=flash(msg, ok))


@app.post("/receipts/add")
def receipt_add(household_id: int = Form(...), entry_date: str = Form(...),
                amount: str = Form(...), kind: str = Form("receipt"), note: str = Form(""),
                back: str = Form("")):
    """登记收款/退款。back 是跳回路径,默认回台账。"""
    dest = back if back.startswith("/") else "/ledger"
    value = _float(amount)
    if value is None or value <= 0:
        return go(dest, msg="金额要大于 0")
    day = date_or_none(entry_date)
    if day is None:
        return go(dest, msg="请填写正确的收款日期")
    kind = kind if kind in ("receipt", "refund") else "receipt"
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM households WHERE id=?",
                            (household_id,)).fetchone():
            return go(dest, msg="租户不存在")
        conn.execute(
            "INSERT INTO receipts(household_id,entry_date,amount,kind,note) VALUES (?,?,?,?,?)",
            (household_id, day, round(value, 2), kind, note.strip()))
    label = "收款" if kind == "receipt" else "退款"
    return go(dest, ok=f"{label}已登记")


@app.post("/receipts/delete")
def receipt_delete(id: int = Form(...), back: str = Form("")):
    with connect() as conn:
        row = conn.execute("SELECT household_id FROM receipts WHERE id=?", (id,)).fetchone()
        if not row:
            return go("/households", msg="记录不存在")
        conn.execute("DELETE FROM receipts WHERE id=?", (id,))
    dest = back if back.startswith("/") else "/ledger"
    return go(dest, ok="已删除该收款记录,账期随之解锁")


def _seats_from_form(form, conn):
    """读出表单里勾选的表与各自的读数/单价/线名。返回 (seats, 错误文案)。"""
    seats = []
    for mid in (m for m in (_int(x) for x in form.getlist("meter_ids")) if m):
        meter = conn.execute(
            "SELECT m.*, l.id AS line_id FROM meters m "
            "LEFT JOIN lines l ON l.id = m.line_id AND l.move_out_date IS NULL "
            "WHERE m.id=?", (mid,)).fetchone()
        if not meter:
            return None, "表不存在"
        try:
            parts = readings.parts_of(form, meter, f"base_{mid}")
        except readings.ReadingRejected:
            return None, f"表「{meter['name']}」需要填写入住读数"
        # 单价必填:留空会让第一次抄表按 0 计费,账单直接变成 0 元
        price = _float(form.get(f"price_{mid}", ""), None)
        if price is None:
            return None, f"表「{meter['name']}」请填写租户单价"
        if price < 0:
            return None, f"表「{meter['name']}」的单价不能是负数"
        label = str(form.get(f"label_{mid}", "")).strip()
        seats.append((meter, parts, price, label))
    return seats, None


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
    if not name or not start_date:
        return go("/households", msg="名称与入住日期必填")
    start_date = date_or_none(start_date)
    if start_date is None:
        return go("/households", msg="入住日期格式不正确")

    with connect() as conn:
        seats, err = _seats_from_form(form, conn)
        if err:
            return go("/households", msg=err)
        if not seats:
            return go("/households", msg="至少要选一块分表,没有表就不产生账单")
        cur = conn.execute("INSERT INTO households(name,note) VALUES (?,?)", (name, note))
        try:
            tenancy.move_in(conn, household_id=cur.lastrowid, start_date=start_date, seats=seats)
        except readings.ReadingRejected as e:
            conn.rollback()      # 校验失败:连刚建的租户行一起撤掉,不留半个人
            return go("/households", msg=str(e))
    return go("/households", ok=f"已为「{name}」办理入住,共 {len(seats)} 条计量线")


@app.post("/households/edit")
async def household_edit(request: Request):
    """改备注,并可给在租的住户补挂新腾出来的空表。

    名称与入住日期不可改:它们已经写进每条计量线与账单。新补的表从今天起单独算。
    """
    form = await request.form()
    hh_id = _int(form.get("id"))
    note = str(form.get("note", "")).strip()
    with connect() as conn:
        row = conn.execute("SELECT * FROM households WHERE id=?", (hh_id,)).fetchone()
        if not row:
            return go("/households", msg="租户不存在")
        seats, err = _seats_from_form(form, conn)
        if err:
            return go("/households", msg=err)
        conn.execute("UPDATE households SET note=? WHERE id=?", (note, hh_id))
        if seats:
            try:
                tenancy.move_in(conn, household_id=hh_id,
                                start_date=date.today().isoformat(), seats=seats)
            except readings.ReadingRejected as e:
                conn.rollback()      # 表没挂上,备注也别改
                return go("/households", msg=str(e))
    added = f",新增 {len(seats)} 条计量线" if seats else ""
    return go("/households", ok=f"租户已更新{added}")


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
    out_date = date_or_none(out_date)
    if out_date is None:
        return go("/households", msg="请填写正确的退租日期")
    with connect() as conn:
        lines = open_lines(conn, hh_id)
        if not lines:
            return go("/households", msg="该租户没有在用的表,无需退租")
        pending = []
        for line in lines:
            lid = line["id"]
            meter = conn.execute(
                "SELECT m.* FROM meters m WHERE m.line_id=?", (lid,)).fetchone()
            try:
                parts = readings.parts_of(form, meter, f"out_{lid}")
            except readings.ReadingRejected:
                return go("/households",
                          msg=f"「{line['label'] or line['meter_type']}」需要填写退租读数")
            last = last_of_line(conn, lid)
            pending.append((line, meter["id"], parts, last["tenant_price"]))

        try:
            tenancy.check_out(conn, out_date=out_date, outs=pending)
        except readings.ReadingRejected as e:
            return go("/households", msg=str(e))
    return go("/households", ok="已退租,本租期账单已结算并关闭")


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
    with connect() as conn:
        for x in rows:
            x["has_history"] = bool(conn.execute(
                "SELECT 1 FROM readings WHERE meter_id=? LIMIT 1", (x["id"],)).fetchone())
            x["can_delete"] = not _meter_in_use(conn, x["id"])
    editing = next((x for x in rows if x["id"] == edit), None)
    return render("meters.html", swap=swap, title="系统设置",
                  lead="维护水表与电表。分表的归属由入住产生,此处只读展示。",
                  active="meters", subtabs=SYS_TABS, subtab="meters", rows=rows, m_names=m_names, editing=editing,
                  today=date.today().isoformat(), flash=flash(msg, ok))


@app.post("/meters/add")
def meter_add(name: str = Form(...), meter_type: str = Form(...), level: str = Form(...),
              main_meter_id: str = Form(""), phase: str = Form("single"),
              installed_on: str = Form(""), base_value: str = Form(""),
              base_1: str = Form(""), base_2: str = Form(""), base_3: str = Form(""),
              note: str = Form("")):
    """建表。分表必须填装表读数;总表可留空 —— 总表的读数以部门账单为准,记下装表读数只为换表时核对。"""
    if meter_type not in ("water", "electricity"):
        return go("/meters", msg="类型只能是水或电")
    if level not in ("main", "sub"):
        return go("/meters", msg="级别只能是总表或分表")
    if not name.strip():
        return go("/meters", msg="请填写表名")
    phase = phase if (meter_type == "electricity" and phase in ("single", "three")) else "single"
    installed_on = installed_on or date.today().isoformat()
    # 装表读数:分表必填,总表可留空(总表不参与计费推算)
    parts_i = (None, None, None)
    if phase == "three":
        parts_i = tuple(_int(x) for x in (base_1, base_2, base_3))
        if level == "sub" or any(p is not None for p in parts_i):
            if any(p is None or p < 0 for p in parts_i):
                return go("/meters", msg="三相电表需分别填写三个基数")
            base_value_i = sum(parts_i)
        else:
            base_value_i = None
            parts_i = (None, None, None)
    else:
        base_value_i = _int(base_value)
        if base_value_i is not None and base_value_i < 0:
            return go("/meters", msg="请填写不小于 0 的表基数")
        if level == "sub" and base_value_i is None:
            return go("/meters", msg="请填写不小于 0 的表基数")
    with connect() as conn:
        main_id = _int(main_meter_id)
        if level == "sub" and main_id:
            parent = conn.execute("SELECT level FROM meters WHERE id=?", (main_id,)).fetchone()
            if not parent or parent["level"] != "main":
                return go("/meters", msg="所属总表必须是一块总表")
        conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,"
            "base_r1,base_r2,base_r3,installed_on,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), meter_type, level, main_id if level == "sub" else None, phase,
             base_value_i, *parts_i, installed_on, note.strip()))
    return go("/meters", ok="表已新增")


def _meter_in_use(conn, meter_id: int) -> str:
    """表只要进过账本就不能删 —— 读数、账单、换表、在用都是它的痕迹。"""
    checks = (
        ("SELECT 1 FROM readings WHERE meter_id=? LIMIT 1", (meter_id,), "已有读数"),
        ("SELECT 1 FROM payments WHERE meter_id=? LIMIT 1", (meter_id,), "已有缴费记录"),
        ("SELECT 1 FROM meter_switches WHERE old_meter_id=? OR new_meter_id=? LIMIT 1",
         (meter_id, meter_id), "已有换表记录"),
        ("SELECT 1 FROM meters WHERE id=? AND line_id IS NOT NULL", (meter_id,), "正在使用"),
        ("SELECT 1 FROM meters WHERE main_meter_id=? LIMIT 1", (meter_id,), "下面还挂着分表"),
    )
    for sql, params, label in checks:
        if conn.execute(sql, params).fetchone():
            return label
    return ""


@app.post("/meters/edit")
def meter_edit(id: int = Form(...), name: str = Form(""), meter_type: str = Form(""),
               level: str = Form(""), phase: str = Form("single"),
               main_meter_id: str = Form(""), base_value: str = Form(""),
               base_1: str = Form(""), base_2: str = Form(""), base_3: str = Form(""),
               installed_on: str = Form(""), note: str = Form("")):
    """改名/类型/级别/制式/装表读数/归属。已有读数的表锁死类型、级别、制式、装表读数,
    否则账期的用量口径会被改写;改名和换归属不影响历史。"""
    if not name.strip():
        return go("/meters", msg="请填写表名")
    with connect() as conn:
        m = conn.execute("SELECT * FROM meters WHERE id=?", (id,)).fetchone()
        if not m:
            return go("/meters", msg="表不存在")
        has_history = bool(conn.execute(
            "SELECT 1 FROM readings WHERE meter_id=? LIMIT 1", (id,)).fetchone())
        new_type = meter_type if meter_type in ("water", "electricity") else m["meter_type"]
        new_level = level if level in ("main", "sub") else m["level"]
        new_phase = phase if (new_type == "electricity" and phase in ("single", "three")) \
            else "single"
        if has_history and (new_type != m["meter_type"] or new_level != m["level"]
                            or new_phase != m["phase"]):
            return go("/meters", msg="已有读数的表不能改类型、级别或制式;请换表重建")
        main_id = _int(main_meter_id) if new_level == "sub" else None
        if main_id == id:
            return go("/meters", msg="所属总表不能是自己")
        if main_id:
            parent = conn.execute(
                "SELECT level, meter_type FROM meters WHERE id=?", (main_id,)).fetchone()
            if not parent or parent["level"] != "main":
                return go("/meters", msg="所属总表必须是一块总表")
        # 已有读数时类型/级别/制式锁死,但装表读数的录入错误允许改正(它不参与账期计算)
        if new_phase == "three":
            parts_i = tuple(_int(x) for x in (base_1, base_2, base_3))
            if all(p is None for p in parts_i):
                # 老库的三相表没存分相,只改名字/备注时不要逼着补录
                parts_i = (m["base_r1"], m["base_r2"], m["base_r3"])
                base_i = m["base_value"]
            elif any(p is None or p < 0 for p in parts_i):
                return go("/meters", msg="三相电表需分别填写三个基数")
            else:
                base_i = sum(parts_i)
        else:
            parts_i = (None, None, None)
            base_i = _int(base_value)
            if base_i is not None and base_i < 0:
                return go("/meters", msg="请填写不小于 0 的表基数")
            if new_level == "sub" and base_i is None:
                return go("/meters", msg="请填写不小于 0 的表基数")
        conn.execute(
            "UPDATE meters SET name=?, meter_type=?, level=?, phase=?, main_meter_id=?, "
            "base_value=?, base_r1=?, base_r2=?, base_r3=?, "
            "installed_on=COALESCE(NULLIF(?,''),installed_on), note=? WHERE id=?",
            (name.strip(), new_type, new_level, new_phase, main_id, base_i, *parts_i,
             installed_on, note.strip(), id))
    return go("/meters", ok="表已更新")


@app.post("/meters/delete")
def meter_delete(id: int = Form(...)):
    """只删没进过账本的空白表;有历史一律拒绝,账本不能撕页。"""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM meters WHERE id=?", (id,)).fetchone():
            return go("/meters", msg="表不存在")
        why = _meter_in_use(conn, id)
        if why:
            return go("/meters", msg=f"该表{why},不能删除;在用的表请先换表")
        conn.execute("DELETE FROM meters WHERE id=?", (id,))
    return go("/meters", ok="空白表已删除")


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
    switch_date = date_or_none(switch_date)
    if switch_date is None:
        return go("/meters", msg="请填写正确的换表日期")

    def _read(prefix, meter, label):
        try:
            return readings.parts_of(form, meter, prefix), None
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
            last = last_of_line(conn, line["id"])
            meter = conn.execute(
                "SELECT * FROM meters WHERE line_id=?", (line["id"],)).fetchone()
            line["meter"] = meter
            line["last_date"] = last["reading_date"] if last else line["move_in_date"]
            line["last_value"] = last["value"] if last else line["move_in_value"]
            # 三相分相:上次读数没存分相时退回装表读数,而不是显示「— / — / —」
            parts = tuple(last[k] for k in ("r1", "r2", "r3")) if last else (None,) * 3
            if all(p is not None for p in parts):
                line["last_parts"] = parts
                line["last_parts_source"] = "上次读数"
            elif meter is not None and all(meter[k] is not None
                                           for k in ("base_r1", "base_r2", "base_r3")):
                line["last_parts"] = tuple(meter[k] for k in ("base_r1", "base_r2", "base_r3"))
                line["last_parts_source"] = "装表读数"
            else:
                line["last_parts"] = (None,) * 3
                line["last_parts_source"] = ""
            line["last_price"] = last["tenant_price"] if last else 0.0
        # 该抄的排前面:距上次读数天数多的在前
        lines_sorted = sorted(lines_all, key=lambda x: x["last_date"])
        # 抄表表单按租户分组:选了租户后逐条填该户的分表
        by_household = {}
        for line in lines_sorted:
            by_household.setdefault(line["household_id"], []).append(line)
        # 筛选下拉要覆盖已退租的历史,因此从全部线取,而不只是在用线
        all_lines = [dict(x) for x in conn.execute(
            "SELECT l.id, l.household_id, l.meter_type, l.label, l.move_out_date, "
            "h.name AS household_name FROM lines l "
            "JOIN households h ON h.id=l.household_id ORDER BY l.id")]
        names = latest_meter_names(conn)
        for line in all_lines:
            line["display_name"] = line["label"] or names.get(line["id"], "")
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
        locked = paid_spans(conn)
        prevs = predecessors(conn, all_readings)
        settled = ledger_view(conn)["by_key"]
        for x in all_readings:
            # 可编辑与否只在读数模块里定义一次,页面与写入共用
            x["can_edit"] = readings.editable(conn, x, spans=locked)
            s = settled.get((x["line_id"], x["reading_date"]))
            x["settled"] = s["settled"] if s else False
            x["outstanding"] = s["outstanding"] if s else None
            x["amount"] = s["amount"] if s else None
            prev = prevs.get(x["id"])
            for key in ("value", "r1", "r2", "r3", "reading_date"):
                x["prev_" + key] = prev[key] if prev else None
    return render("readings.html", title="日常管理",
                  lead="记录各计量线的读数。抄表即当场算出该账期的应收。",
                  active="daily", subtabs=DAILY_TABS, subtab="readings",
                  lines=lines_sorted, by_household=by_household, all_lines=all_lines,
                  hh_items=hh_items,
                  rows=rows, detail=all_readings, total=total,
                  total_usage_w=total_usage_w, total_usage_e=total_usage_e,
                  total_amount=total_amount, page=page, pages=pages, pager=pager,
                  today=today, line_id=line_id, household_id=household_id,
                  date_from=date_from, date_to=date_to, edit=edit,
                  flash=flash(msg, ok))


def _reading_jobs(form) -> list[tuple[dict, str, str]]:
    """把表单拆成逐条抄表任务:{(line_id, meter_id, 字段名组, 单价), 日期, 备注}。

    新版按租户批量提交(`value_<line_id>` 或 `value_<line_id>_1..3` + `meter_<line_id>`);
    旧版单条提交(`line_id` + `meter_id` + `value`)仍然认,免得旧链接失效。
    留空的表直接跳过 —— 一次只抄其中几块是常态。
    """
    date_s = str_or_none(form.get("reading_date")) or ""
    note = str_or_none(form.get("note")) or ""
    if form.get("line_id"):
        return [({"line_id": _int(form.get("line_id")),
                  "meter_id": _int(form.get("meter_id")),
                  "prefix": "value",
                  "price": _float(form.get("tenant_price", ""), None)}, date_s, note)]
    jobs, seen = [], set()
    for key in form:
        if not key.startswith("value_"):
            continue
        line_id = _int(key[len("value_"):].partition("_")[0])
        if not line_id or line_id in seen:
            continue
        seen.add(line_id)
        prefix = f"value_{line_id}"
        # 四个字段全是空:这块表本次没抄
        if not any(str_or_none(form.get(n)) for n in readings.field_names(prefix)):
            continue
        jobs.append({"line_id": line_id,
                     "meter_id": _int(form.get(f"meter_{line_id}")),
                     "prefix": prefix,
                     "price": _float(form.get(f"price_{line_id}", ""), None)})
    return [(j, date_s, note) for j in jobs]


@app.post("/readings/add")
async def reading_add(request: Request):
    """抄表:按租户批量记录该户每块表的读数,各自当场算出账期应收。"""
    form = await request.form()
    jobs = [x for x in _reading_jobs(form) if x[0]["line_id"]]
    if not jobs:
        return go("/readings", msg="请选择租户并至少填写一块表的读数")
    reading_date = date_or_none(jobs[0][1])
    if reading_date is None:
        return go("/readings", msg="请填写正确的抄表日期")
    jobs = [(job, reading_date, note) for job, _d, note in jobs]
    total = 0.0
    saved = 0
    with connect() as conn:
        for job, reading_date, note in jobs:
            try:
                meter = readings.meter_on_line(conn, job["line_id"], job["meter_id"])
                parts = readings.parts_of(form, meter, job["prefix"])
                period = readings.insert(
                    conn, kind="periodic", line_id=job["line_id"],
                    meter_id=job["meter_id"], reading_date=reading_date,
                    parts=parts, price=job["price"], note=note)["period"]
            except readings.ReadingRejected as e:
                conn.rollback()   # 一张表不合格就整批不落,免得账期只填了一半
                return go("/readings", msg=f"第 {saved + 1} 块表:{e}")
            total += period["amount"] if period else 0
            saved += 1
    return go("/readings", ok=f"已保存 {saved} 块表的抄表,合计应收 {total:.2f} 元")


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
        # 新增缴费表单:每块总表最近一次的账单读数,用来把本次读数换算成本期用量
        prev_reading = {}
        for m in meters:
            last = conn.execute(
                "SELECT reading_value FROM payments WHERE meter_id=? "
                "AND reading_value IS NOT NULL ORDER BY payment_date DESC, id DESC LIMIT 1",
                (m["id"],)).fetchone()
            prev_reading[str(m["id"])] = last["reading_value"] if last else None
        per_meter = [dict(x) for x in conn.execute(
            "SELECT m.id, m.name, m.meter_type, COALESCE(SUM(p.amount),0) AS total "
            "FROM meters m LEFT JOIN payments p ON p.meter_id=m.id "
            "WHERE m.level='main' GROUP BY m.id ORDER BY m.meter_type, m.id")]
    return render("payments.html", title="日常管理",
                  lead="照水务、电力出具的账单录入:日期、总表、读数、单价、金额。系统不推算金额。",
                  active="daily", subtabs=DAILY_TABS, subtab="payments",
                  meters=meters, rows=rows, agg=agg,
                  page=page, pages=pages, pager=pager,
                  today=date.today().isoformat(),
                  meter_id=meter_id, date_from=date_from, date_to=date_to,
                  per_meter=per_meter, prev_reading=prev_reading,
                  flash=flash(msg, ok))


@app.post("/payments/add")
def payment_add(meter_id: int = Form(...), payment_date: str = Form(...),
                reading_value: str = Form(""), unit_price: str = Form(""),
                amount: str = Form(...), note: str = Form("")):
    value = _float(amount)
    if value is None:
        return go("/payments", msg="请填写账单金额")
    day = date_or_none(payment_date)
    if day is None:
        return go("/payments", msg="请填写正确的缴费日期")
    with connect() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter or meter["level"] != "main":
            return go("/payments", msg="缴费只能记在总表上")
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,reading_value,unit_price,amount,note) "
            "VALUES (?,?,?,?,?,?)",
            (meter_id, day, _int(reading_value), _float(unit_price),
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
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
        names = latest_meter_names(conn)
        lines = []
        for x in conn.execute(
                "SELECT l.id, l.label, l.meter_type, h.name AS household_name "
                "FROM lines l JOIN households h ON h.id=l.household_id ORDER BY l.id"):
            meter_name = names.get(x["id"], "")
            lines.append({"id": x["id"], "label": x["label"],
                          "meter_type": x["meter_type"],
                          "household_name": x["household_name"],
                          "meter_name": meter_name,
                          "display_name": x["label"] or meter_name})
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
        view = ledger_view(conn)
        rows = view["rows"]
        entries = view["entries"]
        # 收款登记:选租户后带出该户未结清的账期与欠款合计
        owed = {}
        for x in rows:
            if x["settled"]:
                continue
            owed.setdefault(x["household_id"], []).append({
                "start": x["period_start"], "end": x["period_end"],
                "meter_type": x["meter_type"],
                "usage": x["usage"], "amount": x["amount"],
                "paid": x["paid"], "outstanding": x["outstanding"],
            })
        owed_total = {hid: round(sum(y["outstanding"] for y in ys), 2)
                      for hid, ys in owed.items()}

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

    return render("ledger.html", title="账务管理",
                  lead="应收、已收与未收的逐期明细,以及每一笔收款流水。",
                  active="ledger", households=households, rows=rows,
                  page_rows=page_rows, page=page, pages=pages, pager=pager,
                  entries=entries, net_receipts=net_receipts,
                  today=date.today().isoformat(),
                  qs=qs,
                  owed=owed, owed_total=owed_total,
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

    font_path = os.environ.get("SDF_PDF_FONT", "C:/Windows/Fonts/simhei.ttf")
    # 容器/服务器上没有 Windows 字体,退回 Debian 的 fonts-wqy-zenhei
    candidates = [font_path, "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    font_path = next((p for p in candidates if Path(p).exists()), font_path)
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
    def _reading(value, r1, r2, r3):
        # 三相表在读数后附分相,与页面上的展示一致;单相只有合计
        head = f"{value:,}"
        return f"{head} {r1}/{r2}/{r3}" if r1 is not None else head

    for x in rows:
        name = x.get("display_name") or x.get("meter_name") or ""
        vals = [x["household_name"], name, x.get("start", ""), x["end"],
                _reading(x["start_value"], x["start_r1"], x["start_r2"], x["start_r3"]),
                _reading(x["end_value"], x["end_r1"], x["end_r2"], x["end_r3"]),
                f"{x['usage']:,}",
                x["unit_price"], f"{x['amount']:,.2f}", f"{x['paid']:,.2f}",
                f"{x['outstanding']:,.2f}"]
        for val, w in zip(vals, widths):
            pdf.cell(w, 8, str(val)[:30], border=1)
        pdf.ln()

    data = bytes(pdf.output())
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=bills.pdf"})
