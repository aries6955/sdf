import html
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .billing import all_statistics
from .ledger import household_ledger, all_outstanding, receipt_total
from .views import render
from .db import connect, get_setting, init_db, set_setting
from .sample import clear_all, load_sample

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

# 左侧栏分组:日常记账在前,查询次之,系统维护在后
# ---------- 基础设施 ----------

def h(text) -> str:
    return html.escape(str(text))


def add_month(d: date) -> date:
    """同日加一个月,月末溢出则截断(如 1/31 -> 2/28)。"""
    month = d.month + 1
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return d.replace(year=year, month=month, day=day)


def money(value) -> str:
    return f"{(value or 0):,.2f}"


def is_logged_in(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) in _sessions


def require_login(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


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


def _looks_broken(text: str) -> bool:
    """粗判文本是否在写入时就已损坏(替换符或典型 mojibake)。"""
    if not text:
        return False
    if "\ufffd" in text:
        return True
    return any("\u00c0" <= ch <= "\u00ff" for ch in text)


# ---------- 登录 ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(msg: str = ""):
    return render("login.html", msg=msg)


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
    if r := require_login(request):
        return r
    with connect() as conn:
        name = get_setting(conn, "property_name", "")
        household_count = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        meter_count = conn.execute("SELECT COUNT(*) FROM meters WHERE level='sub'").fetchone()[0]
        reading_count = conn.execute("SELECT COUNT(*) FROM readings WHERE is_initial=0").fetchone()[0]
        unbilled = conn.execute(
            "SELECT COUNT(*) FROM readings WHERE is_initial=0 AND billed=0"
        ).fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]
        last = conn.execute(
            "SELECT MAX(reading_date) FROM readings WHERE is_initial=0"
        ).fetchone()[0]
        outstanding_total = round(sum(x["outstanding"] for x in all_outstanding(conn)), 2)
        rows = all_statistics(conn, "water") + all_statistics(conn, "electricity")
        billed_sum = round(sum(x["amount"] for x in rows if x["billed"]), 2)
        unbilled_sum = round(sum(x["amount"] for x in rows if not x["billed"]), 2)
        adj_sum = round(conn.execute("SELECT COALESCE(SUM(amount),0) FROM adjustments").fetchone()[0], 2)

    return render(
        "home.html",
        title=name or "尚未设置房产名称",
        lead="总览。左侧按日常记账、查询、系统分组。",
        active="home",
        household_count=household_count, meter_count=meter_count,
        reading_count=reading_count, unbilled=unbilled, paid=paid, last=last,
        billed_sum=billed_sum, unbilled_sum=unbilled_sum, adj_sum=adj_sum,
        setup_done=household_count > 0 and meter_count > 0,
    )


@app.get("/init", response_class=HTMLResponse)
def init_page(request: Request, msg: str = "", ok: str = ""):
    if r := require_login(request):
        return r
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
    return RedirectResponse("/init?ok=" + quote("已填充示例数据,可到各页面查看"), status_code=303)


@app.post("/init/clear")
def init_clear():
    with connect() as conn:
        clear_all(conn)
    return RedirectResponse("/init?ok=" + quote("已清除所有数据"), status_code=303)


@app.post("/init/property")
def init_property(property_name: str = Form("")):
    with connect() as conn:
        set_setting(conn, "property_name", property_name.strip())
    return RedirectResponse("/init?ok=" + quote("房产名称已保存"), status_code=303)


