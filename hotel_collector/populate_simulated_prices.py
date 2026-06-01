"""
InsightBridge — 基于澳门统计局数据的模拟市场价格生成器
populate_simulated_prices.py
================================================
核心思路（来自产品决策 2026-06-01）：
  真实抓取数据量不足且质量不稳定。解决方案：
  以澳门统计局(DSEC) 6年历史数据 + 酒店个体溢价因子 + 日期场景乘数
  生成完整的模拟 price_snapshots，作为模型运行的市场参照基准。

数据层次：
  DSEC 市场均值（月份 × 星级）
    × 星级内酒店个体溢价（品牌/位置/档次）
    × 日期场景（周末/节假日/旺季）
    × 随机扰动（±8%，体现真实价格波动）
  = 单一酒店单一入住日的模拟BAR价格

生成规则：
  - 覆盖未来60天入住日期
  - 每家酒店 × 每个入住日 = 1条 price_snapshot
  - source_ok=1, notes="dsec_sim_v1|<场景>"
  - 不覆盖已有 source_ok=1 的真实抓取数据（protect_real=True 时）

使用方法：
  python3 populate_simulated_prices.py            # 全量76家 × 未来60天
  python3 populate_simulated_prices.py --days 30  # 只生成30天
  python3 populate_simulated_prices.py --rebuild  # 清除旧模拟数据重建
  python3 populate_simulated_prices.py --tier 4_star  # 只生成某星级
"""

from __future__ import annotations
import argparse
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 路径设置 ────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "hotel_real_data.db"
sys.path.insert(0, str(BASE_DIR))

from dsec_loader import get_market_adr, get_market_occupancy, init_and_seed
from hotel_data_collector import HOTELS_76


# ══════════════════════════════════════════════════════════════════════════
#  § 1  澳门节假日 & 重大活动日历（用于需求场景判断）
#       数据来源：澳门政府公假 + 旅游局年度大事
# ══════════════════════════════════════════════════════════════════════════

# 特定高峰日期（YYYY-MM-DD）→ 场景标签 & 价格乘数
_SPECIAL_DATES: dict[str, tuple[str, float]] = {}

def _add_range(start: str, end: str, label: str, mult: float):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= e:
        _SPECIAL_DATES[d.strftime("%Y-%m-%d")] = (label, mult)
        d += timedelta(days=1)

# 春节（农历新年）— 浮动，近年澳门春节接待量极高
_add_range("2026-01-28", "2026-02-04", "spring_festival", 1.70)
_add_range("2027-02-15", "2027-02-22", "spring_festival", 1.70)

# 劳动节黄金周（5月1-5日）
_add_range("2026-05-01", "2026-05-05", "labour_day_gw", 1.40)
_add_range("2027-05-01", "2027-05-05", "labour_day_gw", 1.40)

# 国庆节黄金周（10月1-7日）
_add_range("2026-10-01", "2026-10-07", "national_day_gw", 1.55)
_add_range("2027-10-01", "2027-10-07", "national_day_gw", 1.55)

# 澳门格兰披治大赛车（11月第三周四天）
_add_range("2026-11-19", "2026-11-22", "grand_prix",      1.50)
_add_range("2027-11-18", "2027-11-21", "grand_prix",      1.50)

# 澳门国际烟花汇演（7-9月周末，约10场）
for _sat in [
    "2026-07-25","2026-08-01","2026-08-08","2026-08-15",
    "2026-08-22","2026-08-29","2026-09-05","2026-09-12",
]:
    _SPECIAL_DATES[_sat] = ("fireworks", 1.25)

# 澳门公众假期（本地休假，周末效应叠加）
_MAC_HOLIDAYS_2026 = [
    "2026-01-01",  # 元旦
    "2026-04-02","2026-04-03","2026-04-04","2026-04-05","2026-04-06",  # 复活节+清明
    "2026-05-01",  # 劳动节
    "2026-05-25",  # 佛诞（四月初八）
    "2026-06-19",  # 端午节
    "2026-09-25","2026-09-26",  # 中秋
    "2026-10-01","2026-10-02",  # 国庆
    "2026-11-02",  # 追思节
    "2026-12-08",  # 圣母无染原罪节
    "2026-12-20",  # 澳门回归纪念日
    "2026-12-24","2026-12-25",  # 圣诞
]
for _hd in _MAC_HOLIDAYS_2026:
    if _hd not in _SPECIAL_DATES:
        _SPECIAL_DATES[_hd] = ("public_holiday", 1.20)


