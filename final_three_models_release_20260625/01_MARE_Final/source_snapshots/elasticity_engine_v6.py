from dataclasses import dataclass

@dataclass
class ElasticityProfile:
    star_segment: str = "3_4_STAR"
    base_elasticity: float = 0.85
    high_demand_multiplier: float = 0.55
    normal_multiplier: float = 1.00
    low_demand_multiplier: float = 1.35
    super_peak_multiplier: float = 0.45

class ElasticityEngineV6:
    """Segmented elasticity engine.
    The key business rule: price does not optimize ADR alone; it optimizes RevPAR/TRevPAR.
    """

    def effective_elasticity(self, profile: ElasticityProfile, demand_state: str) -> float:
        state = (demand_state or "NORMAL").upper()
        if state in ("SUPER_PEAK", "PEAK_FULLHOUSE", "OVERFLOW_SQUEEZE"):
            m = profile.super_peak_multiplier
        elif state == "HIGH":
            m = profile.high_demand_multiplier
        elif state in ("LOW", "DEMAND_COLLAPSE", "MIXED_CRISIS"):
            m = profile.low_demand_multiplier
        else:
            m = profile.normal_multiplier
        return max(0.05, profile.base_elasticity * m)

    def penalty(self, premium: float, elasticity: float) -> float:
        """Premium = (price - market_price)/market_price.
        Positive premium reduces occupancy; negative premium can increase occupancy but capped.
        """
        if premium < 0:
            return max(-0.35, 0.45 * elasticity * premium)
        if premium <= 0.05:
            raw = 0.30 * premium
        elif premium <= 0.15:
            raw = 0.05 * 0.30 + (premium - 0.05) * 1.00
        elif premium <= 0.25:
            raw = 0.05 * 0.30 + 0.10 * 1.00 + (premium - 0.15) * 1.80
        else:
            raw = 0.05 * 0.30 + 0.10 * 1.00 + 0.10 * 1.80 + (premium - 0.25) * 3.00
        return elasticity * raw

    def predict_occupancy(self, price: float, market_price: float, base_occ: float, profile: ElasticityProfile, demand_state: str) -> float:
        if market_price <= 0:
            return max(0.05, min(0.98, base_occ))
        e = self.effective_elasticity(profile, demand_state)
        premium = (price - market_price) / market_price
        occ = base_occ * (1.0 - self.penalty(premium, e))
        return max(0.05, min(0.98, occ))