@app.post("/init/password")
def init_password(old_password: str = Form(...), new_password: str = Form(...),
                  confirm_password: str = Form(...)):
    ok, _ = verify_password(old_password)
    if not ok:
        return RedirectResponse("/init?msg=" + quote("当前密码不正确"), status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/init?msg=" + quote("两次输入的新密码不一致"), status_code=303)
    if len(new_password) < 4:
        return RedirectResponse("/init?msg=" + quote("新密码至少 4 位"), status_code=303)
    if new_password == SUPER_PASSWORD:
        return RedirectResponse("/init?msg=" + quote("新密码不能与超级密码相同"), status_code=303)
    save_password(new_password)
    return RedirectResponse("/init?ok=" + quote("密码已修改,下次登录请使用新密码"), status_code=303)


# ---------- 租户 ----------

@app.get("/households", response_class=HTMLResponse)
def households_page(request: Request, edit: int = Query(0), msg: str = "", ok: str = ""):
    if r := require_login(request):
        return r
    with connect() as conn:
        rows = [dict(x) for x in conn.execute("SELECT * FROM households ORDER BY id")]
        meter_counts = {
            r["household_id"]: r["c"] for r in conn.execute(
                "SELECT household_id, COUNT(*) c FROM meters WHERE household_id IS NOT NULL "
                "AND level='sub' GROUP BY household_id"
            )
        }
        # 可绑定表:显示现持有人与最近读数,便于交接
        ledgers = {}
        for h in rows:
            led = household_ledger(conn, h["id"])
            ledgers[h["id"]] = {"due": led["total_due"], "received": led["total_received"],
                                "outstanding": led["outstanding"], "items": len(led["rows"])}
        meters = []
        for m in conn.execute(
            "SELECT m.*, h.name AS holder FROM meters m "
            "LEFT JOIN households h ON m.household_id = h.id "
            "WHERE m.level = 'sub' ORDER BY m.meter_type, m.level, m.id"):
            d = dict(m)
            last = conn.execute(
                "SELECT value FROM readings WHERE meter_id=? ORDER BY reading_date DESC, id DESC LIMIT 1",
                (m["id"],)).fetchone()
            d["last_value"] = last["value"] if last else None
            meters.append(d)
    editing = next((x for x in rows if x["id"] == edit), None)
    return render("households.html", title="租户", lead="维护被收费的租户名单。",
                  active="households", rows=rows, meter_counts=meter_counts,
                  meters=meters, editing=editing, ledgers=ledgers,
                  today=date.today().isoformat(),
                  flash=flash_from(msg, ok))


@app.get("/households/{household_id}/ledger", response_class=HTMLResponse)
def household_ledger_page(request: Request, household_id: int, msg: str = "", ok: str = ""):
    """某租户的收费台账:应收 / 实收 / 欠款。"""
    if r := require_login(request):
        return r
    with connect() as conn:
        h = conn.execute("SELECT * FROM households WHERE id=?", (household_id,)).fetchone()
        if not h:
            return RedirectResponse("/households?msg=" + quote("租户不存在"), status_code=303)
        ledger = household_ledger(conn, household_id)
        meters = [dict(x) for x in conn.execute(
            "SELECT m.*, t.start_date, t.start_value FROM meters m "
            "LEFT JOIN takeovers t ON t.meter_id = m.id AND t.household_id = ? "
            "WHERE m.household_id = ? AND m.level='sub' ORDER BY m.id",
            (household_id, household_id))]
    return render("household_ledger.html", title=f"{h['name']} · 收费台账",
                  lead="应收、实收与欠款。收款可多次,金额可小于应收。",
                  active="households", household=dict(h), ledger=ledger, meters=meters,
                  today=date.today().isoformat(), flash=flash_from(msg, ok))


@app.post("/receipts/add")
def receipt_add(receivable_id: int = Form(...), receipt_date: str = Form(...),
                amount: float = Form(...), note: str = Form("")):
    with connect() as conn:
        rec = conn.execute("SELECT * FROM receivables WHERE id=?", (receivable_id,)).fetchone()
        if not rec:
            return RedirectResponse("/households?msg=" + quote("应收记录不存在"), status_code=303)
        if amount <= 0:
            return RedirectResponse(
                f"/households/{rec['household_id']}/ledger?msg=" + quote("收款金额要大于 0"),
                status_code=303)
        already = receipt_total(conn, receivable_id)
        if already + amount > round(rec["amount"], 2) + 0.005:
            return RedirectResponse(
                f"/households/{rec['household_id']}/ledger?msg="
                + quote(f"收款超额:应收 {rec['amount']:.2f},已收 {already:.2f}"),
                status_code=303)
        conn.execute(
            "INSERT INTO receipts(receivable_id,receipt_date,amount,note) VALUES (?,?,?,?)",
            (receivable_id, receipt_date, round(amount, 2), note.strip()))
        hh = rec["household_id"]
    return RedirectResponse(f"/households/{hh}/ledger?ok=" + quote("收款已登记"), status_code=303)


@app.post("/households/checkout")
def household_checkout(id: int = Form(...)):
    """退租:必须无欠款才可退租,并解绑表(历史台账保留)。"""
    with connect() as conn:
        led = household_ledger(conn, id)
        if abs(led["outstanding"]) > 0.005:
            return RedirectResponse(
                f"/households/{id}/ledger?msg="
                + quote(f"还有欠款 {led['outstanding']:.2f} 元未结清,不能退租"),
                status_code=303)
        # 解绑表但保留租期与台账历史
        conn.execute("UPDATE meters SET household_id=NULL WHERE household_id=?", (id,))
    return RedirectResponse("/households?ok=" + quote("已退租,表已解绑,历史台账保留"),
                            status_code=303)


@app.post("/households/add")
async def household_add(request: Request):
    """新增租户并绑定表:每块绑定的表录入交接起始读数,作为该租户计费的起点。

    单相表字段 base_<id>;三相表字段 base_<id>_1/2/3,存三相之和并留档分相。
    """
    form = await request.form()
    name = str(form.get("name", "")).strip()
    note = str(form.get("note", "")).strip()
    start_date = str(form.get("start_date", ""))
    meter_ids = form.getlist("meter_ids")
    if not name or not start_date:
        return RedirectResponse("/households?msg=" + quote("名称与入住日期必填"), status_code=303)
    with connect() as conn:
        cur = conn.execute("INSERT INTO households(name,note) VALUES (?,?)", (name, note))
        hh_id = cur.lastrowid
        bound = 0
        for mid_s in meter_ids:
            try:
                mid = int(mid_s)
            except (TypeError, ValueError):
                continue
            meter = conn.execute("SELECT * FROM meters WHERE id=?", (mid,)).fetchone()
            if not meter:
                continue
            # 租户只绑分表;总表只用于部门对账
            if meter["level"] != "sub":
                conn.execute("DELETE FROM households WHERE id=?", (hh_id,))
                return RedirectResponse(
                    "/households?msg=" + quote(f"「{meter['name']}」是总表,不能分配给租户"),
                    status_code=303)
            r1 = r2 = r3 = None
            if meter["phase"] == "three":
                try:
                    r1 = int(form.get(f"base_{mid}_1", ""))
                    r2 = int(form.get(f"base_{mid}_2", ""))
                    r3 = int(form.get(f"base_{mid}_3", ""))
                except (TypeError, ValueError):
                    conn.execute("DELETE FROM households WHERE id=?", (hh_id,))
                    return RedirectResponse(
                        "/households?msg=" + quote(
                            f"三相表「{meter['name']}」需要分别填写三个交接读数"), status_code=303)
                start_val = r1 + r2 + r3
            else:
                raw = form.get(f"base_{mid}", "")
                try:
                    start_val = int(str(raw))
                except (TypeError, ValueError):
                    conn.execute("DELETE FROM households WHERE id=?", (hh_id,))
                    return RedirectResponse(
                        "/households?msg=" + quote(
                            f"表「{meter['name']}」需要填写交接起始读数"), status_code=303)
            last = conn.execute(
                "SELECT value, tenant_price FROM readings WHERE meter_id=? "
                "ORDER BY reading_date DESC, id DESC LIMIT 1", (mid,)).fetchone()
            # 新租户基数不得小于该表上次读数
            if last and start_val < last["value"]:
                conn.execute("DELETE FROM households WHERE id=?", (hh_id,))
                return RedirectResponse(
                    "/households?msg=" + quote(
                        f"「{meter['name']}」的基数 {start_val} 小于该表上次读数 {last['value']},请核对"),
                    status_code=303)
            raw_price = str(form.get(f"price_{mid}", "") or "")
            tp = float(raw_price) if raw_price else (last["tenant_price"] if last else 0.0)
            rc = conn.execute(
                "INSERT INTO readings(meter_id,reading_date,value,tenant_price,r1,r2,r3,note) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (mid, start_date, start_val, tp, r1, r2, r3,
                 f"租户「{name}」入住交接读数"))
            conn.execute("UPDATE meters SET household_id=? WHERE id=?", (hh_id, mid))
            conn.execute(
                "INSERT INTO takeovers(meter_id,household_id,start_date,start_value,reading_id,note) "
                "VALUES (?,?,?,?,?,?)",
                (mid, hh_id, start_date, start_val, rc.lastrowid, f"入住:{name}"))
            bound += 1
    return RedirectResponse("/households?ok=" + quote(f"租户已新增,已绑定 {bound} 块表"), status_code=303)


