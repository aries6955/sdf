"""账务:应收、实收、欠款。

出账(reading.billed=1)时生成一条应收。收款可以多次、可以不收足,
欠款 = 应收合计 − 实收合计。冲正(adjustments)按租户累计,计入应收口径。
"""


def receivable_for_reading(conn, reading_id: int):
    return conn.execute(
        "SELECT * FROM receivables WHERE reading_id = ?", (reading_id,)).fetchone()


def receipt_total(conn, receivable_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM receipts WHERE receivable_id = ?",
        (receivable_id,),
    ).fetchone()
    return round(row[0], 2)


def household_ledger(conn, household_id: int) -> dict:
    """某租户的完整账务:每笔应收 + 已收 + 欠款,以及合计。"""
    rows = conn.execute(
        "SELECT r.*, m.name AS meter_name, m.meter_type "
        "FROM receivables r JOIN meters m ON r.meter_id = m.id "
        "WHERE r.household_id = ? ORDER BY r.period_end, r.id",
        (household_id,),
    ).fetchall()
    items = []
    for x in rows:
        received = receipt_total(conn, x["id"])
        items.append({
            "id": x["id"],
            "meter_name": x["meter_name"],
            "meter_type": x["meter_type"],
            "period_start": x["period_start"],
            "period_end": x["period_end"],
            "usage": x["usage_amount"],
            "unit_price": x["unit_price"],
            "amount": round(x["amount"], 2),
            "received": received,
            "outstanding": round(x["amount"] - received, 2),
            "settled": received >= round(x["amount"], 2) - 0.005,
            "has_receipts": received > 0,
            "reading_id": x["reading_id"],
        })
    adj = round(conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM adjustments WHERE household_id = ?",
        (household_id,),
    ).fetchone()[0], 2)
    total_due = round(sum(i["amount"] for i in items) + adj, 2)
    total_received = round(sum(i["received"] for i in items), 2)
    return {
        "rows": items,
        "adjustments": adj,
        "total_due": total_due,
        "total_received": total_received,
        "outstanding": round(total_due - total_received, 2),
    }


def all_outstanding(conn) -> list[dict]:
    """所有有欠款的租户汇总,供总览与统计使用。"""
    result = []
    for h in conn.execute("SELECT id, name FROM households ORDER BY id"):
        led = household_ledger(conn, h["id"])
        if abs(led["outstanding"]) > 0.005:
            result.append({
                "household_id": h["id"],
                "household_name": h["name"],
                "total_due": led["total_due"],
                "total_received": led["total_received"],
                "outstanding": led["outstanding"],
            })
    return result


def unbilled_amount(conn, household_id: int = None) -> float:
    """尚未出账的账单金额(待出账),供提醒用。"""
    from .billing import all_statistics
    rows = all_statistics(conn, "water") + all_statistics(conn, "electricity")
    if household_id is not None:
        rows = [r for r in rows if r["household_id"] == household_id]
    return round(sum(r["amount"] for r in rows if not r["billed"]), 2)
