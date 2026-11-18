"""计费逻辑:账期按租期分段。

租期(takeovers)记录每块表从哪个读数起归哪个租户。
交接读数是共享边界:上一任的最后一段账单以它为终点,新租户以它为起点,
同一行读数既结上段也开下段,不重复计量。
"""


def _meter_readings(conn, meter_id: int):
    """按时间序返回一块表的全部读数行(含基数)。"""
    return conn.execute(
        "SELECT * FROM readings WHERE meter_id = ? ORDER BY reading_date, id",
        (meter_id,),
    ).fetchall()


def _windows_for_meter(conn, meter_id: int):
    """一块表的租期窗口:[(household_id, 边界读数行, 下一边界读数行或 None)]。"""
    takeovers = conn.execute(
        "SELECT * FROM takeovers WHERE meter_id = ? ORDER BY start_date, id",
        (meter_id,),
    ).fetchall()
    if not takeovers:
        return []
    rows = _meter_readings(conn, meter_id)
    by_id = {r["id"]: r for r in rows}

    def resolve(t):
        if t["reading_id"]:
            row = by_id.get(t["reading_id"])
            if row:
                return row
        cand = [r for r in rows if r["reading_date"] == t["start_date"]]
        return max(cand, key=lambda r: r["id"]) if cand else None

    windows = []
    for i, t in enumerate(takeovers):
        boundary = resolve(t)
        if boundary is None:
            continue
        nxt = takeovers[i + 1] if i + 1 < len(takeovers) else None
        next_boundary = resolve(nxt) if nxt else None
        windows.append((t["household_id"], boundary, next_boundary))
    return windows


def _takeover_windows(conn, household_id: int = None, meter_type: str = None):
    """带过滤的租期窗口(按租户/类型筛选)。"""
    if household_id is not None:
        meter_ids = [r["meter_id"] for r in conn.execute(
            "SELECT DISTINCT meter_id FROM takeovers WHERE household_id = ?",
            (household_id,),
        ).fetchall()]
    else:
        meter_ids = [r["id"] for r in conn.execute("SELECT id FROM meters").fetchall()]
    windows = []
    for mid in meter_ids:
        if meter_type:
            m = conn.execute("SELECT meter_type FROM meters WHERE id=?", (mid,)).fetchone()
            if not m or m["meter_type"] != meter_type:
                continue
        for hh_id, boundary, next_boundary in _windows_for_meter(conn, mid):
            if household_id is not None and hh_id != household_id:
                continue
            windows.append((hh_id, boundary, next_boundary))
    return windows


def household_period_usage(conn, household_id: int, meter_type: str) -> list[dict]:
    """某租户在某类型(水/电)下的全部账单期,按租期分段计算。"""
    result = []
    for hh_id, boundary, next_boundary in _takeover_windows(
            conn, household_id=household_id, meter_type=meter_type):
        meter = conn.execute(
            "SELECT * FROM meters WHERE id = ?", (boundary["meter_id"],)).fetchone()
        # 总表只用于部门对账,不产生租户账单(ADR-0008/0010)
        if not meter or meter["level"] == "main":
            continue
        window = [r for r in _meter_readings(conn, meter["id"]) if r["id"] > boundary["id"]]
        if next_boundary is not None:
            window = [r for r in window if r["id"] <= next_boundary["id"]]
        prev = boundary
        for cur in window:
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
            prev = cur
    result.sort(key=lambda r: (r["end"], r["reading_id"]))
    return result


def all_statistics(conn, meter_type: str) -> list[dict]:
    rows = []
    for h in conn.execute("SELECT * FROM households ORDER BY id"):
        for p in household_period_usage(conn, h["id"], meter_type):
            rows.append({**p, "household_id": h["id"], "household_name": h["name"]})
    rows.sort(key=lambda r: (r["end"], r["household_id"]))
    return rows
