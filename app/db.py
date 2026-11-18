import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data.sqlite3"


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

CREATE TABLE IF NOT EXISTS meters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    meter_type TEXT NOT NULL CHECK (meter_type IN ('water', 'electricity')),
    level TEXT NOT NULL CHECK (level IN ('main', 'sub')),
    main_meter_id INTEGER REFERENCES meters(id),
    household_id INTEGER REFERENCES households(id),
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id INTEGER NOT NULL REFERENCES meters(id),
    reading_date TEXT NOT NULL,
    value INTEGER NOT NULL,
    department_price REAL NOT NULL DEFAULT 0,
    tenant_price REAL NOT NULL DEFAULT 0,
    billed INTEGER NOT NULL DEFAULT 0,
    is_initial INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_readings_meter_date ON readings(meter_id, reading_date, id);

CREATE TABLE IF NOT EXISTS meter_switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_meter_id INTEGER NOT NULL REFERENCES meters(id),
    new_meter_id INTEGER NOT NULL REFERENCES meters(id),
    switch_date TEXT NOT NULL,
    old_final_value INTEGER NOT NULL,
    new_base_value INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id INTEGER NOT NULL REFERENCES meters(id),
    payment_date TEXT NOT NULL,
    amount REAL NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0,
    reading_value INTEGER,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    household_id INTEGER NOT NULL REFERENCES households(id),
    amount REAL NOT NULL,
    adjust_date TEXT NOT NULL,
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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧数据库补上新增的列(幂等)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(payments)")}
    if "unit_price" not in cols:
        conn.execute("ALTER TABLE payments ADD COLUMN unit_price REAL NOT NULL DEFAULT 0")
    if "reading_value" not in cols:
        conn.execute("ALTER TABLE payments ADD COLUMN reading_value INTEGER")


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
