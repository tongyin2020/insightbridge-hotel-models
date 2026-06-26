"""AI Model Value Proof — KPI metrics endpoint for MARE.

Returns KPI data demonstrating the AI pricing engine's impact across
4 dimensions: Revenue, Channel Optimization, Operational Efficiency,
and Qualitative Feedback.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from typing import Optional

from app.auth import require_auth
from app.services.pricing_engine import recommend

router = APIRouter()

# City demo profiles for investor roadshow
CITY_PROFILES = {
    "macau": {
        "hotel_id": "macau_demo",
        "base_price": 900.0,
        "total_rooms": 500,
        "avg_occupancy": 0.78,
        "ota_commission_rate": 0.18,
        "currency": "MOP",
    },
    "dubai": {
        "hotel_id": "dubai_demo",
        "base_price": 1350.0,
        "total_rooms": 800,
        "avg_occupancy": 0.82,
        "ota_commission_rate": 0.15,
        "currency": "AED",
    },
    "london": {
        "hotel_id": "london_demo",
        "base_price": 380.0,
        "total_rooms": 350,
        "avg_occupancy": 0.85,
        "ota_commission_rate": 0.22,
        "currency": "GBP",
    },
}


class ModelValueRequest(BaseModel):
    hotel_id: str = "hotel_demo_001"
    base_price: float = 898.0
    total_rooms: int = 500
    avg_occupancy: float = 0.78
    ota_commission_rate: float = 0.18
    months_active: int = 6
    city: str = "macau"


def _generate_monthly_trend(months: int, base: float, growth_rate: float):
    """Generate monthly trend data with realistic growth."""
    trend = []
    for i in range(months):
        noise = random.uniform(-0.02, 0.02)
        value = base * (1 + growth_rate * (i + 1) / months + noise)
        trend.append(round(value, 2))
    return trend


class _DummyInput:
    """Lightweight input object matching MARE RecommendationRequest fields."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _DummySettings:
    """Lightweight settings object matching MARE HotelSetting fields."""
    def __init__(self, floor_price, ceiling_price, base_price=None):
        self.floor_price = floor_price
        self.ceiling_price = ceiling_price
        self.base_price = base_price


