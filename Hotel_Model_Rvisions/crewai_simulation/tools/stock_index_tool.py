"""
stock_index_tool.py — 股指期货量化交易 CrewAI 工具
====================================================
移植 Oil-Model 架构（RegimeService / SignalService / RiskService）并
叠加股指期货专用数学模型：

  核心信号体系（已实现）：
  ① Engle-Granger 协整检验（OLS + 简化ADF）
  ② 卡尔曼滤波器（动态对冲比率 β）
  ③ Z-Score 价差均值回归（统计套利）
  ④ 动量/趋势跟随（EMA + ADX + ATR 止损）

  扩展数学模型（v2 新增）：
  ⑤  HalfLifeEstimator   — OU 过程均值回归半衰期
  ⑥  HurstExponent       — R/S 分析（趋势/随机/均值回归 分类）
  ⑦  JohansenTest        — 迹统计量多资产协整检验（纯 numpy）
  ⑧  SimpleGARCH         — GARCH(1,1) 波动率预测
  ⑨  PerformanceMetrics  — Sharpe / Sortino / MaxDD / Calmar / 盈亏比
  ⑩  PortfolioVaR        — 历史模拟 VaR / CVaR（组合级）
  ⑪  RollingCorrelation  — 60 日滚动相关矩阵
  ⑫  StrategyMonitor     — 渐进恢复链（GREEN→RECOVERY→COOLDOWN）
  ⑬  EventCalendar       — US 宏观日历（FOMC/CPI/NFP → 自动 EVENT 模式）
  ⑭  VIXFilter           — VIX 跨资产风险过滤器
  ⑮  IBExecutor          — Interactive Brokers 执行桩（US期货）
  ⑯  CTPExecutor         — CTP 执行桩（CN期货）

支持市场：
  US  → ES (S&P500), NQ (Nasdaq), YM (Dow), RTY (Russell 2000)
        代理标的：^GSPC, ^IXIC, ^DJI, ^RUT
  CN  → IF (沪深300), IC (中证500), IH (上证50), IM (中证1000)
        代理标的：000300.SS, 000905.SS, 000016.SS, 000852.SS

数据层：yfinance（免费，无需 API Key，实盘用 IB/CTP）
执行层：纸交易（默认）| IB via ib_insync（US）| CTP-Python（CN）

工具列表：
  StockIndexSignalTool      → 单品种趋势信号 + 双品种套利信号
  StockIndexRiskTool        → 风控状态查询 / Kill Switch
  StockIndexPortfolioTool   → 持仓 / PnL / 时间止损检查
  StockIndexAnalyticsTool   → 数学分析：半衰期/Hurst/VaR/相关矩阵/绩效
"""

from __future__ import annotations

import os
import math
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta
from enum import Enum
from typing import Optional, List, Dict, Tuple, Any
from collections import deque

import numpy as np

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════════
#  1. 枚举 & 数据结构（移植自 Oil-Model core.py）
# ══════════════════════════════════════════════════════════════════

class Regime(str, Enum):
    NORMAL  = "NORMAL"   # 常规波动，技术信号主导
    TREND   = "TREND"    # 单边强趋势（ADX>28）
    EVENT   = "EVENT"    # 重大事件窗口（财报/Fed/宏观）
    BLOCKED = "BLOCKED"  # 极端波动，停止执行

class SignalType(str, Enum):
    MOMENTUM = "MOMENTUM"   # 单品种趋势跟随
    STAT_ARB = "STAT_ARB"  # 双品种统计套利（协整）

