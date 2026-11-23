"""账务:应收、实收、预收余额、欠款。

收款与退款都记在**租户名下**,不指定账单。系统按账期从早到晚自动冲抵
(先欠先冲);超出全部欠款的部分成为该租户的预收余额,可用于抵扣后续账期,也可退还。

冲抵是推导出来的,不单独存表 —— 唯一的持久事实是应收与收退款条目,
这样任何时刻都能从事实重算出同一个结果。
"""

from .reading_index import latest_meter_names

EPS = 0.005


def _entries(conn, household_id: int):
    return conn.execute(
        "SELECT * FROM receipts WHERE household_id = ? ORDER BY entry_date, id",
        (household_id,)).fetchall()


def _receivables(conn, household_id: int):
    rows = conn.execute(
        "SELECT r.*, l.meter_type, l.label FROM receivables r "
        "JOIN lines l ON r.line_id = l.id "
        "WHERE r.household_id = ? ORDER BY r.period_end, r.id",
        (household_id,)).fetchall()
    names = latest_meter_names(conn, {x["line_id"] for x in rows})
    return [{**dict(x), "meter_name": names.get(x["line_id"], "")} for x in rows]


def _place(net_paid: float, due_rows) -> list[float]:
    """先欠先冲:把已付净额按账期先后依次分配,返回每一期落到的钱。

    完整台账与锁判定共用这一处,两者因此永远不会漂移。
    """
    remaining, placed = net_paid, []
    for x in due_rows:
        amount = round(x["amount"], 2)
        take = round(min(max(remaining, 0.0), amount), 2)
        remaining = round(remaining - take, 2)
        placed.append(take)
    return placed


def _net_paid(conn, household_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'receipt' THEN amount ELSE -amount END), 0) "
        "FROM receipts WHERE household_id = ?", (household_id,)).fetchone()
    return round(row[0], 2)


def _settle(due_rows, entries) -> dict:
    """纯函数:一组应收 + 一组收退款 → 逐期冲抵结果与合计。

    完整台账、账期视图、锁判定都从这里出发,因此三者的「已收」不可能漂移。
    """
    inflow = round(sum(e["amount"] for e in entries if e["kind"] == "receipt"), 2)
    refunded = round(sum(e["amount"] for e in entries if e["kind"] == "refund"), 2)
    net_paid = round(inflow - refunded, 2)

    remaining = net_paid
    items = []
    for x, placed in zip(due_rows, _place(net_paid, due_rows)):
        amount = round(x["amount"], 2)
        remaining = round(remaining - placed, 2)
        items.append({
            "id": x["id"],
            "line_id": x["line_id"],
            "meter_type": x["meter_type"],
            "label": x["label"],
            "meter_name": x["meter_name"] or "",
            "period_start": x["period_start"],
            "period_end": x["period_end"],
            "usage": x["usage_amount"],
            "unit_price": x["unit_price"],
            "amount": amount,
            "paid": placed,
            "outstanding": round(amount - placed, 2),
            "settled": placed >= amount - EPS,
            "partial": 0 < placed < amount - EPS,
        })

    total_due = round(sum(i["amount"] for i in items), 2)
    total_paid = round(sum(i["paid"] for i in items), 2)
    balance = round(max(remaining, 0.0), 2)     # 预收余额
    outstanding = round(max(total_due - total_paid, 0.0), 2)

    return {
        "rows": items,
        "total_due": total_due,
        "inflow": inflow,
        "refunded": refunded,
        "total_paid": total_paid,
        "balance": balance,
        "outstanding": outstanding,
        "entries": [dict(e) for e in entries],
    }


def household_ledger(conn, household_id: int) -> dict:
    """某租户的完整账务:每笔应收 + 冲抵结果 + 余额,以及合计。"""
    return _settle(_receivables(conn, household_id), _entries(conn, household_id))


def paid_spans(conn) -> list[dict]:
    """全部已收到钱的账期区间,一次算完。抄表页逐行判定可编辑性时用。

    与 paid_periods 同一处冲抵,只是不按户取 —— 页面有几十行时不能一行跑一遍。
    区间带 ``line_id``:锁定是「这一条计量线的这一期」,不是「这个租户这段时间」。
    """
    spans = []
    for x in ledger_view(conn)["rows"]:
        if x["paid"] > EPS:
            spans.append({"line_id": x["line_id"], "household_id": x["household_id"],
                          "start": x["period_start"], "end": x["period_end"]})
    return spans


def paid_periods(conn, household_id: int) -> list[dict]:
    """该租户已收到钱的账期。锁定规则的唯一判据,三处调用共用(改读数、撤回、补录)。

    "已收款"按冲抵结果算:先欠先冲之后落在这一期上的钱大于 0,这一期就算锁死。
    这里只取账期的起止与金额,与完整台账共用同一处冲抵,不构造逐笔明细。
    """
    due_rows = conn.execute(
        "SELECT line_id, period_start, period_end, amount FROM receivables "
        "WHERE household_id = ? ORDER BY period_end, id", (household_id,)).fetchall()
    placed = _place(_net_paid(conn, household_id), due_rows)
    return [{"line_id": x["line_id"], "household_id": household_id,
             "start": x["period_start"], "end": x["period_end"], "paid": take}
            for x, take in zip(due_rows, placed) if take > EPS]


