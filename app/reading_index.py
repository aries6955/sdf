"""读数查询:一条计量线与一块表的读数序列、以及"此刻读到哪"。

读写两侧共用同一份排序语义 —— 顺序一律是 ``(reading_date, id)``。
换表之后「这块表的上一条」与「这条线的上一条」是两个不同的问题,名字分开,
调用方不必自己拼 ORDER BY,也不必自己决定要不要排除自己。
"""


def readings_on_line(conn, line_id: int):
    """该线的全部读数,按 (日期, id) 升序。账期划界与邻接定位都从这一串出发。"""
    return conn.execute(
        "SELECT * FROM readings WHERE line_id = ? ORDER BY reading_date, id", (line_id,)
    ).fetchall()


def last_of_line(conn, line_id: int):
    """该线最新的读数(跨换表)。没有则 None。"""
    return conn.execute(
        "SELECT * FROM readings WHERE line_id = ? "
        "ORDER BY reading_date DESC, id DESC LIMIT 1", (line_id,)).fetchone()


def last_of_meter(conn, meter_id: int, *, up_to: str = None, exclude_id: int = None):
    """该表最新的读数。up_to 只取不晚于该日期的(含当天),exclude_id 排除指定一条。

    换表后同一块表与这条线的最新读数会分叉,所以要按表取时用这个,不要按线。
    """
    sql = "SELECT * FROM readings WHERE meter_id = ?"
    params = [meter_id]
    if up_to is not None:
        sql += " AND reading_date <= ?"
        params.append(up_to)
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return conn.execute(sql + " ORDER BY reading_date DESC, id DESC LIMIT 1",
                        params).fetchone()


def latest_meter_names(conn, line_ids=None) -> dict:
    """每条线最新读数所在表的表名。列表页逐行取表名时,整页一次查完。

    line_ids: 只查这几条线;不传则查全部。每条线一行返回,条数与传入的一致。
    """
    sql = ("SELECT r.line_id, m.name FROM readings r JOIN meters m ON m.id = r.meter_id "
           "WHERE r.id = (SELECT r2.id FROM readings r2 WHERE r2.line_id = r.line_id "
           "              ORDER BY r2.reading_date DESC, r2.id DESC LIMIT 1)")
    if line_ids is not None:
        line_ids = list(line_ids)
        if not line_ids:
            return {}
        sql += f" AND r.line_id IN ({','.join('?' * len(line_ids))})"
        rows = conn.execute(sql, line_ids).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    return {x["line_id"]: x["name"] for x in rows}


def neighbours(conn, line_id: int, reading_date: str, reading_id: int = None):
    """一条读数的 (前一条, 后一条),按 (日期, id) 共同定位。

    中间补录时也成立。没有前一条时向前取线首(入住读数),因为跨表比较要从租期起点看。
    """
    if reading_id is None:
        prev = _before(conn, line_id, reading_date, None)
        nxt = _after(conn, line_id, reading_date, None)
        if prev is None:
            prev = conn.execute(
                "SELECT * FROM readings WHERE line_id=? ORDER BY reading_date, id LIMIT 1",
                (line_id,)).fetchone()
        return prev, nxt
    return (_before(conn, line_id, reading_date, reading_id),
            _after(conn, line_id, reading_date, reading_id))


def _before(conn, line_id, reading_date, reading_id):
    if reading_id is None:
        sql, params = "reading_date < ?", (line_id, reading_date)
    else:
        sql, params = ("(reading_date < ? OR (reading_date = ? AND id < ?))",
                       (line_id, reading_date, reading_date, reading_id))
    return conn.execute(
        f"SELECT * FROM readings WHERE line_id=? AND {sql} "
        "ORDER BY reading_date DESC, id DESC LIMIT 1", params).fetchone()


def _after(conn, line_id, reading_date, reading_id):
    if reading_id is None:
        sql, params = "reading_date > ?", (line_id, reading_date)
    else:
        sql, params = ("(reading_date > ? OR (reading_date = ? AND id > ?))",
                       (line_id, reading_date, reading_date, reading_id))
    return conn.execute(
        f"SELECT * FROM readings WHERE line_id=? AND {sql} "
        "ORDER BY reading_date, id LIMIT 1", params).fetchone()


def predecessors(conn, rows) -> dict:
    """一组读数的「同一块表上的上一条读数」,按 id 索引,一次查完。

    列表页有几十行时不能一行查一遍。上一条必须是同一块表上的 —— 换表后的旧表读数
    与新表读数相减会得出一个巨大的负数。
    """
    meter_ids = {r["meter_id"] for r in rows}
    if not meter_ids:
        return {}
    seq = conn.execute(
        f"SELECT * FROM readings WHERE meter_id IN ({','.join('?' * len(meter_ids))}) "
        "ORDER BY meter_id, reading_date, id", list(meter_ids)).fetchall()
    prev_of = {}
    last = {}
    for r in seq:
        key = r["meter_id"]
        prev_of[r["id"]] = last.get(key)
        last[key] = r
    return {r["id"]: prev_of.get(r["id"]) for r in rows}