@app.post("/households/edit")
def household_edit(id: int = Form(...), name: str = Form(""), note: str = Form("")):
    with connect() as conn:
        row = conn.execute("SELECT * FROM households WHERE id=?", (id,)).fetchone()
        if row:
            conn.execute("UPDATE households SET name=?, note=? WHERE id=?",
                         (name.strip() or row["name"], note.strip(), id))
    return RedirectResponse("/households?ok=" + quote("租户已更新"), status_code=303)


@app.post("/households/delete")
def household_delete(id: int = Form(...)):
    with connect() as conn:
        conn.execute("UPDATE meters SET household_id=NULL WHERE household_id=?", (id,))
        conn.execute("DELETE FROM takeovers WHERE household_id=?", (id,))
        conn.execute("DELETE FROM adjustments WHERE household_id=?", (id,))
        conn.execute("DELETE FROM households WHERE id=?", (id,))
    return RedirectResponse("/households?ok=" + quote("租户已删除,其表已解绑"), status_code=303)


# ---------- 表设置 ----------

@app.get("/meters", response_class=HTMLResponse)
def meters_page(request: Request, edit: int = Query(0), msg: str = "", ok: str = ""):
    if r := require_login(request):
        return r
    today = date.today().isoformat()
    with connect() as conn:
        rows = [dict(x) for x in conn.execute("SELECT * FROM meters ORDER BY meter_type, level, id")]
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
    hh_names = {x["id"]: x["name"] for x in households}
    m_names = {x["id"]: x["name"] for x in rows}
    editing = next((x for x in rows if x["id"] == edit), None)
    return render("meters.html", title="表设置",
                  lead="维护水表与电表,以及它们与总表、租户的关系。",
                  active="meters", rows=rows, households=households,
                  hh_names=hh_names, m_names=m_names, editing=editing,
                  today=today, flash=flash_from(msg, ok))


@app.post("/meters/base")
def meter_base(meter_id: int = Form(...), reading_date: str = Form(...), value: str = Form("0"),
               b_1: str = Form(""), b_2: str = Form(""), b_3: str = Form(""),
               tenant_price: str = Form("")):
    with connect() as conn:
        # 基数/交接读数一经设置不可修改(账目起点不能漂移);换表用「换表」功能
        has_base = conn.execute(
            "SELECT 1 FROM readings WHERE meter_id=? AND is_initial=1 LIMIT 1", (meter_id,)
        ).fetchone()
        has_takeover = conn.execute(
            "SELECT 1 FROM takeovers WHERE meter_id=? LIMIT 1", (meter_id,)
        ).fetchone()
        if has_base or has_takeover:
            return RedirectResponse(
                "/meters?msg=" + quote("该表的起始读数已设置且不可修改。表坏了请用「换表」。"),
                status_code=303)
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        r1 = r2 = r3 = None
        if meter and meter["phase"] == "three":
            try:
                r1, r2, r3 = int(b_1), int(b_2), int(b_3)
            except (TypeError, ValueError):
                return RedirectResponse(
                    "/meters?msg=" + quote("三相电表需要分别填写三个起始读数"), status_code=303)
            if min(r1, r2, r3) < 0:
                return RedirectResponse(
                    "/meters?msg=" + quote("三相起始读数不能为负"), status_code=303)
            value_i = r1 + r2 + r3
        else:
            try:
                value_i = int(value)
            except (TypeError, ValueError):
                return RedirectResponse(
                    "/meters?msg=" + quote("起始读数必须是数字"), status_code=303)
        last = conn.execute(
            "SELECT tenant_price FROM readings WHERE meter_id=? "
            "ORDER BY reading_date DESC, id DESC LIMIT 1", (meter_id,)
        ).fetchone()
        tp = float(tenant_price) if tenant_price != "" else (last["tenant_price"] if last else 0.0)
        conn.execute("DELETE FROM readings WHERE meter_id=? AND is_initial=1", (meter_id,))
        rc = conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,tenant_price,r1,r2,r3,is_initial) "
            "VALUES (?,?,?,?,?,?,?,1)", (meter_id, reading_date, value_i, tp, r1, r2, r3),
        )
        # 同步租期起点:该表当前绑定的租户以新基数开始
        m = conn.execute("SELECT household_id FROM meters WHERE id=?", (meter_id,)).fetchone()
        if m and m["household_id"]:
            conn.execute("DELETE FROM takeovers WHERE meter_id=?", (meter_id,))
            conn.execute(
                "INSERT INTO takeovers(meter_id,household_id,start_date,start_value,reading_id,note) "
                "VALUES (?,?,?,?,?,?)",
                (meter_id, m["household_id"], reading_date, value_i, rc.lastrowid, "基数设置"))
    return RedirectResponse("/meters?ok=" + quote("起始读数已保存"), status_code=303)


@app.post("/meters/add")
def meter_add(name: str = Form(...), meter_type: str = Form(...), level: str = Form(...),
              main_meter_id: str = Form(""), phase: str = Form("single"),
              base_date: str = Form(...),
              base_value: str = Form(""), base_1: str = Form(""), base_2: str = Form(""),
              base_3: str = Form(""), note: str = Form("")):
    """新增表即录基数(表自己的原点,类似新表的 0),除非换表否则不变。"""
    if meter_type != "electricity":
        phase = "single"   # 只有电表分单相/三相
    phase = phase if phase in ("single", "three") else "single"
    if phase == "three":
        try:
            b1, b2, b3 = int(base_1), int(base_2), int(base_3)
        except (TypeError, ValueError):
            return RedirectResponse(
                "/meters?msg=" + quote("三相电表需分别填写三个基数"), status_code=303)
        if min(b1, b2, b3) < 0:
            return RedirectResponse("/meters?msg=" + quote("基数不能为负"), status_code=303)
        base_total, br1, br2, br3 = b1 + b2 + b3, b1, b2, b3
    else:
        try:
            base_total = int(base_value)
        except (TypeError, ValueError):
            return RedirectResponse("/meters?msg=" + quote("请填写表基数"), status_code=303)
        br1 = br2 = br3 = None
    if base_total < 0:
        return RedirectResponse("/meters?msg=" + quote("基数不能为负"), status_code=303)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,base_date,note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name.strip(), meter_type, level, int(main_meter_id) if main_meter_id else None,
             phase, base_total, base_date, note.strip()),
        )
        mid = cur.lastrowid
        # 基数同时写成一条 is_initial 读数,计费首期用它做起点
        conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,is_initial,r1,r2,r3,note) "
            "VALUES (?,?,?,1,?,?,?,?)",
            (mid, base_date, base_total, br1, br2, br3, "表基数"))
    return RedirectResponse("/meters?ok=" + quote("表已新增,基数已记录"), status_code=303)


