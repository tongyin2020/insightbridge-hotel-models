from hros_v6.revenue_decision_layer_v6 import RevenueDecisionLayerV6
from hros_v6.elasticity_engine_v6 import ElasticityProfile
from hros_v6.schemas_v6 import MarketSignal, WeeklyHotelRecord
from hros_v6.hotel_learning_loop import HotelLearningLoop
from hros_v6.crm_guardrails import CRMGuardrails
from hros_v6.selfacq_engine_v6 import SelfACQEngineV6
from hros_v6.revenue_attribution_engine import RevenueAttributionEngine

def test_revenue_decision_layer_runs():
    d = RevenueDecisionLayerV6().optimize(
        star_rating=4,
        market_signal=MarketSignal(market_price=1100, demand_state="HIGH", ota_booking_pace=0.5),
        base_occ=0.82,
        floor_price=750,
        ceiling_price=2000,
        elasticity_profile=ElasticityProfile(base_elasticity=0.85),
    )
    assert d.recommended_price > 0
    assert 0 < d.predicted_occupancy <= 0.98

def test_learning_loop_summary():
    records = [WeeklyHotelRecord("H1", "2026-06-01", "Deluxe", "Direct", 10, 1200, 12000)]
    summary = HotelLearningLoop().summarize(records)
    assert summary["adr"] == 1200
    assert summary["channel_mix"]["Direct"] == 1.0

def test_crm_guardrail_caps_discount():
    g = CRMGuardrails()
    assert g.cap_discount(3, 0.22, 100)["discount"] == 0.15

def test_selfacq_engine():
    out = SelfACQEngineV6().evaluate(direct_price=1000, ota_gross_price=1040, ota_commission_rate=0.15, direct_conversion_prob=1.0, repeat_probability=0.2, future_margin=500, acquisition_cost=30)
    assert "direct_advantage" in out

def test_attribution():
    out = RevenueAttributionEngine().attribute(baseline_adr=1000, baseline_occ=0.75, mare_adr=1080, mare_occ=0.74, crm_incremental_value=100, selfacq_incremental_value=50, rooms_available=50)
    assert out["baseline_revpar"] == 750