@router.post("/metrics")
def get_model_value_metrics(
    req: ModelValueRequest,
    authorization: str | None = Header(default=None),
):
    claims = require_auth(authorization)

    # Apply city profile if selected
    profile = CITY_PROFILES.get(req.city, CITY_PROFILES["macau"])
    bp = req.base_price if req.base_price != 898.0 else profile["base_price"]
    rooms = req.total_rooms if req.total_rooms != 500 else profile["total_rooms"]
    occ = req.avg_occupancy if req.avg_occupancy != 0.78 else profile["avg_occupancy"]
    comm = req.ota_commission_rate if req.ota_commission_rate != 0.18 else profile["ota_commission_rate"]
    currency = profile["currency"]

    settings = _DummySettings(
        floor_price=bp * 0.7,
        ceiling_price=bp * 1.5,
        base_price=bp,
    )

    base_input = _DummyInput(
        hotel_id=profile["hotel_id"],
        base_price=bp,
        season="shoulder",
        current_occupancy=occ,
        competitor_price=bp * 1.08,
        competitor_availability=0.4,
        remaining_inventory=int(rooms * (1 - occ)),
        total_rooms=rooms,
        booking_velocity_24h=12.0,
        days_to_arrival=14,
        cancellation_rate=0.08,
        holiday=0.15,
        event_ticket_sales=0.2,
        weekend=0.3,
        border_flow=0.1,
        visitors_stats=0.1,
        flight_ferry=0.08,
        zhuhai_saturation=0.05,
        ota_booking_pace=0.4,
        weather=0.0,
        elasticity_signal=0.0,
        guest_segment="unknown",
        avg_clv=0.0,
        repurchase_probability=0.5,
        price_sensitivity="medium",
        churn_risk=0.0,
        loyalty_tier="",
        previous_price=0.0,
        avg_30d_price=0.0,
        historical_avg=0.0,
        max_deviation_pct=25.0,
        customer_historical_rate=0.0,
    )

    result_shoulder = recommend(base_input, settings)

    peak_input = _DummyInput(
        hotel_id=profile["hotel_id"],
        base_price=bp,
        season="peak",
        current_occupancy=min(occ + 0.12, 0.98),
        competitor_price=bp * 1.08,
        competitor_availability=0.4,
        remaining_inventory=int(rooms * 0.1),
        total_rooms=rooms,
        booking_velocity_24h=18.0,
        days_to_arrival=14,
        cancellation_rate=0.08,
        holiday=0.6,
        event_ticket_sales=0.4,
        weekend=0.3,
        border_flow=0.1,
        visitors_stats=0.1,
        flight_ferry=0.08,
        zhuhai_saturation=0.05,
        ota_booking_pace=0.6,
        weather=0.0,
        elasticity_signal=0.0,
        guest_segment="unknown",
        avg_clv=0.0,
        repurchase_probability=0.5,
        price_sensitivity="medium",
        churn_risk=0.0,
        loyalty_tier="",
        previous_price=0.0,
        avg_30d_price=0.0,
        historical_avg=0.0,
        max_deviation_pct=25.0,
        customer_historical_rate=0.0,
    )

    result_peak = recommend(peak_input, settings)

    ai_price = result_shoulder["recommended_price"]
    peak_price = result_peak["recommended_price"]

    # Revenue metrics
    revpar_base = bp * occ
    revpar_ai = ai_price * min(occ + 0.03, 0.99)
    revpar_lift_pct = round((revpar_ai / revpar_base - 1) * 100, 1)

    adr_base = bp
    adr_ai = round((ai_price + peak_price) / 2, 0)
    adr_lift_pct = round((adr_ai / adr_base - 1) * 100, 1)

    random.seed(42)
    forecast_7d = round(100 - random.uniform(2.5, 4.8), 1)
    forecast_14d = round(100 - random.uniform(3.5, 6.2), 1)
    forecast_30d = round(100 - random.uniform(5.0, 8.5), 1)

    # Channel metrics
    direct_booking_base_pct = 22.0
    direct_booking_ai_pct = round(direct_booking_base_pct + random.uniform(5.0, 10.0), 1)
    direct_lift = round(direct_booking_ai_pct - direct_booking_base_pct, 1)

    ota_commission_per_night = round(ai_price * comm)
    rooms_per_month = int(rooms * occ * 30)
    ota_rooms_saved = int(rooms_per_month * direct_lift / 100)
    commission_saved_monthly = round(ota_rooms_saved * ota_commission_per_night)
    commission_saved_annual = commission_saved_monthly * 12

    # Operational metrics
    manual_hours_daily = 4.5
    ai_hours_daily = 0.8
    hours_saved_pct = round((1 - ai_hours_daily / manual_hours_daily) * 100, 0)
    automation_rate = 85.0
    response_human_minutes = 720
    response_ai_minutes = 3

    # ADR decisions
    total_decisions_month = rooms_per_month
    ai_uplifts = int(total_decisions_month * 0.35)
    ai_holds = int(total_decisions_month * 0.45)
    ai_reductions = total_decisions_month - ai_uplifts - ai_holds

    # Qualitative
    usability_score = 4.2
    trust_score = 3.8

    # Monthly labels
    months = min(req.months_active, 12)
    month_labels = []
    now = datetime.now(timezone.utc)
    for i in range(months):
        m = now.month - months + i + 1
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        month_labels.append(f"{y}-{m:02d}")

    return {
        "demo_mode": True,
        "city": req.city,
        "currency": currency,
        "hotel_id": profile["hotel_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months_active": months,
        "month_labels": month_labels,

        "revenue": {
            "revpar": {
                "baseline": round(revpar_base, 0),
                "current": round(revpar_ai, 0),
                "lift_pct": revpar_lift_pct,
                "target_range": "8%-15%",
                "trend": _generate_monthly_trend(months, revpar_base, revpar_lift_pct / 100),
            },
            "adr": {
                "baseline": round(adr_base, 0),
                "current": round(adr_ai, 0),
                "lift_pct": adr_lift_pct,
                "uplifts_count": ai_uplifts,
                "holds_count": ai_holds,
                "reductions_count": ai_reductions,
                "total_decisions": total_decisions_month,
                "trend": _generate_monthly_trend(months, adr_base, adr_lift_pct / 100),
            },
            "forecast_accuracy": {
                "d7": forecast_7d,
                "d14": forecast_14d,
                "d30": forecast_30d,
                "target": 95.0,
            },
        },

        "channel": {
            "direct_booking_pct": {
                "baseline": direct_booking_base_pct,
                "current": direct_booking_ai_pct,
                "lift_points": direct_lift,
                "target_range": "5-10pp",
                "trend": _generate_monthly_trend(months, direct_booking_base_pct, direct_lift / 100),
            },
            "commission_savings": {
                "monthly": commission_saved_monthly,
                "annual": commission_saved_annual,
                "ota_commission_rate": comm * 100,
                "rooms_redirected_monthly": ota_rooms_saved,
                "per_night_saved": ota_commission_per_night,
                "covers_system_cost": commission_saved_annual > 50000,
                "trend": _generate_monthly_trend(months, commission_saved_monthly * 0.6, 0.4),
            },
        },

        "operations": {
            "man_hours": {
                "manual_daily": manual_hours_daily,
                "ai_daily": ai_hours_daily,
                "saved_pct": hours_saved_pct,
                "automation_rate": automation_rate,
            },
            "response_latency": {
                "human_minutes": response_human_minutes,
                "ai_minutes": response_ai_minutes,
                "speedup_factor": round(response_human_minutes / response_ai_minutes),
            },
        },

        "qualitative": {
            "usability_score": usability_score,
            "trust_score": trust_score,
            "max_score": 5.0,
        },
    }


class ROICalculatorRequest(BaseModel):
    total_rooms: int = 500
    current_revpar: float = 700.0
    ota_commission_rate: float = 0.18
    annual_system_cost: float = 200000.0
    city: str = "macau"


@router.post("/v1/model-value/roi-calculator")
async def roi_calculator(
    req: ROICalculatorRequest,
    authorization: str = Header(None),
):
    claims = require_auth(authorization)
    profile = CITY_PROFILES.get(req.city, CITY_PROFILES["macau"])
    currency = profile["currency"]
    rooms = req.total_rooms
    revpar = req.current_revpar
    comm_rate = req.ota_commission_rate
    sys_cost = req.annual_system_cost

    nights_year = 365
    current_annual_revenue = round(rooms * revpar * nights_year)

    revpar_lift_pct = 14.5
    adr_lift_pct = 18.0
    direct_booking_lift_pp = 8.0

    projected_revpar = round(revpar * (1 + revpar_lift_pct / 100), 2)
    projected_annual_revenue = round(rooms * projected_revpar * nights_year)
    annual_revenue_increase = projected_annual_revenue - current_annual_revenue

    avg_occ = profile["avg_occupancy"]
    rooms_per_year = int(rooms * avg_occ * nights_year)
    baseline_direct_pct = 22.0
    new_direct_pct = baseline_direct_pct + direct_booking_lift_pp
    rooms_redirected = int(rooms_per_year * direct_booking_lift_pp / 100)
    avg_room_rate = revpar / avg_occ if avg_occ > 0 else revpar
    commission_per_room = round(avg_room_rate * comm_rate, 2)
    commission_savings_annual = round(rooms_redirected * commission_per_room)

    total_annual_benefit = annual_revenue_increase + commission_savings_annual
    net_profit_year1 = total_annual_benefit - sys_cost

    monthly_benefit = total_annual_benefit / 12
    payback_months = round(sys_cost / monthly_benefit, 1) if monthly_benefit > 0 else 999

    roi_pct_year1 = round((net_profit_year1 / sys_cost) * 100, 1) if sys_cost > 0 else 0
    cumulative_5y = (total_annual_benefit * 5) - sys_cost
    roi_pct_5y = round((cumulative_5y / sys_cost) * 100, 1) if sys_cost > 0 else 0

    projection = []
    cumulative = 0
    for yr in range(1, 6):
        growth_factor = 1 + 0.05 * (yr - 1)
        yr_benefit = round(total_annual_benefit * growth_factor)
        yr_cost = sys_cost if yr == 1 else round(sys_cost * 0.3)
        yr_net = yr_benefit - yr_cost
        cumulative += yr_net
        projection.append({
            "year": yr,
            "benefit": yr_benefit,
            "cost": yr_cost,
            "net_profit": yr_net,
            "cumulative": cumulative,
        })

    return {
        "currency": currency,
        "city": req.city,
        "inputs": {
            "total_rooms": rooms,
            "current_revpar": revpar,
            "ota_commission_rate": comm_rate,
            "annual_system_cost": sys_cost,
        },
        "current": {
            "annual_revenue": current_annual_revenue,
            "direct_booking_pct": baseline_direct_pct,
        },
        "projected": {
            "revpar": projected_revpar,
            "revpar_lift_pct": revpar_lift_pct,
            "adr_lift_pct": adr_lift_pct,
            "annual_revenue": projected_annual_revenue,
            "annual_revenue_increase": annual_revenue_increase,
            "direct_booking_pct": new_direct_pct,
            "direct_booking_lift_pp": direct_booking_lift_pp,
        },
        "savings": {
            "rooms_redirected_annual": rooms_redirected,
            "commission_per_room": commission_per_room,
            "commission_savings_annual": commission_savings_annual,
        },
        "roi": {
            "total_annual_benefit": total_annual_benefit,
            "net_profit_year1": net_profit_year1,
            "payback_months": payback_months,
            "roi_pct_year1": roi_pct_year1,
            "roi_pct_5y": roi_pct_5y,
        },
        "projection_5y": projection,
    }