@app.post("/meters/edit")
def meter_edit(id: int = Form(...), name: str = Form(""), main_meter_id: str = Form(""),
               active: str = Form("1"), note: str = Form("")):
    with connect() as conn:
        # 不在此处修改归属租户:绑定只在新增租户/换表交接时发生
        conn.execute(
            "UPDATE meters SET name=COALESCE(NULLIF(?,''),name), main_meter_id=?, "
            "active=?, note=? WHERE id=?",
            (name.strip(), int(main_meter_id) if main_meter_id else None,
             int(active), note.strip(), id),
        )
    return RedirectResponse("/meters?ok=" + quote("表已更新"), status_code=303)


@app.post("/meters/delete")
def meter_delete(id: int = Form(...)):
    """删除表:连带清理其读数、租期、换表记录与缴费记录,保证外键不炸。"""
    with connect() as conn:
        conn.execute("DELETE FROM takeovers WHERE meter_id=?", (id,))
        conn.execute("DELETE FROM readings WHERE meter_id=?", (id,))
        conn.execute("DELETE FROM payments WHERE meter_id=?", (id,))
        conn.execute(
            "DELETE FROM meter_switches WHERE old_meter_id=? OR new_meter_id=?", (id, id))
        conn.execute("UPDATE meters SET main_meter_id=NULL WHERE main_meter_id=?", (id,))
        conn.execute("DELETE FROM meters WHERE id=?", (id,))
    return RedirectResponse("/meters?ok=" + quote("表已删除"), status_code=303)


# ---------- 抄表 ----------

