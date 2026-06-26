from datetime import date
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.hotel_setting import HotelSetting
from app.models.pricing_history import PricingHistory
from app.auth import require_auth
from app.schemas.recommendations import RecommendationRequest, FeedbackRequest
from app.services.pricing_engine import recommend

router = APIRouter()

@router.post("")
def get_recommendation(payload: RecommendationRequest, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    if claims["role"] != "admin" and claims.get("hotel_id") != payload.hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    settings = db.query(HotelSetting).filter(HotelSetting.hotel_id == payload.hotel_id).first()
    rec = recommend(payload, settings)
    history = PricingHistory(
        history_id=f"ph_{uuid4().hex[:12]}",
        hotel_id=payload.hotel_id,
        date=date.today(),
        recommended_price=rec["recommended_price"],
        applied_price=None,
        expected_lift=rec["expected_revenue_lift"],
        status="pending",
        demand_score=rec["demand_score"],
        confidence=rec["confidence"],
        season=rec["season"],
    )
    db.add(history)
    db.commit()
    rec["history_id"] = history.history_id
    return rec

@router.post("/feedback")
def save_feedback(payload: FeedbackRequest, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    from app.models.recommendation_feedback import RecommendationFeedback
    claims = require_auth(authorization)
    if claims["role"] != "admin" and claims.get("hotel_id") != payload.hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    row = db.query(PricingHistory).filter(PricingHistory.history_id == payload.history_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="History item not found")
    row.status = payload.action
    if payload.final_price is not None:
        row.applied_price = payload.final_price

    fb = RecommendationFeedback(
        feedback_id=f"fb_{uuid4().hex[:12]}",
        history_id=payload.history_id,
        hotel_id=payload.hotel_id,
        action=payload.action,
        final_price=payload.final_price,
        occupancy_outcome=payload.occupancy_outcome,
        revenue_outcome=payload.revenue_outcome,
        notes=payload.notes,
    )
    db.add(fb)
    db.commit()
    return {"status": "saved", "feedback_id": fb.feedback_id}
