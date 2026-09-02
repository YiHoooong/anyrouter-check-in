#!/bin/sh
# AnyRouter 签到容器入口：单次运行 or 定时循环 + 后台 Web UI
set -e

# 工作目录切到挂载点，脚本硬编码的相对路径产物（balance_hash.txt、
# .browser_profiles/、checkin_screenshots/）都落到 /app/data，随 volume 持久化。
# /app/checkin.py 以绝对路径运行，脚本所在目录 /app 会进 sys.path，utils 包可正常导入。
cd /app/data

RUN_PY="/app/.venv/bin/python /app/checkin.py"
WEBUI_PY="/app/.venv/bin/python /app/webui.py"
LOCK="/app/data/.checkin.lock"

INTERVAL_HOURS="${CHECKIN_INTERVAL_HOURS:-6}"
RUN_ON_START="${CHECKIN_RUN_ON_START:-true}"

# docker compose run --rm checkin once → 立即运行一次后退出（用于测试，不开 Web UI）
if [ "$1" = "once" ]; then
  exec $RUN_PY
fi

# 后台启动 Web UI（账号管理 + 手动签到），与定时循环共用 /app/data 和同一把锁
if [ "${CHECKIN_WEBUI_ENABLED:-true}" = "true" ]; then
  echo "[DOCKER] starting Web UI on port ${CHECKIN_WEBUI_PORT:-8090}"
  $WEBUI_PY &
fi

run_once() {
  echo "[DOCKER] $(date '+%Y-%m-%d %H:%M:%S') check-in starting"
  # 用 flock 与 Web UI 手动触发互斥：抢不到锁则跳过本轮
  flock -n "$LOCK" -c "$RUN_PY" \
    && echo "[DOCKER] check-in finished" \
    || echo "[DOCKER] check-in run skipped or failed (lock busy / non-zero exit); will retry next cycle"
}

if [ "$RUN_ON_START" = "true" ]; then
  run_once
fi

while true; do
  sleep "$((INTERVAL_HOURS * 3600))"
  run_once
done
