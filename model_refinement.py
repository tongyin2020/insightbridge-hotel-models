from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
OUTCOME_DB_PATH = REPORTS_DIR / "director_outcome_store.db"

EXTREME_CATEGORIES = {"peak", "crisis", "market_shock", "stress"}
NORMAL_CATEGORIES = {"normal"}


def _round_to_step(value: float, step: int = 5) -> int:
    if step <= 0:
        return int(round(value))
    return int(round(value / step) * step)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def ensure_outcome_store() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(OUTCOME_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS director_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_system TEXT,
            run_at TEXT,
            sim_hour INTEGER,
            hotel_id TEXT,
            scenario_name TEXT,
            scenario_category TEXT,
            base_price REAL,
            crm_adjusted_price REAL,
            integration_score REAL,
            crm_matched INTEGER,
            psrs_status TEXT,
            whatsapp_delivered INTEGER,
            upsell_revenue REAL,
            ota_commission_saved REAL,
            outcome_score REAL,
            outcome_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@dataclass(frozen=True)
class DirectorFeedbackSignal:
    avg_score: float
    samples: int
    match_rate_delta: float
    discount_bias_delta: float


def get_director_feedback_signal(hotel_id: str, lookback: int = 24) -> DirectorFeedbackSignal:
    ensure_outcome_store()
    conn = sqlite3.connect(OUTCOME_DB_PATH)
    row = conn.execute(
        """
        SELECT
            AVG(outcome_score),
            COUNT(*)
        FROM (
            SELECT outcome_score
            FROM director_outcomes
            WHERE hotel_id = ?
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (hotel_id, lookback),
    ).fetchone()
    conn.close()

    avg_score = _to_float(row[0], 0.5) if row else 0.5
    samples = int(row[1] or 0) if row else 0
    if samples < 6:
        return DirectorFeedbackSignal(avg_score=avg_score, samples=samples, match_rate_delta=0.0, discount_bias_delta=0.0)

    if avg_score >= 0.72:
        return DirectorFeedbackSignal(avg_score, samples, match_rate_delta=0.06, discount_bias_delta=0.01)
    if avg_score <= 0.40:
        return DirectorFeedbackSignal(avg_score, samples, match_rate_delta=-0.08, discount_bias_delta=-0.015)
    if avg_score <= 0.55:
        return DirectorFeedbackSignal(avg_score, samples, match_rate_delta=-0.03, discount_bias_delta=-0.005)
    return DirectorFeedbackSignal(avg_score, samples, match_rate_delta=0.02, discount_bias_delta=0.0)


def record_director_outcome(
    *,
    source_system: str,
    run_at: str,
    sim_hour: int,
    hotel_id: str,
    scenario_name: str,
    scenario_category: str,
    base_price: float,
    crm_adjusted_price: float,
    integration_score: float,
    crm_matched: bool,
    psrs_status: str,
    whatsapp_delivered: bool,
    upsell_revenue: float,
    ota_commission_saved: float,
    payload: dict[str, Any],
) -> float:
    ensure_outcome_store()

    revenue_bonus = min(1.0, (_to_float(upsell_revenue) + _to_float(ota_commission_saved)) / max(_to_float(base_price) * 0.22, 1.0))
    synced_score = 1.0 if psrs_status == "synced" else 0.45 if psrs_status == "pending" else 0.0
    price_discipline = 1.0
    if base_price > 0 and crm_adjusted_price < base_price * 0.88:
        price_discipline = 0.35
    elif base_price > 0 and crm_adjusted_price < base_price * 0.93:
        price_discipline = 0.70

    outcome_score = round(
        min(
            1.0,
            integration_score * 0.52
            + (1.0 if crm_matched else 0.0) * 0.12
            + (1.0 if whatsapp_delivered else 0.0) * 0.08
            + synced_score * 0.13
            + revenue_bonus * 0.10
            + price_discipline * 0.05,
        ),
        4,
    )

    conn = sqlite3.connect(OUTCOME_DB_PATH)
    conn.execute(
        """
        INSERT INTO director_outcomes (
            source_system, run_at, sim_hour, hotel_id, scenario_name, scenario_category,
            base_price, crm_adjusted_price, integration_score, crm_matched, psrs_status,
            whatsapp_delivered, upsell_revenue, ota_commission_saved, outcome_score, outcome_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_system,
            run_at,
            sim_hour,
            hotel_id,
            scenario_name,
            scenario_category,
            base_price,
            crm_adjusted_price,
            integration_score,
            int(bool(crm_matched)),
            psrs_status,
            int(bool(whatsapp_delivered)),
            upsell_revenue,
            ota_commission_saved,
            outcome_score,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()
    return outcome_score


def apply_selfacq_profit_guard(
    result: dict[str, Any],
    hotel: dict[str, Any],
    scenario: Any,
) -> dict[str, Any]:
    direct_offer_price = _to_float(result.get("direct_offer_price"))
    ota_standard_price = _to_float(result.get("ota_standard_price"))
    ota_net_revenue = _to_float(result.get("ota_net_revenue"))
    direct_net_revenue = _to_float(result.get("direct_net_revenue"), direct_offer_price)
    loyalty = str(result.get("loyalty_tier") or getattr(scenario, "loyalty_tier", "none"))
    category = str(getattr(scenario, "category", "normal"))

    category_multiplier = {
        "normal": 1.0,
        "peak": 0.85,
        "crisis": 1.35,
        "market_shock": 1.25,
        "stress": 1.15,
    }.get(category, 1.0)
    loyalty_base_cost = {"platinum": 18.0, "gold": 24.0, "silver": 42.0, "bronze": 58.0, "none": 88.0}.get(loyalty, 88.0)
    acquisition_cost = round(loyalty_base_cost * category_multiplier, 1)

    direct_net_profit = round(direct_net_revenue - acquisition_cost, 1)
    ota_net_profit = round(ota_net_revenue, 1)
    attractive_ceiling = ota_standard_price * (0.995 if loyalty in ("platinum", "gold") else 0.985)
    target_profit = ota_net_profit * 0.98
    required_direct_price = _round_to_step(target_profit + acquisition_cost)

    guard_action = "pass"
    if direct_net_profit < target_profit:
        if required_direct_price <= attractive_ceiling:
            direct_offer_price = max(direct_offer_price, required_direct_price)
            direct_net_revenue = float(direct_offer_price)
            direct_net_profit = round(direct_net_revenue - acquisition_cost, 1)
            guard_action = "raise_price"
        else:
            direct_offer_price = min(direct_offer_price, _round_to_step(attractive_ceiling))
            direct_net_revenue = float(direct_offer_price)
            direct_net_profit = round(direct_net_revenue - acquisition_cost, 1)
            guard_action = "bundle_only"

    direct_advantage = round(direct_net_profit - ota_net_profit, 1)
    direct_wins = direct_net_profit >= ota_net_profit * 0.98 and guard_action != "bundle_only"

    result.update(
        {
            "direct_offer_price": int(direct_offer_price),
            "direct_net_revenue": round(direct_net_revenue, 0),
            "direct_net_profit_after_cac": direct_net_profit,
            "ota_net_profit": ota_net_profit,
            "acquisition_cost": acquisition_cost,
            "direct_advantage_after_cac": direct_advantage,
            "direct_wins_vs_ota": direct_wins,
            "selfacq_guard_action": guard_action,
            "selfacq_profit_guard_passed": direct_net_profit >= target_profit and guard_action != "bundle_only",
        }
    )
    return result


def parse_pct_text(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).replace("%", "").replace("+", "").strip()
    return _to_float(text)


def compute_dual_score(
    *,
    profit_uplift_pct: float,
    failure_rate_pct: float,
    anomaly_rate_pct: float,
    real_data_ratio: float,
    explainability_ratio: float,
    runtime_cost_ratio: float,
) -> float:
    stability_score = max(0.0, 100.0 - failure_rate_pct * 1.4)
    anomaly_score = max(0.0, 100.0 - anomaly_rate_pct)
    profit_score = max(0.0, min(100.0, 50.0 + profit_uplift_pct * 4.0))
    real_score = max(0.0, min(100.0, real_data_ratio * 100.0))
    explain_score = max(0.0, min(100.0, explainability_ratio * 100.0))
    cost_score = max(0.0, min(100.0, runtime_cost_ratio * 100.0))

    return round(
        stability_score * 0.30
        + profit_score * 0.25
        + anomaly_score * 0.15
        + real_score * 0.15
        + explain_score * 0.10
        + cost_score * 0.05,
        2,
    )
