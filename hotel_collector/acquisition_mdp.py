"""
InsightBridge — 意图触发寻客 MDP
acquisition_mdp.py
================================================
基于"意图触发营销"（Intent-Triggered Marketing）框架
实现马尔可夫决策过程（MDP）行动选择器

状态空间 S：
  - sentiment_level:   声誉强度 {low, medium, high, crisis}
  - momentum:          声誉方向 {falling, stable, rising}
  - delta_r_level:     vs竞对优势 {weak, neutral, strong}
  - ltv_level:         客户LTV估计 {low, medium, high}
  - influencer_flag:   是否疑似KOL/意见领袖 {0, 1}

行动空间 A：
  A0 — 不干预（观望）
  A1 — 发放9折优惠券（直接转化）
  A2 — 触发VIP权益升级邀请（高LTV客户）
  A3 — 延迟24小时再观察（不确定状态）

奖励函数 R(S, A)：
  R = P(Conv|S,A) × Revenue_uplift
    - Cost(A)
    - OTA_Commission_Saved × P(Direct)
    - κ × P(Backlash|S,A)

调用方：
  hotel_data_collector.py run_collection() — 每日采集后触发评估
  run_21d_harness.py — 模拟测试中触发行动日志
"""

from __future__ import annotations
import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("ACQUISITION_MDP")

DB_PATH = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_collector/hotel_real_data.db")

# ── 奖励函数超参数 ─────────────────────────────────────────────────────────
KAPPA = 0.30        # 口碑反噬风险惩罚系数（越高越保守）
OTA_COMMISSION = 0.15   # OTA佣金率（节省直订时的额外奖励）

# ── 行动成本（相对于ADR的比例）──────────────────────────────────────────────
ACTION_COSTS: dict[str, float] = {
    "A0": 0.000,   # 不干预：零成本
    "A1": 0.100,   # 9折券：让利10%
    "A2": 0.030,   # VIP升级：运营成本约3%（早餐、礼品等）
    "A3": 0.005,   # 延迟：极小成本（系统资源）
}

# ── 各(状态, 行动)组合的转化概率估计 ─────────────────────────────────────────
# P(Conv|S,A)：基于行业文献初始估计，待A/B测试校准
#
# 键格式：(sentiment_level, momentum, delta_r_level) → {action: conv_rate}
# 说明：sentiment_level="crisis"时 A1 高转化（窗口期），但 backlash 风险也高
CONV_TABLE: dict[tuple, dict[str, float]] = {
    # (sentiment, momentum, delta_r)
    ("high",   "rising",  "strong"):  {"A0": 0.12, "A1": 0.28, "A2": 0.35, "A3": 0.10},
    ("high",   "rising",  "neutral"): {"A0": 0.10, "A1": 0.25, "A2": 0.30, "A3": 0.09},
    ("high",   "stable",  "strong"):  {"A0": 0.09, "A1": 0.22, "A2": 0.32, "A3": 0.08},
    ("high",   "stable",  "neutral"): {"A0": 0.08, "A1": 0.20, "A2": 0.27, "A3": 0.07},
    ("high",   "falling", "strong"):  {"A0": 0.07, "A1": 0.18, "A2": 0.25, "A3": 0.12},
    ("medium", "rising",  "strong"):  {"A0": 0.08, "A1": 0.22, "A2": 0.28, "A3": 0.09},
    ("medium", "rising",  "neutral"): {"A0": 0.07, "A1": 0.19, "A2": 0.24, "A3": 0.08},
    ("medium", "stable",  "neutral"): {"A0": 0.06, "A1": 0.16, "A2": 0.20, "A3": 0.07},
    ("medium", "falling", "weak"):    {"A0": 0.05, "A1": 0.14, "A2": 0.15, "A3": 0.10},
    ("low",    "falling", "weak"):    {"A0": 0.04, "A1": 0.12, "A2": 0.10, "A3": 0.08},
    ("crisis", "falling", "weak"):    {"A0": 0.03, "A1": 0.18, "A2": 0.06, "A3": 0.05},
}
# 默认转化率（未在表中的状态组合）
DEFAULT_CONV: dict[str, float] = {"A0": 0.05, "A1": 0.15, "A2": 0.18, "A3": 0.07}

