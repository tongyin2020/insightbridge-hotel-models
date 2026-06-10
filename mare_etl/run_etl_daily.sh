#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" "$ROOT_DIR/mare_etl/extract.py" --mode incremental
LATEST_EXTRACT="$(ls -1t "$ROOT_DIR/mare_etl/staging"/*.jsonl 2>/dev/null | head -n 1 | sed 's#.*/##' | cut -d'_' -f1-3)"
if [ -n "${LATEST_EXTRACT:-}" ]; then
  "$PYTHON_BIN" "$ROOT_DIR/mare_etl/transform.py"
  "$PYTHON_BIN" "$ROOT_DIR/mare_etl/load.py" --all-pending
fi

