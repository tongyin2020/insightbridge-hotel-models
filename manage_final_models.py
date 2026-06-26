from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")
RUNNER = BASE / "run_final_models_only.py"
CHECKER = BASE / "check_final_models.py"
LOG_DIR = BASE / "reports" / "runtime_logs"
PID_FILE = LOG_DIR / "final_models_loop.pid"
LOOP_LOG = LOG_DIR / "final_models_loop.log"


def ensure_dirs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid() -> int | None:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def current_sim_hour() -> int:
    hour = datetime.now().hour
    return int(hour)


def run_once(sim_hour: int | None = None) -> int:
    ensure_dirs()
    hour = current_sim_hour() if sim_hour is None else sim_hour
    cmd = [sys.executable, str(RUNNER), "--sim-hour", str(hour)]
    print(f"[run-once] sim_hour={hour}")
    return subprocess.run(cmd, cwd=str(BASE)).returncode


def run_check() -> int:
    cmd = [sys.executable, str(CHECKER)]
    return subprocess.run(cmd, cwd=str(BASE)).returncode


def loop_forever(interval_minutes: int) -> int:
    ensure_dirs()
    existing = read_pid()
    if existing and process_alive(existing):
        print(f"[loop] already running with PID {existing}")
        return 1

    write_pid()
    print(f"[loop] started with PID {os.getpid()}")
    print(f"[loop] interval={interval_minutes} minutes")
    try:
        with LOOP_LOG.open("a", encoding="utf-8") as log:
            while True:
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log.write(f"\n[{stamp}] cycle start\n")
                log.flush()
                rc = run_once()
                log.write(f"[{stamp}] cycle result rc={rc}\n")
                log.flush()
                if rc != 0:
                    log.write(f"[{stamp}] warning: run_once returned {rc}\n")
                    log.flush()
                time.sleep(max(interval_minutes, 1) * 60)
    finally:
        remove_pid()


def start_detached(interval_minutes: int) -> int:
    ensure_dirs()
    existing = read_pid()
    if existing and process_alive(existing):
        print(f"[start] loop already running with PID {existing}")
        return 1

    cmd = [
        sys.executable,
        str(BASE / "manage_final_models.py"),
        "loop",
        "--interval-minutes",
        str(interval_minutes),
    ]
    with LOOP_LOG.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    time.sleep(1)
    print(f"[start] launched background loop PID {proc.pid}")
    print(f"[start] log: {LOOP_LOG}")
    return 0


def stop_loop() -> int:
    pid = read_pid()
    if not pid:
        print("[stop] no PID file found")
        return 1
    if not process_alive(pid):
        print(f"[stop] PID {pid} is not running; cleaning stale PID file")
        remove_pid()
        return 1
    os.kill(pid, signal.SIGTERM)
    time.sleep(1)
    if process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    remove_pid()
    print(f"[stop] stopped PID {pid}")
    return 0


def status() -> int:
    ensure_dirs()
    pid = read_pid()
    print("InsightBridge Final Models Runtime Status")
    print("=" * 60)
    if pid and process_alive(pid):
        print(f"loop_status: RUNNING (PID {pid})")
    elif pid:
        print(f"loop_status: STALE PID FILE ({pid})")
    else:
        print("loop_status: STOPPED")
    print(f"log_file: {LOOP_LOG}")
    return run_check()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the final three InsightBridge models.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run-once", help="Run the final three models once in the foreground.")

    start = sub.add_parser("start", help="Start a background loop for the final three models.")
    start.add_argument("--interval-minutes", type=int, default=60)

    loop = sub.add_parser("loop", help="Internal loop mode used by start.")
    loop.add_argument("--interval-minutes", type=int, default=60)

    sub.add_parser("stop", help="Stop the background loop.")
    sub.add_parser("status", help="Show loop status and latest model outputs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "run-once":
        return run_once()
    if args.command == "start":
        return start_detached(args.interval_minutes)
    if args.command == "loop":
        return loop_forever(args.interval_minutes)
    if args.command == "stop":
        return stop_loop()
    if args.command == "status":
        return status()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
