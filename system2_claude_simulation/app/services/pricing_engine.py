"""MARE v19.1 Pricing Engine, adjusted for Macau 3-5 star hotels.

This working copy folds in the pre-launch corrections identified in audit:
1. Re-center priors on Macau midscale hotels rather than mixed-city demo logic.
2. Smooth hard threshold jumps in demand and occupancy adjustments.
3. Clamp unstable inputs before they can create pathological raw prices.
4. Keep recommendation traces explainable for later real-data calibration.
"""

from __future__ import annotations

import hashlib
import json
import os
from math import floor
from pathlib import Path
from typing import Any, Optional

DEFAULT_WEIGHTS = {
    "season_multipliers": {
        "off_peak": 0.92,
        "shoulder": 1.00,
        "peak": 1.08,
        "super_peak": 1.15,
    },
    # Macau 3-star and above prior:
    # border / road traffic, weekend, competitor pressure and booking pace matter
    # more than high-end event monetisation or weather noise.
    "demand_weights": {
        "holiday": 0.16,
        "event_ticket_sales": 0.10,
        "weekend": 0.14,
        "border_flow": 0.18,
        "visitors_stats": 0.08,
        "flight_ferry": 0.05,
        "zhuhai_saturation": 0.12,
        "ota_booking_pace": 0.07,   # ↓0.05 腾出DSEC权重（与top-level引擎对齐）
        "weather": 0.03,            # ↓0.02 腾出DSEC权重
        "dsec_market_occ": 0.07,    # 补充(2026-06-01): 澳门统计局月度入住率信号
    },
}

WEIGHTS_PATH = Path(os.getenv(
    "MODEL_WEIGHTS_PATH",
    # 修正(2026-06-01): 本地Mac环境无/app路径; 使用相对于此文件的data目录
    str(Path(__file__).parent.parent.parent / "data" / "model_weights.json")
))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    ratio = _clamp((x - x0) / (x1 - x0), 0.0, 1.0)
    return y0 + (y1 - y0) * ratio


def load_weights():
    if WEIGHTS_PATH.exists():
        try:
            loaded = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
            if "demand_weights" in loaded and "season_multipliers" in loaded:
                return loaded
        except Exception:
            pass
    WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_PATH.write_text(json.dumps(DEFAULT_WEIGHTS, indent=2), encoding="utf-8")
    return DEFAULT_WEIGHTS


def demand_score(data):
    weights = load_weights()["demand_weights"]
    score = 0.0
    contributions = []
    for key, weight in weights.items():
        raw_val = getattr(data, key, 0.0)
        val = _clamp(raw_val, -1.0, 1.0)
        contrib = val * weight
        score += contrib
        contributions.append(
            {
                "name": key,
                "raw_value": raw_val,
                "value_used": val,
                "contribution": round(contrib, 4),
            }
        )
    return round(score, 4), contributions


def demand_state(score):
    if score > 0.30:
        return "HIGH"
    if score < -0.12:
        return "LOW"
    return "NORMAL"


def demand_adjustment(score):
    # Smooth ramps replace the old 0 / +8% / +12% discontinuities.
    if score <= -0.22:
        return -0.06
    if score < -0.06:
        return round(_lerp(score, -0.22, -0.06, -0.06, 0.0), 4)
    if score <= 0.18:
        return 0.0
    if score < 0.42:
        return round(_lerp(score, 0.18, 0.42, 0.0, 0.12), 4)
    return 0.12


def competition_adjustment(state, competitor_price, our_base, competitor_availability):
    # Midscale Macau pricing is competitor-sensitive, but we cap the adjustment
    # so a bad scrape cannot dominate the entire recommendation.
    if competitor_price is None or competitor_price <= 0:
        return 0.0

    safe_base = max(our_base, 1)
    safe_comp = _clamp(float(competitor_price), safe_base * 0.60, safe_base * 1.60)
    gap_ratio = (safe_comp - safe_base) / safe_base
    availability = _clamp(float(competitor_availability or 0.0), 0.0, 1.0)
    availability_boost = 0.015 if availability > 0.55 else 0.0
    weight = 0.25 if state == "HIGH" else 0.90 if state == "LOW" else 0.65
    return round(_clamp(gap_ratio * weight + availability_boost, -0.15, 0.15), 4)