class Direction(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"

class ExitReason(str, Enum):
    STOP_LOSS       = "STOP_LOSS"
    TIME_STOP       = "TIME_STOP"
    SPREAD_MEAN_REV = "SPREAD_MEAN_REV"
    RISK_CONTROL    = "RISK_CONTROL"
    KILL_SWITCH     = "KILL_SWITCH"

@dataclass
class Bar:
    ts: str
    open: float; high: float; low: float; close: float; volume: float
    symbol: str = ""

    @property
    def range(self) -> float: return self.high - self.low
    @property
    def body(self) -> float:  return abs(self.close - self.open)

@dataclass
class Indicators:
    ema_fast: float       # EMA20
    ema_slow: float       # EMA50
    adx: float            # ADX(14)
    atr: float            # ATR(14)
    atr_baseline: float   # 60 周期均值 ATR
    rsi: float            # RSI(14)
    bb_upper: float       # Bollinger 上轨
    bb_lower: float       # Bollinger 下轨
    vwap: float           # VWAP
    volume_ratio: float   # 当前量 / 20 周期均量

    @property
    def ema_bullish(self)   -> bool: return self.ema_fast > self.ema_slow
    @property
    def trend_strong(self)  -> bool: return self.adx > 22.0
    @property
    def is_high_vol(self)   -> bool:
        return self.atr / self.atr_baseline > 1.8 if self.atr_baseline > 0 else False

@dataclass
class RiskState:
    daily_pnl:          float = 0.0
    consecutive_losses: int   = 0
    total_trades_today: int   = 0
    is_halted:          bool  = False
    halt_reason:        str   = ""
    kill_switch_active: bool  = False

    def register(self, pnl: float):
        self.daily_pnl += pnl
        self.total_trades_today += 1
        if pnl < 0: self.consecutive_losses += 1
        else:       self.consecutive_losses = 0

@dataclass
class Position:
    id: str
    symbol: str
    signal_type: SignalType
    direction: Direction
    size: float              # 手数
    entry_price: float
    stop_loss: float
    entry_ts: str
    pair_leg: Optional[str] = None       # 套利：对冲品种
    pair_direction: Optional[str] = None # 套利：对冲方向
    pair_size: Optional[float] = None    # 套利：对冲手数
    pair_entry: Optional[float] = None   # 套利：对冲入场价
    take_profit: Optional[float] = None  # 止盈价位

    @property
    def age_minutes(self) -> float:
        try:
            t0 = datetime.fromisoformat(self.entry_ts)
            return (datetime.now(timezone.utc) - t0).total_seconds() / 60
        except Exception:
            return 0.0

# ══════════════════════════════════════════════════════════════════
#  2. 技术指标库（移植自 FX-model indicators.py，纯 numpy）
# ══════════════════════════════════════════════════════════════════

class TI:
    """Technical Indicators — pure numpy, no TA-Lib dependency"""

    @staticmethod
    def ema(prices: np.ndarray, n: int) -> np.ndarray:
        out = np.full(len(prices), np.nan)
        if len(prices) < n: return out
        mult = 2.0 / (n + 1)
        out[n - 1] = prices[:n].mean()
        for i in range(n, len(prices)):
            out[i] = (prices[i] - out[i - 1]) * mult + out[i - 1]
        return out

    @staticmethod
    def atr(highs, lows, closes, n=14) -> np.ndarray:
        L = len(closes)
        tr = np.zeros(L)
        tr[0] = highs[0] - lows[0]
        for i in range(1, L):
            tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        out = np.full(L, np.nan)
        out[n] = tr[1:n+1].mean()
        for i in range(n+1, L):
            out[i] = (out[i-1] * (n-1) + tr[i]) / n
        return out

    @staticmethod
    def adx(highs, lows, closes, n=14) -> np.ndarray:
        L = len(closes)
        out = np.full(L, np.nan)
        if L < n * 2: return out
        tr = np.zeros(L); pdm = np.zeros(L); mdm = np.zeros(L)
        tr[0] = highs[0] - lows[0]
        for i in range(1, L):
            tr[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            up  = highs[i] - highs[i-1]
            dn  = lows[i-1] - lows[i]
            pdm[i] = up  if up > dn and up > 0 else 0
            mdm[i] = dn  if dn > up and dn > 0 else 0
        # Wilder smoothing
        tr_s = pdm_s = mdm_s = 0.0
        tr_s  = tr[1:n+1].sum(); pdm_s = pdm[1:n+1].sum(); mdm_s = mdm[1:n+1].sum()
        dx_arr = []
        for i in range(n+1, L):
            tr_s  = tr_s  - tr_s/n  + tr[i]
            pdm_s = pdm_s - pdm_s/n + pdm[i]
            mdm_s = mdm_s - mdm_s/n + mdm[i]
            pdi = 100*pdm_s/tr_s if tr_s else 0
            mdi = 100*mdm_s/tr_s if tr_s else 0
            dx_arr.append(100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi) else 0)
        if len(dx_arr) >= n:
            adx_val = sum(dx_arr[:n]) / n
            out[n*2] = adx_val
            for j, i in enumerate(range(n*2+1, L)):
                adx_val = (adx_val*(n-1) + dx_arr[n+j]) / n
                out[i] = adx_val
        return out

    @staticmethod
    def rsi(closes, n=14) -> np.ndarray:
        L = len(closes)
        out = np.full(L, np.nan)
        if L < n+1: return out
        d = np.diff(closes)
        g = np.where(d>0, d, 0.0); lo = np.where(d<0, -d, 0.0)
        ag = g[:n].mean(); al = lo[:n].mean()
        out[n] = 100 - 100/(1+ag/al) if al>0 else 100
        for i in range(n, len(d)):
            ag = (ag*(n-1)+g[i])/n; al = (al*(n-1)+lo[i])/n
            out[i+1] = 100 - 100/(1+ag/al) if al>0 else 100
        return out

    @staticmethod
    def bollinger(closes, n=20, nstd=2.0):
        L = len(closes)
        mid = np.full(L, np.nan); up = np.full(L, np.nan); dn = np.full(L, np.nan)
        for i in range(n-1, L):
            w = closes[i-n+1:i+1]
            m = w.mean(); s = w.std(ddof=0)
            mid[i]=m; up[i]=m+nstd*s; dn[i]=m-nstd*s
        return up, mid, dn

    @staticmethod
    def vwap(highs, lows, closes, volumes):
        tp = (highs + lows + closes) / 3
        cv = np.cumsum(volumes)
        ctv = np.cumsum(tp * volumes)
        out = np.full(len(closes), np.nan)
        mask = cv > 0
        out[mask] = ctv[mask] / cv[mask]
        return out

    @staticmethod
    def latest(arr: np.ndarray) -> Optional[float]:
        for v in reversed(arr):
            if not np.isnan(v): return float(v)
        return None

# ══════════════════════════════════════════════════════════════════
#  3. 核心数学模型：协整 + 卡尔曼滤波器
# ══════════════════════════════════════════════════════════════════

class KalmanHedgeRatio:
    """
    卡尔曼滤波器动态对冲比率估计
    ──────────────────────────────
    模型：y_t = β_t * x_t + α_t + ε_t
    状态：θ_t = [α_t, β_t]
    观测：y_t = H * θ_t + ε    H = [1, x_t]
    转移：θ_t = θ_{t-1} + w_t  (random walk prior)

    参数：
      delta  — 状态噪声强度（越大 β 越快跟随，越小越稳定，推荐 1e-4~1e-3）
      ve     — 观测噪声方差（初始估计，推荐 0.001）
    """

    def __init__(self, delta: float = 1e-4, ve: float = 0.001):
        self.delta = delta
        self.ve    = ve
        self._theta = np.zeros(2)           # [α, β]
        self._P     = np.eye(2) * 1.0       # 误差协方差矩阵
        self._Q     = delta / (1 - delta) * np.eye(2)  # 过程噪声
        self._initialized = False
        self._spread_history: deque = deque(maxlen=200)

    def update(self, y: float, x: float) -> Tuple[float, float, float]:
        """
        输入最新价格对 (y=品种A, x=品种B)
        返回 (alpha, beta, spread)
        """
        H = np.array([1.0, x])  # 观测矩阵

        if not self._initialized:
            # 首次：用当前值初始化
            self._theta = np.array([y - x, 1.0])
            self._initialized = True

        # 预测
        P_pred = self._P + self._Q

        # 创新（残差）
        y_pred  = H @ self._theta
        innov   = y - y_pred
        S       = H @ P_pred @ H.T + self.ve  # 创新协方差

        # 卡尔曼增益
        K = P_pred @ H.T / S

        # 更新
        self._theta = self._theta + K * innov
        self._P     = (np.eye(2) - np.outer(K, H)) @ P_pred

        alpha, beta = self._theta[0], self._theta[1]
        spread = y - beta * x - alpha
        self._spread_history.append(spread)
        return alpha, beta, spread

    def zscore(self, window: int = 60) -> Optional[float]:
        """当前价差的 Z-Score（基于滚动窗口均值/标准差）"""
        if len(self._spread_history) < window:
            return None
        arr = np.array(list(self._spread_history)[-window:])
        mu  = arr.mean(); sigma = arr.std()
        if sigma < 1e-9: return None
        return float((arr[-1] - mu) / sigma)

    @property
    def beta(self) -> float:
        return float(self._theta[1])

    @property
    def alpha(self) -> float:
        return float(self._theta[0])

    @property
    def spread(self) -> Optional[float]:
        return self._spread_history[-1] if self._spread_history else None


def engle_granger_coint(y: np.ndarray, x: np.ndarray) -> dict:
    """
    Engle-Granger 协整检验（简化版，不依赖 statsmodels）
    ──────────────────────────────────────────────────────
    步骤：
    1. OLS 回归：y = α + β*x + ε
    2. 计算残差 ε
    3. ADF 检验残差（简化 t 统计量，临界值 -3.34 @ 5%）
    返回：
      beta       — 静态对冲比率
      alpha      — 截距
      adf_stat   — ADF 统计量（越负越好）
      cointegrated — 是否协整（ADF < -3.34 为真）
      spread_std — 价差标准差
    """
    n = len(y)
    # OLS：加截距项
    X = np.column_stack([np.ones(n), x])
    try:
        coeffs = np.linalg.lstsq(X, y, rcond=None)[0]
    except Exception:
        return {"beta": 1.0, "alpha": 0.0, "adf_stat": 0.0,
                "cointegrated": False, "spread_std": np.nan}

    alpha, beta = coeffs[0], coeffs[1]
    residuals   = y - (alpha + beta * x)

    # 简化 ADF：回归 Δε_t = γ*ε_{t-1} + δ*Δε_{t-1} + u
    diffs = np.diff(residuals)
    lagged = residuals[:-1]
    # OLS for ADF (no lags version for simplicity)
    if len(lagged) < 10:
        return {"beta": beta, "alpha": alpha, "adf_stat": 0.0,
                "cointegrated": False, "spread_std": float(residuals.std())}

    X_adf = np.column_stack([np.ones(len(lagged)), lagged])
    try:
        adf_coeffs, resid, _, _ = np.linalg.lstsq(X_adf, diffs, rcond=None)
    except Exception:
        return {"beta": beta, "alpha": alpha, "adf_stat": 0.0,
                "cointegrated": False, "spread_std": float(residuals.std())}

    gamma = adf_coeffs[1]
    fitted = X_adf @ adf_coeffs
    sse = ((diffs - fitted)**2).sum()
    se  = np.sqrt(sse / max(1, len(diffs)-2) / max(1e-12, (lagged**2).sum()))
    adf_stat = gamma / se if se > 0 else 0.0

    # 5% 临界值 ≈ -3.34（Engle-Granger residual-based）
    cointegrated = adf_stat < -3.34

    return {
        "beta":          round(float(beta), 4),
        "alpha":         round(float(alpha), 4),
        "adf_stat":      round(float(adf_stat), 3),
        "cointegrated":  cointegrated,
        "spread_std":    round(float(residuals.std()), 4),
    }

# ══════════════════════════════════════════════════════════════════
#  4. 市场状态识别（移植自 Oil-Model regime_service.py）
# ══════════════════════════════════════════════════════════════════

class RegimeEngine:
    """
    量化市场状态识别
    NORMAL  → ADX < 22，常规波动
    TREND   → ADX > 28，单边趋势
    BLOCKED → 波动率 > 3.5x 基准ATR，极端行情
    EVENT   → 外部手动设置（重大财报/Fed/政策）
    """

    BLOCKED_VOL_MULT = 3.5
    TREND_ADX_MIN    = 28.0
    TREND_ADX_MAX    = 60.0  # ADX > 60 可能假突破，降为 NORMAL

    def __init__(self):
        self._current     = Regime.NORMAL
        self._event_until: Optional[float] = None

    def evaluate(self, ind: Indicators) -> Regime:
        # 手动事件窗口优先
        if self._event_until and time.time() < self._event_until:
            return Regime.EVENT

        # 自动事件日历检测（FOMC/CPI/NFP → EVENT）
        # _EVENT_CALENDAR 在 Section 18 定义，延迟引用
        try:
            if _EVENT_CALENDAR.is_event_active():
                return Regime.EVENT
        except NameError:
            pass  # EventCalendar 尚未初始化

        # 极端波动 → 停止
        if ind.atr_baseline > 0:
            vol_ratio = ind.atr / ind.atr_baseline
            if vol_ratio > self.BLOCKED_VOL_MULT:
                return Regime.BLOCKED

        # 强趋势
        if self.TREND_ADX_MIN < ind.adx < self.TREND_ADX_MAX:
            return Regime.TREND

        return Regime.NORMAL

    def set_event_window(self, seconds: int = 1800):
        """人工标记事件窗口（如 FOMC/财报，默认30分钟）"""
        self._event_until = time.time() + seconds

    def clear_event(self):
        self._event_until = None

_REGIME_ENGINE = RegimeEngine()

# ══════════════════════════════════════════════════════════════════
#  5. 风控服务（完整移植自 Oil-Model risk_service.py）
# ══════════════════════════════════════════════════════════════════

class RiskService:
    """
    独立风控守门人
    不可绕过的停止条件：
      ① Kill Switch（人工，只有重启能解除）
      ② 当日亏损 ≥ MAX_DAILY_LOSS_PCT
      ③ 连续亏损 ≥ MAX_CONSECUTIVE_LOSSES
      ④ 点差过宽
      ⑤ 数据异常
    """
    MAX_DAILY_LOSS_PCT       = float(os.getenv("SI_MAX_DAILY_LOSS_PCT", "0.03"))   # -3%
    MAX_CONSECUTIVE_LOSSES   = int(os.getenv("SI_MAX_CONSEC_LOSSES", "4"))
    MAX_RISK_PER_TRADE_PCT   = float(os.getenv("SI_RISK_PER_TRADE_PCT", "0.01"))   # 1%
    EQUITY_USDT              = float(os.getenv("SI_EQUITY", "100000"))              # 账户规模

    def __init__(self):
        self.state     = RiskState()
        self._cur_date = date.today()
        self._positions: Dict[str, Position] = {}

    # ── 核心检查 ──────────────────────────────────────────────────

    def can_trade(self, spread_pct: float = 0.0) -> Tuple[bool, str]:
        """返回 (allowed, reason)"""
        if self.state.kill_switch_active:
            return False, "KILL_SWITCH_ACTIVE"
        if self.state.is_halted:
            return False, self.state.halt_reason
        self._day_reset_check()
        if spread_pct > 0.003:  # 点差 > 0.3% 拒绝
            return False, f"SPREAD_TOO_WIDE:{spread_pct:.4f}"
        loss_pct = abs(min(0, self.state.daily_pnl)) / self.EQUITY_USDT
        if loss_pct >= self.MAX_DAILY_LOSS_PCT:
            self._halt(f"DAILY_LOSS_LIMIT:{loss_pct:.1%}")
            return False, self.state.halt_reason
        if self.state.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
            self._halt(f"CONSEC_LOSSES:{self.state.consecutive_losses}")
            return False, self.state.halt_reason
        return True, "OK"

    def position_size(self, atr: float, price: float,
                      multiplier: float = 1.0) -> float:
        """
        ATR 固定风险仓位计算
        size = (equity * risk_pct) / (1.5 * ATR * price)
        """
        risk_usdt    = self.EQUITY_USDT * self.MAX_RISK_PER_TRADE_PCT * multiplier
        stop_dist    = 1.5 * atr
        cost_per_lot = stop_dist * price   # 每手风险（USDT）
        if cost_per_lot <= 0: return 1.0
        return max(0.1, round(risk_usdt / cost_per_lot, 2))

    def register_trade(self, pnl: float):
        self.state.register(pnl)

    def activate_kill_switch(self, reason: str = "MANUAL"):
        self.state.kill_switch_active = True
        self.state.is_halted = True
        self.state.halt_reason = f"KILL_SWITCH:{reason}"

    def reset_halt(self) -> bool:
        if self.state.kill_switch_active:
            return False  # kill switch 不可自动解除
        self.state.is_halted = False
        self.state.halt_reason = ""
        return True

    def _halt(self, reason: str):
        self.state.is_halted = True
        self.state.halt_reason = reason

    def _day_reset_check(self):
        today = date.today()
        if today != self._cur_date:
            killed = self.state.kill_switch_active
            self.state = RiskState()
            self.state.kill_switch_active = killed
            self._cur_date = today

    @property
    def summary(self) -> dict:
        return {
            "kill_switch":        self.state.kill_switch_active,
            "is_halted":          self.state.is_halted,
            "halt_reason":        self.state.halt_reason,
            "daily_pnl":          round(self.state.daily_pnl, 2),
            "consecutive_losses": self.state.consecutive_losses,
            "total_trades_today": self.state.total_trades_today,
        }

# ══════════════════════════════════════════════════════════════════
#  6. 数据层：yfinance（US 指数）
# ══════════════════════════════════════════════════════════════════

# 品种映射：CrewAI 标识 → yfinance ticker
SYMBOL_MAP_US = {
    "ES":  "^GSPC",   # S&P 500
    "NQ":  "^IXIC",   # Nasdaq Composite
    "YM":  "^DJI",    # Dow Jones
    "RTY": "^RUT",    # Russell 2000
    "SPY": "SPY",     # ETF（流动性代理）
    "QQQ": "QQQ",
}
SYMBOL_MAP_CN = {
    "IF":  "000300.SS",  # 沪深300
    "IC":  "000905.SS",  # 中证500
    "IH":  "000016.SS",  # 上证50
    "IM":  "000852.SS",  # 中证1000
}
ALL_SYMBOLS = {**SYMBOL_MAP_US, **SYMBOL_MAP_CN}

# 时间止损（分钟）—— 日内策略
MAX_HOLD_MINUTES = {
    "ES": 120, "NQ": 90, "YM": 120, "RTY": 90,
    "IF": 60,  "IC": 60,  "IH": 60,  "IM": 60,
}

_OHLCV_CACHE: Dict[str, Tuple[float, Any]] = {}  # ticker → (ts, df)
_CACHE_TTL = 300  # 5分钟缓存

def _fetch_ohlcv(symbol: str, period: str = "3mo",
                 interval: str = "1d") -> Optional[Any]:
    """下载 OHLCV 数据（带缓存，减少 API 调用）"""
    cache_key = f"{symbol}:{interval}"
    now = time.time()
    if cache_key in _OHLCV_CACHE:
        ts, df = _OHLCV_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return df

    ticker = ALL_SYMBOLS.get(symbol, symbol)
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            return None
        _OHLCV_CACHE[cache_key] = (now, df)
        return df
    except Exception as e:
        return None

def _compute_indicators(df) -> Optional[Indicators]:
    """计算所有技术指标"""
    try:
        closes  = df["Close"].values.flatten().astype(float)
        highs   = df["High"].values.flatten().astype(float)
        lows    = df["Low"].values.flatten().astype(float)
        volumes = df["Volume"].values.flatten().astype(float)

        if len(closes) < 60: return None

        ema20  = TI.ema(closes, 20)
        ema50  = TI.ema(closes, 50)
        atr14  = TI.atr(highs, lows, closes, 14)
        adx14  = TI.adx(highs, lows, closes, 14)
        rsi14  = TI.rsi(closes, 14)
        bb_up, bb_mid, bb_dn = TI.bollinger(closes, 20)
        vwap_v = TI.vwap(highs, lows, closes, volumes)

        # 基准 ATR（60 周期均值）
        atr_valid = atr14[~np.isnan(atr14)]
        atr_base  = float(atr_valid[-60:].mean()) if len(atr_valid) >= 20 else float(atr14[-1]) if not np.isnan(atr14[-1]) else 1.0

        # 量比
        vol_ma = np.nanmean(volumes[-20:]) if len(volumes) >= 20 else volumes.mean()
        vol_ratio = float(volumes[-1] / vol_ma) if vol_ma > 0 else 1.0

        def lv(arr): return TI.latest(arr) or 0.0

        return Indicators(
            ema_fast     = lv(ema20),
            ema_slow     = lv(ema50),
            adx          = lv(adx14),
            atr          = lv(atr14),
            atr_baseline = atr_base,
            rsi          = lv(rsi14),
            bb_upper     = lv(bb_up),
            bb_lower     = lv(bb_dn),
            vwap         = lv(vwap_v),
            volume_ratio = vol_ratio,
        )
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════════════
#  7. 全局状态
# ══════════════════════════════════════════════════════════════════

_RISK_SVC   = RiskService()
_POSITIONS: Dict[str, Position] = {}
_KALMAN_CACHE: Dict[str, KalmanHedgeRatio] = {}  # "A:B" → KalmanHedgeRatio
_DAILY_PNL: float = 0.0
_COOLDOWN_UNTIL: float = 0.0
_SI_MODE: str = os.getenv("SI_MODE", "PAPER").upper()  # PAPER | IB | CTP

# ══════════════════════════════════════════════════════════════════
#  8. 信号引擎
# ══════════════════════════════════════════════════════════════════

def _momentum_signal(symbol: str, ind: Indicators,
                     regime: Regime, price: float) -> dict:
    """
    单品种动量/趋势信号
    ─────────────────────────────────────────────────────────
    确认条件（移植自 Oil-Model signal_service.py）：
      ① EMA 方向对齐（EMA20 > EMA50 for LONG）
      ② ADX > 22（趋势强度）
      ③ RSI 不超买超卖（20-80）
      ④ 成交量 > 0.8x 均量
      ⑤ 价格在 VWAP 正确一侧
      ⑥ 未处于 BLOCKED 状态
    """
    if regime == Regime.BLOCKED:
        return {"direction": None, "type": "MOMENTUM",
                "reason": "BLOCKED", "confidence": 0}

    conds = {}
    # 多头条件
    long_ema  = ind.ema_fast > ind.ema_slow
    short_ema = ind.ema_fast < ind.ema_slow
    adx_ok    = ind.adx > 22
    rsi_long  = 30 < ind.rsi < 70
    vol_ok    = ind.volume_ratio > 0.8
    above_vwap = price > ind.vwap
    below_vwap = price < ind.vwap

    if long_ema and adx_ok and rsi_long and vol_ok and above_vwap:
        conds = {"EMA": True, "ADX": True, "RSI": True, "VOL": True, "VWAP": True}
        conf  = 55 + min(30, ind.adx - 22) + min(10, (ind.volume_ratio - 1) * 10)
        return {"direction": "LONG", "type": "MOMENTUM",
                "confidence": round(conf, 1),
                "stop_atr_mult": 1.5, "conditions": conds,
                "reason": "EMA+ADX+VWAP alignment"}

    if short_ema and adx_ok and rsi_long and vol_ok and below_vwap:
        conds = {"EMA": True, "ADX": True, "RSI": True, "VOL": True, "VWAP": True}
        conf  = 55 + min(30, ind.adx - 22) + min(10, (ind.volume_ratio - 1) * 10)
        return {"direction": "SHORT", "type": "MOMENTUM",
                "confidence": round(conf, 1),
                "stop_atr_mult": 1.5, "conditions": conds,
                "reason": "EMA+ADX+VWAP alignment"}

    # 不满足
    failed = []
    if not adx_ok:  failed.append(f"ADX={ind.adx:.1f}<22")
    if not vol_ok:  failed.append(f"VOL_RATIO={ind.volume_ratio:.2f}")
    if not rsi_long: failed.append(f"RSI={ind.rsi:.1f}")
    return {"direction": None, "type": "MOMENTUM",
            "reason": f"NO_EDGE:{','.join(failed)}", "confidence": 0}


def _stat_arb_signal(sym_a: str, sym_b: str) -> dict:
    """
    双品种统计套利信号（Kalman + Cointegration）
    ─────────────────────────────────────────────
    流程：
      1. 下载两品种 3 个月日线数据
      2. Engle-Granger 协整检验（静态 β）
      3. Kalman 滤波器更新动态 β
      4. 计算 Z-Score：|Z| > 2.0 → 进场
         Z > +2  → SHORT A + LONG B（价差均值回归）
         Z < -2  → LONG A + SHORT B
         |Z| < 0.5 → 平仓
    """
    df_a = _fetch_ohlcv(sym_a)
    df_b = _fetch_ohlcv(sym_b)
    if df_a is None or df_b is None:
        return {"signal": "NO_DATA", "sym_a": sym_a, "sym_b": sym_b}

    # 对齐日期
    closes_a = df_a["Close"].values.flatten().astype(float)
    closes_b = df_b["Close"].values.flatten().astype(float)
    n = min(len(closes_a), len(closes_b), 90)  # 最近90天
    if n < 30:
        return {"signal": "INSUFFICIENT_DATA", "sym_a": sym_a, "sym_b": sym_b}

    ca = closes_a[-n:]
    cb = closes_b[-n:]

    # 1. 协整检验（静态参考）
    coint = engle_granger_coint(ca, cb)

    # 2. Kalman 滤波器（动态 β）
    cache_key = f"{sym_a}:{sym_b}"
    if cache_key not in _KALMAN_CACHE:
        _KALMAN_CACHE[cache_key] = KalmanHedgeRatio(delta=1e-4)
    kf = _KALMAN_CACHE[cache_key]

    # 用历史序列更新 Kalman（或使用已有状态继续更新最新一根）
    for ya, xb in zip(ca, cb):
        alpha, beta, spread = kf.update(ya, xb)

    z = kf.zscore(window=60)
    price_a = float(ca[-1])
    price_b = float(cb[-1])

    result = {
        "sym_a": sym_a, "sym_b": sym_b,
        "cointegrated":  bool(coint["cointegrated"]),  # 强制转 Python bool
        "adf_stat":      float(coint["adf_stat"]),
        "static_beta":   float(coint["beta"]),
        "kalman_beta":   round(float(kf.beta), 4),
        "kalman_alpha":  round(float(kf.alpha), 4),
        "spread":        round(float(kf.spread or 0), 4),
        "zscore":        round(float(z), 3) if z is not None else None,
        "price_a":       round(price_a, 2),
        "price_b":       round(price_b, 2),
    }

    # 信号判断
    if z is None:
        result["signal"] = "INSUFFICIENT_SPREAD_HISTORY"
        return result

    if not coint["cointegrated"] and abs(z) < 1.0:
        result["signal"] = "NOT_COINTEGRATED"
        return result

    ENTRY_Z  = 2.0
    EXIT_Z   = 0.5
    REDUCE_Z = 1.0

    if z > ENTRY_Z:
        # 价差过高：做空 A（贵），做多 B（便宜）
        result["signal"]    = "SHORT_A_LONG_B"
        result["direction_a"] = "SHORT"
        result["direction_b"] = "LONG"
        result["reason"]    = f"Z={z:.2f} > {ENTRY_Z} → 均值回归"
        result["confidence"] = min(95, 60 + (z - ENTRY_Z) * 10)

    elif z < -ENTRY_Z:
        # 价差过低：做多 A，做空 B
        result["signal"]    = "LONG_A_SHORT_B"
        result["direction_a"] = "LONG"
        result["direction_b"] = "SHORT"
        result["reason"]    = f"Z={z:.2f} < -{ENTRY_Z} → 均值回归"
        result["confidence"] = min(95, 60 + (abs(z) - ENTRY_Z) * 10)

    elif abs(z) < EXIT_Z:
        result["signal"]  = "EXIT_SPREAD_REVERTED"
        result["reason"]  = f"Z={z:.2f} → 价差均值回归，平仓"

    elif REDUCE_Z < abs(z) < ENTRY_Z:
        result["signal"] = "HOLD_MONITOR"
        result["reason"] = f"Z={z:.2f} 靠近信号阈值，继续监控"

    else:
        result["signal"] = "NO_SIGNAL"

    return result

# ══════════════════════════════════════════════════════════════════
#  9. Paper Broker（股指期货模拟执行）
# ══════════════════════════════════════════════════════════════════

import uuid

class IndexPaperBroker:
    """纸交易：开仓/平仓，记录 PnL"""

    def open_momentum(self, symbol: str, direction: str,
                      price: float, atr: float,
                      sm_mult: float = 1.0, vix_mult: float = 1.0,
                      tp_atr_mult: float = 2.0) -> dict:
        base_size  = _RISK_SVC.position_size(atr, price)
        final_size = max(0.1, round(base_size * sm_mult * vix_mult, 2))
        if direction == "LONG":
            stop        = price - 1.5 * atr
            take_profit = price + tp_atr_mult * atr
        else:
            stop        = price + 1.5 * atr
            take_profit = price - tp_atr_mult * atr
        pos = Position(
            id=str(uuid.uuid4())[:8], symbol=symbol,
            signal_type=SignalType.MOMENTUM,
            direction=Direction(direction),
            size=final_size, entry_price=price, stop_loss=stop,
            entry_ts=datetime.now(timezone.utc).isoformat(),
            take_profit=round(take_profit, 2),
        )
        _POSITIONS[symbol] = pos
        return {"action": "OPEN", "type": "MOMENTUM", "symbol": symbol,
                "direction": direction, "size": final_size, "price": price,
                "stop_loss": round(stop, 2), "take_profit": round(take_profit, 2),
                "mode": "PAPER"}

    def open_stat_arb(self, sym_a: str, dir_a: str, price_a: float,
                      sym_b: str, dir_b: str, price_b: float,
                      beta: float, atr_a: float,
                      sm_mult: float = 1.0, vix_mult: float = 1.0,
                      tp_atr_mult: float = 2.0) -> dict:
        base_size_a = _RISK_SVC.position_size(atr_a, price_a, multiplier=0.5)
        size_a      = max(0.1, round(base_size_a * sm_mult * vix_mult, 2))
        size_b      = round(size_a * beta, 2)
        if dir_a == "LONG":
            stop_a        = price_a - 1.5 * atr_a
            take_profit_a = price_a + tp_atr_mult * atr_a
        else:
            stop_a        = price_a + 1.5 * atr_a
            take_profit_a = price_a - tp_atr_mult * atr_a
        pos = Position(
            id=str(uuid.uuid4())[:8], symbol=sym_a,
            signal_type=SignalType.STAT_ARB,
            direction=Direction(dir_a),
            size=size_a, entry_price=price_a,
            stop_loss=stop_a,
            entry_ts=datetime.now(timezone.utc).isoformat(),
            pair_leg=sym_b, pair_direction=dir_b,
            pair_size=size_b, pair_entry=price_b,
            take_profit=round(take_profit_a, 2),
        )
        _POSITIONS[f"{sym_a}:{sym_b}"] = pos
        return {"action": "OPEN", "type": "STAT_ARB",
                "leg_a": {"symbol": sym_a, "dir": dir_a, "size": size_a, "price": price_a,
                          "stop_loss": round(stop_a, 2), "take_profit": round(take_profit_a, 2)},
                "leg_b": {"symbol": sym_b, "dir": dir_b, "size": size_b, "price": price_b},
                "beta": round(beta, 4), "mode": "PAPER"}

    def check_exits(self, cur_prices: Dict[str, float]) -> List[dict]:
        """检查所有持仓的退出条件，自动平仓并返回已关闭持仓列表"""
        closed = []
        for key in list(_POSITIONS.keys()):
            pos = _POSITIONS.get(key)
            if pos is None:
                continue
            sym   = pos.symbol
            p_cur = cur_prices.get(sym) or cur_prices.get(key)
            if p_cur is None:
                continue

            reason = None
            # 止损检查
            if pos.direction == Direction.LONG and p_cur <= pos.stop_loss:
                reason = "STOP_LOSS"
            elif pos.direction == Direction.SHORT and p_cur >= pos.stop_loss:
                reason = "STOP_LOSS"
            # 止盈检查
            elif pos.take_profit is not None:
                if pos.direction == Direction.LONG and p_cur >= pos.take_profit:
                    reason = "TAKE_PROFIT"
                elif pos.direction == Direction.SHORT and p_cur <= pos.take_profit:
                    reason = "TAKE_PROFIT"
            # 时间止损
            if reason is None:
                max_hold = MAX_HOLD_MINUTES.get(sym, 120)
                if pos.age_minutes >= max_hold:
                    reason = "TIME_STOP"

            if reason:
                result = self.close(key, p_cur, reason=reason)
                result["exit_reason"] = reason
                closed.append(result)
        return closed

    def close(self, key: str, cur_price_a: float,
              cur_price_b: Optional[float] = None,
              reason: str = "SIGNAL") -> dict:
        pos = _POSITIONS.pop(key, None)
        if pos is None:
            return {"action": "CLOSE", "status": "NO_POSITION", "key": key}

        if pos.signal_type == SignalType.MOMENTUM:
            mult = 1 if pos.direction == Direction.LONG else -1
            pnl  = mult * (cur_price_a - pos.entry_price) * pos.size
        else:
            mult_a = 1 if pos.direction == Direction.LONG else -1
            mult_b = 1 if pos.pair_direction == "LONG" else -1
            pnl_a  = mult_a * (cur_price_a - pos.entry_price) * pos.size
            pnl_b  = mult_b * ((cur_price_b or cur_price_a) - (pos.pair_entry or cur_price_a)) * (pos.pair_size or 0)
            pnl    = pnl_a + pnl_b

        global _DAILY_PNL, _COOLDOWN_UNTIL
        _DAILY_PNL    += pnl
        _COOLDOWN_UNTIL = time.time() + 300  # 5 分钟冷静期
        _RISK_SVC.register_trade(pnl)
        return {"action": "CLOSE", "key": key, "pnl": round(pnl, 2),
                "reason": reason, "mode": "PAPER"}

_PAPER_BROKER = IndexPaperBroker()

def _latest_price(symbol: str) -> float:
    """获取最新收盘价"""
    df = _fetch_ohlcv(symbol, period="5d", interval="1d")
    if df is None:
        return {"ES": 5200.0, "NQ": 18000.0, "IF": 3800.0, "IC": 5000.0}.get(symbol, 100.0)
    return float(df["Close"].values.flatten()[-1])

# ══════════════════════════════════════════════════════════════════
#  10. CrewAI 工具
# ══════════════════════════════════════════════════════════════════

# ── Tool 1：信号工具 ──────────────────────────────────────────────

class SISignalInput(BaseModel):
    mode: str = Field(
        default="momentum",
        description="信号模式：'momentum'（单品种趋势）或 'stat_arb'（双品种套利）"
    )
    symbols: str = Field(
        default="ES,NQ",
        description="品种列表（逗号分隔）。momentum 模式逐个分析；stat_arb 模式取前两个作为配对"
    )

class StockIndexSignalTool(BaseTool):
    name: str = "StockIndexSignalTool"
    description: str = (
        "股指期货量化信号分析工具，支持两种模式：\n\n"
        "① momentum（趋势跟随）：\n"
        "  - 下载 yfinance 数据（US：ES/NQ/YM/RTY；CN：IF/IC/IH/IM）\n"
        "  - 计算 EMA20/50、ADX14、ATR14、RSI14、VWAP、量比\n"
        "  - 五条件动态确认（EMA对齐 + ADX>22 + RSI + 量比 + VWAP位置）\n"
        "  - 输出：LONG/SHORT/NO_SIGNAL + 置信度 + 止损位\n\n"
        "② stat_arb（Kalman协整套利）：\n"
        "  - Engle-Granger 协整检验（ADF统计量）\n"
        "  - 卡尔曼滤波器动态 β 估计\n"
        "  - Z-Score 价差信号（|Z|>2进场，|Z|<0.5平仓）\n"
        "  - 输出：SHORT_A_LONG_B / LONG_A_SHORT_B / EXIT / HOLD\n\n"
        "输入示例：\n"
        "  momentum: symbols='ES,NQ,IF'\n"
        "  stat_arb: symbols='ES,NQ'"
    )
    args_schema: type[BaseModel] = SISignalInput

    def _run(self, mode: str = "momentum", symbols: str = "ES,NQ") -> str:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        ts = datetime.now(timezone.utc).isoformat()

        if mode.lower() == "stat_arb":
            if len(sym_list) < 2:
                return json.dumps({"error": "stat_arb 模式需要至少两个品种，如 'ES,NQ'"})
            result = _stat_arb_signal(sym_list[0], sym_list[1])
            # 加入风控检查
            ok, reason = _RISK_SVC.can_trade()
            result["risk_ok"]     = ok
            result["risk_reason"] = reason
            result["timestamp"]   = ts
            return json.dumps(result, ensure_ascii=False, indent=2)

        # momentum 模式
        signals = []
        for sym in sym_list:
            df = _fetch_ohlcv(sym)
            if df is None:
                signals.append({"symbol": sym, "error": "数据获取失败"})
                continue

            ind = _compute_indicators(df)
            if ind is None:
                signals.append({"symbol": sym, "error": "指标计算失败（数据不足）"})
                continue

            price  = float(df["Close"].values.flatten()[-1])
            regime = _REGIME_ENGINE.evaluate(ind)
            sig    = _momentum_signal(sym, ind, regime, price)

            ok, risk_reason = _RISK_SVC.can_trade()
            pos = _POSITIONS.get(sym)

            signals.append({
                "symbol":   sym,
                "price":    round(price, 2),
                "regime":   regime.value,
                "signal":   sig,
                "risk_ok":  ok,
                "risk_reason": risk_reason if not ok else "OK",
                "position": {
                    "open": pos is not None,
                    "direction": pos.direction.value if pos else None,
                    "age_min": round(pos.age_minutes, 1) if pos else 0,
                    "max_hold_min": MAX_HOLD_MINUTES.get(sym, 120),
                },
                "indicators": {
                    "ema20":  round(ind.ema_fast, 2),
                    "ema50":  round(ind.ema_slow, 2),
                    "adx":    round(ind.adx, 1),
                    "atr":    round(ind.atr, 2),
                    "rsi":    round(ind.rsi, 1),
                    "vol_ratio": round(ind.volume_ratio, 2),
                },
            })

        return json.dumps({
            "timestamp": ts,
            "mode": "momentum",
            "signals": signals,
        }, ensure_ascii=False, indent=2)

# ── Tool 2：风控工具 ──────────────────────────────────────────────

class SIRiskInput(BaseModel):
    action: str = Field(
        default="status",
        description="status（查询）/ kill_switch（激活紧急停止）/ reset_halt（解除停手，kill_switch除外）"
    )
    reason: str = Field(default="MANUAL", description="kill_switch 的原因说明")

class StockIndexRiskTool(BaseTool):
    name: str = "StockIndexRiskTool"
    description: str = (
        "股指期货风控管理工具：\n"
        "- status：查询当前风控状态（每日PnL、连续亏损、停手原因）\n"
        "- kill_switch：激活紧急停止（仅重启程序可解除）\n"
        "- reset_halt：解除普通风控停手（kill_switch 无效）\n\n"
        "风控参数（.env 设置）：\n"
        "  SI_MAX_DAILY_LOSS_PCT=0.03  每日最大亏损 3%\n"
        "  SI_MAX_CONSEC_LOSSES=4      连续亏损 4 笔停手\n"
        "  SI_RISK_PER_TRADE_PCT=0.01  每笔风险 1%\n"
        "  SI_EQUITY=100000            账户规模 USDT"
    )
    args_schema: type[BaseModel] = SIRiskInput

    def _run(self, action: str = "status", reason: str = "MANUAL") -> str:
        if action == "kill_switch":
            _RISK_SVC.activate_kill_switch(reason)
            return json.dumps({"action": "kill_switch", "activated": True,
                               "reason": reason}, ensure_ascii=False)
        if action == "reset_halt":
            ok = _RISK_SVC.reset_halt()
            return json.dumps({"action": "reset_halt", "success": ok,
                               "note": "Kill Switch 需重启程序" if not ok else "已解除"})
        # status
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "risk": _RISK_SVC.summary,
            "daily_pnl_total": round(_DAILY_PNL, 2),
            "open_positions":  len(_POSITIONS),
            "cooldown_active": time.time() < _COOLDOWN_UNTIL,
            "cooldown_remaining_sec": max(0, _COOLDOWN_UNTIL - time.time()),
        }, ensure_ascii=False, indent=2)

