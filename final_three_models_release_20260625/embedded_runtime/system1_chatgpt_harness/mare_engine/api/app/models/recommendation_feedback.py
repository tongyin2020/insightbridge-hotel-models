from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer, Float, Text
from app.db.base import Base

class RecommendationFeedback(Base):
    __tablename__ = "recommendation_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hotel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    final_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    occupancy_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_outcome: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