def span_hit(spans, line_id: int, reading_date: str, inserting: bool):
    """在一组已付款区间里找命中。区间口径见 touched_by_reading。

    只比同一条计量线:同一租户水路那期已收款,不该拦住电路补录。
    """
    for p in spans:
        if p["line_id"] != line_id:
            continue
        inside = (p["start"] < reading_date < p["end"] if inserting
                  else p["start"] <= reading_date <= p["end"])
        if inside:
            return p
    return None


def touched_by_reading(conn, line_id: int, reading_date: str,
                       inserting: bool) -> dict | None:
    """在某条计量线的某日期增/改读数会动到哪个已收款的账期;没有则返回 None。

    插入一条读数会把包含它的账期一分为二,因此区间用开区间;
    改或删一条读数会动到以它为端点的账期,用闭区间。
    """
    row = conn.execute("SELECT household_id FROM lines WHERE id=?", (line_id,)).fetchone()
    if row is None:
        return None
    return span_hit(paid_periods(conn, row["household_id"]), line_id, reading_date, inserting)


def ledger_view(conn) -> dict:
    """全部租户逐期的应收与冲抵结果 —— 所有读侧页面的唯一入口。

    冲抵每户做一次(仍是 ``_settle`` 这个纯函数),不再按页面各算一遍;
    账期行、已收、欠款、预收余额都从这里投影出去,所以五个页面不可能各自漂移。
    """
    due_rows = conn.execute(
        "SELECT r.*, l.meter_type, l.label, r.household_id AS hh FROM receivables r "
        "JOIN lines l ON r.line_id = l.id "
        "ORDER BY r.household_id, r.period_end, r.id").fetchall()
    entries = conn.execute(
        "SELECT r.*, h.name AS household_name FROM receipts r "
        "JOIN households h ON h.id = r.household_id "
        "ORDER BY r.entry_date, r.id").fetchall()
    meters = latest_meter_names(conn)

    grouped_due, grouped_entries = {}, {}
    for x in due_rows:
        grouped_due.setdefault(x["hh"], []).append(x)
    for e in entries:
        grouped_entries.setdefault(e["household_id"], []).append(e)

    rows, households = [], []
    for h in conn.execute("SELECT id, name FROM households ORDER BY id"):
        hh, name = h["id"], h["name"]
        led = _settle([{**dict(x), "meter_name": meters.get(x["line_id"], "")}
                       for x in grouped_due.get(hh, [])],
                      grouped_entries.get(hh, []))
        for x in led["rows"]:
            rows.append({**x, "household_id": hh, "household_name": name})
        households.append({"household_id": hh, "household_name": name,
                           "total_due": led["total_due"], "total_paid": led["total_paid"],
                           "balance": led["balance"], "outstanding": led["outstanding"]})

    return {
        "rows": rows,
        "by_key": {(x["line_id"], x["period_end"]): x for x in rows},
        "households": households,
        "entries": [dict(e) for e in reversed(entries)],   # 流水按登记时间倒序
        "totals": {
            "total_due": round(sum(x["total_due"] for x in households), 2),
            "total_paid": round(sum(x["total_paid"] for x in households), 2),
            "balance": round(sum(x["balance"] for x in households), 2),
            "outstanding": round(sum(x["outstanding"] for x in households), 2),
        },
    }


def tenant_due(conn, meter_type: str = None, household_id: int = None,
               date_from: str = "", date_to: str = "") -> float:
    """向租户应收:账期金额之和。这是房东向租户开出的钱。"""
    from .billing import all_periods
    return round(sum(p["amount"] for p in all_periods(
        conn, meter_type, household_id, date_from, date_to)), 2)


def paid_to_utility(conn, date_from: str = "", date_to: str = "") -> dict:
    """向部门缴出:按水/电分列的缴费金额。部门账单为准,系统不推算。"""
    out = {}
    for t in ("water", "electricity"):
        out[t] = round(conn.execute(
            "SELECT COALESCE(SUM(p.amount),0) FROM payments p JOIN meters m ON p.meter_id=m.id "
            "WHERE m.meter_type=? AND p.payment_date >= ? AND p.payment_date <= ?",
            (t, date_from or "0000-01-01", date_to or "9999-12-31")).fetchone()[0], 2)
    return out


def paid_for(conn, meter_type: str = None, date_from: str = "",
             date_to: str = "") -> float:
    """向部门缴出中与统计口径对应的那一份:只看水就只取水费,只看电就只取电费。"""
    paid = paid_to_utility(conn, date_from, date_to)
    return paid["water"] + paid["electricity"] if meter_type is None else paid[meter_type]


def margin_from(due: float, paid: float) -> float:
    """毛差:向租户应收减向部门缴出。两个数必须取自同一组筛选条件。"""
    return round(due - paid, 2)