# ── 口碑反噬概率 P(Backlash|S,A) ────────────────────────────────────────────
# 声誉危机期强推折扣有较高反噬风险（"促销焦虑"）
BACKLASH_TABLE: dict[tuple, dict[str, float]] = {
    ("crisis", "falling", "weak"):  {"A0": 0.01, "A1": 0.25, "A2": 0.12, "A3": 0.02},
    ("low",    "falling", "weak"):  {"A0": 0.01, "A1": 0.10, "A2": 0.05, "A3": 0.01},
    ("medium", "falling", "weak"):  {"A0": 0.01, "A1": 0.06, "A2": 0.03, "A3": 0.01},
}
DEFAULT_BACKLASH: dict[str, float] = {"A0": 0.005, "A1": 0.03, "A2": 0.015, "A3": 0.005}


# ══════════════════════════════════════════════════════════════════════════
#  状态分类函数
# ══════════════════════════════════════════════════════════════════════════

def classify_sentiment_level(R_t: Optional[float]) -> str:
    """R_t [-1,1] → sentiment_level 离散化"""
    if R_t is None:
        return "medium"   # 冷启动默认
    if R_t >= 0.4:
        return "high"
    if R_t >= 0.0:
        return "medium"
    if R_t >= -0.4:
        return "low"
    return "crisis"


def classify_delta_r(delta_R: Optional[float]) -> str:
    """ΔR_t [-1,1] → 竞对差距离散化"""
    if delta_R is None:
        return "neutral"
    if delta_R >= 0.15:
        return "strong"
    if delta_R <= -0.15:
        return "weak"
    return "neutral"


def classify_ltv(avg_clv: Optional[float], star: int = 3) -> str:
    """LTV 估计 → 离散化（简单分位：按星级标准化）"""
    if avg_clv is None:
        return "medium"
    thresholds = {2: (3000, 8000), 3: (5000, 15000), 4: (10000, 30000), 5: (20000, 60000)}
    lo, hi = thresholds.get(star, (5000, 15000))
    if avg_clv >= hi:
        return "high"
    if avg_clv >= lo:
        return "medium"
    return "low"


# ══════════════════════════════════════════════════════════════════════════
#  奖励函数
# ══════════════════════════════════════════════════════════════════════════

def reward(action: str, state_key: tuple, adr: float, ltv_level: str) -> float:
    """
    R(S, A) = P(Conv|S,A) × Revenue_uplift
            - Cost(A) × adr
            - OTA_Commission_Saved × P(Direct|A)  [A1直订奖励]
            - κ × P(Backlash|S,A) × adr

    Revenue_uplift 对 A2（VIP升级）高LTV客户额外 × 1.5
    """
    conv = CONV_TABLE.get(state_key, DEFAULT_CONV).get(action, 0.05)
    backlash = BACKLASH_TABLE.get(state_key, DEFAULT_BACKLASH).get(action, 0.005)
    cost = ACTION_COSTS[action] * adr

    # 直订奖励（只对 A1/A2，因为会附带直订引流）
    ota_saved = OTA_COMMISSION * adr * conv if action in ("A1", "A2") else 0.0

    # LTV 放大（A2 对高LTV客户价值更高）
    ltv_multiplier = 1.5 if (action == "A2" and ltv_level == "high") else 1.0
    revenue_uplift = conv * adr * ltv_multiplier

    r = revenue_uplift - cost + ota_saved - KAPPA * backlash * adr
    return round(r, 2)


# ══════════════════════════════════════════════════════════════════════════
#  MDP 决策器
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class MDPDecision:
    hotel_id:        str
    timestamp:       str
    action:          str          # "A0" / "A1" / "A2" / "A3"
    action_label:    str          # 人类可读描述
    R_t:             Optional[float]
    M_t:             Optional[float]
    delta_R:         Optional[float]
    alert_level:     str
    sentiment_level: str
    momentum:        str
    delta_r_level:   str
    ltv_level:       str
    influencer_flag: int
    best_reward:     float
    all_rewards:     dict
    trigger_reason:  str


ACTION_LABELS = {
    "A0": "不干预（观望）",
    "A1": "发放9折优惠券",
    "A2": "触发VIP权益升级",
    "A3": "延迟24h再评估",
}


