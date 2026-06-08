#!/bin/bash
# S1 ChatGPT Harness 启动脚本
# 单一主目录：InsightBridge_九大模型_v2026/system1_chatgpt_harness

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-/opt/anaconda3/bin/python3}"

cd "$SCRIPT_DIR" || exit 1

# run_21d_harness.py 会自己通过 python-dotenv 读取 .env。
# 这里不在 shell 层解析，避免带空格的值（例如酒店名）破坏启动。
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

# 确保 output 目录存在
mkdir -p "$BASE_DIR/hotel_model_staging_output"
mkdir -p "$SCRIPT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting S1 ChatGPT Harness from $SCRIPT_DIR" >> "$SCRIPT_DIR/logs/launchd.out.log"

exec "$PYTHON" "$SCRIPT_DIR/run_21d_harness.py" \
    --output-dir "$BASE_DIR/hotel_model_staging_output"
