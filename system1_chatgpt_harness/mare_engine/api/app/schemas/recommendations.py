from typing import Optional

from pydantic import BaseModel, field_validator


class RecommendationRequest(BaseModel):
    hotel_id: str
    hotel_star: int = 3
    season: str
    base_price: float
    holiday: float = 0
    weekend: float = 0
    border_flow: float = 0
    visitors_stats: float = 0
    flight_ferry: float = 0
    zhuhai_saturation: float = 0
    ota_booking_pace: float = 0
    weather: float = 0
    event_ticket_sales: float = 0
    competitor_price: float
    competitor_availability: float = 0
    current_occupancy: float
    elasticity_signal: float = 0

    # --- Dynamic ceiling inputs (optional) ---
    upper_tier_adr: Optional[float] = None
    neighborhood_availability: Optional[float] = None  # 0-1
    same_day_demand_score: Optional[float] = None  # 0-1
    event_density: Optional[float] = None  # 0-1

    # --- Supply scarcity inputs (P1-2, optional) ---
    remaining_inventory: int = 0
    total_rooms: int = 0
    booking_velocity_24h: float = 0
    days_to_arrival: int = 30
    cancellation_rate: float = 0.1

    # --- CRM inputs (P1-1, optional) ---
    guest_segment: str = 'unknown'
    avg_clv: float = 0
    repurchase_probability: float = 0.5
    price_sensitivity: str = 'medium'
    churn_risk: float = 0.0
    loyalty_tier: str = ''

    # --- Fair pricing inputs (P2, optional) ---
    previous_price: float = 0
    avg_30d_price: float = 0
    historical_avg: float = 0
    max_deviation_pct: float = 25.0
    customer_historical_rate: float = 0

    # --- Policy context (optional) ---
    guest_satisfaction: Optional[float] = None  # 0-5
    data_freshness_minutes: Optional[float] = None
    ota_prices: Optional[dict[str, float]] = None  # channel -> price
    dsec_market_occ: float = 0.0
    mha_market_occ: float = 0.0

    @field_validator("season")
    @classmethod
    def validate_season(cls, v: str) -> str:
        allowed = {"off_peak", "shoulder", "peak", "super_peak"}
        if v not in allowed:
            raise ValueError(f"season must be one of {allowed}")
        return v

    @field_validator("base_price", "competitor_price")
    @classmethod
    def validate_prices(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Price must be positive")
        if v > 100000:
            raise ValueError("Price unreasonably high")
        return v

    @field_validator("current_occupancy")
    @classmethod
    def validate_occupancy(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError("Occupancy must be between 0 and 1")
        return v

    @field_validator("hotel_id")
    @classmethod
    def validate_hotel_id(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 64:
            raise ValueError("Invalid hotel_id")
        return v


class FeedbackRequest(BaseModel):
    history_id: str
    hotel_id: str
    action: str
    final_price: float | None = None
    occupancy_outcome: float | None = None
    revenue_outcome: float | None = None
    notes: str | None = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"applied", "ignored", "adjusted"}
        if v not in allowed:
            raise ValueError(f"action must be one of {allowed}")
        return v

    @field_validator("final_price")
    @classmethod
    def validate_final_price(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("final_price must be positive")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v: str | None) -> str | None:
        if v and len(v) > 2000:
            raise ValueError("Notes too long (max 2000 chars)")
        return v
