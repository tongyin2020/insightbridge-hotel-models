class RevenueAttributionEngine:
    """Explains where revenue lift comes from: MARE, CRM, SelfACQ and mix effects."""

    def attribute(self, *, baseline_adr: float, baseline_occ: float,
                  mare_adr: float, mare_occ: float,
                  crm_incremental_value: float = 0.0,
                  selfacq_incremental_value: float = 0.0,
                  rooms_available: float = 1.0) -> dict:
        baseline_revpar = baseline_adr * baseline_occ
        mare_revpar = mare_adr * mare_occ
        mare_lift = mare_revpar - baseline_revpar
        crm_lift = crm_incremental_value / max(rooms_available, 1.0)
        selfacq_lift = selfacq_incremental_value / max(rooms_available, 1.0)
        total_lift = mare_lift + crm_lift + selfacq_lift
        return {
            "baseline_revpar": round(baseline_revpar, 2),
            "optimized_revpar": round(baseline_revpar + total_lift, 2),
            "mare_contribution": round(mare_lift, 2),
            "crm_contribution": round(crm_lift, 2),
            "selfacq_contribution": round(selfacq_lift, 2),
            "total_lift_mop": round(total_lift, 2),
            "total_lift_pct": round(total_lift / baseline_revpar * 100, 2) if baseline_revpar else 0.0,
        }
