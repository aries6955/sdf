"""示例数据:用于验证计费逻辑,可在初始化页面一键填充或清空。

覆盖:多租户、同一租户两块水表、三相电表、一次换表(账期不切断)、
一次退租与随之出现的空档期、收款先欠先冲与退款。
"""

PROPERTY = "示例:城南小区 3 栋"

# 表(名称, 类型, 级别, 所属总表, 相数)
METERS = [
    ("总水表", "water", "main", None, "single"),
    ("总电表", "electricity", "main", None, "single"),
    ("101水表", "water", "sub", "总水表", "single"),
    ("101备用水表", "water", "sub", "总水表", "single"),   # 备用,换表时接上
    ("101电表", "electricity", "sub", "总电表", "single"),
    ("102水表", "water", "sub", "总水表", "single"),
    ("103水表", "water", "sub", "总水表", "single"),
    ("103电表", "electricity", "sub", "总电表", "three"),   # 三相
]

# 分表的装表读数(总表不设基数)
BASES = {
    "101水表": ("2025-01-01", 200),
    "101备用水表": ("2025-01-01", 0),
    "101电表": ("2025-01-01", 1000),
    "102水表": ("2025-01-01", 150),
    "103水表": ("2025-01-01", 80),
    "103电表": ("2025-01-01", 300),
}

# (租户, 入住日期, [(表, 读数, 单价)])  三相表读数为元组
TAKEOVERS = [
    ("张三", "2025-01-01", [("101水表", 200, 5.00), ("101电表", 1000, 1.00)]),
    ("李四", "2025-01-01", [("102水表", 150, 5.00)]),
    ("王五", "2025-01-01", [("103水表", 80, 5.00), ("103电表", (100, 100, 100), 1.00)]),
]

# (表, 日期, 读数, 单价)  三相表读数为元组
READINGS = [
    ("101水表", "2025-02-01", 232, 5.00),
    ("101电表", "2025-02-01", 1120, 1.00),
    ("102水表", "2025-02-01", 176, 5.00),
    ("103水表", "2025-02-01", 98, 5.00),
    ("103电表", "2025-02-01", (110, 110, 110), 1.00),
    ("101水表", "2025-03-01", 268, 5.50),
    ("101电表", "2025-03-01", 1260, 1.10),
    ("102水表", "2025-03-01", 205, 5.50),
    ("103水表", "2025-03-01", 118, 5.50),
    ("103电表", "2025-03-01", (122, 122, 122), 1.10),
]

# (总表, 缴费日期, 账单读数, 账单单价, 金额, 备注)
PAYMENTS = [
    ("总水表", "2025-02-05", 5120, 4.20, 504.00, "1 月水费"),
    ("总电表", "2025-02-05", 20400, 0.85, 340.00, "1 月电费"),
    ("总水表", "2025-03-06", 5250, 4.50, 585.30, "2 月水费"),
    ("总电表", "2025-03-06", 20850, 0.90, 405.00, "2 月电费"),
]


def clear_all(conn) -> None:
    """清空全部业务数据,并重置自增计数。"""
    tables = ("receipts", "receivables", "payments", "readings", "meter_switches",
              "meters", "lines", "households", "settings")
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
    has_seq = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone()
    if has_seq:
        marks = ",".join("?" * len(tables))
        conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({marks})", tables)


def load_sample(conn) -> None:
    """清空后写入一套可核对的示例数据。"""
    from .billing import record_period

    clear_all(conn)
    conn.execute("INSERT INTO settings(key,value) VALUES ('property_name',?)", (PROPERTY,))

    for name in ("张三", "李四", "王五"):
        conn.execute("INSERT INTO households(name) VALUES (?)", (name,))
    hh = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM households")}

    ids: dict[str, int] = {}
    for name, mtype, level, main, phase in METERS:
        b = BASES.get(name)
        cur = conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,base_date,"
            "installed_on) VALUES (?,?,?,?,?,?,?,?)",
            (name, mtype, level, ids.get(main) if main else None, phase,
             b[1] if b else None, b[0] if b else None, b[0] if b else "2025-01-01"))
        ids[name] = cur.lastrowid

    lines: dict[str, int] = {}
    for hh_name, move_in, seats in TAKEOVERS:
        for meter_name, value, price in seats:
            lines[meter_name] = _open_line(conn, hh[hh_name], meter_name, move_in, value, price,
                                           ids)

    for meter_name, d, value, price in READINGS:
        _periodic(conn, lines[meter_name], meter_name, d, value, price, ids, record_period)

    # 换表:张三的 101 水表 → 101备用水表,2025-03-15。账期不切断。
    lines["101备用水表"] = lines["101水表"]
    _switch(conn, ids, "101水表", "101备用水表", "2025-03-15", 280, 0)
    _periodic(conn, lines["101备用水表"], "101备用水表", "2025-04-01", 35, 5.50, ids,
              record_period)

    # 王五 2025-04-01 退租
    _checkout(conn, ids, "103水表", "2025-04-01", 130)
    _checkout(conn, ids, "103电表", "2025-04-01", (135, 135, 135))

    # 李四 2025-04-10 入住 103 房;水表 145(空档 15 归房东),没有电表
    _open_line(conn, hh["李四"], "103水表", "2025-04-10", 145, 5.50, ids)

    for meter, d, rv, up, amount, note in PAYMENTS:
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,reading_value,unit_price,amount,note) "
            "VALUES (?,?,?,?,?,?)", (ids[meter], d, rv, up, amount, note))

    # 张三先交 200 元,再退 20 元(演示先欠先冲与退款)
    conn.execute(
        "INSERT INTO receipts(household_id,entry_date,amount,kind,note) VALUES (?,?,?,?,?)",
        (hh["张三"], "2025-03-10", 200.00, "receipt", "微信转账"))
    conn.execute(
        "INSERT INTO receipts(household_id,entry_date,amount,kind,note) VALUES (?,?,?,?,?)",
        (hh["张三"], "2025-03-20", 20.00, "refund", "抹零"))


