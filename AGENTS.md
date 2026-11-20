# AGENTS.md

## 完成标准

改完代码跑 `uv run python scripts/check.py`,全部通过才算完成。单跑 pytest 会漏掉未使用 import、死变量与行内多语句。

## 进程纪律

**只结束自己启动的进程**,按 PID 或端口精确停,不按进程名匹配。

- 停服务 `uv run python scripts/stop_server.py <端口>`;开发期用 `--reload` 起,改代码后必须重启
- 无头截图用 `scripts/shot.py`(独立 `--user-data-dir`,只结束自己的 PID)

## 改前先读

- 计费或账务 → `GLOSSARY.md` + 相关 `docs/adr/`。绕开会重新引入双份归属(`ADR-0012`)或账单被拆成两张(`ADR-0014`)
- HTML/CSS/文案 → `DESIGN.md`

## Agent skills

- Issue tracker:`.scratch/<feature-slug>/` 下的 markdown。`docs/agents/issue-tracker.md`
- Triage labels:`docs/agents/triage-labels.md`
- Domain docs:`docs/agents/domain.md`

## 派子代理

审查 `reviewer`、实现 `worker`、探索 `scout`。`claude-code`/`codex-exec` 是外部 CLI,需本机安装,不是 Pi 子代理。

`reviewer` 没有 shell,也不读提交区间:要审已提交的 diff,先 `git diff` 落成文件再给它路径。
