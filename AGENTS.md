# AGENTS.md

## 操作纪律

### 完成标准:测试全过

改完任何代码后运行 `uv run pytest -q`,全部通过才算完成;有失败先修复再报告。

### 改完代码必须重启服务

改 `.py`/模板/CSS 后,运行中的 uvicorn 不会自动加载旧进程外的新代码。
开发期一律用 `--reload` 启动;若服务已是旧进程,先 `uv run python scripts/stop_server.py <端口>` 按 PID 停,再重启。

### 不要按进程名杀浏览器

**禁止** `taskkill /F /IM msedge.exe`、`killall chrome` 这类按进程名终止的命令 ——
会把用户正在使用的整个浏览器连同所有标签页一起关掉。

需要无头浏览器截图时,用 `scripts/shot.py`:它使用独立 `--user-data-dir` 启动一次性实例,
只结束自己启动的进程。同理,任何“顺带清理”都不得越界到用户的其他进程。

### 起停服务只动自己的进程

启动项目用后台方式并记下 PID;停止时按 PID 结束,不要用 `taskkill /IM uvicorn.exe` 这类模糊匹配。

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/` in this repo (no remote tracker). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles, label strings equal to role names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### UI design contract

改任何 HTML/CSS/文案前先读 `DESIGN.md`(token、组件配方、措辞规则、Do's & Don'ts);新增页面照它的配方做。