def profit_adjustment(occupancy, elasticity_signal):
    occ = _clamp(float(occupancy), 0.0, 1.0)
    elasticity = _clamp(float(elasticity_signal or 0.0), -1.0, 1.0)

    if occ <= 0.50:
        return round(-0.08 - 0.02 * max(elasticity, 0.0), 4)
    if occ < 0.68:
        return round(_lerp(occ, 0.50, 0.68, -0.08, 0.00), 4)
    if occ < 0.82:
        return round(_lerp(occ, 0.68, 0.82, 0.00, 0.05), 4)
    if occ < 0.95:
        premium = 0.05 + 0.03 * _lerp(occ, 0.82, 0.95, 0.0, 1.0)
        return round(premium + 0.01 * max(elasticity, 0.0), 4)
    return round(0.08 + 0.01 * max(elasticity, 0.0), 4)


def crm_adjustment(
    guest_segment: str = "unknown",
    avg_clv: float = 0,
    repurchase_probability: float = 0.5,
    price_sensitivity: str = "medium",
    churn_risk: float = 0.0,
    loyalty_tier: str = "",
) -> tuple[float, str]:
    # In the Macau 3-star and above market, CRM should protect relationships lightly,
    # not dominate price formation.
    adjustment = 0.0
    reasons: list[str] = []

    tier_lower = loyalty_tier.lower().strip()
    segment_lower = guest_segment.lower().strip()
    sensitivity_lower = price_sensitivity.lower().strip()
    churn_risk = _clamp(float(churn_risk or 0.0), 0.0, 1.0)
    repurchase_probability = _clamp(float(repurchase_probability or 0.0), 0.0, 1.0)
    avg_clv = max(float(avg_clv or 0.0), 0.0)

    if segment_lower == "corporate":
        return -0.01, "Corporate segment: light contracted-rate protection."

    if avg_clv > 4500 and repurchase_probability > 0.45 and churn_risk > 0.35:
        discount = -0.02 - 0.03 * churn_risk
        adjustment += discount
        reasons.append(f"High-value repeat guest with churn risk: {discount:+.1%}")
    elif tier_lower in ("vip", "platinum", "diamond"):
        discount = -0.02 - 0.01 * min(avg_clv / 15000, 1.0)
        adjustment += discount
        reasons.append(f"{loyalty_tier} loyalty discount: {discount:+.1%}")
    elif tier_lower == "gold":
        adjustment += -0.015
        reasons.append("Gold loyalty discount: -1.5%")

    if segment_lower in ("new", "walk_in", "ota") and sensitivity_lower in ("low", "very_low"):
        premium = 0.01 + 0.015 * (1.0 - repurchase_probability)
        adjustment += premium
        reasons.append(f"Low-sensitivity transient guest: {premium:+.1%}")

    if not reasons:
        reasons.append("CRM signal treated as minor modifier in midscale market.")

    return round(_clamp(adjustment, -0.05, 0.03), 4), " | ".join(reasons)


def supply_scarcity_adjustment(
    remaining_inventory: int,
    total_rooms: int,
    booking_velocity_24h: float = 0,
    days_to_arrival: int = 30,
    cancellation_rate: float = 0.1,
) -> tuple[float, str]:
    if total_rooms <= 0:
        return 0.0, "Invalid total_rooms; no scarcity adjustment."

    safe_total = max(int(total_rooms), 1)
    safe_remaining = int(_clamp(float(remaining_inventory), 0.0, float(safe_total)))
    scarcity_ratio = safe_remaining / safe_total
    velocity_pressure = _clamp(float(booking_velocity_24h) / max(safe_total * 0.08, 1), 0.0, 1.0)
    urgency = _clamp((1.0 / max(int(days_to_arrival), 1)) * 8, 0.0, 1.0)
    cancel_buffer = _clamp(float(cancellation_rate or 0.0), 0.0, 0.4)

    scarcity_index = (
        0.55 * (1.0 - scarcity_ratio)
        + 0.20 * velocity_pressure
        + 0.15 * urgency
        - 0.10 * cancel_buffer
    )
    scarcity_index = _clamp(scarcity_index, 0.0, 1.0)

    if scarcity_ratio < 0.10:
        adjustment = 0.06 + 0.05 * scarcity_index
        label = "Critical"
    elif scarcity_ratio < 0.25:
        adjustment = 0.025 + 0.04 * scarcity_index
        label = "Scarce"
    elif scarcity_ratio <= 0.50:
        adjustment = 0.02 * scarcity_index
        label = "Balanced"
    else:
        adjustment = -0.015 - 0.025 * (1.0 - scarcity_index)
        label = "Abundant"

    reason = (
        f"Supply {label} (remaining={safe_remaining}/{safe_total}, "
        f"velocity={booking_velocity_24h:.0f}/24h, days_out={days_to_arrival}, "
        f"cancel_rate={cancel_buffer:.0%}): scarcity_index={scarcity_index:.2f}, "
        f"adjustment={adjustment:+.1%}"
    )
    return round(_clamp(adjustment, -0.06, 0.12), 4), reason


