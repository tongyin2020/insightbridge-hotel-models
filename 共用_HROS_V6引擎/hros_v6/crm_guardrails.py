class CRMGuardrails:
    """Prevents loyalty discounts from destroying ADR/RevPAR."""
    def __init__(self, max_discount_3star=0.15, max_discount_4star=0.12, max_discount_5star=0.10):
        self.max_map = {3: max_discount_3star, 4: max_discount_4star, 5: max_discount_5star}

    def cap_discount(self, star_rating: int, proposed_discount: float, incremental_value: float) -> dict:
        cap = self.max_map.get(star_rating, 0.12)
        if incremental_value <= 0:
            return {"discount": 0.0, "reason": "No discount: negative incremental value"}
        final = min(max(0.0, proposed_discount), cap)
        return {"discount": final, "reason": "Discount capped by star-level guardrail" if final < proposed_discount else "Discount approved"}