# ══════════════════════════════════════════════════════════════════════════
#  § 2  酒店个体溢价因子（within-tier premium）
#       基准=1.0（等于该星级DSEC市场均价）
#       来源：品牌溢价研究 + 澳门市场定价观察
# ══════════════════════════════════════════════════════════════════════════

# 5_deluxe — DSEC "5★" ADR × 1.35（大型综合度假村相对普通5★均高30-40%）× 个体因子
_DELUXE_BASE_MULT = 1.35   # 相对DSEC 5★均值的档次溢价

_HOTEL_PREMIUM: dict[str, float] = {
    # ── 五星豪华（12家）──────────────────────────────────────────────────
    "MAC_5DX_WYNN_001": 1.22,   # 永利澳门 — 澳门半岛标志性奢华
    "MAC_5DX_WYNN_002": 1.28,   # 永利皇宫 — 路氹最高端之一
    "MAC_5DX_NUWA_003": 1.05,   # 颐居（城市梦想）
    "MAC_5DX_NOLM_004": 0.82,   # 新东方置地（较老，半岛非核心位置）
    "MAC_5DX_GRAN_005": 0.88,   # 新葡京（SJM旗舰，半岛历史名酒店）
    "MAC_5DX_MGMM_006": 0.96,   # 澳门美高梅
    "MAC_5DX_T13_007":  2.60,   # 十三皇宫（路环，超豪华，仅199间）
    "MAC_5DX_FOUR_008": 1.32,   # 四季
    "MAC_5DX_GLPA_009": 1.18,   # 上葡京（全新，路氹）
    "MAC_5DX_MGMC_010": 1.12,   # 美狮美高梅（路氹）
    "MAC_5DX_ALTI_011": 0.75,   # 新濠锋（较老，氹仔北）
    "MAC_5DX_LGPA_012": 0.85,   # 励宫（澳门半岛）

    # ── 五星（28家）─────────────────────────────────────────────────────
    "MAC_5ST_VENE_013": 1.15,   # 威尼斯人（旗舰）
    "MAC_5ST_GALX_014": 0.88,   # 银河（volume）
    "MAC_5ST_CONA_015": 1.08,   # 康莱德
    "MAC_5ST_MORP_016": 1.42,   # 摩珀斯（标志性设计，Zaha Hadid）
    "MAC_5ST_STMR_017": 1.32,   # 瑞吉（Marriott高端）
    "MAC_5ST_RITZ_018": 1.38,   # 丽思卡尔顿（Marriott顶级）
    "MAC_5ST_JWMR_019": 1.10,   # JW万豪
    "MAC_5ST_OKUR_020": 1.02,   # 大仓（日系精品）
    "MAC_5ST_BANY_021": 1.12,   # 悦榕庄
    "MAC_5ST_HYAT_022": 1.10,   # 君悦
    "MAC_5ST_ANDA_023": 1.06,   # 安达仕
    "MAC_5ST_LGRD_024": 1.08,   # 伦敦人名汇
    "MAC_5ST_STAR_025": 0.74,   # 星际（SJM，半岛）
    "MAC_5ST_LISB_026": 0.68,   # 葡京（历史名酒店，较老）
    "MAC_5ST_ROYL_027": 0.62,   # 皇都（半岛，预算5★）
    "MAC_5ST_SOFT_028": 1.00,   # 索菲特（十六浦，半岛江边）
    "MAC_5ST_MAND_029": 1.22,   # 文华东方（半岛精品）
    "MAC_5ST_ARTZ_030": 0.80,   # 雅辰（Grand Lapa，花园式）
    "MAC_5ST_LARC_031": 0.75,   # 凯旋门（半岛）
    "MAC_5ST_BROD_032": 0.80,   # 百老汇（路氹，预算）
    "MAC_5ST_GCOL_033": 0.68,   # 鹭环海天（路环，度假型，房价含餐）
    "MAC_5ST_RIVI_034": 0.85,   # 濠璟
    "MAC_5ST_GLIS_035": 0.85,   # 葡京人之家（待确认）
    "MAC_5ST_VILL_036": 0.80,
    "MAC_5ST_HGNG_037": 0.78,
    "MAC_5ST_SOHO_038": 0.78,
    "MAC_5ST_ROYA_039": 0.72,
    "MAC_5ST_PARK_040": 0.78,

    # ── 四星（18家）─────────────────────────────────────────────────────
    "MAC_4ST_STCT_041": 1.22,   # 新濠影汇（路氹，品牌溢价）
    "MAC_4ST_LGND_042": 1.18,   # 澳门伦敦人（路氹）
    "MAC_4ST_LSBM_043": 1.08,   # 葡京人（半岛，新开业）
    "MAC_4ST_RIOH_044": 0.88,   # 利澳（氹仔，大众市场）
    "MAC_4ST_GOLD_045": 0.82,   # 金龙（半岛，性价比）
    "MAC_4ST_CASA_046": 0.84,   # 皇家金堡
    "MAC_4ST_METR_047": 0.86,   # 维景
    "MAC_4ST_BEVP_048": 0.84,   # 富豪
    "MAC_4ST_HRBV_049": 0.88,   # 励庭海景（半岛海景）
    "MAC_4ST_ASCT_050": 0.92,   # 雅诗阁（服务公寓）
    "MAC_4ST_GRVW_051": 0.90,   # 君怡（氹仔）
    "MAC_4ST_GRDR_052": 0.85,   # 骏龙
    "MAC_4ST_HOLI_053": 1.05,   # 假日（路氹，品牌）
    "MAC_4ST_PRES_054": 0.82,   # 总统
    "MAC_4ST_PCOL_055": 0.90,   # 竹湾（路环，特色度假）
    "MAC_4ST_GOCR_056": 0.80,   # 金皇冠
    "MAC_4ST_PMIN_057": 0.88,   # 皇庭海景
    "MAC_4ST_REMH_058": 0.85,   # 幻宇

    # ── 三星（18家）─────────────────────────────────────────────────────
    "MAC_3ST_EMPE_059": 0.90,   # 帝濠（半岛）
    "MAC_3ST_FORT_060": 0.95,   # 财神（半岛，大众）
    "MAC_3ST_SLND_061": 0.88,
    "MAC_3ST_MAND_062": 0.90,
    "MAC_3ST_WINN_063": 0.92,
    "MAC_3ST_GDEN_064": 0.88,
    "MAC_3ST_SHAN_065": 0.86,
    "MAC_3ST_ASIA_066": 0.88,
    "MAC_3ST_CENT_067": 0.90,
    "MAC_3ST_HOWR_068": 0.85,
    "MAC_3ST_CHIN_069": 0.88,
    "MAC_3ST_MTPL_070": 0.92,
    "MAC_3ST_BFUL_071": 0.90,
    "MAC_3ST_LUCK_072": 0.86,
    "MAC_3ST_BONS_073": 0.85,
    "MAC_3ST_PATA_074": 0.88,
    "MAC_3ST_MACR_075": 0.90,
    "MAC_3ST_HPKK_076": 0.88,
}

