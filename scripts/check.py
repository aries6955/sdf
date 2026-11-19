"""仓库唯一的检查命令:静态检查 + 测试,任一项失败即非零退出。

用法:
    uv run python scripts/check.py
"""

import subprocess
import sys


def run(args: list[str]) -> int:
    print("$ " + " ".join(args), flush=True)
    return subprocess.call(args)


def main() -> int:
    steps = [
        ["uv", "run", "ruff", "check", "app", "tests", "scripts"],
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
