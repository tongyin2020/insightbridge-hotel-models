#!/bin/bash
# ============================================================
# InsightBridge 九大模型 — 定时自动运行脚本
# 每天 06:00 / 18:00 由 launchd 触发
#
# 逻辑：
#   1. 检查 S1/S2/S3 是否正在运行
#   2. 如未运行 → 检查是否21天模拟完成(504小时) → 若完成则归档DB后重启新一轮
#   3. 如未运行且未完成 → 直接重启（崩溃恢复）
#   4. 如正在运行 → 跳过（不干预运行中的进程）
# ============================================================

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"
PYTHON="/opt/anaconda3/bin/python3"
LOG="$BASE/auto_run_9models.log"
ARCHIVE_DIR="$BASE/db_archives"

mkdir -p "$ARCHIVE_DIR"
mkdir -p "$BASE/system1_chatgpt_harness/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

# 检查 PID 是否存活
is_running_by_pid() {
    local pid_file="$1"
    [ -f "$pid_file" ] || return 1
    local pid
    pid=$(cat "$pid_file" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
    return 1
}

# 检查进程名是否存活
is_running_by_name() {
    pgrep -f "$1" > /dev/null 2>&1
}

# 查询 SQLite DB 中已完成的最大 sim_hour
get_max_hour() {
    local db="$1"
    [ -f "$db" ] || { echo "-1"; return; }
    "$PYTHON" -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('$db', timeout=5)
    r = conn.execute('SELECT MAX(sim_hour) FROM hourly_runs').fetchone()
    conn.close()
    print(r[0] if r and r[0] is not None else -1)
except:
    print(-1)
" 2>/dev/null
}

# 归档 DB 文件（重命名加时间戳）
archive_db() {
    local db="$1"
    local label="$2"
    if [ -f "$db" ]; then
        local ts
        ts=$(date '+%Y%m%d_%H%M')
        local archived="$ARCHIVE_DIR/${label}_${ts}.db"
        mv "$db" "$archived"
        log "  📦 DB已归档 → db_archives/${label}_${ts}.db"
    fi
}

# 启动系统并写 PID 文件
start_system() {
    local name="$1"
    local script="$2"
    local cwd="$3"
    local pid_file="$4"
    local log_file="$5"
    local extra_args="${6:-}"

    cd "$cwd" || { log "  ❌ cd 失败: $cwd"; return 1; }

    # 加载 .env（S1 需要 API KEY）
    if [ -f "$cwd/.env" ]; then
        set -a
        source "$cwd/.env"
        set +a
    fi

    nohup "$PYTHON" -u "$script" $extra_args >> "$log_file" 2>&1 &
    local pid=$!
    echo "$pid" > "$pid_file"
    log "  ✅ $name 已启动 (PID $pid)"
}

# ─────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════"
log "  InsightBridge 九大模型 定时检查 (6:00/18:00)"
log "════════════════════════════════════════════════"

TOTAL_HOURS=504   # 21天 × 24小时

# ══════════════════════════════
# S2 — Claude版（3+3模型, MARE+CRM+SelfACQ × 3星级）
# ══════════════════════════════
S2_NAME="S2 Claude版"
S2_SCRIPT="$BASE/system2_claude_simulation/run_simulation.py"
S2_CWD="$BASE/system2_claude_simulation"
S2_PID="$S2_CWD/simulation.pid"
S2_DB="$S2_CWD/results.db"
S2_LOG="$S2_CWD/simulation_output.log"

log "── $S2_NAME ──"
if is_running_by_pid "$S2_PID"; then
    log "  ✓ 运行中 (PID $(cat $S2_PID))，跳过"
else
    MAX_H=$(get_max_hour "$S2_DB")
    log "  已停止，当前进度: 第${MAX_H}小时 / ${TOTAL_HOURS}小时"
    if [ "$MAX_H" -ge $((TOTAL_HOURS - 1)) ] 2>/dev/null; then
        log "  🏁 21天模拟已完成！归档数据库，开始新一轮..."
        archive_db "$S2_DB" "s2_results"
    fi
    start_system "$S2_NAME" "$S2_SCRIPT" "$S2_CWD" "$S2_PID" "$S2_LOG"
fi

# ══════════════════════════════
# S3 — CrewAI版（3+3模型, Firecrawl增强）
# ══════════════════════════════
S3_NAME="S3 CrewAI版"
S3_SCRIPT="$BASE/system3_crewai/main.py"
S3_CWD="$BASE/system3_crewai"
S3_PID="$S3_CWD/crewai.pid"
S3_DB="$S3_CWD/crewai_results.db"
S3_LOG="$S3_CWD/crewai_sim.log"

log "── $S3_NAME ──"
if is_running_by_pid "$S3_PID"; then
    log "  ✓ 运行中 (PID $(cat $S3_PID))，跳过"
else
    MAX_H=$(get_max_hour "$S3_DB")
    log "  已停止，当前进度: 第${MAX_H}小时 / ${TOTAL_HOURS}小时"
    if [ "$MAX_H" -ge $((TOTAL_HOURS - 1)) ] 2>/dev/null; then
        log "  🏁 21天模拟已完成！归档数据库，开始新一轮..."
        archive_db "$S3_DB" "s3_crewai_results"
        # 同时归档 WAL 文件
        rm -f "$S3_DB-shm" "$S3_DB-wal"
    fi
    start_system "$S3_NAME" "$S3_SCRIPT" "$S3_CWD" "$S3_PID" "$S3_LOG"
fi

# ══════════════════════════════
# S1 — ChatGPT版（3模型, GPT-4o驱动）
# ══════════════════════════════
S1_NAME="S1 ChatGPT版"
S1_SCRIPT="$BASE/system1_chatgpt_harness/run_21d_harness.py"
S1_CWD="$BASE/system1_chatgpt_harness"
S1_PID="$S1_CWD/s1_harness.pid"
S1_LOG="$S1_CWD/logs/auto_run.log"
S1_OUTPUT_DIR="$BASE/hotel_model_staging_output"

log "── $S1_NAME ──"
# S1 优先用 PID 文件检查，再用进程名
if is_running_by_pid "$S1_PID" || is_running_by_name "run_21d_harness.py"; then
    # 如果按进程名找到但 PID 文件不对，更新 PID 文件
    LIVE_PID=$(pgrep -f "run_21d_harness.py" 2>/dev/null | head -1)
    [ -n "$LIVE_PID" ] && echo "$LIVE_PID" > "$S1_PID"
    log "  ✓ 运行中 (PID ${LIVE_PID:-$(cat $S1_PID 2>/dev/null)})，跳过"
else
    log "  已停止，重新启动..."
    mkdir -p "$S1_OUTPUT_DIR"
    start_system "$S1_NAME" "$S1_SCRIPT" "$S1_CWD" "$S1_PID" "$S1_LOG" \
        "--output-dir $S1_OUTPUT_DIR"
fi

# ══════════════════════════════
# 总结状态
# ══════════════════════════════
log "── 当前状态汇总 ──"
for SYS_PID in "$S2_PID" "$S3_PID" "$S1_PID"; do
    if is_running_by_pid "$SYS_PID"; then
        log "  ✅ PID $(cat $SYS_PID) — 运行中"
    else
        log "  ⚠️  $SYS_PID — 未运行"
    fi
done

log "════════════════════════════════════════════════"
log "完成。下次检查: 今天 06:00 或 18:00"
log ""
