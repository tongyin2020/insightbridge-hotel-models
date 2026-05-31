"""
InsightBridge — 声誉情感引擎
sentiment_engine.py
================================================
基于"声誉动态定价"（Reputation-Based Dynamic Pricing）与
"意图触发营销"（Intent-Triggered Marketing）框架

核心输出：
  R_t    — 实时声誉指数 [-1, 1]
  M_t    — 声誉动量（变化速度）
  ΔR_t   — 与同档竞对均值的声誉差
  γ_eff  — 置信压缩后的定价影响系数
  ε      — 价格→情感弹性（需30天+数据后校准）

调用方：
  compute_dynamic_base_price() in run_21d_harness.py  → 接收 reputation_adj
  AcquisitionMDP in acquisition_mdp.py                → 接收 trigger signals
"""

from __future__ import annotations
import math
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("SENTIMENT")

# ── 数据库路径
DB_PATH = Path("/Users/tongyin/Desktop/InsightBridge_模型测试系统/hotel_collector/hotel_real_data.db")

# ── 时间衰减因子 λ（月单位；λ=0.15 → 半衰期约4.6个月）
LAMBDA_DECAY = 0.15

# ── 澳门市场平台权重（内地客主导）
PLATFORM_WEIGHTS: dict[str, float] = {
    "ctrip":        0.45,   # 携程/去哪儿
    "meituan":      0.20,   # 美团民宿
    "booking_com":  0.20,   # Booking.com
    "tripadvisor":  0.10,   # TripAdvisor
    "agoda":        0.05,   # Agoda
}
DEFAULT_PLATFORM_WEIGHT = 0.10

# ── 定价影响系数上限（防止声誉信号过度主导价格）
GAMMA_MAX = 0.12            # ΔR_t 最多带来 ±12% 定价调节
MOMENTUM_BOOST = 0.05       # 动量最多额外 ±5%
PRICE_SENTIMENT_ELASTICITY = -0.008   # 每涨价10%，30天后评分约降0.08 (初始估计，待校准)


# ══════════════════════════════════════════════════════════════════════════
#  核心公式1：实时声誉指数 R_t
#  R_t = Σ(S_i · W_i · e^{-λ(t-ti)}) / Σ(W_i · e^{-λ(t-ti)})
# ══════════════════════════════════════════════════════════════════════════
def compute_R_t(hotel_id: str, conn: sqlite3.Connection) -> Optional[float]:
    """
    计算酒店的实时声誉指数 R_t ∈ [-1, 1]
    输入：review_sentiment 表（avg_sentiment 为 SnowNLP/TextBlob 的 [0,1] 输出）
    输出：归一化到 [-1,1] 的加权衰减均值
    """
    rows = conn.execute("""
        SELECT captured_date, avg_sentiment, sample_count, source
        FROM review_sentiment
        WHERE hotel_id = ? AND avg_sentiment IS NOT NULL
        ORDER BY captured_date DESC
        LIMIT 60
    """, (hotel_id,)).fetchall()

    if not rows:
        return None

    now = datetime.now()
    numerator = denominator = 0.0

    for date_str, s_raw, n, source in rows:
        # SnowNLP 输出 [0,1]，转换为 [-1,1]
        S_i = float(s_raw) * 2 - 1.0

        # 时间衰减（λ 以月为单位）
        try:
            t_i = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        days_ago = max((now - t_i).days, 0)
        decay = math.exp(-LAMBDA_DECAY * days_ago / 30.0)

        # 平台权重
        W_platform = PLATFORM_WEIGHTS.get(source, DEFAULT_PLATFORM_WEIGHT)

        # 样本量权重（log 压缩，防止大样本垄断）
        W_size = math.log(max(int(n or 1), 1) + 1) / math.log(101)

        W_i = W_platform * W_size * decay
        numerator  += S_i * W_i
        denominator += W_i

    if denominator < 1e-9:
        return None

    R_t = numerator / denominator
    return round(max(-1.0, min(1.0, R_t)), 4)


