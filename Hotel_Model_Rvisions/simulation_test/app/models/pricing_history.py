from datetime import date
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, Float, Date
from app.db.base import Base

class PricingHistory(Base):
    __tablename__ = "pricing_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    history_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    hotel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    recommended_price: Mapped[float] = mapped_column(Float, nullable=False)
    applied_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_lift: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    demand_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
    season: Mapped[str | None] = mapped_column(String(32), nullable=True)
