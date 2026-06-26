class SelfACQEngineV6:
    """Direct acquisition engine: Direct LTV vs OTA net revenue, including CAC/conversion/repeat."""
    def evaluate(self, *, direct_price: float, ota_gross_price: float, ota_commission_rate: float,
                 direct_conversion_prob: float, repeat_probability: float, future_margin: float,
                 acquisition_cost: float, discount_cost: float = 0.0, crm_value: float = 0.0,
                 discount_rate: float = 0.10) -> dict:
        ota_net = ota_gross_price * (1.0 - ota_commission_rate)
        discounted_future = repeat_probability * future_margin / (1.0 + discount_rate)
        expected_direct_ltv = direct_conversion_prob * (direct_price + discounted_future + crm_value) - acquisition_cost - discount_cost
        advantage = expected_direct_ltv - ota_net
        return {
            "ota_net_revenue": round(ota_net, 2),
            "expected_direct_ltv": round(expected_direct_ltv, 2),
            "direct_advantage": round(advantage, 2),
            "direct_wins": advantage > 0,
        }
