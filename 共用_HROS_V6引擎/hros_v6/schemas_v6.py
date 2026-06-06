from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass(frozen=True)
class WeeklyHotelRecord:
    hotel_id: str
    date: str
    room_type: str
    channel: str
    rooms_sold: float
    adr: float
    revenue: float
    occupancy: Optional[float] = None

@dataclass(frozen=True)
class MarketSignal:
    market_price: float
    competitor_price: Optional[float] = None
    demand_state: str = "NORMAL"
    data_quality: float = 1.0
    event_density: float = 0.0
    border_flow: float = 0.0
    zhuhai_saturation: float = 0.0
    ota_booking_pace: float = 0.0

@dataclass(frozen=True)
class RevenueDecisionV6:
    recommended_price: float
    predicted_occupancy: float
    predicted_revpar: float
    baseline_revpar: float
    lift_pct: float
    risk_score: float
    opportunity_score: float
    confidence: float
    objective_value: float
    reason: Dict[str, float] = field(default_factory=dict)
