from collections import defaultdict
from .db import connect


def _meter_periods(conn, meter_id: int):
    """Return (prev_reading, current_reading) pairs for a meter, excluding base rows."""
    rows = conn.execute(
        "SELECT * FROM readings WHERE meter_id = ? AND is_initial = 0 "
        "ORDER BY reading_date, id",
        (meter_id,),
    ).fetchall()
    base = conn.execute(
        "SELECT * FROM readings WHERE meter_id = ? AND is_initial = 1 "
        "ORDER BY reading_date, id LIMIT 1",
        (meter_id,),
    ).fetchone()
    periods = []
    prev = base
    for row in rows:
        if prev is not None:
            periods.append((prev, row))
        prev = row
    return periods


def household_period_usage(conn, household_id: int, meter_type: str) -> list[dict]:
    """All billable periods for a household and utility type, as usage+cost rows."""
    # 总表只用于与部门缴费对账,不产生住户账单,因此排除 level='main'
    meters = conn.execute(
        "SELECT * FROM meters WHERE household_id = ? AND meter_type = ? AND level = 'sub' ORDER BY id",
        (household_id, meter_type),
    ).fetchall()
    result = []
    for meter in meters:
        for prev, cur in _meter_periods(conn, meter["id"]):
            usage = cur["value"] - prev["value"]
            result.append(
                {
                    "meter_id": meter["id"],
                    "meter_name": meter["name"],
                    "level": meter["level"],
                    "reading_id": cur["id"],
                    "start": prev["reading_date"],
                    "end": cur["reading_date"],
                    "usage": usage,
                    "unit_price": cur["tenant_price"],
                    "amount": round(usage * cur["tenant_price"], 2),
                    "billed": bool(cur["billed"]),
                }
            )
    result.sort(key=lambda r: (r["end"], r["reading_id"]))
    return result


def all_statistics(conn, meter_type: str) -> list[dict]:
    rows = []
    for h in conn.execute("SELECT * FROM households ORDER BY id"):
        for p in household_period_usage(conn, h["id"], meter_type):
            rows.append({**p, "household_id": h["id"], "household_name": h["name"]})
    rows.sort(key=lambda r: (r["end"], r["household_id"]))
    return rows
