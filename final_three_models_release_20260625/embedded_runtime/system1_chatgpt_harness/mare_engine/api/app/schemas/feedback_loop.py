"""Pydantic schemas for the feedback + outcome data loop.

Covers the full lifecycle: recommendation -> execution -> outcome -> analytics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# RecommendationLog – full decision trace stored with every recommendation
# ---------------------------------------------------------------------------

class RecommendationLog(BaseModel):
    """Immutable snapshot of every signal that influenced a recommendation."""

    history_id: str
    hotel_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # inputs
    season: str
    base_price: float
    demand_score: float
    demand_state: str
    competitor_price: float
    current_occupancy: float

    # intermediate computations
    seasonal_base: float
    demand_adjustment: float
    competition_adjustment: float
    profit_adjustment: float
    raw_price: float

    # guardrails
    floor_price: float
    ceiling_price: float
    dynamic_ceiling: Optional[float] = None
    guardrail_violations: list[str] = Field(default_factory=list)

    # output
    recommended_price: int
    confidence: str
    expected_lift: str

    # shadow
    shadow_price: Optional[int] = None
    shadow_weights: Optional[dict] = None

    # model metadata
    model_weights_hash: Optional[str] = None
    factor_breakdown: list[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ExecutionLog – what the hotel actually applied
# ---------------------------------------------------------------------------

class ExecutionLogRequest(BaseModel):
    """Submitted by the hotel/operator after acting on a recommendation."""

    history_id: str
    hotel_id: str
    action: str  # applied | ignored | adjusted
    final_price: Optional[float] = None
    adjusted_by: Optional[str] = None  # username or role
    reason_code: Optional[str] = None  # e.g. gm_override, ota_mismatch
    approval_level: Optional[str] = None  # auto | manager | gm | revenue_director
    notes: Optional[str] = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        allowed = {"applied", "ignored", "adjusted"}
        if v not in allowed:
            raise ValueError(f"action must be one of {allowed}")
        return v

    @field_validator("final_price")
    @classmethod
    def _validate_price(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("final_price must be positive")
        return v


class ExecutionLogResponse(BaseModel):
    execution_id: str
    status: str = "recorded"


# ---------------------------------------------------------------------------
# OutcomeLog – actual results after the price was live
# ---------------------------------------------------------------------------

class OutcomeLogRequest(BaseModel):
    """Actual performance data submitted after the stay date passes."""

    history_id: str
    hotel_id: str
    date: str  # ISO date of the stay night

    # actuals
    occupancy: Optional[float] = None
    adr: Optional[float] = None  # average daily rate achieved
    revpar: Optional[float] = None
    revenue: Optional[float] = None
    rooms_sold: Optional[int] = None
    rooms_available: Optional[int] = None
    guest_satisfaction: Optional[float] = None  # 0-5 scale

    @field_validator("occupancy")
    @classmethod
    def _validate_occupancy(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= 1):
            raise ValueError("occupancy must be between 0 and 1")
        return v

    @field_validator("guest_satisfaction")
    @classmethod
    def _validate_satisfaction(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= 5):
            raise ValueError("guest_satisfaction must be between 0 and 5")
        return v


class OutcomeLogResponse(BaseModel):
    outcome_id: str
    status: str = "recorded"


# ---------------------------------------------------------------------------
# FeedbackAnalytics – aggregated accuracy metrics
# ---------------------------------------------------------------------------

class FeedbackAnalytics(BaseModel):
    """Aggregated view of recommendation accuracy for a hotel."""

    hotel_id: str
    period_start: Optional[str] = None
    period_end: Optional[str] = None

    total_recommendations: int = 0
    applied_count: int = 0
    ignored_count: int = 0
    adjusted_count: int = 0

    # accuracy metrics
    mean_price_deviation: Optional[float] = None  # avg(applied - recommended)
    mean_occupancy_vs_predicted: Optional[float] = None
    mean_revenue_vs_predicted: Optional[float] = None
    hit_rate: Optional[float] = None  # % of recs within 5% of optimal

    # guardrail metrics
    floor_clip_rate: Optional[float] = None
    ceiling_clip_rate: Optional[float] = None
    override_rate: Optional[float] = None  # % needing gm/director override

    # shadow testing
    shadow_outperform_rate: Optional[float] = None


# ---------------------------------------------------------------------------
# History entry for the GET endpoint
# ---------------------------------------------------------------------------

class FeedbackHistoryEntry(BaseModel):
    history_id: str
    hotel_id: str
    timestamp: Optional[str] = None
    recommended_price: Optional[float] = None
    action: Optional[str] = None
    final_price: Optional[float] = None
    occupancy_outcome: Optional[float] = None
    revenue_outcome: Optional[float] = None
    adr_outcome: Optional[float] = None
    guest_satisfaction: Optional[float] = None