# ── Tool 3：持仓工具 ──────────────────────────────────────────────

class SIPortfolioInput(BaseModel):
    action: str = Field(
        default="list",
        description="list（查询持仓）/ close（平仓，需提供 key）"
    )
    key: str = Field(
        default="",
        description="平仓用：单品种填 'ES'，套利填 'ES:NQ'"
    )
    reason: str = Field(default="SIGNAL", description="平仓原因")

class StockIndexPortfolioTool(BaseTool):
    name: str = "StockIndexPortfolioTool"
    description: str = (
        "股指期货持仓管理工具：\n"
        "- list：查询所有持仓（含年龄、PnL、时间止损状态）\n"
        "- close：平仓指定品种（单品种 key='ES'；套利组合 key='ES:NQ'）\n\n"
        "时间止损自动检测：超过最大持仓时间时返回 TIME_STOP 警告。\n"
        "套利组合按配对 key 一并平仓两条腿。"
    )
    args_schema: type[BaseModel] = SIPortfolioInput

    def _run(self, action: str = "list", key: str = "",
             reason: str = "SIGNAL") -> str:

        if action == "close":
            if not key:
                return json.dumps({"error": "请提供 key（如 'ES' 或 'ES:NQ'）"})
            # 获取当前价格
            parts = key.split(":")
            p_a = _latest_price(parts[0])
            p_b = _latest_price(parts[1]) if len(parts) > 1 else None
            result = _PAPER_BROKER.close(key, p_a, p_b, reason)
            return json.dumps(result, ensure_ascii=False, indent=2)

        # list
        positions_out = []
        for k, pos in _POSITIONS.items():
            price_now = _latest_price(pos.symbol)
            # 未实现 PnL 估算
            mult = 1 if pos.direction == Direction.LONG else -1
            upnl = mult * (price_now - pos.entry_price) * pos.size

            # 时间止损检查
            max_hold = MAX_HOLD_MINUTES.get(pos.symbol, 120)
            time_stop = pos.age_minutes >= max_hold

            # 止损检查
            price_stop = (price_now <= pos.stop_loss
                          if pos.direction == Direction.LONG
                          else price_now >= pos.stop_loss)

            # 止盈检查
            tp = pos.take_profit
            price_tp = False
            if tp is not None:
                price_tp = (price_now >= tp if pos.direction == Direction.LONG
                            else price_now <= tp)

            positions_out.append({
                "key": k,
                "symbol": pos.symbol,
                "type": pos.signal_type.value,
                "direction": pos.direction.value,
                "size": pos.size,
                "entry_price": pos.entry_price,
                "current_price": round(price_now, 2),
                "stop_loss": round(pos.stop_loss, 2),
                "take_profit": round(tp, 2) if tp is not None else None,
                "unrealized_pnl": round(upnl, 2),
                "age_minutes": round(pos.age_minutes, 1),
                "max_hold_minutes": max_hold,
                "alerts": {
                    "TIME_STOP":   time_stop,
                    "STOP_LOSS":   price_stop,
                    "TAKE_PROFIT": price_tp,
                },
                "pair": {
                    "symbol": pos.pair_leg,
                    "direction": pos.pair_direction,
                    "size": pos.pair_size,
                } if pos.pair_leg else None,
            })

        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "open_positions": len(_POSITIONS),
            "positions": positions_out,
            "daily_pnl": round(_DAILY_PNL, 2),
        }, ensure_ascii=False, indent=2)