def select_action(
    hotel_id: str,
    R_t: Optional[float],
    M_t: Optional[float],
    delta_R: Optional[float],
    alert_level: str = "low",
    adr: float = 800.0,
    avg_clv: Optional[float] = None,
    star: int = 3,
    influencer_flag: int = 0,
) -> MDPDecision:
    """
    对单个酒店评估所有行动的预期奖励，选出最优行动。

    Args:
        hotel_id:        酒店ID
        R_t:             声誉指数 [-1,1]
        M_t:             声誉动量 [-1,1]
        delta_R:         vs竞对差 [-1,1]
        alert_level:     "high"/"medium"/"low"（来自 get_reputation_signals）
        adr:             当前ADR（用于计算绝对收益）
        avg_clv:         客户平均LTV（MOP）
        star:            酒店星级
        influencer_flag: 是否有KOL意向信号
    """
    # ── 状态离散化 ────────────────────────────────────────────────────────
    sentiment_level = classify_sentiment_level(R_t)
    momentum        = M_t and ("rising" if M_t > 0.05 else "falling" if M_t < -0.05 else "stable") or "stable"
    delta_r_level   = classify_delta_r(delta_R)
    ltv_level       = classify_ltv(avg_clv, star)

    state_key = (sentiment_level, momentum, delta_r_level)

    # ── 候选行动集（根据状态过滤不合适行动）──────────────────────────────
    candidates = ["A0", "A1", "A2", "A3"]

    # A2（VIP升级）只在中高LTV客户或高alert时激活
    if ltv_level == "low" and alert_level != "high":
        candidates = [a for a in candidates if a != "A2"]

    # 声誉危机期 A1 风险高 → 如果 kappa > 0.25，改为保守策略 A3
    if sentiment_level == "crisis" and KAPPA > 0.25:
        candidates = [a for a in candidates if a != "A1"]

    # KOL 信号存在时强制纳入 A2（高价值渠道机会）
    if influencer_flag and "A2" not in candidates:
        candidates.append("A2")

    # ── 计算各行动奖励 ────────────────────────────────────────────────────
    all_rewards = {}
    for a in ["A0", "A1", "A2", "A3"]:
        all_rewards[a] = reward(a, state_key, adr, ltv_level)

    best_action = max(candidates, key=lambda a: all_rewards[a])
    best_reward = all_rewards[best_action]

    # ── 触发原因 ──────────────────────────────────────────────────────────
    reasons = []
    if alert_level == "high":
        reasons.append("竞对口碑显著下滑，窗口期截流")
    if sentiment_level in ("high",) and momentum == "rising":
        reasons.append("声誉上升期，溢价能力强")
    if sentiment_level == "crisis":
        reasons.append("本店声誉危机，需防御")
    if influencer_flag:
        reasons.append("检测到KOL意向信号")
    if not reasons:
        reasons.append("常规声誉评估")
    trigger_reason = " | ".join(reasons)

    return MDPDecision(
        hotel_id=hotel_id,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        action=best_action,
        action_label=ACTION_LABELS[best_action],
        R_t=R_t,
        M_t=M_t,
        delta_R=delta_R,
        alert_level=alert_level,
        sentiment_level=sentiment_level,
        momentum=momentum,
        delta_r_level=delta_r_level,
        ltv_level=ltv_level,
        influencer_flag=influencer_flag,
        best_reward=best_reward,
        all_rewards=all_rewards,
        trigger_reason=trigger_reason,
    )


# ══════════════════════════════════════════════════════════════════════════
#  DB 存储：acquisition_triggers 表
# ══════════════════════════════════════════════════════════════════════════

