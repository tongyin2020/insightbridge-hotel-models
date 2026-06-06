from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.revenue_analytics import RevenueAnalytics
from app.auth import require_auth
from app.services.pricing_engine import weight_table

router = APIRouter()

@router.get("/revenue-lift")
def revenue_lift(db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    q = db.query(RevenueAnalytics)
    if claims["role"] != "admin":
        q = q.filter(RevenueAnalytics.hotel_id == claims.get("hotel_id"))
    rows = q.all()
    first = rows[0] if rows else None
    return {
        "current_month_lift": first.month_lift if first else "--",
        "quarter_lift": first.quarter_lift if first else "--",
        "applied_rate": first.applied_rate if first else "--",
        "avg_confidence": first.avg_confidence if first else "--",
        "by_hotel": [{"hotel_id": r.hotel_id, "hotel_name": r.hotel_id, "month_lift": r.month_lift or ""} for r in rows],
    }

@router.get("/weights")
def weights(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    return {"rows": weight_table()}

@router.get("/sensitivity")
def sensitivity(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    return {"rows": [
        {"factor": "Occupancy", "revenue_impact": "±5.2%", "tier": "Tier 1"},
        {"factor": "Competitor Price", "revenue_impact": "±4.6%", "tier": "Tier 1"},
        {"factor": "Booking Pace", "revenue_impact": "±3.8%", "tier": "Tier 1"},
        {"factor": "Holiday", "revenue_impact": "±3.5%", "tier": "Tier 2"},
    ]}