def apply_guardrails(price, floor_price=750, ceiling_price=1015):
    return int(floor(max(floor_price, min(ceiling_price, price))))


def compute_dynamic_ceiling(
    static_ceiling: float,
    upper_tier_adr: Optional[float] = None,
    neighborhood_availability: Optional[float] = None,
    same_day_demand_score: Optional[float] = None,
    event_density: Optional[float] = None,
) -> float:
    ceiling = float(static_ceiling)

    if upper_tier_adr is not None and upper_tier_adr > 0:
        tier_cap = upper_tier_adr * 0.88
        ceiling = min(ceiling * 1.10, tier_cap)

    availability = None if neighborhood_availability is None else _clamp(neighborhood_availability, 0.0, 1.0)
    if availability is not None and availability < 0.30:
        ceiling *= 1.0 + 0.06 * (1.0 - availability)

    demand = None if same_day_demand_score is None else _clamp(same_day_demand_score, 0.0, 1.0)
    if demand is not None and demand > 0.60:
        ceiling *= 1.0 + 0.05 * demand

    events = None if event_density is None else _clamp(event_density, 0.0, 1.0)
    if events is not None and events > 0.40:
        ceiling *= 1.0 + 0.04 * events

    return round(min(ceiling, static_ceiling * 1.15), 2)


def expected_lift(state, occupancy, season):
    occ = _clamp(float(occupancy), 0.0, 1.0)
    if state == "HIGH" and occ > 0.82:
        return "+6.5%"
    if state == "LOW":
        return "+1.8%"
    if season in ("peak", "super_peak"):
        return "+7.2%"
    return "+4.6%"


def confidence(score, occupancy):
    strength = abs(score)
    occ = _clamp(float(occupancy), 0.0, 1.0)
    if strength > 0.30 or occ > 0.90:
        return "High"
    if strength > 0.14:
        return "Medium"
    return "Low"


