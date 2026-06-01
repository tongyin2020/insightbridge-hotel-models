"""Shadow parameter testing for MARE v19.1.

Runs an alternative set of model weights ("shadow weights") through the same
pricing pipeline and compares the outcome against production.  Over time the
shadow parameters can be auto-evolved via small perturbations guided by
outcome gradients, then promoted to production when they outperform.
"""

from __future__ import annotations

import copy
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from math import floor
from pathlib import Path
from typing import Any, Optional

from app.services.pricing_engine import (
    competition_adjustment,
    confidence,
    demand_adjustment,
    demand_score as _demand_score_fn,
    demand_state,
    expected_lift,
    load_weights,
)

# ---------------------------------------------------------------------------
# Storage path for shadow weights (JSON sidecar next to production weights)
# ---------------------------------------------------------------------------

SHADOW_WEIGHTS_PATH = Path(
    os.getenv("SHADOW_WEIGHTS_PATH", "/app/data/shadow_weights.json")
)
SHADOW_HISTORY_PATH = Path(
    os.getenv("SHADOW_HISTORY_PATH", "/app/data/shadow_history.json")
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ShadowResult:
    """Result of a single shadow pricing run."""

    shadow_price: int
    shadow_weights: dict
    production_price: int
    price_delta: int
    demand_score: float
    demand_state: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class ShadowPerformance:
    """Comparison between shadow and production over a window."""

    hotel_id: str
    window_size: int = 0
    shadow_mean_revenue: Optional[float] = None
    production_mean_revenue: Optional[float] = None
    shadow_win_rate: Optional[float] = None  # fraction where shadow > prod
    recommendation: str = "hold"  # hold | promote | revert


# ---------------------------------------------------------------------------
# Shadow weight management
# ---------------------------------------------------------------------------

def _load_shadow_weights() -> dict:
    """Load shadow weights from disk, initialising from production if absent."""
    if SHADOW_WEIGHTS_PATH.exists():
        return json.loads(SHADOW_WEIGHTS_PATH.read_text(encoding="utf-8"))
    # Bootstrap from production weights with small perturbation
    weights = copy.deepcopy(load_weights())
    weights = _perturb_weights(weights, magnitude=0.03)
    _save_shadow_weights(weights)
    return weights


def _save_shadow_weights(weights: dict) -> None:
    SHADOW_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHADOW_WEIGHTS_PATH.write_text(
        json.dumps(weights, indent=2), encoding="utf-8"
    )


def _perturb_weights(weights: dict, magnitude: float = 0.02) -> dict:
    """Apply small random perturbation to demand_weights."""
    out = copy.deepcopy(weights)
    dw = out.get("demand_weights", {})
    for key in dw:
        delta = random.uniform(-magnitude, magnitude)
        dw[key] = max(0.0, min(1.0, dw[key] + delta))
    # Re-normalise so weights sum to ~1
    total = sum(dw.values())
    if total > 0:
        for key in dw:
            dw[key] = round(dw[key] / total, 4)
    out["demand_weights"] = dw
    return out


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def generate_shadow_recommendation(
    data: Any,
    hotel_settings: Any = None,
    production_price: int = 0,
) -> ShadowResult:
    """Run the pricing pipeline with shadow weights and return the result.

    Parameters
    ----------
    data : RecommendationRequest
        Same input used for the production recommendation.
    hotel_settings : HotelSetting | None
        Optional hotel-specific floor/ceiling.
    production_price : int
        The production recommended price (for delta calculation).
    """
    shadow_weights = _load_shadow_weights()

    # --- replicate pipeline with shadow weights ---
    season_mult = shadow_weights.get("season_multipliers", {}).get(data.season, 1.0)
    seasonal_base = data.base_price * season_mult

    # Demand score with shadow demand_weights
    dw = shadow_weights.get("demand_weights", {})
    score = 0.0
    for key, weight in dw.items():
        val = getattr(data, key, 0.0)
        score += val * weight

    state = demand_state(score)
    d_adj = demand_adjustment(score)
    c_adj = competition_adjustment(
        state, data.competitor_price, seasonal_base, data.competitor_availability
    )

    from app.services.pricing_engine import profit_adjustment
    p_adj = profit_adjustment(data.current_occupancy, data.elasticity_signal)

    raw_price = seasonal_base * (1 + d_adj + c_adj + p_adj)
    floor_price = hotel_settings.floor_price if hotel_settings else 750
    ceiling_price = hotel_settings.ceiling_price if hotel_settings else 1015
    shadow_price = int(floor(max(floor_price, min(ceiling_price, raw_price))))

    return ShadowResult(
        shadow_price=shadow_price,
        shadow_weights=shadow_weights,
        production_price=production_price,
        price_delta=shadow_price - production_price,
        demand_score=score,
        demand_state=state,
    )


def evaluate_shadow_performance(
    hotel_id: str,
    shadow_results: list[dict] | None = None,
) -> ShadowPerformance:
    """Compare shadow vs production outcomes.

    In a production system this would query the outcome log.  For now it
    reads from the shadow history file.
    """
    history = _load_shadow_history()
    entries = [e for e in history if e.get("hotel_id") == hotel_id]

    if not entries:
        return ShadowPerformance(hotel_id=hotel_id)

    # Simple heuristic: if shadow price was closer to the applied price more
    # often, shadow wins.
    wins = 0
    for entry in entries:
        applied = entry.get("applied_price")
        if applied is None:
            continue
        shadow_err = abs(entry.get("shadow_price", 0) - applied)
        prod_err = abs(entry.get("production_price", 0) - applied)
        if shadow_err < prod_err:
            wins += 1

    total = max(len(entries), 1)
    win_rate = wins / total

    recommendation = "hold"
    if win_rate > 0.60 and len(entries) >= 10:
        recommendation = "promote"
    elif win_rate < 0.30 and len(entries) >= 10:
        recommendation = "revert"

    return ShadowPerformance(
        hotel_id=hotel_id,
        window_size=len(entries),
        shadow_win_rate=round(win_rate, 4),
        recommendation=recommendation,
    )


def auto_evolve_shadow(hotel_id: str, magnitude: float = 0.02) -> dict:
    """Apply a small perturbation to shadow weights guided by outcome gradient.

    Returns the new shadow weights.
    """
    perf = evaluate_shadow_performance(hotel_id)

    current = _load_shadow_weights()

    if perf.recommendation == "revert":
        # Shadow is doing poorly -- reset to production with fresh perturbation
        new_weights = _perturb_weights(load_weights(), magnitude=magnitude)
    else:
        # Keep evolving from current shadow
        new_weights = _perturb_weights(current, magnitude=magnitude)

    _save_shadow_weights(new_weights)
    return new_weights


def promote_shadow_to_production() -> dict:
    """Replace production weights with current shadow weights.

    Returns the promoted weights.
    """
    from app.services.pricing_engine import WEIGHTS_PATH

    shadow = _load_shadow_weights()
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(shadow, indent=2), encoding="utf-8")
    # Reset shadow with fresh perturbation
    new_shadow = _perturb_weights(shadow, magnitude=0.03)
    _save_shadow_weights(new_shadow)
    return shadow


# ---------------------------------------------------------------------------
# Shadow history persistence (simple JSON file)
# ---------------------------------------------------------------------------

def _load_shadow_history() -> list[dict]:
    if SHADOW_HISTORY_PATH.exists():
        return json.loads(SHADOW_HISTORY_PATH.read_text(encoding="utf-8"))
    return []


def append_shadow_history(entry: dict) -> None:
    """Append a shadow comparison entry to the history file."""
    history = _load_shadow_history()
    history.append(entry)
    # Keep last 1000 entries
    history = history[-1000:]
    SHADOW_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHADOW_HISTORY_PATH.write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )
