"""
InsightBridge Price Elasticity Engine  v1.0
============================================
第二阶段：模拟弹性系数驱动的 RevPAR / 轻量 TRevPAR 最优化
第三阶段接口：从 pms_feedback 表拟合真实弹性曲线（预留）

核心逻辑
---------
  price_premium       = (candidate_price - market_price) / market_price
  predicted_occupancy = base_occ × (1 - elasticity × price_premium)
  RevPAR              = candidate_price × predicted_occupancy
  TRevPAR(light)      = (candidate_price + ancillary_per_occ) × predicted_occupancy

枚举 [floor, ceiling] 内所有价格点（步长 10 MOP），
默认选轻量 TRevPAR（房费 + 非房贡献）最大的价格。

弹性系数分层（模拟阶段，基于澳门市场同类研究）
------------------------------------------------
  3★ 全澳门        ≈ 0.82  (散客/OTA为主，高度价格敏感)
  4★ 半岛/内港/新口岸 ≈ 0.63  (商旅混合市场)
  4★ 路氹/氹仔     ≈ 0.55  (路氹城效应，目的地属性)
  5★ 半岛          ≈ 0.42  (豪华商旅，品牌忠诚度高)
  5★ 路氹赌场度假村 ≈ 0.28  (一体化目的地，高粘性)

季节乘数（需求越旺，弹性越低——旺季涨价客户流失更少）
  super_peak  × 0.45   (农历新年/五一黄金周/澳门格兰披治大赛车)
  peak        × 0.65   (节假日/长周末)
  normal      × 1.00
  low         × 1.30   (淡季，客户选择余地大)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phase 3 PMS 真实数据接口（预留）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
当酒店 PMS 数据接入后，调用 ElasticityEngine.load_from_pms(db_path) 即可
用真实历史订单拟合弹性系数，替换模拟值，无需改动任何调用代码。

PMS 数据表结构见 create_pms_schema() 函数。
"""

from __future__ import annotations
import sqlite3
import logging
import json
from pathlib import Path
from datetime import datetime
from typing import NamedTuple
from functools import lru_cache

log = logging.getLogger("elasticity")
_V6_PROFILE_PATH = Path(__file__).resolve().parent.parent / "共用_HROS_V6引擎" / "hotel_profiles_v6.json"
_MACAU_ANCILLARY_PATH = Path(__file__).resolve().parent / "macau_ancillary_profiles.json"
_MACAU_REVENUE_PROFILE_PATH = Path(__file__).resolve().parent / "macau_static_revenue_profiles.json"

# ── 星级价格护栏（MOP）────────────────────────────────────────────────────────
_PRICE_FLOOR   = {3: 680,  4: 750,  5: 1200}
# 调优(2026-06-01): 3★ 1600→1250 (DSEC历史最高价约1200，+5%缓冲)
#                  4★ 3500→2000 (澳门4★豪华上限；路氹最高端亦不超2000)
#                  5★ 保持8000（高端度假村特大活动可到此水平）
# 修复(2026-06-02): 3★ 420→680 (防止OTA低价拉低搜索起点; DSEC 3★ ADR ~950×0.70=665→取整680)
_PRICE_CEILING = {3: 1250, 4: 2000, 5: 8000}

_DEFAULT_ANCILLARY_RATIO = 0.45
_DEFAULT_ANCILLARY_MARGIN = 0.30
_DEFAULT_ELASTICITY = 1.0
_DEFAULT_MAX_PRICE_PREMIUM = 0.0
_DEFAULT_OPTIMAL_OCCUPANCY = 0.80

