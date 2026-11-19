import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data.sqlite3"


# 模型要点见 GLOSSARY.md 与 docs/adr/0012..0023。
#
# 一条「计量线」是一段连续的计量关系:某个租户、某一类水电、从入户读数到退租读数。
# 它是归属的唯一真源 —— meters 上没有租户字段,「现在归谁」一律从 lines 的开放行推导。
# 换表不切断计量线:旧表终读数与新表基数各记一条 reading,用量按表分段累加。
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

-- 计量线 = 租期。move_out_date 为空即「在用」;同一块表同一时间只有一条开放线。
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    meter_type TEXT NOT NULL CHECK (meter_type IN ('water', 'electricity')),
    label TEXT NOT NULL DEFAULT '',
    move_in_date TEXT NOT NULL,
    move_out_date TEXT,
    move_in_value INTEGER NOT NULL,
    move_out_value INTEGER,
    note TEXT NOT NULL DEFAULT ''
);

-- 表:总表(base 为空)与分表(装表时录一次 base)。retired_on 只由换表写入。
CREATE TABLE IF NOT EXISTS meters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    meter_type TEXT NOT NULL CHECK (meter_type IN ('water', 'electricity')),
    level TEXT NOT NULL CHECK (level IN ('main', 'sub')),
    main_meter_id INTEGER REFERENCES meters(id),
    phase TEXT NOT NULL DEFAULT 'single' CHECK (phase IN ('single', 'three')),
    base_value INTEGER,
    installed_on TEXT,      -- 装表日期;装表读数(base_value)当天的日期
    retired_on TEXT,
    line_id INTEGER REFERENCES lines(id),
    note TEXT NOT NULL DEFAULT ''
);

-- 一块表同一时间只服务一条计量线(换下的表 line_id 为空,保留历史)
CREATE UNIQUE INDEX IF NOT EXISTS idx_meters_one_line
    ON meters(line_id) WHERE line_id IS NOT NULL;

-- 读数挂在计量线上,另记取自哪块表。
-- kind: 'move_in' 入户 | 'periodic' 抄表 | 'move_out' 退租 | 'switch_out' 旧表终读数 | 'switch_base' 新表基数
-- 前三种是账期边界,后两种是换表内部读数,只参与分段累加。
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL REFERENCES lines(id),
    meter_id INTEGER NOT NULL REFERENCES meters(id),
    reading_date TEXT NOT NULL,
    value INTEGER NOT NULL,
    r1 INTEGER,
    r2 INTEGER,
    r3 INTEGER,
    tenant_price REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'periodic'
        CHECK (kind IN ('move_in', 'periodic', 'move_out', 'switch_out', 'switch_base')),
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_readings_line ON readings(line_id, reading_date, id);
CREATE INDEX IF NOT EXISTS idx_readings_meter ON readings(meter_id, reading_date, id);

-- 应收:一个账期一条,抄表当场生成。
CREATE TABLE IF NOT EXISTS receivables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id INTEGER NOT NULL REFERENCES lines(id),
    household_id INTEGER NOT NULL REFERENCES households(id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    usage_amount INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receivables_hh ON receivables(household_id, period_end, id);

-- 实收 / 退款:记在租户名下,不指定账单。冲抵按最早欠账优先自动推导。
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id),
    entry_date TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'receipt' CHECK (kind IN ('receipt', 'refund')),
    note TEXT NOT NULL DEFAULT ''
);

-- 缴费:照部门账单手填,系统不推算金额。
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id INTEGER NOT NULL REFERENCES meters(id),
    payment_date TEXT NOT NULL,
    reading_value INTEGER,
    unit_price REAL,
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meter_switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_meter_id INTEGER NOT NULL REFERENCES meters(id),
    new_meter_id INTEGER NOT NULL REFERENCES meters(id),
    switch_date TEXT NOT NULL,
    old_final_value INTEGER NOT NULL,
    new_base_value INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
