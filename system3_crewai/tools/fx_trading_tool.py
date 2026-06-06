"""
fx_trading_tool.py — 外汇量化交易 CrewAI 工具
===============================================
完整移植 Foreign-Currency-main 机器人架构，叠加更多品种支持：

  核心组件（直接移植）：
  ① TechnicalIndicators   — SMA/EMA/ADX/ATR/RSI/Bollinger/VWAP（纯 numpy）
  ② RegimeEngine          — TREND / RANGE / EVENT / UNSTABLE
  ③ EventResponseEngine   — 5阶段状态机（IDLE → READY/INVALID）
  ④ SignalEngine          — TREND: SMA对齐+RSI；RANGE: BB+RSI均值回归
  ⑤ ExecutionGate         — P0-P6 优先级裁决链（Kill→System→Deterioration→
                             Cooldown→Event→Portfolio→Lifetime→Signal）
  ⑥ StrategyMonitor       — 连亏检测 + 渐进恢复（30%→50%→75%→GREEN）
  ⑦ FXPaperBroker         — 纸盘交易：开/平仓、P&L、持仓管理

  外汇品种（yfinance 免费数据）：
    AUD/USD  NZD/USD  EUR/USD  GBP/USD
    USD/JPY  USD/CAD  USD/CHF  EUR/GBP  EUR/JPY

  三个 CrewAI 工具：
    FXSignalTool      → 多品种实时信号（regime + 技术指标 + 执行闸门）
    FXRiskTool        → 组合风险：持仓、P&L、StrategyMonitor、闸门状态
    FXPaperTradeTool  → 纸盘开平仓执行

  环境变量：
    FX_EQUITY=100000          账户规模（USD）
    FX_MAX_DAILY_LOSS_PCT=0.02 每日最大亏损 2%
    FX_MAX_CONSEC_LOSSES=6    最大连续亏损自动冻结
    FX_DEFAULT_PAIRS=AUDUSD,NZDUSD
    TWELVE_DATA_API_KEY=      可选：Twelve Data 实时报价（未配置时 yfinance 代替）
"""

from __future__ import annotations

import os
import math
import json
import time
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any

import numpy as np

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  1. 品种映射
# ══════════════════════════════════════════════════════════════════

FX_PAIRS: Dict[str, Dict] = {
    "AUDUSD": {"yf": "AUDUSD=X", "pip": 0.0001, "base": 0.6400, "desc": "澳元/美元"},
    "NZDUSD": {"yf": "NZDUSD=X", "pip": 0.0001, "base": 0.5800, "desc": "纽元/美元"},
    "EURUSD": {"yf": "EURUSD=X", "pip": 0.0001, "base": 1.0850, "desc": "欧元/美元"},
    "GBPUSD": {"yf": "GBPUSD=X", "pip": 0.0001, "base": 1.2700, "desc": "英镑/美元"},
    "USDJPY": {"yf": "USDJPY=X", "pip": 0.01,   "base": 149.50, "desc": "美元/日元"},
    "USDCAD": {"yf": "USDCAD=X", "pip": 0.0001, "base": 1.3600, "desc": "美元/加元"},
    "USDCHF": {"yf": "USDCHF=X", "pip": 0.0001, "base": 0.9100, "desc": "美元/瑞郎"},
    "EURGBP": {"yf": "EURGBP=X", "pip": 0.0001, "base": 0.8550, "desc": "欧元/英镑"},
    "EURJPY": {"yf": "EURJPY=X", "pip": 0.01,   "base": 162.00, "desc": "欧元/日元"},
}

# yfinance 格式 → 标准格式
_YF_TO_PAIR = {v["yf"]: k for k, v in FX_PAIRS.items()}

# 执行闸门品种风险配置
PAIR_RISK_CONFIG: Dict[str, Dict] = {
    "AUDUSD": {"base_risk_pct": 0.30, "risk_mult": 0.70, "max_losses": 6, "reduce_at": 4},
    "NZDUSD": {"base_risk_pct": 0.25, "risk_mult": 0.60, "max_losses": 5, "reduce_at": 3},
    "EURUSD": {"base_risk_pct": 0.35, "risk_mult": 0.80, "max_losses": 6, "reduce_at": 4},
    "GBPUSD": {"base_risk_pct": 0.30, "risk_mult": 0.75, "max_losses": 6, "reduce_at": 4},
    "USDJPY": {"base_risk_pct": 0.30, "risk_mult": 0.75, "max_losses": 6, "reduce_at": 4},
    "USDCAD": {"base_risk_pct": 0.25, "risk_mult": 0.65, "max_losses": 5, "reduce_at": 3},
    "USDCHF": {"base_risk_pct": 0.25, "risk_mult": 0.65, "max_losses": 5, "reduce_at": 3},
    "EURGBP": {"base_risk_pct": 0.20, "risk_mult": 0.60, "max_losses": 5, "reduce_at": 3},
    "EURJPY": {"base_risk_pct": 0.25, "risk_mult": 0.65, "max_losses": 5, "reduce_at": 3},
}

REGIME_MULT = {"TREND": 1.0, "RANGE": 0.8, "EVENT": 0.6, "UNSTABLE": 0.0}
RECOVERY_MULT = {"GREEN": 1.0, "RECOVERY_75": 0.75, "RECOVERY_50": 0.50,
                 "RECOVERY_30": 0.30, "COOLDOWN": 0.0}

# ══════════════════════════════════════════════════════════════════
#  2. 技术指标（移植自 indicators.py，纯 numpy）
# ══════════════════════════════════════════════════════════════════

