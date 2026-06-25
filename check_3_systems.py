#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")

S1_PID = BASE_DIR / "system1_chatgpt_harness" / "logs" / "harness.pid"
S2_PID = BASE_DIR / "system2_claude_simulation" / "simulation.pid"
S3_PID = BASE_DIR / "system3_crewai" / "crewai.pid"

S1_OUTPUT_DIR = BASE_DIR / "hotel_model_staging_output"
S1_LOG = BASE_DIR / "system1_chatgpt_harness" / "logs" / "manual_restart.log"
S2_DB = BASE_DIR / "system2_claude_simulation" / "results.db"
S2_LOG = BASE_DIR / "system2_claude_simulation" / "simulation.log"
S3_DB = BASE_DIR / "system3_crewai" / "crewai_results.db"
S3_LOG = BASE_DIR / "system3_crewai" / "crewai_simulation.log"
ML_STATE = BASE_DIR / "hotel_collector" / "mare_ml_state.json"

LIVE_WINDOW_MINUTES = 90


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_running(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "N/A"
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes/60.0:.1f}h"


def age_from_dt(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


def file_age(path: Path) -> float | None:
    if not path.exists():
        return None
    return age_from_dt(datetime.fromtimestamp(path.stat().st_mtime))


def file_mtime_dt(path: Path) -> datetime | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime)


def parse_run_timestamp(path: Path) -> datetime | None:
    match = re.search(r"run_(\d{8}T\d{6})Z\.jsonl$", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except Exception:
        return None


def latest_s1_output() -> tuple[Path | None, datetime | None, datetime | None, float | None]:
    files = list(S1_OUTPUT_DIR.glob("run_*.jsonl"))
    if not files:
        return None, None, None, None

    latest_path = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    start_ts = parse_run_timestamp(latest_path)
    activity_ts = file_mtime_dt(latest_path)
    activity_age = age_from_dt(activity_ts)
    return latest_path, start_ts, activity_ts, activity_age


def latest_db_activity(db_path: Path) -> tuple[int | None, str | None, int | None]:
    if not db_path.exists():
        return None, None, None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(sim_hour), MAX(run_at), COUNT(*) FROM hourly_runs"
        ).fetchone()
        return row if row else (None, None, None)
    finally:
        conn.close()


def parse_db_time(ts: str | None) -> tuple[datetime | None, float | None]:
    if not ts:
        return None, None
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt, age_from_dt(dt)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(ts)
        return dt, age_from_dt(dt)
    except Exception:
        return None, None


def tail_text(path: Path, limit: int = 5) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def print_log_tail(path: Path) -> None:
    print("log_tail:")
    for line in tail_text(path):
        print(f"  {line}")


def main() -> int:
    print("InsightBridge 3-System Health Check")
    print("=" * 60)

    s1_pid = read_pid(S1_PID)
    s1_running = pid_running(s1_pid)
    s1_file, s1_ts, s1_activity_ts, s1_age = latest_s1_output()
    s1_live = bool(s1_running) and s1_age is not None and s1_age <= LIVE_WINDOW_MINUTES

    print("[System 1]")
    print(f"PID: {s1_pid if s1_pid is not None else 'missing'}")
    print(f"process_running: {s1_running}")
    print(f"latest_output: {s1_file.name if s1_file else 'missing'}")
    print(f"run_start_timestamp: {s1_ts.strftime('%Y-%m-%d %H:%M:%S') if s1_ts else 'N/A'}")
    print(f"last_file_activity: {s1_activity_ts.strftime('%Y-%m-%d %H:%M:%S') if s1_activity_ts else 'N/A'}")
    print(f"output_age: {fmt_age(s1_age)}")
    print(f"status: {'LIVE' if s1_live else 'ATTENTION'}")
    print_log_tail(S1_LOG)
    print("-" * 60)

    s2_pid = read_pid(S2_PID)
    s2_running = pid_running(s2_pid)
    s2_hour, s2_run_at, s2_rows = latest_db_activity(S2_DB)
    _, s2_age = parse_db_time(s2_run_at)
    s2_live = bool(s2_running) and s2_age is not None and s2_age <= LIVE_WINDOW_MINUTES

    print("[System 2]")
    print(f"PID: {s2_pid if s2_pid is not None else 'missing'}")
    print(f"process_running: {s2_running}")
    print(f"last_hour: {s2_hour if s2_hour is not None else 'N/A'}")
    print(f"last_run_at: {s2_run_at or 'N/A'}")
    print(f"activity_age: {fmt_age(s2_age)}")
    print(f"rows: {s2_rows if s2_rows is not None else 'N/A'}")
    print(f"status: {'LIVE' if s2_live else 'ATTENTION'}")
    print_log_tail(S2_LOG)
    print("-" * 60)

    s3_pid = read_pid(S3_PID)
    s3_running = pid_running(s3_pid)
    s3_hour, s3_run_at, s3_rows = latest_db_activity(S3_DB)
    _, s3_age = parse_db_time(s3_run_at)
    s3_live = bool(s3_running) and s3_age is not None and s3_age <= LIVE_WINDOW_MINUTES

    print("[System 3]")
    print(f"PID: {s3_pid if s3_pid is not None else 'missing'}")
    print(f"process_running: {s3_running}")
    print(f"last_hour: {s3_hour if s3_hour is not None else 'N/A'}")
    print(f"last_run_at: {s3_run_at or 'N/A'}")
    print(f"activity_age: {fmt_age(s3_age)}")
    print(f"rows: {s3_rows if s3_rows is not None else 'N/A'}")
    print(f"status: {'LIVE' if s3_live else 'ATTENTION'}")
    print_log_tail(S3_LOG)
    print("-" * 60)

    ml_age = file_age(ML_STATE)
    print(f"[INFO] MARE ML state age: {fmt_age(ml_age)}")

    if s1_live and s2_live and s3_live:
        print("Overall: All 3 systems are live.")
        return 0

    print("Overall: At least one system needs attention.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
