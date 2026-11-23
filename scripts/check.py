"""仓库唯一的检查命令:静态检查 + 测试,任一项失败即非零退出。

用法:
    uv run python scripts/check.py

除 ruff / node --check / pytest 外,还有两条本仓库特有的守护规则:
设计契约(字号与颜色必须走 CSS 变量)与模板内联脚本的语法。
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "app/static/style.css"
TEMPLATES = ROOT / "app/templates"


def run(args: list[str]) -> int:
    print("$ " + " ".join(args), flush=True)
    return subprocess.call(args)


def check_design_tokens() -> int:
    """style.css 里的字号与颜色必须走 DESIGN.md 的 token。

    UI 变乱的根源是散落的 ``font-size: 13.7px`` 与随手写的 ``color: #94a3b8``。
    这里只挡"硬编码",不判断选哪一档 —— 那一档由 DESIGN.md 决定。
    图标字形用 ``/* 图标`` 标注豁免。
    """
    bad = []
    for no, line in enumerate(CSS.read_text(encoding="utf-8").splitlines(), 1):
        if "/* 图标" in line or line.lstrip().startswith("*"):
            continue
        if re.search(r"font-size:\s*[0-9.]+px", line):
            bad.append(f"{no}: {line.strip()}")
        if re.search(r"^\s*color:\s*(#[0-9a-fA-F]{3,8}|rgb)", line):
            bad.append(f"{no}: {line.strip()}")
    if bad:
        print("设计契约:字号/颜色写死了,请改用 --fs-* 与 --ink/--muted 等变量:", file=sys.stderr)
        for line in bad:
            print("  " + line, file=sys.stderr)
        return 1
    return 0


def check_inline_scripts() -> int:
    """模板里的内联 <script> 也要过 node --check。

    static/*.js 已由 check.py 覆盖,但页面里的脚本(确认卡片、弹窗、筛选联动)
    目前完全没验过语法 —— Jinja 拼接出的 JS 一旦写坏,只能靠手动点页面才发现。
    """
    if not TEMPLATES.is_dir():
        return 0
    failed = []
    for tpl in sorted(TEMPLATES.glob("*.html")):
        html = tpl.read_text(encoding="utf-8")
        for i, m in enumerate(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                          html, re.S)):
            body = m.group(1).strip()
            if not body:
                continue
            # Jinja 表达式换成占位符,否则不是合法 JS
            body = re.sub(r"\{\{.*?\}\}", "0", body, flags=re.S)
            body = re.sub(r"\{%.*?%\}", "", body, flags=re.S)
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                             delete=False) as fh:
                fh.write(body)
                path = fh.name
            code = subprocess.call(["node", "--check", path],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            Path(path).unlink(missing_ok=True)
            if code != 0:
                failed.append(f"{tpl.name} 第 {i + 1} 段 <script>")
    if failed:
        print("模板内联脚本语法错误:", file=sys.stderr)
        for f in failed:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


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

    for name, fn in (("设计契约", check_design_tokens),
                     ("模板内联脚本", check_inline_scripts)):
        code = fn()
        if code != 0:
            print(f"\n失败:{name}", file=sys.stderr)
            return code

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
