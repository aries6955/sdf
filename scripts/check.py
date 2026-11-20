"""仓库唯一的检查命令:静态检查 + 测试,任一项失败即非零退出。

用法:
    uv run python scripts/check.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args: list[str]) -> int:
    print("$ " + " ".join(args), flush=True)
    return subprocess.call(args)


def main() -> int:
    js = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "app/static").glob("*.js"))
    steps = [
        ["uv", "run", "ruff", "check", "app", "tests", "scripts"],
        # 模板里的内联脚本验不了,但 static/*.js 能手写错 —— node --check 零依赖
        *[["node", "--check", f] for f in js],
        ["uv", "run", "pytest", "-q"],
    ]
    for step in steps:
        code = run(step)
        if code != 0:
            print(f"\n失败:{' '.join(step)}", file=sys.stderr)
            return code
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
