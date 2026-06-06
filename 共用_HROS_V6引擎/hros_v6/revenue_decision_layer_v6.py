from .schemas_v6 import MarketSignal, RevenueDecisionV6
from .elasticity_engine_v6 import ElasticityEngineV6, ElasticityProfile

class RevenueDecisionLayerV6:
    """Discrete price search for RevPAR/TRevPAR optimization with demand-state guardrails."""
    def __init__(self, step_by_star=None):
        self.elasticity = ElasticityEngineV6()
        self.step_by_star = step_by_star or {"3": 10, "4": 10, "5": 50}

    def _step(self, star_rating: int) -> int:
        return self.step_by_star.get(str(star_rating), 10)

    def optimize(self, *, star_rating: int, market_signal: MarketSignal, base_occ: float,
                 floor_price: float, ceiling_price: float, elasticity_profile: ElasticityProfile,
                 ancillary_per_occ: float = 0.0, clv_per_occ: float = 0.0,
                 market_share_weight: float = 0.0) -> RevenueDecisionV6:
        market = max(1.0, market_signal.market_price)
        step = self._step(star_rating)
        best = None
        price = int(floor_price)
        while price <= ceiling_price:
            occ = self.elasticity.predict_occupancy(price, market, base_occ, elasticity_profile, market_signal.demand_state)
            revpar = price * occ
            trevpar = revpar + ancillary_per_occ * occ
            objective = trevpar + clv_per_occ * occ + market_share_weight * occ * market
            if best is None or objective > best["objective"]:
                best = {"price": price, "occ": occ, "revpar": revpar, "objective": objective, "trevpar": trevpar}
            price += step

        baseline_revpar = market * base_occ
        lift = (best["revpar"] - baseline_revpar) / baseline_revpar * 100 if baseline_revpar > 0 else 0.0
        risk = self._risk(best["price"], market, best["occ"], market_signal)
        opp = self._opportunity(market_signal)
        confidence = max(40.0, min(95.0, 95.0 - 0.30 * risk + 0.05 * market_signal.data_quality * 100))
        return RevenueDecisionV6(
            recommended_price=round(best["price"], 2),
            predicted_occupancy=round(best["occ"], 4),
            predicted_revpar=round(best["revpar"], 2),
            baseline_revpar=round(baseline_revpar, 2),
            lift_pct=round(lift, 2),
            risk_score=round(risk, 2),
            opportunity_score=round(opp, 2),
            confidence=round(confidence, 2),
            objective_value=round(best["objective"], 2),
            reason={"market_price": market, "trevpar": round(best["trevpar"], 2), "demand_state": market_signal.demand_state}
        )

    def _risk(self, price, market, occ, signal: MarketSignal) -> float:
        competitor = signal.competitor_price or market
        ref = (market + competitor) / 2
        premium = (price - ref) / ref
        raw = max(0, premium - 0.05) * 180 + max(0, 0.70 - occ) * 80 + max(0, 0.45 - signal.ota_booking_pace) * 60 + max(0, 0.60 - signal.data_quality) * 35
        return min(100.0, raw / 3.0)

    def _opportunity(self, signal: MarketSignal) -> float:
        score = signal.event_density * 22 + signal.border_flow * 18 + signal.zhuhai_saturation * 18 + signal.ota_booking_pace * 18
        return max(0.0, min(100.0, score))

    def validate_demand_state_logic(self, high_avg: float, normal_avg: float, tolerance_pct: float = 0.03) -> dict:
        """Flag the report issue: HIGH demand average price should not be materially lower than NORMAL unless strategy says so."""
        if normal_avg <= 0:
            return {"status": "insufficient_data"}
        gap = (high_avg - normal_avg) / normal_avg
        return {"gap_pct": round(gap * 100, 2), "flag": gap < -tolerance_pct, "message": "HIGH lower than NORMAL; inspect objective weights" if gap < -tolerance_pct else "OK"}
