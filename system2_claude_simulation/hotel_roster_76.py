"""
hotel_roster_76.py — 澳门旅游局官方76家3-5星酒店名单
来源：hotel_data_collector.HOTELS_76（澳门旅游局MGTO官方分级名单）

供三大系统共用：
  - System 1 ChatGPT版 (run_21d_harness.py)
  - System 2 Claude版  (run_simulation.py)
  - System 3 CrewAI版  (main.py → 通过 run_simulation 导入)

星级分布：3★ 18家 | 4★ 18家 | 5★ 28家 | 5★豪华 12家 = 共76家
"""

from __future__ import annotations
import sys
from pathlib import Path

# ── 导入官方名单 ────────────────────────────────────────────────────────────
_COLLECTOR_DIR = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_collector")
if str(_COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTOR_DIR))

try:
    from hotel_data_collector import HOTELS_76 as _RAW_76
except ImportError as e:
    raise ImportError(f"无法导入HOTELS_76: {e}. 请确认hotel_data_collector.py路径正确。")

# ── 区域代码映射 ────────────────────────────────────────────────────────────
_AREA_CODE = {
    "澳门半岛": "PENIN",
    "路氹城":   "COTAI",
    "氹仔":     "TAIPA",
    "路环":     "COLOANE",
}

# ── 各档次默认底价（运行时会被compute_dynamic_base_price覆盖）──────────────
_BASE_PRICE = {
    "5_deluxe": 2200.0,   # 五星豪华：永利/四季/美高梅级别
    "5_star":   1500.0,   # 五星：威尼斯人/君悦/瑞吉级别
    "4_star":    980.0,   # 四星：新濠影汇/利澳/维景级别
    "3_star":    680.0,   # 三星：帝濠/财神/东望洋级别
}

# ── 星级数字映射（5_deluxe 按5★处理）──────────────────────────────────────
_STAR_NUM = {
    "5_deluxe": 5,
    "5_star":   5,
    "4_star":   4,
    "3_star":   3,
}


def _build() -> tuple[list[dict], list[dict]]:
    """
    将HOTELS_76转换为仿真系统所需格式，返回 (hotels_3star, hotels_45star)。

    hotel dict 字段:
        hotel_id     : str   — 原始MGTO ID，如 MAC_5DX_WYNN_001
        name         : str   — 中文名称
        en           : str   — 英文名称
        star         : int   — 数字星级 3/4/5
        tier         : str   — 原始档次 5_deluxe/5_star/4_star/3_star
        base_price   : float — 默认底价（运行时由DSEC+MakCorps覆盖）
        total_rooms  : int   — 真实客房数
        district     : str   — 区域代码
        area         : str   — 中文区域名
        market_segment: str | None — macau_luxury_direct(4-5★) / None(3★)
        booking_com_id: str  — Booking.com ID（供MakCorps采集）
    """
    hotels_3: list[dict] = []
    hotels_45: list[dict] = []

    for h in _RAW_76:
        tier = h["tier"]
        star = _STAR_NUM[tier]
        entry = {
            "hotel_id":       h["id"],
            "name":           h["cn"],
            "en":             h["en"],
            "star":           star,
            "tier":           tier,
            "base_price":     _BASE_PRICE[tier],
            "total_rooms":    h["rooms"],
            "district":       _AREA_CODE.get(h["area"], "PENIN"),
            "area":           h["area"],
            "market_segment": "macau_luxury_direct" if star >= 4 else None,
            "booking_com_id": h.get("booking_com_id", ""),
        }
        if star == 3:
            hotels_3.append(entry)
        else:
            hotels_45.append(entry)

    return hotels_3, hotels_45


# ── 模块级列表（固定，供全局直接引用）─────────────────────────────────────
HOTELS_3STAR, HOTELS_45STAR = _build()
ALL_HOTELS_76: list[dict] = HOTELS_3STAR + HOTELS_45STAR

assert len(ALL_HOTELS_76) == 76,  f"酒店数量异常: {len(ALL_HOTELS_76)}"
assert len(HOTELS_3STAR)  == 18,  f"3★数量异常: {len(HOTELS_3STAR)}"
assert len(HOTELS_45STAR) == 58,  f"4-5★数量异常: {len(HOTELS_45STAR)}"


if __name__ == "__main__":
    print(f"✅ 澳门旅游局官方酒店名单加载成功")
    print(f"   3★: {len(HOTELS_3STAR)}家  |  4-5★: {len(HOTELS_45STAR)}家  |  合计: {len(ALL_HOTELS_76)}家")
    print()
    from collections import Counter
    tier_counts = Counter(h["tier"] for h in ALL_HOTELS_76)
    area_counts = Counter(h["area"] for h in ALL_HOTELS_76)
    print("按档次:", dict(tier_counts))
    print("按区域:", dict(area_counts))
    print()
    print("3★ 示例:")
    for h in HOTELS_3STAR[:3]:
        print(f"  {h['hotel_id']} {h['name']} rooms={h['total_rooms']}")
    print("4★ 示例:")
    for h in HOTELS_45STAR[:3]:
        print(f"  {h['hotel_id']} {h['name']} rooms={h['total_rooms']}")
