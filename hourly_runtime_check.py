#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_PATH = BASE / "hourly_runtime_check.log"
STATUS_PATH = BASE / "reports" / "hourly_runtime_status.json"

S1_PID = BASE / "system1_chatgpt_harness" / "s1_harness.pid"
S1_OUTDIR = BASE / "hotel_model_staging_output"

S2_PID = BASE / "system2_claude_simulation" / "simulation.pid"
S2_DB = BASE / "system2_claude_simulation" / "results.db"

S3_PID = BASE / "system3_crewai" / "crewai.pid"
S3_DB = BASE / "system3_crewai" / "crewai_results.db"

COLLECTOR_DB = BASE / "hotel_collector" / "hotel_real_data.db"

MODEL_STALE_MINUTES = 95
COLLECTOR_STALE_HOURS = 30

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hourly_runtime_check")


def pid_alive(pid_path: Path) -> tuple[bool, int | None]:
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except Exception:
        return False, pid


def age_minutes(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 60.0


def latest_s1_age_minutes() -> float | None:
    summaries = sorted(
        S1_OUTDIR.glob("summary_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not summaries:
        return None
    return age_minutes(summaries[0])


def latest_run_at_age_minutes(db_path: Path, table: str = "hourly_runs") -> float | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(f"SELECT MAX(run_at) FROM {table}").fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        last = datetime.fromisoformat(str(row[0]))
        return (datetime.now() - last).total_seconds() / 60.0
    except Exception:
        return None


def build_model_status(name: str, pid_path: Path, freshness_minutes: float | None) -> dict:
    alive, pid = pid_alive(pid_path)
    fresh = freshness_minutes is not None and freshness_minutes <= MODEL_STALE_MINUTES
    ok = alive and fresh
    return {
        "name": name,
        "pid": pid,
        "alive": alive,
        "freshness_minutes": None if freshness_minutes is None else round(freshness_minutes, 1),
        "ok": ok,
    }


def main() -> int:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)

    s1 = build_model_status("system1", S1_PID, latest_s1_age_minutes())
    s2 = build_model_status("system2", S2_PID, latest_run_at_age_minutes(S2_DB))
    s3 = build_model_status("system3", S3_PID, latest_run_at_age_minutes(S3_DB))

    collector_age = age_minutes(COLLECTOR_DB)
    collector_ok = (
        collector_age is not None
        and collector_age <= COLLECTOR_STALE_HOURS * 60
    )
    collector = {
        "name": "collector_db",
        "freshness_hours": None if collector_age is None else round(collector_age / 60.0, 1),
        "ok": collector_ok,
    }

    overall_ok = all(item["ok"] for item in (s1, s2, s3)) and collector_ok
    payload = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall_ok": overall_ok,
        "systems": [s1, s2, s3],
        "collector": collector,
    }
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    level = logging.INFO if overall_ok else logging.WARNING
    log.log(level, json.dumps(payload, ensure_ascii=False))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
