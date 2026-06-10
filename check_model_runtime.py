#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026")


@dataclass
class CheckResult:
    name: str
    ok: bool
    lines: list[str]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _mtime_age(path: Path) -> float | None:
    if not path.exists():
        return None
    return _now_utc().timestamp() - path.stat().st_mtime


def _sqlite_activity_age(db_path: Path) -> float | None:
    """
    When SQLite WAL mode is enabled, the freshest writes may land in `*.db-wal`
    instead of the main `*.db` file. Use the newest available artifact as the
    activity heartbeat.
    """
    candidates = [db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]
    mtimes = [p.stat().st_mtime for p in candidates if p.exists()]
    if not mtimes:
        return None
    return _now_utc().timestamp() - max(mtimes)


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int | None) -> bool | None:
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


def _latest_s1_jsonl() -> Path | None:
    out_dir = BASE_DIR / "hotel_model_staging_output"
    candidates = sorted(out_dir.glob("run_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _check_s1(stale_after_sec: int) -> CheckResult:
    pid_path = BASE_DIR / "system1_chatgpt_harness" / "s1_harness.pid"
    latest_jsonl = _latest_s1_jsonl()
    pid = _read_pid(pid_path)
    alive = _pid_alive(pid)
    age = _mtime_age(latest_jsonl) if latest_jsonl else None
    fresh = age is not None and age <= stale_after_sec
    ok = bool(alive) and fresh

    lines = [
        f"PID: {pid if pid is not None else 'missing'} | running: {alive}",
        f"latest output: {latest_jsonl if latest_jsonl else 'missing'}",
        f"output age: {_fmt_age(age)}",
    ]

    if latest_jsonl and latest_jsonl.exists():
        try:
            last_mare: dict[str, Any] | None = None
            with latest_jsonl.open("r", encoding="utf-8") as fh:
                for line in fh:
                    obj = json.loads(line)
                    if obj.get("model") == "mare":
                        last_mare = obj
            if last_mare:
                result = last_mare.get("result", {})
                lines.append(
                    "latest MARE ML: "
                    f"enabled={result.get('ml_enabled')} "
                    f"elasticity={result.get('ml_elasticity_multiplier')} "
                    f"premium={result.get('ml_premium_delta')} "
                    f"occ={result.get('ml_occupancy_delta')} "
                    f"state_version={result.get('ml_state_version')}"
                )
        except Exception as exc:
            lines.append(f"latest MARE parse failed: {type(exc).__name__}: {exc}")

    return CheckResult("System 1", ok, lines)


def _check_sqlite_system(name: str, pid_path: Path, db_path: Path, stale_after_sec: int) -> CheckResult:
    pid = _read_pid(pid_path)
    alive = _pid_alive(pid)
    age = _sqlite_activity_age(db_path)
    fresh = age is not None and age <= stale_after_sec
    ok = bool(alive) and fresh

    lines = [
        f"PID: {pid if pid is not None else 'missing'} | running: {alive}",
        f"database: {db_path if db_path.exists() else 'missing'}",
        f"database activity age: {_fmt_age(age)}",
    ]

    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT MAX(id), MAX(run_at), COUNT(*) FROM hourly_runs").fetchone()
            lines.append(f"hourly_runs: max_id={row[0]} last_run_at={row[1]} total_rows={row[2]}")

            mare = conn.execute(
                "SELECT run_at, model_type, output_json FROM hourly_runs "
                "WHERE model_type LIKE 'MARE%' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if mare:
                _, model_type, output_json = mare
                data = json.loads(output_json)
                lines.append(
                    f"latest {model_type} ML: "
                    f"enabled={data.get('ml_enabled')} "
                    f"elasticity={data.get('ml_elasticity_multiplier')} "
                    f"premium={data.get('ml_premium_delta')} "
                    f"occ={data.get('ml_occupancy_delta')} "
                    f"state_version={data.get('ml_state_version')}"
                )
            conn.close()
        except Exception as exc:
            lines.append(f"db read failed: {type(exc).__name__}: {exc}")

    return CheckResult(name, ok, lines)


def _check_ml_state(stale_after_sec: int) -> CheckResult:
    path = BASE_DIR / "hotel_collector" / "mare_ml_state.json"
    age = _mtime_age(path)
    fresh = age is not None and age <= stale_after_sec
    ok = bool(fresh)

    lines = [
        f"state file: {path if path.exists() else 'missing'}",
        f"state age: {_fmt_age(age)}",
    ]

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profiles = data.get("profiles", {})
            lines.append(f"profiles: {len(profiles)}")
            top = sorted(
                (
                    (name, int(profile.get("stats", {}).get("decisions", 0)))
                    for name, profile in profiles.items()
                ),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            if top:
                lines.append("top profiles: " + ", ".join(f"{name}={count}" for name, count in top))
        except Exception as exc:
            lines.append(f"state parse failed: {type(exc).__name__}: {exc}")

    return CheckResult("MARE ML State", ok, lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether the three hotel model systems are still running.")
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=90,
        help="Treat outputs older than this as stale. Default: 90 minutes.",
    )
    args = parser.parse_args()

    stale_after_sec = max(1, args.stale_minutes) * 60

    checks = [
        _check_s1(stale_after_sec),
        _check_sqlite_system(
            "System 2",
            BASE_DIR / "system2_claude_simulation" / "simulation.pid",
            BASE_DIR / "system2_claude_simulation" / "results.db",
            stale_after_sec,
        ),
        _check_sqlite_system(
            "System 3",
            BASE_DIR / "system3_crewai" / "crewai.pid",
            BASE_DIR / "system3_crewai" / "crewai_results.db",
            stale_after_sec,
        ),
        _check_ml_state(stale_after_sec),
    ]

    all_ok = True
    print("\nInsightBridge Runtime Check")
    print("=" * 60)
    for check in checks:
        all_ok = all_ok and check.ok
        status = "OK" if check.ok else "ATTENTION"
        print(f"\n[{status}] {check.name}")
        for line in check.lines:
            print(f"  - {line}")

    print("\nOverall:", "OK" if all_ok else "ATTENTION")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