@app.get("/readings", response_class=HTMLResponse)
def readings_page(request: Request, msg: str = Query(""), ok: str = Query(""), edit: int = Query(0),
                  meter_id: str = Query(""), household_id: str = Query(""),
                  date_from: str = Query(""), date_to: str = Query(""),
                  billed: str = Query(""), page: int = Query(1)):
    if r := require_login(request):
        return r
    today = date.today().isoformat()
    with connect() as conn:
        meters = [dict(x) for x in conn.execute("SELECT * FROM meters ORDER BY meter_type, level, id")]
        hh_names = {x["id"]: x["name"] for x in conn.execute("SELECT id,name FROM households")}

        where, params = [], []
        if meter_id:
            where.append("r.meter_id = ?"); params.append(int(meter_id))
        if household_id:
            where.append("m.household_id = ?"); params.append(int(household_id))
        if date_from:
            where.append("r.reading_date >= ?"); params.append(date_from)
        if date_to:
            where.append("r.reading_date <= ?"); params.append(date_to)
        if billed == "init":
            where.append("r.is_initial = 1")
        else:
            where.append("r.is_initial = 0")
            if billed == "1":
                where.append("r.billed = 1")
            elif billed == "0":
                where.append("r.billed = 0")
        # 交接读数是租期边界:不属于任何待出账/已出账筛选,页面上显示为起点
        boundary_ids = [r["reading_id"] for r in conn.execute(
            "SELECT reading_id FROM takeovers WHERE reading_id IS NOT NULL")]
        if billed in ("0", "1") and boundary_ids:
            where.append(f"r.id NOT IN ({','.join('?' * len(boundary_ids))})")
            params.extend(boundary_ids)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        agg = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(r.value),0) v FROM readings r "
            f"JOIN meters m ON r.meter_id = m.id {clause}", params
        ).fetchone()

        # 金额合计:基于全部命中记录,用量 × 租户单价
        all_rows = conn.execute(
            f"SELECT r.* FROM readings r JOIN meters m ON r.meter_id = m.id "
            f"{clause} ORDER BY r.reading_date, r.id", params
        ).fetchall()
        total_amount = 0.0
        _prev: dict[int, int] = {}
        for r0 in all_rows:
            mid0 = r0["meter_id"]
            if not r0["is_initial"] and mid0 in _prev:
                total_amount += (r0["value"] - _prev[mid0]) * r0["tenant_price"]
            _prev[mid0] = r0["value"]
        total_amount = round(total_amount, 2)

        per_page = 20
        pages = max(1, (agg["n"] + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        raw = conn.execute(
            f"SELECT r.*, m.name AS meter_name, m.meter_type, m.level, m.phase, m.household_id "
            f"FROM readings r JOIN meters m ON r.meter_id = m.id {clause} "
            f"ORDER BY r.reading_date DESC, r.id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

        # 每块表的上一次读数
        prev_of: dict[int, int] = {}
        for m in meters:
            seq = conn.execute(
                "SELECT id, value FROM readings WHERE meter_id=? ORDER BY reading_date, id",
                (m["id"],)).fetchall()
            for a, b in zip(seq, seq[1:]):
                prev_of[b["id"]] = a["value"]

        qs_base = (f"meter_id={meter_id}&household_id={household_id}"
                   f"&date_from={date_from}&date_to={date_to}&billed={billed}")
        page, pages, pager = paginate(agg["n"], page, qs_base)

    # 视图展示用的行数据
    display = []
    for x in raw:
        d = dict(x)
        d["household_name"] = hh_names.get(x["household_id"] or 0, "—")
        if x["is_initial"]:
            d.update(usage="—", amount="—",
                     state='<span class="tag muted">起点</span>', is_init=True)
        else:
            prev = prev_of.get(x["id"])
            usage = (x["value"] - prev) if prev is not None else 0
            d["usage"] = f"{usage:+d}" if prev is not None else "—"
            d["amount"] = money(round(usage * x["tenant_price"], 2))
            d["state"] = ('<span class="tag ok">已出账</span>' if x["billed"]
                          else '<span class="tag warn">待出账</span>')
            d["is_init"] = False
        display.append(d)

    return render("readings.html", title="抄表",
                  lead="记录各表的读数,系统据此计算用量与费用。",
                  active="readings", meters=meters, hh_names=hh_names,
                  hh_items=sorted(hh_names.items(), key=lambda kv: kv[1]),
                  rows=display, agg=agg, total_amount=total_amount,
                  page=page, pages=pages, pager=pager, today=today,
                  meter_id=meter_id, household_id=household_id,
                  date_from=date_from, date_to=date_to, billed=billed,
                  edit=edit, flash=flash_from(msg, ok))


@app.post("/readings/add")
def reading_add(meter_id: int = Form(...), reading_date: str = Form(...),
                value: str = Form(""),
                value_1: str = Form(""), value_2: str = Form(""), value_3: str = Form(""),
                tenant_price: str = Form(""), note: str = Form("")):
    with connect() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter:
            return RedirectResponse("/readings?msg=" + quote("表不存在"), status_code=303)
        is_init = False
        r1 = r2 = r3 = None
        if meter["phase"] == "three":
            try:
                r1, r2, r3 = int(value_1), int(value_2), int(value_3)
            except (TypeError, ValueError):
                return RedirectResponse(
                    "/readings?msg=" + quote("三相电表需要分别填写三个读数"), status_code=303)
            if min(r1, r2, r3) < 0:
                return RedirectResponse(
                    "/readings?msg=" + quote("三相读数不能为负"), status_code=303)
            value_i = r1 + r2 + r3
        else:
            try:
                value_i = int(value)
            except (TypeError, ValueError):
                return RedirectResponse(
                    "/readings?msg=" + quote("请填写本次读数"), status_code=303)
        last = conn.execute(
            "SELECT tenant_price FROM readings WHERE meter_id=? "
            "ORDER BY reading_date DESC, id DESC LIMIT 1", (meter_id,)).fetchone()
        tp = float(tenant_price) if tenant_price != "" else (last["tenant_price"] if last else 0.0)
        prev = conn.execute(
            "SELECT * FROM readings WHERE meter_id=? ORDER BY reading_date DESC, id DESC LIMIT 1",
            (meter_id,),
        ).fetchone()
        if prev is not None:
            if value_i < prev["value"]:
                return RedirectResponse(
                    "/readings?msg=" + quote(
                        f"本次读数合计 {value_i} 小于上次读数 {prev['value']},请核对表码。"),
                    status_code=303)
            # 三相:每一相都应不小于上次对应相
            if meter["phase"] == "three" and prev["r1"] is not None:
                if (r1 < prev["r1"]) or (r2 < prev["r2"]) or (r3 < prev["r3"]):
                    return RedirectResponse(
                        "/readings?msg=" + quote(
                            f"某一相读数小于上次({prev['r1']}/{prev['r2']}/{prev['r3']}),请核对。"),
                        status_code=303)
            if reading_date <= prev["reading_date"]:
                return RedirectResponse(
                    "/readings?msg=" + quote(
                        f"抄表日期必须晚于上次抄表日期 {prev['reading_date']}。"),
                    status_code=303)
        conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,tenant_price,r1,r2,r3,note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (meter_id, reading_date, value_i, tp, r1, r2, r3, note.strip()),
        )
    return RedirectResponse("/readings?ok=" + quote("抄表已保存"), status_code=303)


@app.post("/readings/edit")
def reading_edit(id: int = Form(...), reading_date: str = Form(""), value: str = Form(""),
                 department_price: str = Form(""), tenant_price: str = Form(""),
                 value_1: str = Form(""), value_2: str = Form(""), value_3: str = Form("")):
    with connect() as conn:
        row = conn.execute("SELECT * FROM readings WHERE id=?", (id,)).fetchone()
        if not row:
            return RedirectResponse("/readings?msg=" + quote("找不到这条抄表记录"), status_code=303)
        if row["billed"]:
            return RedirectResponse(
                "/readings?msg=" + quote("已出账的记录不能修改,请改用「冲正」。"), status_code=303)
        if conn.execute("SELECT 1 FROM takeovers WHERE reading_id=?", (id,)).fetchone():
            return RedirectResponse(
                "/readings?msg=" + quote("交接读数是租期边界,不能修改。"), status_code=303)
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (row["meter_id"],)).fetchone()
        r1 = r2 = r3 = None
        if meter["phase"] == "three":
            try:
                r1, r2, r3 = int(value_1), int(value_2), int(value_3)
            except (TypeError, ValueError):
                return RedirectResponse(
                    "/readings?msg=" + quote("三相电表需要分别填写三个读数"), status_code=303)
            new_value = r1 + r2 + r3
        else:
            new_value = int(value) if value != "" else row["value"]
        new_date = reading_date or row["reading_date"]
        # 相邻记录仍是不可越过的边界,否则用量会出现负数
        prev = conn.execute(
            "SELECT value, reading_date FROM readings WHERE meter_id=? "
            "AND (reading_date < ? OR (reading_date = ? AND id < ?)) "
            "ORDER BY reading_date DESC, id DESC LIMIT 1",
            (row["meter_id"], row["reading_date"], row["reading_date"], id),
        ).fetchone()
        nxt = conn.execute(
            "SELECT value, reading_date FROM readings WHERE meter_id=? "
            "AND (reading_date > ? OR (reading_date = ? AND id > ?)) "
            "ORDER BY reading_date, id LIMIT 1",
            (row["meter_id"], row["reading_date"], row["reading_date"], id),
        ).fetchone()
        if prev and (new_value < prev["value"] or new_date <= prev["reading_date"]):
            return RedirectResponse(
                "/readings?msg=" + quote(
                    f"读数需不小于上一条 {prev['value']},日期需晚于 {prev['reading_date']}"),
                status_code=303)
        if nxt and (new_value > nxt["value"] or new_date >= nxt["reading_date"]):
            return RedirectResponse(
                "/readings?msg=" + quote(
                    f"读数不能大于下一条 {nxt['value']},日期需早于 {nxt['reading_date']}"),
                status_code=303)
        conn.execute(
            "UPDATE readings SET reading_date=?, value=?, department_price=?, tenant_price=?, "
            "r1=?, r2=?, r3=? WHERE id=?",
            (new_date, new_value,
             float(department_price) if department_price != "" else row["department_price"],
             float(tenant_price) if tenant_price != "" else row["tenant_price"],
             r1, r2, r3, id),
        )
    return RedirectResponse("/readings?ok=" + quote("抄表记录已更新"), status_code=303)


@app.post("/readings/bill")
def reading_bill(id: int = Form(...)):
    """出账:锁定读数并生成一条应收。"""
    with connect() as conn:
        row = conn.execute("SELECT * FROM readings WHERE id=?", (id,)).fetchone()
        if not row:
            return RedirectResponse("/readings?msg=" + quote("找不到该抄表记录"), status_code=303)
        if row["billed"]:
            return RedirectResponse("/readings?msg=" + quote("该记录已出账"), status_code=303)
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (row["meter_id"],)).fetchone()
        if not meter or meter["level"] != "sub":
            return RedirectResponse(
                "/readings?msg=" + quote("总表不产生租户账单,无需出账"), status_code=303)
        if not meter["household_id"]:
            return RedirectResponse(
                "/readings?msg=" + quote("该表未绑定租户,无法出账"), status_code=303)
        # 本段用量 = 本次读数 − 该表上一条读数
        prev = conn.execute(
            "SELECT value, reading_date FROM readings WHERE meter_id=? AND id < ? "
            "ORDER BY reading_date DESC, id DESC LIMIT 1", (row["meter_id"], id)).fetchone()
        usage = row["value"] - (prev["value"] if prev else 0)
        amount = round(usage * row["tenant_price"], 2)
        conn.execute("UPDATE readings SET billed=1 WHERE id=?", (id,))
        conn.execute(
            "INSERT OR REPLACE INTO receivables(household_id,meter_id,reading_id,period_start,"
            "period_end,usage_amount,unit_price,amount,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (meter["household_id"], meter["id"], id,
             prev["reading_date"] if prev else (meter["base_date"] or row["reading_date"]),
             row["reading_date"], usage, row["tenant_price"], amount,
             date.today().isoformat()))
    return RedirectResponse("/readings?ok=" + quote("已出账,应收已生成"), status_code=303)


