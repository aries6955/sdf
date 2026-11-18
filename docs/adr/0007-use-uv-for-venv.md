# 使用 uv 管理项目虚拟环境

项目使用 uv 在仓库目录内建立 .venv 并安装依赖。环境已有 FastAPI 系统安装,但项目依赖通过 uv 锁定在 pyproject.toml/uv.lock 中,避免不同机器依赖漂移;PDF 导出等新增依赖也经 uv 安装。
