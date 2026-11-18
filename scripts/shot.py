"""用独立 Edge 实例截图,用完只杀自己启动的那个进程树。

绝不按进程名杀 msedge.exe —— 那会连用户正在用的浏览器一起关掉。
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def screenshot(url: str, out: Path, size: str = "1280,900", wait: float = 2.0) -> bool:
    """用一次性 Edge 实例给 url 截图,只结束本函数启动的进程。"""
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    # 独立 user-data-dir:与用户正在用的浏览器完全隔离,
    # 且避免复用已有实例(否则 --screenshot 不会生效)
    with tempfile.TemporaryDirectory(prefix="sdf-shot-") as profile:
        proc = subprocess.Popen(
            [
                EDGE,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={profile}",
                f"--window-size={size}",
                f"--virtual-time-budget={int(wait * 1000)}",
                f"--screenshot={out}",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        return out.exists()


if __name__ == "__main__":
    import sys

    url = sys.argv[1]
    out = Path(sys.argv[2])
    ok = screenshot(url, out)
    print("ok" if ok else "failed", out)
