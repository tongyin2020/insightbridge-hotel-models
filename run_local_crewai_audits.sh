#!/bin/bash
set -euo pipefail

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"
CREW_DIR="$BASE/system3_crewai"
PYTHON="/opt/anaconda3/bin/python3"
STAMP="$(date '+%Y%m%d_%H%M%S')"
RUN_LOG="$BASE/reports/local_crewai_audit_run_${STAMP}.log"

mkdir -p "$BASE/reports"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$RUN_LOG"
}

check_key() {
  local key_name="$1"
  local value
  value="$(grep "^${key_name}=" "$CREW_DIR/.env" 2>/dev/null | cut -d= -f2- || true)"
  if [ -z "$value" ] || [[ "$value" == your_* ]]; then
    return 1
  fi
  return 0
}

log ""
log "════════════════════════════════════════════════"
log "本机 CrewAI 审计启动"
log "项目: $BASE"
log "════════════════════════════════════════════════"

if [ ! -x "$PYTHON" ]; then
  echo "找不到 Python: $PYTHON"
  exit 1
fi

if [ ! -f "$CREW_DIR/.env" ]; then
  echo "找不到配置文件: $CREW_DIR/.env"
  exit 1
fi

log "检查关键 API 配置"
if check_key "PERPLEXITY_API_KEY"; then
  log "✓ Perplexity 已配置"
else
  log "⚠ Perplexity 未配置，审计脚本会自动降级到其他模型"
fi

if check_key "DEEPSEEK_API_KEY"; then
  log "✓ DeepSeek 已配置"
else
  log "⚠ DeepSeek 未配置"
fi

if check_key "OPENAI_API_KEY"; then
  log "✓ OpenAI 已配置"
else
  log "⚠ OpenAI 未配置"
fi

if check_key "WOLFRAM_APP_ID"; then
  log "✓ Wolfram 已配置"
else
  log "⚠ Wolfram 未配置"
fi

log "先做本地语法检查"
HOME="$BASE" "$PYTHON" -m py_compile \
  "$CREW_DIR/code_audit_crew.py" \
  "$CREW_DIR/model_math_audit_crew.py"

log "开始运行代码清理审计"
HOME="$BASE" "$PYTHON" "$CREW_DIR/code_audit_crew.py" 2>&1 | tee -a "$RUN_LOG"

log "开始运行数学/逻辑审计"
HOME="$BASE" "$PYTHON" "$CREW_DIR/model_math_audit_crew.py" 2>&1 | tee -a "$RUN_LOG"

log "审计完成"
log "生成的报告目录: $BASE/reports"
log "本次运行日志: $RUN_LOG"

echo ""
echo "运行完成。"
echo "请打开 $BASE/reports 查看最新生成的 audit 报告。"
