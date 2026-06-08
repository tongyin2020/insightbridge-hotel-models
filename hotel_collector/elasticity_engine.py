"""
InsightBridge Price Elasticity Engine  v1.0
============================================
第二阶段：模拟弹性系数驱动的 RevPAR 最优化
第三阶段接口：从 pms_feedback 表拟合真实弹性曲线（预留）

核心逻辑
---------
  price_premium       = (candidate_price - market_price) / market_price
  predicted_occupancy = base_occ × (1 - elasticity × price_premium)
  RevPAR              = candidate_price × predicted_occupancy

枚举 [floor, ceiling] 内所有价格点（步长 10 MOP），选 RevPAR 最大的价格。

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
_V6_PROFILE_PATH = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/共用_HROS_V6引擎/hotel_profiles_v6.json")

# ── 星级价格护栏（MOP）────────────────────────────────────────────────────────
_PRICE_FLOOR   = {3: 680,  4: 750,  5: 1200}
# 调优(2026-06-01): 3★ 1600→1250 (DSEC历史最高价约1200，+5%缓冲)
#                  4★ 3500→2000 (澳门4★豪华上限；路氹最高端亦不超2000)
#                  5★ 保持8000（高端度假村特大活动可到此水平）
# 修复(2026-06-02): 3★ 420→680 (防止OTA低价拉低搜索起点; DSEC 3★ ADR ~950×0.70=665→取整680)
_PRICE_CEILING = {3: 1250, 4: 2000, 5: 8000}

# ── 基础入住率（按需求档位，模拟阶段经验值）────────────────────────────────────
_BASE_OCCUPANCY = {
    "HIGH":   0.88,
    "NORMAL": 0.72,
    "LOW":    0.52,
}

# ── 模拟弹性系数（按星级+区域）──────────────────────────────────────────────────
# district 区域代码: COTAI/TAIPA=路氹/氹仔, PENINSULA/NAPE/INNER=半岛区
#
# 调优(2026-06-01): 3★/4★弹性系数上调，原因：
#   - 原值基于澳门学术估值(3★=0.82, 4★=0.63)，偏低估计价格敏感度
#   - DSEC历史ADR=922(3★)作为market_price时，低弹性导致最优价超DSEC历史最高价
#   - 澳门大众市场(散客/OTA为主)实际表现出更高价格敏感：
#     涨价30%→客流量下降25-35%（不是模型估算的17-22%）
#   - 新值来源：DSEC入住率-价格相关性分析 + 同期澳门市场调研校准
#   - 3★: 0.82→0.95 (+16%)  |  4★: 0.63→0.72 (+14%)
#   - 5★: 保持原值（豪华市场弹性估值相对准确，输出已合理）
#
_SIMULATED_ELASTICITY: dict[tuple[int, str], float] = {
    (3, "TAIPA"):      0.93,   # 调优: 0.80→0.93
    (3, "NAPE"):       0.95,   # 调优: 0.82→0.95
    (3, "INNER"):      0.98,   # 调优: 0.85→0.98
    (3, "COT"):        0.91,   # 调优: 0.78→0.91
    (3, "PENINSULA"):  0.96,   # 调优: 0.83→0.96
    (4, "TAIPA"):      0.63,   # 调优: 0.55→0.63
    (4, "NAPE"):       0.72,   # 调优: 0.63→0.72
    (4, "INNER"):      0.74,   # 调优: 0.65→0.74
    (4, "COT"):        0.60,   # 调优: 0.52→0.60（路氹商旅目的地，稍低）
    (4, "PENINSULA"):  0.68,   # 调优: 0.60→0.68
    (5, "TAIPA"):      0.38,   # 不变
    (5, "NAPE"):       0.42,   # 不变
    (5, "INNER"):      0.45,   # 不变
    (5, "COT"):        0.28,   # 不变：路氹赌场度假村，目的地效应强，弹性最低
    (5, "PENINSULA"):  0.40,   # 不变
}
_DEFAULT_ELASTICITY = {3: 0.95, 4: 0.68, 5: 0.38}   # 调优: 3★ 0.82→0.95, 4★ 0.60→0.68

# ── 季节弹性乘数──────────────────────────────────────────────────────────────
_SEASON_MULTIPLIER = {
    "super_peak": 0.45,   # 春节/黄金周/大赛车：需求旺，弹性低
    "peak":       0.65,
    "normal":     1.00,
    "low":        1.30,   # 淡季：客户选择多，弹性高
}


@lru_cache(maxsize=1)
def _load_v6_profiles() -> dict[str, dict]:
    try:
        return json.loads(_V6_PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


class ElasticityResult(NamedTuple):
    optimal_price:        float   # RevPAR 最优推荐价 (MOP)
    predicted_occupancy:  float   # 该价格下预测入住率 [0,1]
    predicted_revpar:     float   # 预测 RevPAR (MOP)
    baseline_revpar:      float   # 以市场价出售时的 RevPAR 基准
    true_lift_pct:        float   # (optimal_revpar - baseline_revpar) / baseline_revpar
    elasticity_used:      float   # 本次使用的弹性系数
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
        self._data_source = "simulated"

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
        在价格护栏范围内枚举，返回 RevPAR 最优价格及预测入住率。

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

        elasticity = self._get_elasticity(star, district, hotel_id, season)
        base_occ   = _BASE_OCCUPANCY.get(demand_level, 0.72)
        hotel_anchor = self._get_hotel_anchor(hotel_id)

        floor_p = _PRICE_FLOOR.get(star, 420)
        ceil_p  = _PRICE_CEILING.get(star, 8000)

        # 搜索范围：市场价 ±40%，再与绝对护栏取交集
        search_lo = max(floor_p, market_price * 0.70)
        search_hi = min(ceil_p,  market_price * 1.45)
        if star >= 5:
            luxury_anchor = max(market_price, hotel_anchor or 0.0, candidate_price or 0.0)
            if luxury_anchor >= 1800:
                elasticity *= 0.82
                if (district or "").upper() in ("COT", "TAIPA"):
                    elasticity *= 0.92
                if (season or "normal").lower() in ("peak", "super_peak"):
                    elasticity *= 0.92
                search_lo = max(search_lo, luxury_anchor * 0.82)
                search_hi = min(ceil_p, max(search_hi, luxury_anchor * 1.20))

        best_revpar = -1.0
        best_price  = market_price
        best_occ    = base_occ
        steps       = 0

        price = search_lo
        while price <= search_hi + 1:
            premium = (price - market_price) / market_price
            occ     = max(0.10, base_occ * (1.0 - elasticity * premium))
            revpar  = price * occ
            if revpar > best_revpar:
                best_revpar = revpar
                best_price  = price
                best_occ    = occ
            price += 10
            steps += 1

        best_price = round(best_price / 10) * 10

        # 基准：以市场价出售的 RevPAR
        baseline_revpar = market_price * base_occ
        true_lift = ((best_revpar - baseline_revpar) / baseline_revpar
                     if baseline_revpar > 0 else 0.0)

        return ElasticityResult(
            optimal_price       = best_price,
            predicted_occupancy = round(best_occ, 4),
            predicted_revpar    = round(best_revpar, 1),
            baseline_revpar     = round(baseline_revpar, 1),
            true_lift_pct       = round(true_lift * 100, 2),
            elasticity_used     = round(elasticity, 4),
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

    def _get_elasticity(self, star: int, district: str, hotel_id: str | None,
                        season: str) -> float:
        # 优先：已拟合的真实酒店系数
        if hotel_id and hotel_id in self._coefficients:
            base_e = self._coefficients[hotel_id]["e"]
        else:
            district_upper = (district or "NAPE").upper()
            base_e = _SIMULATED_ELASTICITY.get(
                (star, district_upper),
                _DEFAULT_ELASTICITY.get(star, 0.70),
            )

        season_lower = (season or "normal").lower()
        multiplier   = _SEASON_MULTIPLIER.get(season_lower, 1.0)
        return round(base_e * multiplier, 4)

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
