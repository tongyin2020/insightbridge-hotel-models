"""
Bundle API endpoints (P1-3).

Provides active bundle offers and performance reporting per hotel.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db.session import get_db
from app.models.hotel_setting import HotelSetting
from app.services.bundle_engine import generate_bundle_offers

router = APIRouter()


def _authorize(authorization: str | None, hotel_id: str) -> dict:
    claims = require_auth(authorization)
    if claims["role"] != "admin" and claims.get("hotel_id") != hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return claims


@router.get("/{hotel_id}/active")
def get_active_bundles(
    hotel_id: str,
    demand_state: str = Query("LOW", description="Current demand state (HIGH, NORMAL, LOW)"),
    occupancy_rate: float = Query(0.5, ge=0, le=1, description="Current occupancy ratio 0-1"),
    season: str = Query("shoulder", description="Season: off_peak, shoulder, peak, super_peak"),
    base_rate: float = Query(800, gt=0, description="Current base room rate (MOP)"),
    scarcity_index: float = Query(0.5, ge=0, le=1, description="Scarcity index 0-1"),
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """Return currently active bundle offers for a hotel."""
    _authorize(authorization, hotel_id)

    # Optionally pull base_rate from hotel settings if not overridden
    settings = db.query(HotelSetting).filter(HotelSetting.hotel_id == hotel_id).first()
    if settings and base_rate == 800:
        base_rate = getattr(settings, "default_base_price", base_rate) or base_rate

    offers = generate_bundle_offers(
        demand_state=demand_state.upper(),
        occupancy_rate=occupancy_rate,
        season=season,
        base_rate=base_rate,
        scarcity_index=scarcity_index,
    )

    return {
        "hotel_id": hotel_id,
        "demand_state": demand_state.upper(),
        "occupancy_rate": occupancy_rate,
        "bundle_count": len(offers),
        "bundles": [o.to_dict() for o in offers],
    }


@router.get("/{hotel_id}/performance")
def get_bundle_performance(
    hotel_id: str,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    """
    Return bundle performance metrics for a hotel.

    In production this would query a bundle_redemptions table.  For now
    we return a stub with the expected schema so the frontend can integrate.
    """
    _authorize(authorization, hotel_id)

    # Stub — replace with real query once bundle_redemptions table exists
    return {
        "hotel_id": hotel_id,
        "period": "last_30_days",
        "total_bundles_offered": 0,
        "total_bundles_redeemed": 0,
        "redemption_rate": 0.0,
        "incremental_revenue": 0.0,
        "avg_discount_given": 0.0,
        "top_bundles": [],
        "note": "Bundle performance tracking will populate once redemption data is recorded.",
    }
