from datetime import date
from .session import SessionLocal
from app.models.hotel import Hotel
from app.models.user import User
from app.models.hotel_setting import HotelSetting
from app.models.pricing_history import PricingHistory
from app.models.revenue_analytics import RevenueAnalytics
from app.auth import hash_password
import os

def run():
    demo_password = os.getenv("DEMO_PASSWORD", "HotelAI2026!")
    db = SessionLocal()
    try:
        if db.query(Hotel).count() > 0:
            print("Seed skipped: data already exists")
            return
        db.add_all([
            Hotel(hotel_id="hotel_demo_001", name="Macau Central Hotel", city="Macau", rooms=120, tier="3-star", status="active"),
            Hotel(hotel_id="hotel_demo_002", name="Harbor View Suites", city="Macau", rooms=88, tier="4-star", status="active"),
            User(user_id="user_admin_001", hotel_id=None, email="admin@company.com", password_hash=hash_password(demo_password), role="admin", status="active"),
            User(user_id="user_hotel_001", hotel_id="hotel_demo_001", email="hotel@client.com", password_hash=hash_password(demo_password), role="hotel_operator", status="active"),
            HotelSetting(hotel_id="hotel_demo_001", floor_price=750, ceiling_price=1015, base_price=898),
            HotelSetting(hotel_id="hotel_demo_002", floor_price=760, ceiling_price=1025, base_price=920),
            PricingHistory(history_id="ph_seed001", hotel_id="hotel_demo_001", date=date(2026,3,20), recommended_price=910, applied_price=905, expected_lift="+6.1%", status="applied", demand_score=0.32, confidence="High", season="shoulder"),
            RevenueAnalytics(hotel_id="hotel_demo_001", month_lift="+6.8%", quarter_lift="+7.4%", applied_rate="84%", avg_confidence="High"),
            RevenueAnalytics(hotel_id="hotel_demo_002", month_lift="+5.9%", quarter_lift="+6.5%", applied_rate="81%", avg_confidence="Medium"),
        ])
        db.commit()
        print("Seed completed")
    finally:
        db.close()

if __name__ == "__main__":
    run()