# 缺失酒店的默认因子（按星级）
_TIER_DEFAULT_PREMIUM = {
    "5_deluxe": 1.00,
    "5_star":   0.90,
    "4_star":   0.88,
    "3_star":   0.90,
}


# ══════════════════════════════════════════════════════════════════════════
#  § 3  日期需求场景判断
# ══════════════════════════════════════════════════════════════════════════

def get_date_scenario(d: date) -> tuple[str, float]:
    """
    返回 (场景标签, 价格乘数)。
    优先级：特殊节日 > 周末 > 正常工作日
    """
    key = d.strftime("%Y-%m-%d")
    if key in _SPECIAL_DATES:
        return _SPECIAL_DATES[key]

    # 月份季节性（DSEC season_mult已内置到月份ADR，这里只叠加周末效应）
    weekday = d.weekday()   # 0=周一, 5=周六, 6=周日
    if weekday >= 4:        # 周五/六/日
        return ("weekend", 1.15)
    if weekday == 3:        # 周四（周末前夜）
        return ("pre_weekend", 1.05)

    return ("weekday", 1.00)


# ══════════════════════════════════════════════════════════════════════════
#  § 4  单条模拟价格生成
# ══════════════════════════════════════════════════════════════════════════

def simulate_bar_price(
    hotel: dict,
    checkin: date,
    conn: sqlite3.Connection,
    noise_pct: float = 0.08,
) -> tuple[float, float, str]:
    """
    返回 (simulated_bar_mop, simulated_occupancy, scenario_label)

    计算公式：
      tier_base = DSEC市场均值ADR(月份, 星级)
      deluxe_adj = _DELUXE_BASE_MULT（仅5_deluxe，对DSEC 5★均值叠加档次溢价）
      hotel_premium = 酒店个体溢价因子
      scenario_mult = 日期场景乘数
      noise = Uniform(-noise_pct, +noise_pct)

      bar = tier_base × deluxe_adj × hotel_premium × scenario_mult × (1 + noise)
    """
    tier = hotel.get("tier", "")
    star = hotel.get("star", 4)
    hotel_id = hotel.get("id", "")

    # 1. DSEC市场均值（当月当星级）
    month = checkin.month
    # 对DSEC数据，5_deluxe和5_star都映射到star=5
    dsec_star = min(star, 5)
    base_adr = get_market_adr(month, dsec_star, conn) or _FALLBACK_ADR(tier, month)
    base_occ = get_market_occupancy(month, dsec_star, conn) or _FALLBACK_OCC(tier)

    # 2. 档次调整（5_deluxe在DSEC"5★"均值基础上上浮）
    if tier == "5_deluxe":
        base_adr *= _DELUXE_BASE_MULT
    elif tier == "3_star":
        # DSEC 3★ ADR已直接反映，无需调整
        pass

    # 3. 酒店个体溢价
    premium = _HOTEL_PREMIUM.get(hotel_id, _TIER_DEFAULT_PREMIUM.get(tier, 1.0))

    # 4. 日期场景
    scenario_label, scenario_mult = get_date_scenario(checkin)

    # 5. 随机扰动（体现真实市场价格波动）
    noise = random.uniform(-noise_pct, +noise_pct)

    # 6. 最终价格
    final_bar = base_adr * premium * scenario_mult * (1.0 + noise)
    final_bar = round(final_bar / 10) * 10   # 取整到10 MOP（符合实际定价习惯）

    # 7. 入住率（scenario对入住率影响较小）
    occ_noise = random.uniform(-0.04, +0.04)
    final_occ = min(0.99, max(0.20, base_occ * scenario_mult ** 0.4 * (1.0 + occ_noise)))
    final_occ = round(final_occ, 3)

    return final_bar, final_occ, scenario_label


