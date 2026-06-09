from __future__ import annotations

import fcntl
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).resolve().parent / "mare_ml_state.json"

_PRIOR_ALPHA = 2.0
_PRIOR_BETA = 2.0

_CANDIDATES = {
    "elasticity_multiplier": [0.90, 0.97, 1.00, 1.03, 1.10],
    "premium_delta": [-0.05, -0.02, 0.00, 0.02, 0.05],
    "occupancy_delta": [-0.04, -0.02, 0.00, 0.02, 0.04],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_key(value: float) -> str:
    return f"{value:.4f}"


def _default_arm_state() -> dict[str, dict[str, float]]:
    return {
        arm: {
            _candidate_key(v): {"alpha": _PRIOR_ALPHA, "beta": _PRIOR_BETA}
            for v in values
        }
        for arm, values in _CANDIDATES.items()
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "meta": {
                "version": 1,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "description": "MARE profile-level contextual bandit state",
            },
            "profiles": {},
        }
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "meta": {
                "version": 1,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "description": "MARE profile-level contextual bandit state (recovered)",
            },
            "profiles": {},
        }


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(tmp, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        state["meta"]["updated_at"] = _utc_now()
        fh.seek(0)
        fh.truncate(0)
        fh.write(json.dumps(state, ensure_ascii=False, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    os.replace(str(tmp), str(STATE_PATH))


@dataclass(frozen=True)
class MareMLDecision:
    profile_name: str
    elasticity_multiplier: float
    premium_delta: float
    occupancy_delta: float
    state_version: int


def _ensure_profile(state: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = state.setdefault("profiles", {})
    if profile_name not in profiles:
        profiles[profile_name] = {
            "bandit": _default_arm_state(),
            "stats": {
                "decisions": 0,
                "successes": 0,
                "failures": 0,
                "last_success_at": None,
                "last_failure_at": None,
            },
        }
    return profiles[profile_name]


def choose_adjustments(profile_name: str, *, deterministic: bool = False) -> MareMLDecision:
    state = _load_state()
    profile = _ensure_profile(state, profile_name)
    bandit = profile["bandit"]

    chosen: dict[str, float] = {}
    for arm_name, candidates in bandit.items():
        if deterministic:
            best_key = max(
                candidates,
                key=lambda k: candidates[k]["alpha"] / max(candidates[k]["alpha"] + candidates[k]["beta"], 1e-9),
            )
        else:
            best_key = max(
                candidates,
                key=lambda k: random.betavariate(candidates[k]["alpha"], candidates[k]["beta"]),
            )
        chosen[arm_name] = float(best_key)

    return MareMLDecision(
        profile_name=profile_name,
        elasticity_multiplier=chosen["elasticity_multiplier"],
        premium_delta=chosen["premium_delta"],
        occupancy_delta=chosen["occupancy_delta"],
        state_version=int(state.get("meta", {}).get("version", 1)),
    )


def update_adjustments(decision: MareMLDecision, success: bool) -> None:
    state = _load_state()
    profile = _ensure_profile(state, decision.profile_name)
    bandit = profile["bandit"]

    updates = {
        "elasticity_multiplier": decision.elasticity_multiplier,
        "premium_delta": decision.premium_delta,
        "occupancy_delta": decision.occupancy_delta,
    }
    for arm_name, value in updates.items():
        key = _candidate_key(value)
        arm_state = bandit.setdefault(arm_name, {})
        arm_state.setdefault(key, {"alpha": _PRIOR_ALPHA, "beta": _PRIOR_BETA})
        if success:
            arm_state[key]["alpha"] += 1.0
        else:
            arm_state[key]["beta"] += 1.0

    stats = profile.setdefault("stats", {})
    stats["decisions"] = int(stats.get("decisions", 0)) + 1
    if success:
        stats["successes"] = int(stats.get("successes", 0)) + 1
        stats["last_success_at"] = _utc_now()
    else:
        stats["failures"] = int(stats.get("failures", 0)) + 1
        stats["last_failure_at"] = _utc_now()

    _save_state(state)


def score_mare_outcome(result: dict[str, Any], anomalies: list[str] | tuple[str, ...] | None) -> bool:
    anomalies = [str(a) for a in (anomalies or []) if a]
    if any("EXCEPTION" in a or "CRITICAL" in a for a in anomalies):
        return False
    if any("price_floor_ceiling" in a or "gm_approval" in a for a in anomalies):
        return False

    try:
        total_lift = float(result.get("true_lift_pct", 0.0) or 0.0)
    except Exception:
        total_lift = 0.0
    if not total_lift:
        lift_txt = str(result.get("expected_revenue_lift") or "0").replace("%", "")
        try:
            total_lift = float(lift_txt)
        except Exception:
            total_lift = 0.0

    pred_occ = float(result.get("predicted_occupancy") or 0.0)
    target_occ = float(result.get("optimal_occupancy_target") or result.get("optimal_occupancy") or 0.0)
    if target_occ <= 0:
        target_occ = pred_occ

    occ_gap = abs(pred_occ - target_occ)
    room_lift_txt = str(result.get("expected_room_revenue_lift") or "0").replace("%", "")
    try:
        room_lift = float(room_lift_txt)
    except Exception:
        room_lift = 0.0

    return bool(total_lift > 0.0 and occ_gap <= 0.12 and room_lift > -8.0)


def get_profile_snapshot(profile_name: str) -> dict[str, Any]:
    state = _load_state()
    profile = _ensure_profile(state, profile_name)
    summary: dict[str, Any] = {"profile_name": profile_name, "stats": profile.get("stats", {})}
    for arm_name, candidates in profile.get("bandit", {}).items():
        best_key = max(
            candidates,
            key=lambda k: candidates[k]["alpha"] / max(candidates[k]["alpha"] + candidates[k]["beta"], 1e-9),
        )
        summary[arm_name] = {
            "best_estimate": float(best_key),
            "posterior_mean": round(
                candidates[best_key]["alpha"] / max(candidates[best_key]["alpha"] + candidates[best_key]["beta"], 1e-9),
                4,
            ),
        }
    return summary
