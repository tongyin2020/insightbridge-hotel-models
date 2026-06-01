from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import String, Integer
from app.db.base import Base

class Hotel(Base):
    __tablename__ = "hotels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotel_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    rooms: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