def _FALLBACK_ADR(tier: str, month: int) -> float:
    """DSEC数据缺失时的硬编码后备均值（后疫情正常化水平 2023-2025 均值）"""
    base = {
        "5_deluxe": 1980, "5_star": 1120, "4_star": 850, "3_star": 440
    }.get(tier, 800)
    # 简单季节乘数
    seasonal = {
        1:1.45, 2:1.10, 3:0.90, 4:0.95, 5:1.15, 6:0.95,
        7:1.10, 8:1.20, 9:0.95, 10:1.40, 11:0.88, 12:1.05
    }.get(month, 1.0)
    return base * seasonal

def _FALLBACK_OCC(tier: str) -> float:
    return {"5_deluxe": 0.90, "5_star": 0.82, "4_star": 0.74, "3_star": 0.66}.get(tier, 0.75)


# ══════════════════════════════════════════════════════════════════════════
#  § 5  批量写入 price_snapshots
# ══════════════════════════════════════════════════════════════════════════

NOTES_TAG = "dsec_sim_v1"   # 用于标识模拟数据，便于与真实数据区分

def populate(
    hotels: list[dict],
    days_ahead: int = 60,
    protect_real: bool = True,
    rebuild: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, int]:
    """
    为指定酒店列表生成模拟 price_snapshots。

    参数：
      days_ahead   : 生成未来多少天的入住价格
      protect_real : True = 跳过已有 source_ok=1 且非模拟数据的记录（保护真实数据）
      rebuild      : True = 先删除该酒店的所有模拟数据再重建

    返回：{"inserted": N, "skipped": N, "hotels": N}
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")

    # 确保 DSEC 数据已初始化
    init_and_seed(conn)

    today = date.today()
    snap_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checkin_dates = [today + timedelta(days=d) for d in range(1, days_ahead + 1)]

    stats = {"inserted": 0, "skipped": 0, "hotels": len(hotels)}

    for hotel in hotels:
        hotel_id   = hotel["id"]
        hotel_name = hotel.get("cn", "")
        tier       = hotel.get("tier", "")
        star       = hotel.get("star", 4)
        area       = hotel.get("area", "")
        rooms      = hotel.get("rooms", 0)

        if rebuild:
            conn.execute(
                "DELETE FROM price_snapshots WHERE hotel_id=? AND notes LIKE ?",
                (hotel_id, f"{NOTES_TAG}%")
            )

        for checkin in checkin_dates:
            checkin_str  = checkin.strftime("%Y-%m-%d")
            checkout_str = (checkin + timedelta(days=1)).strftime("%Y-%m-%d")

            # 如果已有真实数据，跳过（protect_real）
            if protect_real:
                exists = conn.execute("""
                    SELECT id FROM price_snapshots
                    WHERE hotel_id=? AND checkin_date=?
                      AND source_ok=1
                      AND (notes IS NULL OR notes NOT LIKE ?)
                """, (hotel_id, checkin_str, f"{NOTES_TAG}%")).fetchone()
                if exists:
                    stats["skipped"] += 1
                    continue

            # 检查是否已有模拟数据（防止重复）
            if not rebuild:
                dup = conn.execute("""
                    SELECT id FROM price_snapshots
                    WHERE hotel_id=? AND checkin_date=? AND notes LIKE ?
                """, (hotel_id, checkin_str, f"{NOTES_TAG}%")).fetchone()
                if dup:
                    stats["skipped"] += 1
                    continue

            # 生成模拟价格
            bar, occ, scenario = simulate_bar_price(hotel, checkin, conn)

            conn.execute("""
                INSERT INTO price_snapshots
                    (hotel_id, hotel_name_cn, star, tier, area, total_rooms,
                     snapshot_time, checkin_date,
                     official_bar, official_rack, member_rate,
                     currency, room_type, avail_status,
                     low_stock_flag, booking_rate, agoda_rate,
                     source_ok, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                hotel_id, hotel_name, star, tier, area, rooms,
                snap_time, checkin_str,
                bar,                          # official_bar
                round(bar * 1.12),            # official_rack (官网挂牌价，约高12%)
                round(bar * 0.95),            # member_rate (会员价，约低5%)
                "MOP", "标准房",
                "low" if occ > 0.88 else "available",
                1 if occ > 0.90 else 0,
                round(bar * 1.05),            # booking_rate (OTA约高5%)
                round(bar * 1.03),            # agoda_rate
                1,                            # source_ok = 1 (模拟数据可信)
                f"{NOTES_TAG}|{scenario}",
            ))
            stats["inserted"] += 1

        conn.commit()

    if _owns:
        conn.close()

    return stats