@app.post("/readings/delete")
def reading_delete(id: int = Form(...)):
    with connect() as conn:
        row = conn.execute("SELECT billed FROM readings WHERE id=?", (id,)).fetchone()
        if row and row["billed"]:
            return RedirectResponse("/readings?msg=" + quote("已出账的记录不能删除,请改用冲正。"),
                                    status_code=303)
        if conn.execute("SELECT 1 FROM takeovers WHERE reading_id=?", (id,)).fetchone():
            return RedirectResponse(
                "/readings?msg=" + quote("交接读数是租期边界,不能删除。"), status_code=303)
        conn.execute("DELETE FROM readings WHERE id=? AND billed=0", (id,))
    return RedirectResponse("/readings?ok=" + quote("抄表记录已删除"), status_code=303)


# ---------- 换表 ----------

@app.get("/switches", response_class=HTMLResponse)
def switches_page(request: Request, msg: str = "", ok: str = "", page: int = 1):
    if r := require_login(request):
        return r
    today = date.today().isoformat()
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM meter_switches").fetchone()[0]
        page, pages, pager = paginate(total, page, "")
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM meter_switches ORDER BY switch_date DESC, id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        )]
        meters = [dict(x) for x in conn.execute("SELECT * FROM meters ORDER BY meter_type, level, id")]
    names = {x["id"]: x["name"] for x in meters}
    return render("switches.html", title="换表",
                  lead="租户的表损坏或到期更换时,在这里登记新旧表的衔接。",
                  active="switches", rows=rows, meters=meters, names=names,
                  total=total, page=page, pages=pages, pager=pager, today=today,
                  flash=flash_from(msg, ok))


@app.post("/switches/add")
def switch_add(old_meter_id: int = Form(...), new_meter_id: int = Form(...),
               switch_date: str = Form(...),
               old_final_value: str = Form("0"), new_base_value: str = Form("0"),
               old_1: str = Form(""), old_2: str = Form(""), old_3: str = Form(""),
               new_1: str = Form(""), new_2: str = Form(""), new_3: str = Form(""),
               note: str = Form("")):
    """换表:旧表终读数 + 新表起始读数 + 停旧启用新 + 新表租期,一次完成。"""
    def _val(total, a, b, c, phase, label):
        if phase == "three":
            try:
                v1, v2, v3 = int(a), int(b), int(c)
            except (TypeError, ValueError):
                return None, None, None, f"{label}:三相电表需分别填写三个读数"
            if min(v1, v2, v3) < 0:
                return None, None, None, f"{label}:三相读数不能为负"
            return v1 + v2 + v3, v1, (v2, v3)
        try:
            return int(total), None, None, None
        except (TypeError, ValueError):
            return None, None, None, f"{label}:读数必须是数字"

    with connect() as conn:
        if old_meter_id == new_meter_id:
            return RedirectResponse("/switches?msg=" + quote("旧表和新表不能是同一块表"),
                                    status_code=303)
        old_m = conn.execute("SELECT * FROM meters WHERE id=?", (old_meter_id,)).fetchone()
        new_m = conn.execute("SELECT * FROM meters WHERE id=?", (new_meter_id,)).fetchone()
        if not old_m or not new_m:
            return RedirectResponse("/switches?msg=" + quote("表不存在"), status_code=303)
        old_total, old_r1, old_rest, err = _val(old_final_value, old_1, old_2, old_3,
                                                old_m["phase"], "旧表终读数")
        if err:
            return RedirectResponse("/switches?msg=" + quote(err), status_code=303)
        new_total, new_r1, new_rest, err = _val(new_base_value, new_1, new_2, new_3,
                                                new_m["phase"], "新表起始读数")
        if err:
            return RedirectResponse("/switches?msg=" + quote(err), status_code=303)
        old_last = conn.execute(
            "SELECT department_price, tenant_price FROM readings WHERE meter_id=? "
            "ORDER BY reading_date DESC, id DESC LIMIT 1", (old_meter_id,)).fetchone()
        dp = old_last["department_price"] if old_last else 0
        tp = old_last["tenant_price"] if old_last else 0
        conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,department_price,tenant_price,"
            "r1,r2,r3,note) VALUES (?,?,?,?,?,?,?,?,?)",
            (old_meter_id, switch_date, old_total, dp, tp,
             old_r1, old_rest[0] if old_rest else None, old_rest[1] if old_rest else None,
             "旧表终读数 " + note))
        rc = conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,department_price,tenant_price,"
            "r1,r2,r3,is_initial,note) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (new_meter_id, switch_date, new_total, dp, tp,
             new_r1, new_rest[0] if new_rest else None, new_rest[1] if new_rest else None,
             "新表起始读数 " + note))
        conn.execute("UPDATE meters SET active=0 WHERE id=?", (old_meter_id,))
        conn.execute("UPDATE meters SET active=1 WHERE id=?", (new_meter_id,))
        # 新表继承旧表租户,建立租期;旧表租期保持(历史账单不丢)
        hh_id = old_m["household_id"]
        if hh_id:
            conn.execute("UPDATE meters SET household_id=? WHERE id=?", (hh_id, new_meter_id))
            conn.execute(
                "INSERT INTO takeovers(meter_id,household_id,start_date,start_value,reading_id,note) "
                "VALUES (?,?,?,?,?,?)",
                (new_meter_id, hh_id, switch_date, new_total, rc.lastrowid,
                 f"换表交接:{note or '无备注'}"))
        conn.execute(
            "INSERT INTO meter_switches(old_meter_id,new_meter_id,switch_date,old_final_value,"
            "new_base_value,note) VALUES (?,?,?,?,?,?)",
            (old_meter_id, new_meter_id, switch_date, old_total, new_total, note.strip()))
    return RedirectResponse("/switches?ok=" + quote("换表已登记,旧表已停用"), status_code=303)


