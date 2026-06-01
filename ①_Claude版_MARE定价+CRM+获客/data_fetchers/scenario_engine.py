"""
scenario_engine.py — 酒店内部数据场景模拟器
================================================================
核心问题：真实酒店内部数据（入住率、预订节奏、CRM记录、PSRS状态等）
          无法从外部获取，但没有这些数据模型测试毫无意义。

解决方案：定义14个覆盖"正常运营、极端边界、系统故障、市场危机"
          的标准场景，系统性地轮换应用于所有425家酒店。

效果：
  - 每家酒店在21天内遍历全部14个场景多次（约36轮）
  - 同一小时内不同酒店覆盖不同场景 → 发现场景间的系统性差异
  - 极端场景（价格战、需求崩溃、PSRS故障）暴露模型的边界稳定性
  - 与实时外部数据（天气/渡轮/Booking.com）叠加 → 混合真实+模拟
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HotelScenario:
    name: str
    description_cn: str

    # ── 房间/库存内部数据 ─────────────────────────────────────────────
    occupancy: float            # 当前入住率 0.0-1.0
    booking_velocity_24h: float # 过去24小时预订速度 0.0-1.0
    days_to_arrival: int        # 距入住天数（0=当天，90=超前预订）
    cancellation_rate: float    # 近期取消率 0.0-0.5
    remaining_inventory_ratio: float  # 剩余可售比 = 1-occupancy（允许超卖场景）

    # ── 客户内部画像 ──────────────────────────────────────────────────
    guest_segment: str          # budget/corporate/luxury_leisure/casino_vip/hengqin_dual
    avg_clv: float              # 客户终身价值（MOP）
    loyalty_tier: str           # none/bronze/silver/gold/platinum
    churn_risk: float           # 流失风险 0.0-1.0
    previous_price: float | None  # 上次成交价（None=无历史记录）

    # ── 竞对价格调整系数（叠加在Booking.com实时价格上）──────────────
    competitor_price_multiplier: float  # 1.0=正常, 0.6=价格战, 1.8=竞对涨价

    # ── 系统健康状态（内部IT基础设施）───────────────────────────────
    psrs_health: str            # healthy / degraded / error
    crm_match_rate_override: float | None  # None=使用默认逻辑，0-1=强制覆盖

    # ── 渠道分布权重 [直销网站, OTA-Booking, OTA-Agoda, 散客, WhatsApp, 电话]
    channel_weights: tuple      # 6个元素，相对权重

    # ── 市场级信号（当Firecrawl/实时数据不可用时的场景化替代值）──────
    # 这三个因子在Playwright版也无实时来源；CrewAI版用Firecrawl尝试，
    # 但若FC也失败，则使用这里的值替代简单统计估算，覆盖全值域。
    sim_border_flow: float       # 口岸客流信号 -1~1（0=平均，1=爆满，-1=冷清）
    sim_zhuhai_saturation: float # 珠海酒店饱和度 0~1
    sim_ota_booking_pace: float  # OTA预订节奏 0~1

    # ── 场景分类标签（用于报告分析）─────────────────────────────────
    category: str               # normal/peak/crisis/stress/market_shock


# ══════════════════════════════════════════════════════════════════════
#  14个标准测试场景
#  设计原则：
#  ① 4个正常/良好场景    ② 3个极端高需求场景
#  ③ 3个危机/低需求场景  ④ 2个市场冲击场景
#  ⑤ 2个系统故障场景
# ══════════════════════════════════════════════════════════════════════
SCENARIOS: list[HotelScenario] = [

    # ─── ① 正常运营场景 ─────────────────────────────────────────────
    HotelScenario(
        name="NORMAL_OPS",
        description_cn="正常运营（平日均衡状态）",
        occupancy=0.65, booking_velocity_24h=0.25,
        days_to_arrival=7, cancellation_rate=0.10,
        remaining_inventory_ratio=0.35,
        guest_segment="budget", avg_clv=800,
        loyalty_tier="none", churn_risk=0.30,
        previous_price=None,
        competitor_price_multiplier=1.0,
        psrs_health="healthy", crm_match_rate_override=None,
        channel_weights=(12, 42, 28, 8, 6, 4),
        sim_border_flow=0.45, sim_zhuhai_saturation=0.30, sim_ota_booking_pace=0.35,
        category="normal",
    ),

    HotelScenario(
        name="STEADY_CORPORATE",
        description_cn="稳定商务客源（中高忠诚度）",
        occupancy=0.72, booking_velocity_24h=0.32,
        days_to_arrival=5, cancellation_rate=0.08,
        remaining_inventory_ratio=0.28,
        guest_segment="corporate", avg_clv=4500,
        loyalty_tier="gold", churn_risk=0.12,
        previous_price=850.0,
        competitor_price_multiplier=1.0,
        psrs_health="healthy", crm_match_rate_override=0.72,
        channel_weights=(30, 28, 15, 5, 18, 4),
        sim_border_flow=0.55, sim_zhuhai_saturation=0.38, sim_ota_booking_pace=0.42,
        category="normal",
    ),

    HotelScenario(
        name="LOYAL_REPEAT_GUEST",
        description_cn="忠诚回头客为主（白金/黄金会员）",
        occupancy=0.68, booking_velocity_24h=0.28,
        days_to_arrival=10, cancellation_rate=0.05,
        remaining_inventory_ratio=0.32,
        guest_segment="luxury_leisure", avg_clv=12000,
        loyalty_tier="platinum", churn_risk=0.05,
        previous_price=920.0,
        competitor_price_multiplier=1.02,
        psrs_health="healthy", crm_match_rate_override=0.88,
        channel_weights=(48, 12, 8, 2, 26, 4),
        sim_border_flow=0.50, sim_zhuhai_saturation=0.35, sim_ota_booking_pace=0.38,
        category="normal",
    ),

    HotelScenario(
        name="ADVANCE_BOOKING_WAVE",
        description_cn="提前75天大量预订（早鸟波）",
        occupancy=0.42, booking_velocity_24h=0.18,
        days_to_arrival=75, cancellation_rate=0.22,
        remaining_inventory_ratio=0.58,
        guest_segment="luxury_leisure", avg_clv=8000,
        loyalty_tier="silver", churn_risk=0.10,
        previous_price=None,
        competitor_price_multiplier=0.98,
        psrs_health="healthy", crm_match_rate_override=0.65,
        channel_weights=(38, 22, 12, 3, 20, 5),
        sim_border_flow=0.30, sim_zhuhai_saturation=0.25, sim_ota_booking_pace=0.20,
        category="normal",
    ),

    # ─── ② 极端高需求场景 ───────────────────────────────────────────
    HotelScenario(
        name="PEAK_FULLHOUSE",
        description_cn="满房危机（节假日爆满，入住率97%）",
        occupancy=0.97, booking_velocity_24h=0.92,
        days_to_arrival=0, cancellation_rate=0.02,
        remaining_inventory_ratio=0.03,
        guest_segment="budget", avg_clv=600,
        loyalty_tier="none", churn_risk=0.05,
        previous_price=None,
        competitor_price_multiplier=1.30,
        psrs_health="healthy", crm_match_rate_override=0.28,
        channel_weights=(5, 52, 35, 6, 1, 1),
        sim_border_flow=0.88, sim_zhuhai_saturation=0.78, sim_ota_booking_pace=0.92,
        category="peak",
    ),

    HotelScenario(
        name="LAST_MINUTE_SURGE",
        description_cn="同日急订浪潮（24h预订速度0.95）",
        occupancy=0.78, booking_velocity_24h=0.95,
        days_to_arrival=0, cancellation_rate=0.04,
        remaining_inventory_ratio=0.22,
        guest_segment="corporate", avg_clv=3200,
        loyalty_tier="silver", churn_risk=0.12,
        previous_price=780.0,
        competitor_price_multiplier=1.18,
        psrs_health="healthy", crm_match_rate_override=0.50,
        channel_weights=(18, 30, 22, 22, 6, 2),
        sim_border_flow=0.72, sim_zhuhai_saturation=0.62, sim_ota_booking_pace=0.88,
        category="peak",
    ),

    HotelScenario(
        name="OVERFLOW_SQUEEZE",
        description_cn="超额预订压力（理论入住率>100%，需拒订）",
        occupancy=0.99, booking_velocity_24h=0.88,
        days_to_arrival=0, cancellation_rate=0.01,
        remaining_inventory_ratio=0.00,
        guest_segment="budget", avg_clv=500,
        loyalty_tier="none", churn_risk=0.02,
        previous_price=None,
        competitor_price_multiplier=1.45,
        psrs_health="degraded", crm_match_rate_override=0.18,
        channel_weights=(3, 54, 38, 4, 0, 1),
        sim_border_flow=0.95, sim_zhuhai_saturation=0.90, sim_ota_booking_pace=0.98,
        category="peak",
    ),

    # ─── ③ 危机/低需求场景 ──────────────────────────────────────────
    HotelScenario(
        name="DEMAND_COLLAPSE",
        description_cn="需求崩溃（入住率18%，极端低迷）",
        occupancy=0.18, booking_velocity_24h=0.03,
        days_to_arrival=14, cancellation_rate=0.40,
        remaining_inventory_ratio=0.82,
        guest_segment="budget", avg_clv=400,
        loyalty_tier="none", churn_risk=0.78,
        previous_price=550.0,
        competitor_price_multiplier=0.78,
        psrs_health="healthy", crm_match_rate_override=0.15,
        channel_weights=(8, 55, 30, 5, 1, 1),
        sim_border_flow=-0.35, sim_zhuhai_saturation=0.12, sim_ota_booking_pace=0.05,
        category="crisis",
    ),

    HotelScenario(
        name="HIGH_CANCELLATION_STORM",
        description_cn="取消率暴增（45%取消，预订恢复缓慢）",
        occupancy=0.58, booking_velocity_24h=0.52,
        days_to_arrival=10, cancellation_rate=0.45,
        remaining_inventory_ratio=0.42,
        guest_segment="budget", avg_clv=600,
        loyalty_tier="none", churn_risk=0.62,
        previous_price=680.0,
        competitor_price_multiplier=0.88,
        psrs_health="degraded", crm_match_rate_override=0.22,
        channel_weights=(8, 56, 28, 6, 1, 1),
        sim_border_flow=0.20, sim_zhuhai_saturation=0.22, sim_ota_booking_pace=0.18,
        category="crisis",
    ),

    HotelScenario(
        name="MIXED_CRISIS",
        description_cn="复合危机（低入住+价格战+高取消+系统降级）",
        occupancy=0.22, booking_velocity_24h=0.06,
        days_to_arrival=20, cancellation_rate=0.42,
        remaining_inventory_ratio=0.78,
        guest_segment="budget", avg_clv=350,
        loyalty_tier="none", churn_risk=0.82,
        previous_price=620.0,
        competitor_price_multiplier=0.62,
        psrs_health="degraded", crm_match_rate_override=0.10,
        channel_weights=(5, 58, 30, 5, 1, 1),
        sim_border_flow=-0.50, sim_zhuhai_saturation=0.08, sim_ota_booking_pace=0.03,
        category="crisis",
    ),

    # ─── ④ 市场冲击场景 ─────────────────────────────────────────────
    HotelScenario(
        name="PRICE_WAR",
        description_cn="竞对发动价格战（全面降价40%）",
        occupancy=0.55, booking_velocity_24h=0.20,
        days_to_arrival=5, cancellation_rate=0.18,
        remaining_inventory_ratio=0.45,
        guest_segment="budget", avg_clv=500,
        loyalty_tier="none", churn_risk=0.52,
        previous_price=720.0,
        competitor_price_multiplier=0.60,
        psrs_health="healthy", crm_match_rate_override=None,
        channel_weights=(10, 52, 32, 4, 1, 1),
        sim_border_flow=0.35, sim_zhuhai_saturation=0.28, sim_ota_booking_pace=0.22,
        category="market_shock",
    ),

    HotelScenario(
        name="COMPETITOR_SPIKE",
        description_cn="竞对涨价80%（稀缺溢价机会窗口）",
        occupancy=0.88, booking_velocity_24h=0.75,
        days_to_arrival=1, cancellation_rate=0.04,
        remaining_inventory_ratio=0.12,
        guest_segment="corporate", avg_clv=5500,
        loyalty_tier="gold", churn_risk=0.08,
        previous_price=860.0,
        competitor_price_multiplier=1.80,
        psrs_health="healthy", crm_match_rate_override=0.62,
        channel_weights=(25, 30, 20, 15, 8, 2),
        sim_border_flow=0.75, sim_zhuhai_saturation=0.72, sim_ota_booking_pace=0.82,
        category="market_shock",
    ),

    # ─── ⑤ 系统故障场景 ─────────────────────────────────────────────
    HotelScenario(
        name="PSRS_FAILURE",
        description_cn="PSRS系统完全故障（数据同步中断）",
        occupancy=0.70, booking_velocity_24h=0.40,
        days_to_arrival=3, cancellation_rate=0.15,
        remaining_inventory_ratio=0.30,
        guest_segment="corporate", avg_clv=2500,
        loyalty_tier="silver", churn_risk=0.28,
        previous_price=780.0,
        competitor_price_multiplier=1.0,
        psrs_health="error", crm_match_rate_override=0.05,
        channel_weights=(15, 40, 30, 10, 3, 2),
        sim_border_flow=0.48, sim_zhuhai_saturation=0.35, sim_ota_booking_pace=0.40,
        category="stress",
    ),

    HotelScenario(
        name="OTA_MONOPOLY",
        description_cn="OTA完全垄断（95%依赖OTA，直销为零）",
        occupancy=0.75, booking_velocity_24h=0.35,
        days_to_arrival=4, cancellation_rate=0.12,
        remaining_inventory_ratio=0.25,
        guest_segment="budget", avg_clv=700,
        loyalty_tier="none", churn_risk=0.38,
        previous_price=None,
        competitor_price_multiplier=1.0,
        psrs_health="healthy", crm_match_rate_override=0.08,
        channel_weights=(1, 62, 32, 3, 1, 1),
        sim_border_flow=0.60, sim_zhuhai_saturation=0.45, sim_ota_booking_pace=0.88,
        category="stress",
    ),
]

NUM_SCENARIOS: int = len(SCENARIOS)
SCENARIO_MAP: dict[str, HotelScenario] = {s.name: s for s in SCENARIOS}


def get_scenario(hotel_index: int, sim_hour: int) -> HotelScenario:
    """
    为每家酒店在每个小时分配一个场景（交错式覆盖）。

    算法设计：
      idx = (hotel_index + sim_hour * 3) % 14

    效果：
      - 同一小时内145家2-3星酒店同时覆盖多个场景（不重复堆在同一场景）
      - 21天内每家酒店遍历全部14个场景约36次
      - 场景内数字 × 3 确保连续小时不重复同一场景
    """
    return SCENARIOS[(hotel_index + sim_hour * 3) % NUM_SCENARIOS]


def get_scenario_stats() -> str:
    """返回场景分布统计（用于启动时打印）"""
    cats = {}
    for s in SCENARIOS:
        cats.setdefault(s.category, []).append(s.name)
    lines = [f"  ── 测试场景（共{NUM_SCENARIOS}个）──────────────────────"]
    cat_names = {"normal": "正常运营", "peak": "极端高需求", "crisis": "危机低迷",
                 "market_shock": "市场冲击", "stress": "系统压力"}
    for cat, names in cats.items():
        lines.append(f"  {cat_names.get(cat, cat)}: {', '.join(names)}")
    return "\n".join(lines)
