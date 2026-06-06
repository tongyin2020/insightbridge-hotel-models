from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, Float
from app.db.base import Base

class HotelSetting(Base):
    __tablename__ = "hotel_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    floor_price: Mapped[float] = mapped_column(Float, default=750)
    ceiling_price: Mapped[float] = mapped_column(Float, default=1015)
    base_price: Mapped[float] = mapped_column(Float, default=898)
