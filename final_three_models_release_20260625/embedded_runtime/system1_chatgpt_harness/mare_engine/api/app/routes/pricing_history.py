from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.pricing_history import PricingHistory
from app.auth import require_auth

router = APIRouter()

@router.get("")
def pricing_history(db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    q = db.query(PricingHistory)
    if claims["role"] != "admin":
        q = q.filter(PricingHistory.hotel_id == claims.get("hotel_id"))
    rows = q.order_by(PricingHistory.date.desc()).all()
    return {"rows": [
        {
            "history_id": r.history_id,
            "date": str(r.date),
            "hotel_id": r.hotel_id,
            "recommended_price": f"MOP {int(r.recommended_price)}",
            "applied_price": f"MOP {int(r.applied_price)}" if r.applied_price is not None else "",
            "expected_lift": r.expected_lift or "",
            "status": r.status,
            "confidence": r.confidence or "",
        }
        for r in rows
    ]}
