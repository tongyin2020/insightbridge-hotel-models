#!/bin/bash

set -u

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"
PYTHON="/opt/anaconda3/bin/python3"
LOG_DIR="$BASE/reports/daily_model_reports"
PIPELINE_LOG="$LOG_DIR/daily_9models_pipeline.log"
LOCK_DIR="/tmp/insightbridge_daily_9models.lock"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$PIPELINE_LOG"
}

cleanup() {
    rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "已有一条每日总控在运行，本次跳过。"
    exit 0
fi

trap cleanup EXIT

log ""
log "════════════════════════════════════════════════"
log "InsightBridge 每日 09:00 总控开始"
log "════════════════════════════════════════════════"

log "步骤 1/3：检查并启动九模型主进程"
/bin/bash "$BASE/auto_run_9models.sh" >> "$PIPELINE_LOG" 2>&1
log "步骤 1/3 完成"

log "步骤 2/3：执行一次限时真实数据采集（上限 65 分钟）"
export MAX_COLLECTION_MINUTES="${MAX_COLLECTION_MINUTES:-65}"
export FAST_MODE_BUFFER_MINUTES="${FAST_MODE_BUFFER_MINUTES:-20}"
export CRITICAL_MODE_BUFFER_MINUTES="${CRITICAL_MODE_BUFFER_MINUTES:-10}"
"$PYTHON" "$BASE/hotel_collector/hotel_data_collector.py" >> "$PIPELINE_LOG" 2>&1
COLLECT_EXIT=$?
if [ "$COLLECT_EXIT" -eq 0 ]; then
    log "步骤 2/3 完成"
else
    log "步骤 2/3 返回异常码 $COLLECT_EXIT，继续生成日报"
fi

log "步骤 3/3：生成日报并推送 Telegram"
"$PYTHON" "$BASE/daily_report.py" >> "$PIPELINE_LOG" 2>&1
REPORT_EXIT=$?
if [ "$REPORT_EXIT" -eq 0 ]; then
    log "步骤 3/3 完成"
else
    log "步骤 3/3 返回异常码 $REPORT_EXIT"
fi

log "════════════════════════════════════════════════"
log "每日 09:00 总控结束"
log "════════════════════════════════════════════════"
log ""

exit "$REPORT_EXIT"