def _weights_hash() -> str:
    raw = json.dumps(load_weights(), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def recommend(data, hotel_settings=None):
    from app.services.bundle_engine import generate_bundle_offers
    from app.services.fair_pricing import FairPricingEngine
    from app.services.policy_engine import PolicyEngine, PricingContext
    from app.services.shadow_testing import append_shadow_history, generate_shadow_recommendation

    weights = load_weights()
    reasons_list: list[dict] = []

    seasonal_base = data.base_price * weights["season_multipliers"].get(data.season, 1.0)

    score, breakdown = demand_score(data)
    state = demand_state(score)
    d_adj = demand_adjustment(score)
    reasons_list.append({"title": "Demand Engine", "detail": f"Demand state {state} with score {score:.2f}."})

    remaining_inventory = getattr(data, "remaining_inventory", 0)
    total_rooms = getattr(data, "total_rooms", 0)
    booking_velocity_24h = getattr(data, "booking_velocity_24h", 0.0)
    days_to_arrival = getattr(data, "days_to_arrival", 30)
    cancellation_rate = getattr(data, "cancellation_rate", 0.1)

    if total_rooms > 0:
        s_adj, s_reason = supply_scarcity_adjustment(
            remaining_inventory=remaining_inventory,
            total_rooms=total_rooms,
            booking_velocity_24h=booking_velocity_24h,
            days_to_arrival=days_to_arrival,
            cancellation_rate=cancellation_rate,
        )
        safe_remaining = int(_clamp(float(remaining_inventory), 0.0, float(total_rooms)))
        s_index = safe_remaining / max(total_rooms, 1)
    else:
        s_adj, s_reason = 0.0, "No inventory data provided; scarcity skipped."
        s_index = 0.5
    reasons_list.append({"title": "Supply Scarcity", "detail": s_reason})

    competitor_availability = _clamp(getattr(data, "competitor_availability", 0.0), 0.0, 1.0)
    current_occupancy = _clamp(getattr(data, "current_occupancy", 0.0), 0.0, 1.0)
    elasticity_signal = _clamp(getattr(data, "elasticity_signal", 0.0), -1.0, 1.0)

    c_adj = competition_adjustment(state, data.competitor_price, seasonal_base, competitor_availability)
    reasons_list.append({"title": "Market Positioning", "detail": f"Competitor price MOP {data.competitor_price:.0f} influences market alignment."})

    crm_adj, crm_reason = crm_adjustment(
        guest_segment=getattr(data, "guest_segment", "unknown"),
        avg_clv=getattr(data, "avg_clv", 0.0),
        repurchase_probability=getattr(data, "repurchase_probability", 0.5),
        price_sensitivity=getattr(data, "price_sensitivity", "medium"),
        churn_risk=getattr(data, "churn_risk", 0.0),
        loyalty_tier=getattr(data, "loyalty_tier", ""),
    )
    reasons_list.append({"title": "CRM Engine", "detail": crm_reason})

    p_adj = profit_adjustment(current_occupancy, elasticity_signal)
    reasons_list.append({"title": "Profit Engine", "detail": f"Occupancy at {current_occupancy*100:.0f}% adjusts for revenue optimization."})

    raw_price = seasonal_base * (1 + d_adj + s_adj + c_adj + crm_adj + p_adj)

    fair_engine = FairPricingEngine()
    fair_context = {
        "previous_price": getattr(data, "previous_price", 0),
        "season": data.season,
        "avg_30d_price": getattr(data, "avg_30d_price", 0),
        "historical_avg": getattr(data, "historical_avg", 0),
        "max_deviation_pct": getattr(data, "max_deviation_pct", 18.0),
        "loyalty_tier": getattr(data, "loyalty_tier", ""),
        "customer_historical_rate": getattr(data, "customer_historical_rate", 0),
    }
    fairness_report = fair_engine.evaluate(raw_price, fair_context)
    fair_price = fairness_report.get("final_fair_price", raw_price)
    reasons_list.append(
        {
            "title": "Fair Pricing",
            "detail": "Price adjusted by fairness engine to protect customer trust."
            if fairness_report.get("any_adjustment_applied")
            else "Price passed all fairness checks.",
        }
    )

    floor_price = hotel_settings.floor_price if hotel_settings else 750
    static_ceiling = hotel_settings.ceiling_price if hotel_settings else 1015

    dyn_ceiling = compute_dynamic_ceiling(
        static_ceiling=static_ceiling,
        upper_tier_adr=getattr(data, "upper_tier_adr", None),
        neighborhood_availability=getattr(data, "neighborhood_availability", None),
        same_day_demand_score=getattr(data, "same_day_demand_score", None),
        event_density=getattr(data, "event_density", None),
    )
    ceiling_used = dyn_ceiling if dyn_ceiling != static_ceiling else static_ceiling

    policy_engine = PolicyEngine()
    policy_ctx = PricingContext(
        proposed_price=fair_price,
        base_price=data.base_price,
        floor_price=floor_price,
        ceiling_price=static_ceiling,
        dynamic_ceiling=dyn_ceiling if dyn_ceiling != static_ceiling else None,
        competitor_price=max(float(data.competitor_price), 0.0),
        current_occupancy=current_occupancy,
        demand_score=score,
        demand_state=state,
        season=data.season,
        guest_satisfaction=getattr(data, "guest_satisfaction", None),
        data_freshness_minutes=getattr(data, "data_freshness_minutes", None),
        ota_prices=getattr(data, "ota_prices", None),
        hotel_settings=hotel_settings,
    )
    guardrail_report = policy_engine.evaluate(policy_ctx)
    final_price = guardrail_report.final_price or apply_guardrails(fair_price, floor_price, ceiling_used)

    violation_names = [v.rule_name for v in guardrail_report.violations]
    reasons_list.append(
        {
            "title": "Guardrails",
            "detail": (
                f"Policy engine evaluated {len(policy_engine.rules)} rules. "
                f"Final output clipped between MOP {int(floor_price)} and MOP {int(ceiling_used)}."
                + (f" Violations: {', '.join(violation_names)}." if violation_names else "")
            ),
        }
    )

    bundle_offers_raw = generate_bundle_offers(
        demand_state=state,
        occupancy_rate=current_occupancy,
        season=data.season,
        base_rate=final_price,
        scarcity_index=1.0 - s_index if total_rooms > 0 else 0.5,
    )
    bundle_offers = [b.to_dict() for b in bundle_offers_raw]
    if bundle_offers:
        reasons_list.append({"title": "Bundle Engine", "detail": f"Generated {len(bundle_offers)} bundle offer(s) for conversion support."})

    shadow_price: Optional[int] = None
    try:
        shadow_result = generate_shadow_recommendation(
            data=data,
            hotel_settings=hotel_settings,
            production_price=final_price,
        )
        shadow_price = shadow_result.shadow_price
        append_shadow_history(
            {
                "hotel_id": data.hotel_id,
                "production_price": final_price,
                "shadow_price": shadow_price,
                "price_delta": shadow_result.price_delta,
                "timestamp": shadow_result.timestamp,
            }
        )
    except Exception:
        pass

    recommendation_log = {
        "hotel_id": data.hotel_id,
        "market_segment": "macau_3star_plus",
        "season": data.season,
        "base_price": data.base_price,
        "demand_score": score,
        "demand_state": state,
        "seasonal_base": seasonal_base,
        "demand_adjustment": d_adj,
        "competition_adjustment": c_adj,
        "profit_adjustment": p_adj,
        "raw_price": raw_price,
        "floor_price": floor_price,
        "ceiling_price": static_ceiling,
        "dynamic_ceiling": dyn_ceiling if dyn_ceiling != static_ceiling else None,
        "recommended_price": final_price,
        "confidence": confidence(score, current_occupancy),
        "expected_lift": expected_lift(state, current_occupancy, data.season),
        "shadow_price": shadow_price,
        "model_weights_hash": _weights_hash(),
        "guardrail_violations": violation_names,
        "factor_breakdown": breakdown[:8],
    }

    # ── HROS V5 字段追加 ─────────────────────────────────────────────
    result_dict = {
        "hotel_id": data.hotel_id,
        "market_segment": "macau_3star_plus",
        "season": data.season,
        "demand_score": score,
        "demand_state": state,
        "recommended_price": final_price,
        "expected_revenue_lift": expected_lift(state, current_occupancy, data.season),
        "confidence": confidence(score, current_occupancy),
        "summary": f"Recommended price is MOP {final_price}, optimized for Macau 3-star and above revenue management.",
        "meta": (
            f"Seasonal base MOP {seasonal_base:.0f} -> raw MOP {raw_price:.0f} "
            f"-> fair MOP {fair_price:.0f} -> final MOP {final_price}"
        ),
        "dynamic_ceiling": dyn_ceiling if dyn_ceiling != static_ceiling else None,
        "guardrail_report": guardrail_report.to_dict(),
        "shadow_price": shadow_price,
        "recommendation_log": recommendation_log,
        "scarcity_index": round(1.0 - s_index, 2) if total_rooms > 0 else None,
        "scarcity_adjustment": s_adj,
        "crm_adjustment": crm_adj,
        "crm_detail": crm_reason,
        "fairness_report": fairness_report,
        "bundle_offers": bundle_offers,
        "reasons": reasons_list,
        "factor_breakdown": breakdown[:8],
    }


def weight_table():
    weights = load_weights()["demand_weights"]
    return [{"factor": k, "weight": f"{v:.3f}"} for k, v in weights.items()]


# ══════════════════════════════════════════════════════════════════════════════
# HROS V5 集成：在 recommend() 返回的 result 中追加 V5 评分字段
# 调用方式：在 recommend() 函数最后调用 apply_hros_v5_fields(result, data)
# ══════════════════════════════════════════════════════════════════════════════

def apply_hros_v5_fields(result: dict, data) -> dict:
    """
    在现有 result 中追加 HROS V5 新增字段。
    不修改原有字段，低侵入集成。
    """
    try:
        import sys, os
        _hros_path = os.path.join(os.path.dirname(__file__), "../../../../")
        _expert_path = os.path.expanduser(
            "~/Desktop/InsightBridge_HROS_V5_Final/共用_HROS_V5引擎")
        for _p in [_hros_path, _expert_path]:
            if _p not in sys.path:
                sys.path.insert(0, _p)

        from hros.risk_engine import calculate_price_risk
        from hros.opportunity_engine import calculate_opportunity_score

        market_price = float(getattr(data, "competitor_price", 0) or 0) or \
                       float(getattr(data, "base_price", 900))
        signals = {
            "event_density":      getattr(data, "event_ticket_sales", 0.0),
            "border_flow":        getattr(data, "border_flow", 0.0),
            "zhuhai_saturation":  getattr(data, "zhuhai_saturation", 0.0),
            "ota_booking_pace":   getattr(data, "ota_booking_pace", 0.5),
            "is_holiday":         bool(getattr(data, "holiday", 0)),
            "is_weekend":         bool(getattr(data, "weekend", 0)),
            "occupancy":          getattr(data, "current_occupancy", 0.72),
        }

        result["risk_score_v5"] = calculate_price_risk(
            price=result.get("recommended_price", market_price),
            market_price=market_price,
            predicted_occ=result.get("predicted_occupancy",
                                     getattr(data, "current_occupancy", 0.72)),
            ota_booking_pace=signals["ota_booking_pace"],
            competitor_price=getattr(data, "competitor_price", None),
            data_quality=getattr(data, "data_quality", 1.0),
        )
        result["opportunity_score_v5"] = calculate_opportunity_score(signals)
    except Exception as _e:
        result["risk_score_v5"] = None
        result["opportunity_score_v5"] = None
    return result
