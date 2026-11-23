"""异地备份:配置解析、快照校验与未配置时的静默退出。"""

import sqlite3

import app.db as db
from app import backup, offsite

R2_KEYS = ("SDF_R2_ACCOUNT_ID", "SDF_R2_ACCESS_KEY_ID",
           "SDF_R2_SECRET_ACCESS_KEY", "SDF_R2_BUCKET")


def _clear(monkeypatch):
    for k in R2_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_unconfigured_skips_quietly(monkeypatch, tmp_path, capsys):
    """没配凭据不是错误:cron 不该每天收到报警。"""
    _clear(monkeypatch)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    assert offsite.main() == 0
    assert "跳过异地备份" in capsys.readouterr().err


def test_config_needs_every_key(monkeypatch):
    _clear(monkeypatch)
    assert offsite.config() == {}
    for k in R2_KEYS[:-1]:
        monkeypatch.setenv(k, "x")
    assert offsite.config() == {}
    monkeypatch.setenv("SDF_R2_BUCKET", "sdf-backup")
    assert offsite.config()["bucket"] == "sdf-backup"


def test_rclone_env_points_at_r2(monkeypatch):
    _clear(monkeypatch)
    for k in R2_KEYS:
        monkeypatch.setenv(k, "v-" + k)
    env = offsite.rclone_env(offsite.config())
    assert env["RCLONE_CONFIG_R2_PROVIDER"] == "Cloudflare"
    assert env["RCLONE_CONFIG_R2_ENDPOINT"] == "https://v-SDF_R2_ACCOUNT_ID.r2.cloudflarestorage.com"
    assert env["RCLONE_CONFIG_R2_SECRET_ACCESS_KEY"] == "v-SDF_R2_SECRET_ACCESS_KEY"


def test_verify_rejects_corrupt_snapshot(tmp_path):
    """坏文件必须在上传前被拦下,而不是悄悄传上去。"""
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database")
    try:
        offsite.verify(bad)
    except sqlite3.DatabaseError:
        pass          # integrity_check 读不到页也是拒绝
    else:
        raise AssertionError("坏快照竟然通过了校验")


def test_verify_accepts_created_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    name = backup.create("offsite-test")
    offsite.verify(backup.backup_dir() / name)
