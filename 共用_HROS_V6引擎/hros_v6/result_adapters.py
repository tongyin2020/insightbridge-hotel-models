from __future__ import annotations

from hros_v6.revenue_attribution_engine import RevenueAttributionEngine


def attach_revenue_attribution(
    result: dict,
    *,
    baseline_adr: float,
    baseline_occ: float,
    mare_adr: float,
    mare_occ: float,
    crm_incremental_value: float = 0.0,
    selfacq_incremental_value: float = 0.0,
    rooms_available: float = 1.0,
) -> dict:
    updated = dict(result or {})
    updated["revenue_attribution"] = RevenueAttributionEngine().attribute(
        baseline_adr=float(baseline_adr or 0.0),
        baseline_occ=float(baseline_occ or 0.0),
        mare_adr=float(mare_adr or 0.0),
        mare_occ=float(mare_occ or 0.0),
        crm_incremental_value=float(crm_incremental_value or 0.0),
        selfacq_incremental_value=float(selfacq_incremental_value or 0.0),
        rooms_available=float(rooms_available or 1.0),
    )
    return updated


def adapt_director_to_v6_result(result: dict, *, hotel_profile_version: str = "V6") -> dict:
    updated = dict(result or {})
    price = float(updated.get("crm_adjusted_price", 0.0) or 0.0)
    occupancy = float(updated.get("occupancy", 0.0) or 0.0)
    revpar = float(updated.get("elasticity_revpar", price * occupancy) or 0.0)
    lift_pct = float(updated.get("elasticity_lift_pct", 0.0) or 0.0)
    integration = float(updated.get("integration_score", 0.0) or 0.0)
    psrs = str(updated.get("psrs_status", "pending"))

    risk_score = {
        "synced": max(0.0, 35.0 - integration * 20.0),
        "pending": max(10.0, 55.0 - integration * 20.0),
        "error": max(35.0, 80.0 - integration * 10.0),
    }.get(psrs, 60.0)
    opportunity_score = round(min(100.0, integration * 100.0), 2)
    confidence = round(min(100.0, 40.0 + integration * 50.0), 2)

    updated.update({
        "recommended_price": round(price, 2),
        "predicted_occupancy": round(occupancy, 4),
        "predicted_revpar": round(revpar, 2),
        "expected_revenue_lift": f"{lift_pct:+.2f}%",
        "price_risk_score": round(risk_score, 2),
        "opportunity_score": opportunity_score,
        "rate_confidence": confidence,
        "hotel_profile_version": hotel_profile_version,
    })
    return updated


def adapt_selfacq_to_v6_result(
    result: dict,
    *,
    hotel_profile_version: str = "V6",
) -> dict:
    updated = dict(result or {})
    price = float(updated.get("direct_offer_price", 0.0) or 0.0)
    occupancy = float(updated.get("predicted_occupancy", updated.get("occupancy", 0.0)) or 0.0)
    predicted_revpar = price * occupancy if price and occupancy else 0.0
    lift_raw = updated.get("revpar_lift_vs_market", "+0.0%")
    try:
        lift_pct = float(str(lift_raw).replace("%", "").replace("+", ""))
    except ValueError:
        lift_pct = 0.0

    direct_advantage = float(updated.get("selfacq_v6_advantage", 0.0) or 0.0)
    risk_score = 35.0 if updated.get("direct_wins_vs_ota") else 65.0
    opportunity_score = round(max(0.0, min(100.0, direct_advantage / 10.0)), 2)
    confidence = 75.0 if updated.get("direct_wins_vs_ota") else 45.0

    updated.update({
        "recommended_price": round(price, 2),
        "predicted_revpar": round(predicted_revpar, 2),
        "expected_revenue_lift": f"{lift_pct:+.2f}%",
        "price_risk_score": round(risk_score, 2),
        "opportunity_score": opportunity_score,
        "rate_confidence": confidence,
        "hotel_profile_version": hotel_profile_version,
    })
    return updated
