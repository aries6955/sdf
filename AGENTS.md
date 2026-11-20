# AGENTS.md

## 完成标准

改完任何代码运行 `uv run python scripts/check.py`(ruff + pytest),全部通过才算完成。
单跑 `uv run pytest -q` 会漏掉未使用 import、死变量与行内多语句。

## 进程纪律

**只结束自己启动的进程**,按 PID 或端口精确停;不按进程名匹配。

- 停服务:`uv run python scripts/stop_server.py <端口>`
- 启动服务:后台起并记下 PID,开发期用 `--reload`。改 `.py`/模板/CSS 后必须重启,旧进程不会加载新代码。
- 无头截图:`scripts/shot.py`,它用独立 `--user-data-dir` 的一次性实例,只结束自己的 PID。

## 领域与 UI

- 改计费或账务逻辑前先读 `GLOSSARY.md` 与相关 `docs/adr/`。两条硬约束:归属只来自租期(`ADR-0012`)、
  账期沿计量线连续且换表不切断(`ADR-0014`)——绕开就会重新引入双份归属或账单被拆成两张。
- 改 HTML/CSS/文案前先读 `DESIGN.md`(token、组件配方、措辞规则);新增页面照它的配方做。

## Agent skills

- Issue tracker:spec 与 issues 是 `.scratch/<feature-slug>/` 下的 markdown,无远程 tracker。
  见 `docs/agents/issue-tracker.md`
- Triage labels:五个角色,标签串与角色名相同。见 `docs/agents/triage-labels.md`
- Domain docs:单上下文,`GLOSSARY.md` + `docs/adr/`。见 `docs/agents/domain.md`

## 派子代理

审查用 `reviewer`、实现用 `worker`、探索用 `scout`。`claude-code`/`codex-exec` 是外部 CLI profile,
要求本机装了对应命令,不是 Pi 子代理。

`reviewer` 没有 shell,`watchdog_diff` 也不读提交区间:要审已提交的 diff,先把 `git diff` 落成文件再给它路径。
