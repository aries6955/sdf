#!/usr/bin/env bash
# 宿主 cron 的入口:异地备份逻辑在容器内的 app/offsite.py,rclone 也随镜像发布。
# 这里只负责找到容器并转发退出码。用法见 README「异地备份」。
set -euo pipefail

CONTAINER=${SDF_CONTAINER:-sdf}

docker exec "$CONTAINER" python -m app.offsite