def _type_of(meter_name: str) -> str:
    return "electricity" if "电" in meter_name else "water"


def _split(value):
    if isinstance(value, tuple):
        return sum(value), value[0], value[1], value[2]
    return value, None, None, None


def _open_line(conn, hh_id, meter_name, move_in, value, price, ids) -> int:
    total, r1, r2, r3 = _split(value)
    line = conn.execute(
        "INSERT INTO lines(household_id,meter_type,label,move_in_date,move_in_value)"
        " VALUES (?,?,?,?,?)", (hh_id, _type_of(meter_name), "", move_in, total)).lastrowid
    conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line, ids[meter_name]))
    conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,kind,"
        "note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (line, ids[meter_name], move_in, total, r1, r2, r3, price, "move_in", "入住读数"))
    return line


def _periodic(conn, line_id, meter_name, d, value, price, ids, record):
    total, r1, r2, r3 = _split(value)
    cur = conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,kind)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (line_id, ids[meter_name], d, total, r1, r2, r3, price, "periodic"))
    record(conn, line_id, cur.lastrowid)


def _switch(conn, ids, old_name, new_name, d, old_final, new_base):
    from .billing import line_readings
    old_id, new_id = ids[old_name], ids[new_name]
    old_m = conn.execute("SELECT * FROM meters WHERE id=?", (old_id,)).fetchone()
    line_id = old_m["line_id"]
    rows = line_readings(conn, line_id)
    price = rows[-1]["tenant_price"] if rows else 0.0
    conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,tenant_price,kind,note)"
        " VALUES (?,?,?,?,?,?,?)",
        (line_id, old_id, d, old_final, price, "switch_out", "旧表终读数"))
    conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,tenant_price,kind,note)"
        " VALUES (?,?,?,?,?,?,?)",
        (line_id, new_id, d, new_base, price, "switch_base", "新表基数"))
    conn.execute("UPDATE meters SET line_id=NULL, retired_on=? WHERE id=?", (d, old_id))
    conn.execute("UPDATE meters SET line_id=? WHERE id=?", (line_id, new_id))
    conn.execute(
        "INSERT INTO meter_switches(old_meter_id,new_meter_id,switch_date,old_final_value,"
        "new_base_value,note) VALUES (?,?,?,?,?,?)",
        (old_id, new_id, d, old_final, new_base, "示例换表"))


def _checkout(conn, ids, meter_name, d, value):
    from .billing import record_period
    meter = conn.execute("SELECT * FROM meters WHERE id=?", (ids[meter_name],)).fetchone()
    line_id = meter["line_id"]
    total, r1, r2, r3 = _split(value)
    rows = conn.execute("SELECT tenant_price FROM readings WHERE line_id=? "
                        "ORDER BY reading_date DESC, id DESC LIMIT 1", (line_id,)).fetchone()
    price = rows["tenant_price"] if rows else 0
    cur = conn.execute(
        "INSERT INTO readings(line_id,meter_id,reading_date,value,r1,r2,r3,tenant_price,kind,"
        "note) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (line_id, meter["id"], d, total, r1, r2, r3, price, "move_out", "退租读数"))
    record_period(conn, line_id, cur.lastrowid)
    conn.execute("UPDATE lines SET move_out_date=?, move_out_value=? WHERE id=?",
                 (d, total, line_id))
    conn.execute("UPDATE meters SET line_id=NULL WHERE line_id=?", (line_id,))
