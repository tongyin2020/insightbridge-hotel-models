from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BASE = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")
FINAL = BASE / "final_three_models_release_20260625"
SUITE = FINAL / "run_final_model_suite.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the final three InsightBridge models only.")
    parser.add_argument("--hotel-id", default=None)
    parser.add_argument("--sim-hour", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cmd = [sys.executable, str(SUITE), "--sim-hour", str(args.sim_hour)]
    if args.hotel_id:
        cmd.extend(["--hotel-id", args.hotel_id])
    print("Running final three models only...")
    print("Suite:", SUITE)
    subprocess.run(cmd, check=True)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
