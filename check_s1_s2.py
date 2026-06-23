#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
import re

BASE_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")

S1_PID = BASE_DIR / "system1_chatgpt_harness" / "logs" / "harness.pid"
S2_PID = BASE_DIR / "system2_claude_simulation" / "simulation.pid"

S1_OUTPUT_DIR = BASE_DIR / "hotel_model_staging_output"
S1_LOG = BASE_DIR / "system1_chatgpt_harness" / "logs" / "manual_restart.log"

S2_DB = BASE_DIR / "system2_claude_simulation" / "results.db"
S2_LOG = BASE_DIR / "system2_claude_simulation" / "simulation.log"

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


def fmt_age_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "N/A"
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes/60.0:.1f}h"


def age_minutes_from_dt(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


def parse_run_timestamp(path: Path) -> datetime | None:
    m = re.search(r"run_(\d{8}T\d{6})Z\.jsonl$", path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    except Exception:
        return None


def latest_s1_by_name() -> tuple[Path | None, datetime | None, float | None]:
    files = list(S1_OUTPUT_DIR.glob("run_*.jsonl"))
    if not files:
        return None, None, None

    parsed = []
    for f in files:
        ts = parse_run_timestamp(f)
        if ts is not None:
            parsed.append((f, ts))

    if parsed:
        latest_file, latest_ts = sorted(parsed, key=lambda x: x[1], reverse=True)[0]
        return latest_file, latest_ts, age_minutes_from_dt(latest_ts)

    latest_file = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    mtime_dt = datetime.fromtimestamp(latest_file.stat().st_mtime)
    return latest_file, mtime_dt, age_minutes_from_dt(mtime_dt)


def tail_text(path: Path, limit: int = 5) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return lines[-limit:]
    except Exception:
        return []


def latest_s2_db() -> tuple[int | None, str | None, int | None]:
    if not S2_DB.exists():
        return None, None, None
    conn = sqlite3.connect(S2_DB)
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
            return dt, age_minutes_from_dt(dt)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(ts)
        return dt, age_minutes_from_dt(dt)
    except Exception:
        return None, None


def main() -> int:
    print("InsightBridge S1/S2 Check")
    print("=" * 60)

    s1_pid = read_pid(S1_PID)
    s1_running = pid_running(s1_pid)
    s1_file, s1_ts, s1_age = latest_s1_by_name()
    s1_live = bool(s1_running) and s1_age is not None and s1_age <= LIVE_WINDOW_MINUTES

    print("[System 1]")
    print(f"PID: {s1_pid if s1_pid is not None else 'missing'}")
    print(f"process_running: {s1_running}")
    print(f"latest_output: {s1_file.name if s1_file else 'missing'}")
    print(f"output_timestamp: {s1_ts.strftime('%Y-%m-%d %H:%M:%S') if s1_ts else 'N/A'}")
    print(f"output_age: {fmt_age_minutes(s1_age)}")
    print(f"status: {'LIVE' if s1_live else 'ATTENTION'}")
    print("log_tail:")
    for line in tail_text(S1_LOG):
        print(f"  {line}")

    print("-" * 60)

    s2_pid = read_pid(S2_PID)
    s2_running = pid_running(s2_pid)
    s2_hour, s2_run_at, s2_rows = latest_s2_db()
    _, s2_age = parse_db_time(s2_run_at)
    s2_live = bool(s2_running) and s2_age is not None and s2_age <= LIVE_WINDOW_MINUTES

    print("[System 2]")
    print(f"PID: {s2_pid if s2_pid is not None else 'missing'}")
    print(f"process_running: {s2_running}")
    print(f"last_hour: {s2_hour if s2_hour is not None else 'N/A'}")
    print(f"last_run_at: {s2_run_at or 'N/A'}")
    print(f"activity_age: {fmt_age_minutes(s2_age)}")
    print(f"rows: {s2_rows if s2_rows is not None else 'N/A'}")
    print(f"status: {'LIVE' if s2_live else 'ATTENTION'}")
    print("log_tail:")
    for line in tail_text(S2_LOG):
        print(f"  {line}")

    print("-" * 60)

    if s1_live and s2_live:
        print("Overall: System 1 and System 2 are both live.")
        return 0

    print("Overall: At least one of System 1 / System 2 needs attention.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