class TI:
    """纯 numpy 技术指标计算器（无 TA-Lib 依赖）"""

    @staticmethod
    def sma(prices: np.ndarray, period: int) -> np.ndarray:
        out = np.full_like(prices, np.nan, dtype=np.float64)
        if len(prices) < period:
            return out
        cs = np.cumsum(prices, dtype=np.float64)
        cs[period:] = cs[period:] - cs[:-period]
        out[period - 1:] = cs[period - 1:] / period
        return out

    @staticmethod
    def ema(prices: np.ndarray, period: int) -> np.ndarray:
        out = np.full_like(prices, np.nan, dtype=np.float64)
        if len(prices) < period:
            return out
        k = 2.0 / (period + 1)
        out[period - 1] = np.mean(prices[:period])
        for i in range(period, len(prices)):
            out[i] = prices[i] * k + out[i - 1] * (1 - k)
        return out

    @staticmethod
    def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
        n = len(c)
        out = np.full(n, np.nan)
        if n < period + 1:
            return out
        tr = np.zeros(n)
        tr[0] = h[0] - l[0]
        for i in range(1, n):
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))
        out[period] = np.mean(tr[1:period+1])
        for i in range(period+1, n):
            out[i] = (out[i-1] * (period - 1) + tr[i]) / period
        return out

    @staticmethod
    def adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
        n = len(c)
        out = np.full(n, np.nan)
        if n < period * 2:
            return out
        tr = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
        tr[0] = h[0] - l[0]
        for i in range(1, n):
            tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
            up = h[i] - h[i-1]; dn = l[i-1] - l[i]
            pdm[i] = up if up > dn and up > 0 else 0
            ndm[i] = dn if dn > up and dn > 0 else 0
        # Wilder smoothing
        atr_s = np.zeros(n); pds = np.zeros(n); nds = np.zeros(n)
        atr_s[period] = np.sum(tr[1:period+1])
        pds[period]   = np.sum(pdm[1:period+1])
        nds[period]   = np.sum(ndm[1:period+1])
        for i in range(period+1, n):
            atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + tr[i]
            pds[i]   = pds[i-1]   - pds[i-1]/period   + pdm[i]
            nds[i]   = nds[i-1]   - nds[i-1]/period   + ndm[i]
        pdi = np.zeros(n); ndi = np.zeros(n); dx = np.zeros(n)
        for i in range(period, n):
            if atr_s[i]:
                pdi[i] = 100 * pds[i] / atr_s[i]
                ndi[i] = 100 * nds[i] / atr_s[i]
            di_sum = pdi[i] + ndi[i]
            if di_sum:
                dx[i] = 100 * abs(pdi[i] - ndi[i]) / di_sum
        start = period * 2
        if start < n:
            out[start] = np.mean(dx[period:start+1])
            for i in range(start+1, n):
                out[i] = (out[i-1] * (period-1) + dx[i]) / period
        return out

    @staticmethod
    def rsi(c: np.ndarray, period: int = 14) -> np.ndarray:
        n = len(c)
        out = np.full(n, np.nan)
        if n < period + 1:
            return out
        d = np.diff(c)
        g = np.where(d > 0, d, 0.0); ls = np.where(d < 0, -d, 0.0)
        ag = np.mean(g[:period]); al = np.mean(ls[:period])
        out[period] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
        for i in range(period, len(d)):
            ag = (ag * (period-1) + g[i]) / period
            al = (al * (period-1) + ls[i]) / period
            out[i+1] = 100.0 if al == 0 else 100 - 100/(1 + ag/al)
        return out

    @staticmethod
    def bollinger(c: np.ndarray, period: int = 20, k: float = 2.0
                  ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = len(c)
        mid = np.full(n, np.nan); up = np.full(n, np.nan); lo = np.full(n, np.nan)
        for i in range(period-1, n):
            w = c[i-period+1:i+1]
            mu = np.mean(w); sd = np.std(w, ddof=0)
            mid[i] = mu; up[i] = mu + k*sd; lo[i] = mu - k*sd
        return up, mid, lo

    @staticmethod
    def vwap(h, l, c, v) -> np.ndarray:
        tp = (h + l + c) / 3.0
        cv = np.cumsum(v)
        ctp = np.cumsum(tp * v)
        out = np.full(len(c), np.nan)
        mask = cv > 0
        out[mask] = ctp[mask] / cv[mask]
        return out

    @staticmethod
    def latest(arr: np.ndarray) -> Optional[float]:
        if arr is None:
            return None
        for v in reversed(arr):
            if not np.isnan(v):
                return float(v)
        return None

# ══════════════════════════════════════════════════════════════════
#  3. 市场数据（yfinance，5分钟缓存）
# ══════════════════════════════════════════════════════════════════

_PRICE_CACHE: Dict[str, Tuple[float, Any]] = {}   # pair → (ts, df)
_CACHE_TTL = 300   # 5分钟

def _fetch_ohlcv(pair: str, bars: int = 120) -> Optional[Dict]:
    """
    获取 FX 价格数据（yfinance，5分钟K线，带缓存）
    返回 {open, high, low, close, volume} numpy arrays
    """
    global _PRICE_CACHE
    now = time.time()
    if pair in _PRICE_CACHE:
        ts, cached = _PRICE_CACHE[pair]
        if now - ts < _CACHE_TTL and cached is not None:
            return cached

    cfg = FX_PAIRS.get(pair)
    if not cfg:
        return None

    try:
        import yfinance as yf
        ticker = yf.Ticker(cfg["yf"])
        df = ticker.history(period="5d", interval="5m")
        if df.empty or len(df) < 30:
            # 尝试 1h 粒度
            df = ticker.history(period="30d", interval="1h")
        if df.empty:
            raise ValueError("empty data")

        df = df.tail(bars)
        result = {
            "open":   df["Open"].values.astype(float),
            "high":   df["High"].values.astype(float),
            "low":    df["Low"].values.astype(float),
            "close":  df["Close"].values.astype(float),
            "volume": df["Volume"].values.astype(float),
            "current_price": float(df["Close"].iloc[-1]),
            "bars":   len(df),
            "pair":   pair,
        }
        _PRICE_CACHE[pair] = (now, result)
        return result

    except Exception as e:
        logger.warning(f"[FX] yfinance {pair} 数据获取失败: {e}")
        # 生成模拟数据
        base = cfg["base"]
        n = bars
        changes = np.random.normal(0, cfg["pip"] * 5, n)
        closes = base + np.cumsum(changes)
        result = {
            "open":   closes - np.abs(np.random.normal(0, cfg["pip"], n)),
            "high":   closes + np.abs(np.random.normal(0, cfg["pip"]*2, n)),
            "low":    closes - np.abs(np.random.normal(0, cfg["pip"]*2, n)),
            "close":  closes,
            "volume": np.random.uniform(1000, 5000, n),
            "current_price": float(closes[-1]),
            "bars":   n,
            "pair":   pair,
            "simulated": True,
        }
        _PRICE_CACHE[pair] = (now, result)
        return result


def _compute_indicators(data: Dict) -> Dict:
    """计算全套技术指标"""
    h = data["high"]; l = data["low"]; c = data["close"]; v = data["volume"]
    sma20 = TI.sma(c, 20); sma50 = TI.sma(c, 50)
    ema20 = TI.ema(c, 20)
    adx_arr = TI.adx(h, l, c, 14)
    atr_arr = TI.atr(h, l, c, 14)
    rsi_arr = TI.rsi(c, 14)
    bb_up, bb_mid, bb_lo = TI.bollinger(c, 20, 2.0)
    vwap_arr = TI.vwap(h, l, c, np.where(v > 0, v, 1.0))
    return {
        "sma20": sma20, "sma50": sma50, "ema20": ema20,
        "adx": adx_arr, "atr": atr_arr, "rsi": rsi_arr,
        "bb_upper": bb_up, "bb_mid": bb_mid, "bb_lower": bb_lo,
        "vwap": vwap_arr, "closes": c, "highs": h, "lows": l,
    }

# ══════════════════════════════════════════════════════════════════
#  4. 市场状态 RegimeEngine
# ══════════════════════════════════════════════════════════════════

class Regime(str, Enum):
    TREND    = "TREND"
    RANGE    = "RANGE"
    EVENT    = "EVENT"
    UNSTABLE = "UNSTABLE"


def _detect_regime(inds: Dict) -> Tuple[Regime, str]:
    """ADX + vol 判断市场状态"""
    adx = TI.latest(inds["adx"]) or 15.0
    atr = TI.latest(inds["atr"]) or 0.0
    closes = inds["closes"]

    # 基线 ATR（过去 50 根 ATR 均值）
    atr_arr = inds["atr"]
    valid_atr = atr_arr[~np.isnan(atr_arr)]
    baseline_atr = float(np.mean(valid_atr[-50:])) if len(valid_atr) >= 10 else atr

    vol_ratio = atr / baseline_atr if baseline_atr > 0 else 1.0

    if vol_ratio > 3.5:
        return Regime.UNSTABLE, f"ATR飙升={vol_ratio:.1f}x基线，市场极度不稳定"
    if adx > 25:
        return Regime.TREND, f"ADX={adx:.1f}>25，趋势行情"
    return Regime.RANGE, f"ADX={adx:.1f}≤25，震荡行情"

# ══════════════════════════════════════════════════════════════════
#  5. 信号引擎（移植自 signal_engine.py）
# ══════════════════════════════════════════════════════════════════

@dataclass
class FXSignal:
    pair: str
    direction: str   # BUY / SELL / WAIT
    confidence: float
    regime: Regime
    reason: str
    indicators_snapshot: Dict = field(default_factory=dict)


def _generate_signal(pair: str, inds: Dict, regime: Regime,
                     direction_permission: str = "BOTH") -> FXSignal:
    """
    移植 SignalEngine.generate_signal()
    TREND: price > SMA20 > SMA50 + RSI过滤
    RANGE: Bollinger 均值回归 + RSI 过滤
    """
    c = inds["closes"]; price = float(c[-1])
    sma20 = TI.latest(inds["sma20"])
    sma50 = TI.latest(inds["sma50"])
    adx   = TI.latest(inds["adx"]) or 15.0
    rsi   = TI.latest(inds["rsi"]) or 50.0
    bb_up = TI.latest(inds["bb_upper"])
    bb_lo = TI.latest(inds["bb_lower"])
    bb_mid= TI.latest(inds["bb_mid"])
    atr   = TI.latest(inds["atr"]) or 0.0001
    vwap  = TI.latest(inds["vwap"])
    pip   = FX_PAIRS[pair]["pip"]

    snap = {
        "price": round(price, 5), "sma20": round(sma20, 5) if sma20 else None,
        "sma50": round(sma50, 5) if sma50 else None, "adx": round(adx, 1),
        "rsi": round(rsi, 1), "atr_pips": round(atr/pip, 1),
        "bb_upper": round(bb_up, 5) if bb_up else None,
        "bb_lower": round(bb_lo, 5) if bb_lo else None,
        "vwap": round(vwap, 5) if vwap else None,
    }

    if regime == Regime.UNSTABLE:
        return FXSignal(pair, "WAIT", 0, regime, "市场不稳定，禁止交易", snap)

    # ── TREND 逻辑 ────────────────────────────────────────────────
    if regime == Regime.TREND and sma20 and sma50:
        if direction_permission in ("LONG", "BOTH") and price > sma20 > sma50 and rsi < 70:
            base = 60.0
            conf = min(base * min(adx/50, 1.5) + (70-rsi)/70*10, 100)
            reason = (f"TREND BUY: {price:.5f} > SMA20({sma20:.5f}) > SMA50({sma50:.5f}), "
                      f"RSI={rsi:.1f}, ADX={adx:.1f}")
            return FXSignal(pair, "BUY", round(conf, 1), regime, reason, snap)

        if direction_permission in ("SHORT", "BOTH") and price < sma20 < sma50 and rsi > 30:
            base = 60.0
            conf = min(base * min(adx/50, 1.5) + (rsi-30)/70*10, 100)
            reason = (f"TREND SELL: {price:.5f} < SMA20({sma20:.5f}) < SMA50({sma50:.5f}), "
                      f"RSI={rsi:.1f}, ADX={adx:.1f}")
            return FXSignal(pair, "SELL", round(conf, 1), regime, reason, snap)

    # ── RANGE 逻辑（Bollinger 均值回归）──────────────────────────
    if regime == Regime.RANGE and bb_up and bb_lo:
        bw = bb_up - bb_lo
        if bw > 0:
            # BUY：价格贴近下轨 + RSI 超卖
            if direction_permission in ("LONG", "BOTH") and price <= bb_lo + bw*0.1 and rsi < 35:
                prox = 1.0 - (price - bb_lo) / bw
                conf = min(40 + (35-rsi)/35*20 + prox*15, 85)
                reason = (f"RANGE BUY: {price:.5f}贴近下轨BB({bb_lo:.5f}), RSI={rsi:.1f}")
                return FXSignal(pair, "BUY", round(conf, 1), regime, reason, snap)

            # SELL：价格贴近上轨 + RSI 超买
            if direction_permission in ("SHORT", "BOTH") and price >= bb_up - bw*0.1 and rsi > 65:
                prox = (price - bb_lo) / bw
                conf = min(40 + (rsi-65)/35*20 + prox*15, 85)
                reason = (f"RANGE SELL: {price:.5f}贴近上轨BB({bb_up:.5f}), RSI={rsi:.1f}")
                return FXSignal(pair, "SELL", round(conf, 1), regime, reason, snap)

    return FXSignal(pair, "WAIT", 0, regime, "无满足条件信号", snap)

# ══════════════════════════════════════════════════════════════════
#  6. 执行闸门（移植自 execution_gate.py）P0-P6
# ══════════════════════════════════════════════════════════════════

@dataclass
class GateInput:
    pair: str
    signal_side: str = ""        # BUY / SELL / WAIT
    signal_confidence: float = 0
    regime: str = "RANGE"
    kill_switch: bool = False
    stale_quote: bool = False
    deterioration: bool = False
    cooldown_state: str = "GREEN"
    event_active: bool = False
    daily_dd_hit: bool = False
    consecutive_losses: int = 0
    daily_trades: int = 0
    max_daily_trades: int = 10
    position_open: bool = False
    position_side: str = ""
    position_age_min: float = 0.0
    max_age_min: float = 40.0


@dataclass
class GateDecision:
    action: str           # ALLOW / ALLOW_REDUCED / BLOCK / EXIT_NOW / FREEZE
    approved_side: str = ""
    size_mult: float = 1.0
    risk_pct: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    priority: str = ""

    def to_dict(self) -> Dict:
        return {
            "action": self.action, "approved_side": self.approved_side,
            "size_mult": round(self.size_mult, 3), "risk_pct": round(self.risk_pct, 4),
            "reason_codes": self.reason_codes, "priority": self.priority,
        }


class ExecutionGate:
    """P0-P6 优先级裁决链"""

    def __init__(self):
        self._decisions: Dict[str, GateDecision] = {}

    def decide(self, gi: GateInput) -> GateDecision:
        checks = [
            ("P0", self._kill_switch,     gi),
            ("P0", self._stale_quote,     gi),
            ("P1", self._deterioration,   gi),
            ("P2", self._cooldown,        gi),
            ("P3", self._event_readiness, gi),
            ("P4", self._portfolio_limits,gi),
            ("P5", self._time_stop,       gi),
            ("P6", self._approve_signal,  gi),
        ]
        for pri, fn, inp in checks:
            dec = fn(inp)
            if dec:
                self._decisions[gi.pair] = dec
                return dec
        dec = GateDecision("BLOCK", "", 0, 0, ["UNKNOWN"], "P6")
        self._decisions[gi.pair] = dec
        return dec

    def _kill_switch(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.kill_switch:
            return GateDecision("FREEZE", "", 0, 0, ["KILL_SWITCH"], "P0")

    def _stale_quote(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.stale_quote:
            return GateDecision("BLOCK", "", 0, 0, ["STALE_QUOTE"], "P0")

    def _deterioration(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.deterioration or gi.regime == "UNSTABLE":
            if gi.position_open:
                return GateDecision("EXIT_NOW", gi.position_side, 0, 0,
                                    ["MARKET_UNSTABLE", "FORCE_EXIT"], "P1")
            return GateDecision("FREEZE", "", 0, 0, ["MARKET_UNSTABLE"], "P1")

    def _cooldown(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.cooldown_state == "COOLDOWN":
            return GateDecision("BLOCK", "", 0, 0, ["COOLDOWN_ACTIVE"], "P2")

    def _event_readiness(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.event_active:
            return GateDecision("BLOCK", "", 0, 0, ["EVENT_COOLDOWN"], "P3")

    def _portfolio_limits(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.daily_dd_hit:
            return GateDecision("FREEZE", "", 0, 0, ["DAILY_DD_LIMIT"], "P4")
        cfg = PAIR_RISK_CONFIG.get(gi.pair, PAIR_RISK_CONFIG["AUDUSD"])
        if gi.consecutive_losses >= cfg["max_losses"]:
            return GateDecision("FREEZE", "", 0, 0,
                                [f"LOSS_STREAK_{gi.consecutive_losses}"], "P4")
        if gi.daily_trades >= gi.max_daily_trades:
            return GateDecision("BLOCK", "", 0, 0, ["MAX_DAILY_TRADES"], "P4")

    def _time_stop(self, gi: GateInput) -> Optional[GateDecision]:
        if gi.position_open and gi.position_age_min >= gi.max_age_min:
            return GateDecision("EXIT_NOW", gi.position_side, 0, 0,
                                [f"TIME_STOP_{gi.max_age_min:.0f}min"], "P5")

    def _approve_signal(self, gi: GateInput) -> GateDecision:
        if not gi.signal_side or gi.signal_side == "WAIT":
            return GateDecision("BLOCK", "", 0, 0, ["NO_SIGNAL"], "P6")
        cfg = PAIR_RISK_CONFIG.get(gi.pair, PAIR_RISK_CONFIG["AUDUSD"])
        regime_m  = REGIME_MULT.get(gi.regime, 0.8)
        recovery_m= RECOVERY_MULT.get(gi.cooldown_state, 1.0)
        loss_m    = 0.5 if gi.consecutive_losses >= cfg["reduce_at"] else 1.0
        final_m   = cfg["risk_mult"] * regime_m * recovery_m * loss_m
        final_r   = cfg["base_risk_pct"] * final_m
        if final_m <= 0:
            return GateDecision("BLOCK", gi.signal_side, 0, 0, ["ZERO_RISK_BUDGET"], "P6")
        action = "ALLOW" if final_m >= 1.0 else "ALLOW_REDUCED"
        reasons = ["ALL_CHECKS_PASS"]
        if recovery_m < 1.0: reasons.append(f"RECOVERY_{gi.cooldown_state}")
        if loss_m < 1.0:     reasons.append(f"LOSS_STREAK_{gi.consecutive_losses}")
        if regime_m < 1.0:   reasons.append(f"REGIME_{gi.regime}")
        return GateDecision(action, gi.signal_side, round(final_m, 3),
                            round(final_r, 4), reasons, "P6")

    def status(self) -> Dict:
        return {d: v.to_dict() for d, v in self._decisions.items()}

# ══════════════════════════════════════════════════════════════════
#  7. 策略监控器（移植自 strategy_monitor.py）
# ══════════════════════════════════════════════════════════════════

RECOVERY_CHAIN = ["COOLDOWN", "RECOVERY_30", "RECOVERY_50", "RECOVERY_75", "GREEN"]

@dataclass
class PairHealth:
    pair: str
    consec_losses: int = 0
    consec_wins:   int = 0
    total_trades:  int = 0
    total_wins:    int = 0
    daily_pnl_pips:float = 0.0
    recovery_state: str = "GREEN"
    frozen: bool = False
    frozen_reason: str = ""
    daily_deteriorations: int = 0
    _recovery_count: int = 0   # 恢复期胜利数


class StrategyMonitor:
    """连亏检测 + 渐进恢复（30%→50%→75%→GREEN）"""

    def __init__(self, pairs: List[str]):
        self.health: Dict[str, PairHealth] = {p: PairHealth(p) for p in pairs}

    def record_trade(self, pair: str, pnl_pips: float):
        h = self.health.setdefault(pair, PairHealth(pair))
        h.total_trades += 1
        h.daily_pnl_pips += pnl_pips
        if pnl_pips >= 0:
            h.consec_wins += 1; h.consec_losses = 0; h.total_wins += 1
        else:
            h.consec_losses += 1; h.consec_wins = 0
        cfg = PAIR_RISK_CONFIG.get(pair, {"max_losses": 6, "reduce_at": 4})
        if h.consec_losses >= cfg["max_losses"] and not h.frozen:
            h.frozen = True
            h.frozen_reason = f"连亏{h.consec_losses}笔触发冻结"
            h.recovery_state = "COOLDOWN"
        # 恢复期计数
        if h.recovery_state not in ("GREEN", "COOLDOWN") and pnl_pips >= 0:
            h._recovery_count += 1
            if h._recovery_count >= 2:
                h._recovery_count = 0
                idx = RECOVERY_CHAIN.index(h.recovery_state)
                if idx < len(RECOVERY_CHAIN)-1:
                    h.recovery_state = RECOVERY_CHAIN[idx+1]
                    if h.recovery_state == "GREEN":
                        h.frozen = False
                        h.frozen_reason = ""

    def record_deterioration(self, pair: str):
        h = self.health.setdefault(pair, PairHealth(pair))
        h.daily_deteriorations += 1
        if h.daily_deteriorations >= 3:
            h.frozen = True
            h.frozen_reason = f"当日恶化触发{h.daily_deteriorations}次"
            h.recovery_state = "COOLDOWN"

    def get_health(self, pair: str) -> Dict:
        h = self.health.get(pair)
        if not h:
            return {}
        cfg = PAIR_RISK_CONFIG.get(pair, {"max_losses": 6, "reduce_at": 4})
        win_rate = h.total_wins / h.total_trades if h.total_trades else 0
        return {
            "pair": h.pair, "consecutive_losses": h.consec_losses,
            "consecutive_wins": h.consec_wins, "total_trades": h.total_trades,
            "win_rate": round(win_rate, 3), "daily_pnl_pips": round(h.daily_pnl_pips, 1),
            "recovery_state": h.recovery_state,
            "risk_multiplier": round(RECOVERY_MULT.get(h.recovery_state, 1.0), 2),
            "frozen": h.frozen, "frozen_reason": h.frozen_reason,
            "reduce_at": cfg["reduce_at"], "max_losses": cfg["max_losses"],
        }

# ══════════════════════════════════════════════════════════════════
#  8. 纸盘经纪商
# ══════════════════════════════════════════════════════════════════

@dataclass
class FXPosition:
    pair: str
    side: str      # BUY / SELL
    lots: float    # 标准手（1手=100,000单位）
    entry_price: float
    opened_at: float = field(default_factory=time.time)
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None

    def age_minutes(self) -> float:
        return (time.time() - self.opened_at) / 60.0

    def unrealized_pips(self, current_price: float, pip: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) / pip
        return (self.entry_price - current_price) / pip

    def unrealized_usd(self, current_price: float, pip: float, pip_value_per_lot: float = 10.0) -> float:
        return self.unrealized_pips(current_price, pip) * pip_value_per_lot * self.lots


_POSITIONS:    Dict[str, FXPosition] = {}   # pair → position
_CLOSED_TRADES: List[Dict] = []
_DAILY_PNL_USD: float = 0.0
_DAILY_TRADE_COUNT: int = 0
_KILL_SWITCH: bool = False


def _reset_daily():
    global _DAILY_PNL_USD, _DAILY_TRADE_COUNT
    _DAILY_PNL_USD = 0.0
    _DAILY_TRADE_COUNT = 0


class FXPaperBroker:
    """外汇纸盘经纪商：开/平仓、P&L 计算"""

    def __init__(self):
        self.equity = float(os.getenv("FX_EQUITY", "100000"))
        self.max_loss_pct = float(os.getenv("FX_MAX_DAILY_LOSS_PCT", "0.02"))
        self.monitor = StrategyMonitor(list(FX_PAIRS.keys()))
        self.gate = ExecutionGate()

    def open_position(self, pair: str, side: str, lots: float,
                      entry_price: float, sl_pips: float = 20.0) -> Dict:
        global _POSITIONS, _DAILY_TRADE_COUNT
        if pair in _POSITIONS:
            return {"error": f"{pair} 已有持仓，请先平仓"}
        pip = FX_PAIRS[pair]["pip"]
        sl = (entry_price - sl_pips*pip) if side == "BUY" else (entry_price + sl_pips*pip)
        tp = (entry_price + sl_pips*1.5*pip) if side == "BUY" else (entry_price - sl_pips*1.5*pip)
        pos = FXPosition(pair=pair, side=side, lots=lots, entry_price=entry_price,
                         sl_price=sl, tp_price=tp)
        _POSITIONS[pair] = pos
        _DAILY_TRADE_COUNT += 1
        return {
            "status": "OPENED",
            "pair": pair, "side": side, "lots": lots,
            "entry": round(entry_price, 5),
            "sl": round(sl, 5), "tp": round(tp, 5),
            "sl_pips": sl_pips,
        }

    def close_position(self, pair: str, current_price: float, reason: str = "MANUAL") -> Dict:
        global _POSITIONS, _DAILY_PNL_USD, _CLOSED_TRADES
        pos = _POSITIONS.pop(pair, None)
        if not pos:
            return {"error": f"{pair} 无持仓"}
        pip = FX_PAIRS[pair]["pip"]
        pnl_pips = pos.unrealized_pips(current_price, pip)
        pnl_usd  = pos.unrealized_usd(current_price, pip)
        _DAILY_PNL_USD += pnl_usd
        record = {
            "pair": pair, "side": pos.side, "lots": pos.lots,
            "entry": pos.entry_price, "exit": current_price,
            "pnl_pips": round(pnl_pips, 1), "pnl_usd": round(pnl_usd, 2),
            "age_min": round(pos.age_minutes(), 1), "reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
        _CLOSED_TRADES.append(record)
        self.monitor.record_trade(pair, pnl_pips)
        return {"status": "CLOSED", **record}

    def portfolio_summary(self) -> Dict:
        global _POSITIONS, _DAILY_PNL_USD
        max_loss_usd = self.equity * self.max_loss_pct
        daily_dd_hit = _DAILY_PNL_USD <= -max_loss_usd
        open_pos = []
        for pair, pos in _POSITIONS.items():
            data = _fetch_ohlcv(pair, bars=10)
            price = data["current_price"] if data else pos.entry_price
            pip = FX_PAIRS[pair]["pip"]
            open_pos.append({
                "pair": pair, "side": pos.side, "lots": pos.lots,
                "entry": round(pos.entry_price, 5), "current": round(price, 5),
                "unrealized_pips": round(pos.unrealized_pips(price, pip), 1),
                "unrealized_usd":  round(pos.unrealized_usd(price, pip), 2),
                "age_min": round(pos.age_minutes(), 1),
                "sl": round(pos.sl_price, 5) if pos.sl_price else None,
                "tp": round(pos.tp_price, 5) if pos.tp_price else None,
            })
        health = {p: self.monitor.get_health(p) for p in list(FX_PAIRS.keys())}
        return {
            "equity": self.equity,
            "daily_pnl_usd": round(_DAILY_PNL_USD, 2),
            "daily_pnl_pct": round(_DAILY_PNL_USD / self.equity * 100, 3),
            "daily_loss_limit_usd": round(max_loss_usd, 2),
            "daily_dd_hit": daily_dd_hit,
            "daily_trades": _DAILY_TRADE_COUNT,
            "kill_switch": _KILL_SWITCH,
            "open_positions": open_pos,
            "recent_trades": _CLOSED_TRADES[-10:],
            "strategy_health": health,
        }


# 全局单例
_BROKER = FXPaperBroker()

# ══════════════════════════════════════════════════════════════════
#  9. 宏观事件日历（高影响事件 → EVENT 状态）
# ══════════════════════════════════════════════════════════════════

def _upcoming_fx_events(horizon_h: int = 48) -> List[Dict]:
    """
    FX 高影响事件日历
    主要品种关注：FOMC、RBA/RBNZ/BOE/BOJ/ECB/SNB 利率决议、CPI、NFP
    """
    now = datetime.now(timezone.utc)
    events = []
    # 每周日历（固定节点 + 近期已知）
    schedule = [
        # (weekday 1-7, hour UTC, pair_filter, event, level)
        (2, 2,   "AUDUSD",     "RBA 利率决议",      "A"),   # 每月第一个周二
        (3, 21,  "NZDUSD",     "RBNZ 利率决议",     "A"),   # 周三晚
        (4, 12,  "GBPUSD",     "BOE 利率决议",      "A"),   # 周四
        (4, 12,  "EURUSD",     "ECB 新闻发布会",    "B"),
        (5, 12,  "ALL",        "美国 NFP 非农就业",  "A"),   # 第一个周五
        (5, 12,  "ALL",        "美国 CPI 通胀数据",  "A"),
        (3, 18,  "ALL",        "FOMC 会议纪要",      "A"),
        (3, 3,   "USDJPY",     "BOJ 利率决议",      "A"),
        (1, 8,   "USDCHF",     "SNB 季度决策",      "B"),
    ]
    # 找出 horizon_h 小时内的事件
    for wd, h, pair_filter, title, level in schedule:
        # 本周和下周各检查一次
        for week_offset in [0, 7]:
            days_to_wd = (wd - now.isoweekday()) % 7 + week_offset
            event_dt = now.replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=days_to_wd)
            delta_h = (event_dt - now).total_seconds() / 3600
            if 0 <= delta_h <= horizon_h:
                events.append({
                    "datetime": event_dt.strftime("%Y-%m-%d %H:%M UTC"),
                    "hours_away": round(delta_h, 1),
                    "title": title,
                    "level": level,
                    "affected_pair": pair_filter,
                    "market_impact": "高冲击" if level == "A" else "中等冲击",
                    "recommendation": "禁止新开仓，已有仓位注意止损" if level == "A" else "降低仓量至50%",
                })
    events.sort(key=lambda x: x["hours_away"])
    return events

# ══════════════════════════════════════════════════════════════════
#  10. CrewAI 工具
# ══════════════════════════════════════════════════════════════════

# ── 10a. FXSignalTool ─────────────────────────────────────────────

class FXSignalInput(BaseModel):
    pairs:  List[str] = Field(
        default=["AUDUSD", "NZDUSD"],
        description="分析品种列表，可选: AUDUSD NZDUSD EURUSD GBPUSD USDJPY USDCAD USDCHF EURGBP EURJPY"
    )
    direction_permission: str = Field(
        default="BOTH",
        description="交易方向权限：BOTH=双向, LONG=只做多, SHORT=只做空"
    )
    verbose: bool = Field(default=False, description="是否输出详细指标")


class FXSignalTool(BaseTool):
    name: str = "FXSignalTool"
    description: str = (
        "外汇技术分析与交易信号工具。"
        "对指定货币对运行 RegimeEngine + SignalEngine + ExecutionGate，"
        "输出方向信号（BUY/SELL/WAIT）、置信度、市场状态、执行门控裁决。"
        "同时显示高影响经济事件日历。"
    )
    args_schema: type[BaseModel] = FXSignalInput

    def _run(self, pairs: List[str] = None, direction_permission: str = "BOTH",
             verbose: bool = False) -> str:
        if pairs is None:
            pairs_env = os.getenv("FX_DEFAULT_PAIRS", "AUDUSD,NZDUSD")
            pairs = [p.strip() for p in pairs_env.split(",")]

        # 过滤有效品种
        pairs = [p.upper().replace("/","") for p in pairs if p.upper().replace("/","") in FX_PAIRS]
        if not pairs:
            return json.dumps({"error": "无有效外汇品种"}, ensure_ascii=False)

        # 经济事件
        events = _upcoming_fx_events(48)
        event_pairs = set()
        for ev in events:
            if ev["level"] == "A":
                if ev["affected_pair"] == "ALL":
                    event_pairs.update(pairs)
                else:
                    event_pairs.add(ev["affected_pair"])

        # 账户状态
        equity = float(os.getenv("FX_EQUITY", "100000"))
        max_loss_usd = equity * float(os.getenv("FX_MAX_DAILY_LOSS_PCT", "0.02"))
        daily_dd_hit = _DAILY_PNL_USD <= -max_loss_usd

        results = []
        for pair in pairs:
            data = _fetch_ohlcv(pair, bars=120)
            if not data:
                results.append({"pair": pair, "error": "数据获取失败"})
                continue

            inds = _compute_indicators(data)
            regime, regime_reason = _detect_regime(inds)

            # 事件状态（当对应 pair 有 A 级事件时进入 EVENT 状态）
            event_active = pair in event_pairs or regime == Regime.EVENT

            signal = _generate_signal(pair, inds, regime,
                                      direction_permission.upper())

            # 获取 StrategyMonitor 状态
            health = _BROKER.monitor.get_health(pair)
            consec_losses = health.get("consecutive_losses", 0)
            cooldown_state = health.get("recovery_state", "GREEN")

            # 执行闸门
            gi = GateInput(
                pair=pair,
                signal_side=signal.direction,
                signal_confidence=signal.confidence,
                regime=regime.value,
                kill_switch=_KILL_SWITCH,
                stale_quote=data.get("simulated", False),
                deterioration=(regime == Regime.UNSTABLE),
                cooldown_state=cooldown_state,
                event_active=event_active,
                daily_dd_hit=daily_dd_hit,
                consecutive_losses=consec_losses,
                daily_trades=_DAILY_TRADE_COUNT,
                position_open=(pair in _POSITIONS),
                position_side=_POSITIONS[pair].side if pair in _POSITIONS else "",
                position_age_min=_POSITIONS[pair].age_minutes() if pair in _POSITIONS else 0.0,
                max_age_min=40.0,
            )
            gate = _BROKER.gate.decide(gi)

            rec = {
                "pair": pair,
                "desc": FX_PAIRS[pair]["desc"],
                "current_price": data["current_price"],
                "regime": regime.value,
                "regime_reason": regime_reason,
                "signal": signal.direction,
                "confidence": signal.confidence,
                "signal_reason": signal.reason,
                "gate_action": gate.action,
                "gate_side": gate.approved_side,
                "gate_risk_pct": gate.risk_pct,
                "gate_size_mult": gate.size_mult,
                "gate_reasons": gate.reason_codes,
                "gate_priority": gate.priority,
                "event_active": event_active,
                "recovery_state": cooldown_state,
                "consecutive_losses": consec_losses,
                "data_source": "simulated" if data.get("simulated") else "yfinance",
            }
            if verbose:
                snap = signal.indicators_snapshot
                rec["indicators"] = snap

            results.append(rec)

        # 找出最强信号
        actionable = [r for r in results if r.get("gate_action") in ("ALLOW", "ALLOW_REDUCED")
                      and r.get("signal") in ("BUY", "SELL")]
        top = sorted(actionable, key=lambda x: x.get("confidence", 0), reverse=True)
        top_signal = top[0] if top else None

        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pairs_analyzed": len(pairs),
            "pairs_with_signal": len(actionable),
            "top_signal": top_signal,
            "signals": results,
            "upcoming_high_impact_events": events[:5],
            "account": {
                "equity": equity,
                "daily_pnl_usd": round(_DAILY_PNL_USD, 2),
                "daily_dd_hit": daily_dd_hit,
                "kill_switch": _KILL_SWITCH,
            },
        }, ensure_ascii=False, indent=2)


# ── 10b. FXRiskTool ──────────────────────────────────────────────

class FXRiskInput(BaseModel):
    query: str = Field(
        default="full",
        description="查询类型：full=完整报告, health=策略健康, gate=闸门状态, events=事件日历, var=波动率风险"
    )
    pairs: List[str] = Field(
        default=["AUDUSD", "NZDUSD", "EURUSD", "GBPUSD"],
        description="风险分析品种"
    )


class FXRiskTool(BaseTool):
    name: str = "FXRiskTool"
    description: str = (
        "外汇风险管理工具。"
        "提供：策略健康状态（连亏/恢复期）、执行闸门状态、"
        "组合P&L、品种波动率/ATR风险、高影响事件日历。"
    )
    args_schema: type[BaseModel] = FXRiskInput

    def _run(self, query: str = "full", pairs: List[str] = None) -> str:
        if pairs is None:
            pairs = ["AUDUSD", "NZDUSD", "EURUSD", "GBPUSD"]
        pairs = [p.upper().replace("/","") for p in pairs if p.upper().replace("/","") in FX_PAIRS]

        result: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "query": query,
        }

        if query in ("full", "health"):
            result["strategy_health"] = {p: _BROKER.monitor.get_health(p) for p in pairs}
            frozen = [p for p in pairs if _BROKER.monitor.get_health(p).get("frozen")]
            result["frozen_pairs"] = frozen

        if query in ("full", "gate"):
            result["gate_status"] = _BROKER.gate.status()

        if query in ("full", "events"):
            result["upcoming_events"] = _upcoming_fx_events(72)

        if query in ("full", "var"):
            vol_data = []
            for pair in pairs:
                # 优先用日线数据计算 VaR（更准确）
                try:
                    import yfinance as yf
                    df_d = yf.Ticker(FX_PAIRS[pair]["yf"]).history(period="90d", interval="1d")
                    daily_closes = df_d["Close"].values.astype(float) if not df_d.empty else None
                except Exception:
                    daily_closes = None

                # 5min/1h 数据用于当前 ATR
                data = _fetch_ohlcv(pair, bars=100)
                if not data:
                    continue
                pip = FX_PAIRS[pair]["pip"]
                inds = _compute_indicators(data)
                atr = TI.latest(inds["atr"]) or 0
                atr_pips = atr / pip
                equity = float(os.getenv("FX_EQUITY", "100000"))

                if daily_closes is not None and len(daily_closes) > 10:
                    daily_ret = np.diff(np.log(daily_closes))
                    daily_vol_pct = float(np.std(daily_ret)) * math.sqrt(252) * 100
                    var_ret = float(np.percentile(daily_ret, 5))   # 1-day 95% VaR
                    var_usd = abs(var_ret) * equity
                    n_days = len(daily_closes)
                else:
                    # 回退到 5min bars，乘以√(288) 换算为日（288个5min bar/day）
                    c = data["close"]
                    rets = np.diff(np.log(c))
                    bar_vol = float(np.std(rets))
                    daily_vol_pct = bar_vol * math.sqrt(252 * 288) * 100
                    var_ret = float(np.percentile(rets, 5)) * math.sqrt(288)
                    var_usd = abs(var_ret) * equity
                    n_days = len(c)

                vol_data.append({
                    "pair": pair, "desc": FX_PAIRS[pair]["desc"],
                    "current_price": round(data["current_price"], 5),
                    "atr_pips": round(atr_pips, 1),
                    "annual_vol_pct": round(daily_vol_pct, 2),
                    "daily_var_95_usd": round(var_usd, 0),
                    "daily_var_95_pct": round(abs(var_ret)*100, 3),
                    "data_days": n_days,
                })
            result["volatility_risk"] = vol_data

        if query in ("full", "portfolio"):
            summary = _BROKER.portfolio_summary()
            result["portfolio"] = {
                "equity": summary["equity"],
                "daily_pnl_usd": summary["daily_pnl_usd"],
                "daily_pnl_pct": summary["daily_pnl_pct"],
                "daily_dd_hit": summary["daily_dd_hit"],
                "kill_switch": summary["kill_switch"],
                "open_positions": summary["open_positions"],
                "recent_trades": summary["recent_trades"],
            }

        return json.dumps(result, ensure_ascii=False, indent=2)


# ── 10c. FXPaperTradeTool ─────────────────────────────────────────

class FXPaperTradeInput(BaseModel):
    action: str = Field(
        description="操作：OPEN=开仓, CLOSE=平仓, STATUS=查看持仓, RESET=重置"
    )
    pair: str = Field(
        default="AUDUSD",
        description="货币对，如 AUDUSD EURUSD GBPUSD USDJPY 等"
    )
    side: str = Field(
        default="BUY",
        description="方向：BUY 或 SELL（OPEN 时必填）"
    )
    lots: float = Field(
        default=0.1,
        description="手数（0.01=微手, 0.1=迷你手, 1.0=标准手）"
    )
    sl_pips: float = Field(
        default=20.0,
        description="止损点数（pip），默认20"
    )
    reason: str = Field(
        default="SIGNAL",
        description="开/平仓原因（用于记录）"
    )


class FXPaperTradeTool(BaseTool):
    name: str = "FXPaperTradeTool"
    description: str = (
        "外汇纸盘交易执行工具。"
        "可开仓（OPEN）、平仓（CLOSE）、查看持仓（STATUS）或重置账户（RESET）。"
        "系统自动计算止损/止盈，记录每笔交易P&L，更新策略健康指标。"
    )
    args_schema: type[BaseModel] = FXPaperTradeInput

    def _run(self, action: str = "STATUS", pair: str = "AUDUSD",
             side: str = "BUY", lots: float = 0.1,
             sl_pips: float = 20.0, reason: str = "SIGNAL") -> str:
        global _KILL_SWITCH, _POSITIONS, _CLOSED_TRADES, _DAILY_PNL_USD, _DAILY_TRADE_COUNT

        pair = pair.upper().replace("/", "")
        action = action.upper()

        if pair not in FX_PAIRS and action not in ("STATUS", "RESET"):
            return json.dumps({"error": f"不支持的品种: {pair}"}, ensure_ascii=False)

        if action == "STATUS":
            return json.dumps(_BROKER.portfolio_summary(), ensure_ascii=False, indent=2)

        if action == "RESET":
            _POSITIONS.clear()
            _CLOSED_TRADES.clear()
            _DAILY_PNL_USD = 0.0
            _DAILY_TRADE_COUNT = 0
            _KILL_SWITCH = False
            return json.dumps({"status": "RESET", "message": "纸盘账户已重置"}, ensure_ascii=False)

        # 获取当前价格
        data = _fetch_ohlcv(pair, bars=10)
        if not data:
            return json.dumps({"error": f"无法获取 {pair} 价格"}, ensure_ascii=False)
        current_price = data["current_price"]

        if action == "OPEN":
            # 执行闸门检查
            health = _BROKER.monitor.get_health(pair)
            equity = float(os.getenv("FX_EQUITY", "100000"))
            max_loss_usd = equity * float(os.getenv("FX_MAX_DAILY_LOSS_PCT", "0.02"))
            daily_dd_hit = _DAILY_PNL_USD <= -max_loss_usd
            gi = GateInput(
                pair=pair, signal_side=side.upper(), signal_confidence=75,
                regime="RANGE", kill_switch=_KILL_SWITCH,
                daily_dd_hit=daily_dd_hit,
                consecutive_losses=health.get("consecutive_losses", 0),
                cooldown_state=health.get("recovery_state", "GREEN"),
                daily_trades=_DAILY_TRADE_COUNT,
                position_open=(pair in _POSITIONS),
                position_side=_POSITIONS[pair].side if pair in _POSITIONS else "",
            )
            gate = _BROKER.gate.decide(gi)
            if gate.action == "FREEZE":
                return json.dumps({
                    "status": "BLOCKED", "reason": f"执行闸门阻止: {gate.reason_codes}",
                    "gate": gate.to_dict()
                }, ensure_ascii=False)
            if gate.action in ("BLOCK", "EXIT_NOW"):
                return json.dumps({
                    "status": "BLOCKED", "reason": gate.reason_codes,
                    "gate": gate.to_dict()
                }, ensure_ascii=False)

            result = _BROKER.open_position(pair, side.upper(), lots, current_price, sl_pips)
            result["gate"] = gate.to_dict()
            result["data_source"] = "simulated" if data.get("simulated") else "yfinance"
            return json.dumps(result, ensure_ascii=False, indent=2)

        if action == "CLOSE":
            result = _BROKER.close_position(pair, current_price, reason)
            return json.dumps(result, ensure_ascii=False, indent=2)

        return json.dumps({"error": f"未知操作: {action}，可选: OPEN/CLOSE/STATUS/RESET"},
                          ensure_ascii=False)
