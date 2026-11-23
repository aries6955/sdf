"""异地备份:把当前库的一致快照推到 Cloudflare R2。

rclone 随镜像发布,服务器上不必另装 —— 宿主 cron 只跑
    docker exec sdf python -m app.offsite
凭据走 SDF_R2_* 环境变量,和 SDF_PASSWORD 一样在 docker run 时给。
未配置时直接退出,不影响正常使用。

为什么不是容器内定时:@app 是单进程 uvicorn,没有调度器;把它塞进进程只会
让备份和业务抢同一把写锁。cron 在容器外,失败也能记进系统日志。
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime

from . import backup

KEEP_DAYS = os.environ.get("SDF_KEEP_DAYS", "30")
PREFIX = "sdf"          # R2 里的目录,桶内可与他物共存


def config() -> dict:
    """从环境变量读 R2 凭据;缺任何一项都返回空 dict,由调用方决定怎么办。"""
    keys = ("SDF_R2_ACCOUNT_ID", "SDF_R2_ACCESS_KEY_ID",
            "SDF_R2_SECRET_ACCESS_KEY", "SDF_R2_BUCKET")
    if not all(os.environ.get(k) for k in keys):
        return {}
    return {
        "account": os.environ["SDF_R2_ACCOUNT_ID"],
        "key": os.environ["SDF_R2_ACCESS_KEY_ID"],
        "secret": os.environ["SDF_R2_SECRET_ACCESS_KEY"],
        "bucket": os.environ["SDF_R2_BUCKET"],
    }


def rclone_env(cfg: dict) -> dict:
    """rclone 走环境变量建 remote,不需要 rclone.conf —— 镜像里也就没有可丢的配置文件。"""
    env = dict(os.environ)
    env.update({
        "RCLONE_CONFIG_R2_TYPE": "s3",
        "RCLONE_CONFIG_R2_PROVIDER": "Cloudflare",
        # R2 不支持 ACL;Token 未给建桶权限,所以要跳过 bucket 存在性检查
        "RCLONE_CONFIG_R2_REGION": "auto",
        "RCLONE_CONFIG_R2_NO_CHECK_BUCKET": "true",
        "RCLONE_CONFIG_R2_ACCESS_KEY_ID": cfg["key"],
        "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": cfg["secret"],
        "RCLONE_CONFIG_R2_ENDPOINT": f"https://{cfg['account']}.r2.cloudflarestorage.com",
    })
    return env


def verify(path) -> None:
    """快照必须能自查通过才上传 —— 传上去一个坏文件比没有备份更糟。"""
    with sqlite3.connect(path) as conn:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise ValueError(f"快照损坏:{ok}")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("households", "meters", "readings", "payments")}
    print("校验通过", counts)


def main() -> int:
    cfg = config()
    if not cfg:
        print("未配置 SDF_R2_*,跳过异地备份", file=sys.stderr)
        return 0
    name = backup.create(datetime.now().strftime("offsite-%Y%m%d-%H%M%S"))
    snap = backup.backup_dir() / name
    try:
        verify(snap)
        remote = f"r2:{cfg['bucket']}/{PREFIX}/{name}"
        env = rclone_env(cfg)
        subprocess.run(["rclone", "copyto", str(snap), remote], env=env, check=True)
        subprocess.run(["rclone", "delete", f"r2:{cfg['bucket']}/{PREFIX}",
                        "--min-age", f"{KEEP_DAYS}d"], env=env, check=True)
    finally:
        # 本地不留:否则 /init 页面的备份列表会被每日快照淹没
        snap.unlink(missing_ok=True)
    print(f"已上传 {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
