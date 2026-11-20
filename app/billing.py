"""计费:账期沿「计量线」连续计算。

一条线 = 一个租户、一路水电、从入户读数到退租读数。
线内的账期由读数划界:入户读数开第一条,每次抄表结一条,退租读数结最后一条。

换表不切断账期:旧表终读数(switch_out)与新表基数(switch_base)记在同一条线上,
用量按表分段累加 —— 旧表段 (终读数 − 上次读数) + 新表段 (本次读数 − 新表基数)。
"""

BOUNDARY_KINDS = ("move_in", "periodic", "move_out")


def line_readings(conn, line_id: int):
    return conn.execute(
        "SELECT * FROM readings WHERE line_id = ? ORDER BY reading_date, id", (line_id,)
    ).fetchall()


def get_line(conn, line_id: int):
    return conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()


def open_lines(conn, household_id: int = None):
    sql = ("SELECT l.*, h.name AS household_name FROM lines l "
           "JOIN households h ON l.household_id = h.id WHERE l.move_out_date IS NULL")
    params = []
    if household_id is not None:
        sql += " AND l.household_id = ?"
        params.append(household_id)
    return conn.execute(sql + " ORDER BY l.household_id, l.meter_type, l.id", params).fetchall()



def segment_usage(rows, start_row, end_row) -> int:
    """start_row 之后到 end_row(含)的用量,跨表分段累加。"""
    usage = 0
    cur_meter = start_row["meter_id"]
    cur_value = start_row["value"]
    started = False
    for r in rows:
        if not started:
            if r["id"] == start_row["id"]:
                started = True
            continue
        if r["meter_id"] == cur_meter:
            usage += r["value"] - cur_value
        cur_meter = r["meter_id"]
        cur_value = r["value"]
        if r["id"] == end_row["id"]:
            break
    return usage


def _period(rows, prev, cur) -> dict:
    usage = segment_usage(rows, prev, cur)
    return {
        "line_id": cur["line_id"],
        "reading_id": cur["id"],
        "start": prev["reading_date"],
        "end": cur["reading_date"],
        "usage": usage,
        "unit_price": cur["tenant_price"],
        "amount": round(usage * cur["tenant_price"], 2),
        # 账期两端的表读数,供统计页展示;跨表分段时两者取自不同的表。
        # 三相表同时带出分相,否则只看得到合计,无法核对。
        "start_value": prev["value"],
        "end_value": cur["value"],
        "start_r1": prev["r1"], "start_r2": prev["r2"], "start_r3": prev["r3"],
        "end_r1": cur["r1"], "end_r2": cur["r2"], "end_r3": cur["r3"],
    }


def periods_for_line(conn, line_id: int) -> list[dict]:
    """一条线的全部账期。账期只由边界读数(move_in/periodic/move_out)切分。"""
    rows = line_readings(conn, line_id)
    boundary = [r for r in rows if r["kind"] in BOUNDARY_KINDS]
    return [_period(rows, prev, cur) for prev, cur in zip(boundary, boundary[1:])]


def all_periods(conn, meter_type: str = None, household_id: int = None,
                date_from: str = "", date_to: str = "") -> list[dict]:
    """全部账期,带租户、表、线信息。"""
    sql = ("SELECT l.id AS line_id, l.household_id, l.meter_type, l.label, "
           "h.name AS household_name, "
           "(SELECT COALESCE(NULLIF(l.label,''), m.name) FROM meters m "
           " WHERE m.id = (SELECT r.meter_id FROM readings r WHERE r.line_id = l.id "
           "               ORDER BY r.reading_date DESC, r.id DESC LIMIT 1)) AS display_name, "
           "(SELECT m.name FROM meters m WHERE m.id = (SELECT r.meter_id FROM readings r "
           "  WHERE r.line_id = l.id ORDER BY r.reading_date DESC, r.id DESC LIMIT 1)) AS meter_name "
           "FROM lines l JOIN households h ON l.household_id = h.id")
    where, params = [], []
    if meter_type:
        where.append("l.meter_type = ?")
        params.append(meter_type)
    if household_id is not None:
        where.append("l.household_id = ?")
        params.append(household_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = []
    for line in conn.execute(sql + " ORDER BY l.id", params):
        for p in periods_for_line(conn, line["line_id"]):
            if date_from and p["end"] < date_from:
                continue
            if date_to and p["end"] > date_to:
                continue
            rows.append({**p, "household_id": line["household_id"],
                         "household_name": line["household_name"],
                         "meter_type": line["meter_type"], "label": line["label"],
                         "display_name": line["display_name"] or "",
                         "meter_name": line["meter_name"] or ""})
    rows.sort(key=lambda r: (r["end"], r["line_id"]))
    return rows



def recompute_line(conn, line_id: int) -> None:
    """按当前读数重算该线的全部账期。

    账期是从读数推导出来的,所以任何读数变动(新增、补录到中间、修改、撤回)
    都只是"重算一遍",不需要单独的增删逻辑。已收的款项按账期先后重新冲抵。
    """
    line = get_line(conn, line_id)
    if line is None:
        return
    periods = periods_for_line(conn, line_id)
    keep = [p["end"] for p in periods]
    for p in periods:
        exists = conn.execute(
            "SELECT id FROM receivables WHERE line_id=? AND period_end=?",
            (line_id, p["end"])).fetchone()
        if exists:
            conn.execute(
                "UPDATE receivables SET household_id=?, period_start=?, usage_amount=?, "
                "unit_price=?, amount=? WHERE id=?",
                (line["household_id"], p["start"], p["usage"], p["unit_price"], p["amount"],
                 exists["id"]))
        else:
            conn.execute(
                "INSERT INTO receivables(line_id, household_id, period_start, period_end, "
                "usage_amount, unit_price, amount, created_at) "
                "VALUES (?,?,?,?,?,?,?,date('now'))",
                (line_id, line["household_id"], p["start"], p["end"], p["usage"],
                 p["unit_price"], p["amount"]))
    if keep:
        marks = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM receivables WHERE line_id=? AND period_end NOT IN ({marks})",
            [line_id, *keep])
    else:
        conn.execute("DELETE FROM receivables WHERE line_id=?", (line_id,))


def vacancy_usage(conn, date_from: str = "", date_to: str = "") -> list[dict]:
    """空档期用量:同一块表上,两任租户之间无人使用的区间。归房东,不收费。"""
    rows = []
    for m in conn.execute(
            "SELECT id, name FROM meters WHERE level='sub' ORDER BY meter_type, id"):
        seq = conn.execute(
            "SELECT * FROM readings WHERE meter_id=? ORDER BY reading_date, id",
            (m["id"],)).fetchall()
        for prev, cur in zip(seq, seq[1:]):
            if cur["kind"] != "move_in":
                continue
            if prev["line_id"] == cur["line_id"]:
                continue
            gap = cur["value"] - prev["value"]
            if gap <= 0:
                continue
            if date_from and cur["reading_date"] < date_from:
                continue
            if date_to and cur["reading_date"] > date_to:
                continue
            rows.append({"meter_name": m["name"], "start": prev["reading_date"],
                         "end": cur["reading_date"], "usage": gap})
    return rows
