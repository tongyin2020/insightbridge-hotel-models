#!/bin/zsh
set -euo pipefail

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"
PY="/opt/anaconda3/bin/python3"

S1_DIR="$BASE/system1_chatgpt_harness"
S2_DIR="$BASE/system2_claude_simulation"
S3_DIR="$BASE/system3_crewai"

S1_PID_FILE="$S1_DIR/logs/harness.pid"
S2_PID_FILE="$S2_DIR/simulation.pid"
S3_PID_FILE="$S3_DIR/crewai.pid"

stop_if_running() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
  fi
}

start_system() {
  local name="$1"
  local dir="$2"
  local cmd="$3"
  local log_file="$4"
  local pid_file="$5"

  mkdir -p "$(dirname "$log_file")"
  cd "$dir"
  nohup "$PY" "$cmd" > "$log_file" 2>&1 &
  echo $! > "$pid_file"
  echo "[$name] started with PID $(cat "$pid_file")"
}

echo "Restarting InsightBridge 3 systems..."

stop_if_running "$S1_PID_FILE"
stop_if_running "$S2_PID_FILE"
stop_if_running "$S3_PID_FILE"

start_system "System 1" "$S1_DIR" "run_21d_harness.py" "$S1_DIR/logs/manual_restart.log" "$S1_PID_FILE"
start_system "System 2" "$S2_DIR" "run_simulation.py" "$S2_DIR/simulation.log" "$S2_PID_FILE"
start_system "System 3" "$S3_DIR" "main.py" "$S3_DIR/crewai_simulation.log" "$S3_PID_FILE"

echo
echo "Run this to verify:"
echo "python3 \"$BASE/check_3_systems.py\""
