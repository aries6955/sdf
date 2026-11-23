# syntax=docker/dockerfile:1
# 依赖装进独立 venv,运行时镜像只带 venv + app:不带 uv、不带构建缓存
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
# 文泉驿正黑:PDF 导出需要中文字体(约 16MB);时区用 POSIX 写法,省掉 tzdata
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app /app/app
ENV PATH="/app/.venv/bin:$PATH" \
    SDF_DATA_DIR=/data \
    TZ=CST-8 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8536
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8536"]
