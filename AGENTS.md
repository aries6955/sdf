# AGENTS.md

## 完成标准

改完代码跑 `uv run python scripts/check.py`(ruff + `node --check` + pytest),全部通过才算完成。单跑 pytest 会漏掉未使用 import、死变量与行内多语句。

一次性脚本也一律 `uv run python -`,裸 `python` 会落到系统解释器,报 `ModuleNotFoundError`。

## 进程纪律

**只结束自己启动的进程**,按 PID 或端口精确停,不按进程名匹配。

- 停服务 `uv run python scripts/stop_server.py <端口>`;开发期用 `--reload` 起,改代码后必须重启
- 无头截图用 `scripts/shot.py`(独立 `--user-data-dir`,只结束自己的 PID)
- 要看登录后的页面:`uv run python scripts/dump_page.py /readings --shot`
  (带登录态、内联样式与 JS;`--click 'button[data-edit]'` 可验证靠 JS 展开的状态)。
  Git Bash 下加 `MSYS_NO_PATHCONV=1`,否则 `/readings` 会被转成 Windows 路径
- 产物落 `.tmp/`,每次覆盖同名文件;取文件前看 mtime,别读到上一轮的旧页面
- 服务日志:`/tmp/sdf-server.log`。页面没跟着改,先看它有没有 reload 失败

## 改前先读

- 计费或账务 → `GLOSSARY.md` + 相关 `docs/adr/`。绕开会重新引入双份归属(`ADR-0012`)或账单被拆成两张(`ADR-0014`)
- 动样式(字号/颜色/间距/圆角/阴影/表格对齐)→ **先读 `DESIGN.md`** 的 Tokens 与「对齐铁律」。
  字号只有 `--fs-*` 七档,颜色只用 `--ink/--ink-2/--muted/--brand*` 等变量;
  `scripts/check.py` 会拦下写死的 `font-size: 13.7px` 和 `color: #xxxxxx`。改完同步该文件
- 页面结构(卡片顺序、筛选条位置、弹窗)→ `DESIGN.md` 的「筛选条」「弹窗」「提示」三节

## Agent skills

- Issue tracker:`.scratch/<feature-slug>/` 下的 markdown。`docs/agents/issue-tracker.md`
- Triage labels:`docs/agents/triage-labels.md`
- Domain docs:`docs/agents/domain.md`

## 派子代理

审查 `reviewer`、实现 `worker`、探索 `scout`。`claude-code`/`codex-exec` 是外部 CLI,需本机安装,不是 Pi 子代理。

`reviewer` 没有 shell,也不读提交区间:要审已提交的 diff,先 `git diff` 落成文件再给它路径。
