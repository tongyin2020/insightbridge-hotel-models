#!/bin/bash
set -euo pipefail

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"
PY="/opt/anaconda3/bin/python3"

cd "$BASE"
"$PY" "$BASE/manage_final_models.py" start --interval-minutes 60

