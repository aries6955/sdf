"""账务:应收、实收、预收余额、欠款。

收款与退款都记在**租户名下**,不指定账单。系统按账期从早到晚自动冲抵
(先欠先冲);超出全部欠款的部分成为该租户的预收余额,可用于抵扣后续账期,也可退还。

冲抵是推导出来的,不单独存表 —— 唯一的持久事实是应收与收退款条目,
这样任何时刻都能从事实重算出同一个结果。
"""

EPS = 0.005


def _entries(conn, household_id: int):
    return conn.execute(
        "SELECT * FROM receipts WHERE household_id = ? ORDER BY entry_date, id",
        (household_id,)).fetchall()


def _receivables(conn, household_id: int):
    return conn.execute(
        "SELECT r.*, l.meter_type, l.label, "
        "(SELECT m.name FROM meters m WHERE m.id = (SELECT rd.meter_id FROM readings rd "
        "  WHERE rd.line_id = l.id ORDER BY rd.reading_date DESC, rd.id DESC LIMIT 1)) AS meter_name "
        "FROM receivables r JOIN lines l ON r.line_id = l.id "
        "WHERE r.household_id = ? ORDER BY r.period_end, r.id",
        (household_id,)).fetchall()


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


def household_ledger(conn, household_id: int) -> dict:
    """某租户的完整账务:每笔应收 + 冲抵结果 + 余额,以及合计。"""
    due_rows = _receivables(conn, household_id)
    entries = _entries(conn, household_id)

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


def paid_periods(conn, household_id: int) -> list[dict]:
    """该租户已收到钱的账期。锁定规则的唯一判据,三处调用共用(改读数、撤回、补录)。

    "已收款"按冲抵结果算:先欠先冲之后落在这一期上的钱大于 0,这一期就算锁死。
    这里只取账期的起止与金额,与完整台账共用同一处冲抵,不构造逐笔明细。
    """
    due_rows = conn.execute(
        "SELECT period_start, period_end, amount FROM receivables "
        "WHERE household_id = ? ORDER BY period_end, id", (household_id,)).fetchall()
    placed = _place(_net_paid(conn, household_id), due_rows)
    return [{"start": x["period_start"], "end": x["period_end"], "paid": take}
            for x, take in zip(due_rows, placed) if take > EPS]


def touched_by_reading(conn, household_id: int, reading_date: str,
                       inserting: bool) -> dict | None:
    """在某日期增/改读数会动到哪个已收款的账期;没有则返回 None。

    插入一条读数会把包含它的账期一分为二,因此区间用开区间;
    改或删一条读数会动到以它为端点的账期,用闭区间。
    """
    for p in paid_periods(conn, household_id):
        inside = (p["start"] < reading_date < p["end"] if inserting
                  else p["start"] <= reading_date <= p["end"])
        if inside:
            return p
    return None


def all_outstanding(conn) -> list[dict]:
    """所有有欠款的租户,供首页与统计使用。"""
    result = []
    for h in conn.execute("SELECT id, name FROM households ORDER BY id"):
        led = household_ledger(conn, h["id"])
        if led["outstanding"] > EPS:
            result.append({
                "household_id": h["id"],
                "household_name": h["name"],
                "total_due": led["total_due"],
                "total_paid": led["total_paid"],
                "outstanding": led["outstanding"],
            })
    return result


def totals(conn, meter_type: str = None) -> dict:
    """应收合计 / 已收合计 / 预收余额合计,供首页与统计展示。"""
    from .billing import all_periods
    total_due = round(sum(p["amount"] for p in all_periods(conn, meter_type)), 2)
    total_paid = 0.0
    balance = 0.0
    for (hh_id,) in conn.execute("SELECT id FROM households"):
        led = household_ledger(conn, hh_id)
        total_paid += led["total_paid"]
        balance += led["balance"]
    return {"total_due": total_due,
            "total_paid": round(total_paid, 2),
            "balance": round(balance, 2)}
