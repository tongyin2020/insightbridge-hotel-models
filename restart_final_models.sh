#!/bin/bash
set -euo pipefail

BASE="/Users/tongyin/Desktop/InsightBridge_九大模型_v2026"

echo "Restarting final three models only..."
python3 "$BASE/run_final_models_only.py"
python3 "$BASE/check_final_models.py"
