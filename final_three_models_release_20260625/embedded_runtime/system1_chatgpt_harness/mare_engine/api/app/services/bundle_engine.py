"""
Off-peak Conversion / Bundle Engine (P1-3).

Generates conversion-focused bundle offers when demand is low or occupancy
is below threshold, helping drive incremental revenue during soft periods.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, asdict


@dataclass
class BundleOffer:
    bundle_id: str
    name: str
    name_zh: str
    components: list[str]
    total_value: float
    bundle_price: float
    discount_pct: float
    target_segment: str
    validity_hours: int

    def to_dict(self) -> dict:
        return asdict(self)


def _make_id() -> str:
    return f"bndl_{uuid.uuid4().hex[:10]}"


def generate_bundle_offers(
    demand_state: str,
    occupancy_rate: float,
    season: str,
    base_rate: float,
    scarcity_index: float = 0.5,
) -> list[BundleOffer]:
    """
    Generate conversion-focused bundle offers for off-peak periods.

    Only generates when *demand_state* is ``LOW`` or *occupancy_rate* < 0.65.
    Returns an empty list otherwise so callers can safely iterate.

    Parameters
    ----------
    demand_state : str
        One of ``HIGH``, ``NORMAL``, ``LOW``.
    occupancy_rate : float
        Current occupancy as a ratio 0-1.
    season : str
        ``off_peak``, ``shoulder``, ``peak``, ``super_peak``.
    base_rate : float
        Current base room rate (MOP).
    scarcity_index : float
        0 (abundant) to 1 (critical). Higher values suppress aggressive
        discounting.
    """
    # Only generate bundles in soft-demand conditions
    if demand_state not in ("LOW",) and occupancy_rate >= 0.65:
        return []

    offers: list[BundleOffer] = []

    # Discount aggression: more aggressive when occupancy is very low and
    # scarcity is not a concern
    aggression = max(0.0, 1.0 - scarcity_index) * max(0.0, 1.0 - occupancy_rate)

    # ---- 1. Early check-in + late checkout -----------------------------------
    if occupancy_rate < 0.50:
        # Free upgrade when hotel is very empty
        early_late_value = base_rate * 0.15
        offers.append(BundleOffer(
            bundle_id=_make_id(),
            name="Early Check-in & Late Checkout",
            name_zh="提前入住及延遲退房",
            components=["Early check-in (12:00)", "Late checkout (16:00)"],
            total_value=round(base_rate + early_late_value, 2),
            bundle_price=round(base_rate, 2),
            discount_pct=round(early_late_value / (base_rate + early_late_value) * 100, 1),
            target_segment="leisure",
            validity_hours=48,
        ))
    else:
        surcharge = base_rate * 0.08
        total = base_rate + surcharge
        bundle_price = round(total * 0.92, 2)
        offers.append(BundleOffer(
            bundle_id=_make_id(),
            name="Early Check-in & Late Checkout",
            name_zh="提前入住及延遲退房",
            components=["Early check-in (14:00)", "Late checkout (14:00)"],
            total_value=round(total, 2),
            bundle_price=bundle_price,
            discount_pct=round((1 - bundle_price / total) * 100, 1),
            target_segment="leisure",
            validity_hours=24,
        ))

    # ---- 2. Breakfast package ------------------------------------------------
    breakfast_value = base_rate * 0.12  # ~12% of room rate
    total_breakfast = base_rate + breakfast_value
    breakfast_price = round(total_breakfast * 0.85, 2)
    offers.append(BundleOffer(
        bundle_id=_make_id(),
        name="Room + Breakfast Package",
        name_zh="含早餐住宿套餐",
        components=["Standard room", "Buffet breakfast for 2"],
        total_value=round(total_breakfast, 2),
        bundle_price=breakfast_price,
        discount_pct=15.0,
        target_segment="leisure",
        validity_hours=72,
    ))

    # ---- 3. Transit package --------------------------------------------------
    shuttle_value = base_rate * 0.08
    total_transit = base_rate + shuttle_value
    transit_price = round(total_transit * 0.90, 2)
    offers.append(BundleOffer(
        bundle_id=_make_id(),
        name="Transit Stay Package",
        name_zh="中轉住宿套餐",
        components=["Standard room", "Airport/Ferry shuttle transfer"],
        total_value=round(total_transit, 2),
        bundle_price=transit_price,
        discount_pct=10.0,
        target_segment="transit",
        validity_hours=24,
    ))

    # ---- 4. Same-day flash deal (walk-in) ------------------------------------
    flash_discount = 0.20 + 0.05 * aggression  # up to 25% when very soft
    flash_price = round(base_rate * (1 - flash_discount), 2)
    offers.append(BundleOffer(
        bundle_id=_make_id(),
        name="Same-Day Flash Deal",
        name_zh="即日快閃優惠",
        components=["Standard room (same-day only)"],
        total_value=round(base_rate, 2),
        bundle_price=flash_price,
        discount_pct=round(flash_discount * 100, 1),
        target_segment="walk_in",
        validity_hours=6,
    ))

    # ---- 5. Weekend escape (Fri-Sun) ----------------------------------------
    if season in ("off_peak", "shoulder"):
        weekend_nights = 2
        total_weekend = base_rate * weekend_nights
        weekend_discount = 0.10 + 0.03 * aggression
        weekend_price = round(total_weekend * (1 - weekend_discount), 2)
        offers.append(BundleOffer(
            bundle_id=_make_id(),
            name="Weekend Escape (Fri-Sun)",
            name_zh="週末度假套餐 (五至日)",
            components=["2-night stay (Fri-Sun)", "Complimentary minibar"],
            total_value=round(total_weekend, 2),
            bundle_price=weekend_price,
            discount_pct=round(weekend_discount * 100, 1),
            target_segment="leisure",
            validity_hours=96,
        ))

    # ---- 6. Long-stay discount (3+ nights) -----------------------------------
    long_nights = 3
    total_long = base_rate * long_nights
    long_discount = 0.12 + 0.04 * aggression
    long_price = round(total_long * (1 - long_discount), 2)
    offers.append(BundleOffer(
        bundle_id=_make_id(),
        name="Long Stay Discount (3+ Nights)",
        name_zh="長住優惠 (三晚以上)",
        components=["3-night minimum stay", "Daily housekeeping", "Late checkout"],
        total_value=round(total_long, 2),
        bundle_price=long_price,
        discount_pct=round(long_discount * 100, 1),
        target_segment="extended_stay",
        validity_hours=168,
    ))

    return offers
