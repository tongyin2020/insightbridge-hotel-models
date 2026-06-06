from collections import defaultdict
from typing import Iterable
from .schemas_v6 import WeeklyHotelRecord

class HotelLearningLoop:
    """Turns weekly Excel summaries into hotel-specific calibration signals.
    Input granularity: date x room_type x channel, not individual bookings.
    """

    def summarize(self, records: Iterable[WeeklyHotelRecord]) -> dict:
        by_room = defaultdict(lambda: {"rooms": 0.0, "revenue": 0.0})
        by_channel = defaultdict(lambda: {"rooms": 0.0, "revenue": 0.0})
        total_rooms = 0.0
        total_revenue = 0.0
        for r in records:
            total_rooms += r.rooms_sold
            total_revenue += r.revenue
            by_room[r.room_type]["rooms"] += r.rooms_sold
            by_room[r.room_type]["revenue"] += r.revenue
            by_channel[r.channel]["rooms"] += r.rooms_sold
            by_channel[r.channel]["revenue"] += r.revenue
        return {
            "total_rooms_sold": total_rooms,
            "total_revenue": total_revenue,
            "adr": total_revenue / total_rooms if total_rooms else 0.0,
            "room_type_adr": {k: v["revenue"] / v["rooms"] for k, v in by_room.items() if v["rooms"]},
            "channel_adr": {k: v["revenue"] / v["rooms"] for k, v in by_channel.items() if v["rooms"]},
            "channel_mix": {k: v["rooms"] / total_rooms for k, v in by_channel.items()} if total_rooms else {},
        }

    def update_hotel_profile(self, current_profile: dict, weekly_summary: dict, learning_rate: float = 0.25) -> dict:
        """Simple exponential smoothing profile update. Replace with LightGBM later after enough samples."""
        profile = dict(current_profile or {})
        profile["baseline_adr"] = self._smooth(profile.get("baseline_adr"), weekly_summary.get("adr"), learning_rate)
        profile["channel_mix"] = weekly_summary.get("channel_mix", profile.get("channel_mix", {}))
        profile["room_type_adr"] = weekly_summary.get("room_type_adr", profile.get("room_type_adr", {}))
        profile["channel_adr"] = weekly_summary.get("channel_adr", profile.get("channel_adr", {}))
        profile["calibration_weeks"] = int(profile.get("calibration_weeks", 0)) + 1
        profile["calibration_status"] = "first_calibration" if profile["calibration_weeks"] >= 4 else "learning"
        return profile

    def _smooth(self, old, new, lr):
        if new is None:
            return old
        if old is None:
            return new
        return old * (1 - lr) + new * lr
