"""示例数据:用于验证计费逻辑是否正常,可在初始化页面一键填充或清空。"""

from .db import connect

# 三位租户、四块分表(其中李四只有水表、没有电表),两块总表
HOUSEHOLDS = ["张三", "李四", "王五"]

# (表名, 类型, 级别, 所属总表, 租户)
METERS = [
    ("总水表", "water", "main", None, None),
    ("总电表", "electricity", "main", None, None),
    ("101水表", "water", "sub", "总水表", "张三"),
    ("101电表", "electricity", "sub", "总电表", "张三"),
    ("102水表", "water", "sub", "总水表", "李四"),
    ("103水表", "water", "sub", "总水表", "王五"),
]

# (表名, 日期, 读数, 部门单价, 租户单价)
BASES = [
    ("总水表", "2025-01-01", 5000, 4.20, 5.00),
    ("总电表", "2025-01-01", 20000, 0.85, 1.00),
    ("101水表", "2025-01-01", 200, 4.20, 5.00),
    ("101电表", "2025-01-01", 1000, 0.85, 1.00),
    ("102水表", "2025-01-01", 150, 4.20, 5.00),
    ("103水表", "2025-01-01", 80, 4.20, 5.00),
]

# (表名, 日期, 读数, 部门单价, 租户单价)  —— 单价在 3 月调整过一次
READINGS = [
    ("总水表", "2025-02-01", 5120, 4.20, 5.00),
    ("总电表", "2025-02-01", 20400, 0.85, 1.00),
    ("101水表", "2025-02-01", 232, 4.20, 5.00),
    ("101电表", "2025-02-01", 1120, 0.85, 1.00),
    ("102水表", "2025-02-01", 176, 4.20, 5.00),
    ("103水表", "2025-02-01", 98, 4.20, 5.00),
    ("总水表", "2025-03-01", 5250, 4.50, 5.50),
    ("总电表", "2025-03-01", 20850, 0.90, 1.10),
    ("101水表", "2025-03-01", 268, 4.50, 5.50),
    ("101电表", "2025-03-01", 1260, 0.90, 1.10),
    ("102水表", "2025-03-01", 205, 4.50, 5.50),
    ("103水表", "2025-03-01", 118, 4.50, 5.50),
]

# (总表, 缴费日期, 金额, 账期起, 账期止, 备注)
PAYMENTS = [
    ("总水表", "2025-02-05", 504.00, "2025-01-01", "2025-02-01", "1 月水费"),
    ("总电表", "2025-02-05", 340.00, "2025-01-01", "2025-02-01", "1 月电费"),
    ("总水表", "2025-03-06", 585.00, "2025-02-01", "2025-03-01", "2 月水费"),
    ("总电表", "2025-03-06", 405.00, "2025-02-01", "2025-03-01", "2 月电费"),
]


def clear_all(conn) -> None:
    for table in ("receipts", "receivables", "adjustments", "payments", "takeovers",
                  "meter_switches", "readings",
                  "meters", "households", "settings"):
        conn.execute(f"DELETE FROM {table}")


def load_sample(conn) -> None:
    """清空后写入一套可核对的示例数据。"""
    clear_all(conn)
    conn.execute("INSERT INTO settings(key,value) VALUES ('property_name',?)",
                 ("示例:城南小区 3 栋",))

    for name in HOUSEHOLDS:
        conn.execute("INSERT INTO households(name) VALUES (?)", (name,))
    hh = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM households")}

    ids: dict[str, int] = {}
    for name, mtype, level, main, household in METERS:
        cur = conn.execute(
            "INSERT INTO meters(name,meter_type,level,main_meter_id,household_id) VALUES (?,?,?,?,?)",
            (name, mtype, level, ids.get(main) if main else None,
             hh.get(household) if household else None),
        )
        ids[name] = cur.lastrowid

    for name, date, value, dp, tp in BASES:
        rc = conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,department_price,tenant_price,is_initial) "
            "VALUES (?,?,?,?,?,1)",
            (ids[name], date, value, dp, tp),
        )
        hh_id = None
        m = conn.execute("SELECT household_id FROM meters WHERE id=?", (ids[name],)).fetchone()
        hh_id = m["household_id"] if m else None
        if hh_id:
            conn.execute(
                "INSERT INTO takeovers(meter_id,household_id,start_date,start_value,reading_id,note) "
                "VALUES (?,?,?,?,?,?)",
                (ids[name], hh_id, date, value, rc.lastrowid, "示例数据"))

    for name, date, value, dp, tp in READINGS:
        conn.execute(
            "INSERT INTO readings(meter_id,reading_date,value,department_price,tenant_price) "
            "VALUES (?,?,?,?,?)",
            (ids[name], date, value, dp, tp),
        )

    # 1 月的两期抄表视为已出账,2 月留作待出账,方便观察两种状态
    conn.execute(
        "UPDATE readings SET billed=1 WHERE is_initial=0 AND reading_date < '2025-03-01'"
    )

    for meter, date, amount, start, end, note in PAYMENTS:
        conn.execute(
            "INSERT INTO payments(meter_id,payment_date,amount,period_start,period_end,note) "
            "VALUES (?,?,?,?,?,?)",
            (ids[meter], date, amount, start, end, note),
        )

    # 一条冲正:张三 2 月水表抄错,补收 25 元
    rid = conn.execute(
        "SELECT id FROM readings WHERE meter_id=? AND reading_date='2025-03-01'",
        (ids["101水表"],),
    ).fetchone()
    if rid:
        conn.execute(
            "INSERT INTO adjustments(reading_id,household_id,amount,adjust_date,note) "
            "VALUES (?,?,?,?,?)",
            (rid["id"], hh["张三"], 25.00, "2025-03-08", "水表抄错,补收 5 吨水费"),
        )
