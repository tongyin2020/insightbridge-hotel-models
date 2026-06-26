from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.hotel import Hotel
from app.models.hotel_setting import HotelSetting
from app.auth import require_auth
from app.schemas.settings import HotelSettingsUpdate

router = APIRouter()

@router.get("")
def list_hotels(db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    q = db.query(Hotel)
    if claims["role"] != "admin":
        q = q.filter(Hotel.hotel_id == claims.get("hotel_id"))
    rows = q.all()
    return {"rows": [
        {"hotel_id": r.hotel_id, "name": r.name, "city": r.city, "rooms": r.rooms, "tier": r.tier, "status": r.status}
        for r in rows
    ]}

@router.get("/{hotel_id}/settings")
def get_settings(hotel_id: str, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    if claims["role"] != "admin" and claims.get("hotel_id") != hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    row = db.query(HotelSetting).filter(HotelSetting.hotel_id == hotel_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Settings not found")
    return {"hotel_id": row.hotel_id, "floor_price": row.floor_price, "ceiling_price": row.ceiling_price, "base_price": row.base_price}

@router.put("/{hotel_id}/settings")
def update_settings(hotel_id: str, payload: HotelSettingsUpdate, db: Session = Depends(get_db), authorization: str | None = Header(default=None)):
    claims = require_auth(authorization)
    if claims["role"] != "admin" and claims.get("hotel_id") != hotel_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    row = db.query(HotelSetting).filter(HotelSetting.hotel_id == hotel_id).first()
    if not row:
        row = HotelSetting(hotel_id=hotel_id)
        db.add(row)
    row.floor_price = payload.floor_price
    row.ceiling_price = payload.ceiling_price
    row.base_price = payload.base_price
    db.commit()
    return {"status": "updated", "hotel_id": hotel_id}