# ══════════════════════════════════════════════════════════════════════════
#  核心公式2：声誉动量 M_t = (R_t - R_{t-Δt}) / Δt
# ══════════════════════════════════════════════════════════════════════════
def compute_M_t(hotel_id: str, conn: sqlite3.Connection,
                window_days: int = 14) -> Optional[float]:
    """
    计算声誉动量（变化速度），窗口默认14天
    正值 = 声誉上升（可预期涨价机会）
    负值 = 声誉下降（需防御性定价或触发寻客）
    """
    # 当前时段 R_t（最近14天数据）
    now = datetime.now()
    cutoff_new = (now - timedelta(days=window_days)).strftime("%Y-%m-%d")
    cutoff_old = (now - timedelta(days=window_days * 2)).strftime("%Y-%m-%d")

    def _R_segment(date_from: str, date_to: str) -> Optional[float]:
        rows = conn.execute("""
            SELECT avg_sentiment, sample_count, source
            FROM review_sentiment
            WHERE hotel_id = ?
              AND captured_date BETWEEN ? AND ?
              AND avg_sentiment IS NOT NULL
        """, (hotel_id, date_from, date_to)).fetchall()
        if not rows:
            return None
        weighted_sum = weight_sum = 0.0
        for s_raw, n, source in rows:
            S_i = float(s_raw) * 2 - 1.0
            W_i = PLATFORM_WEIGHTS.get(source, DEFAULT_PLATFORM_WEIGHT) * \
                  math.log(max(int(n or 1), 1) + 1)
            weighted_sum += S_i * W_i
            weight_sum   += W_i
        return (weighted_sum / weight_sum) if weight_sum > 0 else None

    R_now  = _R_segment(cutoff_new, now.strftime("%Y-%m-%d"))
    R_prev = _R_segment(cutoff_old, cutoff_new)

    if R_now is None or R_prev is None:
        return None

    # 归一化到 [-1, 1]（最大变化 = ±2 在 window 内）
    M_t = (R_now - R_prev) / 2.0
    return round(max(-1.0, min(1.0, M_t)), 4)


# ══════════════════════════════════════════════════════════════════════════
#  核心公式3：相对竞争声誉差 ΔR_t = R_self - R_comp_avg
# ══════════════════════════════════════════════════════════════════════════
def compute_delta_R(hotel_id: str, tier: str,
                    conn: sqlite3.Connection) -> tuple[Optional[float], Optional[float]]:
    """
    计算本酒店声誉与同档次竞对均值的差距
    返回 (R_self, delta_R_t)
    delta_R_t > 0 = 口碑优于竞对 → 可适度溢价
    delta_R_t < 0 = 口碑弱于竞对 → 需防守定价
    """
    R_self = compute_R_t(hotel_id, conn)
    if R_self is None:
        return None, None

    # 同档次其他酒店均值（排除自身）
    rows = conn.execute("""
        SELECT rs.hotel_id, AVG(rs.avg_sentiment * 2 - 1) as R_avg
        FROM review_sentiment rs
        JOIN price_snapshots ps ON rs.hotel_id = ps.hotel_id
        WHERE ps.tier = ?
          AND rs.hotel_id != ?
          AND rs.avg_sentiment IS NOT NULL
          AND rs.captured_date >= date('now', '-30 days')
        GROUP BY rs.hotel_id
    """, (tier, hotel_id)).fetchall()

    if not rows:
        return R_self, None

    competitor_Rs = [float(r[1]) for r in rows if r[1] is not None]
    if not competitor_Rs:
        return R_self, None

    R_comp_avg = sum(competitor_Rs) / len(competitor_Rs)
    delta_R = R_self - R_comp_avg
    return R_self, round(max(-1.0, min(1.0, delta_R)), 4)


