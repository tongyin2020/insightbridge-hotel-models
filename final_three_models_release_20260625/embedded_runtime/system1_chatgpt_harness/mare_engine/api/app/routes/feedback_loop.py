"""Routes for the feedback + outcome data loop.

Endpoints
---------
POST /execution   – record what was actually applied
POST /outcome     – record actual stay-night results
GET  /history/{hotel_id}    – paginated decision history
GET  /analytics/{hotel_id}  – aggregated accuracy metrics
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db.session import get_db
from app.models.pricing_history import PricingHistory
from app.models.recommendation_feedback import RecommendationFeedback
from app.schemas.feedback_loop import (
    ExecutionLogRequest,
    ExecutionLogResponse,
    FeedbackAnalytics,
    FeedbackHistoryEntry,
    OutcomeLogRequest,
    OutcomeLogResponse,
)

router = APIRouter()


def _enforce_access(claims: dict, hotel_id: str) -> None:
    if claims["role"] != "admin" and claims.get("hotel_id") != hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# POST /execution
# ---------------------------------------------------------------------------

@router.post("/execution", response_model=ExecutionLogResponse)
def record_execution(
    payload: ExecutionLogRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Record the action taken on a recommendation (applied / ignored / adjusted)."""
    claims = require_auth(authorization)
    _enforce_access(claims, payload.hotel_id)

    history = (
        db.query(PricingHistory)
        .filter(PricingHistory.history_id == payload.history_id)
        .first()
    )
    if not history:
        raise HTTPException(status_code=404, detail="History item not found")

    # Update history row
    history.status = payload.action
    if payload.final_price is not None:
        history.applied_price = payload.final_price

    # Also persist to feedback table for richer detail
    fb = RecommendationFeedback(
        feedback_id=f"ex_{uuid4().hex[:12]}",
        history_id=payload.history_id,
        hotel_id=payload.hotel_id,
        action=payload.action,
        final_price=payload.final_price,
        notes=payload.notes,
    )
    db.add(fb)
    db.commit()

    return ExecutionLogResponse(execution_id=fb.feedback_id)


# ---------------------------------------------------------------------------
# POST /outcome
# ---------------------------------------------------------------------------

@router.post("/outcome", response_model=OutcomeLogResponse)
def record_outcome(
    payload: OutcomeLogRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Record actual stay-night performance metrics."""
    claims = require_auth(authorization)
    _enforce_access(claims, payload.hotel_id)

    history = (
        db.query(PricingHistory)
        .filter(PricingHistory.history_id == payload.history_id)
        .first()
    )
    if not history:
        raise HTTPException(status_code=404, detail="History item not found")

    # Store outcome in feedback table
    fb = RecommendationFeedback(
        feedback_id=f"oc_{uuid4().hex[:12]}",
        history_id=payload.history_id,
        hotel_id=payload.hotel_id,
        action="outcome",
        occupancy_outcome=payload.occupancy,
        revenue_outcome=payload.revenue,
        notes=(
            f"adr={payload.adr} revpar={payload.revpar} "
            f"rooms_sold={payload.rooms_sold} satisfaction={payload.guest_satisfaction}"
        ),
    )
    db.add(fb)
    db.commit()

    return OutcomeLogResponse(outcome_id=fb.feedback_id)


# ---------------------------------------------------------------------------
# GET /history/{hotel_id}
# ---------------------------------------------------------------------------

@router.get("/history/{hotel_id}", response_model=list[FeedbackHistoryEntry])
def get_history(
    hotel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Return paginated decision history for a hotel."""
    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)

    rows = (
        db.query(PricingHistory)
        .filter(PricingHistory.hotel_id == hotel_id)
        .order_by(PricingHistory.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    # Join with latest feedback for each history_id
    results: list[FeedbackHistoryEntry] = []
    for row in rows:
        fb = (
            db.query(RecommendationFeedback)
            .filter(RecommendationFeedback.history_id == row.history_id)
            .order_by(RecommendationFeedback.id.desc())
            .first()
        )
        results.append(
            FeedbackHistoryEntry(
                history_id=row.history_id,
                hotel_id=row.hotel_id,
                timestamp=str(row.date) if row.date else None,
                recommended_price=row.recommended_price,
                action=fb.action if fb else row.status,
                final_price=fb.final_price if fb else row.applied_price,
                occupancy_outcome=fb.occupancy_outcome if fb else None,
                revenue_outcome=fb.revenue_outcome if fb else None,
            )
        )
    return results


# ---------------------------------------------------------------------------
# GET /analytics/{hotel_id}
# ---------------------------------------------------------------------------

@router.get("/analytics/{hotel_id}", response_model=FeedbackAnalytics)
def get_analytics(
    hotel_id: str,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Return aggregated accuracy metrics for a hotel."""
    claims = require_auth(authorization)
    _enforce_access(claims, hotel_id)

    histories = (
        db.query(PricingHistory)
        .filter(PricingHistory.hotel_id == hotel_id)
        .all()
    )
    feedbacks = (
        db.query(RecommendationFeedback)
        .filter(RecommendationFeedback.hotel_id == hotel_id)
        .all()
    )

    total = len(histories)
    applied = sum(1 for f in feedbacks if f.action == "applied")
    ignored = sum(1 for f in feedbacks if f.action == "ignored")
    adjusted = sum(1 for f in feedbacks if f.action == "adjusted")

    # Price deviation
    deviations = []
    for fb in feedbacks:
        if fb.final_price is not None:
            hist = next(
                (h for h in histories if h.history_id == fb.history_id), None
            )
            if hist:
                deviations.append(fb.final_price - hist.recommended_price)

    mean_dev = sum(deviations) / len(deviations) if deviations else None

    # Override rate (adjusted / total with feedback)
    total_with_feedback = applied + ignored + adjusted
    override_rate = (
        adjusted / total_with_feedback if total_with_feedback > 0 else None
    )

    return FeedbackAnalytics(
        hotel_id=hotel_id,
        total_recommendations=total,
        applied_count=applied,
        ignored_count=ignored,
        adjusted_count=adjusted,
        mean_price_deviation=round(mean_dev, 2) if mean_dev is not None else None,
        override_rate=round(override_rate, 4) if override_rate is not None else None,
    )
