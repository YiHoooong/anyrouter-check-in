#!/bin/sh
# AnyRouter 签到容器入口：单次运行 or 定时循环 + 后台 Web UI
set -e

# 工作目录切到挂载点，脚本硬编码的相对路径产物（balance_hash.txt、
# .browser_profiles/、checkin_screenshots/）都落到 /app/data，随 volume 持久化。
# /app/checkin.py 以绝对路径运行，脚本所在目录 /app 会进 sys.path，utils 包可正常导入。
cd /app/data 2>/dev/null || { mkdir -p /app/data && cd /app/data; }

RUN_PY="/app/.venv/bin/python /app/checkin.py"
WEBUI_PY="/app/.venv/bin/python /app/webui.py"

# 账号/设置文件：compose 会传绝对路径；若没传（例如把镜像直接转成 LXC、丢了一堆
# 环境变量的部署），就按当前 cwd 推导并导出绝对路径。否则子进程（webui 触发签到、
# 定时循环）各自按自己的 cwd 解析相对路径，会得出不一致的文件、锁和日志。
ACCOUNTS_FILE="${CHECKIN_ACCOUNTS_FILE:-$PWD/data/accounts.json}"
SETTINGS_FILE="${CHECKIN_WEBUI_SETTINGS_FILE:-$PWD/data/webui_settings.json}"
DATA_DIR="$(dirname "$ACCOUNTS_FILE")"
LOCK="$DATA_DIR/.checkin.lock"
LOG_FILE="$DATA_DIR/last_run.log"
export CHECKIN_ACCOUNTS_FILE="$ACCOUNTS_FILE"
export CHECKIN_WEBUI_SETTINGS_FILE="$SETTINGS_FILE"
# 时间显示统一北京时间（compose 已传 TZ；缺省时兜底，避免日志/通知差 8 小时）
export TZ="${TZ:-Asia/Shanghai}"
# 全新卷（匿名卷/LXC 首启）时 DATA_DIR 可能还不存在
mkdir -p "$DATA_DIR"

INTERVAL_HOURS="${CHECKIN_INTERVAL_HOURS:-6}"
RUN_ON_START="${CHECKIN_RUN_ON_START:-true}"

# docker compose run --rm checkin once → 立即运行一次后退出（用于测试，不开 Web UI）
if [ "$1" = "once" ]; then
  exec $RUN_PY
fi

# 后台启动 Web UI（账号管理 + 手动签到），与定时循环共用同一把锁
if [ "${CHECKIN_WEBUI_ENABLED:-true}" = "true" ]; then
  echo "[DOCKER] starting Web UI on port ${CHECKIN_WEBUI_PORT:-8090}"
  $WEBUI_PY &
fi

# 定时签到：输出（含起止标记）追加进 Web UI 读的同一个 last_run.log，
# 同时保留 stdout（docker compose logs 仍能看到）。
# LXC 等没有 docker logs 的部署里 stdout 无处可去，日志文件是 Web UI 唯一来源。
run_once() {
  {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DOCKER] check-in starting"
    if flock -n "$LOCK" -c "$RUN_PY"; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DOCKER] check-in finished"
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] [DOCKER] check-in skipped or failed (lock busy / non-zero exit)"
    fi
  } 2>&1 | tee -a "$LOG_FILE"
}

if [ "$RUN_ON_START" = "true" ]; then
  run_once
fi

while true; do
  sleep "$((INTERVAL_HOURS * 3600))"
  run_once
done
