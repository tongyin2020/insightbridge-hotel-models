"""
tools/risk_guard.py
═══════════════════════════════════════════════════════════════════════════════
InsightBridge — 高杠杆资金安全守护系统（三道防线 + 两个附加机制）

在100-220倍杠杆下，盈利是目标，但资金安全是前提。
本模块集成了以下数学模型：

  ┌─────────────────────────────────────────────────────────────────────┐
  │ 防线一：卡尔曼滤波动态止损 (KalmanDynamicStop)                       │
  │    从杂乱跳动中提取"真实价格曲线"，基于偏离程度设置动态止损          │
  │    原理：状态空间模型，递推最优估计，无滞后，抗噪                    │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 防线二：流动性枯竭退出 (LiquidityExhaustionDetector)                 │
  │    监控10档盘口深度总量。买盘萎缩40%→支撑消失，领先价格暴跌50-200ms │
  │    附带OBI斜率追踪(OBISlopeTracker)：速度比绝对值更危险              │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 防线三：波动率断路器 (VolatilityBreaker)                             │
  │    监控点差(Spread)实时扩张。>3倍均值→停止所有开仓、立即平仓         │
  │    高杠杆下点差跳升=瞬间浮亏，发布数据时必须自动触发                 │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 附加1：时间断路器 (TemporalCutoff)                                   │
  │    进场30秒不盈利→强制离场。高杠杆不宜在不确定性中磨损               │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 附加2：跨品种联动预警 (CrossAssetLinkage)                            │
  │    ZN/ZB国债盘口崩塌→领先ES/NQ股指100-500ms。利用时间差逃生          │
  └─────────────────────────────────────────────────────────────────────┘

数学基础：
  Kalman Filter:    P(k|k) = (I-KH)P(k|k-1), K=P(k|k-1)Hᵀ(HPH ᵀ+R)⁻¹
  Weighted OBI:     Σ(wᵢ·Bid_i - wᵢ·Ask_i) / Σwᵢ(Bid_i+Ask_i), wᵢ=1/(i+1)
  OBI Velocity:     dOBI/dt → 斜率负且急剧 = 支撑崩塌
  Liquidity Depth:  ΔDepth/Depth₀ < -40% → EXHAUST
  Spread Ratio:     current_spread / avg_spread(60s) > 3.0 → BREAKER
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class RiskSignal:
    """统一的风险信号输出格式"""
    should_exit:    bool
    urgency:        str       # IMMEDIATE / SOON / MONITOR / HOLD
    source:         str       # 触发防线名称
    reason:         str
    pnl_pct:        float = 0.0
    detail:         dict  = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "should_exit": self.should_exit,
            "urgency":     self.urgency,
            "source":      self.source,
            "reason":      self.reason,
            "pnl_pct":     round(self.pnl_pct, 4),
            **self.detail,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  防线一：卡尔曼滤波动态止损
# ══════════════════════════════════════════════════════════════════════════════

class KalmanPriceFilter:
    """
    1D Kalman Filter for price tracking (constant velocity model).

    State vector:  x = [price, velocity]ᵀ
    Measurement:   z = [raw_price]

    Equations:
      Predict:  x̂ₖ₋  = F·x̂ₖ₋₁,  P⁻ = F·P·Fᵀ + Q
      Update:   K     = P⁻·Hᵀ·(H·P⁻·Hᵀ + R)⁻¹
                x̂ₖ   = x̂ₖ₋ + K·(z - H·x̂ₖ₋)
                Pₖ    = (I - K·H)·P⁻

    应用：从高频价格噪声中提取"真实价格曲线"。
    动态止损逻辑：当市价偏离真实价格曲线超过杠杆调整后的阈值，触发止损。
    """

    def __init__(
        self,
        process_noise:     float = 1e-4,   # Q — 价格真实漂移的不确定性
        measurement_noise: float = 5e-4,   # R — 行情数据的噪声程度
    ):
        self.Q = process_noise
        self.R = measurement_noise
        # 状态向量 [price, velocity]
        self._x: Optional[np.ndarray] = None
        # 协方差矩阵 2×2
        self._P: Optional[np.ndarray] = None

    def reset(self):
        self._x = None
        self._P = None

    def update(self, price: float) -> Tuple[float, float]:
        """
        输入原始价格，输出 (filtered_price, velocity)。
        velocity > 0 → 上涨动能；velocity < 0 → 下跌动能。
        """
        price = float(price)

        if self._x is None:
            self._x = np.array([price, 0.0])
            self._P = np.eye(2) * 1.0
            return price, 0.0

        # 状态转移矩阵（恒速模型）
        F = np.array([[1.0, 1.0],
                      [0.0, 1.0]])
        # 观测矩阵（只观测价格）
        H = np.array([[1.0, 0.0]])
        # 过程噪声协方差
        Q = np.array([[self.Q,       0.0],
                      [0.0, self.Q * 0.1]])
        # 观测噪声协方差
        R = np.array([[self.R]])

        # ── Predict ──────────────────────────────────────────────
        x_pred = F @ self._x
        P_pred = F @ self._P @ F.T + Q

        # ── Update ───────────────────────────────────────────────
        innov  = price - (H @ x_pred)[0]
        S      = (H @ P_pred @ H.T + R)[0, 0]
        K      = (P_pred @ H.T / S).flatten()

        self._x = x_pred + K * innov
        self._P = (np.eye(2) - np.outer(K, H)) @ P_pred

        return float(self._x[0]), float(self._x[1])

    def filtered_price(self) -> float:
        return float(self._x[0]) if self._x is not None else 0.0

    def velocity(self) -> float:
        return float(self._x[1]) if self._x is not None else 0.0


class KalmanDynamicStop:
    """
    基于卡尔曼滤波的动态止损系统。

    核心逻辑：
      - 用卡尔曼滤波剔除价格噪声，得到"真实价格"
      - 动态止损 = 真实价格 ± (ATR × 杠杆调整系数)
      - 若价格速度(velocity)与持仓方向相反，止损收紧30%
      - 避免因随机噪声触发止损，只在真实趋势反转时离场

    杠杆调整公式：
      stop_distance = price × atr_pct × (1 / leverage^0.25)
      （杠杆越高，止损距离越小，但不能小于 0.001 = 0.1%）
    """

    def __init__(
        self,
        process_noise:     float = 1e-4,
        measurement_noise: float = 5e-4,
    ):
        self._kf = KalmanPriceFilter(process_noise, measurement_noise)
        self._prices: deque = deque(maxlen=30)

    def update(self, price: float) -> Tuple[float, float]:
        """更新一个新价格，返回 (filtered_price, velocity)。"""
        fp, vel = self._kf.update(price)
        self._prices.append(price)
        return fp, vel

    def get_dynamic_stop(
        self,
        direction:  int,
        leverage:   int   = 100,
        atr_pct:    float = 0.003,
    ) -> dict:
        """
        计算当前动态止损价格。

        Returns:
            stop_price, stop_distance_pct, kalman_price, velocity, tightened
        """
        fp   = self._kf.filtered_price()
        vel  = self._kf.velocity()

        if fp <= 0:
            return {"error": "Kalman not initialized"}

        # 杠杆越高，能承受的止损距离越小
        leverage_factor = max(0.15, 1.0 / (leverage ** 0.25))
        base_distance   = fp * atr_pct * leverage_factor

        # 速度反向时收紧止损
        tightened = False
        if direction == 1 and vel < -fp * 0.0002:   # 多单但价格在加速下跌
            base_distance *= 0.70
            tightened = True
        elif direction == -1 and vel > fp * 0.0002:  # 空单但价格在加速上涨
            base_distance *= 0.70
            tightened = True

        # 最小止损距离（防止太紧被噪声打掉）
        min_distance = fp * 0.0008
        base_distance = max(base_distance, min_distance)

        if direction == 1:
            stop_price = fp - base_distance
        else:
            stop_price = fp + base_distance

        return {
            "stop_price":         round(stop_price, 4),
            "stop_distance_pct":  round(base_distance / fp * 100, 3),
            "kalman_price":       round(fp, 4),
            "velocity":           round(vel, 6),
            "tightened_by_vel":   tightened,
            "leverage_factor":    round(leverage_factor, 3),
        }

    def evaluate_exit(
        self,
        current_price: float,
        direction:     int,
        leverage:      int   = 100,
        atr_pct:       float = 0.003,
    ) -> RiskSignal:
        """根据卡尔曼滤波止损，判断是否应该退出。"""
        self.update(current_price)
        stop_info = self.get_dynamic_stop(direction, leverage, atr_pct)

        if "error" in stop_info:
            return RiskSignal(False, "HOLD", "Kalman", "数据不足", detail=stop_info)

        stop_price = stop_info["stop_price"]
        hit = (direction == 1 and current_price <= stop_price) or \
              (direction == -1 and current_price >= stop_price)

        pnl_approx = 0.0
        return RiskSignal(
            should_exit = hit,
            urgency     = "IMMEDIATE" if hit else "HOLD",
            source      = "KalmanDynamicStop",
            reason      = (f"价格{current_price:.2f}触及卡尔曼动态止损{stop_price:.2f}"
                           if hit else "价格在卡尔曼止损范围内"),
            pnl_pct     = pnl_approx,
            detail      = stop_info,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  OBI斜率追踪（配合防线二使用）
# ══════════════════════════════════════════════════════════════════════════════

class OBISlopeTracker:
    """
    追踪OBI的变化速率（斜率/速度）。

    绝对值 vs 斜率：
      OBI = 0.3 → 买盘稍强，正常
      OBI 0.8 → 0.3 in 50ms → 斜率 = -10/s → 支撑崩塌中！

    核心公式：slope = ΔOBI / Δt（每秒变化量）
    """

    def __init__(self, window: int = 8):
        self._obi_buf:  deque = deque(maxlen=window)
        self._time_buf: deque = deque(maxlen=window)

    def update(self, obi: float, ts: Optional[float] = None):
        self._obi_buf.append(float(obi))
        self._time_buf.append(ts or time.time())

    def slope(self) -> float:
        """返回OBI斜率（每秒）。负值=支撑在消失。"""
        if len(self._obi_buf) < 2:
            return 0.0
        dt = self._time_buf[-1] - self._time_buf[0]
        if dt < 1e-6:
            return 0.0
        return (self._obi_buf[-1] - self._obi_buf[0]) / dt

    def is_collapsing(self, threshold: float = -0.3) -> bool:
        """斜率低于阈值 → 支撑正在加速消失。"""
        return self.slope() < threshold

    def current(self) -> float:
        return float(self._obi_buf[-1]) if self._obi_buf else 0.0

    def peak(self) -> float:
        return max(self._obi_buf) if self._obi_buf else 0.0

    def drawdown_from_peak(self) -> float:
        """从峰值回落了多少（正值=回落程度）。"""
        if not self._obi_buf:
            return 0.0
        return self.peak() - self.current()

    def summary(self) -> dict:
        return {
            "obi_current":     round(self.current(), 3),
            "obi_peak":        round(self.peak(), 3),
            "obi_slope_per_s": round(self.slope(), 3),
            "drawdown":        round(self.drawdown_from_peak(), 3),
            "collapsing":      self.is_collapsing(),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  防线二：流动性枯竭退出
# ══════════════════════════════════════════════════════════════════════════════

def calc_weighted_obi(
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    levels: int = 5,
) -> float:
    """
    计算加权OBI（前N档，权重递减）。

    Args:
        bids: [(price, size), ...] 按最优价排列
        asks: [(price, size), ...]
        levels: 档数（建议5-10）

    公式：
        wᵢ = 1/(i+1)    i=0,1,...,levels-1
        OBI = Σwᵢ·Bidᵢ - Σwᵢ·Askᵢ
              ─────────────────────────
                  Σwᵢ·(Bidᵢ + Askᵢ)

    相比只看买一卖一，5档加权OBI能识别"虚假挂单欺骗"。
    """
    bid_sum = 0.0
    ask_sum = 0.0
    weight_sum = 0.0

    n = min(levels, len(bids), len(asks))
    for i in range(n):
        w       = 1.0 / (i + 1)
        bid_sum += bids[i][1] * w
        ask_sum += asks[i][1] * w
        weight_sum += w * (bids[i][1] + asks[i][1])

    if weight_sum < 1e-10:
        return 0.0
    return (bid_sum - ask_sum) / weight_sum


class LiquidityExhaustionDetector:
    """
    检测盘口深度崩塌——在价格跌落之前50-200ms发出预警。

    核心逻辑：
      做多时监控买盘总深度（5档）：
        depth_change = (current_total_bid - peak_bid) / peak_bid
        若 depth_change < -40% → 支撑消失，AI先于市场逃生

      做空时监控卖盘总深度：
        若卖盘萎缩 → 做空动能枯竭

    附带：
      - OBI斜率追踪：检测支撑坍塌的速度
      - 连续衰减计数：防止单次抖动触发误报（需连续N次）
    """

    def __init__(
        self,
        exhaustion_threshold: float = 0.40,   # 深度萎缩40%触发
        consecutive_required: int   = 3,       # 需连续3次才触发（防抖）
        depth_levels:         int   = 5,
    ):
        self.threshold    = exhaustion_threshold
        self.consec_req   = consecutive_required
        self.levels       = depth_levels

        self._bid_history: deque = deque(maxlen=20)
        self._ask_history: deque = deque(maxlen=20)
        self._obi_slope   = OBISlopeTracker(window=10)
        self._consec_warn: int   = 0

    def update(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        ts: Optional[float] = None,
    ):
        """每次盘口更新时调用。"""
        bid_total = sum(b[1] for b in bids[:self.levels])
        ask_total = sum(a[1] for a in asks[:self.levels])

        self._bid_history.append(bid_total)
        self._ask_history.append(ask_total)

        # 更新OBI斜率
        total = bid_total + ask_total
        obi   = (bid_total - ask_total) / total if total > 0 else 0.0
        self._obi_slope.update(obi, ts)

    def evaluate(self, direction: int = 1) -> RiskSignal:
        """
        direction=+1 做多 → 监控买盘枯竭
        direction=-1 做空 → 监控卖盘枯竭
        """
        if len(self._bid_history) < 3:
            return RiskSignal(False, "HOLD", "LiquidityExhaustion",
                              "数据点不足，等待更多盘口快照")

        bid_hist = list(self._bid_history)
        ask_hist = list(self._ask_history)

        if direction == 1:
            hist = bid_hist
            label = "买盘"
        else:
            hist = ask_hist
            label = "卖盘"

        peak_depth    = max(hist)
        current_depth = hist[-1]
        depth_change  = ((current_depth - peak_depth) / peak_depth
                         if peak_depth > 0 else 0.0)

        # 加权OBI
        obi_slope_info = self._obi_slope.summary()
        collapsing     = self._obi_slope.is_collapsing(threshold=-0.25)

        # 判断是否枯竭
        exhausted = depth_change < -self.threshold

        if exhausted:
            self._consec_warn += 1
        else:
            self._consec_warn = max(0, self._consec_warn - 1)

        triggered = self._consec_warn >= self.consec_req

        urgency = "HOLD"
        if triggered:
            urgency = "IMMEDIATE"
        elif exhausted:
            urgency = "SOON"
        elif collapsing:
            urgency = "MONITOR"

        return RiskSignal(
            should_exit = triggered,
            urgency     = urgency,
            source      = "LiquidityExhaustion",
            reason      = (
                f"{'⚠ ' if triggered else ''}{label}深度从峰值萎缩 "
                f"{abs(depth_change)*100:.1f}%"
                f"{'（连续'+str(self._consec_warn)+'次）' if exhausted else ''}，"
                f"OBI斜率={obi_slope_info['obi_slope_per_s']:.2f}/s"
                f"{'（坍塌中）' if collapsing else ''}"
            ),
            detail = {
                "peak_depth":          round(peak_depth, 0),
                "current_depth":       round(current_depth, 0),
                "depth_change_pct":    round(depth_change * 100, 1),
                "consecutive_warning": self._consec_warn,
                **obi_slope_info,
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  防线三：波动率断路器
# ══════════════════════════════════════════════════════════════════════════════

class VolatilityBreaker:
    """
    监控买卖点差(Spread)实时扩张，保护高杠杆头寸。

    危险场景：
      FOMC发布后，BTC/USDT点差从0.5美元瞬间跳至3美元
      在100倍杠杆下，这等于立刻浮亏 3/100000 × 100 = 0.3%（对小账户是致命的）

    断路规则：
      current_spread / rolling_avg_spread(60s) > threshold(默认3.0)
      → HALT_TRADING（停止开仓）+ 若已持仓 → CLOSE_IMMEDIATELY

    附加：
      - 点差趋势追踪：点差在扩大还是收窄？
      - 恢复检测：点差回落后自动解除断路
    """

    def __init__(
        self,
        trigger_ratio:    float = 3.0,    # 超过正常值的倍数触发
        recovery_ratio:   float = 1.5,    # 低于此倍数时解除断路
        history_window:   int   = 120,    # 用120个数据点计算基线均值
    ):
        self.trigger_ratio  = trigger_ratio
        self.recovery_ratio = recovery_ratio
        self._spread_hist:  deque = deque(maxlen=history_window)
        self._triggered:    bool  = False
        self._trigger_ts:   float = 0.0

    def update(self, bid: float, ask: float):
        """每次行情更新时调用。"""
        if bid > 0 and ask > bid:
            spread_pct = (ask - bid) / bid
            self._spread_hist.append(spread_pct)

    def evaluate(self, bid: float, ask: float) -> dict:
        """
        检查当前点差是否触发断路器。
        Returns: dict with breaker_triggered, spread_ratio, action, reason
        """
        self.update(bid, ask)

        if len(self._spread_hist) < 10:
            return {
                "breaker_triggered": False,
                "spread_ratio":      1.0,
                "action":            "NORMAL",
                "reason":            "历史数据不足，跳过断路器检查",
            }

        current_spread = (ask - bid) / bid if bid > 0 else 0.0

        # 基线：排除最近5个点（这几个本身可能已经异常）
        baseline_data = list(self._spread_hist)[:-5]
        avg_spread    = float(np.mean(baseline_data)) if baseline_data else current_spread
        std_spread    = float(np.std(baseline_data))  if baseline_data else 0.0

        if avg_spread < 1e-8:
            return {"breaker_triggered": False, "spread_ratio": 1.0,
                    "action": "NORMAL", "reason": "均值为零，跳过"}

        ratio = current_spread / avg_spread

        # 触发断路
        if ratio >= self.trigger_ratio:
            self._triggered   = True
            self._trigger_ts  = time.time()

        # 恢复检测（触发后点差回落）
        if self._triggered and ratio < self.recovery_ratio:
            seconds_since = time.time() - self._trigger_ts
            if seconds_since > 30:    # 持续30秒稳定后才解除
                self._triggered = False

        action = "HALT_ALL_TRADING" if self._triggered else "NORMAL"

        return {
            "breaker_triggered":   self._triggered,
            "spread_ratio":        round(ratio, 2),
            "current_spread_pct":  round(current_spread * 100, 4),
            "avg_spread_pct":      round(avg_spread * 100, 4),
            "std_spread_pct":      round(std_spread * 100, 5),
            "trigger_threshold":   self.trigger_ratio,
            "action":              action,
            "reason": (
                f"⚡ 点差扩大至正常值的 {ratio:.1f}x — 断路器激活！"
                if self._triggered else
                f"点差正常（{ratio:.1f}x均值）"
            ),
        }

    @property
    def is_triggered(self) -> bool:
        return self._triggered


# ══════════════════════════════════════════════════════════════════════════════
#  附加1：时间断路器
# ══════════════════════════════════════════════════════════════════════════════

class TemporalCutoff:
    """
    高杠杆时间断路器。

    规则：
      ① 30秒不盈利规则：进场后30秒价格没有产生盈利波动 → 强制离场
         判断失误或进入震荡，不参与任何不确定的磨损
      ② 最大持仓时间：不同杠杆对应不同最大持仓秒数
         100x → 15min = 900s
         150x → 10min = 600s
         200x → 8min  = 480s

    为什么是30秒？
      脉冲行情在入场后头几秒最猛。如果30秒后还在0附近波动，
      说明趋势没有启动，这笔交易的论文已经失效。
    """

    LEVERAGE_MAX_HOLD: Dict[int, int] = {
        50:  1800,   # 30min
        100: 900,    # 15min
        150: 600,    # 10min
        200: 480,    # 8min
    }

    def __init__(
        self,
        breakeven_window_secs: float = 30.0,
        min_profit_pct:        float = 0.0001,  # 0.01% 算作盈利
    ):
        self.bw_secs      = breakeven_window_secs
        self.min_profit   = min_profit_pct
        self._entry_time:  Optional[float] = None
        self._entry_price: float = 0.0
        self._direction:   int   = 0
        self._leverage:    int   = 100

    def on_entry(self, price: float, direction: int, leverage: int = 100):
        """开仓时调用。"""
        self._entry_time  = time.time()
        self._entry_price = float(price)
        self._direction   = direction
        self._leverage    = leverage

    def evaluate(self, current_price: float) -> RiskSignal:
        """每次价格更新时调用，判断是否触发时间断路。"""
        if self._entry_time is None:
            return RiskSignal(False, "HOLD", "TemporalCutoff", "无持仓")

        elapsed = time.time() - self._entry_time

        pnl_pct = ((current_price - self._entry_price)
                   / self._entry_price * self._direction)

        # 最大持仓时间检查
        lev_key    = min(self.LEVERAGE_MAX_HOLD.keys(),
                         key=lambda k: abs(k - self._leverage))
        max_hold_s = self.LEVERAGE_MAX_HOLD[lev_key]

        if elapsed >= max_hold_s:
            return RiskSignal(
                should_exit = True,
                urgency     = "IMMEDIATE",
                source      = "TemporalCutoff",
                reason      = (f"⏰ 超过最大持仓时间 {max_hold_s}s"
                               f"（{self._leverage}x杠杆），PnL={pnl_pct*100:.2f}%"),
                pnl_pct     = pnl_pct,
                detail      = {"elapsed_secs": round(elapsed, 1),
                               "max_hold_secs": max_hold_s},
            )

        # 30秒不盈利检查
        if elapsed >= self.bw_secs and pnl_pct < self.min_profit:
            return RiskSignal(
                should_exit = True,
                urgency     = "SOON",
                source      = "TemporalCutoff",
                reason      = (f"⏱ {self.bw_secs:.0f}秒内无盈利"
                               f"（PnL={pnl_pct*100:.3f}%），判断失误或进入震荡"),
                pnl_pct     = pnl_pct,
                detail      = {"elapsed_secs": round(elapsed, 1),
                               "breakeven_window_secs": self.bw_secs},
            )

        return RiskSignal(
            should_exit = False,
            urgency     = "HOLD",
            source      = "TemporalCutoff",
            reason      = f"持仓正常，已过去 {elapsed:.0f}s，PnL={pnl_pct*100:.3f}%",
            pnl_pct     = pnl_pct,
            detail      = {"elapsed_secs": round(elapsed, 1)},
        )

    def reset(self):
        self._entry_time  = None
        self._entry_price = 0.0
        self._direction   = 0


# ══════════════════════════════════════════════════════════════════════════════
#  附加2：跨品种联动预警
# ══════════════════════════════════════════════════════════════════════════════

class CrossAssetLinkage:
    """
    跨品种领先-滞后关系预警。

    已知市场规律（平均时间差）：
      ZN (10年期国债期货) → ES/NQ (股指期货)：领先 100-500ms
      DXY (美元指数)      → BTCUSDT：领先 200-800ms（正相关或负相关取决于环境）
      VIX 跳升            → BTC 跌落：领先 100-300ms

    逻辑：
      当国债盘口 OBI 突然极端化（< -0.70）→ 股指即将跟进下跌
      先于股指平仓，利用100-500ms时间差逃生
    """

    # 品种间相关关系配置
    LINKAGE_CONFIG: Dict[str, dict] = {
        "ZN→ES": {
            "leader":      "ZN",
            "follower":    "ES",
            "lag_ms":      300,
            "correlation": -0.85,     # ZN下跌 → ES通常跟跌
            "obi_trigger": -0.65,
            "description": "国债下跌预示股指下跌",
        },
        "ZN→NQ": {
            "leader":      "ZN",
            "follower":    "NQ",
            "lag_ms":      250,
            "correlation": -0.82,
            "obi_trigger": -0.65,
            "description": "国债下跌预示纳指下跌（科技股利率敏感）",
        },
        "VIX→BTC": {
            "leader":      "VIX",
            "follower":    "BTC",
            "lag_ms":      500,
            "correlation": -0.60,
            "obi_trigger": 30.0,      # VIX绝对值，非OBI
            "description": "VIX恐慌指数飙升预示BTC下跌",
        },
    }

    def __init__(self):
        self._leader_obi_hist: Dict[str, deque] = {}
        self._alert_active:    Dict[str, bool]  = {}

    def update_leader(self, symbol: str, obi: float):
        """更新领先品种的OBI。"""
        if symbol not in self._leader_obi_hist:
            self._leader_obi_hist[symbol] = deque(maxlen=20)
        self._leader_obi_hist[symbol].append(obi)

    def check_linkage(
        self,
        follower_symbol: str,
        follower_direction: int,
    ) -> dict:
        """
        检查是否有领先品种发出警报，预示当前持仓面临风险。

        follower_symbol:    当前持仓品种（如 "ES", "BTC"）
        follower_direction: 当前持仓方向（+1/-1）
        """
        alerts = []

        for key, cfg in self.LINKAGE_CONFIG.items():
            if cfg["follower"].upper() not in follower_symbol.upper():
                continue

            leader = cfg["leader"]
            if leader not in self._leader_obi_hist:
                continue

            hist = list(self._leader_obi_hist[leader])
            if not hist:
                continue

            leader_obi = hist[-1]
            triggered  = False
            reason     = ""

            if leader == "VIX":
                # VIX使用绝对值判断
                if leader_obi > cfg["obi_trigger"] and follower_direction == 1:
                    triggered = True
                    reason    = f"VIX={leader_obi:.1f}（>{cfg['obi_trigger']}），BTC多头风险"
            else:
                # OBI判断
                if leader_obi < cfg["obi_trigger"] and follower_direction == 1:
                    triggered = True
                    reason    = (f"{leader} OBI={leader_obi:.2f}（<{cfg['obi_trigger']}），"
                                 f"{cfg['description']}，预计{cfg['lag_ms']}ms内跟进")
                elif leader_obi > -cfg["obi_trigger"] and follower_direction == -1:
                    triggered = True
                    reason    = (f"{leader} OBI={leader_obi:.2f}，"
                                 f"空头持仓面临反弹风险")

            if triggered:
                alerts.append({
                    "linkage":       key,
                    "leader":        leader,
                    "leader_obi":    round(leader_obi, 3),
                    "expected_lag_ms": cfg["lag_ms"],
                    "reason":        reason,
                })

        if alerts:
            return {
                "cross_asset_alert": True,
                "urgency":   "IMMEDIATE" if len(alerts) >= 2 else "SOON",
                "alerts":    alerts,
                "action":    "EXIT_BEFORE_CONTAGION",
                "reason":    f"跨品种联动预警（{len(alerts)}个领先信号）: "
                             + "; ".join(a["reason"] for a in alerts),
            }

        return {
            "cross_asset_alert": False,
            "urgency":   "HOLD",
            "action":    "MONITOR",
            "reason":    "跨品种信号正常",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  综合守护系统：RiskGuardSystem
# ══════════════════════════════════════════════════════════════════════════════

class RiskGuardSystem:
    """
    三道防线综合守护系统。

    将 KalmanDynamicStop、LiquidityExhaustionDetector、VolatilityBreaker、
    TemporalCutoff、CrossAssetLinkage 组合为一个统一接口。

    典型使用流程：
        guard = RiskGuardSystem()
        guard.on_entry(entry_price=105000, direction=1, leverage=100)

        # 每次行情更新时（可以是每秒、每Tick）
        result = guard.evaluate(
            current_price=105200,
            bid=105199.5, ask=105200.5,
            bids=[(105199.5, 10.5), ...],
            asks=[(105200.5, 8.2), ...],
        )
        if result["should_exit"]:
            # 立即执行平仓
    """

    def __init__(
        self,
        spread_trigger_ratio:     float = 3.0,
        depth_exhaustion_pct:     float = 0.40,
        breakeven_window_secs:    float = 30.0,
        kalman_process_noise:     float = 1e-4,
        kalman_measurement_noise: float = 5e-4,
    ):
        self.kalman   = KalmanDynamicStop(kalman_process_noise, kalman_measurement_noise)
        self.liquidity= LiquidityExhaustionDetector(depth_exhaustion_pct)
        self.breaker  = VolatilityBreaker(spread_trigger_ratio)
        self.temporal = TemporalCutoff(breakeven_window_secs)
        self.cross    = CrossAssetLinkage()

        self._direction: int   = 0
        self._leverage:  int   = 100
        self._atr_pct:   float = 0.003

    def on_entry(
        self,
        entry_price: float,
        direction:   int,
        leverage:    int   = 100,
        atr_pct:     float = 0.003,
    ):
        """开仓时初始化所有防线。"""
        self._direction = direction
        self._leverage  = leverage
        self._atr_pct   = atr_pct
        self.temporal.on_entry(entry_price, direction, leverage)
        self.kalman._kf.reset()

    def evaluate(
        self,
        current_price: float,
        bid:           float = 0.0,
        ask:           float = 0.0,
        bids:          Optional[List[Tuple[float, float]]] = None,
        asks:          Optional[List[Tuple[float, float]]] = None,
    ) -> dict:
        """
        综合评估所有防线，返回统一结果。

        优先级：
          IMMEDIATE → 立即平仓（任一触发）
          SOON      → 尽快平仓
          MONITOR   → 继续观察
          HOLD      → 正常持仓
        """
        signals: List[RiskSignal] = []

        # 防线一：卡尔曼动态止损
        sig1 = self.kalman.evaluate_exit(
            current_price, self._direction, self._leverage, self._atr_pct)
        signals.append(sig1)

        # 防线二：流动性枯竭
        if bids and asks:
            self.liquidity.update(bids, asks)
        sig2 = self.liquidity.evaluate(self._direction)
        signals.append(sig2)

        # 防线三：波动率断路器
        if bid > 0 and ask > bid:
            breaker_result = self.breaker.evaluate(bid, ask)
            sig3 = RiskSignal(
                should_exit = breaker_result["breaker_triggered"],
                urgency     = "IMMEDIATE" if breaker_result["breaker_triggered"] else "HOLD",
                source      = "VolatilityBreaker",
                reason      = breaker_result["reason"],
                detail      = breaker_result,
            )
            signals.append(sig3)

        # 附加1：时间断路器
        sig4 = self.temporal.evaluate(current_price)
        signals.append(sig4)

        # 汇总
        urgency_rank = {"IMMEDIATE": 4, "SOON": 3, "MONITOR": 2, "HOLD": 1}
        top_signal   = max(signals, key=lambda s: urgency_rank.get(s.urgency, 0))
        any_exit     = any(s.should_exit for s in signals)

        return {
            "should_exit":      any_exit,
            "urgency":          top_signal.urgency,
            "primary_source":   top_signal.source,
            "primary_reason":   top_signal.reason,
            "all_signals":      [s.to_dict() for s in signals],
            "kalman_stop":      sig1.detail,
            "exit_count":       sum(1 for s in signals if s.should_exit),
        }

    def reset(self):
        """平仓后重置所有状态。"""
        self.temporal.reset()
        self.kalman._kf.reset()
        self._direction = 0


# ══════════════════════════════════════════════════════════════════════════════
#  HardStopController — 统一硬风控指挥官（不可被AI覆盖）
#
#  来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）
#
#  设计原则：
#    "AI可以决定是否进场，但不能决定是否保命。保命必须由硬编码强制执行。"
#
#  八个硬规则（优先级从高到低）：
#    1. 行情延迟 > 阈值       → FLATTEN:FEED_LAG
#    2. 订单簿失同步          → FLATTEN:BOOK_DESYNC
#    3. 单笔亏损 > 预算       → FLATTEN:TRADE_BUDGET
#    4. 当日亏损 > 日限       → FLATTEN:DAILY_LIMIT + 停机
#    5. 连续亏损次数 > 限制   → FLATTEN:CONSEC_LOSS + 停机
#    6. 点差爆炸              → FLATTEN:SPREAD_BLOWOUT
#    7. 深度坍塌              → FLATTEN:DEPTH_COLLAPSE
#    8. BOCPD 变化点          → FLATTEN:CHANGE_POINT 或 REDUCE_50
#
#  输入集成：KalmanFilter + LiquidityDetector + VolatilityBreaker（已有）
#             + FalseBreakoutDetector + BOCPDEngine（新增）
# ══════════════════════════════════════════════════════════════════════════════

try:
    from tools.quant_core import (
        FalseBreakoutDetector, BOCPDEngine, CorrectPositionSizer,
        ASSET_CONFIGS, AssetObservationConfig,
        AggressionRatioEngine, AggressionSnapshot,
        OBISnapshot,
    )
    _QUANT_OK = True
except ImportError:
    try:
        from quant_core import (
            FalseBreakoutDetector, BOCPDEngine, CorrectPositionSizer,
            ASSET_CONFIGS, AssetObservationConfig,
            AggressionRatioEngine, AggressionSnapshot,
            OBISnapshot,
        )
        _QUANT_OK = True
    except ImportError:
        _QUANT_OK = False


@dataclass
class HardStopDecision:
    """统一硬风控决策输出。"""
    action:          str    # "HOLD" / "REDUCE_50" / "FLATTEN"
    reason:          str    # 触发原因代码
    message:         str    # 人类可读说明
    shutdown_today:  bool   # 是否关闭今天所有交易
    allow_new_entry: bool   # 是否允许新开仓


@dataclass
class AccountRiskState:
    """账户级风险状态，需要从外部持续更新。"""
    equity:            float = 100000.0   # 当前净值
    daily_pnl_pct:     float = 0.0        # 当日盈亏比例（负数=亏损）
    consec_losses:     int   = 0          # 连续亏损次数
    active_position:   int   = 0          # +1=多 -1=空 0=无仓
    position_pnl_pct:  float = 0.0        # 当前持仓浮盈亏（%）
    feed_lag_ms:       float = 0.0        # 当前行情延迟（ms）
    book_desync:       bool  = False      # 订单簿是否失同步
    ack_timeout_count: int   = 0          # 本日订单回报超时次数


class HardStopController:
    """
    统一硬风控指挥官 — 所有交易指令必须先通过此控制器。

    使用方法（每个事件循环调用一次 check()）：
        controller = HardStopController(
            asset_class="CRYPTO",
            max_loss_pct=0.01,         # 单笔最大1%
            daily_loss_limit_pct=0.015, # 日亏损1.5%停机
            max_consec_losses=3,
        )

        # 每个tick/bar更新状态
        state = AccountRiskState(
            equity=50000,
            daily_pnl_pct=-0.005,
            consec_losses=1,
            active_position=1,
            position_pnl_pct=-0.003,
            feed_lag_ms=120.0,
        )

        # 可选：更新假突破检测和BOCPD
        fbs_result = controller.fbs.evaluate(...)
        bocpd_result = controller.bocpd.update(fragility_score)

        decision = controller.check(state, fbs_result, bocpd_result)
        if decision.action != "HOLD":
            # 执行平仓指令
            broker.close_all()
    """

    def __init__(
        self,
        asset_class:          str   = "CRYPTO",
        max_loss_pct:         float = 0.01,    # 单笔1%（用户确认）
        daily_loss_limit_pct: float = 0.015,   # 日亏1.5%停机
        max_consec_losses:    int   = 3,        # 连亏3次停机
        max_ack_timeouts:     int   = 5,        # 超时次数上限
    ):
        cfg = ASSET_CONFIGS.get(asset_class, ASSET_CONFIGS["CRYPTO"]) if _QUANT_OK else None

        self.asset_class         = asset_class
        self.max_loss_pct        = max_loss_pct
        self.daily_loss_limit    = daily_loss_limit_pct
        self.max_consec_losses   = max_consec_losses
        self.max_ack_timeouts    = max_ack_timeouts
        self.cfg                 = cfg

        # 今日停机标志（连亏或日限触发后置True，不可通过API覆盖）
        self._shutdown_today     = False

        # 子模块（如果quant_core可用）
        if _QUANT_OK and cfg:
            self.fbs   = FalseBreakoutDetector(cfg=cfg)
            self.bocpd = BOCPDEngine(
                hazard=1.0 / 200.0 if asset_class == "CRYPTO" else 1.0 / 350.0)
        else:
            self.fbs   = None
            self.bocpd = None

    # ── 主检查函数 ────────────────────────────────────────────────────────────
    def check(
        self,
        state:       AccountRiskState,
        fbs_result:  Optional[object] = None,   # FalseBreakoutResult
        bocpd_result: Optional[object] = None,  # BOCPDResult
    ) -> HardStopDecision:
        """
        综合检查所有硬风控条件。

        检查顺序（优先级从高到低）：
          1. 行情延迟
          2. 订单簿失同步
          3. 今日停机标志
          4. 当日亏损上限
          5. 连续亏损次数
          6. 单笔亏损预算
          7. 点差爆炸（来自 FalseBreakoutResult）
          8. 深度坍塌
          9. BOCPD 变化点
        """
        cfg = self.cfg

        # ── 规则1：行情延迟 ──────────────────────────────────────────────────
        if cfg and state.feed_lag_ms > cfg.feed_lag_block_ms:
            return HardStopDecision(
                action="FLATTEN",
                reason="FEED_LAG",
                message=f"行情延迟{state.feed_lag_ms:.0f}ms > 阈值{cfg.feed_lag_block_ms:.0f}ms",
                shutdown_today=False,
                allow_new_entry=False,
            )

        # ── 规则2：订单簿失同步 ──────────────────────────────────────────────
        if state.book_desync:
            return HardStopDecision(
                action="FLATTEN",
                reason="BOOK_DESYNC",
                message="订单簿序列号失同步，立即平仓并重建book",
                shutdown_today=False,
                allow_new_entry=False,
            )

        # ── 规则3：今日停机 ──────────────────────────────────────────────────
        if self._shutdown_today:
            return HardStopDecision(
                action="FLATTEN" if state.active_position != 0 else "HOLD",
                reason="SHUTDOWN_TODAY",
                message="今日已触发停机条件，不允许新交易",
                shutdown_today=True,
                allow_new_entry=False,
            )

        # ── 规则4：当日亏损上限 ──────────────────────────────────────────────
        if state.daily_pnl_pct <= -self.daily_loss_limit:
            self._shutdown_today = True
            return HardStopDecision(
                action="FLATTEN",
                reason="DAILY_LIMIT",
                message=(f"当日亏损{abs(state.daily_pnl_pct)*100:.2f}%"
                         f" ≥ 上限{self.daily_loss_limit*100:.2f}%，今日停机"),
                shutdown_today=True,
                allow_new_entry=False,
            )

        # ── 规则5：连续亏损 ──────────────────────────────────────────────────
        if state.consec_losses >= self.max_consec_losses:
            self._shutdown_today = True
            return HardStopDecision(
                action="FLATTEN",
                reason="CONSEC_LOSS",
                message=(f"连续亏损{state.consec_losses}次 ≥ 上限{self.max_consec_losses}次，今日停机"),
                shutdown_today=True,
                allow_new_entry=False,
            )

        # ── 规则6：单笔亏损预算 ──────────────────────────────────────────────
        if (state.active_position != 0
                and state.position_pnl_pct <= -self.max_loss_pct):
            return HardStopDecision(
                action="FLATTEN",
                reason="TRADE_BUDGET",
                message=(f"单笔亏损{abs(state.position_pnl_pct)*100:.2f}%"
                         f" ≥ 上限{self.max_loss_pct*100:.2f}%"),
                shutdown_today=False,
                allow_new_entry=True,
            )

        # ── 规则7/8：假突破信号 ──────────────────────────────────────────────
        if fbs_result is not None:
            if getattr(fbs_result, "force_exit", False):
                sigs = getattr(fbs_result, "triggered_signals", [])
                return HardStopDecision(
                    action="FLATTEN",
                    reason="FALSE_BREAKOUT",
                    message=f"假突破评分≥3，信号: {', '.join(sigs)}",
                    shutdown_today=False,
                    allow_new_entry=False,
                )
            elif getattr(fbs_result, "block_entry", False):
                return HardStopDecision(
                    action="HOLD",
                    reason="FALSE_BREAKOUT_BLOCK",
                    message="假突破评分≥2，禁止新入场",
                    shutdown_today=False,
                    allow_new_entry=False,
                )

        # ── 规则9：BOCPD 变化点 ──────────────────────────────────────────────
        if bocpd_result is not None:
            action_bocpd = getattr(bocpd_result, "action", "HOLD")
            if action_bocpd == "FLATTEN":
                return HardStopDecision(
                    action="FLATTEN",
                    reason="CHANGE_POINT",
                    message=(f"BOCPD变化点概率{getattr(bocpd_result,'cp_prob',0):.2f}"
                             f" ≥ {self.bocpd.th_exit if self.bocpd else 0.65:.2f}"),
                    shutdown_today=False,
                    allow_new_entry=False,
                )
            elif action_bocpd == "REDUCE_50":
                return HardStopDecision(
                    action="REDUCE_50",
                    reason="CHANGE_POINT_FRAGILE",
                    message=(f"BOCPD脆弱状态，概率{getattr(bocpd_result,'cp_prob',0):.2f}，建议减仓50%"),
                    shutdown_today=False,
                    allow_new_entry=False,
                )

        # ── 通过所有检查 ──────────────────────────────────────────────────────
        return HardStopDecision(
            action="HOLD",
            reason="OK",
            message="所有风控检查通过",
            shutdown_today=False,
            allow_new_entry=True,
        )

    def reset_daily(self) -> None:
        """每日开始时重置（勿在盘中调用）。"""
        self._shutdown_today = False

    def force_shutdown(self) -> None:
        """手动触发今日停机（供外部紧急调用）。"""
        self._shutdown_today = True

