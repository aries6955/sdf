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
    "103电表": ("2025-01-01", 300, (100, 100, 100)),
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
    """清空后写入一套可核对的示例数据。全部经应用层的真实写入路径,不走平行实现。"""
    from . import readings, tenancy

    clear_all(conn)
    conn.execute("INSERT INTO settings(key,value) VALUES ('property_name',?)", (PROPERTY,))

    for name in ("张三", "李四", "王五"):
        conn.execute("INSERT INTO households(name) VALUES (?)", (name,))
    hh = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM households")}

    ids: dict[str, int] = {}
    for name, mtype, level, main, phase in METERS:
        b = BASES.get(name)
        parts = b[2] if b and len(b) > 2 else (None, None, None)
        cur = conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,phase,base_value,"
            "base_r1,base_r2,base_r3,installed_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, mtype, level, ids.get(main) if main else None, phase,
             b[1] if b else None, *parts, b[0] if b else "2025-01-01"))
        ids[name] = cur.lastrowid

    lines: dict[str, int] = {}
    for hh_name, move_in, seats in TAKEOVERS:
        for meter_name, value, price in seats:
            lines[meter_name] = tenancy.move_in(
                conn, household_id=hh[hh_name], start_date=move_in,
                seats=[(_meter(conn, ids, meter_name), _split(value), price, "")])[0]

    for meter_name, d, value, price in READINGS:
        readings.insert(conn, kind="periodic", line_id=lines[meter_name],
                        meter_id=ids[meter_name], reading_date=d, parts=_split(value),
                        price=price)

    # 换表:张三的 101 水表 → 101备用水表,2025-03-15。账期不切断。
    lines["101备用水表"] = lines["101水表"]
    tenancy.swap_meter(conn, old_meter=_meter(conn, ids, "101水表"),
                       new_meter=_meter(conn, ids, "101备用水表"),
                       switch_date="2025-03-15",
                       old_parts=_split(280), new_parts=_split(0), note="示例换表")
    readings.insert(conn, kind="periodic", line_id=lines["101备用水表"],
                    meter_id=ids["101备用水表"], reading_date="2025-04-01",
                    parts=_split(35), price=5.50)

    # 王五 2025-04-01 退租
    _check_out(conn, ids, [("103水表", _split(130)),
                                       ("103电表", (405, 135, 135, 135))], "2025-04-01")

    # 李四 2025-04-10 入住 103 房;水表 145(空档 15 归房东),没有电表
    tenancy.move_in(conn, household_id=hh["李四"], start_date="2025-04-10",
                    seats=[(_meter(conn, ids, "103水表"), _split(145), 5.50, "")])

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


def _meter(conn, ids, name):
    return conn.execute("SELECT * FROM meters WHERE id=?", (ids[name],)).fetchone()


def _split(value) -> tuple:
    """示例表里的读数写法折成 (合计, r1, r2, r3)。三相写成元组。"""
    if isinstance(value, tuple):
        return sum(value), value[0], value[1], value[2]
    return value, None, None, None


def _check_out(conn, ids, outs, d):
    """示例专用:一次退掉该租户的多条线。outs 为 [(表名, parts)]。"""
    from . import tenancy
    from .reading_index import last_of_line

    seats = []
    for meter_name, parts in outs:
        meter = _meter(conn, ids, meter_name)
        line = conn.execute("SELECT * FROM lines WHERE id=?", (meter["line_id"],)).fetchone()
        price = last_of_line(conn, line["id"])["tenant_price"]
        seats.append((line, meter["id"], parts, price))
    tenancy.check_out(conn, out_date=d, outs=seats)
