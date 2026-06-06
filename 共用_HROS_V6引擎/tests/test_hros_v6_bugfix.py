"""
HROS V6 Bug-fix 验证测试
===========================
覆盖 Wolfram/DeepSeek/Gemini 三方审查发现的所有问题。
运行：pytest tests/test_hros_v6_bugfix.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hros_v6.hotel_learning_loop import HotelLearningLoop
from hros_v6.schemas_v6 import WeeklyHotelRecord, MarketSignal
from hros_v6.revenue_decision_layer_v6 import RevenueDecisionLayerV6
from hros_v6.elasticity_engine_v6 import ElasticityEngineV6, ElasticityProfile


# ──────────────────────────────────────────────────────────────────────────────
#  P0-1  rooms_sold 语义（DeepSeek/Gemini）
# ──────────────────────────────────────────────────────────────────────────────

def _make_records(price, occ, rooms_avail=100, n=10):
    """Helper: generate n identical weekly records with correct rooms_sold."""
    rooms_sold = occ * rooms_avail
    return [
        WeeklyHotelRecord(
            hotel_id="H_TEST", date="2026-W01",
            room_type="Standard", channel="Direct",
            rooms_sold=rooms_sold,
            adr=float(price),
            revenue=float(price) * rooms_sold,
            occupancy=occ,
        )
        for _ in range(n)
    ]


def test_adr_equals_price_when_homogeneous():
    """ADR = total_revenue / total_rooms_sold = (price * rooms_sold * n) / (rooms_sold * n) = price."""
    records = _make_records(price=1200, occ=0.75, rooms_avail=50)
    result = HotelLearningLoop().summarize(records)
    assert abs(result["adr"] - 1200.0) < 0.01, f"ADR expected 1200, got {result['adr']}"


def test_channel_mix_sums_to_one():
    """channel_mix values must sum to 1.0 after summarize."""
    records = [
        WeeklyHotelRecord("H1", "w1", "Std", "Direct", 30, 1000, 30000),
        WeeklyHotelRecord("H1", "w1", "Std", "OTA",    20, 900,  18000),
    ]
    result = HotelLearningLoop().summarize(records)
    total = sum(result["channel_mix"].values())
    assert abs(total - 1.0) < 1e-9, f"channel_mix sum = {total}"


# ──────────────────────────────────────────────────────────────────────────────
#  P0-2  best=None 崩溃（Gemini）
# ──────────────────────────────────────────────────────────────────────────────

def test_optimize_floor_greater_than_ceiling_no_crash():
    """floor > ceiling: must return fallback RevenueDecisionV6, not raise TypeError."""
    rdl = RevenueDecisionLayerV6()
    signal = MarketSignal(market_price=1000, demand_state="NORMAL")
    profile = ElasticityProfile()
    result = rdl.optimize(
        star_rating=4,
        market_signal=signal,
        base_occ=0.75,
        floor_price=2000,    # floor > ceiling: impossible range
        ceiling_price=500,
        elasticity_profile=profile,
    )
    assert result.recommended_price > 0
    assert result.confidence == 40.0   # fallback confidence floor
    assert result.risk_score == 100.0  # max risk on impossible range


def test_optimize_equal_floor_ceiling():
    """floor == ceiling: single candidate price, must not crash."""
    rdl = RevenueDecisionLayerV6()
    signal = MarketSignal(market_price=1000, demand_state="NORMAL")
    result = rdl.optimize(
        star_rating=4, market_signal=signal, base_occ=0.75,
        floor_price=1000, ceiling_price=1000,
        elasticity_profile=ElasticityProfile(),
    )
    assert result.recommended_price == 1000.0


# ──────────────────────────────────────────────────────────────────────────────
#  P1-A  channel_mix 指数平滑（不再直接覆盖）
# ──────────────────────────────────────────────────────────────────────────────

def test_channel_mix_is_smoothed_not_overwritten():
    """After update_hotel_profile, channel_mix must blend old and new, not replace."""
    loop = HotelLearningLoop()
    old_profile = {"baseline_adr": 1000.0, "channel_mix": {"Direct": 0.6, "OTA": 0.4}}
    new_summary  = {"adr": 1100.0, "channel_mix": {"Direct": 1.0}}  # OTA vanished this week

    updated = loop.update_hotel_profile(old_profile, new_summary, learning_rate=0.25)
    mix = updated["channel_mix"]

    # OTA should not be zeroed: old weight 0.4 carries over
    assert mix.get("OTA", 0) > 0.0, "OTA channel should not disappear after one week"
    # Direct should not jump to 1.0 immediately
    assert mix.get("Direct", 0) < 1.0, "Direct should not be 1.0 after one-week spike"
    # Must still sum to 1.0 after normalisation
    assert abs(sum(mix.values()) - 1.0) < 1e-9, f"mix sums to {sum(mix.values())}"


# ──────────────────────────────────────────────────────────────────────────────
#  P1-B  calibration_status 语义（DeepSeek/Gemini）
# ──────────────────────────────────────────────────────────────────────────────

def test_calibration_status_learning_before_4_weeks():
    loop = HotelLearningLoop()
    profile = {}
    summary = {"adr": 1000.0, "channel_mix": {"Direct": 1.0}}
    for _ in range(3):
        profile = loop.update_hotel_profile(profile, summary)
    assert profile["calibration_status"] == "learning"
    assert profile["calibration_weeks"] == 3


def test_calibration_status_ready_after_4_weeks():
    loop = HotelLearningLoop()
    profile = {}
    summary = {"adr": 1000.0, "channel_mix": {"Direct": 1.0}}
    for _ in range(4):
        profile = loop.update_hotel_profile(profile, summary)
    assert profile["calibration_status"] == "first_calibration"
    assert profile["calibration_weeks"] == 4


# ──────────────────────────────────────────────────────────────────────────────
#  Wolfram 验证：指数平滑数值
# ──────────────────────────────────────────────────────────────────────────────

def test_exponential_smoothing_convergence():
    """α=0.25: after many rounds converging to target C=1200."""
    loop = HotelLearningLoop()
    profile = {"baseline_adr": 500.0}
    summary = {"adr": 1200.0, "channel_mix": {}}
    for _ in range(60):
        profile = loop.update_hotel_profile(profile, summary, learning_rate=0.25)
    assert abs(profile["baseline_adr"] - 1200.0) < 0.1, \
        f"Did not converge: got {profile['baseline_adr']}"


def test_exponential_smoothing_halflife():
    """α=0.25: old weight should fall to ~50% after ~2.4 weeks (Wolfram: log(0.5)/log(0.75)≈2.409)."""
    # After n rounds of pure new=0, old decays: (0.75)^n
    # At n=2: (0.75)^2 = 0.5625 > 0.5
    # At n=3: (0.75)^3 = 0.4219 < 0.5  → half-life is between 2 and 3
    loop = HotelLearningLoop()
    old_val = 1000.0
    profile = {"baseline_adr": old_val}
    zero_summary = {"adr": 0.0, "channel_mix": {}}
    profile = loop.update_hotel_profile(profile, zero_summary, 0.25)
    profile = loop.update_hotel_profile(profile, zero_summary, 0.25)
    w2 = profile["baseline_adr"] / old_val  # (0.75)^2
    assert abs(w2 - 0.5625) < 0.001, f"2-week residual = {w2}, expected 0.5625"

    profile = loop.update_hotel_profile(profile, zero_summary, 0.25)
    w3 = profile["baseline_adr"] / old_val
    assert abs(w3 - 0.421875) < 0.001, f"3-week residual = {w3}, expected 0.421875"


# ──────────────────────────────────────────────────────────────────────────────
#  弹性惩罚连续性（Wolfram 验证）
# ──────────────────────────────────────────────────────────────────────────────

def test_penalty_continuity_at_005():
    eng = ElasticityEngineV6()
    e = 1.0
    left  = eng.penalty(0.0499999, e)
    right = eng.penalty(0.0500001, e)
    assert abs(left - right) < 0.0002, f"Discontinuity at 0.05: {left} vs {right}"


def test_penalty_continuity_at_015():
    eng = ElasticityEngineV6()
    e = 1.0
    left  = eng.penalty(0.1499999, e)
    right = eng.penalty(0.1500001, e)
    assert abs(left - right) < 0.0002


def test_penalty_continuity_at_025():
    eng = ElasticityEngineV6()
    e = 1.0
    left  = eng.penalty(0.2499999, e)
    right = eng.penalty(0.2500001, e)
    assert abs(left - right) < 0.0002


def test_penalty_convex_slopes():
    """Slopes must be strictly increasing: 0.30 < 1.00 < 1.80 < 3.00."""
    eng = ElasticityEngineV6()
    e = 1.0
    eps = 0.001
    slope1 = (eng.penalty(0.05, e) - eng.penalty(0.05 - eps, e)) / eps
    slope2 = (eng.penalty(0.10 + eps, e) - eng.penalty(0.10, e)) / eps
    slope3 = (eng.penalty(0.20 + eps, e) - eng.penalty(0.20, e)) / eps
    slope4 = (eng.penalty(0.30 + eps, e) - eng.penalty(0.30, e)) / eps
    assert slope1 < slope2 < slope3 < slope4, \
        f"Slopes not increasing: {slope1:.3f} {slope2:.3f} {slope3:.3f} {slope4:.3f}"


# ──────────────────────────────────────────────────────────────────────────────
#  learning_rate 范围校验
# ──────────────────────────────────────────────────────────────────────────────

def test_invalid_learning_rate_raises():
    import pytest as _pytest
    loop = HotelLearningLoop()
    with _pytest.raises(ValueError):
        loop.update_hotel_profile({}, {"adr": 1000.0, "channel_mix": {}}, learning_rate=1.5)

    with _pytest.raises(ValueError):
        loop.update_hotel_profile({}, {"adr": 1000.0, "channel_mix": {}}, learning_rate=0.0)
