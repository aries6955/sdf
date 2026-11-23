"""数据库备份与恢复:清空前自动留一份,也可手动备份/恢复。

备份落在项目 backups/ 目录,用 SQLite 在线备份 API 复制整个库(含 settings 与自增计数),
恢复即整库覆盖 —— 会丢掉恢复之后录入的数据,所以前端必须二次确认。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from . import db
from .db import connect

AUTO_NAME = "清空前自动备份.sqlite3"


def backup_dir() -> Path:
    """备份目录跟着数据库走 —— 与 DB_PATH 一起被测试替换。"""
    return db.DB_PATH.parent / "backups"


def backups() -> list[dict]:
    """按时间倒序列出备份文件。"""
    backup_dir().mkdir(parents=True, exist_ok=True)
    rows = []
    for p in sorted(backup_dir().glob("*.sqlite3"), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        rows.append({"name": p.name, "size": p.stat().st_size,
                     "time": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return rows


def create(name: str | None = None) -> str:
    """复制当前库到备份目录,返回备份文件名。"""
    backup_dir().mkdir(parents=True, exist_ok=True)
    name = name or datetime.now().strftime("备份-%Y%m%d-%H%M%S.sqlite3")
    if not name.endswith(".sqlite3"):
        name += ".sqlite3"
    _check_name(name)
    path = backup_dir() / name
    with connect() as src, sqlite3.connect(path) as dst:
        src.backup(dst)
    return name


def _check_name(name: str) -> None:
    """备份名只能是一段文件名 —— 它直接拼进路径,放 .. 就能跳出 backups/。

    先拦目录分隔符与 ..,再拦落单的奇怪字符(允许中文名与 -_)。
    """
    if Path(name).name != name or ".." in name or "/" in name or "\\" in name:
        raise ValueError("备份名不能带路径")
    if any(not c.isalnum() and c not in "-_" for c in Path(name).stem):
        raise ValueError("备份名只能含中文、字母、数字、- 和 _")


def restore(name: str) -> None:
    """用指定备份整库覆盖当前库。调用方负责确认。

    恢复前先验快照能通过 integrity_check 并留一份当前库 —— 拿损坏文件恢复
    会把主库一起弄坏,且此时已没有回头路。
    """
    _check_name(name)
    path = backup_dir() / name
    if not path.is_file():
        raise FileNotFoundError(name)
    with sqlite3.connect(path) as src:
        ok = src.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"备份文件已损坏:{ok}")
    # 覆盖前留一份当前库。名字必须与待恢复的那份不同:
    # 若复用 AUTO_NAME,正好在恢复「清空前自动备份」时会把自己覆盖成空库。
    create(datetime.now().strftime("恢复前-%Y%m%d-%H%M%S.sqlite3"))
    with sqlite3.connect(path) as src, connect() as dst:
        src.backup(dst)
