"""
tools/quant_core.py
═══════════════════════════════════════════════════════════════════════════════
量化底层数学引擎 — 所有 CrewAI 交易 Agent 的共享底层逻辑

模块：
  OBIEngine         盘口失衡率（Order Book Imbalance）
  HawkesEngine      成交密度 / 动能衰竭检测
  GARCHEngine       波动率状态机 GARCH(1,1)
  CrossAssetLeader  跨资产领先指标（国债→股指→BTC）
  SignalGate        信号门控（AND 门）
  CrowdingDetector  交易拥挤度检测（防止被反向收割）
  MultiFactorEngine 多因子组合打分（动量/价值/质量/波动率）
  StrategyEvaluator 策略有效性评估（过拟合/回撤修复/胜率盈亏比）

原则：
  - 零外部依赖（只用 numpy），可在任何环境运行
  - 每个引擎输出结构化信号，直接喂给 CrewAI Agent
  - 所有阈值均有文献/实盘来源，不靠直觉猜测
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np


# ══════════════════════════════════════════════════════════════════════════════
#  1. OBI Engine — 盘口失衡率
#
#  公式（5档加权）：
#    OBI = (Σ bid_vol_i·w_i − Σ ask_vol_i·w_i) / (Σ bid_vol_i·w_i + Σ ask_vol_i·w_i)
#    w_i = 1/(i+1)   距离越近权重越大
#
#  范围 [−1, +1]：
#    +1.0 ~ +0.6   强力买盘支撑 → 多头有利
#     0.0 附近     均衡 / 震荡
#    −0.6 ~ −1.0  强力卖盘压制 → 空头有利
#
#  退出信号（最重要）：
#    OBI 从 > 0.5 在 100ms 内跌至 < 0.1 → 支撑坍塌，提前平多
#    OBI 斜率（slope）< −0.4 且持有多仓 → 撤退
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OBISnapshot:
    ts:            float
    obi:           float          # 当前 OBI [-1, +1]
    obi_slope:     float          # OBI 变化速率（per second）
    bid_depth:     float          # 5档买盘总量（USD名义）
    ask_depth:     float          # 5档卖盘总量
    depth_ratio:   float          # bid_depth / ask_depth
    exit_signal:   bool           # True = 应立即撤退
    entry_allowed: bool           # True = 盘口支持开仓


class OBIEngine:
    """
    盘口失衡率引擎。

    输入：Level 2 订单簿（bids/asks 各 N 档）
    输出：OBISnapshot，含退出信号

    参数：
      levels      监控档位数（推荐 5，更多对高频毫秒级太慢）
      exit_slope  OBI 斜率触发阈值（推荐 -0.4/s，即每秒跌0.4）
      exit_obi    OBI 绝对值触发阈值（跌破此值且持多仓 → 退出）
      history_len OBI 历史窗口（用于计算斜率）
    """

    def __init__(
        self,
        levels:     int   = 5,
        exit_slope: float = -0.4,    # OBI斜率阈值（/s）
        exit_obi:   float = 0.1,     # OBI绝对值下限
        entry_obi:  float = 0.3,     # 开多需要 OBI > 0.3
        history_len: int  = 20,
    ):
        self.levels     = levels
        self.exit_slope = exit_slope
        self.exit_obi   = exit_obi
        self.entry_obi  = entry_obi
        self._history   = deque(maxlen=history_len)   # (ts, obi)

    def update(
        self,
        bids: List[Tuple[float, float]],   # [(price, volume), ...]
        asks: List[Tuple[float, float]],
        current_position: int = 0,         # +1=多 -1=空 0=无
    ) -> OBISnapshot:
        """
        计算最新 OBI 快照。

        Args:
            bids: 买盘 [(price, volume), ...] 从高到低排列
            asks: 卖盘 [(price, volume), ...] 从低到高排列
            current_position: 当前持仓方向

        Returns:
            OBISnapshot
        """
        n = min(self.levels, len(bids), len(asks))

        bid_wsum = ask_wsum = 0.0
        bid_total = ask_total = 0.0

        for i in range(n):
            w = 1.0 / (i + 1)
            b_vol = bids[i][1] if i < len(bids) else 0.0
            a_vol = asks[i][1] if i < len(asks) else 0.0

            bid_wsum  += b_vol * w
            ask_wsum  += a_vol * w
            bid_total += b_vol * bids[i][0] if i < len(bids) else 0.0
            ask_total += a_vol * asks[i][0] if i < len(asks) else 0.0

        denom = bid_wsum + ask_wsum
        obi   = (bid_wsum - ask_wsum) / denom if denom > 0 else 0.0
        ts    = time.time()

        # OBI 斜率（线性回归最近 N 点）
        self._history.append((ts, obi))
        slope = self._compute_slope()

        # 退出信号
        exit_signal = False
        if current_position == 1:    # 持多仓
            exit_signal = (obi < self.exit_obi) or (slope < self.exit_slope)
        elif current_position == -1: # 持空仓
            exit_signal = (obi > -self.exit_obi) or (slope > -self.exit_slope)

        # 开仓许可
        entry_allowed = abs(obi) >= self.entry_obi

        depth_ratio = (bid_total / ask_total) if ask_total > 0 else 1.0

        return OBISnapshot(
            ts=ts, obi=obi, obi_slope=slope,
            bid_depth=bid_total, ask_depth=ask_total,
            depth_ratio=depth_ratio,
            exit_signal=exit_signal,
            entry_allowed=entry_allowed,
        )

    def _compute_slope(self) -> float:
        """OBI 对时间的线性回归斜率（per second）"""
        if len(self._history) < 3:
            return 0.0
        ts_arr  = np.array([h[0] for h in self._history])
        obi_arr = np.array([h[1] for h in self._history])
        ts_arr  -= ts_arr[0]   # 归零
        if ts_arr[-1] < 1e-9:
            return 0.0
        # 最小二乘斜率
        cov = np.cov(ts_arr, obi_arr)
        var = np.var(ts_arr)
        return float(cov[0, 1] / var) if var > 1e-9 else 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  2. Hawkes Engine — 成交密度 / 动能衰竭检测
#
#  霍克斯过程（简化自激泊松过程）：
#    λ(t) = μ + α · Σ exp(−β(t − t_i))   (t_i = 过去成交时间)
#
#  经济含义：
#    λ(t) 上升 → 成交在加速 → 趋势启动或延续
#    λ(t) 下降（价格仍涨）→ 动能衰竭 → 提前退出信号
#
#  实盘规则（高杠杆用）：
#    进场条件：λ(t) / λ_baseline > 2.0（成交密度高于基准2倍）
#    退出条件：λ(t) / λ_peak < 0.5（从峰值跌落超50%）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HawkesSnapshot:
    ts:            float
    intensity:     float      # λ(t) 当前成交密度
    intensity_ma:  float      # 基准强度（移动平均）
    ratio:         float      # intensity / intensity_ma
    peak:          float      # 本仓周期内的峰值强度
    peak_ratio:    float      # intensity / peak（衰竭检测）
    exhaustion:    bool       # True = 动能衰竭，建议退出
    acceleration:  bool       # True = 成交加速，支持进场


class HawkesEngine:
    """
    成交密度引擎（Hawkes Process 简化版）。

    输入：每根 K 线的成交笔数（trades count）或 tick 时间戳列表
    输出：HawkesSnapshot，含动能衰竭信号

    参数：
      decay         自激衰减系数（越大 = 记忆越短，0.1~0.5 推荐）
      entry_ratio   进场倍率阈值（intensity 高于基准多少倍才支持进场）
      exhaust_ratio 衰竭检测阈值（从峰值跌落多少算衰竭）
      window        基准均值窗口（K线数）
    """

    def __init__(
        self,
        decay:        float = 0.3,
        entry_ratio:  float = 1.8,
        exhaust_ratio: float = 0.5,
        window:       int   = 20,
    ):
        self.decay        = decay
        self.entry_ratio  = entry_ratio
        self.exhaust_ratio= exhaust_ratio
        self._intensity_history = deque(maxlen=window * 3)
        self._peak        = 0.0
        self._in_position = False

    def update(
        self,
        trade_count: float,          # 本根K线成交笔数
        bar_seconds: float = 600,    # K线周期秒数（10min=600）
        in_position: bool  = False,
    ) -> HawkesSnapshot:
        """
        用 K 线成交笔数近似 Hawkes intensity。

        λ(t) ≈ trade_count / bar_seconds（成交笔数/秒）
        """
        ts = time.time()
        lambda_t = trade_count / max(bar_seconds, 1)

        # 指数衰减平滑（模拟 Hawkes 记忆效应）
        if self._intensity_history:
            prev = self._intensity_history[-1]
            lambda_smooth = (1 - self.decay) * prev + self.decay * lambda_t
        else:
            lambda_smooth = lambda_t

        self._intensity_history.append(lambda_smooth)

        # 基准（移动平均）
        arr = np.array(self._intensity_history)
        intensity_ma = float(arr.mean()) if len(arr) > 0 else lambda_smooth

        ratio = lambda_smooth / intensity_ma if intensity_ma > 1e-9 else 1.0

        # 仓位内峰值跟踪
        if in_position and not self._in_position:
            self._peak = lambda_smooth   # 新开仓，重置峰值
        if in_position:
            self._peak = max(self._peak, lambda_smooth)
        else:
            self._peak = 0.0
        self._in_position = in_position

        peak_ratio = lambda_smooth / self._peak if self._peak > 1e-9 else 1.0

        exhaustion   = in_position and (peak_ratio < self.exhaust_ratio)
        acceleration = ratio > self.entry_ratio

        return HawkesSnapshot(
            ts=ts,
            intensity=lambda_smooth,
            intensity_ma=intensity_ma,
            ratio=ratio,
            peak=self._peak,
            peak_ratio=peak_ratio,
            exhaustion=exhaustion,
            acceleration=acceleration,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  3. GARCH Engine — 波动率状态机
#
#  GARCH(1,1)：σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}
#
#  参数（BTC 10分钟K线实证值）：
#    ω ≈ 0.000001   长期均值项
#    α ≈ 0.10       ARCH 项（冲击敏感度）
#    β ≈ 0.85       GARCH 项（波动持续性）
#    α + β < 1 确保均值回归
#
#  波动率状态机：
#    CALM      σ_t  < 0.005   （<0.5%/10min）正常开仓
#    NORMAL    σ_t  < 0.010   正常开仓
#    ELEVATED  σ_t  < 0.020   减半仓位
#    EXTREME   σ_t >= 0.020   禁止开仓，触发断路器
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GARCHSnapshot:
    ts:          float
    sigma:       float        # 预测波动率（下根K线）
    sigma_ann:   float        # 年化波动率（参考用）
    regime:      str          # CALM / NORMAL / ELEVATED / EXTREME
    size_mult:   float        # 建议仓位乘数（1.0/0.5/0.25/0.0）
    circuit_break: bool       # True = 禁止开仓


class GARCHEngine:
    """
    GARCH(1,1) 波动率预测引擎。

    输入：收益率序列（10分钟K线 pct_change）
    输出：GARCHSnapshot

    参数推荐（BTC 10min）：
      omega = 1e-6
      alpha = 0.10
      beta  = 0.85
    """

    BARS_PER_YEAR = 365 * 24 * 6   # 10分钟K线：52560根/年

    REGIMES = [
        ("CALM",     0.005, 1.0),
        ("NORMAL",   0.010, 1.0),
        ("ELEVATED", 0.020, 0.5),
        ("EXTREME",  999.0, 0.0),
    ]

    def __init__(
        self,
        omega: float = 1e-6,
        alpha: float = 0.10,
        beta:  float = 0.85,
        min_bars: int = 30,
    ):
        assert alpha + beta < 1.0, "GARCH: α+β 必须 < 1（平稳性条件）"
        self.omega    = omega
        self.alpha    = alpha
        self.beta     = beta
        self.min_bars = min_bars
        self._sigma2  = omega / (1 - alpha - beta)  # 初始化为长期均值
        self._initialized = False

    def update(self, returns: np.ndarray) -> GARCHSnapshot:
        """
        用收益率序列更新 GARCH 状态，返回下一根 K 线的预测波动率。

        Args:
            returns: 最近 N 根K线的收益率（pct_change，如 0.002 表示 0.2%）

        Returns:
            GARCHSnapshot
        """
        if len(returns) < self.min_bars:
            # 数据不足，用历史标准差兜底
            sigma = float(np.std(returns)) if len(returns) > 1 else 0.01
            self._sigma2 = sigma ** 2
        else:
            # 递推 GARCH(1,1)
            sigma2 = self._sigma2
            for r in returns[-self.min_bars:]:
                sigma2 = (self.omega
                          + self.alpha * r ** 2
                          + self.beta  * sigma2)
            self._sigma2 = sigma2
            self._initialized = True

        sigma     = math.sqrt(max(self._sigma2, 1e-12))
        sigma_ann = sigma * math.sqrt(self.BARS_PER_YEAR)

        # 状态分类
        regime   = "EXTREME"
        size_mult = 0.0
        for name, threshold, mult in self.REGIMES:
            if sigma < threshold:
                regime    = name
                size_mult = mult
                break

        circuit_break = (regime == "EXTREME")

        return GARCHSnapshot(
            ts=time.time(),
            sigma=round(sigma, 6),
            sigma_ann=round(sigma_ann, 4),
            regime=regime,
            size_mult=size_mult,
            circuit_break=circuit_break,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  4. Cross-Asset Leader — 跨资产领先指标
#
#  经济逻辑：
#    国债收益率（10yr）→ 股指期货 → BTC（风险资产传导链）
#    当国债收益率快速下跌 → Risk-off → BTC 通常跟随下跌
#    当国债收益率温和下跌 → 流动性宽松 → BTC 可能上涨
#
#  领先时差（实证）：
#    债→股：约 5-30 分钟
#    债/股→BTC：约 15-60 分钟（BTC 反应慢于传统市场）
#
#  信号：
#    RISK_ON   → 流动性改善，支持做多 BTC
#    RISK_OFF  → 避险情绪，支持做空 BTC
#    NEUTRAL   → 无明确宏观偏向
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CrossAssetSignal:
    ts:           float
    bond_yield_chg: float    # 国债收益率变化（bps，过去1小时）
    spx_chg:      float      # 标普500变化（%，过去1小时）
    vix_level:    float      # VIX 水平（>30 = 极度恐慌）
    macro_bias:   str        # "RISK_ON" / "RISK_OFF" / "NEUTRAL"
    btc_bias:     str        # 对 BTC 的影响："BULLISH"/"BEARISH"/"NEUTRAL"
    confidence:   float      # 0.0 ~ 1.0


class CrossAssetLeader:
    """
    跨资产领先指标引擎。

    数据来源：
      - 国债收益率：通过 FRED API 或 yfinance (^TNX)
      - 股指：yfinance (^GSPC / ES=F)
      - VIX：yfinance (^VIX)

    设计选择：
      独立计算，不依赖实时行情推送；
      10分钟刷新一次，给 LSTM 信号提供宏观过滤层。
    """

    def evaluate(
        self,
        bond_yield_chg_1h: float,  # 1小时收益率变化（bps）
        spx_chg_1h:        float,  # 1小时标普变化（%）
        vix:               float,  # 当前 VIX
    ) -> CrossAssetSignal:
        """
        规则引擎（可替换为 ML 模型）：

        规则优先级：
          1. VIX > 35 → 强 RISK_OFF，BTC 熊
          2. 债券快速下跌（>5bps/h）+ SPX < -0.3% → RISK_OFF
          3. 债券温和下跌（<-2bps/h）+ SPX > 0 → RISK_ON
          4. 其他 → NEUTRAL
        """
        ts = time.time()

        # 极端恐慌
        if vix > 35:
            return CrossAssetSignal(
                ts=ts,
                bond_yield_chg=bond_yield_chg_1h,
                spx_chg=spx_chg_1h,
                vix_level=vix,
                macro_bias="RISK_OFF",
                btc_bias="BEARISH",
                confidence=0.85,
            )

        # Risk-off 判定
        if bond_yield_chg_1h > 5 and spx_chg_1h < -0.3:
            # 利率快速上升 + 股市下跌 → 流动性收紧
            return CrossAssetSignal(ts=ts,
                bond_yield_chg=bond_yield_chg_1h,
                spx_chg=spx_chg_1h, vix_level=vix,
                macro_bias="RISK_OFF", btc_bias="BEARISH",
                confidence=0.7)

        if bond_yield_chg_1h < -5 and spx_chg_1h < -0.5:
            # 利率下降 + 股市暴跌 → 避险买债，BTC 承压
            return CrossAssetSignal(ts=ts,
                bond_yield_chg=bond_yield_chg_1h,
                spx_chg=spx_chg_1h, vix_level=vix,
                macro_bias="RISK_OFF", btc_bias="BEARISH",
                confidence=0.65)

        # Risk-on 判定
        if bond_yield_chg_1h < -2 and spx_chg_1h > 0.2:
            # 利率温和下降 + 股市上涨 → 流动性宽松
            return CrossAssetSignal(ts=ts,
                bond_yield_chg=bond_yield_chg_1h,
                spx_chg=spx_chg_1h, vix_level=vix,
                macro_bias="RISK_ON", btc_bias="BULLISH",
                confidence=0.6)

        # Neutral
        return CrossAssetSignal(ts=ts,
            bond_yield_chg=bond_yield_chg_1h,
            spx_chg=spx_chg_1h, vix_level=vix,
            macro_bias="NEUTRAL", btc_bias="NEUTRAL",
            confidence=0.4)


# ══════════════════════════════════════════════════════════════════════════════
#  5. Signal Gate — 最终信号门控
#
#  所有引擎输出必须通过此门控才能执行
#  逻辑：AND 条件（所有条件必须同时满足）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GateResult:
    approved:     bool
    final_size_mult: float    # 综合仓位乘数
    reject_reasons: List[str] = field(default_factory=list)
    approve_reasons: List[str] = field(default_factory=list)


class SignalGate:
    """
    信号门控：整合 LSTM + OBI + Hawkes + GARCH + CrossAsset

    通过条件（所有必须满足）：
      1. LSTM 信号可信（confidence >= threshold）
      2. OBI 支持方向（方向一致且不在退出信号中）
      3. Hawkes 成交加速（动能确认）
      4. GARCH 波动率非极端（非 EXTREME）
      5. CrossAsset 宏观无强烈对立信号

    仓位乘数 = GARCH.size_mult × CrossAsset 修正
    """

    def evaluate(
        self,
        lstm_direction:   int,     # +1 / -1
        lstm_confidence:  float,
        lstm_threshold:   float,
        obi:              OBISnapshot,
        hawkes:           HawkesSnapshot,
        garch:            GARCHSnapshot,
        cross_asset:      CrossAssetSignal,
    ) -> GateResult:

        rejects  = []
        approves = []

        # ── 条件1: LSTM ────────────────────────────────────────────
        if lstm_confidence < lstm_threshold:
            rejects.append(f"LSTM置信度不足 {lstm_confidence:.3f}<{lstm_threshold}")
        else:
            approves.append(f"LSTM: {lstm_direction:+d} conf={lstm_confidence:.3f}")

        # ── 条件2: OBI ─────────────────────────────────────────────
        if obi.exit_signal:
            rejects.append(f"OBI退出信号: obi={obi.obi:.3f} slope={obi.obi_slope:.3f}")
        elif not obi.entry_allowed:
            rejects.append(f"OBI不支持进场: {obi.obi:.3f}")
        else:
            # OBI 方向一致性检查
            obi_direction_ok = (
                (lstm_direction == 1  and obi.obi > 0) or
                (lstm_direction == -1 and obi.obi < 0)
            )
            if not obi_direction_ok:
                rejects.append(f"OBI方向不一致: lstm={lstm_direction} obi={obi.obi:.3f}")
            else:
                approves.append(f"OBI: {obi.obi:.3f}")

        # ── 条件3: Hawkes ───────────────────────────────────────────
        if hawkes.exhaustion:
            rejects.append(f"Hawkes动能衰竭: ratio={hawkes.peak_ratio:.2f}")
        # Hawkes 加速是加分项，不是硬性要求
        if hawkes.acceleration:
            approves.append(f"Hawkes加速: ×{hawkes.ratio:.1f}")

        # ── 条件4: GARCH ────────────────────────────────────────────
        if garch.circuit_break:
            rejects.append(f"GARCH断路器: σ={garch.sigma:.4f} EXTREME")
        else:
            approves.append(f"GARCH: {garch.regime} σ={garch.sigma:.4f}")

        # ── 条件5: CrossAsset ───────────────────────────────────────
        macro_conflict = (
            (lstm_direction == 1  and cross_asset.btc_bias == "BEARISH"
             and cross_asset.confidence > 0.7) or
            (lstm_direction == -1 and cross_asset.btc_bias == "BULLISH"
             and cross_asset.confidence > 0.7)
        )
        if macro_conflict:
            rejects.append(
                f"宏观对立: btc_bias={cross_asset.btc_bias} "
                f"conf={cross_asset.confidence:.2f}")
        else:
            approves.append(f"宏观: {cross_asset.macro_bias}")

        # ── 最终决策 ────────────────────────────────────────────────
        approved = len(rejects) == 0

        # 综合仓位乘数
        ca_mult = 0.7 if cross_asset.macro_bias == "RISK_OFF" else 1.0
        if cross_asset.macro_bias == "RISK_ON" and lstm_direction == 1:
            ca_mult = 1.2   # 顺风加成（最多20%）
        final_mult = garch.size_mult * ca_mult

        return GateResult(
            approved=approved,
            final_size_mult=round(final_mult, 2),
            reject_reasons=rejects,
            approve_reasons=approves,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  6. CrowdingDetector — 交易拥挤度检测
#
#  核心认知：当一个信号被太多人使用（如布林带、RSI超买），
#  市场会提前消化甚至反向收割这批仓位。
#
#  检测方法：
#    计算我们的 LSTM 信号与5个常用技术指标的方向一致性。
#    一致程度越高 → 拥挤度越高 → 被反向收割风险越大。
#
#  指标集（业内最常用的"拥挤策略"）：
#    1. Bollinger Band 突破（最拥挤的策略之一）
#    2. RSI 超买超卖（RSI>70做空 / RSI<30做多）
#    3. MACD 金叉死叉
#    4. EMA 20/50 交叉
#    5. 布林带中轨均值回归
#
#  输出：
#    crowding_score  0.0~1.0（1.0=完全拥挤，0.0=独特信号）
#    is_crowded      crowding_score > 0.6 → 警告
#    size_penalty    仓位折扣（拥挤时减仓）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CrowdingSnapshot:
    crowding_score:    float      # 0.0 ~ 1.0
    is_crowded:        bool
    size_penalty:      float      # 仓位折扣系数 (0.5~1.0)
    agreements:        List[str]  # 与我们信号一致的指标
    contrarians:       List[str]  # 与我们信号相反的指标（好事）
    unique_signal:     bool       # True = 信号具有独特性，不拥挤


class CrowdingDetector:
    """
    交易拥挤度检测器。

    原理：
      若 LSTM 信号与多个大众指标方向一致 → 拥挤 → 减仓或拒绝
      若 LSTM 信号与大众指标方向相反   → 独特 → 正常仓位甚至加成

    参数：
      crowding_threshold  超过此比例的指标同向 → 判定为拥挤（推荐 0.6）
      penalty_factor      拥挤时仓位折扣（推荐 0.5）
    """

    def __init__(
        self,
        crowding_threshold: float = 0.6,
        penalty_factor:     float = 0.5,
    ):
        self.crowding_threshold = crowding_threshold
        self.penalty_factor     = penalty_factor

    def evaluate(
        self,
        closes:     np.ndarray,    # 最近N根K线收盘价（至少60根）
        direction:  int,           # LSTM 输出方向 +1/-1
    ) -> CrowdingSnapshot:
        """
        计算当前信号的拥挤度。

        Args:
            closes:    价格序列（至少60根K线）
            direction: LSTM 信号方向 +1=做多 -1=做空
        """
        if len(closes) < 60:
            return CrowdingSnapshot(
                crowding_score=0.0, is_crowded=False,
                size_penalty=1.0, agreements=[], contrarians=[],
                unique_signal=True,
            )

        signals = {}

        # ── 指标1: Bollinger Band (20, 2σ) ───────────────────────────────────
        bb_period = 20
        bb_mean = np.mean(closes[-bb_period:])
        bb_std  = np.std(closes[-bb_period:])
        price   = closes[-1]
        upper   = bb_mean + 2 * bb_std
        lower   = bb_mean - 2 * bb_std

        if price > upper:
            signals["BB突破"] = -1   # 价格突破上轨 → 均值回归 → 做空信号
        elif price < lower:
            signals["BB突破"] = +1   # 价格突破下轨 → 均值回归 → 做多信号
        else:
            signals["BB突破"] = 0    # 中性

        # ── 指标2: RSI(14) ────────────────────────────────────────────────────
        rsi = self._rsi(closes, 14)
        if rsi > 70:
            signals["RSI超卖"] = -1   # 超买 → 大众做空
        elif rsi < 30:
            signals["RSI超卖"] = +1   # 超卖 → 大众做多
        else:
            signals["RSI超卖"] = 0

        # ── 指标3: MACD (12,26,9) ─────────────────────────────────────────────
        macd_line, signal_line = self._macd(closes)
        if macd_line > signal_line and (self._macd(closes[:-1])[0] <=
                                         self._macd(closes[:-1])[1]):
            signals["MACD金叉"] = +1   # 金叉 → 大众做多
        elif macd_line < signal_line and (self._macd(closes[:-1])[0] >=
                                           self._macd(closes[:-1])[1]):
            signals["MACD死叉"] = -1   # 死叉 → 大众做空
        else:
            pass   # 无交叉信号

        # ── 指标4: EMA 20/50 交叉 ────────────────────────────────────────────
        if len(closes) >= 50:
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            prev_ema20 = self._ema(closes[:-1], 20)
            prev_ema50 = self._ema(closes[:-1], 50)
            if ema20 > ema50 and prev_ema20 <= prev_ema50:
                signals["EMA金叉"] = +1
            elif ema20 < ema50 and prev_ema20 >= prev_ema50:
                signals["EMA死叉"] = -1

        # ── 指标5: BB 中轨均值回归（最拥挤策略） ─────────────────────────────
        bb_position = (price - bb_mean) / (bb_std + 1e-9)
        if bb_position > 1.5:
            signals["BB均值回归"] = -1   # 偏离均值过高 → 做空
        elif bb_position < -1.5:
            signals["BB均值回归"] = +1   # 偏离均值过低 → 做多

        # ── 计算拥挤度 ───────────────────────────────────────────────────────
        actionable = {k: v for k, v in signals.items() if v != 0}
        if not actionable:
            return CrowdingSnapshot(
                crowding_score=0.0, is_crowded=False,
                size_penalty=1.0, agreements=[], contrarians=[],
                unique_signal=True,
            )

        agreements  = [k for k, v in actionable.items() if v == direction]
        contrarians = [k for k, v in actionable.items() if v == -direction]

        crowding_score = len(agreements) / len(actionable)
        is_crowded     = crowding_score >= self.crowding_threshold

        # 拥挤时折扣仓位；若信号独特（大众反向），给予加成
        if is_crowded:
            size_penalty = self.penalty_factor
        elif len(contrarians) > len(agreements):
            size_penalty = 1.2   # 独特信号：轻微加成（最多20%）
        else:
            size_penalty = 1.0

        return CrowdingSnapshot(
            crowding_score = round(crowding_score, 3),
            is_crowded     = is_crowded,
            size_penalty   = size_penalty,
            agreements     = agreements,
            contrarians    = contrarians,
            unique_signal  = not is_crowded,
        )

    # ── 指标计算工具 ──────────────────────────────────────────────────────────
    @staticmethod
    def _rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes[-(period + 1):])
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = gains.mean()
        avg_loss = losses.mean()
        if avg_loss < 1e-9:
            return 100.0
        rs  = avg_gain / avg_loss
        return float(100 - 100 / (1 + rs))

    @staticmethod
    def _ema(closes: np.ndarray, period: int) -> float:
        if len(closes) < period:
            return float(closes[-1])
        k   = 2.0 / (period + 1)
        ema = float(closes[-period])
        for c in closes[-period + 1:]:
            ema = c * k + ema * (1 - k)
        return ema

    @staticmethod
    def _macd(closes: np.ndarray,
              fast: int = 12, slow: int = 26, signal: int = 9
              ) -> Tuple[float, float]:
        if len(closes) < slow + signal:
            return 0.0, 0.0
        detector = CrowdingDetector
        ema_fast   = detector._ema(closes, fast)
        ema_slow   = detector._ema(closes, slow)
        macd_line  = ema_fast - ema_slow

        # signal line：MACD 的 9 日 EMA（简化：直接用最后9个MACD值的EMA）
        macd_series = []
        for i in range(signal, 0, -1):
            ef = detector._ema(closes[:-i] if i > 0 else closes, fast)
            es = detector._ema(closes[:-i] if i > 0 else closes, slow)
            macd_series.append(ef - es)
        macd_series.append(macd_line)
        signal_line = detector._ema(np.array(macd_series), signal)
        return macd_line, signal_line


# ══════════════════════════════════════════════════════════════════════════════
#  7. MultiFactorEngine — 多因子组合打分
#
#  四因子框架（来自学术文献 + 加密市场实证）：
#
#  因子1 动量（Momentum）        权重 35%
#    = sign(ret_2h) × |ret_2h| / ATR
#    逻辑：加密市场由情绪驱动，动量效应持续时间更长（数小时级别）
#
#  因子2 价值/反向（Value）       权重 25%
#    = −sign(funding_rate) × |funding_rate| / 0.001
#    逻辑：资金费率极高 → 多头过度拥挤 → 反向做空；反之做多
#    （资金费率套利是加密市场独有的"确定性"逻辑）
#
#  因子3 质量（Quality）          权重 20%
#    = sign(OI_change) × sign(price_change)  若方向一致=+1，背离=-1
#    逻辑：价格上涨 + OI增加 = 真实趋势（质量高）
#           价格上涨 + OI减少 = 空头回补（趋势脆弱，质量低）
#
#  因子4 波动率（Volatility）     权重 20%
#    = 1 − min(GARCH_sigma / 0.015, 1.0)
#    逻辑：波动率越低 → 环境越确定 → 信号质量越高
#
#  综合得分 = Σ(factor_i × weight_i)  ∈ [−1, +1]
#  最终仓位乘数 = 0.5 + 0.5 × max(综合得分, 0)  ∈ [0.5, 1.0]（做多方向）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MultiFactorScore:
    total_score:      float      # -1.0 ~ +1.0
    momentum_score:   float
    value_score:      float
    quality_score:    float
    volatility_score: float
    size_multiplier:  float      # 仓位乘数 0.5 ~ 1.2
    factor_breakdown: dict       # 详细因子分解
    signal_quality:   str        # "HIGH" / "MEDIUM" / "LOW"


class MultiFactorEngine:
    """
    四因子综合打分引擎（加密货币版）。

    加权配置（可调）：
      momentum   0.35  — 动量因子（趋势跟踪）
      value      0.25  — 价值/反向因子（资金费率套利）
      quality    0.20  — 质量因子（OI确认）
      volatility 0.20  — 波动率因子（环境质量）
    """

    WEIGHTS = {
        "momentum":   0.35,
        "value":      0.25,
        "quality":    0.20,
        "volatility": 0.20,
    }

    def score(
        self,
        ret_2h:        float,    # 2小时收益率（如 0.005=0.5%）
        atr_pct:       float,    # ATR / price（如 0.004=0.4%）
        funding_rate:  float,    # 当前资金费率（如 0.0001=0.01%/8h）
        oi_change_pct: float,    # OI 变化率（正=增仓 负=减仓）
        price_change_pct: float, # 价格变化率（同周期，用于质量判断）
        garch_sigma:   float,    # GARCH 预测波动率
        direction:     int,      # 拟开仓方向 +1/-1
    ) -> MultiFactorScore:
        """
        计算四因子综合得分。

        所有因子归一化到 [-1, +1]，从 direction 角度出发：
          +1 = 该因子支持开仓
          -1 = 该因子反对开仓
           0 = 中性
        """

        # ── 因子1: 动量 ──────────────────────────────────────────────────────
        # 动量强度 = 方向收益率 / ATR（归一化）
        if atr_pct > 1e-6:
            raw_momentum = ret_2h / atr_pct
        else:
            raw_momentum = 0.0
        # 裁剪到 [-2, +2]，再归一化到 [-1, +1]
        momentum_raw = float(np.clip(raw_momentum, -2, 2)) / 2.0
        # 从 direction 角度：方向一致得正分
        momentum_score = momentum_raw * direction

        # ── 因子2: 价值/资金费率反向 ────────────────────────────────────────
        # 资金费率归一化（0.001 = 0.1% 为极端值）
        fr_norm = float(np.clip(funding_rate / 0.001, -1, 1))
        # 高资金费率 → 多头拥挤 → 反向信号（做空获正分）
        # 所以做多时：高资金费率 = 逆风
        value_score = -fr_norm * direction   # 逆向因子

        # ── 因子3: 质量（OI × Price 方向一致性）────────────────────────────
        oi_dir    = 1 if oi_change_pct > 0.001 else (-1 if oi_change_pct < -0.001 else 0)
        price_dir = 1 if price_change_pct > 0 else (-1 if price_change_pct < 0 else 0)

        if oi_dir == 0 or price_dir == 0:
            quality_raw = 0.0
        elif oi_dir == price_dir:
            quality_raw = 1.0   # OI 与价格方向一致 → 真实趋势
        else:
            quality_raw = -0.5  # 背离 → 可疑（程度较轻）

        quality_score = quality_raw * direction * price_dir

        # ── 因子4: 波动率（低波动 = 高质量环境）────────────────────────────
        # σ 归一化：0.015（1.5%/10min）以上视为恶劣环境
        vol_norm   = float(np.clip(garch_sigma / 0.015, 0, 1))
        volatility_score = 1.0 - vol_norm   # 低波动得高分，方向无关

        # ── 综合得分 ─────────────────────────────────────────────────────────
        w = self.WEIGHTS
        total = (
            w["momentum"]   * momentum_score   +
            w["value"]      * value_score       +
            w["quality"]    * quality_score     +
            w["volatility"] * volatility_score
        )
        total = float(np.clip(total, -1, 1))

        # ── 仓位乘数 ─────────────────────────────────────────────────────────
        # 得分 > 0：支持开仓，乘数 0.5~1.2
        # 得分 < 0：反对开仓，乘数 0.0~0.5（但最终 Gate 可能拒绝）
        if total > 0:
            size_mult = 0.5 + 0.7 * total   # 0.5 ~ 1.2
        else:
            size_mult = 0.5 + 0.5 * total   # 0.0 ~ 0.5

        if total >= 0.5:
            quality = "HIGH"
        elif total >= 0.1:
            quality = "MEDIUM"
        else:
            quality = "LOW"

        return MultiFactorScore(
            total_score      = round(total, 4),
            momentum_score   = round(momentum_score,   4),
            value_score      = round(value_score,      4),
            quality_score    = round(quality_score,    4),
            volatility_score = round(volatility_score, 4),
            size_multiplier  = round(float(np.clip(size_mult, 0.0, 1.2)), 3),
            factor_breakdown = {
                "动量(35%)":   f"{momentum_score:+.3f}",
                "价值(25%)":   f"{value_score:+.3f}",
                "质量(20%)":   f"{quality_score:+.3f}",
                "波动率(20%)": f"{volatility_score:+.3f}",
                "加权合计":    f"{total:+.4f}",
            },
            signal_quality = quality,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  8. StrategyEvaluator — 策略有效性评估
#
#  三大压力测试指标：
#
#  ① 最大回撤修复期（Max Drawdown Recovery）
#     从谷底回到前高需要多少根K线？
#     > 500根（约3.5天）→ 策略失效信号
#
#  ② 胜率-盈亏比平衡点（Break-even Analysis）
#     最低胜率 = 1 / (1 + 盈亏比)
#     现实胜率 必须 > 最低胜率 才能正期望
#
#  ③ 样本内外过拟合检测（IS/OOS Ratio）
#     IS Sharpe / OOS Sharpe < 2.0 → 可接受
#     IS Sharpe / OOS Sharpe > 3.0 → 严重过拟合
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StrategyHealth:
    # 胜率盈亏比
    win_rate:          float
    avg_win:           float
    avg_loss:          float
    profit_factor:     float      # avg_win * win_rate / (avg_loss * loss_rate)
    breakeven_wr:      float      # 保本所需最低胜率
    has_edge:          bool       # 实际胜率 > 保本胜率

    # 回撤
    max_drawdown_pct:  float
    recovery_bars:     int        # 修复期（K线数）
    recovery_days:     float      # 修复期（天数，10min线）

    # 过拟合
    is_sharpe:         float      # 样本内 Sharpe
    oos_sharpe:        float      # 样本外 Sharpe
    overfit_ratio:     float      # IS/OOS（> 3 = 过拟合）
    is_overfit:        bool

    # 综合判断
    grade:             str        # "A" / "B" / "C" / "F"
    issues:            List[str]


class StrategyEvaluator:
    """
    策略有效性评估器。

    输入：完整的 equity curve（资金曲线）和交易记录
    输出：StrategyHealth，含过拟合检测和有效性评分
    """

    def evaluate(
        self,
        equity_curve:  np.ndarray,   # 每根K线的净值序列
        trade_returns: np.ndarray,   # 每笔交易的收益率序列
        is_split:      float = 0.7,  # 样本内/外分割比例
    ) -> StrategyHealth:
        """
        全面评估策略健康度。

        Args:
            equity_curve:  净值曲线（如 [10000, 10050, 9980, ...]）
            trade_returns: 每笔交易收益率（如 [0.012, -0.003, 0.008, ...]）
            is_split:      前70%为样本内
        """
        issues = []

        # ── 胜率盈亏比 ────────────────────────────────────────────────────────
        wins   = trade_returns[trade_returns > 0]
        losses = trade_returns[trade_returns < 0]

        win_rate   = len(wins) / len(trade_returns) if len(trade_returns) > 0 else 0.0
        avg_win    = float(wins.mean())    if len(wins) > 0   else 0.0
        avg_loss   = float(abs(losses.mean())) if len(losses) > 0 else 1e-9

        profit_factor = (avg_win * win_rate) / (avg_loss * (1 - win_rate) + 1e-9)

        # 保本胜率
        if avg_win > 1e-9:
            breakeven_wr = avg_loss / (avg_win + avg_loss)
        else:
            breakeven_wr = 1.0
        has_edge = win_rate > breakeven_wr

        if not has_edge:
            issues.append(
                f"无正期望：胜率{win_rate:.1%} < 保本胜率{breakeven_wr:.1%}")

        # ── 最大回撤修复期 ────────────────────────────────────────────────────
        eq  = np.array(equity_curve, dtype=float)
        mdd = self._max_drawdown(eq)

        # 修复期：找到最大回撤谷底后，到下次创新高需要多少步
        peak_idx, trough_idx = self._find_mdd_period(eq)
        if trough_idx < len(eq) - 1:
            new_high_idx = trough_idx
            for i in range(trough_idx, len(eq)):
                if eq[i] >= eq[peak_idx]:
                    new_high_idx = i
                    break
            recovery_bars = new_high_idx - trough_idx
        else:
            recovery_bars = len(eq) - trough_idx   # 尚未修复

        recovery_days = recovery_bars * 10 / (60 * 24)   # 10分钟K线 → 天

        if recovery_bars > 500:
            issues.append(
                f"回撤修复期过长：{recovery_bars}根K线({recovery_days:.1f}天)")

        # ── 过拟合检测（IS/OOS Sharpe 比）───────────────────────────────────
        split = int(len(trade_returns) * is_split)
        if split >= 5 and len(trade_returns) - split >= 5:
            is_rets  = trade_returns[:split]
            oos_rets = trade_returns[split:]
            is_sharpe  = self._sharpe(is_rets)
            oos_sharpe = self._sharpe(oos_rets)
            overfit_ratio = (is_sharpe / oos_sharpe
                             if abs(oos_sharpe) > 0.01 else 9.9)
        else:
            is_sharpe = oos_sharpe = overfit_ratio = 0.0

        is_overfit = overfit_ratio > 3.0
        if is_overfit:
            issues.append(
                f"疑似过拟合：IS Sharpe/OOS Sharpe = {overfit_ratio:.2f} > 3.0")

        # ── 综合评级 ──────────────────────────────────────────────────────────
        if len(issues) == 0 and profit_factor > 1.5 and win_rate > 0.45:
            grade = "A"
        elif len(issues) <= 1 and profit_factor > 1.0:
            grade = "B"
        elif profit_factor > 0.8:
            grade = "C"
        else:
            grade = "F"

        return StrategyHealth(
            win_rate       = round(win_rate, 4),
            avg_win        = round(avg_win, 6),
            avg_loss       = round(avg_loss, 6),
            profit_factor  = round(profit_factor, 4),
            breakeven_wr   = round(breakeven_wr, 4),
            has_edge       = has_edge,
            max_drawdown_pct = round(mdd * 100, 2),
            recovery_bars  = recovery_bars,
            recovery_days  = round(recovery_days, 1),
            is_sharpe      = round(is_sharpe, 3),
            oos_sharpe     = round(oos_sharpe, 3),
            overfit_ratio  = round(overfit_ratio, 2),
            is_overfit     = is_overfit,
            grade          = grade,
            issues         = issues,
        )

    @staticmethod
    def _max_drawdown(equity: np.ndarray) -> float:
        """计算最大回撤（比例）"""
        if len(equity) < 2:
            return 0.0
        peak = equity[0]
        mdd  = 0.0
        for v in equity:
            peak = max(peak, v)
            dd   = (peak - v) / peak
            mdd  = max(mdd, dd)
        return float(mdd)

    @staticmethod
    def _find_mdd_period(equity: np.ndarray):
        """找到最大回撤的峰值和谷值索引"""
        peak_idx = trough_idx = 0
        best_dd  = 0.0
        running_peak = equity[0]
        running_peak_idx = 0
        for i, v in enumerate(equity):
            if v >= running_peak:
                running_peak = v
                running_peak_idx = i
            else:
                dd = (running_peak - v) / running_peak
                if dd > best_dd:
                    best_dd   = dd
                    peak_idx  = running_peak_idx
                    trough_idx = i
        return peak_idx, trough_idx

    @staticmethod
    def _sharpe(returns: np.ndarray, rf: float = 0.0) -> float:
        """简化 Sharpe（年化）"""
        if len(returns) < 2:
            return 0.0
        mu  = float(returns.mean())
        std = float(returns.std())
        if std < 1e-9:
            return 0.0
        bars_per_year = 52560   # 10min K线
        return float((mu - rf) / std * math.sqrt(bars_per_year))


# ══════════════════════════════════════════════════════════════════════════════
#  9. AggressionRatioEngine — 主动成交方向占比
#
#  来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）
#
#  Aggression = AggBuyVol / (AggBuyVol + AggSellVol)
#
#  含义：
#    > 0.58  主动买盘主导 → 多头动能强，入场支持
#    < 0.42  主动卖盘主导 → 空头动能强，入场支持
#    0.42~0.58  方向模糊，等待
#
#  在假突破识别中：
#    突破后 aggression 迅速反向 → 假突破信号之一
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AggressionSnapshot:
    ts:                float
    ratio:             float          # 0.0~1.0，>0.5买方主导
    agg_buy_vol:       float
    agg_sell_vol:      float
    direction:         str            # "BUY" / "SELL" / "NEUTRAL"
    reversal_detected: bool           # 在多仓情况下空方突然主导 → True


class AggressionRatioEngine:
    """
    主动成交方向引擎。

    输入：tick 成交流 [(volume, side), ...], side="buy"/"sell"
    输出：AggressionSnapshot

    参数：
      window_ms       时间窗口（毫秒），默认2000ms
      buy_threshold   买方主导阈值（默认0.58）
      sell_threshold  卖方主导阈值（默认0.42）
    """

    def __init__(
        self,
        window_ms:       float = 2000.0,
        buy_threshold:   float = 0.58,
        sell_threshold:  float = 0.42,
    ):
        self.window_ms     = window_ms
        self.buy_threshold  = buy_threshold
        self.sell_threshold = sell_threshold
        self._trades: deque = deque()   # (ts_ms, volume, side)
        self._prev_ratio:   float = 0.5
        self._prev_direction: str = "NEUTRAL"

    def add_trade(self, ts_ms: float, volume: float, side: str) -> None:
        """
        添加一笔成交。

        Args:
            ts_ms:  成交时间（毫秒时间戳）
            volume: 成交量
            side:   "buy" 或 "sell"（主动方向）
        """
        self._trades.append((ts_ms, volume, side.lower()))
        # 清除窗口外数据
        cutoff = ts_ms - self.window_ms
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def snapshot(
        self,
        ts_ms:            float,
        current_position: int = 0,   # +1=多 -1=空 0=无
    ) -> AggressionSnapshot:
        """
        返回当前时间窗口内的主动成交快照。
        """
        buy_vol = sell_vol = 0.0
        cutoff  = ts_ms - self.window_ms
        for t, v, s in self._trades:
            if t < cutoff:
                continue
            if s == "buy":
                buy_vol += v
            else:
                sell_vol += v

        total = buy_vol + sell_vol
        ratio = buy_vol / total if total > 1e-9 else 0.5

        if ratio >= self.buy_threshold:
            direction = "BUY"
        elif ratio <= self.sell_threshold:
            direction = "SELL"
        else:
            direction = "NEUTRAL"

        # 反转检测：持多仓时卖方突然主导
        reversal = False
        if current_position == 1 and direction == "SELL":
            reversal = True
        elif current_position == -1 and direction == "BUY":
            reversal = True

        self._prev_ratio     = ratio
        self._prev_direction = direction

        return AggressionSnapshot(
            ts=ts_ms / 1000.0,
            ratio=round(ratio, 4),
            agg_buy_vol=round(buy_vol, 4),
            agg_sell_vol=round(sell_vol, 4),
            direction=direction,
            reversal_detected=reversal,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  10. FalseBreakoutDetector — 假突破检测器（五信号复合评分）
#
#  来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）
#
#  FalseBreakoutScore（FBS）= 以下信号的累加：
#    ① wOBI 对持仓方向翻转                         +1
#    ② Aggression 反向且持续                        +1
#    ③ 同向深度 < 0.55 基线                         +1
#    ④ Spread > 2.5x 基线                          +1
#    ⑤ 距事件锚点过去N秒未形成新高/低（无进展）     +1
#
#  判断：
#    FBS >= 2 → 禁止新入场 / 减仓
#    FBS >= 3 → 立即平仓
#
#  不同品种的基准阈值（来自报告中的实战参数表）：
#    asset   obs_min  obs_max  obi_entry  aggr_buy  spread_block  depth_exit
#    CRYPTO  15s      45s      0.18       0.58       1.8x          0.55
#    FX      30s      120s     0.12       0.56       1.7x          0.60
#    ES/NQ   30s      90s      0.10       0.55       2.0x          0.55
#    ZN/ZB   30s      120s     0.12       0.55       2.5x          0.55
#    CL      60s      180s     0.14       0.57       2.2x          0.50
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AssetObservationConfig:
    """每个品种的观察期和假突破检测参数。"""
    asset_class:       str      # "CRYPTO" / "FX" / "EQUITY_INDEX" / "BOND" / "COMMODITY"
    obs_min_s:         float    # 最短观察期（秒）
    obs_max_s:         float    # 最长观察期（秒）
    no_progress_s:     float    # 无进展退出时间（秒）
    min_r_for_progress: float   # 无进展退出所需最低R
    obi_entry:         float    # 入场所需 |wOBI| 阈值
    aggr_buy_entry:    float    # 入场所需买方主动成交比例阈值
    aggr_sell_entry:   float    # 入场所需卖方主动成交比例阈值
    spread_block_mult: float    # 点差倍数 → 禁止开仓
    spread_exit_mult:  float    # 点差倍数 → 强制平仓
    depth_exit_ratio:  float    # 同向深度萎缩比例 → 退出
    cp_prob_reduce:    float    # BOCPD 变化点概率 → 减仓50%
    cp_prob_exit:      float    # BOCPD 变化点概率 → 全平
    max_ack_ms:        float    # 订单回报超时（ms）
    feed_lag_block_ms: float    # 行情延迟阻止开仓（ms）


# 五个品种的默认参数（来自报告实战参数表）
ASSET_CONFIGS: Dict[str, AssetObservationConfig] = {
    "CRYPTO": AssetObservationConfig(
        asset_class="CRYPTO",
        obs_min_s=15.0,  obs_max_s=45.0,
        no_progress_s=10.0,  min_r_for_progress=0.15,
        obi_entry=0.18,
        aggr_buy_entry=0.58,  aggr_sell_entry=0.42,
        spread_block_mult=1.8,  spread_exit_mult=2.5,
        depth_exit_ratio=0.55,
        cp_prob_reduce=0.45,  cp_prob_exit=0.65,
        max_ack_ms=300.0,  feed_lag_block_ms=250.0,
    ),
    "FX": AssetObservationConfig(
        asset_class="FX",
        obs_min_s=30.0,  obs_max_s=120.0,
        no_progress_s=20.0,  min_r_for_progress=0.12,
        obi_entry=0.12,
        aggr_buy_entry=0.56,  aggr_sell_entry=0.44,
        spread_block_mult=1.7,  spread_exit_mult=2.2,
        depth_exit_ratio=0.60,
        cp_prob_reduce=0.45,  cp_prob_exit=0.65,
        max_ack_ms=800.0,  feed_lag_block_ms=500.0,
    ),
    "EQUITY_INDEX": AssetObservationConfig(
        asset_class="EQUITY_INDEX",
        obs_min_s=30.0,  obs_max_s=90.0,
        no_progress_s=20.0,  min_r_for_progress=0.15,
        obi_entry=0.10,
        aggr_buy_entry=0.55,  aggr_sell_entry=0.45,
        spread_block_mult=2.0,  spread_exit_mult=2.5,
        depth_exit_ratio=0.55,
        cp_prob_reduce=0.45,  cp_prob_exit=0.65,
        max_ack_ms=1000.0,  feed_lag_block_ms=750.0,
    ),
    "BOND": AssetObservationConfig(
        asset_class="BOND",
        obs_min_s=30.0,  obs_max_s=120.0,
        no_progress_s=30.0,  min_r_for_progress=0.12,
        obi_entry=0.12,
        aggr_buy_entry=0.55,  aggr_sell_entry=0.45,
        spread_block_mult=2.5,  spread_exit_mult=3.0,
        depth_exit_ratio=0.55,
        cp_prob_reduce=0.45,  cp_prob_exit=0.65,
        max_ack_ms=1000.0,  feed_lag_block_ms=750.0,
    ),
    "COMMODITY": AssetObservationConfig(
        asset_class="COMMODITY",
        obs_min_s=60.0,  obs_max_s=180.0,
        no_progress_s=30.0,  min_r_for_progress=0.15,
        obi_entry=0.14,
        aggr_buy_entry=0.57,  aggr_sell_entry=0.43,
        spread_block_mult=2.2,  spread_exit_mult=2.8,
        depth_exit_ratio=0.50,
        cp_prob_reduce=0.45,  cp_prob_exit=0.65,
        max_ack_ms=1000.0,  feed_lag_block_ms=750.0,
    ),
}


@dataclass
class FalseBreakoutResult:
    score:              int       # 0~5，数字越高假突破可能性越大
    block_entry:        bool      # score >= 2 → 禁止入场
    force_exit:         bool      # score >= 3 → 强制平仓
    triggered_signals:  List[str] # 触发了哪些信号
    obi_reversed:       bool
    aggr_reversed:      bool
    depth_collapsed:    bool
    spread_blown:       bool
    no_progress:        bool
    entry_gate:         str       # "ALLOW" / "ALLOW_REDUCED" / "BLOCK" / "WAIT"


class FalseBreakoutDetector:
    """
    假突破检测器：五信号复合评分。

    在重大宏观事件后的"假突破期"（事件后30-120秒），
    65%的情况下初始方向与最终趋势相反（FOMC），
    因此需要等待确认并识别假突破后立即退出。

    使用方法：
        detector = FalseBreakoutDetector(cfg=ASSET_CONFIGS["CRYPTO"])
        result = detector.evaluate(
            obi_snap=obi_engine.update(...),
            aggr_snap=aggr_engine.snapshot(...),
            current_spread=0.5,
            baseline_spread=0.3,
            same_side_depth=800000,
            baseline_same_side_depth=1500000,
            position=1,  # +1=多 -1=空
            elapsed_s=25.0,
            current_r=0.08,   # 当前浮盈 in R units
            entry_price=50000.0,
        )
    """

    def __init__(self, cfg: Optional[AssetObservationConfig] = None):
        self.cfg = cfg or ASSET_CONFIGS["CRYPTO"]

    def evaluate(
        self,
        obi_snap:              OBISnapshot,
        aggr_snap:             AggressionSnapshot,
        current_spread:        float,
        baseline_spread:       float,
        same_side_depth:       float,
        baseline_same_side_depth: float,
        position:              int,        # +1=多 -1=空 0=无
        elapsed_s:             float,      # 自进场以来的秒数
        current_r:             float,      # 当前浮盈（R单位）
        entry_price:           float = 0.0,
    ) -> FalseBreakoutResult:
        """
        综合评估假突破风险。

        Returns:
            FalseBreakoutResult，含综合评分和各信号详情
        """
        triggered = []
        score = 0

        # ① wOBI 对持仓方向翻转
        obi_reversed = False
        if position == 1 and obi_snap.obi < 0.0:
            obi_reversed = True
            score += 1
            triggered.append(f"wOBI反转({obi_snap.obi:.3f})")
        elif position == -1 and obi_snap.obi > 0.0:
            obi_reversed = True
            score += 1
            triggered.append(f"wOBI反转({obi_snap.obi:.3f})")

        # ② Aggression 反向且持续
        aggr_reversed = aggr_snap.reversal_detected
        if aggr_reversed:
            score += 1
            triggered.append(f"Aggression反向({aggr_snap.ratio:.2f})")

        # ③ 同向深度 < 阈值 × 基线
        depth_ratio = (same_side_depth / baseline_same_side_depth
                       if baseline_same_side_depth > 0 else 1.0)
        depth_collapsed = depth_ratio < self.cfg.depth_exit_ratio
        if depth_collapsed:
            score += 1
            triggered.append(
                f"深度坍塌({depth_ratio:.2f}<{self.cfg.depth_exit_ratio})")

        # ④ 点差 > 阈值 × 基线
        spread_mult = (current_spread / baseline_spread
                       if baseline_spread > 1e-9 else 1.0)
        spread_blown = spread_mult > self.cfg.spread_exit_mult
        if spread_blown:
            score += 1
            triggered.append(
                f"点差爆炸({spread_mult:.2f}x>{self.cfg.spread_exit_mult}x)")

        # ⑤ 无进展（超时且未达到最低R目标）
        no_progress = (elapsed_s > self.cfg.no_progress_s
                       and current_r < self.cfg.min_r_for_progress)
        if no_progress:
            score += 1
            triggered.append(
                f"无进展({elapsed_s:.0f}s,R={current_r:.3f})")

        block_entry = score >= 2
        force_exit  = score >= 3

        # ── 入场门（供确认阶段使用）────────────────────────────────────────
        spread_blocks = spread_mult > self.cfg.spread_block_mult
        depth_weak    = depth_ratio < 0.80
        obi_ok = abs(obi_snap.obi) >= self.cfg.obi_entry
        aggr_ok = (
            (position >= 0 and aggr_snap.ratio >= self.cfg.aggr_buy_entry) or
            (position <= 0 and aggr_snap.ratio <= self.cfg.aggr_sell_entry)
        )

        gate_score = sum([obi_ok, aggr_ok,
                          not depth_weak, not spread_blocks,
                          current_r > 0])

        if spread_blocks or depth_weak:
            entry_gate = "BLOCK"
        elif gate_score >= 4:
            entry_gate = "ALLOW"
        elif gate_score == 3:
            entry_gate = "ALLOW_REDUCED"
        else:
            entry_gate = "WAIT"

        return FalseBreakoutResult(
            score=score,
            block_entry=block_entry,
            force_exit=force_exit,
            triggered_signals=triggered,
            obi_reversed=obi_reversed,
            aggr_reversed=aggr_reversed,
            depth_collapsed=depth_collapsed,
            spread_blown=spread_blown,
            no_progress=no_progress,
            entry_gate=entry_gate,
        )


# ══════════════════════════════════════════════════════════════════════════════
#  11. BOCPDEngine — 贝叶斯在线变化点检测
#
#  来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）
#
#  原理：Bayesian Online Change Point Detection（BOCPD）
#    不是预测未来价格，而是检测"市场结构已经突然改变"。
#    例如：波动率突然爆炸、流动性突然消失、订单簿结构突然反转。
#
#  输入：脆弱度合成分数（fragility_score）
#    推荐用：[signed_return, spread_mult, same_side_depth_ratio] 的组合
#
#  输出：
#    cp_prob        > 0.45 → FRAGILE（建议减仓50%）
#    cp_prob        > 0.65 → CHANGE_POINT（立即清仓）
#
#  参数设置（来自报告）：
#    Crypto:   hazard≈1/200（约20秒切换一次）
#    FX/期货:  hazard≈1/300~1/500
#    截断 run length: 150~300
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BOCPDResult:
    cp_prob:      float    # 变化点概率 0.0~1.0
    status:       str      # "STABLE" / "FRAGILE" / "CHANGE_POINT"
    run_length:   int      # 当前连续运行长度（越长越稳定）
    action:       str      # "HOLD" / "REDUCE_50" / "FLATTEN"


class BOCPDEngine:
    """
    贝叶斯在线变化点检测（截断版，在线轻量实现）。

    适合监控市场微观结构的突然变化，为提前退出提供数学依据。

    输入：fragility_score（脆弱度分数），建议使用：
        fs = 0.4 * |signed_return_1s| + 0.3 * (spread_mult-1) + 0.3 * (1-depth_ratio)

    算法核心（截断BOCPD）：
        每个时间步维护一个运行长度的对数概率分布
        新观测值通过贝叶斯更新：
            P(r_t | data) ∝ P(x_t | r_t, data_r_t) * P(r_t | r_{t-1})
        变化点概率 = P(run_length=0 at time t)

    复杂度：O(R_max) 每步，内存 O(R_max)
    """

    def __init__(
        self,
        hazard:     float = 1.0 / 200.0,   # 每步变化概率（Crypto用1/200）
        r_max:      int   = 200,            # 最大运行长度（截断）
        threshold_fragile:  float = 0.45,
        threshold_exit:     float = 0.65,
        warmup_steps:       int   = 20,     # 预热步数（稳定先验）
    ):
        self.hazard     = hazard
        self.r_max      = r_max
        self.th_fragile = threshold_fragile
        self.th_exit    = threshold_exit
        self.warmup     = warmup_steps

        # 内部状态
        self._log_post  = np.zeros(r_max + 1)  # 运行长度对数后验
        self._log_post[0] = 0.0                 # 初始全重在 run=0
        self._log_post[1:] = -np.inf
        self._mu        = 0.0                   # 在线均值
        self._var       = 1.0                   # 在线方差
        self._n         = 0                     # 样本数
        self._log_h     = math.log(hazard)
        self._log_1mh   = math.log(1.0 - hazard)

    def update(self, x: float) -> BOCPDResult:
        """
        处理一个新观测值，返回变化点检测结果。

        Args:
            x: 脆弱度分数（建议归一化到 0~3 范围）

        Returns:
            BOCPDResult
        """
        self._n += 1

        # 在线更新全局统计（用于预测分布）
        delta = x - self._mu
        self._mu  += delta / self._n
        delta2     = x - self._mu
        self._var  = ((self._var * (self._n - 1) + delta * delta2) / self._n
                      if self._n > 1 else max(self._var, 1e-6))
        std = max(math.sqrt(self._var), 1e-6)

        # 高斯预测对数似然
        log_pred = -0.5 * ((x - self._mu) ** 2 / self._var) - math.log(std * 2.506)

        # 变化点（run=0）：来自所有先前 run 的 hazard 转移
        log_cp = np.logaddexp.reduce(self._log_post[:-1]) + self._log_h + log_pred

        # 连续（run=r+1）：run 从 0..R-1 增长 + 非变化点
        log_grow = self._log_post[:-1] + self._log_1mh + log_pred

        # 构建新后验
        new_log_post = np.full(self.r_max + 1, -np.inf)
        new_log_post[0] = log_cp
        new_log_post[1:] = log_grow[:self.r_max]

        # 归一化
        log_z = np.logaddexp.reduce(new_log_post)
        self._log_post = new_log_post - log_z

        # 变化点概率
        cp_prob = math.exp(self._log_post[0])

        # 最可能的运行长度
        run_length = int(np.argmax(self._log_post))

        # 预热期内不报警
        if self._n < self.warmup:
            return BOCPDResult(
                cp_prob=0.0, status="STABLE",
                run_length=run_length, action="HOLD")

        if cp_prob >= self.th_exit:
            status, action = "CHANGE_POINT", "FLATTEN"
        elif cp_prob >= self.th_fragile:
            status, action = "FRAGILE", "REDUCE_50"
        else:
            status, action = "STABLE", "HOLD"

        return BOCPDResult(
            cp_prob=round(cp_prob, 4),
            status=status,
            run_length=run_length,
            action=action,
        )

    def reset(self) -> None:
        """重置为初始状态（新事件开始前调用）。"""
        self._log_post     = np.full(self.r_max + 1, -np.inf)
        self._log_post[0]  = 0.0
        self._mu           = 0.0
        self._var          = 1.0
        self._n            = 0


# ══════════════════════════════════════════════════════════════════════════════
#  12. CorrectPositionSizer — 正确的仓位计算公式
#
#  来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）
#
#  核心纠错：
#    ❌ 旧逻辑：按信心值决定仓位大小（越有信心越大仓）
#    ✅ 新逻辑：仓位 = min(5个上限) 确保资金安全第一
#
#  正确公式：
#    名义仓位上限 = 账户净值 × 单笔最大损失比例 / 有效止损幅度
#    有效止损幅度 = 计划止损 + 预期滑点 + 手续费 + 跳空缓冲
#
#  5个上限（取最小值）：
#    1. 风险预算上限（上述公式）
#    2. 流动性上限（基于当前盘口深度）
#    3. 允许滑点上限（滑点预算不超过止损的35%）
#    4. 延迟上限（延迟高时降低仓位）
#    5. 尾部风险上限（EVT/GARCH预测极端亏损时压缩）
#
#  在高杠杆下的实例（来自报告）：
#    账户50000美元，单笔最大风险1%=500美元
#    有效止损=0.22%（计划0.18%+滑点0.04%）
#    名义仓位上限 = 500 / 0.0022 = 227,273美元
#    150x杠杆下只需~1,515美元保证金
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionSizeResult:
    final_notional:         float    # 最终名义仓位（美元）
    final_contracts:        float    # 合约手数（notional / contract_value）
    binding_constraint:     str      # 哪个约束条件最严格
    risk_budget_notional:   float    # 风险预算上限
    liquidity_notional:     float    # 流动性上限
    slippage_notional:      float    # 滑点上限
    tail_risk_notional:     float    # 尾部风险上限
    effective_stop_width:   float    # 有效止损幅度（百分比）
    risk_reward_ratio:      float    # 预期盈亏比
    details:                dict     = field(default_factory=dict)


class CorrectPositionSizer:
    """
    正确的高杠杆事件交易仓位计算器。

    核心原则：仓位不由"信心"决定，而由风险约束决定。
    最终仓位 = min(风险预算, 流动性, 滑点, 延迟, 尾部风险) 五个上限。

    用法示例：
        sizer = CorrectPositionSizer(
            equity=50000,
            max_loss_pct=0.01,       # 单笔最大1%亏损
            contract_value=1000.0,   # 每手合约价值（USDT）
        )
        result = sizer.compute(
            plan_stop_pct=0.0018,    # 计划止损0.18%
            pred_slippage_pct=0.0004,# 预计滑点0.04%
            available_depth=5000000, # 当前可用盘口深度（USD）
            max_slippage_budget=0.35,# 滑点不超过止损的35%
            garch_vol_mult=1.0,      # GARCH 波动率乘数
            feed_lag_ms=100.0,       # 当前行情延迟
            asset_class="CRYPTO",
        )
    """

    def __init__(
        self,
        equity:          float,
        max_loss_pct:    float = 0.01,    # 单笔最大亏损比例（1%）
        contract_value:  float = 1.0,     # 每手合约价值
        jump_buffer_pct: float = 0.0003,  # 跳空缓冲（0.03%）
        fee_pct:         float = 0.0001,  # 手续费（0.01%）
    ):
        self.equity         = equity
        self.max_loss_pct   = max_loss_pct
        self.contract_value = contract_value
        self.jump_buffer    = jump_buffer_pct
        self.fee_pct        = fee_pct

    def compute(
        self,
        plan_stop_pct:       float,    # 计划止损（占价格比例）
        pred_slippage_pct:   float,    # 预测滑点（占价格比例）
        available_depth:     float,    # 同向盘口可用深度（名义USD）
        max_slippage_budget: float = 0.35,  # 滑点不超过止损的35%
        garch_vol_mult:      float = 1.0,   # GARCH波动率乘数（>2时压缩）
        feed_lag_ms:         float = 0.0,   # 当前行情延迟（ms）
        asset_class:         str   = "CRYPTO",
    ) -> PositionSizeResult:
        """
        计算正确的仓位大小。

        Returns:
            PositionSizeResult，含最终仓位和各约束详情
        """
        cfg = ASSET_CONFIGS.get(asset_class, ASSET_CONFIGS["CRYPTO"])

        # 有效止损幅度 = 计划止损 + 滑点 + 手续费 + 跳空缓冲
        effective_stop = (plan_stop_pct + pred_slippage_pct
                          + self.fee_pct + self.jump_buffer)
        effective_stop = max(effective_stop, 1e-6)

        # ① 风险预算上限
        max_loss_usd     = self.equity * self.max_loss_pct
        risk_notional    = max_loss_usd / effective_stop

        # ② 流动性上限（不超过同向深度的10%，避免移动市场）
        liquidity_notional = available_depth * 0.10

        # ③ 滑点上限（滑点不超过止损预算的35%）
        if pred_slippage_pct > plan_stop_pct * max_slippage_budget:
            # 滑点已经超预算，最大仓位压缩到风险预算的50%
            slippage_notional = risk_notional * 0.5
        else:
            slippage_notional = risk_notional * 1.0

        # ④ 延迟上限（行情延迟越高越压缩仓位）
        lag_factor = 1.0
        if feed_lag_ms > cfg.feed_lag_block_ms:
            lag_factor = 0.0    # 超过阻止阈值，不开仓
        elif feed_lag_ms > cfg.feed_lag_block_ms * 0.5:
            lag_factor = 0.5    # 延迟较高，减半

        # ⑤ 尾部风险上限（GARCH波动率乘数）
        if garch_vol_mult > 3.0:
            tail_factor = 0.0   # 极端波动，禁止开仓
        elif garch_vol_mult > 2.0:
            tail_factor = 0.3
        elif garch_vol_mult > 1.5:
            tail_factor = 0.6
        else:
            tail_factor = 1.0
        tail_notional = risk_notional * tail_factor

        # 最终仓位：五个上限取最小值
        candidates = {
            "风险预算":  risk_notional,
            "流动性":    liquidity_notional,
            "滑点预算":  slippage_notional,
            "尾部风险":  tail_notional,
        }
        if lag_factor == 0.0:
            final_notional = 0.0
            binding = "行情延迟>阈值(禁止开仓)"
        else:
            final_notional = min(candidates.values()) * lag_factor
            binding = min(candidates, key=lambda k: candidates[k])

        final_contracts = final_notional / max(self.contract_value, 1.0)

        # 预期盈亏比（简单估算：默认目标2R）
        rr_ratio = 2.0 / (effective_stop / plan_stop_pct) if plan_stop_pct > 0 else 2.0

        return PositionSizeResult(
            final_notional=round(final_notional, 2),
            final_contracts=round(final_contracts, 4),
            binding_constraint=binding,
            risk_budget_notional=round(risk_notional, 2),
            liquidity_notional=round(liquidity_notional, 2),
            slippage_notional=round(slippage_notional, 2),
            tail_risk_notional=round(tail_notional, 2),
            effective_stop_width=round(effective_stop * 100, 4),
            risk_reward_ratio=round(rr_ratio, 2),
            details={
                "max_loss_usd": round(max_loss_usd, 2),
                "lag_factor":   lag_factor,
                "tail_factor":  tail_factor,
                "garch_vol_mult": garch_vol_mult,
            },
        )

