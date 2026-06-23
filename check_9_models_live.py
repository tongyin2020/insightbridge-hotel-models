#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")

S1_PID = BASE_DIR / "system1_chatgpt_harness" / "logs" / "harness.pid"
S2_PID = BASE_DIR / "system2_claude_simulation" / "simulation.pid"
S3_PID = BASE_DIR / "system3_crewai" / "crewai.pid"

S1_OUTPUT_DIR = BASE_DIR / "hotel_model_staging_output"
S2_DB = BASE_DIR / "system2_claude_simulation" / "results.db"
S3_DB = BASE_DIR / "system3_crewai" / "crewai_results.db"
ML_STATE = BASE_DIR / "hotel_collector" / "mare_ml_state.json"

# If recent output/db activity is newer than this many minutes, count it as "live".
LIVE_WINDOW_MINUTES = 90


@dataclass
class CheckResult:
    name: str
    pid: int | None
    pid_running: bool | None
    live: bool
    detail: str


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


def age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 60.0


def age_minutes_from_dt(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 60.0


def parse_run_timestamp(path: Path) -> datetime | None:
    match = re.search(r"run_(\d{8}T\d{6})Z\.jsonl$", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except Exception:
        return None


def latest_s1_output() -> tuple[Path | None, float | None]:
    files = list(S1_OUTPUT_DIR.glob("run_*.jsonl"))
    if not files:
        return None, None

    parsed = []
    for path in files:
        ts = parse_run_timestamp(path)
        if ts is not None:
            parsed.append((path, ts))

    if parsed:
        latest, latest_ts = sorted(parsed, key=lambda item: item[1], reverse=True)[0]
        return latest, age_minutes_from_dt(latest_ts)

    latest = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return latest, age_minutes(latest)


def latest_db_activity(db_path: Path) -> tuple[int | None, str | None, int | None]:
    if not db_path.exists():
        return None, None, None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(sim_hour), MAX(run_at), COUNT(*) FROM hourly_runs"
        ).fetchone()
        if not row:
            return None, None, None
        return row[0], row[1], row[2]
    finally:
        conn.close()


def minutes_since_text(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return (datetime.now() - dt).total_seconds() / 60.0


def fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "N/A"
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes/60.0:.1f}h"


def check_s1() -> CheckResult:
    pid = read_pid(S1_PID)
    running = pid_running(pid)
    latest_file, age = latest_s1_output()
    live = bool(running) and age is not None and age <= LIVE_WINDOW_MINUTES
    detail = (
        f"latest_file={latest_file.name if latest_file else 'missing'} | "
        f"output_age={fmt_age(age)}"
    )
    return CheckResult("System 1", pid, running, live, detail)


def check_db_system(name: str, pid_path: Path, db_path: Path) -> CheckResult:
    pid = read_pid(pid_path)
    running = pid_running(pid)
    max_hour, last_run_at, total_rows = latest_db_activity(db_path)
    age = minutes_since_text(last_run_at)
    live = bool(running) and age is not None and age <= LIVE_WINDOW_MINUTES
    detail = (
        f"last_hour={max_hour if max_hour is not None else 'N/A'} | "
        f"last_run_at={last_run_at or 'N/A'} | "
        f"activity_age={fmt_age(age)} | rows={total_rows if total_rows is not None else 'N/A'}"
    )
    return CheckResult(name, pid, running, live, detail)


def check_ml_state() -> str:
    age = age_minutes(ML_STATE)
    return f"ML state age={fmt_age(age)}"


def print_result(r: CheckResult) -> None:
    status = "LIVE" if r.live else "ATTENTION"
    print(f"[{status}] {r.name}")
    print(f"  PID: {r.pid if r.pid is not None else 'missing'}")
    print(f"  process_running: {r.pid_running}")
    print(f"  {r.detail}")


def main() -> int:
    print("InsightBridge 9-Model Live Check")
    print("=" * 60)

    results = [
        check_s1(),
        check_db_system("System 2", S2_PID, S2_DB),
        check_db_system("System 3", S3_PID, S3_DB),
    ]

    for result in results:
        print_result(result)

    print(f"[INFO] {check_ml_state()}")

    if all(r.live for r in results):
        print("\nOverall: 9 models are truly running.")
        return 0

    print("\nOverall: At least one system needs attention.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
