from .revenue_decision_layer_v6 import RevenueDecisionLayerV6
from .elasticity_engine_v6 import ElasticityProfile
from .schemas_v6 import MarketSignal

def apply_v6_to_mare_output(old_result: dict, *, star_rating: int, market_price: float, base_occ: float,
                            floor_price: float, ceiling_price: float, demand_state: str = "NORMAL",
                            competitor_price: float = None, data_quality: float = 1.0) -> dict:
    """Drop-in adapter: preserve old engine output keys but replace price/lift with V6 decision."""
    layer = RevenueDecisionLayerV6()
    profile = ElasticityProfile(star_segment=f"{star_rating}_STAR")
    signal = MarketSignal(market_price=market_price, competitor_price=competitor_price, demand_state=demand_state, data_quality=data_quality)
    decision = layer.optimize(star_rating=star_rating, market_signal=signal, base_occ=base_occ,
                              floor_price=floor_price, ceiling_price=ceiling_price, elasticity_profile=profile)
    updated = dict(old_result or {})
    updated.update({
        "recommended_price": decision.recommended_price,
        "predicted_occupancy": decision.predicted_occupancy,
        "predicted_revpar": decision.predicted_revpar,
        "expected_revenue_lift": f"{decision.lift_pct:+.2f}%",
        "price_risk_score": decision.risk_score,
        "opportunity_score": decision.opportunity_score,
        "rate_confidence": decision.confidence,
        "v6_reason": decision.reason,
    })
    return updated