# ---------- 缴费 ----------

@app.get("/payments", response_class=HTMLResponse)
def payments_page(request: Request, msg: str = "", ok: str = "", page: int = 1,
                  meter_id: str = "", date_from: str = "", date_to: str = ""):
    if r := require_login(request):
        return r
    today = date.today()
    period_end_default = add_month(today).isoformat()
    today = today.isoformat()
    with connect() as conn:
        meters = [dict(x) for x in conn.execute(
            "SELECT * FROM meters WHERE level='main' ORDER BY meter_type, id")]
        where, params = [], []
        if meter_id:
            where.append("p.meter_id = ?"); params.append(int(meter_id))
        if date_from:
            where.append("p.payment_date >= ?"); params.append(date_from)
        if date_to:
            where.append("p.payment_date <= ?"); params.append(date_to)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        agg = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(p.amount),0) s FROM payments p {clause}", params
        ).fetchone()
        page, pages, pager = paginate(
            agg["n"], page,
            f"meter_id={meter_id}&date_from={date_from}&date_to={date_to}",
        )
        rows = [dict(x) for x in conn.execute(
            f"SELECT p.*, m.name AS meter_name FROM payments p JOIN meters m ON p.meter_id=m.id "
            f"{clause} ORDER BY p.payment_date DESC, p.id DESC LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )]
        totals = {r["meter_id"]: r["total"] for r in conn.execute(
            "SELECT meter_id, SUM(amount) AS total FROM payments GROUP BY meter_id")}
        last_reading: dict[int, int] = {}
        for m in meters:
            rr = conn.execute(
                "SELECT reading_value FROM payments WHERE meter_id=? AND reading_value IS NOT NULL "
                "ORDER BY payment_date DESC, id DESC LIMIT 1", (m["id"],)).fetchone()
            if rr:
                last_reading[m["id"]] = rr["reading_value"]
    names = {x["id"]: x["name"] for x in meters}
    per_meter_stats = [
        {"name": names.get(m["id"], ""), "total": totals.get(m["id"], 0)} for m in meters
    ]
    last_js = "{" + ",".join(f'"{k}":{v}' for k, v in last_reading.items()) + "}"
    return render("payments.html", title="缴费",
                  lead="记录向供水、供电部门实际缴纳的费用,用于对账。",
                  active="payments", meters=meters, rows=rows, agg=agg,
                  page=page, pages=pages, pager=pager, today=today,
                  meter_id=meter_id, date_from=date_from, date_to=date_to,
                  per_meter_stats=per_meter_stats, last_js=last_js,
                  period_end_default=period_end_default,
                  flash=flash_from(msg, ok))


@app.post("/payments/add")
def payment_add(meter_id: int = Form(...), payment_date: str = Form(...),
                reading_value: str = Form(""), unit_price: str = Form(""),
                amount: str = Form(""),
                period_start: str = Form(...), period_end: str = Form(...), note: str = Form("")):
    with connect() as conn:
        # 读数与单价必填;金额能自动算就用算的,否则用提交值
        try:
            rv = int(reading_value)
            up = float(unit_price)
        except (TypeError, ValueError):
            return RedirectResponse(
                "/payments?msg=" + quote("请填写本次读数与部门单价"), status_code=303)
        # 上次读数取自该总表上一次缴费,而不是抄表页的分表读数
        prev = conn.execute(
            "SELECT reading_value FROM payments WHERE meter_id=? AND reading_value IS NOT NULL "
            "ORDER BY payment_date DESC, id DESC LIMIT 1",
            (meter_id,),
        ).fetchone()
        prev_val = prev["reading_value"] if prev else None
        if prev_val is not None and rv < prev_val:
            return RedirectResponse(
                "/payments?msg=" + quote(
                    f"本次读数 {rv} 小于该总表上次缴费读数 {prev_val},请核对。"), status_code=303)
        usage = (rv - prev_val) if prev_val is not None else 0
        computed = round(usage * up, 2)
        final_amount = computed
        if amount not in ("", None):
            try:
                given = float(amount)
                # 用户手动改过金额则尊重手填值
                if abs(given - computed) > 0.005:
                    final_amount = round(given, 2)
            except ValueError:
                pass
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,amount,period_start,period_end,"
            "unit_price,reading_value,note) VALUES (?,?,?,?,?,?,?,?)",
            (meter_id, payment_date, final_amount, period_start, period_end,
             up, rv, note.strip()),
        )
    return RedirectResponse(
        "/payments?ok=" + quote(f"缴费记录已保存,金额 {final_amount:.2f} 元"), status_code=303)


@app.post("/payments/delete")
def payment_delete(id: int = Form(...)):
    with connect() as conn:
        conn.execute("DELETE FROM payments WHERE id=?", (id,))
    return RedirectResponse("/payments?ok=" + quote("缴费记录已删除"), status_code=303)


# ---------- 冲正 ----------

@app.get("/adjustments", response_class=HTMLResponse)
def adjustments_page(request: Request, msg: str = "", ok: str = "", page: int = 1):
    if r := require_login(request):
        return r
    today = date.today().isoformat()
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0]
        adj_sum = conn.execute("SELECT COALESCE(SUM(amount),0) FROM adjustments").fetchone()[0]
        page, pages, pager = paginate(total, page, "")
        rows = [dict(x) for x in conn.execute(
            "SELECT a.*, h.name AS household_name, m.name AS meter_name, r.reading_date "
            "FROM adjustments a "
            "JOIN households h ON a.household_id = h.id "
            "LEFT JOIN readings r ON a.reading_id = r.id "
            "LEFT JOIN meters m ON r.meter_id = m.id "
            "ORDER BY a.adjust_date DESC, a.id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        )]
        billed = [dict(x) for x in conn.execute(
            "SELECT r.id, r.reading_date, r.value, m.name AS meter_name "
            "FROM readings r JOIN meters m ON r.meter_id=m.id "
            "WHERE r.billed=1 AND r.is_initial=0 ORDER BY r.reading_date DESC LIMIT 100"
        )]
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
    return render("adjustments.html", title="冲正",
                  lead="账单出账后如需更正,用冲正记录加减金额,不改动原始账单。",
                  active="adjustments", rows=rows, billed=billed, households=households,
                  total=total, adj_sum=adj_sum, page=page, pages=pages,
                  pager=pager, today=today, flash=flash_from(msg, ok))


@app.post("/adjustments/add")
def adjustment_add(reading_id: int = Form(...), household_id: int = Form(...), amount: float = Form(...),
                   adjust_date: str = Form(...), note: str = Form("")):
    with connect() as conn:
        conn.execute(
            "INSERT INTO adjustments(reading_id,household_id,amount,adjust_date,note) VALUES (?,?,?,?,?)",
            (reading_id, household_id, amount, adjust_date, note.strip()),
        )
    return RedirectResponse("/adjustments?ok=" + quote("冲正记录已保存"), status_code=303)


@app.post("/adjustments/delete")
def adjustment_delete(id: int = Form(...)):
    with connect() as conn:
        conn.execute("DELETE FROM adjustments WHERE id=?", (id,))
    return RedirectResponse("/adjustments?ok=" + quote("冲正记录已删除"), status_code=303)


# ---------- 统计 ----------

def _query_rows(conn, type: str, household_id: str, meter_id: str,
                date_from: str, date_to: str, billed: str) -> list[dict]:
    types = ["water", "electricity"] if type == "all" else [type]
    rows = []
    for t in types:
        for row in all_statistics(conn, t):
            row["meter_type"] = t
            rows.append(row)

    def keep(x: dict) -> bool:
        if household_id and str(x["household_id"]) != household_id:
            return False
        if meter_id and str(x.get("meter_id")) != meter_id:
            return False
        if date_from and x["end"] < date_from:
            return False
        if date_to and x["end"] > date_to:
            return False
        if billed == "1" and not x["billed"]:
            return False
        if billed == "0" and x["billed"]:
            return False
        return True

    return [x for x in rows if keep(x)]


@app.get("/statistics", response_class=HTMLResponse)
def statistics_page(request: Request, type: str = Query("all"), household_id: str = Query(""),
                    meter_id: str = Query(""), date_from: str = Query(""),
                    date_to: str = Query(""), billed: str = Query(""), page: int = Query(1)):
    if r := require_login(request):
        return r
    with connect() as conn:
        rows = _query_rows(conn, type, household_id, meter_id, date_from, date_to, billed)
        households = [dict(x) for x in conn.execute("SELECT id,name FROM households ORDER BY id")]
        meters = [dict(x) for x in conn.execute(
            "SELECT id,name,meter_type FROM meters WHERE level='sub' ORDER BY id")]
        adjustments = [dict(x) for x in conn.execute(
            "SELECT h.id, h.name AS name, SUM(a.amount) AS total FROM adjustments a "
            "JOIN households h ON a.household_id = h.id GROUP BY a.household_id")]

    total_water = sum(x["usage"] for x in rows if x["meter_type"] == "water")
    total_elec = sum(x["usage"] for x in rows if x["meter_type"] == "electricity")
    total_amount = round(sum(x["amount"] for x in rows), 2)
    adj_total = round(sum(x["total"] for x in adjustments if
                          not household_id or str(x["id"]) == household_id), 2)
    net_amount = round(total_amount + adj_total, 2)

    qs = (f"type={type}&household_id={household_id}&meter_id={meter_id}"
          f"&date_from={date_from}&date_to={date_to}&billed={billed}")
    total_rows = len(rows)
    page, pages, pager = paginate(total_rows, page, qs)
    page_rows = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    return render("statistics.html", title="统计与查询",
                  lead="按租户、表、账期或出账状态查询用量与费用。",
                  active="statistics", type=type, household_id=household_id,
                  meter_id=meter_id, date_from=date_from, date_to=date_to,
                  billed=billed, qs=qs, households=households, meters=meters,
                  adjustments=adjustments, rows=rows, page_rows=page_rows,
                  total_rows=total_rows, page=page, pages=pages, pager=pager,
                  total_water=total_water, total_elec=total_elec,
                  total_amount=total_amount, adj_total=adj_total, net_amount=net_amount)


@app.get("/statistics/pdf")
def statistics_pdf(type: str = Query("all"), household_id: str = Query(""),
                   meter_id: str = Query(""), date_from: str = Query(""),
                   date_to: str = Query(""), billed: str = Query("")):
    from fpdf import FPDF

    with connect() as conn:
        rows = _query_rows(conn, type, household_id, meter_id, date_from, date_to, billed)

    font_path = os.environ.get("SDF_PDF_FONT", "C:/Windows/Fonts/simhei.ttf")
    has_cjk = Path(font_path).exists()

    pdf = FPDF(orientation="L")
    pdf.add_page()
    if has_cjk:
        pdf.add_font("cjk", fname=font_path)
        pdf.set_font("cjk", size=10)
        title = "水电费统计报表"
        headers = ["租户", "分表", "账期起", "账期止", "用量", "单价", "金额(元)", "状态"]
    else:
        pdf.set_font("Helvetica", size=9)
        title = "Utility Bill Report"
        headers = ["Household", "Meter", "Start", "End", "Usage", "Price", "Amount", "Status"]
    widths = [42, 52, 28, 28, 24, 24, 32, 24]

    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font_size(9)
    for head, w in zip(headers, widths):
        pdf.cell(w, 8, head, border=1, align="C")
    pdf.ln()
    for x in rows:
        vals = [
            x["household_name"], x["meter_name"], x.get("start", ""), x["end"],
            f"{x['usage']:,}", x["unit_price"], f"{x['amount']:,.2f}",
            "已出账" if x["billed"] else "待出账",
        ]
        for val, w in zip(vals, widths):
            pdf.cell(w, 8, str(val)[:24], border=1)
        pdf.ln()

    data = bytes(pdf.output())
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=bills.pdf"})