def ensure_triggers_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS acquisition_triggers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_id        TEXT NOT NULL,
            triggered_at    TEXT NOT NULL,
            action          TEXT NOT NULL,           -- A0/A1/A2/A3
            action_label    TEXT,
            R_t             REAL,
            M_t             REAL,
            delta_R         REAL,
            alert_level     TEXT,
            sentiment_level TEXT,
            momentum        TEXT,
            ltv_level       TEXT,
            influencer_flag INTEGER DEFAULT 0,
            best_reward     REAL,
            all_rewards_json TEXT,                   -- JSON {"A0":..., "A1":...}
            trigger_reason  TEXT,
            outcome_booked  INTEGER DEFAULT 0,       -- 事后回填：是否实际转化
            outcome_revenue REAL                     -- 事后回填：实际收入
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_acq_hotel_time
        ON acquisition_triggers(hotel_id, triggered_at)
    """)
    conn.commit()


def save_trigger(decision: MDPDecision, conn: sqlite3.Connection) -> int:
    """将 MDP 决策存入 acquisition_triggers 表，返回 row id"""
    ensure_triggers_table(conn)
    cur = conn.execute("""
        INSERT INTO acquisition_triggers
            (hotel_id, triggered_at, action, action_label,
             R_t, M_t, delta_R, alert_level, sentiment_level, momentum,
             ltv_level, influencer_flag, best_reward, all_rewards_json, trigger_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        decision.hotel_id, decision.timestamp, decision.action, decision.action_label,
        decision.R_t, decision.M_t, decision.delta_R,
        decision.alert_level, decision.sentiment_level, decision.momentum,
        decision.ltv_level, decision.influencer_flag,
        decision.best_reward, json.dumps(decision.all_rewards), decision.trigger_reason,
    ))
    conn.commit()
    return cur.lastrowid


# ══════════════════════════════════════════════════════════════════════════
#  批量评估入口：run_acquisition_sweep()
#  在 hotel_data_collector 的 run_collection() 结束后调用
# ══════════════════════════════════════════════════════════════════════════

def run_acquisition_sweep(hotels: list[dict], conn: sqlite3.Connection,
                          verbose: bool = True) -> list[dict]:
    """
    对列表中所有酒店跑一次 MDP 评估并写入 DB。
    只对 alert_level != "low" 或 A0 非最优时才写入记录（节省噪音）。

    Args:
        hotels: HOTELS_76 列表（需含 id, tier, star 字段）
        conn:   活跃的 SQLite 连接
        verbose: 是否打印行动摘要

    Returns:
        list of {hotel_id, action, trigger_reason} for non-trivial decisions
    """
    from sentiment_engine import get_reputation_signals

    ensure_triggers_table(conn)
    active_decisions = []

    for hotel in hotels:
        hotel_id = hotel.get("id") or hotel.get("hotel_id", "")
        tier     = hotel.get("tier", "3_star")
        star     = hotel.get("star", 3)

        try:
            signals = get_reputation_signals(hotel_id, tier, conn)
        except Exception:
            continue

        # 从 price_snapshots 取最新 ADR 估计
        row = conn.execute("""
            SELECT AVG(official_bar) FROM price_snapshots
            WHERE hotel_id = ? AND official_bar > 0
            ORDER BY snapshot_time DESC LIMIT 20
        """, (hotel_id,)).fetchone()
        adr = float(row[0]) if row and row[0] else 800.0

        decision = select_action(
            hotel_id=hotel_id,
            R_t=signals["R_t"],
            M_t=signals["M_t"],
            delta_R=signals["delta_R"],
            alert_level=signals["alert_level"],
            adr=adr,
            star=star,
        )

        # 只记录有实际行动意义的决策（A0 且 alert=low 的跳过，节省存储）
        if decision.action != "A0" or decision.alert_level != "low":
            save_trigger(decision, conn)
            active_decisions.append({
                "hotel_id": hotel_id,
                "action": decision.action,
                "action_label": decision.action_label,
                "trigger_reason": decision.trigger_reason,
                "alert_level": decision.alert_level,
            })
            if verbose:
                log.info(
                    f"  [MDP] {hotel_id} → {decision.action}({decision.action_label})"
                    f" | R={decision.R_t} alert={decision.alert_level}"
                    f" | {decision.trigger_reason}"
                )

    return active_decisions


# ══════════════════════════════════════════════════════════════════════════
#  弹性校准入口（Step 5）：weekly_elasticity_calibration()
#  每周调用一次，更新 PRICE_SENTIMENT_ELASTICITY 先验值
# ══════════════════════════════════════════════════════════════════════════

