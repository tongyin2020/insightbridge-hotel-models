from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer
from app.db.base import Base

class RevenueAnalytics(Base):
    __tablename__ = "revenue_analytics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    month_lift: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quarter_lift: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_rate: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avg_confidence: Mapped[str | None] = mapped_column(String(32), nullable=True)