# ══════════════════════════════════════════════════════════════════════════
#  核心公式4：威尔逊置信压缩因子
#  γ_eff = γ × √(n / (n + z²))
# ══════════════════════════════════════════════════════════════════════════
def wilson_confidence(n: int, z: float = 1.96) -> float:
    """
    基于评论数量的置信因子 ∈ [0, 1]
    n < 5  → 因子 < 0.5（小样本，大幅压缩信号）
    n = 50 → 因子 ≈ 0.93
    n = 200 → 因子 ≈ 0.98
    """
    if n <= 0:
        return 0.0
    factor = math.sqrt(n / (n + z ** 2))
    return round(factor, 4)


# ══════════════════════════════════════════════════════════════════════════
#  综合接口：get_reputation_signals()
#  供 MARE 和 MDP 两个下游模型统一调用
# ══════════════════════════════════════════════════════════════════════════
def get_reputation_signals(hotel_id: str, tier: str,
                           conn: Optional[sqlite3.Connection] = None) -> dict:
    """
    返回本酒店完整声誉信号包，供定价和寻客模型使用。

    输出字段：
      R_t           — 实时声誉指数 [-1,1]
      M_t           — 声誉动量 [-1,1]
      delta_R       — 相对竞对声誉差 [-1,1]
      confidence    — 威尔逊置信因子 [0,1]
      gamma_eff     — 有效定价影响系数（压缩后）
      rep_adj       — 推荐价格调节幅度 [-0.17, +0.17]
      momentum_sign — "rising"/"falling"/"stable"
      alert_level   — "high"/"medium"/"low"（寻客触发强度）
      review_count  — 有效评论记录数
    """
    _owns_conn = conn is None
    if _owns_conn:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)

    try:
        # 评论数量
        row = conn.execute("""
            SELECT COALESCE(SUM(sample_count), 0)
            FROM review_sentiment
            WHERE hotel_id = ? AND avg_sentiment IS NOT NULL
              AND captured_date >= date('now', '-60 days')
        """, (hotel_id,)).fetchone()
        review_count = int(row[0]) if row else 0

        # 各信号计算
        R_t             = compute_R_t(hotel_id, conn)
        M_t             = compute_M_t(hotel_id, conn)
        R_self, delta_R = compute_delta_R(hotel_id, tier, conn)
        confidence      = wilson_confidence(review_count)

        # 置信压缩后的有效 γ
        gamma_eff = GAMMA_MAX * confidence

        # 价格调节幅度 = γ_eff × ΔR_t + MOMENTUM_BOOST × M_t
        delta_R_safe = delta_R if delta_R is not None else 0.0
        M_t_safe     = M_t     if M_t     is not None else 0.0
        rep_adj = round(
            max(-GAMMA_MAX - MOMENTUM_BOOST,
                min(GAMMA_MAX + MOMENTUM_BOOST,
                    gamma_eff * delta_R_safe + MOMENTUM_BOOST * M_t_safe)),
            4
        )

        # 动量方向
        if M_t_safe > 0.05:
            momentum_sign = "rising"
        elif M_t_safe < -0.05:
            momentum_sign = "falling"
        else:
            momentum_sign = "stable"

        # 寻客触发强度（主要看竞对声誉下滑）
        comp_R_avg = (R_self - delta_R_safe) if R_self is not None else None
        if comp_R_avg is not None and comp_R_avg < -0.2 and delta_R_safe > 0.15:
            alert_level = "high"    # 竞对口碑差，我们有明显优势 → 立即截流
        elif comp_R_avg is not None and comp_R_avg < 0.0:
            alert_level = "medium"  # 竞对口碑一般 → 温和截流
        else:
            alert_level = "low"     # 市场声誉均衡 → 观望

        return {
            "R_t":          R_t,
            "M_t":          M_t,
            "delta_R":      delta_R,
            "confidence":   confidence,
            "gamma_eff":    round(gamma_eff, 4),
            "rep_adj":      rep_adj,         # 直接乘以 base_price 得调节额
            "momentum_sign": momentum_sign,
            "alert_level":  alert_level,
            "review_count": review_count,
        }

    except Exception as e:
        log.warning(f"reputation signals error ({hotel_id}): {e}")
        return {
            "R_t": None, "M_t": None, "delta_R": None,
            "confidence": 0.0, "gamma_eff": 0.0, "rep_adj": 0.0,
            "momentum_sign": "stable", "alert_level": "low",
            "review_count": 0,
        }
    finally:
        if _owns_conn:
            conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  价格→情感弹性 ε（从历史数据回归，冷启动用先验值）