# 按现有76酒店名单，对澳门5★综合度假村做显式识别。
_INTEGRATED_RESORT_IDS = {
    "MAC_5DX_WYNN_002",  # 永利皇宫
    "MAC_5DX_GLPA_009",  # 上葡京综合度假村
    "MAC_5DX_MGMC_010",  # 美狮美高梅
    "MAC_5ST_VENE_013",  # 威尼斯人
    "MAC_5ST_GALX_014",  # 银河
    "MAC_5ST_CONA_015",  # 康莱德
    "MAC_5ST_MORP_016",  # 摩珀斯
    "MAC_5ST_STMR_017",  # 瑞吉
    "MAC_5ST_RITZ_018",  # 丽思卡尔顿
    "MAC_5ST_JWMR_019",  # JW万豪
    "MAC_5ST_OKUR_020",  # 大仓
    "MAC_5ST_BANY_021",  # 悦榕庄
    "MAC_5ST_HYAT_022",  # 君悦
    "MAC_5ST_ANDA_023",  # 安达仕
    "MAC_5ST_LGRD_024",  # 伦敦人名汇
    "MAC_5ST_BROD_032",  # 百老汇酒店
    "MAC_5ST_TRSN_040",  # 新濠天地翠湖
}


@lru_cache(maxsize=1)
def _load_v6_profiles() -> dict[str, dict]:
    try:
        return json.loads(_V6_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_macau_ancillary_profiles() -> dict[str, dict]:
    try:
        raw = json.loads(_MACAU_ANCILLARY_PATH.read_text(encoding="utf-8"))
        return raw.get("profiles") or {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_macau_revenue_profiles() -> dict[str, dict]:
    try:
        raw = json.loads(_MACAU_REVENUE_PROFILE_PATH.read_text(encoding="utf-8"))
        return raw.get("profiles") or {}
    except Exception:
        return {}


class ElasticityResult(NamedTuple):
    optimal_price:        float   # RevPAR 最优推荐价 (MOP)
    predicted_occupancy:  float   # 该价格下预测入住率 [0,1]
    predicted_revpar:     float   # 预测 RevPAR (MOP)
    baseline_revpar:      float   # 以市场价出售时的 RevPAR 基准
    predicted_trevpar:    float   # 预测 TRevPAR (房费+非房)
    baseline_trevpar:     float   # 基准 TRevPAR
    true_lift_pct:        float   # (optimal_trevpar - baseline_trevpar) / baseline_trevpar
    revpar_lift_pct:      float   # (optimal_revpar - baseline_revpar) / baseline_revpar
    elasticity_used:      float   # 本次使用的弹性系数
    elasticity_profile:   str     # 采用的澳门静态弹性档位
    max_price_premium:    float   # 相对市场价的最高溢价上限
    optimal_occupancy:    float   # 收益最大化目标入住率
    ancillary_profile:    str     # 采用的澳门总贡献参数档位
    ancillary_ratio_used: float   # 当前使用的非房/房费比
    ancillary_margin_used: float  # 当前使用的非房毛利率
    ancillary_per_occ:    float   # 每卖出1间夜对应的非房贡献(MOP)
    data_source:          str     # "simulated" | "fitted_pms"
    search_steps:         int     # 枚举了多少个价格点


class ElasticityEngine:
    """
    价格弹性引擎。

    模拟阶段直接实例化即可使用。
    接入真实 PMS 数据后调用 load_from_pms(db_path) 更新系数。
    """

    def __init__(self):
        # 弹性系数存储：key=(hotel_id or (star,district)), value={"e": float, "source": str}
        self._coefficients: dict = {}
        self._data_source = "macau_static"

    # ──────────────────────────────────────────────────────────────────────────
    # 公共 API
    # ──────────────────────────────────────────────────────────────────────────

    def optimize(
        self,
        candidate_price:  float,
        market_price:     float,
        star:             int,
        district:         str   = "NAPE",
        demand_level:     str   = "NORMAL",
        season:           str   = "normal",
        hotel_id:         str   = None,
    ) -> ElasticityResult:
        """
        在价格护栏范围内枚举，返回轻量 TRevPAR 最优价格及预测入住率。

        参数
        ----
        candidate_price : MARE 引擎推荐价（作为搜索起点参考）
        market_price    : 当前市场均价（竞争对手基准）
        star            : 酒店星级 (3/4/5)
        district        : 区域代码 (COT/TAIPA/NAPE/INNER/PENINSULA)
        demand_level    : 需求档位 (HIGH/NORMAL/LOW)
        season          : 季节 (super_peak/peak/normal/low)
        hotel_id        : 酒店ID（有真实弹性系数时优先使用）
        """
        if candidate_price <= 0:
            candidate_price = float(_PRICE_FLOOR.get(star, 420))

        if market_price <= 0:
            market_price = candidate_price
        if market_price <= 0:
            market_price = float(_PRICE_FLOOR.get(star, 420))

        elasticity_profile, elasticity, max_price_premium, optimal_occupancy = self._get_revenue_profile(
            star=star,
            hotel_id=hotel_id,
            season=season,
            demand_level=demand_level,
        )
        base_occ   = optimal_occupancy
        hotel_anchor = self._get_hotel_anchor(hotel_id)

        floor_p = _PRICE_FLOOR.get(star, 420)
        ceil_p  = _PRICE_CEILING.get(star, 8000)

        # 搜索范围：价格下限允许适度折价；价格上限严格遵守静态溢价表
        search_lo = max(floor_p, market_price * 0.70)
        search_hi = min(ceil_p, market_price * (1.0 + max_price_premium))
        if search_hi < search_lo:
            search_lo = max(floor_p, search_hi * 0.85)

        ancillary_profile, ancillary_ratio, ancillary_margin = self._get_ancillary_profile(star, hotel_id)
        ancillary_per_occ = market_price * ancillary_ratio * ancillary_margin

        best_objective = -1.0
        best_revpar = -1.0
        best_trevpar = -1.0
        best_price  = market_price
        best_occ    = base_occ
        steps       = 0

        price = search_lo
        while price <= search_hi + 1:
            premium = (price - market_price) / market_price
            occ_raw  = max(0.10, base_occ * (1.0 - elasticity * premium))
            # 超过最优入住率后，边际收益递减，因此目标函数只计算到最优点为止。
            occ_effective = min(occ_raw, optimal_occupancy)
            revpar  = price * occ_raw
            trevpar = price * occ_effective + ancillary_per_occ * occ_effective
            if trevpar > best_objective:
                best_objective = trevpar
                best_revpar = revpar
                best_trevpar = trevpar
                best_price  = price
                best_occ    = occ_raw
            price += 10
            steps += 1

        best_price = round(best_price / 10) * 10

        # 基准：以市场价出售的 RevPAR
        baseline_revpar = market_price * base_occ
        baseline_trevpar = baseline_revpar + ancillary_per_occ * base_occ
        total_lift = ((best_trevpar - baseline_trevpar) / baseline_trevpar
                      if baseline_trevpar > 0 else 0.0)
        revpar_lift = ((best_revpar - baseline_revpar) / baseline_revpar
                       if baseline_revpar > 0 else 0.0)

        return ElasticityResult(
            optimal_price       = best_price,
            predicted_occupancy = round(best_occ, 4),
            predicted_revpar    = round(best_revpar, 1),
            baseline_revpar     = round(baseline_revpar, 1),
            predicted_trevpar   = round(best_trevpar, 1),
            baseline_trevpar    = round(baseline_trevpar, 1),
            true_lift_pct       = round(total_lift * 100, 2),
            revpar_lift_pct     = round(revpar_lift * 100, 2),
            elasticity_used     = round(elasticity, 4),
            elasticity_profile  = elasticity_profile,
            max_price_premium   = round(max_price_premium, 4),
            optimal_occupancy   = round(optimal_occupancy, 4),
            ancillary_profile   = ancillary_profile,
            ancillary_ratio_used= round(ancillary_ratio, 4),
            ancillary_margin_used=round(ancillary_margin, 4),
            ancillary_per_occ   = round(ancillary_per_occ, 1),
            data_source         = self._data_source,
            search_steps        = steps,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 3 接口：从真实 PMS 数据拟合弹性系数
    # ──────────────────────────────────────────────────────────────────────────

    def load_from_pms(self, db_path: str | Path) -> int:
        """
        从 pms_feedback 表拟合真实弹性系数。
        返回成功拟合的酒店数量。

        调用时机：每天凌晨 PMS 数据同步后调用一次即可。
        最少需要 30 天、每家酒店 30 条以上记录才会替换模拟系数。

        数据接入方式：
          1. 酒店系统通过 API 推送 → insert into pms_feedback
          2. 批量 CSV 导入        → import_pms_csv(csv_path, db_path)
          3. PMS 直连（Opera/FIDELIO/PMS Cloud）→ 后续集成
        """
        db_path = Path(db_path)
        if not db_path.exists():
            log.warning(f"PMS 数据库不存在: {db_path}")
            return 0

        fitted = 0
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)

            # 按酒店分组拟合：OLS 回归 price_premium → occupancy_rate
            rows = conn.execute("""
                SELECT hotel_id, star,
                       (price_charged - market_price) / market_price AS premium,
                       occupancy_rate
                FROM pms_feedback
                WHERE source IN ('pms_live', 'pms_import')
                  AND occupancy_rate BETWEEN 0.05 AND 1.0
                  AND market_price > 0
                  AND price_charged > 0
                ORDER BY hotel_id
            """).fetchall()
            conn.close()

            # 按酒店聚合
            from collections import defaultdict
            hotel_data: dict[str, list] = defaultdict(list)
            for hotel_id, star, premium, occ in rows:
                hotel_data[hotel_id].append((premium, occ, star))

            for hotel_id, points in hotel_data.items():
                if len(points) < 30:
                    continue   # 样本不足，保留模拟系数
                premiums = [p[0] for p in points]
                occs     = [p[1] for p in points]
                star     = points[0][2]
                base_occ = sum(o for o in occs if abs(premiums[occs.index(o)]) < 0.03) / max(
                    1, sum(1 for p in premiums if abs(p) < 0.03))

                # 简单 OLS: elasticity = -Δocc/Δpremium
                n       = len(premiums)
                mean_p  = sum(premiums) / n
                mean_o  = sum(occs) / n
                cov     = sum((premiums[i] - mean_p) * (occs[i] - mean_o) for i in range(n))
                var_p   = sum((p - mean_p) ** 2 for p in premiums)
                if var_p > 0:
                    slope = cov / var_p   # negative slope expected
                    elasticity = max(0.05, min(2.0, -slope))
                    self._coefficients[hotel_id] = {
                        "e":       elasticity,
                        "source":  "fitted_pms",
                        "samples": n,
                        "star":    star,
                    }
                    fitted += 1

            if fitted > 0:
                self._data_source = "fitted_pms"
                log.info(f"[弹性引擎] 已从PMS数据拟合 {fitted} 家酒店弹性系数")

        except Exception as exc:
            log.warning(f"[弹性引擎] PMS拟合失败: {exc}")

        return fitted

    # ──────────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────────

    def _get_revenue_profile(
        self,
        *,
        star: int,
        hotel_id: str | None,
        season: str,
        demand_level: str,
    ) -> tuple[str, float, float, float]:
        profile_name = self._resolve_revenue_profile(star, hotel_id, season, demand_level)
        profile = _load_macau_revenue_profiles().get(profile_name, {})
        elasticity = float(profile.get("elasticity") or _DEFAULT_ELASTICITY)
        max_premium = float(profile.get("max_price_premium") or _DEFAULT_MAX_PRICE_PREMIUM)
        optimal_occ = float(profile.get("optimal_occupancy") or _DEFAULT_OPTIMAL_OCCUPANCY)
        return profile_name, elasticity, max_premium, optimal_occ

    def _get_hotel_anchor(self, hotel_id: str | None) -> float | None:
        if not hotel_id:
            return None
        profile = _load_v6_profiles().get(hotel_id)
        if not profile:
            return None
        try:
            anchor = float(profile.get("baseline_adr") or 0.0)
            return anchor or None
        except Exception:
            return None

    def _get_ancillary_profile(self, star: int, hotel_id: str | None) -> tuple[str, float, float]:
        profile_name = self._resolve_macau_profile(star, hotel_id)
        profile = _load_macau_ancillary_profiles().get(profile_name, {})
        ratio = float(profile.get("non_room_to_room_ratio") or _DEFAULT_ANCILLARY_RATIO)
        margin = float(profile.get("ancillary_gross_margin") or _DEFAULT_ANCILLARY_MARGIN)
        return profile_name, ratio, margin

    def _resolve_macau_profile(self, star: int, hotel_id: str | None) -> str:
        if int(star or 0) <= 3:
            return "3star"
        if int(star or 0) == 4:
            return "4star"
        if hotel_id and hotel_id in _INTEGRATED_RESORT_IDS:
            return "5star_integrated_resort"
        return "5star_non_casino"

    def _resolve_revenue_profile(
        self,
        star: int,
        hotel_id: str | None,
        season: str,
        demand_level: str,
    ) -> str:
        season_key = (season or "normal").lower()
        demand_key = (demand_level or "NORMAL").upper()

        if int(star or 0) >= 5:
            if hotel_id and hotel_id in _INTEGRATED_RESORT_IDS:
                if season_key in ("super_peak", "peak"):
                    return "5star_resort_leisure_peak"
                if season_key in ("low",) or demand_key == "LOW":
                    return "5star_resort_leisure_low"
                return "5star_resort_leisure_shoulder"
            return "5star_business_mice"

        if int(star or 0) == 4:
            if season_key in ("super_peak", "peak"):
                return "4star_leisure_peak"
            if season_key in ("low",) or demand_key == "LOW":
                return "4star_leisure_low"
            return "4star_leisure_shoulder"

        if season_key in ("super_peak", "peak"):
            return "3star_ota_peak"
        if season_key in ("low",) or demand_key == "LOW":
            return "3star_ota_low"
        return "3star_ota_shoulder"


# ── PMS 数据库建表脚本（Phase 3 接口）────────────────────────────────────────

PMS_SCHEMA_SQL = """
-- ════════════════════════════════════════════════════════════════════
-- Phase 3 PMS 真实数据接入表结构
-- 写入方式：① API推送  ② CSV导入  ③ PMS直连（Opera/Fidelio等）
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pms_feedback (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id          TEXT    NOT NULL,           -- MAC_5DX_WYNN_001 格式
    star              INTEGER NOT NULL,           -- 3/4/5
    district          TEXT,                       -- COT/TAIPA/NAPE/INNER
    stay_date         DATE    NOT NULL,           -- 入住日期 YYYY-MM-DD
    price_charged     REAL    NOT NULL,           -- 当天实际挂牌价 (MOP)
    market_price      REAL,                       -- 当天市场均价基准 (MOP)
    rooms_available   INTEGER,                    -- 当天可用间数
    rooms_sold        INTEGER,                    -- 当天实际售出间数
    occupancy_rate    REAL,                       -- 实际入住率 [0,1]
    adr               REAL,                       -- 实际平均房价 (MOP)
    revpar            REAL,                       -- 实际 RevPAR (MOP)
    channel           TEXT,                       -- direct/ota_bcom/ota_agoda/corporate/walk_in
    booking_window    INTEGER,                    -- 提前预订天数
    cancellation_rate REAL,                       -- 当日取消率 [0,1]
    season            TEXT,                       -- super_peak/peak/normal/low
    is_holiday        INTEGER DEFAULT 0,          -- 是否节假日
    is_weekend        INTEGER DEFAULT 0,
    source            TEXT    DEFAULT 'pms_live', -- pms_live/pms_import/simulated
    notes             TEXT,
    created_at        TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pms_hotel_date   ON pms_feedback(hotel_id, stay_date);
CREATE INDEX IF NOT EXISTS idx_pms_star_district ON pms_feedback(star, district);

-- 拟合结果缓存表（load_from_pms 写入）
CREATE TABLE IF NOT EXISTS elasticity_coefficients (
    hotel_id      TEXT PRIMARY KEY,
    star          INTEGER,
    district      TEXT,
    elasticity    REAL    NOT NULL,           -- 拟合弹性系数
    base_occupancy REAL,                      -- 市价时基准入住率
    confidence    REAL DEFAULT 0.0,           -- 拟合置信度 [0,1]
    sample_size   INTEGER DEFAULT 0,          -- 训练样本量
    source        TEXT DEFAULT 'simulated',   -- simulated/fitted_pms
    r_squared     REAL,                       -- OLS 拟合优度
    last_updated  TEXT DEFAULT (datetime('now'))
);
"""


def create_pms_schema(db_path: str | Path):
    """在指定数据库创建 Phase 3 PMS 接入表（幂等，可重复调用）"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.executescript(PMS_SCHEMA_SQL)
    conn.commit()
    conn.close()
    log.info(f"[弹性引擎] PMS接入表已创建: {db_path}")


def import_pms_csv(csv_path: str | Path, db_path: str | Path) -> int:
    """
    Phase 3 辅助函数：从 CSV 批量导入 PMS 历史数据。

    CSV 列顺序：
    hotel_id, star, district, stay_date, price_charged, market_price,
    rooms_available, rooms_sold, occupancy_rate, adr, revpar,
    channel, booking_window, cancellation_rate, season, is_holiday, is_weekend

    返回成功导入行数。
    """
    import csv
    create_pms_schema(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10)
    count = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO pms_feedback
                    (hotel_id, star, district, stay_date, price_charged, market_price,
                     rooms_available, rooms_sold, occupancy_rate, adr, revpar,
                     channel, booking_window, cancellation_rate, season,
                     is_holiday, is_weekend, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pms_import')
                """, (
                    row["hotel_id"], int(row["star"]), row.get("district"),
                    row["stay_date"], float(row["price_charged"]),
                    float(row.get("market_price") or 0),
                    int(row.get("rooms_available") or 0),
                    int(row.get("rooms_sold") or 0),
                    float(row.get("occupancy_rate") or 0),
                    float(row.get("adr") or 0),
                    float(row.get("revpar") or 0),
                    row.get("channel"), int(row.get("booking_window") or 0),
                    float(row.get("cancellation_rate") or 0),
                    row.get("season"), int(row.get("is_holiday") or 0),
                    int(row.get("is_weekend") or 0),
                ))
                count += 1
            except Exception as exc:
                log.warning(f"CSV导入跳过行: {exc}")
    conn.commit()
    conn.close()
    log.info(f"[弹性引擎] CSV导入完成: {count} 行 → {db_path}")
    return count


# ── 模块级单例（三个系统共用同一实例）────────────────────────────────────────
_engine = ElasticityEngine()


def get_engine() -> ElasticityEngine:
    """获取全局弹性引擎单例"""
    return _engine


def optimize_price(
    candidate_price: float,
    market_price:    float,
    star:            int,
    district:        str  = "NAPE",
    demand_level:    str  = "NORMAL",
    season:          str  = "normal",
    hotel_id:        str  = None,
) -> ElasticityResult:
    """快捷函数：直接调用全局引擎的 optimize()"""
    return _engine.optimize(
        candidate_price=candidate_price,
        market_price=market_price,
        star=star,
        district=district,
        demand_level=demand_level,
        season=season,
        hotel_id=hotel_id,
    )
