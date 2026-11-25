"""注释与弃置字符串检查(dune-init 唯一实现)。

分类与 dune-init 删除时相同:
- 可删注释:tokenize 的 COMMENT,排除首行 shebang、第 1/2 行编码声明、
  工具指令(# noqa / # type:,删掉会改变 lint 结果);
- 单独成句且结果被丢弃的字符串:ast 的 Expr 语句(含模块/类/函数文档字符串)。

本仓库是 git 项目,--diff 用 git diff 的新增行 + 未跟踪源文件判定新命中;
非 git 项目改与 .dune-baseline.json 比对,脚本运行时自行判断,钩子无需改动。

用法:
    python scripts/check_comments.py              # 全量扫描(存量命中一律报)
    python scripts/check_comments.py --diff       # 只查新增命中(钩子用这个)
    python scripts/check_comments.py --snapshot   # 非 git:写 .dune-baseline.json
    python scripts/check_comments.py --hook-claude  # Claude Code Stop 钩子适配
"""

import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".dune-baseline.json"
SKIP_DIRS = {".git", ".venv", "__pycache__", ".tmp", "backups", "data",
             "node_modules", ".pytest_cache", ".ruff_cache"}
SOURCE_EXTS = {".py"}
# 检查器自身也是源码,不扫自己,否则全量模式永远过不了
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()
ENCODING_DECL = re.compile(r"^[ \t\f]*#.*?coding[:=]", re.ASCII)
MSG = "禁止用注释解释临时补丁或提醒下一个 Agent,请通过命名、类型或测试表达意图并直接修复根因。"


def is_git() -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except FileNotFoundError:
        return False


def iter_source_files() -> list[Path]:
    files = [p for p in ROOT.rglob("*") if p.suffix in SOURCE_EXTS
             and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
             and p.relative_to(ROOT).as_posix() != SELF]
    return sorted(files)


def hits_in_file(path: Path) -> dict[int, str]:
    """返回 {行号: 行内容}。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    protected: set[int] = set()
    if text.startswith("#!"):
        protected.add(1)
    for no in (1, 2):
        if no <= len(lines) and ENCODING_DECL.match(lines[no - 1]):
            protected.add(no)

    out: dict[int, str] = {}

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            body = tok.string.lstrip("#").lstrip()
            if body.startswith(("noqa", "type:")):
                continue  # 工具指令,删掉会改变 lint/类型检查结果
            out[tok.start[0]] = lines[tok.start[0] - 1]
    except (tokenize.TokenError, IndentationError):
        pass  # 语法坏文件由 ast 侧兜不住时交给构建报错

    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(
                node.value, (ast.Constant, ast.JoinedStr)
            ) and not (isinstance(node.value, ast.Constant)
                       and not isinstance(node.value.value, str)):
                for no in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    out[no] = lines[no - 1]

    return {no: content for no, content in out.items() if no not in protected}


def collect_hits() -> dict[str, dict[int, str]]:
    # 键用 posix 路径,与 git diff / ls-files 的输出对齐
    return {p.relative_to(ROOT).as_posix(): hits_in_file(p) for p in iter_source_files()}


def added_lines_from_git() -> tuple[dict[str, set[int]], set[str]]:
    """git diff HEAD 的新增行号,以及未跟踪的源文件。"""
    added: dict[str, set[int]] = {}
    r = subprocess.run(["git", "diff", "-U0", "--no-color", "HEAD"], cwd=ROOT,
                       capture_output=True, text=True)
    cur: str | None = None
    newline = 0
    for line in r.stdout.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("+++") or line.startswith("---"):
            if line.startswith("+++"):
                cur = None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            newline = int(m.group(1))
        elif line.startswith("+") and cur is not None:
            added.setdefault(cur, set()).add(newline)
            newline += 1
        elif line.startswith("-"):
            pass
    r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT,
                       capture_output=True, text=True)
    untracked = {f for f in r.stdout.splitlines() if Path(f).suffix in SOURCE_EXTS}
    return added, untracked


def hit_key(path: str, content: str) -> str:
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def new_hits(hits: dict[str, dict[int, str]]) -> list[str]:
    """--diff 的命中清单,格式 `路径:行号: 内容`。"""
    out = []
    baseline: dict[str, list[str]] = {}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if is_git():
        added, untracked = added_lines_from_git()
        for path, by_no in hits.items():
            known = set(baseline.get(path, []))
            if path in untracked:
                out.extend(f"{path}:{no}: {c.strip()}" for no, c in sorted(by_no.items())
                           if hit_key(path, c) not in known)
                continue
            for no, c in sorted(by_no.items()):
                # 基线吸收 dune-init 时刻已有的命中(含未提交改动);git diff 判定新增
                if no in added.get(path, set()) and hit_key(path, c) not in known:
                    out.append(f"{path}:{no}: {c.strip()}")
        return out

    if not BASELINE.exists():
        print("提示:非 git 项目请先跑 `python scripts/check_comments.py --snapshot` 生成基线,"
              "本次不做比对。", file=sys.stderr)
        return []
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for path, by_no in hits.items():
        known = set(baseline.get(path, []))
        for no, c in sorted(by_no.items()):
            if hit_key(path, c) not in known:
                out.append(f"{path}:{no}: {c.strip()}")
    return out


def report(bad: list[str]) -> int:
    if not bad:
        return 0
    print(f"发现 {len(bad)} 处注释/弃置字符串:", file=sys.stderr)
    for line in bad:
        print("  " + line, file=sys.stderr)
    print(MSG, file=sys.stderr)
    return 1


def main() -> int:
    hits = collect_hits()
    args = sys.argv[1:]
    if "--snapshot" in args:
        snap = {p: [hit_key(p, c) for c in by_no.values()] for p, by_no in hits.items()}
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"基线已写入 {BASELINE.name}。")
        return 0
    if "--diff" in args:
        return report(new_hits(hits))
    if "--hook-claude" in args:
        bad = new_hits(hits)
        if bad:
            reason = "\n".join(bad) + "\n" + MSG
            print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
        return 0
    all_hits = [f"{p}:{no}: {c.strip()}"
                for p, by_no in sorted(hits.items()) for no, c in sorted(by_no.items())]
    return report(all_hits)


if __name__ == "__main__":
    sys.exit(main())