# ══════════════════════════════════════════════════════════════════════════
def estimate_price_sentiment_elasticity(hotel_id: str,
                                        conn: sqlite3.Connection) -> float:
    """
    ε = ∂R_{t+30} / ∂P_t / P_t
    从历史数据对（价格变动，30天后情感变动）做简单线性回归。
    数据不足时返回先验值 -0.008（每涨价10%，30天后评分约-0.08）

    TODO: 30天数据积累后启用回归，目前返回先验值
    """
    try:
        # 查价格变动和对应的情感变动（lag 30天）
        rows = conn.execute("""
            SELECT
                p1.official_bar as price_t,
                p2.official_bar as price_t30,
                s1.avg_sentiment as sent_t,
                s2.avg_sentiment as sent_t30
            FROM price_snapshots p1
            JOIN price_snapshots p2
                ON p1.hotel_id = p2.hotel_id
                AND date(p2.snapshot_time) = date(p1.snapshot_time, '+30 days')
            JOIN review_sentiment s1
                ON p1.hotel_id = s1.hotel_id
                AND date(s1.captured_date) = date(p1.snapshot_time)
            JOIN review_sentiment s2
                ON p1.hotel_id = s2.hotel_id
                AND date(s2.captured_date) = date(p1.snapshot_time, '+30 days')
            WHERE p1.hotel_id = ?
              AND p1.official_bar > 0 AND p2.official_bar > 0
            LIMIT 50
        """, (hotel_id,)).fetchall()

        if len(rows) < 10:
            return PRICE_SENTIMENT_ELASTICITY  # 先验值

        # 简单 OLS：ΔS ~ ε × ΔP/P
        xy = xsq = 0.0
        for price_t, price_t30, sent_t, sent_t30 in rows:
            delta_P_pct = (float(price_t30) - float(price_t)) / float(price_t)
            delta_S     = float(sent_t30) - float(sent_t)
            xy  += delta_P_pct * delta_S
            xsq += delta_P_pct ** 2

        if xsq < 1e-9:
            return PRICE_SENTIMENT_ELASTICITY

        eps = xy / xsq
        # 截断到合理范围
        return round(max(-0.05, min(0.0, eps)), 5)

    except Exception:
        return PRICE_SENTIMENT_ELASTICITY


# ══════════════════════════════════════════════════════════════════════════
#  便捷函数：存储声誉指数快照到 DB
# ══════════════════════════════════════════════════════════════════════════
def save_reputation_snapshot(hotel_id: str, tier: str,
                             conn: sqlite3.Connection) -> dict:
    """计算并存储本酒店的完整声誉快照，供历史趋势分析"""
    signals = get_reputation_signals(hotel_id, tier, conn)
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        conn.execute("""
            INSERT OR REPLACE INTO reputation_metrics
                (hotel_id, computed_date, R_t, M_t, delta_R,
                 confidence, gamma_eff, rep_adj, momentum_sign, alert_level, review_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (hotel_id, today,
              signals["R_t"], signals["M_t"], signals["delta_R"],
              signals["confidence"], signals["gamma_eff"], signals["rep_adj"],
              signals["momentum_sign"], signals["alert_level"], signals["review_count"]))
        conn.commit()
    except Exception as e:
        log.debug(f"save_reputation_snapshot error: {e}")

    return signals