def weekly_elasticity_calibration(conn: sqlite3.Connection) -> dict:
    """
    从 DB 读取价格-情感变动对，运行 OLS 回归，
    更新各酒店的弹性估计并存入 elasticity_calibration 表。

    Returns: {"hotels_calibrated": N, "avg_epsilon": float, "prior": float}
    """
    from sentiment_engine import estimate_price_sentiment_elasticity, PRICE_SENTIMENT_ELASTICITY

    # 确保弹性结果表存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS elasticity_calibration (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_id        TEXT NOT NULL,
            calibrated_date TEXT NOT NULL,
            epsilon         REAL,                   -- ε: price→sentiment弹性
            sample_count    INTEGER,
            method          TEXT DEFAULT 'OLS',     -- "OLS" or "prior"
            UNIQUE(hotel_id, calibrated_date)
        )
    """)
    conn.commit()

    # 获取有足够数据的酒店列表
    hotels_with_data = conn.execute("""
        SELECT DISTINCT hotel_id FROM price_snapshots
        WHERE official_bar > 0
        GROUP BY hotel_id HAVING COUNT(*) >= 10
    """).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    epsilons = []
    calibrated = 0

    for (hotel_id,) in hotels_with_data:
        eps = estimate_price_sentiment_elasticity(hotel_id, conn)
        sample_row = conn.execute("""
            SELECT COUNT(*) FROM price_snapshots WHERE hotel_id = ?
        """, (hotel_id,)).fetchone()
        n = int(sample_row[0]) if sample_row else 0
        method = "OLS" if n >= 10 else "prior"

        conn.execute("""
            INSERT OR REPLACE INTO elasticity_calibration
                (hotel_id, calibrated_date, epsilon, sample_count, method)
            VALUES (?,?,?,?,?)
        """, (hotel_id, today, eps, n, method))
        epsilons.append(eps)
        calibrated += 1

    conn.commit()

    avg_eps = sum(epsilons) / len(epsilons) if epsilons else PRICE_SENTIMENT_ELASTICITY
    log.info(
        f"[弹性校准] 校准{calibrated}家酒店 | 均值ε={avg_eps:.5f}"
        f" | 先验={PRICE_SENTIMENT_ELASTICITY}"
    )
    return {
        "hotels_calibrated": calibrated,
        "avg_epsilon": round(avg_eps, 5),
        "prior": PRICE_SENTIMENT_ELASTICITY,
    }


# ══════════════════════════════════════════════════════════════════════════
#  命令行快速测试入口
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [MDP] %(message)s")

    parser = argparse.ArgumentParser(description="InsightBridge 意图触发 MDP 测试")
    parser.add_argument("--hotel",  default="MAC_5DX_WYNN_001", help="酒店ID")
    parser.add_argument("--adr",    type=float, default=1529.0,  help="当前ADR (MOP)")
    parser.add_argument("--star",   type=int,   default=5,       help="星级")
    parser.add_argument("--sweep",  action="store_true",         help="对所有有数据的酒店做批量扫描")
    parser.add_argument("--calibrate", action="store_true",      help="运行弹性校准")
    args = parser.parse_args()

    with sqlite3.connect(str(DB_PATH), timeout=10) as _conn:
        if args.calibrate:
            result = weekly_elasticity_calibration(_conn)
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.sweep:
            # 简单扫描：从 DB 读所有有sentiment数据的酒店
            _hotels_in_db = _conn.execute(
                "SELECT DISTINCT hotel_id FROM review_sentiment"
            ).fetchall()
            _hotel_list = [{"id": r[0], "hotel_id": r[0], "tier": "5_deluxe", "star": 5}
                           for r in _hotels_in_db]
            decisions = run_acquisition_sweep(_hotel_list, _conn, verbose=True)
            print(f"\n触发行动: {len(decisions)}家")
            for d in decisions:
                print(f"  {d['hotel_id']:30s} → {d['action']}({d['action_label']})")

        else:
            from sentiment_engine import get_reputation_signals
            signals = get_reputation_signals(args.hotel, "5_deluxe", _conn)
            decision = select_action(
                hotel_id=args.hotel,
                R_t=signals["R_t"],
                M_t=signals["M_t"],
                delta_R=signals["delta_R"],
                alert_level=signals["alert_level"],
                adr=args.adr,
                star=args.star,
            )
            print(json.dumps(asdict(decision), indent=2, ensure_ascii=False))
