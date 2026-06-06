"""Hotel Competitor Benchmark Analysis endpoint for MARE.

Accepts the user's hotel parameters alongside 1-3 competitor hotels,
runs the MARE pricing engine for each, and returns a structured
comparison report.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.auth import require_auth
from app.services.pricing_engine import recommend

# ── DSEC市场参照价（澳门统计局历史数据）──────────────────────────────────────
_REAL_DB_PATH = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_collector/hotel_real_data.db")

def _get_dsec_market_price(star_rating: float) -> float | None:
    """从price_snapshots读取DSEC模拟市场均价（近7天，按星级），
    无数据时返回None，由调用方决定fallback。"""
    star = int(round(star_rating))
    star = max(3, min(5, star))  # 限定 3-5 星
    if not _REAL_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
        month = datetime.now().month
        row = conn.execute("""
            SELECT AVG(official_bar), COUNT(*)
            FROM price_snapshots
            WHERE star = ?
              AND official_bar > 200
              AND source_ok = 1
              AND CAST(strftime('%m', checkin_date) AS INTEGER) = ?
              AND snapshot_time >= datetime('now', '-7 days')
        """, (star, month)).fetchone()
        conn.close()
        if row and row[1] and row[1] >= 3:
            return float(row[0])
    except Exception:
        pass
    return None

router = APIRouter()


class BenchmarkHotel(BaseModel):
    name: str
    base_price: float
    star_rating: float = 5.0
    total_rooms: int = 500
    current_occupancy: float = 0.78
    ota_commission_rate: float = 0.18
    vip_discount_rate: float = 0.10


class BenchmarkRequest(BaseModel):
    my_hotel: BenchmarkHotel
    competitors: List[BenchmarkHotel]
    season: str = "shoulder"
    days_to_arrival: int = 14


class _DummyInput:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _DummySettings:
    def __init__(self, floor_price, ceiling_price, base_price=None):
        self.floor_price = floor_price
        self.ceiling_price = ceiling_price
        self.base_price = base_price


def _run_hotel(hotel: BenchmarkHotel, season: str, days: int) -> dict:
    settings = _DummySettings(
        floor_price=hotel.base_price * 0.7,
        ceiling_price=hotel.base_price * 1.5,
        base_price=hotel.base_price,
    )

    # 优先使用DSEC历史均价作为竞争对手参照；无数据时退回 base_price × 1.05
    dsec_price = _get_dsec_market_price(hotel.star_rating)
    competitor_price = dsec_price if dsec_price else hotel.base_price * 1.05

    data = _DummyInput(
        hotel_id="benchmark",
        base_price=hotel.base_price,
        season=season,
        current_occupancy=hotel.current_occupancy,
        competitor_price=competitor_price,
        competitor_availability=0.4,
        remaining_inventory=int(hotel.total_rooms * (1 - hotel.current_occupancy)),
        total_rooms=hotel.total_rooms,
        booking_velocity_24h=10.0,
        days_to_arrival=days,
        cancellation_rate=0.08,
        holiday=0.15 if season in ("shoulder", "off_peak") else 0.5,
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

    result = recommend(data, settings)

    rec_price = result["recommended_price"]
    ota_price = round(rec_price * (1 + hotel.ota_commission_rate))
    direct_price = rec_price
    vip_price = round(rec_price * (1 - hotel.vip_discount_rate))
    commission_absorbed = round(rec_price * hotel.ota_commission_rate)
    direct_savings_pct = round((1 - direct_price / ota_price) * 100, 1) if ota_price > 0 else 0
    revpar = round(rec_price * hotel.current_occupancy)

    return {
        "name": hotel.name,
        "star_rating": hotel.star_rating,
        "base_price": hotel.base_price,
        "total_rooms": hotel.total_rooms,
        "occupancy": round(hotel.current_occupancy * 100, 1),
        "recommended_price": rec_price,
        "ota_price": ota_price,
        "direct_price": direct_price,
        "vip_price": vip_price,
        "commission_absorbed": commission_absorbed,
        "direct_savings_pct": direct_savings_pct,
        "revpar": revpar,
        "demand_state": result.get("demand_state", "unknown"),
        "confidence": result.get("confidence", "unknown"),
        "expected_lift": result.get("expected_revenue_lift", "0%"),
        "ota_commission_rate": hotel.ota_commission_rate,
        "vip_discount_rate": hotel.vip_discount_rate,
    }


@router.post("/analyze")
def benchmark_analyze(
    req: BenchmarkRequest,
    authorization: str | None = Header(default=None),
):
    claims = require_auth(authorization)

    if len(req.competitors) < 1 or len(req.competitors) > 3:
        raise HTTPException(status_code=400, detail="Provide 1-3 competitor hotels")

    my = _run_hotel(req.my_hotel, req.season, req.days_to_arrival)
    comps = [_run_hotel(c, req.season, req.days_to_arrival) for c in req.competitors]

    all_hotels = [my] + comps

    sorted_by_revpar = sorted(all_hotels, key=lambda h: h["revpar"], reverse=True)
    revpar_rank = next(i + 1 for i, h in enumerate(sorted_by_revpar) if h["name"] == my["name"])

    sorted_by_direct = sorted(all_hotels, key=lambda h: h["direct_savings_pct"], reverse=True)
    direct_rank = next(i + 1 for i, h in enumerate(sorted_by_direct) if h["name"] == my["name"])

    avg_comp_price = round(sum(c["recommended_price"] for c in comps) / len(comps))
    price_position = "above" if my["recommended_price"] > avg_comp_price else "below" if my["recommended_price"] < avg_comp_price else "equal"
    price_gap = round(my["recommended_price"] - avg_comp_price)
    price_gap_pct = round(price_gap / avg_comp_price * 100, 1) if avg_comp_price else 0

    avg_comp_revpar = round(sum(c["revpar"] for c in comps) / len(comps))
    revpar_advantage = round(my["revpar"] - avg_comp_revpar)

    insights = []
    if revpar_rank == 1:
        insights.append({"type": "positive", "key": "revpar_leader", "detail_zh": "RevPAR领先所有竞对", "detail_en": "Your hotel leads all competitors in RevPAR"})
    elif revpar_rank > len(all_hotels) // 2:
        insights.append({"type": "warning", "key": "revpar_lagging", "detail_zh": f"RevPAR排名第{revpar_rank}，建议优化", "detail_en": f"RevPAR ranks #{revpar_rank}"})

    if my["direct_savings_pct"] > 10:
        insights.append({"type": "positive", "key": "strong_direct", "detail_zh": f"直订优势{my['direct_savings_pct']}%", "detail_en": f"Direct advantage {my['direct_savings_pct']}%"})

    if price_position == "above" and price_gap_pct > 10:
        insights.append({"type": "info", "key": "premium_pricing", "detail_zh": f"高于竞对均价{price_gap_pct}%", "detail_en": f"Priced {price_gap_pct}% above competitors"})
    elif price_position == "below" and abs(price_gap_pct) > 10:
        insights.append({"type": "warning", "key": "underpriced", "detail_zh": f"低于竞对均价{abs(price_gap_pct)}%", "detail_en": f"Priced {abs(price_gap_pct)}% below competitors"})

    return {
        "my_hotel": my,
        "competitors": comps,
        "ranking": {
            "revpar_rank": revpar_rank,
            "direct_advantage_rank": direct_rank,
            "total_hotels": len(all_hotels),
        },
        "comparison": {
            "avg_competitor_price": avg_comp_price,
            "price_position": price_position,
            "price_gap": price_gap,
            "price_gap_pct": price_gap_pct,
            "avg_competitor_revpar": avg_comp_revpar,
            "revpar_advantage": revpar_advantage,
        },
        "insights": insights,
        "season": req.season,
    }