# ── Tool 5：执行工具（核心机器人）──────────────────────────────────

class SITradeInput(BaseModel):
    action: str = Field(
        default="status",
        description=(
            "'long <SYM>'    — 对指定品种开多仓（如 'long ES'）\n"
            "'short <SYM>'   — 对指定品种开空仓（如 'short NQ'）\n"
            "'stat_arb <A> <B>' — 开统计套利（如 'stat_arb ES NQ'）\n"
            "'close <KEY>'   — 平仓（如 'close ES' 或 'close ES:NQ'）\n"
            "'close_all'     — 平仓全部持仓\n"
            "'scan'          — 扫描所有品种：检查平仓条件+生成+执行新信号\n"
            "'status'        — 完整交易状态快照\n"
            "'reset_monitor' — 手动重置 StrategyMonitor COOLDOWN"
        )
    )
    symbols: str = Field(default="", description="scan时可覆盖品种列表，如 'ES,NQ,IF'")
    mode: str    = Field(default="", description="覆盖执行模式：PAPER / IB / CTP（空=读.env）")
    confidence_threshold: float = Field(default=60.0, description="scan自动执行信号置信度门槛")


class StockIndexTradeTool(BaseTool):
    name: str = "StockIndexTradeTool"
    description: str = (
        "股指期货执行引擎（第5工具）。\n\n"
        "支持操作：\n"
        "  • long/short <SYM>  — 开单（自动通过 VIX/EventCalendar/StrategyMonitor 门控）\n"
        "  • stat_arb <A> <B>  — 统计套利开单（Kalman协整信号）\n"
        "  • close <KEY>       — 手动平仓（可指定 ES 或 ES:NQ）\n"
        "  • close_all         — 全部平仓\n"
        "  • scan              — 自动扫描+执行（检查退出+生成新信号）\n"
        "  • status            — 系统状态快照（持仓/PnL/门控/风控）\n"
        "  • reset_monitor     — 重置 StrategyMonitor COOLDOWN\n\n"
        "执行模式：PAPER（默认）| IB（Interactive Brokers）| CTP（中国期货）\n"
        "Paper broker 始终运行用于状态追踪；IB/CTP 是附加执行层。"
    )
    args_schema: type[BaseModel] = SITradeInput

    def _run(self, action: str = "status", symbols: str = "",
             mode: str = "", confidence_threshold: float = 60.0) -> str:
        try:
            return json.dumps(
                self._execute(action.strip(), symbols.strip(), mode.strip(), confidence_threshold),
                ensure_ascii=False, indent=2
            )
        except Exception as e:
            return json.dumps({"error": str(e), "action": action}, ensure_ascii=False)

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _gate_check(self) -> Tuple[bool, str]:
        """多层入场门控"""
        if _STRATEGY_MONITOR.state == "COOLDOWN":
            return False, "STRATEGY_COOLDOWN"
        if _EVENT_CALENDAR.is_event_active():
            return False, "EVENT_WINDOW_ACTIVE"
        vix_r = _VIX_FILTER.check()
        if not vix_r["allowed"]:
            return False, f"VIX_BLOCKED:{vix_r['vix']:.1f}"
        ok, reason = _RISK_SVC.can_trade()
        if not ok:
            return False, reason
        return True, "OK"

    def _size_multipliers(self) -> Tuple[float, float]:
        sm_mult  = _STRATEGY_MONITOR.size_multiplier
        vix_mult = _VIX_FILTER.check()["multiplier"]
        return sm_mult, vix_mult

    def _resolve_mode(self, mode_override: str) -> str:
        return mode_override.upper() if mode_override else _SI_MODE

    def _execute(self, action: str, symbols: str, mode: str,
                 confidence_threshold: float) -> dict:
        exe_mode = self._resolve_mode(mode)
        action_lower = action.lower()

        # ── status ──────────────────────────────────────────────
        if action_lower == "status":
            gate_ok, gate_reason = self._gate_check()
            next_name, next_dt = _EVENT_CALENDAR.next_event()
            return {
                "timestamp":        datetime.now(timezone.utc).isoformat(),
                "mode":             exe_mode,
                "strategy_monitor": _STRATEGY_MONITOR.summary,
                "risk_service":     _RISK_SVC.summary,
                "event_calendar":   {
                    "active":     _EVENT_CALENDAR.is_event_active(),
                    "next_event": str(next_name),
                },
                "vix_filter":       _VIX_FILTER.check(),
                "open_positions":   {
                    "count": len(_POSITIONS),
                    "keys":  list(_POSITIONS.keys()),
                },
                "daily_pnl":        round(_DAILY_PNL, 2),
                "gates_ok":         {"allowed": gate_ok, "reason": gate_reason},
            }

        # ── reset_monitor ────────────────────────────────────────
        if action_lower == "reset_monitor":
            ok = _STRATEGY_MONITOR.reset_cooldown()
            return {"action": "reset_monitor", "success": ok,
                    "state": _STRATEGY_MONITOR.state}

        # ── close_all ────────────────────────────────────────────
        if action_lower == "close_all":
            results = []
            for key in list(_POSITIONS.keys()):
                sym  = _POSITIONS[key].symbol
                p    = _latest_price(sym)
                res  = _PAPER_BROKER.close(key, p, reason="MANUAL")
                if res.get("pnl") is not None:
                    _STRATEGY_MONITOR.on_trade(res["pnl"])
                results.append(res)
            return {"action": "close_all", "closed": len(results), "results": results}

        # ── close <KEY> ──────────────────────────────────────────
        if action_lower.startswith("close "):
            key = action[6:].strip().upper()
            pos = _POSITIONS.get(key)
            if pos is None:
                return {"action": "close", "status": "NO_POSITION", "key": key}
            p_a = _latest_price(pos.symbol)
            p_b = _latest_price(pos.pair_leg) if pos.pair_leg else None
            result = _PAPER_BROKER.close(key, p_a, p_b, reason="MANUAL")
            if result.get("pnl") is not None:
                _STRATEGY_MONITOR.on_trade(result["pnl"])
            return result

        # ── long / short <SYM> ───────────────────────────────────
        if action_lower.startswith("long ") or action_lower.startswith("short "):
            parts     = action.split()
            direction = parts[0].upper()
            sym       = parts[1].upper() if len(parts) > 1 else ""
            if not sym:
                return {"error": "Missing symbol", "action": action}

            gate_ok, gate_reason = self._gate_check()
            if not gate_ok:
                return {"action": action, "status": "GATE_BLOCKED", "reason": gate_reason}

            df = _fetch_ohlcv(sym)
            if df is None:
                return {"action": action, "status": "NO_DATA", "symbol": sym}
            ind = _compute_indicators(df)
            if ind is None:
                return {"action": action, "status": "INDICATOR_ERROR", "symbol": sym}

            price  = float(df["Close"].values.flatten()[-1])
            regime = _REGIME_ENGINE.evaluate(ind)
            sig    = _momentum_signal(sym, ind, regime, price)

            if sig.get("direction") != direction:
                return {
                    "action": action, "status": "NO_SIGNAL",
                    "symbol": sym, "signal_direction": sig.get("direction"),
                    "wanted": direction, "reason": sig.get("reason"),
                }

            sm_mult, vix_mult = self._size_multipliers()
            result = _PAPER_BROKER.open_momentum(sym, direction, price, ind.atr,
                                                 sm_mult, vix_mult)
            if exe_mode == "IB":
                ib_r = _IB_EXECUTOR.place_bracket_order(
                    sym, direction, int(result["size"]),
                    result["take_profit"], result["stop_loss"],
                )
                result["ib_order"] = ib_r
            elif exe_mode == "CTP":
                ctp_r = _CTP_EXECUTOR.place_order(
                    sym, direction, int(result["size"]), price
                )
                result["ctp_order"] = ctp_r
            result["signal"]     = sig
            result["sm_mult"]    = sm_mult
            result["vix_mult"]   = vix_mult
            return result

        # ── stat_arb <A> <B> ────────────────────────────────────
        if action_lower.startswith("stat_arb"):
            parts = action.split()
            if len(parts) < 3:
                return {"error": "Usage: stat_arb <SYM_A> <SYM_B>", "action": action}
            sym_a = parts[1].upper()
            sym_b = parts[2].upper()

            gate_ok, gate_reason = self._gate_check()
            if not gate_ok:
                return {"action": action, "status": "GATE_BLOCKED", "reason": gate_reason}

            sig = _stat_arb_signal(sym_a, sym_b)
            if sig.get("signal") not in ("SHORT_A_LONG_B", "LONG_A_SHORT_B"):
                return {"action": action, "status": "NO_SIGNAL",
                        "sym_a": sym_a, "sym_b": sym_b,
                        "signal": sig.get("signal"), "zscore": sig.get("zscore")}

            dir_a = sig["direction_a"]
            dir_b = sig["direction_b"]
            p_a   = sig["price_a"]
            p_b   = sig["price_b"]
            beta  = sig["kalman_beta"]

            # atr for leg A
            df_a = _fetch_ohlcv(sym_a)
            atr_a = 1.0
            if df_a is not None:
                ind_a = _compute_indicators(df_a)
                if ind_a:
                    atr_a = ind_a.atr

            sm_mult, vix_mult = self._size_multipliers()
            result = _PAPER_BROKER.open_stat_arb(sym_a, dir_a, p_a,
                                                  sym_b, dir_b, p_b,
                                                  beta, atr_a, sm_mult, vix_mult)
            result["signal"]   = sig
            result["sm_mult"]  = sm_mult
            result["vix_mult"] = vix_mult
            return result

        # ── scan ────────────────────────────────────────────────
        if action_lower == "scan":
            default_syms = os.getenv("SI_DEFAULT_SYMBOLS", "ES,NQ")
            sym_list = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
                        if symbols else
                        [s.strip().upper() for s in default_syms.split(",") if s.strip()])

            # 1. 检查所有持仓退出
            price_map = {}
            for key, pos in list(_POSITIONS.items()):
                price_map[pos.symbol] = _latest_price(pos.symbol)
            exits_closed = _PAPER_BROKER.check_exits(price_map)
            for ex in exits_closed:
                if ex.get("pnl") is not None:
                    _STRATEGY_MONITOR.on_trade(ex["pnl"])

            # 2. 门控检查
            gate_ok, gate_reason = self._gate_check()
            new_positions = []
            skipped       = []

            if gate_ok:
                sm_mult, vix_mult = self._size_multipliers()
                for sym in sym_list:
                    # 已有持仓则跳过
                    if sym in _POSITIONS:
                        skipped.append({"symbol": sym, "reason": "POSITION_EXISTS"})
                        continue
                    # 检查是否已有套利持仓涉及该品种
                    if any(sym in k for k in _POSITIONS):
                        skipped.append({"symbol": sym, "reason": "ARBED_POSITION_EXISTS"})
                        continue

                    df = _fetch_ohlcv(sym)
                    if df is None:
                        skipped.append({"symbol": sym, "reason": "NO_DATA"})
                        continue
                    ind = _compute_indicators(df)
                    if ind is None:
                        skipped.append({"symbol": sym, "reason": "INDICATOR_ERROR"})
                        continue

                    price  = float(df["Close"].values.flatten()[-1])
                    regime = _REGIME_ENGINE.evaluate(ind)
                    sig    = _momentum_signal(sym, ind, regime, price)

                    conf = sig.get("confidence", 0)
                    direction = sig.get("direction")
                    if direction is None or conf < confidence_threshold:
                        skipped.append({"symbol": sym,
                                        "reason": sig.get("reason", "LOW_CONFIDENCE"),
                                        "confidence": conf, "direction": direction})
                        continue

                    result = _PAPER_BROKER.open_momentum(sym, direction, price, ind.atr,
                                                         sm_mult, vix_mult)
                    result["signal"]    = sig
                    result["sm_mult"]   = sm_mult
                    result["vix_mult"]  = vix_mult
                    if exe_mode == "IB":
                        result["ib_order"] = _IB_EXECUTOR.place_bracket_order(
                            sym, direction, int(result["size"]),
                            result["take_profit"], result["stop_loss"],
                        )
                    elif exe_mode == "CTP":
                        result["ctp_order"] = _CTP_EXECUTOR.place_order(
                            sym, direction, int(result["size"]), price
                        )
                    new_positions.append(result)
            else:
                skipped.append({"symbol": "ALL", "reason": gate_reason})

            return {
                "action":        "scan",
                "timestamp":     datetime.now(timezone.utc).isoformat(),
                "mode":          exe_mode,
                "symbols_scanned": sym_list,
                "exits_closed":  exits_closed,
                "new_positions": new_positions,
                "skipped":       skipped,
                "gate_ok":       gate_ok,
                "gate_reason":   gate_reason,
                "daily_pnl":     round(_DAILY_PNL, 2),
            }

        return {"error": f"Unknown action: '{action}'",
                "hint": "Use: long/short <SYM>, stat_arb <A> <B>, close <KEY>, "
                        "close_all, scan, status, reset_monitor"}