# ══════════════════════════════════════════════════════════════════════════
#  § 6  入口
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [DSEC_SIM] %(message)s",
        handlers=[logging.StreamHandler()],
    )
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="InsightBridge 模拟市场价格生成器")
    parser.add_argument("--days",    type=int, default=60,  help="生成未来N天的价格（默认60）")
    parser.add_argument("--tier",    type=str, default="",  help="只生成指定星级（4_star/3_star/...）")
    parser.add_argument("--rebuild", action="store_true",   help="清除旧模拟数据后重建")
    parser.add_argument("--no-protect", action="store_true",help="不保护真实抓取数据（覆盖）")
    args = parser.parse_args()

    hotels = HOTELS_76
    if args.tier:
        hotels = [h for h in hotels if h["tier"] == args.tier]
        log.info(f"⭐ 只处理 {args.tier}：{len(hotels)} 家")

    log.info(f"🚀 开始生成模拟价格 | {len(hotels)} 家酒店 × {args.days} 天 | rebuild={args.rebuild}")

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    result = populate(
        hotels       = hotels,
        days_ahead   = args.days,
        protect_real = not args.no_protect,
        rebuild      = args.rebuild,
        conn         = conn,
    )

    conn.close()

    log.info(
        f"✅ 完成 | 写入 {result['inserted']} 条 | 跳过 {result['skipped']} 条 "
        f"| 覆盖 {result['hotels']} 家酒店"
    )

    # 验证汇总
    verify_conn = sqlite3.connect(str(DB_PATH), timeout=10)
    rows = verify_conn.execute("""
        SELECT tier, COUNT(*) as n,
               AVG(CASE WHEN notes LIKE 'dsec_sim%' THEN official_bar END) as sim_avg,
               AVG(CASE WHEN notes NOT LIKE 'dsec_sim%' AND source_ok=1 THEN official_bar END) as real_avg
        FROM price_snapshots
        WHERE snapshot_time >= datetime('now','-2 hours')
        GROUP BY tier ORDER BY tier DESC
    """).fetchall()
    verify_conn.close()

    log.info("📊 生成验证（按星级）：")
    for r in rows:
        tier, n, sim_avg, real_avg = r
        log.info(
            f"  {tier}: {n} 条 | 模拟均价 MOP {sim_avg:.0f}" +
            (f" | 真实均价 MOP {real_avg:.0f}" if real_avg else "")
        )
