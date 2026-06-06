#!/bin/bash
# S1 ChatGPT Harness 启动脚本
# 单一主目录：InsightBridge_九大模型_v2026/system1_chatgpt_harness

SCRIPT_DIR="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/system1_chatgpt_harness"
PYTHON="/usr/bin/python3"

cd "$SCRIPT_DIR" || exit 1

# 加载环境变量
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# 确保 output 目录存在
mkdir -p "/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_model_staging_output"
mkdir -p "$SCRIPT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting S1 ChatGPT Harness from $SCRIPT_DIR" >> "$SCRIPT_DIR/logs/launchd.out.log"

exec "$PYTHON" "$SCRIPT_DIR/run_21d_harness.py" \
    --output-dir "/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_model_staging_output"