# ══════════════════════════════════════════════════════════════════
#  10. 半衰期估计（OU 过程 / Ornstein-Uhlenbeck）
# ══════════════════════════════════════════════════════════════════

class HalfLifeEstimator:
    """
    均值回归半衰期（OU 过程）
    ─────────────────────────────────────────────────────────────────
    模型：dε_t = λ(μ - ε_t)dt + σ dW_t
    离散近似：Δε_t = a + b·ε_{t-1} + ν_t   (OLS 回归)
    b < 0 表示均值回归；半衰期 = -ln(2) / b

    实用解读：
      half_life < 5 天  → 短线套利（日内或2-3日）
      5-20 天          → 周线套利
      > 20 天          → 跨月，慎用
      b ≥ 0            → 无均值回归特性，不适合统计套利
    """

    @staticmethod
    def estimate(spread: np.ndarray) -> dict:
        """
        输入：价差序列（已去趋势，即 Kalman/EG 残差）
        输出：half_life（天）、lambda（均值回归速度）、mu（均值）、
              sigma（波动率）、is_mean_reverting
        """
        if len(spread) < 10:
            return {"error": "数据不足（<10根）", "half_life": None,
                    "is_mean_reverting": False}

        eps   = spread.astype(float)
        lag   = eps[:-1]
        delta = np.diff(eps)

        # OLS：Δε = a + b·ε_{t-1}
        X = np.column_stack([np.ones(len(lag)), lag])
        try:
            coeffs, resid_ss, _, _ = np.linalg.lstsq(X, delta, rcond=None)
        except Exception as e:
            return {"error": str(e), "half_life": None, "is_mean_reverting": False}

        a, b = float(coeffs[0]), float(coeffs[1])

        if b >= 0:
            return {
                "half_life":       None,
                "lambda":          round(b, 6),
                "mu":              round(-a / b if b != 0 else 0, 4),
                "sigma":           round(float(np.std(delta)), 4),
                "is_mean_reverting": False,
                "note":            "b≥0，序列无均值回归特性",
            }

        half_life = -math.log(2) / b
        mu        = -a / b  # 均值（ε 趋向 μ）

        # R² 拟合优度
        y_hat = X @ coeffs
        ss_res = float(np.sum((delta - y_hat) ** 2))
        ss_tot = float(np.sum((delta - delta.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        return {
            "half_life":         round(half_life, 2),     # 天
            "lambda":            round(b, 6),              # 均值回归速度
            "mu":                round(mu, 4),             # 长期均值
            "sigma":             round(float(np.std(delta)), 4),
            "r2":                round(r2, 4),
            "is_mean_reverting": True,
            "optimal_hold_days": round(half_life * 1.5, 1),  # 推荐持仓时间
            "note":              (
                "短线套利（<5天）" if half_life < 5
                else "周线套利（5-20天）" if half_life < 20
                else "跨月，风险较高"
            ),
        }

    @staticmethod
    def from_kalman(kf: "KalmanHedgeRatio") -> dict:
        """直接从 Kalman 滤波器的价差历史估计半衰期"""
        hist = list(kf._spread_history)
        if not hist:
            return {"error": "Kalman 尚无历史数据"}
        return HalfLifeEstimator.estimate(np.array(hist))


# ══════════════════════════════════════════════════════════════════
#  11. Hurst 指数（R/S 分析）
# ══════════════════════════════════════════════════════════════════

class HurstExponent:
    """
    Hurst 指数（R/S 分析，Rescaled Range）
    ─────────────────────────────────────────────────────────────
    H < 0.4  → 强均值回归（适合统计套利）
    H ≈ 0.5  → 随机游走（无显著方向性）
    H > 0.6  → 趋势持续（适合趋势跟随策略）

    计算方法：
      对不同滞后 τ = 4, 8, 16, ..., N/4
      计算 R/S(τ) = range(cumdev) / std(series_window)
      对 log(R/S) 关于 log(τ) 线性回归，斜率即为 H
    """

    @staticmethod
    def compute(series: np.ndarray, min_lags: int = 4) -> dict:
        prices = series.astype(float)
        n = len(prices)
        if n < 20:
            return {"hurst": None, "error": "数据不足（<20）"}

        # 构建滞后列表（2的幂次）
        lags = []
        tau = 4
        while tau <= n // 4:
            lags.append(tau)
            tau *= 2

        if len(lags) < min_lags:
            # 如果2的幂次太少，用等差间隔
            lags = list(range(4, n // 4, max(1, (n // 4 - 4) // 8)))[:8]

        if len(lags) < 2:
            return {"hurst": None, "error": "序列太短"}

        rs_vals = []
        valid_lags = []
        for lag in lags:
            # 切分为不重叠的窗口
            n_windows = n // lag
            if n_windows < 2:
                continue
            rs_list = []
            for w in range(n_windows):
                chunk = prices[w * lag: (w + 1) * lag]
                mean_c = chunk.mean()
                devs   = np.cumsum(chunk - mean_c)
                r      = devs.max() - devs.min()
                s      = chunk.std(ddof=1)
                if s > 0:
                    rs_list.append(r / s)
            if rs_list:
                rs_vals.append(float(np.mean(rs_list)))
                valid_lags.append(lag)

        if len(valid_lags) < 2:
            return {"hurst": None, "error": "有效滞后不足"}

        log_lags = np.log(valid_lags)
        log_rs   = np.log(rs_vals)

        # OLS 拟合斜率
        X = np.column_stack([np.ones(len(log_lags)), log_lags])
        try:
            coeffs = np.linalg.lstsq(X, log_rs, rcond=None)[0]
        except Exception:
            return {"hurst": None, "error": "回归失败"}

        H = float(coeffs[1])
        H_clipped = max(0.0, min(1.0, H))

        if H_clipped < 0.4:
            regime_type = "mean_reverting"
            interpretation = "强均值回归 — 适合统计套利"
        elif H_clipped < 0.6:
            regime_type = "random_walk"
            interpretation = "随机游走 — 无显著方向性"
        else:
            regime_type = "trending"
            interpretation = "趋势持续 — 适合趋势跟随"

        return {
            "hurst":          round(H_clipped, 4),
            "regime_type":    regime_type,
            "interpretation": interpretation,
            "n_lags":         len(valid_lags),
            "lag_range":      f"{valid_lags[0]}~{valid_lags[-1]}",
        }


# ══════════════════════════════════════════════════════════════════
#  12. Johansen 协整检验（简化迹统计量，纯 numpy）
# ══════════════════════════════════════════════════════════════════

class JohansenTest:
    """
    Johansen 迹检验（Trace Test）— 简化版，纯 numpy
    ─────────────────────────────────────────────────────────────
    支持 2-4 个资产的多变量协整分析
    零假设 H₀(r): 至多 r 个协整向量
    迹统计量 λ_trace(r) = -T·Σ ln(1 - λᵢ)

    5% 临界值（Osterwald-Lenum 1992）：
      r=0: 15.41（2变量），29.68（3变量），47.21（4变量）
      r=1: 3.76 （2变量），15.41（3变量），29.68（4变量）

    注：完整实现需 statsmodels；此版本用 VAR + 特征值近似，
        足够实盘参考，精确研究请用 statsmodels.tsa.johansen
    """

    # 5% 临界值 (k=2,3,4 变量, r=0,1,2,3)
    CRITICAL_5PCT = {
        2: {0: 15.41, 1: 3.76},
        3: {0: 29.68, 1: 15.41, 2: 3.76},
        4: {0: 47.21, 1: 29.68, 2: 15.41, 3: 3.76},
    }

    @staticmethod
    def test(price_matrix: np.ndarray, lag: int = 1) -> dict:
        """
        price_matrix: shape (T, k)，每列是一个资产的价格序列
        lag: VAR 滞后阶数（默认1）

        返回：
          rank         — 协整秩（即协整关系数量）
          trace_stats  — 各 r 的迹统计量列表
          crit_5pct    — 对应 5% 临界值
          cointegrated — 是否存在至少一个协整关系
        """
        T, k = price_matrix.shape
        if T < 50 or k < 2:
            return {"error": f"数据不足 (T={T}, k={k})", "rank": 0, "cointegrated": False}
        if k > 4:
            return {"error": "最多支持4个资产", "rank": 0, "cointegrated": False}

        # 一阶差分
        diffs = np.diff(price_matrix, axis=0)          # (T-1, k)
        levels = price_matrix[:-1]                      # (T-1, k) lagged levels

        # 用 VAR 残差近似：regress diff on lagged levels（无常数，简化版）
        try:
            # 残差矩阵 R0（diff 对 diff lags 的残差）和 R1（levels 对 diff lags）
            # 简化：直接用 diffs 和 levels（假设 lag=1，无额外 diff lags）
            R0 = diffs - diffs.mean(axis=0)
            R1 = levels - levels.mean(axis=0)

            T1 = len(R0)
            S00 = (R0.T @ R0) / T1
            S11 = (R1.T @ R1) / T1
            S01 = (R0.T @ R1) / T1
            S10 = S01.T

            # 求解广义特征值问题：S10 S00^{-1} S01 v = λ S11 v
            try:
                S00_inv = np.linalg.inv(S00)
            except np.linalg.LinAlgError:
                S00_inv = np.linalg.pinv(S00)

            M = np.linalg.solve(S11, S10 @ S00_inv @ S01)
            eigenvalues = np.linalg.eigvals(M)
            eigenvalues = np.real(eigenvalues)
            eigenvalues = np.sort(eigenvalues)[::-1]  # 降序
            eigenvalues = np.clip(eigenvalues, 0, 1 - 1e-10)

        except Exception as e:
            return {"error": f"特征值计算失败: {e}", "rank": 0, "cointegrated": False}

        crits = JohansenTest.CRITICAL_5PCT.get(k, {})
        T_eff = T1

        # 迹统计量：λ_trace(r) = -T Σ_{i=r+1}^{k} ln(1 - λᵢ)
        trace_stats = []
        ranks_pass  = []
        for r in range(k):
            stat = float(-T_eff * np.sum(np.log(1 - eigenvalues[r:])))
            crit = crits.get(r, 999.0)
            passes = stat > crit
            trace_stats.append({
                "r":          r,
                "H0":         f"rank ≤ {r}",
                "trace_stat": round(stat, 3),
                "crit_5pct":  crit,
                "reject_H0":  passes,
            })
            if passes:
                ranks_pass.append(r)

        cointegrated = len(ranks_pass) > 0
        rank = max(ranks_pass) + 1 if ranks_pass else 0

        return {
            "k":              k,
            "T":              T_eff,
            "rank":           rank,
            "cointegrated":   cointegrated,
            "eigenvalues":    [round(float(e), 6) for e in eigenvalues],
            "trace_stats":    trace_stats,
            "interpretation": (
                f"存在 {rank} 个协整关系" if cointegrated
                else "未发现协整关系"
            ),
        }


# ══════════════════════════════════════════════════════════════════
#  13. 简化 GARCH(1,1) 波动率预测
# ══════════════════════════════════════════════════════════════════

class SimpleGARCH:
    """
    GARCH(1,1) 简化实现（矩法估计，非MLE）
    ─────────────────────────────────────────────────────────────
    模型：σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}
    约束：α + β < 1（方差平稳）

    实用说明：
      - 用于估计明日条件波动率
      - VaR 计算时用预测 σ 替代历史 σ
      - α+β 越接近 1 → 波动冲击持续时间越长
    """

    def __init__(self):
        self.omega: float = 0.0
        self.alpha: float = 0.0
        self.beta:  float = 0.0
        self._fitted = False
        self._last_sigma2: float = 1.0

    def fit(self, returns: np.ndarray) -> dict:
        """用矩法近似估计 GARCH(1,1) 参数"""
        r = returns.astype(float)
        n = len(r)
        if n < 30:
            return {"error": "数据不足（<30）", "fitted": False}

        sigma2 = float(np.var(r))   # 无条件方差
        # 自相关系数近似 α+β（平方收益率的一阶自相关）
        r2 = r ** 2
        ac1 = float(np.corrcoef(r2[:-1], r2[1:])[0, 1]) if n > 1 else 0.3
        ac1 = max(0.0, min(0.97, ac1))

        # 矩法：α + β ≈ ac1（平方收益自相关）
        # 经验：β ≈ 0.85 for stock indices
        beta  = min(0.88, ac1 * 0.9)
        alpha = max(0.02, min(0.15, ac1 - beta))
        omega = sigma2 * (1 - alpha - beta)

        self.omega  = max(1e-10, omega)
        self.alpha  = alpha
        self.beta   = beta
        self._fitted = True

        # 用递推计算最新条件方差
        sig2 = sigma2
        for i in range(1, n):
            sig2 = self.omega + self.alpha * r[i-1]**2 + self.beta * sig2
        self._last_sigma2 = max(1e-10, sig2)

        persistence = alpha + beta
        half_life = math.log(0.5) / math.log(persistence) if persistence < 1 else float('inf')

        return {
            "omega":       round(omega, 8),
            "alpha":       round(alpha, 4),
            "beta":        round(beta, 4),
            "persistence": round(persistence, 4),
            "half_life_days": round(half_life, 1),
            "uncond_vol_pct": round(math.sqrt(sigma2) * 100, 4),
            "fitted":      True,
        }

    def forecast(self, h: int = 1) -> dict:
        """预测 h 步后的条件波动率"""
        if not self._fitted:
            return {"error": "模型尚未拟合"}
        persistence = self.alpha + self.beta
        uncond_var  = self.omega / max(1e-12, 1 - persistence)
        sig2 = self._last_sigma2
        forecasts = []
        for i in range(1, h + 1):
            if i == 1:
                sig2_h = self.omega + self.alpha * sig2 / (self.alpha + self.beta) + self.beta * sig2
            else:
                sig2_h = uncond_var + persistence**(i-1) * (sig2 - uncond_var)
            sig2_h = max(1e-10, sig2_h)
            forecasts.append(round(math.sqrt(sig2_h) * 100, 4))  # 转为 %
        return {
            "h1_vol_pct":  forecasts[0] if forecasts else None,
            "forecasts":   forecasts,
            "persistence": round(persistence, 4),
            "current_vol_pct": round(math.sqrt(self._last_sigma2) * 100, 4),
        }


# ══════════════════════════════════════════════════════════════════
#  14. 绩效分析（Sharpe / Sortino / MaxDD / Calmar / 盈亏比）
# ══════════════════════════════════════════════════════════════════

class PerformanceMetrics:
    """
    量化策略绩效指标计算器
    ─────────────────────────────────────────────────────────────
    输入：PnL 序列（美元，每笔交易）或权益曲线（累计净值）
    输出：业界标准的绩效指标集合
    """

    @staticmethod
    def from_trades(pnl_list: list, equity: float = 100_000) -> dict:
        """
        从交易 PnL 列表计算绩效
        pnl_list: [pnl_trade1, pnl_trade2, ...]（美元）
        equity: 初始账户规模（用于计算收益率）
        """
        if not pnl_list:
            return {"error": "无交易记录"}

        returns = np.array(pnl_list, dtype=float) / equity
        n = len(returns)
        wins   = returns[returns > 0]
        losses = returns[returns < 0]

        # Sharpe（假设日频，252 交易日年化）
        mu   = float(returns.mean())
        sig  = float(returns.std(ddof=1)) if n > 1 else 1e-10
        sharpe = (mu / sig * math.sqrt(252)) if sig > 0 else 0.0

        # Sortino
        down_sig = float(returns[returns < 0].std(ddof=1)) if len(losses) > 1 else 1e-10
        sortino  = (mu / down_sig * math.sqrt(252)) if down_sig > 0 else 0.0

        # 最大回撤
        equity_curve = equity + np.cumsum(pnl_list)
        running_max  = np.maximum.accumulate(equity_curve)
        drawdowns    = (running_max - equity_curve) / running_max
        max_dd       = float(drawdowns.max()) if len(drawdowns) > 0 else 0.0

        # Calmar（年化收益 / MaxDD）
        total_ret    = float(equity_curve[-1] - equity) / equity
        # 假设 n 笔交易约等于 n/5 周 = n/260 年
        years_est    = max(0.01, n / 260)
        ann_ret      = (1 + total_ret) ** (1 / years_est) - 1
        calmar       = ann_ret / max_dd if max_dd > 0 else 0.0

        # 盈亏比
        avg_win  = float(wins.mean())  if len(wins)   > 0 else 0.0
        avg_loss = float(abs(losses.mean())) if len(losses) > 0 else 1e-10
        profit_factor = (
            float(wins.sum()) / abs(float(losses.sum()))
            if len(losses) > 0 and losses.sum() != 0 else 0.0
        )

        return {
            "n_trades":      n,
            "win_rate":      round(len(wins) / n * 100, 1),
            "avg_win_pct":   round(avg_win * 100, 3),
            "avg_loss_pct":  round(-avg_loss * 100, 3),
            "profit_factor": round(profit_factor, 3),
            "sharpe":        round(sharpe, 3),
            "sortino":       round(sortino, 3),
            "max_drawdown":  round(max_dd * 100, 2),
            "calmar":        round(calmar, 3),
            "total_return":  round(total_ret * 100, 2),
            "ann_return_est":round(ann_ret * 100, 2),
            "final_equity":  round(float(equity_curve[-1]), 2),
            "note": (
                "优秀" if sharpe > 1.5 and max_dd < 0.15
                else "良好" if sharpe > 1.0
                else "一般" if sharpe > 0.5
                else "需改善"
            ),
        }

    @staticmethod
    def from_equity_curve(equity_curve: np.ndarray) -> dict:
        """从权益曲线（每日净值）计算绩效"""
        if len(equity_curve) < 5:
            return {"error": "数据不足"}
        returns = np.diff(equity_curve) / equity_curve[:-1]
        mu  = float(returns.mean())
        sig = float(returns.std(ddof=1)) if len(returns) > 1 else 1e-10
        sharpe = mu / sig * math.sqrt(252) if sig > 0 else 0.0
        down_sig = float(returns[returns < 0].std(ddof=1)) if np.any(returns < 0) else 1e-10
        sortino  = mu / down_sig * math.sqrt(252) if down_sig > 0 else 0.0
        running_max = np.maximum.accumulate(equity_curve)
        dd = (running_max - equity_curve) / running_max
        max_dd = float(dd.max())
        total_ret = float(equity_curve[-1] - equity_curve[0]) / equity_curve[0]
        years = max(0.01, len(equity_curve) / 252)
        ann_ret = (1 + total_ret) ** (1 / years) - 1
        return {
            "sharpe":        round(sharpe, 3),
            "sortino":       round(sortino, 3),
            "max_drawdown":  round(max_dd * 100, 2),
            "calmar":        round(ann_ret / max_dd, 3) if max_dd > 0 else 0.0,
            "total_return":  round(total_ret * 100, 2),
            "ann_return":    round(ann_ret * 100, 2),
            "n_days":        len(equity_curve),
        }


# ══════════════════════════════════════════════════════════════════
#  15. 组合 VaR / CVaR（历史模拟法）
# ══════════════════════════════════════════════════════════════════

class PortfolioVaR:
    """
    历史模拟法 VaR（不依赖正态假设）
    ─────────────────────────────────────────────────────────────
    支持单资产和多资产组合
    Delta 近似：每个头寸按方向和手数折算美元敞口

    CL 合约：$1000/点；ES 合约：$50/点；NQ 合约：$20/点
    """

    CONTRACT_MULT = {
        "ES": 50, "NQ": 20, "YM": 5, "RTY": 50,
        "IF": 300, "IC": 200, "IH": 300, "IM": 200,
        "CL": 1000, "GC": 100, "SI": 5000,
    }

    @staticmethod
    def single_asset(
        symbol: str,
        price: float,
        lots: float,
        direction: str = "LONG",
        lookback_days: int = 90,
        confidence: float = 0.95,
    ) -> dict:
        """单资产 VaR"""
        mult  = PortfolioVaR.CONTRACT_MULT.get(symbol, 50)
        sign  = 1 if direction.upper() == "LONG" else -1
        notional = price * lots * mult * sign

        # 获取历史日线
        df = _fetch_ohlcv(symbol, period=f"{lookback_days + 10}d", interval="1d")
        if df is None:
            return {"error": f"无法获取 {symbol} 数据"}

        closes = df["Close"].values.flatten().astype(float)
        if len(closes) < 20:
            return {"error": "历史数据不足"}

        ret = np.diff(np.log(closes[-lookback_days:]))
        pnl_hist = ret * abs(notional)  # 历史情景下的 PnL

        if direction.upper() == "SHORT":
            pnl_hist = -pnl_hist

        var_pct  = np.percentile(pnl_hist, (1 - confidence) * 100)
        cvar_pct = float(pnl_hist[pnl_hist <= var_pct].mean()) if np.any(pnl_hist <= var_pct) else var_pct

        ann_vol = float(np.std(ret)) * math.sqrt(252) * 100

        return {
            "symbol":       symbol,
            "lots":         lots,
            "direction":    direction,
            "notional_usd": round(notional, 0),
            f"var_{int(confidence*100)}_usd":  round(abs(var_pct), 2),
            f"cvar_{int(confidence*100)}_usd": round(abs(cvar_pct), 2),
            "ann_vol_pct":  round(ann_vol, 2),
            "confidence":   confidence,
            "lookback":     f"{lookback_days}d",
        }

    @staticmethod
    def portfolio(positions_dict: dict, lookback_days: int = 90,
                  confidence: float = 0.95) -> dict:
        """
        多资产组合 VaR（考虑相关性）
        positions_dict: {"ES": {"lots": 1, "direction": "LONG", "price": 5200}, ...}
        """
        results = {}
        all_returns = {}

        for sym, pos in positions_dict.items():
            df = _fetch_ohlcv(sym, period=f"{lookback_days + 10}d", interval="1d")
            if df is None:
                continue
            closes = df["Close"].values.flatten().astype(float)
            if len(closes) < 20:
                continue
            ret = np.diff(np.log(closes[-lookback_days:]))
            mult  = PortfolioVaR.CONTRACT_MULT.get(sym, 50)
            sign  = 1 if pos.get("direction", "LONG").upper() == "LONG" else -1
            notional = pos.get("price", closes[-1]) * pos.get("lots", 1) * mult * sign
            all_returns[sym] = ret * abs(notional) * sign

        if not all_returns:
            return {"error": "无有效持仓数据"}

        # 对齐长度
        min_len = min(len(r) for r in all_returns.values())
        portfolio_pnl = sum(
            r[-min_len:] for r in all_returns.values()
        )

        var  = float(np.percentile(portfolio_pnl, (1 - confidence) * 100))
        cvar = float(portfolio_pnl[portfolio_pnl <= var].mean()) if np.any(portfolio_pnl <= var) else var

        return {
            "portfolio_var_usd":  round(abs(var), 2),
            "portfolio_cvar_usd": round(abs(cvar), 2),
            "positions":          list(positions_dict.keys()),
            "confidence":         confidence,
            "lookback":           f"{lookback_days}d",
            "note":               "组合VaR已考虑资产间相关性（历史模拟法）",
        }


# ══════════════════════════════════════════════════════════════════
#  16. 滚动相关矩阵
# ══════════════════════════════════════════════════════════════════

class RollingCorrelation:
    """
    多资产滚动 Pearson 相关矩阵
    ─────────────────────────────────────────────────────────────
    应用：
      - 相关性 > 0.9：两品种高度同向，套利空间有限
      - 相关性下降：可能出现结构性价差，套利机会
      - 用于调整组合仓位（避免过度集中的相关风险）
    """

    @staticmethod
    def compute(
        symbols: list,
        window: int = 60,
        period: str = "6mo",
    ) -> dict:
        """
        symbols: ["ES", "NQ", "IF", ...]（最多6个）
        window:  滚动窗口（天数）
        返回：完整相关矩阵 + 60日历史相关系数
        """
        dfs = {}
        for sym in symbols:
            df = _fetch_ohlcv(sym, period=period, interval="1d")
            if df is not None:
                closes = df["Close"].values.flatten().astype(float)
                if len(closes) >= window:
                    dfs[sym] = closes

        if len(dfs) < 2:
            return {"error": "有效品种不足2个"}

        syms = list(dfs.keys())
        # 对齐最短序列
        min_len = min(len(v) for v in dfs.values())
        aligned = {s: dfs[s][-min_len:] for s in syms}

        # 日收益率
        ret_dict = {s: np.diff(np.log(aligned[s])) for s in syms}
        ret_mat  = np.array([ret_dict[s] for s in syms])   # (k, T-1)

        # 全样本相关矩阵
        corr_matrix = np.corrcoef(ret_mat)

        # 最新 window 期
        ret_window  = ret_mat[:, -window:]
        corr_window = np.corrcoef(ret_window)

        def _fmt_matrix(mat):
            out = {}
            k = len(syms)
            for i in range(k):
                for j in range(i + 1, k):
                    key = f"{syms[i]}_{syms[j]}"
                    out[key] = round(float(mat[i, j]), 4)
            return out

        # 找最高/最低相关对
        pairs_corr = _fmt_matrix(corr_window)
        if pairs_corr:
            max_pair = max(pairs_corr, key=pairs_corr.get)
            min_pair = min(pairs_corr, key=pairs_corr.get)
        else:
            max_pair = min_pair = None

        return {
            "symbols":         syms,
            "window":          window,
            "correlation_60d": pairs_corr,
            "max_corr_pair":   max_pair,
            "max_corr_val":    pairs_corr.get(max_pair) if max_pair else None,
            "min_corr_pair":   min_pair,
            "min_corr_val":    pairs_corr.get(min_pair) if min_pair else None,
            "note":            (
                f"最高相关: {max_pair}={pairs_corr.get(max_pair):.3f}（套利空间小）; "
                f"最低相关: {min_pair}={pairs_corr.get(min_pair):.3f}（多元化好）"
                if max_pair else "无"
            ),
        }


# ══════════════════════════════════════════════════════════════════
#  17. StrategyMonitor — 渐进恢复链（参考 FX 模型）
# ══════════════════════════════════════════════════════════════════

class StrategyMonitor:
    """
    连亏保护 + 渐进恢复链
    ─────────────────────────────────────────────────────────────
    状态机：
      GREEN       → 正常，满仓
      RECOVERY_75 → 已连亏2笔，仓位降至75%
      RECOVERY_50 → 连亏4笔，仓位降至50%
      RECOVERY_30 → 连亏6笔，仓位降至30%（同SI_MAX_CONSEC触发）
      COOLDOWN    → 连亏 ≥ SI_MAX_CONSEC 笔，操作员需手动重置

    恢复规则：每阶段连续盈利 2 笔才能升档
    COOLDOWN 不自动恢复，需调用 reset_cooldown()
    """

    STATES = ["GREEN", "RECOVERY_75", "RECOVERY_50", "RECOVERY_30", "COOLDOWN"]
    STATE_MULT = {
        "GREEN":       1.00,
        "RECOVERY_75": 0.75,
        "RECOVERY_50": 0.50,
        "RECOVERY_30": 0.30,
        "COOLDOWN":    0.00,
    }
    LOSS_THRESHOLDS = [2, 4, 6]     # 触发 75/50/30 的连亏笔数
    WINS_TO_RECOVER = 2             # 升档所需连续盈利笔数

    def __init__(self):
        self._state         = "GREEN"
        self._consec_loss   = 0
        self._consec_win    = 0
        self._history: list = []    # [(pnl, state_after), ...]

    @property
    def state(self) -> str:
        return self._state

    @property
    def size_multiplier(self) -> float:
        return self.STATE_MULT[self._state]

    def on_trade(self, pnl: float):
        """每笔交易后调用，更新状态机"""
        self._history.append((round(pnl, 2), self._state))
        if self._state == "COOLDOWN":
            return  # 需人工重置

        if pnl >= 0:
            self._consec_loss = 0
            self._consec_win += 1
            self._try_recover()
        else:
            self._consec_win = 0
            self._consec_loss += 1
            self._update_on_loss()

    def _update_on_loss(self):
        max_c = int(os.getenv("SI_MAX_CONSEC_LOSSES", "4"))
        if self._consec_loss >= max_c:
            self._state = "COOLDOWN"
        elif self._consec_loss >= 6:
            self._state = "RECOVERY_30"
        elif self._consec_loss >= 4:
            self._state = "RECOVERY_50"
        elif self._consec_loss >= 2:
            self._state = "RECOVERY_75"

    def _try_recover(self):
        if self._consec_win < self.WINS_TO_RECOVER:
            return
        idx = self.STATES.index(self._state)
        if idx > 0:
            self._state = self.STATES[idx - 1]
            self._consec_win = 0  # 升档后重置连胜计数

    def reset_cooldown(self, reason: str = "OPERATOR") -> bool:
        """操作员手动重置 COOLDOWN"""
        if self._state == "COOLDOWN":
            self._state       = "RECOVERY_30"
            self._consec_loss = 0
            self._consec_win  = 0
            return True
        return False

    @property
    def summary(self) -> dict:
        return {
            "state":          self._state,
            "size_mult":      self.size_multiplier,
            "consec_loss":    self._consec_loss,
            "consec_win":     self._consec_win,
            "last_5":         self._history[-5:],
            "can_trade":      self._state != "COOLDOWN",
        }


# 全局 StrategyMonitor 实例
_STRATEGY_MONITOR = StrategyMonitor()


# ══════════════════════════════════════════════════════════════════
#  18. 宏观事件日历（US：FOMC / CPI / NFP / 财报季）
# ══════════════════════════════════════════════════════════════════

class EventCalendar:
    """
    US 宏观事件自动检测
    ─────────────────────────────────────────────────────────────
    规则：
      - FOMC：每年8次，约在1/3/5/6/7/9/11/12月（周三下午 2pm ET = 19:00 UTC）
              事件窗口：±2h（前1h + 后1h）
      - CPI：每月第2周周二，08:30 ET = 12:30 UTC，窗口±1.5h
      - NFP：每月第1个周五，08:30 ET = 12:30 UTC，窗口±1.5h
      - PCE：每月最后一个周五，08:30 ET = 12:30 UTC，窗口±1h
      - 财报季周：1/4/7/10 月的第2-5周（降低 TREND 阈值）
    """

    # FOMC 月份（近似，实际以 Fed 官网为准）
    FOMC_MONTHS  = {1, 3, 5, 6, 7, 9, 11, 12}

    def __init__(self):
        self._manual_until: float = 0.0
        self._manual_reason: str  = ""

    def is_event_active(self) -> bool:
        """当前时刻是否处于事件窗口"""
        if time.time() < self._manual_until:
            return True
        name, _ = self.next_event()
        _, in_window = self._check_now()
        return in_window

    def _check_now(self) -> Tuple[str, bool]:
        """检查当前UTC时间是否在任意事件窗口内"""
        now = datetime.now(timezone.utc)
        # FOMC：周三 18:30-21:00 UTC（2pm-3:30pm ET）
        if now.weekday() == 2 and now.month in self.FOMC_MONTHS:
            if 18 <= now.hour <= 21:
                return "FOMC 决议", True

        # CPI/NFP/PCE：08:30 ET = 12:30 UTC（周五/周二）
        # NFP：月第1个周五
        if now.weekday() == 4:  # 周五
            if now.day <= 7 and 12 <= now.hour <= 14:
                return "NFP 非农数据", True
            if now.day >= 25 and 12 <= now.hour <= 13:
                return "PCE 物价指数", True

        # CPI：月第2周周二（day 8-14）
        if now.weekday() == 1 and 8 <= now.day <= 14:
            if 12 <= now.hour <= 14:
                return "CPI 数据", True

        return "", False

    def next_event(self) -> Tuple[str, Optional[datetime]]:
        """返回下一个最近的事件名称和时间"""
        now = datetime.now(timezone.utc)
        candidates = []

        # 下一个 NFP（下一个月的第1个周五 12:30 UTC）
        for delta_months in range(0, 3):
            year  = now.year + (now.month + delta_months - 1) // 12
            month = (now.month + delta_months - 1) % 12 + 1
            # 找该月第1个周五
            first_day = datetime(year, month, 1, 12, 30, tzinfo=timezone.utc)
            days_to_fri = (4 - first_day.weekday()) % 7
            nfp_dt = first_day + timedelta(days=days_to_fri)
            if nfp_dt > now:
                candidates.append(("NFP", nfp_dt))
                break

        # 下一个 FOMC（下一个 FOMC 月的第5个周三 19:00 UTC）
        for delta_months in range(0, 4):
            year  = now.year + (now.month + delta_months - 1) // 12
            month = (now.month + delta_months - 1) % 12 + 1
            if month in self.FOMC_MONTHS:
                # 近似取该月第3个周三
                first_day = datetime(year, month, 1, 19, 0, tzinfo=timezone.utc)
                days_to_wed = (2 - first_day.weekday()) % 7
                fomc_dt = first_day + timedelta(days=days_to_wed + 14)  # 第3个周三
                if fomc_dt > now:
                    candidates.append(("FOMC", fomc_dt))
                    break

        if not candidates:
            return "未知事件", None
        candidates.sort(key=lambda x: x[1])
        name, dt = candidates[0]
        return name, dt

    def hours_to_next(self) -> float:
        """距下一个事件的小时数"""
        _, dt = self.next_event()
        if dt is None:
            return 999.0
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600

    def set_manual_event(self, minutes: int = 60, reason: str = "MANUAL"):
        """手动设置事件窗口"""
        self._manual_until  = time.time() + minutes * 60
        self._manual_reason = reason

    def status(self) -> dict:
        event_name, in_window = self._check_now()
        next_name, next_dt    = self.next_event()
        hours_to  = self.hours_to_next()
        return {
            "in_event_window": in_window,
            "current_event":   event_name if in_window else None,
            "next_event":      next_name,
            "next_event_utc":  next_dt.isoformat()[:16] if next_dt else None,
            "hours_to_next":   round(hours_to, 1),
            "manual_active":   time.time() < self._manual_until,
            "manual_reason":   self._manual_reason if time.time() < self._manual_until else None,
        }


# 全局 EventCalendar 实例（RegimeEngine 引用此对象）
_EVENT_CALENDAR = EventCalendar()


# ══════════════════════════════════════════════════════════════════
#  19. VIX 跨资产风险过滤器
# ══════════════════════════════════════════════════════════════════

class VIXFilter:
    """
    VIX 恐慌指数过滤器
    ─────────────────────────────────────────────────────────────
    从 yfinance 获取 ^VIX（美股波动率指数）

    交易规则：
      VIX > 35  → BLOCKED（极端恐慌，停止所有新单）
      VIX > 25  → 仓位缩减至 50%
      VIX > 20  → 仓位缩减至 75%
      VIX ≤ 20  → 正常满仓（GREEN）

    同时提供 VVIX（VIX的VIX）作为二阶风险信号
    """

    BLOCKED_THRESHOLD = 35.0
    REDUCE_50_THRESHOLD = 25.0
    REDUCE_75_THRESHOLD = 20.0

    def __init__(self):
        self._last_vix: Optional[float] = None
        self._last_fetch: float = 0.0
        self._cache_ttl: float = 300.0  # 5分钟缓存

    def get_vix(self) -> Optional[float]:
        """获取最新 VIX（带缓存）"""
        if (time.time() - self._last_fetch < self._cache_ttl and
                self._last_vix is not None):
            return self._last_vix
        try:
            import yfinance as yf
            df = yf.Ticker("^VIX").history(period="5d", interval="1d")
            if not df.empty:
                self._last_vix   = float(df["Close"].iloc[-1])
                self._last_fetch = time.time()
                return self._last_vix
        except Exception:
            pass
        return self._last_vix  # 返回上次值

    def evaluate(self) -> dict:
        vix = self.get_vix()
        if vix is None:
            return {
                "vix":        None,
                "level":      "UNKNOWN",
                "size_mult":  0.75,   # 保守默认
                "can_trade":  True,
                "note":       "VIX 数据不可用，保守 75% 仓位",
            }

        if vix > self.BLOCKED_THRESHOLD:
            level, mult, can = "EXTREME", 0.0, False
            note = f"VIX={vix:.1f} > {self.BLOCKED_THRESHOLD}，极端恐慌停止交易"
        elif vix > self.REDUCE_50_THRESHOLD:
            level, mult, can = "HIGH", 0.5, True
            note = f"VIX={vix:.1f} 高位，仓位降至 50%"
        elif vix > self.REDUCE_75_THRESHOLD:
            level, mult, can = "ELEVATED", 0.75, True
            note = f"VIX={vix:.1f} 偏高，仓位降至 75%"
        else:
            level, mult, can = "NORMAL", 1.0, True
            note = f"VIX={vix:.1f} 正常，满仓可交易"

        return {
            "vix":       round(vix, 2),
            "level":     level,
            "size_mult": mult,
            "can_trade": can,
            "note":      note,
        }

    def check(self) -> dict:
        """返回供 StockIndexTradeTool 使用的标准接口"""
        ev = self.evaluate()
        return {
            "allowed":    ev.get("can_trade", True),
            "multiplier": ev.get("size_mult", 0.75),
            "vix":        ev.get("vix") or 0.0,
            "tier":       ev.get("level", "UNKNOWN"),
        }


# 全局 VIX 过滤器
_VIX_FILTER = VIXFilter()


# ══════════════════════════════════════════════════════════════════
#  20. 执行层桩：IB（US）/ CTP（CN）
# ══════════════════════════════════════════════════════════════════

class IBExecutor:
    """
    Interactive Brokers 执行层（ib_insync）
    ─────────────────────────────────────────────────────────────
    当前状态：API桩（占位符）
    待接入：ib_insync 库 → pip install ib_insync

    环境变量：
      IB_HOST      = 127.0.0.1（TWS/IB Gateway 本地端口）
      IB_PORT      = 7497（TWS模拟）/ 7496（TWS实盘）/ 4002（Gateway模拟）/ 4001（实盘）
      IB_CLIENT_ID = 1
      IB_PAPER     = true（模拟账户）/ false（实盘）

    合约规格（US期货）：
      ES：S&P 500，CME，$50/点，月合约（ESH4/ESM4/ESU4/ESZ4）
      NQ：Nasdaq，CME，$20/点
      YM：Dow，CBT，$5/点
      RTY：Russell 2000，CME，$50/点
    """

    ES_CONTRACT = {"symbol": "ES", "secType": "FUT", "exchange": "CME", "currency": "USD"}
    NQ_CONTRACT = {"symbol": "NQ", "secType": "FUT", "exchange": "CME", "currency": "USD"}
    YM_CONTRACT = {"symbol": "YM", "secType": "FUT", "exchange": "CBOT", "currency": "USD"}
    RTY_CONTRACT= {"symbol": "RTY","secType": "FUT", "exchange": "CME", "currency": "USD"}

    MULT = {"ES": 50, "NQ": 20, "YM": 5, "RTY": 50}

    def __init__(self):
        self.host      = os.getenv("IB_HOST", "127.0.0.1")
        self.port      = int(os.getenv("IB_PORT", "7497"))
        self.client_id = int(os.getenv("IB_CLIENT_ID", "1"))
        self.paper     = os.getenv("IB_PAPER", "true").lower() == "true"
        self._ib       = None       # ib_insync.IB() 实例（连接后赋值）
        self._connected = False

    @property
    def is_configured(self) -> bool:
        try:
            import ib_insync
            return True
        except ImportError:
            return False

    def connect(self) -> dict:
        """连接 TWS/IB Gateway"""
        if not self.is_configured:
            return {"status": "ERROR", "reason": "ib_insync 未安装，运行: pip install ib_insync"}
        try:
            import ib_insync
            self._ib = ib_insync.IB()
            self._ib.connect(self.host, self.port, clientId=self.client_id)
            self._connected = True
            return {"status": "CONNECTED", "host": self.host, "port": self.port,
                    "paper": self.paper}
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}

    def place_bracket_order(
        self, symbol: str, direction: str, qty: int,
        take_profit: float, stop_loss: float,
    ) -> dict:
        """下 OCO 括号订单（MKT + TP Limit + SL Stop）"""
        if not self._connected:
            return {"status": "STUB", "note": "IB 未连接，PAPER模式模拟",
                    "symbol": symbol, "direction": direction, "qty": qty,
                    "tp": take_profit, "sl": stop_loss}
        try:
            import ib_insync as ib
            contract = ib.Future(**{
                k: v for k, v in {
                    "ES": self.ES_CONTRACT, "NQ": self.NQ_CONTRACT,
                    "YM": self.YM_CONTRACT, "RTY": self.RTY_CONTRACT,
                }.get(symbol.upper(), self.ES_CONTRACT).items()
            })
            action = "BUY" if direction.upper() == "LONG" else "SELL"
            exit_action = "SELL" if action == "BUY" else "BUY"
            parent = ib.MarketOrder(action, qty)
            tp_order = ib.LimitOrder(exit_action, qty, take_profit,
                                     ocaGroup=f"OCA_{symbol}", ocaType=1)
            sl_order = ib.StopOrder(exit_action, qty, stop_loss,
                                    ocaGroup=f"OCA_{symbol}", ocaType=1)
            parent.transmit = False
            tp_order.parentId = parent.orderId
            sl_order.parentId = parent.orderId
            sl_order.transmit = True
            trades = [
                self._ib.placeOrder(contract, parent),
                self._ib.placeOrder(contract, tp_order),
                self._ib.placeOrder(contract, sl_order),
            ]
            return {"status": "PLACED", "symbol": symbol, "direction": direction,
                    "qty": qty, "tp": take_profit, "sl": stop_loss,
                    "order_ids": [str(t.order.orderId) for t in trades]}
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}

    def disconnect(self):
        if self._ib and self._connected:
            try:
                self._ib.disconnect()
            except Exception:
                pass
        self._connected = False

    def status(self) -> dict:
        return {
            "lib_available": self.is_configured,
            "connected":     self._connected,
            "host":          self.host,
            "port":          self.port,
            "paper":         self.paper,
            "note":          "安装: pip install ib_insync" if not self.is_configured else "就绪",
        }


class CTPExecutor:
    """
    CTP 期货执行层（中国期货市场）
    ─────────────────────────────────────────────────────────────
    当前状态：API桩（占位符）
    待接入：openctp-client 或 vnpy-ctp

    环境变量：
      CTP_BROKER_ID    = 期货公司代码（如 9999 = 模拟环境）
      CTP_USER_ID      = 交易账号
      CTP_PASSWORD     = 密码
      CTP_APP_ID       = simnow_client_test（模拟）
      CTP_AUTH_CODE    = 0000000000000000（模拟）
      CTP_FRONT_ADDR   = tcp://182.254.243.31:10901（模拟行情）
      CTP_TD_ADDR      = tcp://182.254.243.31:10900（模拟交易）
      CTP_PAPER        = true（模拟）/ false（实盘）

    合约规格（CN期货）：
      IF：沪深300，中金所，¥300/点，保证金≈¥150,000
      IC：中证500，中金所，¥200/点
      IH：上证50，中金所，¥300/点
      IM：中证1000，中金所，¥200/点
    """

    MULT = {"IF": 300, "IC": 200, "IH": 300, "IM": 200}

    def __init__(self):
        self.broker_id  = os.getenv("CTP_BROKER_ID", "9999")
        self.user_id    = os.getenv("CTP_USER_ID", "")
        self.password   = os.getenv("CTP_PASSWORD", "")
        self.app_id     = os.getenv("CTP_APP_ID", "simnow_client_test")
        self.auth_code  = os.getenv("CTP_AUTH_CODE", "0000000000000000")
        self.front_addr = os.getenv("CTP_FRONT_ADDR", "tcp://182.254.243.31:10901")
        self.td_addr    = os.getenv("CTP_TD_ADDR",    "tcp://182.254.243.31:10900")
        self.paper      = os.getenv("CTP_PAPER", "true").lower() == "true"
        self._api       = None
        self._connected = False

    @property
    def is_configured(self) -> bool:
        return bool(self.user_id and self.password)

    def connect(self) -> dict:
        """连接 CTP 前置（需 openctp-client 或 vnpy-ctp）"""
        try:
            import openctp_client as ctp   # noqa
            return {"status": "STUB", "note": "openctp_client 检测到，需完整实现"}
        except ImportError:
            pass
        return {
            "status": "STUB",
            "note":   "CTP执行桩已准备，安装: pip install openctp-client",
            "broker": self.broker_id,
            "user":   self.user_id or "未配置",
            "paper":  self.paper,
        }

    def place_order(self, contract: str, direction: str, qty: int,
                    price: Optional[float] = None) -> dict:
        """下单（占位符）"""
        order_type = "限价单" if price else "市价单"
        return {
            "status":    "STUB",
            "contract":  contract,
            "direction": direction,
            "qty":       qty,
            "price":     price,
            "type":      order_type,
            "note":      "CTP执行桩，需完整接入后生效",
        }

    def status(self) -> dict:
        return {
            "configured":    self.is_configured,
            "connected":     self._connected,
            "paper":         self.paper,
            "broker_id":     self.broker_id,
            "user":          self.user_id or "未配置",
            "front_addr":    self.front_addr,
            "note":          "需配置 CTP_USER_ID / CTP_PASSWORD" if not self.is_configured else "配置完整",
        }


# 全局执行层实例
_IB_EXECUTOR  = IBExecutor()
_CTP_EXECUTOR = CTPExecutor()


# ══════════════════════════════════════════════════════════════════
#  21. StockIndexAnalyticsTool（第 4 个 CrewAI 工具）
# ══════════════════════════════════════════════════════════════════

class SIAnalyticsInput(BaseModel):
    query: str = Field(
        default="full",
        description=(
            "分析类型：\n"
            "  'half_life'   — 价差均值回归半衰期（OU过程）\n"
            "  'hurst'       — Hurst指数（趋势/均值回归分类）\n"
            "  'johansen'    — Johansen多资产协整检验\n"
            "  'garch'       — GARCH(1,1)波动率预测\n"
            "  'var'         — 组合历史模拟VaR/CVaR\n"
            "  'correlation' — 滚动相关矩阵\n"
            "  'performance' — 策略绩效（Sharpe/Sortino/MaxDD）\n"
            "  'events'      — 宏观事件日历状态\n"
            "  'vix'         — VIX风险过滤器\n"
            "  'execution'   — IB/CTP执行层状态\n"
            "  'recovery'    — StrategyMonitor恢复链状态\n"
            "  'full'        — 全套分析汇总"
        )
    )
    symbols: str = Field(
        default="ES,NQ",
        description="品种列表（逗号分隔）。half_life/hurst/garch 用第1个品种；johansen/correlation 用全部"
    )
    pnl_list: str = Field(
        default="",
        description="绩效分析用：逗号分隔的历史PnL（美元），如 '500,-300,800,-200,1200'"
    )


class StockIndexAnalyticsTool(BaseTool):
    name: str        = "StockIndexAnalyticsTool"
    description: str = (
        "股指期货量化数学分析工具（第4工具）。\n\n"
        "可计算：\n"
        "  • 均值回归半衰期（OU过程，统计套利最优持仓时间）\n"
        "  • Hurst指数（判断序列趋势性/均值回归性）\n"
        "  • Johansen多资产协整检验（纯numpy，支持2-4资产）\n"
        "  • GARCH(1,1)波动率预测（条件方差，VaR输入）\n"
        "  • 组合历史模拟VaR/CVaR（95%/99%置信度）\n"
        "  • 滚动相关矩阵（60日，多品种相关性分析）\n"
        "  • 策略绩效指标（Sharpe/Sortino/MaxDD/Calmar）\n"
        "  • 宏观事件日历（FOMC/CPI/NFP自动检测）\n"
        "  • VIX跨资产风险过滤器\n"
        "  • IB/CTP执行层状态\n"
        "  • StrategyMonitor恢复链状态\n"
    )
    args_schema: type[BaseModel] = SIAnalyticsInput

    def _run(self, query: str = "full", symbols: str = "ES,NQ",
             pnl_list: str = "") -> str:
        try:
            return self._analyze(query, symbols, pnl_list)
        except Exception as e:
            import traceback
            return json.dumps({"error": str(e),
                               "trace": traceback.format_exc()[-500:]},
                              ensure_ascii=False)

    def _analyze(self, query: str, symbols: str, pnl_list: str) -> str:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        sym_a    = sym_list[0] if sym_list else "ES"
        sym_b    = sym_list[1] if len(sym_list) > 1 else "NQ"

        # ── 1. half_life ──────────────────────────────────────────
        if query in ("half_life", "full"):
            # 使用 Kalman 滤波器的价差历史
            cache_key = f"{sym_a}:{sym_b}"
            if cache_key not in _KALMAN_CACHE:
                # 先运行一次 stat arb 以初始化 Kalman
                _stat_arb_signal(sym_a, sym_b)
            kf = _KALMAN_CACHE.get(cache_key)
            if kf and len(kf._spread_history) >= 10:
                hl = HalfLifeEstimator.from_kalman(kf)
            else:
                # 直接从价格计算
                df_a = _fetch_ohlcv(sym_a)
                df_b = _fetch_ohlcv(sym_b)
                if df_a is not None and df_b is not None:
                    ca = df_a["Close"].values.flatten().astype(float)
                    cb = df_b["Close"].values.flatten().astype(float)
                    n  = min(len(ca), len(cb), 90)
                    coint = engle_granger_coint(ca[-n:], cb[-n:])
                    spread = ca[-n:] - coint["beta"] * cb[-n:] - coint["alpha"]
                    hl = HalfLifeEstimator.estimate(spread)
                else:
                    hl = {"error": "数据不可用"}
            if query == "half_life":
                return json.dumps({"query": "half_life",
                                   "pair": f"{sym_a}/{sym_b}",
                                   "result": hl},
                                  ensure_ascii=False, indent=2)

        # ── 2. hurst ─────────────────────────────────────────────
        if query in ("hurst", "full"):
            hurst_results = {}
            for sym in sym_list[:4]:
                df = _fetch_ohlcv(sym)
                if df is not None:
                    closes = df["Close"].values.flatten().astype(float)
                    hurst_results[sym] = HurstExponent.compute(closes)
                else:
                    hurst_results[sym] = {"error": "数据不可用"}
            if query == "hurst":
                return json.dumps({"query": "hurst", "results": hurst_results},
                                  ensure_ascii=False, indent=2)

        # ── 3. johansen ──────────────────────────────────────────
        if query in ("johansen", "full"):
            if len(sym_list) >= 2:
                dfs = []
                syms_ok = []
                for sym in sym_list[:4]:
                    df = _fetch_ohlcv(sym)
                    if df is not None:
                        dfs.append(df["Close"].values.flatten().astype(float))
                        syms_ok.append(sym)
                if len(dfs) >= 2:
                    n = min(len(a) for a in dfs)
                    mat = np.column_stack([a[-n:] for a in dfs])
                    joh = JohansenTest.test(mat)
                    joh["symbols"] = syms_ok
                else:
                    joh = {"error": "数据不足"}
            else:
                joh = {"error": "Johansen 需要至少2个品种"}
            if query == "johansen":
                return json.dumps({"query": "johansen", "result": joh},
                                  ensure_ascii=False, indent=2)

        # ── 4. garch ─────────────────────────────────────────────
        if query in ("garch", "full"):
            garch_results = {}
            garch = SimpleGARCH()
            for sym in sym_list[:4]:
                df = _fetch_ohlcv(sym)
                if df is not None:
                    closes = df["Close"].values.flatten().astype(float)
                    returns = np.diff(np.log(closes))
                    fit = garch.fit(returns)
                    if fit.get("fitted"):
                        fcast = garch.forecast(h=5)
                        garch_results[sym] = {**fit, "forecast": fcast}
                    else:
                        garch_results[sym] = fit
                else:
                    garch_results[sym] = {"error": "数据不可用"}
            if query == "garch":
                return json.dumps({"query": "garch", "results": garch_results},
                                  ensure_ascii=False, indent=2)

        # ── 5. var ───────────────────────────────────────────────
        if query in ("var", "full"):
            positions_for_var = {}
            for sym in sym_list[:4]:
                df = _fetch_ohlcv(sym, period="5d")
                price = float(df["Close"].values.flatten()[-1]) if df is not None else 100.0
                pos = _POSITIONS.get(sym)
                direction = pos.direction.value if pos else "LONG"
                lots = pos.size if pos else 1.0
                positions_for_var[sym] = {"lots": lots, "direction": direction, "price": price}
            if len(positions_for_var) >= 2:
                var_result = PortfolioVaR.portfolio(positions_for_var)
            else:
                sym = sym_list[0]
                var_result = PortfolioVaR.single_asset(
                    sym, positions_for_var.get(sym, {}).get("price", 5000),
                    positions_for_var.get(sym, {}).get("lots", 1),
                    positions_for_var.get(sym, {}).get("direction", "LONG"),
                )
            if query == "var":
                return json.dumps({"query": "var", "result": var_result},
                                  ensure_ascii=False, indent=2)

        # ── 6. correlation ───────────────────────────────────────
        if query in ("correlation", "full"):
            corr = RollingCorrelation.compute(sym_list[:6])
            if query == "correlation":
                return json.dumps({"query": "correlation", "result": corr},
                                  ensure_ascii=False, indent=2)

        # ── 7. performance ───────────────────────────────────────
        if query in ("performance", "full"):
            pnl_nums = []
            if pnl_list.strip():
                try:
                    pnl_nums = [float(x) for x in pnl_list.split(",") if x.strip()]
                except Exception:
                    pass
            if not pnl_nums and _DAILY_PNL != 0:
                pnl_nums = [_DAILY_PNL]
            if pnl_nums:
                perf = PerformanceMetrics.from_trades(pnl_nums, RiskService.EQUITY_USDT)
            else:
                perf = {"note": "暂无交易记录，请提供 pnl_list 参数"}
            if query == "performance":
                return json.dumps({"query": "performance", "result": perf},
                                  ensure_ascii=False, indent=2)

        # ── 8. events ────────────────────────────────────────────
        if query in ("events", "full"):
            ev = _EVENT_CALENDAR.status()
            if query == "events":
                return json.dumps({"query": "events", "result": ev},
                                  ensure_ascii=False, indent=2)

        # ── 9. vix ───────────────────────────────────────────────
        if query in ("vix", "full"):
            vix_data = _VIX_FILTER.evaluate()
            if query == "vix":
                return json.dumps({"query": "vix", "result": vix_data},
                                  ensure_ascii=False, indent=2)

        # ── 10. execution ────────────────────────────────────────
        if query in ("execution", "full"):
            exec_status = {
                "ib":  _IB_EXECUTOR.status(),
                "ctp": _CTP_EXECUTOR.status(),
                "mode": os.getenv("SI_EXEC_MODE", "PAPER"),
            }
            if query == "execution":
                return json.dumps({"query": "execution", "result": exec_status},
                                  ensure_ascii=False, indent=2)

        # ── 11. recovery ─────────────────────────────────────────
        if query in ("recovery", "full"):
            rec = _STRATEGY_MONITOR.summary
            if query == "recovery":
                return json.dumps({"query": "recovery", "result": rec},
                                  ensure_ascii=False, indent=2)

        # ── full 汇总 ────────────────────────────────────────────
        return json.dumps({
            "query":       "full",
            "symbols":     sym_list,
            "timestamp":   datetime.now(timezone.utc).isoformat()[:19],
            "half_life":   hl if "hl" in dir() else None,
            "hurst":       hurst_results if "hurst_results" in dir() else None,
            "johansen":    joh if "joh" in dir() else None,
            "garch":       garch_results if "garch_results" in dir() else None,
            "var":         var_result if "var_result" in dir() else None,
            "correlation": corr if "corr" in dir() else None,
            "performance": perf if "perf" in dir() else None,
            "events":      ev if "ev" in dir() else None,
            "vix":         vix_data if "vix_data" in dir() else None,
            "execution":   exec_status if "exec_status" in dir() else None,
            "recovery":    rec if "rec" in dir() else None,
        }, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════
#  22. 自检（python stock_index_tool.py）
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    print("=" * 60)
    print("StockIndex Tool v2 — 数学模型自检")
    print("=" * 60)

    # ── 1. 半衰期 ─────────────────────────────────────────────
    print("\n[1] HalfLifeEstimator")
    spread = np.cumsum(np.random.randn(200)) * 0.5  # 含均值回归特性的合成序列
    # 叠加均值回归
    for i in range(1, len(spread)):
        spread[i] = spread[i-1] * 0.92 + np.random.randn() * 0.3
    hl = HalfLifeEstimator.estimate(spread)
    print(f"  半衰期: {hl.get('half_life')} 天 | 均值回归: {hl.get('is_mean_reverting')} | 注: {hl.get('note')}")

    # ── 2. Hurst ──────────────────────────────────────────────
    print("\n[2] HurstExponent")
    trending  = np.cumsum(np.random.randn(300) + 0.1)     # 趋势序列
    mr_series = spread                                     # 均值回归序列
    h_trend = HurstExponent.compute(trending)
    h_mr    = HurstExponent.compute(mr_series)
    print(f"  趋势序列  H={h_trend.get('hurst')} → {h_trend.get('regime_type')}")
    print(f"  均值回归  H={h_mr.get('hurst')} → {h_mr.get('regime_type')}")

    # ── 3. Johansen ───────────────────────────────────────────
    print("\n[3] JohansenTest (合成 2 协整序列)")
    x1 = np.cumsum(np.random.randn(200))
    x2 = x1 * 1.5 + np.random.randn(200) * 0.5  # 与 x1 协整
    mat = np.column_stack([x1, x2])
    joh = JohansenTest.test(mat)
    print(f"  协整秩: {joh.get('rank')} | 协整: {joh.get('cointegrated')} | {joh.get('interpretation')}")

    # ── 4. GARCH ──────────────────────────────────────────────
    print("\n[4] SimpleGARCH")
    ret_sim = np.random.randn(300) * 0.01  # 模拟日收益
    g = SimpleGARCH()
    fit = g.fit(ret_sim)
    fcast = g.forecast(h=5)
    print(f"  α={fit.get('alpha'):.4f} β={fit.get('beta'):.4f} 持续性={fit.get('persistence'):.4f}")
    print(f"  明日波动率预测: {fcast.get('h1_vol_pct')}%  当前: {fcast.get('current_vol_pct')}%")

    # ── 5. PerformanceMetrics ─────────────────────────────────
    print("\n[5] PerformanceMetrics")
    pnl_sim = [500, -300, 800, -200, 1200, -400, 600, 900, -100, 700]
    perf = PerformanceMetrics.from_trades(pnl_sim, equity=100_000)
    print(f"  胜率={perf['win_rate']}% | Sharpe={perf['sharpe']} | MaxDD={perf['max_drawdown']}%")
    print(f"  盈亏比={perf['profit_factor']} | 评级={perf['note']}")

    # ── 6. EventCalendar ──────────────────────────────────────
    print("\n[6] EventCalendar")
    ev = _EVENT_CALENDAR.status()
    print(f"  事件窗口中: {ev['in_event_window']}")
    print(f"  下一个事件: {ev['next_event']} @ {ev['next_event_utc']} (距今 {ev['hours_to_next']:.1f}h)")

    # ── 7. VIX Filter ─────────────────────────────────────────
    print("\n[7] VIXFilter")
    vix_res = _VIX_FILTER.evaluate()
    if vix_res.get("vix"):
        print(f"  VIX={vix_res['vix']} | 级别={vix_res['level']} | 仓位系数={vix_res['size_mult']} | {vix_res['note']}")
    else:
        print(f"  {vix_res['note']}")

    # ── 8. StrategyMonitor ────────────────────────────────────
    print("\n[8] StrategyMonitor")
    for pnl in [-300, -200, 500, -400, 600, 700]:
        _STRATEGY_MONITOR.on_trade(pnl)
    sm = _STRATEGY_MONITOR.summary
    print(f"  状态={sm['state']} | 仓位系数={sm['size_mult']} | 连亏={sm['consec_loss']} | 可交易={sm['can_trade']}")

    # ── 9. IB/CTP 执行桩 ──────────────────────────────────────
    print("\n[9] 执行层状态")
    print(f"  IB:  {_IB_EXECUTOR.status()['note']}")
    print(f"  CTP: {_CTP_EXECUTOR.status()['note']}")

    print("\n✅ 所有数学模型自检完成")
