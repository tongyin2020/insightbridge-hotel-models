"""
Fair Pricing Engine (P2 — #8).

Provides price-velocity control, fairness scoring, historical anchoring,
and loyal-customer protection to prevent unfair or erratic pricing.
"""

from __future__ import annotations

from math import floor


class FairPricingEngine:
    """Stateless fairness evaluator — all state is passed in per call."""

    # Season-specific max daily increase rates
    MAX_DAILY_INCREASE = {
        "off_peak": 0.05,
        "shoulder": 0.05,
        "peak": 0.08,
        "super_peak": 0.10,
    }

    # Loyalty tier max increase over personal historical rate
    LOYALTY_MAX_INCREASE = {
        "diamond": 0.05,
        "platinum": 0.05,
        "vip": 0.05,
        "gold": 0.10,
        "silver": 0.15,
    }

    # ------------------------------------------------------------------
    # 1. Price velocity control
    # ------------------------------------------------------------------
    def velocity_check(
        self,
        current_price: float,
        previous_price: float,
        season: str,
    ) -> tuple[float, str, bool]:
        """
        Ensure day-over-day price increase does not exceed seasonal cap.

        Returns
        -------
        capped_price : float
            The price after velocity capping (unchanged if within limit).
        reason : str
            Human-readable explanation.
        was_capped : bool
            ``True`` if the proposed price was reduced.
        """
        if previous_price <= 0:
            return current_price, "No previous price for velocity comparison.", False

        max_rate = self.MAX_DAILY_INCREASE.get(season, 0.05)
        ceiling = previous_price * (1 + max_rate)

        if current_price > ceiling:
            capped = int(floor(ceiling))
            reason = (
                f"Price velocity capped: proposed MOP {current_price:.0f} exceeds "
                f"max {max_rate:.0%}/day from previous MOP {previous_price:.0f}. "
                f"Capped to MOP {capped}."
            )
            return capped, reason, True

        return current_price, f"Price within velocity limit ({max_rate:.0%}/day).", False

    # ------------------------------------------------------------------
    # 2. Customer fairness index
    # ------------------------------------------------------------------
    def customer_fairness_index(
        self,
        proposed_price: float,
        avg_30d_price: float,
    ) -> tuple[float, str]:
        """
        Fairness score 0-100.  Lower = more unfair.
        Based on deviation from 30-day rolling average.

        Returns
        -------
        fairness_score : float
        assessment : str
        """
        if avg_30d_price <= 0:
            return 100.0, "No 30-day average available; fairness assumed."

        deviation = (proposed_price - avg_30d_price) / avg_30d_price

        if deviation <= 0:
            # Price at or below average — fully fair
            score = 100.0
        elif deviation <= 0.05:
            score = 95.0 - deviation * 200  # 95 → 85
        elif deviation <= 0.15:
            score = 85.0 - (deviation - 0.05) * 300  # 85 → 55
        elif deviation <= 0.25:
            score = 55.0 - (deviation - 0.15) * 350  # 55 → 20
        else:
            score = max(0.0, 20.0 - (deviation - 0.25) * 100)

        score = round(max(0.0, min(100.0, score)), 1)

        if score >= 80:
            assessment = "Fair"
        elif score >= 60:
            assessment = "Acceptable"
        elif score >= 40:
            assessment = "Borderline"
        else:
            assessment = "Unfair"

        return score, assessment

    # ------------------------------------------------------------------
    # 3. Historical anchor
    # ------------------------------------------------------------------
    def historical_anchor(
        self,
        proposed_price: float,
        historical_avg: float,
        max_deviation_pct: float = 25.0,
    ) -> tuple[float, str]:
        """
        Anchor proposed price to historical average.

        ``ceiling = min(proposed, historical_avg * (1 + max_deviation_pct / 100))``

        Returns
        -------
        anchored_price : float
        reason : str
        """
        if historical_avg <= 0:
            return proposed_price, "No historical average available; price unchanged."

        ceiling = historical_avg * (1 + max_deviation_pct / 100)
        if proposed_price > ceiling:
            anchored = int(floor(ceiling))
            reason = (
                f"Anchored: proposed MOP {proposed_price:.0f} exceeds "
                f"{max_deviation_pct:.0f}% above historical avg MOP {historical_avg:.0f}. "
                f"Capped to MOP {anchored}."
            )
            return anchored, reason

        return proposed_price, (
            f"Within historical anchor ({max_deviation_pct:.0f}% max deviation "
            f"from avg MOP {historical_avg:.0f})."
        )

    # ------------------------------------------------------------------
    # 4. Loyal customer adjustment
    # ------------------------------------------------------------------
    def loyal_customer_adjustment(
        self,
        proposed_price: float,
        loyalty_tier: str,
        customer_historical_rate: float,
    ) -> tuple[float, str]:
        """
        Ensure loyal guests see limited increase from *their* historical rate.

        - Diamond / Platinum / VIP: max +5% over their avg
        - Gold: max +10%
        - Silver: max +15%

        Returns
        -------
        adjusted_price : float
        reason : str
        """
        tier_lower = loyalty_tier.lower().strip()
        if tier_lower not in self.LOYALTY_MAX_INCREASE:
            return proposed_price, "No loyalty tier cap applicable."

        if customer_historical_rate <= 0:
            return proposed_price, "No customer historical rate; loyalty cap skipped."

        max_pct = self.LOYALTY_MAX_INCREASE[tier_lower]
        ceiling = customer_historical_rate * (1 + max_pct)

        if proposed_price > ceiling:
            adjusted = int(floor(ceiling))
            reason = (
                f"{loyalty_tier} loyalty cap: max +{max_pct:.0%} over customer avg "
                f"MOP {customer_historical_rate:.0f}. Adjusted MOP {proposed_price:.0f} "
                f"→ MOP {adjusted}."
            )
            return adjusted, reason

        return proposed_price, (
            f"{loyalty_tier} loyalty: price within +{max_pct:.0%} of customer avg "
            f"MOP {customer_historical_rate:.0f}."
        )

    # ------------------------------------------------------------------
    # 5. Composite evaluation
    # ------------------------------------------------------------------
    def evaluate(self, proposed_price: float, context: dict) -> dict:
        """
        Run all fairness checks and return a comprehensive report.

        Expected *context* keys (all optional — missing keys are handled
        gracefully):

        - ``previous_price``           : last day's price
        - ``season``                   : season string
        - ``avg_30d_price``            : 30-day rolling average
        - ``historical_avg``           : long-term historical average
        - ``max_deviation_pct``        : anchor deviation cap (default 25)
        - ``loyalty_tier``             : guest loyalty tier
        - ``customer_historical_rate`` : guest's own average rate
        """
        report: dict = {"original_proposed_price": proposed_price}
        price = proposed_price

        # Velocity
        prev = context.get("previous_price") or 0
        season = context.get("season", "shoulder")
        vel_price, vel_reason, vel_capped = self.velocity_check(price, prev, season)
        report["velocity"] = {
            "capped_price": vel_price,
            "reason": vel_reason,
            "was_capped": vel_capped,
        }
        price = vel_price

        # Fairness index
        avg_30d = context.get("avg_30d_price") or 0
        fi_score, fi_assessment = self.customer_fairness_index(price, avg_30d)
        report["fairness_index"] = {
            "score": fi_score,
            "assessment": fi_assessment,
        }

        # Historical anchor
        hist_avg = context.get("historical_avg") or 0
        max_dev = context.get("max_deviation_pct", 25.0)
        anc_price, anc_reason = self.historical_anchor(price, hist_avg, max_dev)
        report["historical_anchor"] = {
            "anchored_price": anc_price,
            "reason": anc_reason,
        }
        price = anc_price

        # Loyalty
        tier = context.get("loyalty_tier", "")
        cust_rate = context.get("customer_historical_rate") or 0
        loy_price, loy_reason = self.loyal_customer_adjustment(price, tier, cust_rate)
        report["loyalty_cap"] = {
            "adjusted_price": loy_price,
            "reason": loy_reason,
        }
        price = loy_price

        report["final_fair_price"] = price
        report["any_adjustment_applied"] = price != proposed_price

        return report
