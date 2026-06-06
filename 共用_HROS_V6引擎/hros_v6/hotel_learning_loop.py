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
        """Simple exponential smoothing profile update. Replace with LightGBM later after enough samples.

        Fix P1-A (DeepSeek/Gemini): channel_mix now also uses exponential smoothing instead of
        direct replacement, preventing single-week anomalies from overwriting historical mix data.

        Fix P1-B: calibration_status semantics clarified:
          "learning"            — < 4 weeks, profile still stabilising
          "first_calibration"   — >= 4 weeks, profile has completed at least one full calibration cycle
        """
        if learning_rate <= 0 or learning_rate > 1:
            raise ValueError(f"learning_rate must be in (0, 1], got {learning_rate}")

        profile = dict(current_profile or {})
        profile["baseline_adr"] = self._smooth(profile.get("baseline_adr"), weekly_summary.get("adr"), learning_rate)

        # --- P1-A FIX: smooth channel_mix per-channel rather than direct overwrite ---
        new_mix = weekly_summary.get("channel_mix", {})
        old_mix = profile.get("channel_mix", {})
        all_channels = set(new_mix) | set(old_mix)
        smoothed_mix = {}
        for ch in all_channels:
            smoothed_mix[ch] = self._smooth(old_mix.get(ch), new_mix.get(ch), learning_rate)
        # Re-normalise so mix sums to 1.0 (smoothing can shift the sum slightly)
        total = sum(smoothed_mix.values())
        if total > 0:
            smoothed_mix = {k: v / total for k, v in smoothed_mix.items()}
        profile["channel_mix"] = smoothed_mix if smoothed_mix else old_mix

        # room_type_adr and channel_adr: smooth per key as well
        for key in ("room_type_adr", "channel_adr"):
            new_d = weekly_summary.get(key, {})
            old_d = profile.get(key, {})
            merged = {k: self._smooth(old_d.get(k), new_d.get(k), learning_rate)
                      for k in set(new_d) | set(old_d)}
            profile[key] = merged if merged else old_d

        profile["calibration_weeks"] = int(profile.get("calibration_weeks", 0)) + 1
        # P1-B: semantics — "learning" while < 4 weeks, "first_calibration" once >= 4 weeks
        profile["calibration_status"] = "learning" if profile["calibration_weeks"] < 4 else "first_calibration"
        return profile

    def _smooth(self, old, new, lr):
        if new is None:
            return old
        if old is None:
            return new
        return old * (1 - lr) + new * lr
