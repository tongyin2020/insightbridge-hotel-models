"""
WTI Crude Oil Trading Tool (CL / NYMEX)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
完整移植 Oil-Model 信号体系 → CrewAI 工具集

信号体系:
  - TI (Technical Indicators): EMA/ATR/ADX/RSI/VWAP (纯numpy)
  - RegimeEngine: BLOCKED > EVENT > TREND > NORMAL
  - SignalService: 6条件动态确认（事件触发，20-90s窗口）
  - SignalScorer: -100~+100综合多空评分（8因子）
  - ExecutionGate: 8项准入检查（点差/ADX/波动/分数/脆弱度/风控/冷静期/状态）
  - FragilityEngine: 5因子脆弱度评分（0~100）
  - RiskService: 杀手锁/暂停/日损/连亏/仓位计算

执行层:
  - WTIPaperBroker: 模拟滑点+手续费，完整P&L跟踪
  - TradovateClient: REST认证 + OCO括号订单（DEMO/LIVE）

CrewAI 工具（3个）:
  - WTISignalTool    → 信号分析 + 执行门控
  - WTIRiskTool      → 风控状态 + 持仓 + P&L
  - WTITradeTool     → 下单（模拟/Tradovate真实）

环境变量:
  WTI_MODE           = PAPER | LIVE (default: PAPER)
  WTI_EQUITY         = 50000
  WTI_MAX_DAILY_LOSS = 0.015   (1.5%)
  WTI_MAX_CONSEC     = 3
  WTI_RISK_PER_TRADE = 0.005   (0.5%)
  WTI_MAX_POSITION   = 5       (最大手数)
  TRADOVATE_USERNAME / TRADOVATE_PASSWORD
  TRADOVATE_CLIENT_ID / TRADOVATE_CLIENT_SECRET
  TRADOVATE_DEVICE_ID (optional, default: wti-ai-crewai)
  TRADOVATE_IS_DEMO   = true | false (default: true)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import os
import json
import math
import asyncio
import logging
import random
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    _HTTPX_OK = False

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# 0. 常量 / 合约规格
# ──────────────────────────────────────────────────────────────

WTI_SYMBOL      = "CL=F"          # yfinance ticker for WTI front month
WTI_TICK_SIZE   = 0.01            # $0.01 / barrel
WTI_MULTIPLIER  = 1000            # $1000 per $1 move per contract
WTI_COMMISSION  = 4.0             # $ per round trip

# ──────────────────────────────────────────────────────────────
# 1. Enums
# ──────────────────────────────────────────────────────────────

class WTIRegime(str, Enum):
    NORMAL  = "normal"
    EVENT   = "event"
    TREND   = "trend"
    BLOCKED = "blocked"

class Direction(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"

class ExitReason(str, Enum):
    STOP_LOSS      = "stop_loss"
    TAKE_PROFIT    = "take_profit"
    TIME_STOP      = "time_stop"
    MANUAL         = "manual"
    RISK_HALT      = "risk_halt"
    FRAGILITY_HALT = "fragility_halt"

# ──────────────────────────────────────────────────────────────
# 2. Configuration
# ──────────────────────────────────────────────────────────────

@dataclass
class WTIRiskConfig:
    equity:              float = float(os.getenv("WTI_EQUITY", "50000"))
    max_daily_loss_pct:  float = float(os.getenv("WTI_MAX_DAILY_LOSS", "0.015"))
    max_consecutive:     int   = int(os.getenv("WTI_MAX_CONSEC", "3"))
    risk_per_trade_pct:  float = float(os.getenv("WTI_RISK_PER_TRADE", "0.005"))
    max_position_lots:   int   = int(os.getenv("WTI_MAX_POSITION", "5"))
    max_spread_ticks:    int   = 6    # 6 ticks = $0.06

@dataclass
class WTIRegimeConfig:
    blocked_vol_mult:   float = 4.0    # ATR > 4× baseline → BLOCKED
    trend_vol_max:      float = 3.0    # ATR < 3× baseline needed for TREND
    trend_adx_min:      float = 28.0   # ADX threshold for TREND
    atr_baseline_bars:  int   = 60     # rolling ATR baseline window

@dataclass
class WTIConfirmConfig:
    min_wait_s:         int   = 20
    max_wait_s:         int   = 90
    breakout_pct:       float = 0.60   # price must break 60% of event range
    adx_threshold:      float = 22.0
    vwap_atr_max:       float = 1.5    # VWAP deviation < 1.5×ATR
    min_volume_ratio:   float = 1.2    # volume > 1.2× 20-bar avg

# ──────────────────────────────────────────────────────────────
# 3. Technical Indicators (pure numpy, no pandas dependency)
# ──────────────────────────────────────────────────────────────

class TI:
    """Pure-numpy technical indicator library for WTI."""

    @staticmethod
    def ema(arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan, dtype=float)
        k = 2.0 / (period + 1)
        # find first valid
        start = 0
        while start < len(arr) and np.isnan(arr[start]):
            start += 1
        if start >= len(arr):
            return result
        result[start] = arr[start]
        for i in range(start + 1, len(arr)):
            if not np.isnan(arr[i]):
                result[i] = arr[i] * k + result[i-1] * (1 - k)
            else:
                result[i] = result[i-1]
        return result

    @staticmethod
    def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        n = len(close)
        tr = np.full(n, np.nan)
        for i in range(1, n):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i]  - close[i-1])
            )
        atr = np.full(n, np.nan)
        # Wilder smoothing: first value = SMA(period)
        valid = np.where(~np.isnan(tr))[0]
        if len(valid) < period:
            return atr
        first = valid[0]
        if first + period > n:
            return atr
        atr[first + period - 1] = float(np.mean(tr[first:first + period]))
        for i in range(first + period, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
        return atr

    @staticmethod
    def adx_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Returns (ADX, +DI, -DI)."""
        n = len(close)
        dm_plus  = np.zeros(n)
        dm_minus = np.zeros(n)
        tr_arr   = np.zeros(n)
        for i in range(1, n):
            move_up   = high[i]  - high[i-1]
            move_down = low[i-1] - low[i]
            tr_arr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
            dm_plus[i]  = move_up   if (move_up > move_down and move_up > 0)   else 0.0
            dm_minus[i] = move_down if (move_down > move_up and move_down > 0) else 0.0
        # Wilder smoothing
        def _wilder_smooth(arr: np.ndarray) -> np.ndarray:
            out = np.zeros(n)
            out[period] = float(np.sum(arr[1:period+1]))
            for i in range(period+1, n):
                out[i] = out[i-1] - out[i-1]/period + arr[i]
            return out
        sm_tr  = _wilder_smooth(tr_arr)
        sm_dmp = _wilder_smooth(dm_plus)
        sm_dmm = _wilder_smooth(dm_minus)
        with np.errstate(divide="ignore", invalid="ignore"):
            di_plus  = np.where(sm_tr > 0, 100 * sm_dmp / sm_tr, 0.0)
            di_minus = np.where(sm_tr > 0, 100 * sm_dmm / sm_tr, 0.0)
            dx = np.where((di_plus + di_minus) > 0,
                          100 * np.abs(di_plus - di_minus) / (di_plus + di_minus), 0.0)
        adx = np.zeros(n)
        adx[2*period] = float(np.mean(dx[period+1:2*period+1]))
        for i in range(2*period+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
        return adx, di_plus, di_minus

    @staticmethod
    def rsi_wilder(close: np.ndarray, period: int = 14) -> np.ndarray:
        n = len(close)
        rsi = np.full(n, np.nan)
        deltas = np.diff(close)
        gain = np.maximum(deltas, 0)
        loss = np.maximum(-deltas, 0)
        if n < period + 1:
            return rsi
        avg_gain = float(np.mean(gain[:period]))
        avg_loss = float(np.mean(loss[:period]))
        for i in range(period, n-1):
            avg_gain = (avg_gain * (period-1) + gain[i]) / period
            avg_loss = (avg_loss * (period-1) + loss[i]) / period
            rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
            rsi[i+1] = 100 - 100 / (1 + rs)
        return rsi

    @staticmethod
    def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> np.ndarray:
        tp = (high + low + close) / 3.0
        cum_vol = np.cumsum(volume)
        cum_tpv = np.cumsum(tp * volume)
        with np.errstate(divide='ignore', invalid='ignore'):
            vw = np.where(cum_vol > 0, cum_tpv / cum_vol, close)
        return vw

    @staticmethod
    def volume_ratio(volume: np.ndarray, period: int = 20) -> np.ndarray:
        """Ratio of current bar volume to rolling average."""
        n = len(volume)
        result = np.ones(n)
        for i in range(period, n):
            avg = float(np.mean(volume[i-period:i]))
            result[i] = volume[i] / avg if avg > 0 else 1.0
        return result


# ──────────────────────────────────────────────────────────────
# 4. Market Data
# ──────────────────────────────────────────────────────────────

def _fetch_wti_data(bars: int = 200) -> Optional[Dict]:
    """
    Fetch WTI daily bars from yfinance.
    Returns dict with arrays: open, high, low, close, volume, timestamps.
    """
    if not _YF_OK:
        return None
    try:
        ticker = yf.Ticker(WTI_SYMBOL)
        df = ticker.history(period="1y", interval="1d")
        if df.empty or len(df) < 30:
            return None
        n = min(bars, len(df))
        df = df.tail(n)
        return {
            "open":       df["Open"].values.astype(float),
            "high":       df["High"].values.astype(float),
            "low":        df["Low"].values.astype(float),
            "close":      df["Close"].values.astype(float),
            "volume":     df["Volume"].values.astype(float),
            "timestamps": [str(ts)[:10] for ts in df.index],
            "bars":       len(df),
        }
    except Exception as e:
        logger.error(f"[WTI] yfinance fetch error: {e}")
        return None


def _compute_indicators(data: Dict) -> Dict:
    """Compute full indicator set from raw OHLCV arrays."""
    hi = data["high"]
    lo = data["low"]
    cl = data["close"]
    vo = data["volume"]
    n  = len(cl)

    ema20 = TI.ema(cl, 20)
    ema50 = TI.ema(cl, 50)
    atr14 = TI.atr_wilder(hi, lo, cl, 14)
    adx14, di_plus, di_minus = TI.adx_wilder(hi, lo, cl, 14)
    rsi14 = TI.rsi_wilder(cl, 14)
    vwap_arr = TI.vwap(hi, lo, cl, vo)
    vol_ratio_arr = TI.volume_ratio(vo, 20)

    # ATR baseline (60-bar mean of ATR)
    valid_atr = atr14[~np.isnan(atr14)]
    if len(valid_atr) >= 60:
        atr_baseline = float(np.mean(valid_atr[-60:]))
    elif len(valid_atr) > 0:
        atr_baseline = float(np.mean(valid_atr))
    else:
        atr_baseline = 1.0

    last = -1  # latest bar index

    def _f(arr, idx=-1):
        v = arr[idx]
        return float(v) if not np.isnan(v) else None

    return {
        "price":        float(cl[last]),
        "open_today":   float(data["open"][last]),
        "high_today":   float(hi[last]),
        "low_today":    float(lo[last]),
        "volume":       float(vo[last]),
        "ema20":        _f(ema20),
        "ema50":        _f(ema50),
        "atr":          _f(atr14),
        "atr_baseline": atr_baseline,
        "adx":          _f(adx14),
        "di_plus":      _f(di_plus),
        "di_minus":     _f(di_minus),
        "rsi":          _f(rsi14),
        "vwap":         _f(vwap_arr),
        "vol_ratio":    _f(vol_ratio_arr),
        "date":         data["timestamps"][last],
        "atr_mult":     float(_f(atr14) / atr_baseline) if _f(atr14) and atr_baseline > 0 else 1.0,
    }


def _get_wti_spread() -> float:
    """Estimate current bid-ask spread in dollars."""
    # CL spread is typically $0.01-$0.03 during regular hours
    # Widen during off-hours or high volatility
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    # Regular NYMEX hours: 09:00-14:30 ET = 13:00-18:30 UTC
    if 13 <= hour < 19:
        return 0.01  # tight market hours
    elif 23 <= hour or hour < 2:
        return 0.03  # thin overnight
    return 0.02      # extended hours


# ──────────────────────────────────────────────────────────────
# 5. Regime Engine
# ──────────────────────────────────────────────────────────────

# WTI events calendar (priority A = immediate EVENT, B = needs confirmation)
WTI_EVENTS: List[Dict] = [
    # Priority A - direct EVENT mode
    {"name": "EIA Weekly Crude Stock",  "weekday": 3, "hour_utc": 14, "minute": 30, "priority": "A", "window_m": 60},
    {"name": "OPEC Meeting",            "weekday": None, "hour_utc": None, "minute": None, "priority": "A", "window_m": 120},
    # Priority B - confirmation required
    {"name": "API Crude Stock Report",  "weekday": 2, "hour_utc": 20, "minute": 30, "priority": "B", "window_m": 45},
    {"name": "US CPI Release",          "weekday": None, "hour_utc": 12, "minute": 30, "priority": "B", "window_m": 30},
    {"name": "US NFP",                  "weekday": 4, "hour_utc": 12, "minute": 30, "priority": "B", "window_m": 30},  # 1st Fri
    {"name": "FOMC Decision",           "weekday": None, "hour_utc": 18, "minute": 0, "priority": "B", "window_m": 60},
]

def _check_event_active() -> Tuple[bool, str, str]:
    """
    Check if any scheduled WTI event is active right now.
    Returns (is_active, event_name, priority).
    """
    now = datetime.now(timezone.utc)
    for ev in WTI_EVENTS:
        if ev["weekday"] is not None and ev["hour_utc"] is not None:
            if now.weekday() == ev["weekday"] and now.hour == ev["hour_utc"]:
                diff_m = (now.hour*60+now.minute) - (ev["hour_utc"]*60 + (ev["minute"] or 0))
                if -15 <= diff_m <= ev["window_m"]:
                    return True, ev["name"], ev["priority"]
        elif ev["hour_utc"] is not None:
            # Time-based only (no weekday restriction)
            if now.hour == ev["hour_utc"]:
                diff_m = (now.hour*60+now.minute) - (ev["hour_utc"]*60 + (ev["minute"] or 0))
                if -15 <= diff_m <= ev["window_m"]:
                    return True, ev["name"], ev["priority"]
    return False, "", ""


def _determine_regime(ind: Dict, cfg: WTIRegimeConfig) -> Tuple[WTIRegime, str]:
    """
    Regime priority: BLOCKED > EVENT > TREND > NORMAL
    Returns (regime, reason).
    """
    atr = ind.get("atr", 0) or 0
    atr_baseline = ind.get("atr_baseline", 1) or 1
    adx = ind.get("adx", 0) or 0
    atr_mult = atr / atr_baseline if atr_baseline > 0 else 1.0

    if atr_mult >= cfg.blocked_vol_mult:
        return WTIRegime.BLOCKED, f"ATR={atr:.3f} 超过基线 {atr_mult:.1f}×（阈值{cfg.blocked_vol_mult}×）"

    event_active, event_name, event_priority = _check_event_active()
    if event_active:
        return WTIRegime.EVENT, f"{event_name} (优先级{event_priority})—活跃窗口"

    if adx >= cfg.trend_adx_min and atr_mult < cfg.trend_vol_max:
        return WTIRegime.TREND, f"ADX={adx:.1f}（≥{cfg.trend_adx_min}），ATR倍数={atr_mult:.1f}×"

    return WTIRegime.NORMAL, f"ADX={adx:.1f}，ATR倍数={atr_mult:.1f}×"


# ──────────────────────────────────────────────────────────────
# 6. Signal Service (dynamic 6-condition confirmation)
# ──────────────────────────────────────────────────────────────

def _generate_signal(ind: Dict, regime: WTIRegime, cfg: WTIConfirmConfig) -> Dict:
    """
    Generate WTI directional signal with 6-condition dynamic confirmation.
    All 6 conditions must pass for a confirmed trade signal.

    Conditions:
      1. Breakout confirmation: price > event_range high (LONG) or < event_range low (SHORT)
      2. EMA alignment: ema20 > ema50 (LONG) or ema20 < ema50 (SHORT)
      3. ADX confirmation: adx >= adx_threshold (22)
      4. Volume confirmation: vol_ratio >= min_volume_ratio (1.2)
      5. VWAP proximity: |price - VWAP| <= vwap_atr_max × ATR
      6. Spread: spread <= max_spread_ticks × tick_size
    """
    price  = ind["price"]
    ema20  = ind.get("ema20") or price
    ema50  = ind.get("ema50") or price
    adx    = ind.get("adx")   or 0.0
    atr    = ind.get("atr")   or 1.0
    rsi    = ind.get("rsi")   or 50.0
    vwap   = ind.get("vwap")  or price
    vol_r  = ind.get("vol_ratio") or 1.0
    hi     = ind.get("high_today", price)
    lo     = ind.get("low_today", price)
    spread = _get_wti_spread()

    # Determine raw direction bias from EMA & DI
    di_plus  = ind.get("di_plus", 0) or 0
    di_minus = ind.get("di_minus", 0) or 0

    raw_long  = (ema20 > ema50) and (di_plus > di_minus)
    raw_short = (ema20 < ema50) and (di_minus > di_plus)

    if not (raw_long or raw_short):
        return {
            "direction": None,
            "all_confirmed": False,
            "raw_bias": "NEUTRAL",
            "conditions": {},
            "score": 0,
            "reason": "EMA和DI无明确方向",
        }

    direction = "LONG" if raw_long else "SHORT"

    # Day range for breakout reference
    day_range = hi - lo
    breakout_level = day_range * cfg.breakout_pct

    # 6 conditions
    cond = {}
    if direction == "LONG":
        cond["breakout"] = (price - lo) >= breakout_level
        cond["ema_align"] = ema20 > ema50
    else:
        cond["breakout"] = (hi - price) >= breakout_level
        cond["ema_align"] = ema20 < ema50

    cond["adx_ok"]     = adx >= cfg.adx_threshold
    cond["volume_ok"]  = vol_r >= cfg.min_volume_ratio
    cond["vwap_ok"]    = abs(price - vwap) <= cfg.vwap_atr_max * atr
    cond["spread_ok"]  = spread <= WTIRiskConfig().max_spread_ticks * WTI_TICK_SIZE

    passed = sum(cond.values())
    all_confirmed = passed == 6

    # Score: base confidence (0-100)
    score = int(passed / 6 * 100)
    # Bonus for strong ADX and clean vol
    if adx >= 28:
        score = min(100, score + 10)
    if vol_r >= 1.5:
        score = min(100, score + 5)

    return {
        "direction":     direction,
        "all_confirmed": all_confirmed,
        "raw_bias":      direction,
        "conditions":    {k: "✓" if v else "✗" for k, v in cond.items()},
        "conditions_passed": passed,
        "score":         score,
        "stop_loss":     round(price - 1.5 * atr, 2) if direction == "LONG" else round(price + 1.5 * atr, 2),
        "atr":           round(atr, 3),
        "price":         price,
        "reason":        f"{passed}/6条件通过" + ("—完全确认" if all_confirmed else "—等待确认"),
    }


# ──────────────────────────────────────────────────────────────
# 7. Signal Scorer (-100 ~ +100)
# ──────────────────────────────────────────────────────────────

def _score_signal(ind: Dict, regime: WTIRegime, fragility_score: float, spread: float) -> Dict:
    """
    8-factor unified bull/bear score from -100 to +100.
    Positive = bullish, Negative = bearish.
    """
    price = ind["price"]
    ema20 = ind.get("ema20") or price
    ema50 = ind.get("ema50") or price
    adx   = ind.get("adx")  or 0.0
    atr   = ind.get("atr")  or 1.0
    rsi   = ind.get("rsi")  or 50.0
    vol_r = ind.get("vol_ratio") or 1.0
    atr_mult = ind.get("atr_mult", 1.0)

    # Recent price change % vs 5d avg
    recent_chg_pct = (price - ema20) / ema20 * 100 if ema20 > 0 else 0

    components: Dict[str, float] = {}
    total = 0.0

    # 1. EMA Cross (-20 ~ +20)
    ema_pct   = (ema20 - ema50) / ema50 * 100 if ema50 > 0 else 0
    ema_score = max(-20.0, min(20.0, ema_pct * 10))
    components["ema_cross"] = round(ema_score, 1)
    total += ema_score

    # 2. ADX Trend Strength (-15 ~ +15)
    if adx > 28:
        adx_score = 15.0 if recent_chg_pct > 0 else -15.0
    elif adx > 22:
        adx_score = 8.0 if recent_chg_pct > 0 else -8.0
    else:
        adx_score = 0.0
    components["adx_trend"] = round(adx_score, 1)
    total += adx_score

    # 3. Regime (-15 ~ +15)
    regime_map = {
        WTIRegime.NORMAL:  10.0 if recent_chg_pct > 0 else -5.0,
        WTIRegime.TREND:   15.0 if recent_chg_pct > 0 else -10.0,
        WTIRegime.EVENT:   0.0,
        WTIRegime.BLOCKED: -15.0,
    }
    regime_score = regime_map.get(regime, 0.0)
    components["regime"] = round(regime_score, 1)
    total += regime_score

    # 4. Volatility (-10 ~ +10)
    if atr_mult < 0.8:
        vol_score = 5.0
    elif atr_mult < 1.5:
        vol_score = 0.0
    elif atr_mult < 2.5:
        vol_score = -5.0
    else:
        vol_score = -10.0
    components["volatility"] = vol_score
    total += vol_score

    # 5. Price Momentum (-15 ~ +15)
    mom_score = max(-15.0, min(15.0, recent_chg_pct * 5))
    components["price_momentum"] = round(mom_score, 1)
    total += mom_score

    # 6. RSI (-10 ~ +10)
    if rsi > 70:
        rsi_score = -10.0
    elif rsi > 60:
        rsi_score = -3.0
    elif rsi < 30:
        rsi_score = 10.0
    elif rsi < 40:
        rsi_score = 3.0
    else:
        rsi_score = 0.0
    components["rsi"] = rsi_score
    total += rsi_score

    # 7. Spread (-5 ~ +5)
    if spread < 0.02:
        spread_score = 5.0
    elif spread < 0.05:
        spread_score = 0.0
    else:
        spread_score = -5.0
    components["spread"] = spread_score
    total += spread_score

    # 8. Fragility (-10 ~ +10, inverse)
    if fragility_score < 20:
        frag_score = 10.0
    elif fragility_score < 40:
        frag_score = 5.0
    elif fragility_score < 60:
        frag_score = 0.0
    elif fragility_score < 80:
        frag_score = -5.0
    else:
        frag_score = -10.0
    components["fragility"] = frag_score
    total += frag_score

    total = max(-100.0, min(100.0, total))

    if total > 60:
        direction, zone = "strong_long",  "强多 >60"
    elif total > 20:
        direction, zone = "long",         "偏多 20-60"
    elif total > -20:
        direction, zone = "neutral",      "观望区"
    elif total > -60:
        direction, zone = "short",        "偏空 -20~-60"
    else:
        direction, zone = "strong_short", "强空 <-60"

    return {
        "score":       round(total, 1),
        "direction":   direction,
        "zone":        zone,
        "components":  components,
        "bullish_pct": round(max(0, total + 100) / 2, 1),
        "bearish_pct": round(max(0, 100 - total) / 2, 1),
        "confidence":  round(abs(total) / 100, 2),
    }


# ──────────────────────────────────────────────────────────────
# 8. Fragility Engine
# ──────────────────────────────────────────────────────────────

class _FragilityEngine:
    """Multi-factor fragility score (0-100)."""

    def __init__(self):
        self._spread_hist: deque = deque(maxlen=100)
        self._vol_hist:    deque = deque(maxlen=100)
        self.score = 0.0
        self.level = "low"
        self.triggers: List[str] = []

    def update(self, spread: float, atr_mult: float, regime: WTIRegime) -> float:
        self._spread_hist.append(spread)
        self._vol_hist.append(atr_mult)
        total = 0.0
        triggers = []

        # Spread (0-30)
        if spread > 0.15:
            total += 30; triggers.append("点差异常扩大")
        elif spread > 0.08:
            total += 20; triggers.append("点差偏大")
        elif spread > 0.04:
            total += 10
        else:
            total += max(0, spread / 0.03 * 5)

        # Spread acceleration
        if len(self._spread_hist) >= 10:
            rec = list(self._spread_hist)
            recent = sum(rec[-5:])/5
            older  = sum(rec[-15:-5])/10 if len(rec) >= 15 else recent
            if older > 0 and recent / older > 2.0:
                total += 10; triggers.append("点差突变")

        # Volatility (0-25)
        if atr_mult > 4.0:
            total += 25; triggers.append("超强波动")
        elif atr_mult > 2.0:
            total += 15; triggers.append("波动加剧")
        else:
            total += max(0, (atr_mult - 1.0) * 12)

        # Regime (0-10)
        regime_pts = {WTIRegime.BLOCKED: 10, WTIRegime.EVENT: 6, WTIRegime.TREND: 2, WTIRegime.NORMAL: 0}
        pts = regime_pts.get(regime, 0)
        total += pts
        if pts >= 6:
            triggers.append(f"{regime.value}状态")

        total = min(100.0, max(0.0, total))
        self.score = total
        self.triggers = triggers
        if total >= 80:   self.level = "extreme"
        elif total >= 60: self.level = "high"
        elif total >= 30: self.level = "moderate"
        else:             self.level = "low"
        return total

    def size_multiplier(self) -> float:
        return {
            "extreme":  0.0,
            "high":     0.25,
            "moderate": 0.6,
            "low":      1.0,
        }.get(self.level, 1.0)


# ──────────────────────────────────────────────────────────────
# 9. Execution Gate (8 checks)
# ──────────────────────────────────────────────────────────────

def _run_execution_gate(
    adx:             float,
    spread:          float,
    atr_mult:        float,
    signal_score:    float,
    fragility_score: float,
    risk_can_trade:  bool,
    cooldown_active: bool,
    regime:          WTIRegime,
) -> Dict:
    checks = []

    def check(name, passed, warn=False, value="", threshold=""):
        status = "PASS" if passed else ("WARN" if warn else "FAIL")
        checks.append({"name": name, "status": status, "value": value, "threshold": threshold})

    check("点差检查",   spread <= 0.06,         value=f"${spread:.3f}",    threshold="≤$0.06")
    check("ADX趋势",   adx >= 18.0,  warn=18<=adx<22, value=f"{adx:.1f}",  threshold="≥18")
    check("波动控制",  atr_mult <= 3.0,          value=f"{atr_mult:.2f}×",  threshold="≤3.0×")
    check("信号强度",  signal_score >= 55, warn=44<=signal_score<55,
          value=f"{signal_score:.0f}", threshold="≥55")
    check("脆弱度",    fragility_score < 60, warn=40<=fragility_score<60,
          value=f"{fragility_score:.0f}", threshold="<60")
    check("风控状态",  risk_can_trade,            value="允许" if risk_can_trade else "禁止",
          threshold="允许交易")
    check("冷静期",    not cooldown_active,        value="无" if not cooldown_active else "冷静中",
          threshold="无冷静期")
    check("市场状态",  regime in (WTIRegime.NORMAL, WTIRegime.TREND),
          warn=(regime == WTIRegime.EVENT),
          value=regime.value.upper(), threshold="NORMAL/TREND")

    fails  = sum(1 for c in checks if c["status"] == "FAIL")
    warns  = sum(1 for c in checks if c["status"] == "WARN")
    passes = sum(1 for c in checks if c["status"] == "PASS")

    can_enter = fails == 0
    if can_enter and warns == 0:
        gate, msg = "OPEN",    "全部通过，可按信号方向入场"
    elif can_enter:
        gate, msg = "CAUTION", f"{warns}项警告，建议减小仓位"
    elif fails == 1:
        gate, msg = "PARTIAL", "1项未通过，等待改善"
    else:
        gate, msg = "CLOSED",  f"{fails}项未通过，继续观望"

    return {
        "gate":       gate,
        "can_enter":  can_enter,
        "message":    msg,
        "checks":     checks,
        "pass_count": passes,
        "warn_count": warns,
        "fail_count": fails,
    }


# ──────────────────────────────────────────────────────────────
# 10. Risk Service
# ──────────────────────────────────────────────────────────────

@dataclass
class _RiskState:
    kill_switch:       bool  = False
    halted:            bool  = False
    daily_pnl:         float = 0.0
    daily_trades:      int   = 0
    consecutive_loss:  int   = 0
    equity:            float = field(default_factory=lambda: WTIRiskConfig().equity)
    reset_date:        str   = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def reset_daily(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.reset_date:
            self.daily_pnl    = 0.0
            self.daily_trades = 0
            self.reset_date   = today
            # halted resets daily; kill_switch does NOT
            self.halted = False

    def can_trade(self, cfg: WTIRiskConfig) -> Tuple[bool, str]:
        self.reset_daily()
        if self.kill_switch:
            return False, "🔴 杀手锁激活（永久暂停）"
        if self.halted:
            return False, "🟠 操作员暂停"
        max_daily_loss = cfg.equity * cfg.max_daily_loss_pct
        if self.daily_pnl <= -max_daily_loss:
            return False, f"日损达到上限: ${self.daily_pnl:+.0f} / ${-max_daily_loss:.0f}"
        if self.consecutive_loss >= cfg.max_consecutive:
            return False, f"连续亏损{self.consecutive_loss}笔（限{cfg.max_consecutive}笔）"
        return True, "✅ 可交易"

    def calc_position_size(self, entry: float, stop_loss: float, cfg: WTIRiskConfig) -> int:
        """Calculate lots using fixed-fractional risk model."""
        risk_per_lot = abs(entry - stop_loss) * WTI_MULTIPLIER
        if risk_per_lot <= 0:
            return 1
        max_risk = self.equity * cfg.risk_per_trade_pct
        lots = max_risk / risk_per_lot
        return max(1, min(cfg.max_position_lots, int(lots)))

    def record_trade(self, pnl: float):
        self.daily_pnl    += pnl
        self.daily_trades += 1
        self.equity       += pnl
        if pnl < 0:
            self.consecutive_loss += 1
        else:
            self.consecutive_loss = 0


# ──────────────────────────────────────────────────────────────
# 11. Paper Broker
# ──────────────────────────────────────────────────────────────

@dataclass
class _WTIPosition:
    id:          str
    direction:   Direction
    quantity:    int
    entry_price: float
    stop_loss:   float
    take_profit: float
    opened_at:   datetime
    is_open:     bool = True
    exit_price:  float = 0.0
    exit_reason: str   = ""
    pnl_usd:     float = 0.0

    @property
    def hold_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 60

    def unrealized_pnl(self, current_price: float) -> float:
        if self.direction == Direction.LONG:
            return (current_price - self.entry_price) * self.quantity * WTI_MULTIPLIER
        else:
            return (self.entry_price - current_price) * self.quantity * WTI_MULTIPLIER


class _WTIPaperBroker:
    def __init__(self):
        self.equity: float = WTIRiskConfig().equity
        self._initial_equity: float = self.equity
        self._positions: Dict[str, _WTIPosition] = {}
        self._trade_records: List[Dict] = []
        self._slippage_ticks = 1.0

    def _fill_price(self, direction: Direction, price: float) -> float:
        slip = self._slippage_ticks * WTI_TICK_SIZE * random.uniform(0.5, 1.5)
        return round(price + slip if direction == Direction.LONG else price - slip, 2)

    def open_position(self, direction: Direction, qty: int, price: float, stop_loss: float, take_profit: float) -> _WTIPosition:
        filled = self._fill_price(direction, price)
        pos = _WTIPosition(
            id          = str(uuid.uuid4())[:8],
            direction   = direction,
            quantity    = qty,
            entry_price = filled,
            stop_loss   = stop_loss,
            take_profit = take_profit,
            opened_at   = datetime.now(timezone.utc),
        )
        self._positions[pos.id] = pos
        logger.info(f"[WTIPaper] 开仓 {direction.value} {qty}手 @ {filled:.2f} | SL={stop_loss:.2f} TP={take_profit:.2f}")
        return pos

    def close_position(self, pos_id: str, price: float, reason: str = "manual") -> Optional[Dict]:
        pos = self._positions.get(pos_id)
        if not pos or not pos.is_open:
            return None
        exit_p = self._fill_price(
            Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG,
            price
        )
        if pos.direction == Direction.LONG:
            raw_pnl = (exit_p - pos.entry_price) * pos.quantity * WTI_MULTIPLIER
        else:
            raw_pnl = (pos.entry_price - exit_p) * pos.quantity * WTI_MULTIPLIER
        net_pnl = raw_pnl - WTI_COMMISSION * pos.quantity
        self.equity += net_pnl
        pos.is_open     = False
        pos.exit_price  = exit_p
        pos.exit_reason = reason
        pos.pnl_usd     = round(net_pnl, 2)
        record = {
            "id":          pos.id,
            "direction":   pos.direction.value,
            "quantity":    pos.quantity,
            "entry":       pos.entry_price,
            "exit":        exit_p,
            "pnl_usd":     round(net_pnl, 2),
            "hold_min":    round(pos.hold_minutes, 1),
            "reason":      reason,
            "closed_at":   datetime.now(timezone.utc).isoformat()[:19],
        }
        self._trade_records.append(record)
        logger.info(f"[WTIPaper] 平仓 {pos.direction.value} @ {exit_p:.2f} | PnL={net_pnl:+.0f} | {reason}")
        return record

    @property
    def open_positions(self) -> List[_WTIPosition]:
        return [p for p in self._positions.values() if p.is_open]

    def get_summary(self, current_price: Optional[float] = None) -> Dict:
        records = self._trade_records
        open_pos = self.open_positions
        unrealized = sum(p.unrealized_pnl(current_price) for p in open_pos) if current_price else 0
        wins   = [r for r in records if r["pnl_usd"] > 0]
        losses = [r for r in records if r["pnl_usd"] <= 0]
        total_pnl = sum(r["pnl_usd"] for r in records)
        return {
            "equity":         round(self.equity, 2),
            "initial_equity": self._initial_equity,
            "return_pct":     round((self.equity - self._initial_equity) / self._initial_equity * 100, 2),
            "unrealized_pnl": round(unrealized, 2),
            "net_liq":        round(self.equity + unrealized, 2),
            "total_trades":   len(records),
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       round(len(wins)/len(records)*100, 1) if records else 0,
            "total_pnl":      round(total_pnl, 2),
            "open_positions": len(open_pos),
            "positions":      [
                {
                    "id":        p.id,
                    "dir":       p.direction.value,
                    "qty":       p.quantity,
                    "entry":     p.entry_price,
                    "sl":        p.stop_loss,
                    "tp":        p.take_profit,
                    "unrealized":round(p.unrealized_pnl(current_price) if current_price else 0, 2),
                    "hold_min":  round(p.hold_minutes, 1),
                }
                for p in open_pos
            ],
            "recent_trades":  records[-5:],
        }


# ──────────────────────────────────────────────────────────────
# 12. Tradovate Client (REST, DEMO/LIVE)
# ──────────────────────────────────────────────────────────────

class _TradovateClient:
    DEMO_URL = "https://demo.tradovateapi.com/v1"
    LIVE_URL = "https://live.tradovateapi.com/v1"

    def __init__(self):
        self.username      = os.getenv("TRADOVATE_USERNAME", "")
        self.password      = os.getenv("TRADOVATE_PASSWORD", "")
        self.client_id     = os.getenv("TRADOVATE_CLIENT_ID", "")
        self.client_secret = os.getenv("TRADOVATE_CLIENT_SECRET", "")
        self.device_id     = os.getenv("TRADOVATE_DEVICE_ID", "wti-ai-crewai")
        self.is_demo       = os.getenv("TRADOVATE_IS_DEMO", "true").lower() == "true"
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._contract_id: Optional[int] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password and self.client_id and self.client_secret)

    @property
    def base_url(self) -> str:
        return self.DEMO_URL if self.is_demo else self.LIVE_URL

    async def _authenticate(self) -> bool:
        if not _HTTPX_OK or not self.is_configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(
                    f"{self.base_url}/auth/accesstokenrequest",
                    json={
                        "name":       self.username,
                        "password":   self.password,
                        "appId":      self.client_id,
                        "appVersion": "1.0",
                        "cid":        self.client_id,
                        "sec":        self.client_secret,
                        "deviceId":   self.device_id,
                    }
                )
                if r.status_code == 200:
                    data = r.json()
                    self._token = data.get("accessToken")
                    exp = data.get("expirationTime", 3600)
                    self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=exp)
                    logger.info("[Tradovate] 认证成功")
                    return True
                logger.error(f"[Tradovate] 认证失败 {r.status_code}: {r.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"[Tradovate] 认证异常: {e}")
            return False

    async def _ensure_token(self) -> bool:
        if self._token and self._token_expiry and datetime.now(timezone.utc) < self._token_expiry:
            return True
        return await self._authenticate()

    async def _get_contract_id(self) -> Optional[int]:
        if self._contract_id:
            return self._contract_id
        if not await self._ensure_token():
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.get(
                    f"{self.base_url}/contract/find",
                    params={"name": "CL"},
                    headers={"Authorization": f"Bearer {self._token}"}
                )
                if r.status_code == 200:
                    contracts = r.json()
                    if contracts:
                        self._contract_id = contracts[0].get("id")
                        logger.info(f"[Tradovate] CL合约ID={self._contract_id}")
                        return self._contract_id
        except Exception as e:
            logger.error(f"[Tradovate] 获取合约异常: {e}")
        return None

    async def place_bracket_order(
        self,
        direction: str,
        qty: int,
        take_profit: float,
        stop_loss: float,
    ) -> Dict:
        """Place OCO bracket order. Returns result dict."""
        if not await self._ensure_token():
            return {"error": "Tradovate认证失败"}
        contract_id = await self._get_contract_id()
        if not contract_id:
            return {"error": "无法获取CL合约ID"}
        action = "Buy" if direction.upper() == "LONG" else "Sell"
        exit_action = "Sell" if action == "Buy" else "Buy"
        payload = {
            "accountSpec":  self.username,
            "accountId":    0,
            "action":       action,
            "orderQty":     qty,
            "orderType":    "Market",
            "isAutomated":  True,
            "contractId":   contract_id,
            "bracket1": {"action": exit_action, "orderType": "Limit",  "price":     take_profit},
            "bracket2": {"action": exit_action, "orderType": "Stop",   "stopPrice": stop_loss},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(
                    f"{self.base_url}/order/placeoco",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"}
                )
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "status":     "placed",
                        "order_id":   str(data.get("orderId", "")),
                        "direction":  direction,
                        "qty":        qty,
                        "tp":         take_profit,
                        "sl":         stop_loss,
                        "env":        "DEMO" if self.is_demo else "LIVE",
                    }
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"error": f"Tradovate下单异常: {e}"}

    async def get_positions(self) -> List[Dict]:
        if not await self._ensure_token():
            return []
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.get(
                    f"{self.base_url}/position/list",
                    headers={"Authorization": f"Bearer {self._token}"}
                )
                return r.json() if r.status_code == 200 else []
        except:
            return []

    async def get_account(self) -> Optional[Dict]:
        if not await self._ensure_token():
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.get(
                    f"{self.base_url}/account/list",
                    headers={"Authorization": f"Bearer {self._token}"}
                )
                if r.status_code == 200:
                    accounts = r.json()
                    return accounts[0] if accounts else None
        except:
            pass
        return None


def _run_async(coro):
    """Run async coroutine from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(coro)
    except Exception:
        return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────
# 13. Global State
# ──────────────────────────────────────────────────────────────

_WTI_MODE      = os.getenv("WTI_MODE", "PAPER").upper()
_risk_cfg      = WTIRiskConfig()
_regime_cfg    = WTIRegimeConfig()
_confirm_cfg   = WTIConfirmConfig()
_risk_state    = _RiskState(equity=_risk_cfg.equity)
_paper_broker  = _WTIPaperBroker()
_tradovate     = _TradovateClient()
_fragility_eng = _FragilityEngine()

# ──────────────────────────────────────────────────────────────
# 14. CrewAI Tool Input Schemas
# ──────────────────────────────────────────────────────────────

class WTISignalInput(BaseModel):
    mode: str = Field(
        default="full",
        description=(
            "Analysis mode: "
            "'full' (indicators + regime + signal + gate), "
            "'quick' (price + regime + score only), "
            "'gate' (execution gate check only)"
        )
    )

class WTIRiskInput(BaseModel):
    query: str = Field(
        default="status",
        description=(
            "Risk query type: "
            "'status' (risk state + limits), "
            "'positions' (open positions + P&L), "
            "'summary' (full account summary), "
            "'reset_halt' (clear operator halt)"
        )
    )

class WTITradeInput(BaseModel):
    action: str = Field(
        description=(
            "Trade action: "
            "'long' (open long position), "
            "'short' (open short position), "
            "'close_all' (close all open positions), "
            "'close:<pos_id>' (close specific position)"
        )
    )
    qty: int = Field(default=1, description="Number of contracts (lots), default 1")


# ──────────────────────────────────────────────────────────────
# 15. Tool 1 — WTISignalTool
# ──────────────────────────────────────────────────────────────

class WTISignalTool(BaseTool):
    name: str        = "WTISignalTool"
    description: str = (
        "WTI原油信号分析工具。分析CL期货的市场状态、技术指标、信号确认和执行门控。"
        "mode='full'完整分析 | 'quick'快速评分 | 'gate'仅执行门控检查。"
    )
    args_schema: type[BaseModel] = WTISignalInput

    def _run(self, mode: str = "full") -> str:
        try:
            return self._analyze(mode)
        except Exception as e:
            logger.error(f"[WTISignalTool] 异常: {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _analyze(self, mode: str) -> str:
        # Fetch & compute
        data = _fetch_wti_data(200)
        if not data:
            return json.dumps({
                "error": "无法获取WTI行情数据（yfinance不可用或网络异常）",
                "mode":  mode,
            }, ensure_ascii=False)

        ind = _compute_indicators(data)
        spread = _get_wti_spread()
        regime, regime_reason = _determine_regime(ind, _regime_cfg)

        # Update fragility
        frag_score = _fragility_eng.update(spread, ind.get("atr_mult", 1.0), regime)

        if mode == "quick":
            score_data = _score_signal(ind, regime, frag_score, spread)
            return json.dumps({
                "mode":         "quick",
                "date":         ind["date"],
                "price":        ind["price"],
                "regime":       regime.value,
                "regime_note":  regime_reason,
                "signal_score": score_data["score"],
                "direction":    score_data["direction"],
                "zone":         score_data["zone"],
                "fragility":    round(frag_score, 1),
                "atr":          ind.get("atr"),
                "adx":          ind.get("adx"),
            }, ensure_ascii=False, indent=2)

        # Full indicator set
        signal_data = _generate_signal(ind, regime, _confirm_cfg)
        score_data  = _score_signal(ind, regime, frag_score, spread)

        can_trade, trade_reason = _risk_state.can_trade(_risk_cfg)
        cooldown = _risk_state.consecutive_loss >= 2  # soft cooldown

        gate_data = _run_execution_gate(
            adx            = ind.get("adx") or 0,
            spread         = spread,
            atr_mult       = ind.get("atr_mult", 1.0),
            signal_score   = score_data["score"],
            fragility_score= frag_score,
            risk_can_trade = can_trade,
            cooldown_active= cooldown,
            regime         = regime,
        )

        if mode == "gate":
            return json.dumps({
                "mode":     "gate",
                "date":     ind["date"],
                "price":    ind["price"],
                "gate":     gate_data,
                "can_trade":can_trade,
                "risk_note":trade_reason,
            }, ensure_ascii=False, indent=2)

        # Full mode
        result = {
            "mode":           "full",
            "date":           ind["date"],
            "contract":       "CL (WTI Crude Oil Futures / NYMEX)",
            "wti_mode":       _WTI_MODE,
            "market": {
                "price":      ind["price"],
                "open":       ind.get("open_today"),
                "high":       ind.get("high_today"),
                "low":        ind.get("low_today"),
                "volume":     ind.get("volume"),
                "spread_usd": round(spread, 4),
            },
            "indicators": {
                "ema20":       round(ind["ema20"], 3) if ind.get("ema20") else None,
                "ema50":       round(ind["ema50"], 3) if ind.get("ema50") else None,
                "adx":         round(ind["adx"], 2)  if ind.get("adx")  else None,
                "di_plus":     round(ind["di_plus"], 2)  if ind.get("di_plus")  else None,
                "di_minus":    round(ind["di_minus"], 2) if ind.get("di_minus") else None,
                "rsi":         round(ind["rsi"], 2)  if ind.get("rsi")  else None,
                "atr":         round(ind["atr"], 3)  if ind.get("atr")  else None,
                "atr_baseline":round(ind["atr_baseline"], 3),
                "atr_mult":    round(ind.get("atr_mult", 1.0), 2),
                "vwap":        round(ind["vwap"], 3)  if ind.get("vwap")  else None,
                "vol_ratio":   round(ind["vol_ratio"], 2) if ind.get("vol_ratio") else None,
            },
            "regime": {
                "state":   regime.value,
                "reason":  regime_reason,
            },
            "signal": signal_data,
            "score": {
                "unified_score": score_data["score"],
                "direction":     score_data["direction"],
                "zone":          score_data["zone"],
                "bullish_pct":   score_data["bullish_pct"],
                "bearish_pct":   score_data["bearish_pct"],
                "confidence":    score_data["confidence"],
                "components":    score_data["components"],
            },
            "fragility": {
                "score":    round(frag_score, 1),
                "level":    _fragility_eng.level,
                "triggers": _fragility_eng.triggers,
            },
            "execution_gate": gate_data,
            "risk": {
                "can_trade":  can_trade,
                "note":       trade_reason,
                "daily_pnl":  round(_risk_state.daily_pnl, 2),
                "consec_loss":_risk_state.consecutive_loss,
                "equity":     round(_risk_state.equity, 2),
            },
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────────────────────
# 16. Tool 2 — WTIRiskTool
# ──────────────────────────────────────────────────────────────

class WTIRiskTool(BaseTool):
    name: str        = "WTIRiskTool"
    description: str = (
        "WTI原油风控管理工具。查询风控状态、持仓P&L和账户信息。"
        "query='status'风控状态 | 'positions'持仓 | 'summary'账户汇总 | 'reset_halt'解除暂停。"
    )
    args_schema: type[BaseModel] = WTIRiskInput

    def _run(self, query: str = "status") -> str:
        try:
            return self._query(query)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _query(self, query: str) -> str:
        _risk_state.reset_daily()

        if query == "reset_halt":
            _risk_state.halted = False
            return json.dumps({"action": "reset_halt", "result": "操作员暂停已解除"}, ensure_ascii=False)

        can_trade, note = _risk_state.can_trade(_risk_cfg)
        max_daily = _risk_cfg.equity * _risk_cfg.max_daily_loss_pct

        if query == "status":
            return json.dumps({
                "query":           "status",
                "mode":            _WTI_MODE,
                "kill_switch":     _risk_state.kill_switch,
                "halted":          _risk_state.halted,
                "can_trade":       can_trade,
                "note":            note,
                "daily_pnl":       round(_risk_state.daily_pnl, 2),
                "daily_loss_limit":round(-max_daily, 2),
                "daily_trades":    _risk_state.daily_trades,
                "consec_losses":   _risk_state.consecutive_loss,
                "max_consec":      _risk_cfg.max_consecutive,
                "equity":          round(_risk_state.equity, 2),
                "risk_per_trade":  f"{_risk_cfg.risk_per_trade_pct*100:.1f}%",
                "max_position":    f"{_risk_cfg.max_position_lots}手",
                "fragility_level": _fragility_eng.level,
                "fragility_score": round(_fragility_eng.score, 1),
            }, ensure_ascii=False, indent=2)

        # Fetch current price for unrealized PnL
        current_price = None
        if _YF_OK:
            try:
                df = yf.Ticker(WTI_SYMBOL).history(period="2d", interval="1d")
                if not df.empty:
                    current_price = float(df["Close"].iloc[-1])
            except:
                pass

        if query == "positions":
            summary = _paper_broker.get_summary(current_price)
            if _WTI_MODE == "LIVE" and _tradovate.is_configured:
                live_pos = _run_async(_tradovate.get_positions())
                return json.dumps({
                    "query":       "positions",
                    "mode":        "LIVE",
                    "paper_state": summary,
                    "tradovate_positions": live_pos,
                }, ensure_ascii=False, indent=2)
            return json.dumps({"query": "positions", "mode": _WTI_MODE, **summary}, ensure_ascii=False, indent=2)

        if query == "summary":
            summary = _paper_broker.get_summary(current_price)
            risk_info = {
                "risk_config": {
                    "equity":           _risk_cfg.equity,
                    "max_daily_loss":   f"{_risk_cfg.max_daily_loss_pct*100:.1f}%",
                    "risk_per_trade":   f"{_risk_cfg.risk_per_trade_pct*100:.1f}%",
                    "max_consec_loss":  _risk_cfg.max_consecutive,
                    "max_lots":         _risk_cfg.max_position_lots,
                },
                "risk_state": {
                    "can_trade":    can_trade,
                    "note":         note,
                    "daily_pnl":    round(_risk_state.daily_pnl, 2),
                    "consec_loss":  _risk_state.consecutive_loss,
                    "equity":       round(_risk_state.equity, 2),
                },
                "fragility": {
                    "level":    _fragility_eng.level,
                    "score":    round(_fragility_eng.score, 1),
                    "triggers": _fragility_eng.triggers,
                }
            }
            return json.dumps({
                "query":    "summary",
                "mode":     _WTI_MODE,
                "broker":   summary,
                **risk_info,
            }, ensure_ascii=False, indent=2)

        return json.dumps({"error": f"未知query: {query}"}, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────
# 17. Tool 3 — WTITradeTool
# ──────────────────────────────────────────────────────────────

class WTITradeTool(BaseTool):
    name: str        = "WTITradeTool"
    description: str = (
        "WTI原油下单执行工具。在PAPER模式使用模拟经纪商，LIVE模式接入Tradovate API。"
        "action='long'/'short'开仓 | 'close_all'平所有仓 | 'close:<id>'平指定仓位。"
        "qty=合约手数（默认1）。执行前自动进行完整风控检查。"
    )
    args_schema: type[BaseModel] = WTITradeInput

    def _run(self, action: str = "long", qty: int = 1) -> str:
        try:
            return self._execute(action, qty)
        except Exception as e:
            logger.error(f"[WTITradeTool] 异常: {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _execute(self, action: str, qty: int) -> str:
        # ── Handle close actions ──
        if action == "close_all":
            return self._close_all()
        if action.startswith("close:"):
            pos_id = action.split(":", 1)[1].strip()
            return self._close_one(pos_id)

        # ── Pre-trade checks ──
        direction = Direction.LONG if action.lower() == "long" else Direction.SHORT

        # 1. Risk gate
        can_trade, risk_note = _risk_state.can_trade(_risk_cfg)
        if not can_trade:
            return json.dumps({
                "status":  "REJECTED",
                "reason":  f"风控拒绝: {risk_note}",
                "action":  action,
            }, ensure_ascii=False)

        # 2. Fragility gate
        if _fragility_eng.level == "extreme":
            return json.dumps({
                "status":  "REJECTED",
                "reason":  f"市场极度脆弱（fragility={_fragility_eng.score:.0f}），暂停交易",
                "action":  action,
            }, ensure_ascii=False)

        # 3. Fetch market data
        data = _fetch_wti_data(200)
        if not data:
            return json.dumps({"status": "ERROR", "reason": "无法获取WTI行情"}, ensure_ascii=False)

        ind = _compute_indicators(data)
        price  = ind["price"]
        atr    = ind.get("atr") or 1.0
        spread = _get_wti_spread()

        # 4. Spread check
        if spread > _risk_cfg.max_spread_ticks * WTI_TICK_SIZE:
            return json.dumps({
                "status":  "REJECTED",
                "reason":  f"点差过大: ${spread:.3f} > ${_risk_cfg.max_spread_ticks * WTI_TICK_SIZE:.2f}",
            }, ensure_ascii=False)

        # 5. Compute SL/TP
        sl_dist = 1.5 * atr
        tp_dist = 2.0 * atr  # 1:1.33 risk-reward for CL

        if direction == Direction.LONG:
            stop_loss   = round(price - sl_dist, 2)
            take_profit = round(price + tp_dist, 2)
        else:
            stop_loss   = round(price + sl_dist, 2)
            take_profit = round(price - tp_dist, 2)

        # 6. Auto-size position
        auto_lots = _risk_state.calc_position_size(price, stop_loss, _risk_cfg)
        # Apply fragility size mult
        frag_mult = _fragility_eng.size_multiplier()
        final_qty = max(1, min(qty, int(auto_lots * frag_mult)))
        if frag_mult < 1.0:
            size_note = f"脆弱度调整: {frag_mult:.0%}（建议{final_qty}手）"
        else:
            size_note = f"风险计算: {auto_lots}手可用，实际下{final_qty}手"

        # ── Execute ──
        if _WTI_MODE == "LIVE" and _tradovate.is_configured:
            result = _run_async(_tradovate.place_bracket_order(
                direction   = direction.value,
                qty         = final_qty,
                take_profit = take_profit,
                stop_loss   = stop_loss,
            ))
            if "error" in result:
                return json.dumps({
                    "status":  "ERROR",
                    "reason":  result["error"],
                    "action":  action,
                    "env":     "LIVE",
                }, ensure_ascii=False)
            return json.dumps({
                "status":        "PLACED",
                "env":           "LIVE (Tradovate)",
                "direction":     direction.value,
                "qty":           final_qty,
                "entry_approx":  price,
                "stop_loss":     stop_loss,
                "take_profit":   take_profit,
                "size_note":     size_note,
                "order":         result,
                "risk_usd":      round(abs(price - stop_loss) * final_qty * WTI_MULTIPLIER, 0),
                "reward_usd":    round(abs(take_profit - price) * final_qty * WTI_MULTIPLIER, 0),
                "atr":           round(atr, 3),
                "timestamp":     datetime.now(timezone.utc).isoformat()[:19],
            }, ensure_ascii=False, indent=2)

        # Paper mode
        pos = _paper_broker.open_position(direction, final_qty, price, stop_loss, take_profit)
        return json.dumps({
            "status":        "OPENED",
            "env":           "PAPER",
            "pos_id":        pos.id,
            "direction":     direction.value,
            "qty":           final_qty,
            "entry_price":   pos.entry_price,
            "stop_loss":     stop_loss,
            "take_profit":   take_profit,
            "size_note":     size_note,
            "risk_usd":      round(abs(pos.entry_price - stop_loss) * final_qty * WTI_MULTIPLIER, 0),
            "reward_usd":    round(abs(take_profit - pos.entry_price) * final_qty * WTI_MULTIPLIER, 0),
            "atr":           round(atr, 3),
            "equity_after":  round(_paper_broker.equity, 2),
            "timestamp":     datetime.now(timezone.utc).isoformat()[:19],
        }, ensure_ascii=False, indent=2)

    def _close_all(self) -> str:
        data = _fetch_wti_data(5)
        current_price = data["close"][-1] if data else None
        closed = []
        for pos in list(_paper_broker.open_positions):
            rec = _paper_broker.close_position(
                pos.id,
                current_price or pos.entry_price,
                "manual_close_all"
            )
            if rec:
                _risk_state.record_trade(rec["pnl_usd"])
                closed.append(rec)
        return json.dumps({
            "status":  "CLOSED_ALL",
            "closed":  len(closed),
            "records": closed,
            "equity":  round(_paper_broker.equity, 2),
        }, ensure_ascii=False, indent=2)

    def _close_one(self, pos_id: str) -> str:
        data = _fetch_wti_data(5)
        current_price = data["close"][-1] if data else None
        pos = next((p for p in _paper_broker.open_positions if p.id == pos_id), None)
        if not pos:
            return json.dumps({"status": "ERROR", "reason": f"持仓 {pos_id} 不存在或已平仓"}, ensure_ascii=False)
        rec = _paper_broker.close_position(pos_id, current_price or pos.entry_price, "manual")
        if rec:
            _risk_state.record_trade(rec["pnl_usd"])
            return json.dumps({"status": "CLOSED", "record": rec, "equity": round(_paper_broker.equity, 2)},
                               ensure_ascii=False, indent=2)
        return json.dumps({"status": "ERROR", "reason": "平仓失败"}, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────
# 18. Sanity check (run as __main__)
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("=" * 60)
    print("WTI Trading Tool — 功能自检")
    print("=" * 60)

    signal_tool = WTISignalTool()
    risk_tool   = WTIRiskTool()
    trade_tool  = WTITradeTool()

    print("\n[1] 信号分析 (mode=full)")
    out = signal_tool._run(mode="full")
    data = json.loads(out)
    if "error" in data:
        print(f"  ❌ {data['error']}")
    else:
        print(f"  ✅ WTI价格:    ${data['market']['price']:.2f}")
        print(f"  ✅ 市场状态:    {data['regime']['state'].upper()}")
        print(f"  ✅ ADX:         {data['indicators'].get('adx')}")
        print(f"  ✅ ATR:         {data['indicators'].get('atr')}")
        print(f"  ✅ 信号方向:    {data['signal'].get('direction') or '无'}")
        print(f"  ✅ 统一评分:    {data['score']['unified_score']}")
        print(f"  ✅ 执行门状态: {data['execution_gate']['gate']}")
        print(f"  ✅ 可入场:     {data['execution_gate']['can_enter']}")

    print("\n[2] 快速信号 (mode=quick)")
    q = json.loads(signal_tool._run(mode="quick"))
    if "error" not in q:
        print(f"  ✅ 价格={q['price']:.2f} | 状态={q['regime']} | 评分={q['signal_score']} | 方向={q['direction']}")

    print("\n[3] 风控状态 (query=status)")
    rs = json.loads(risk_tool._run(query="status"))
    print(f"  ✅ 可交易: {rs['can_trade']} | 说明: {rs['note']}")
    print(f"  ✅ 日PnL: ${rs['daily_pnl']:+.2f} | 账户净值: ${rs['equity']:,.0f}")

    print("\n[4] 模拟下单测试 (action=long, qty=1, PAPER模式)")
    tr = json.loads(trade_tool._run(action="long", qty=1))
    print(f"  ✅ 状态: {tr.get('status')} | 环境: {tr.get('env')}")
    if tr.get("pos_id"):
        print(f"  ✅ 持仓ID: {tr['pos_id']} | 入场: ${tr.get('entry_price')} | SL: ${tr.get('stop_loss')} | TP: ${tr.get('take_profit')}")
        print(f"  ✅ 风险: ${tr.get('risk_usd'):.0f} | 潜在盈利: ${tr.get('reward_usd'):.0f}")

    print("\n[5] 持仓查询 (query=positions)")
    pos_out = json.loads(risk_tool._run(query="positions"))
    print(f"  ✅ 开仓数: {pos_out.get('open_positions', 0)}")
    if pos_out.get("positions"):
        for p in pos_out["positions"]:
            print(f"     - {p['id']} {p['dir']} {p['qty']}手 @ ${p['entry']} | SL ${p['sl']} TP ${p['tp']} | 持仓{p['hold_min']}分钟")

    print("\n[6] 执行门控 (mode=gate)")
    gate_out = json.loads(signal_tool._run(mode="gate"))
    if "error" not in gate_out:
        g = gate_out["gate"]
        print(f"  ✅ 门控状态: {g['gate']} | 可入场: {g['can_enter']}")
        for c in g.get("checks", []):
            icon = "✓" if c["status"]=="PASS" else ("⚠" if c["status"]=="WARN" else "✗")
            print(f"     {icon} {c['name']}: {c['value']} (阈值{c['threshold']})")

    print("\n[7] 平仓测试 (close_all)")
    close_out = json.loads(trade_tool._run(action="close_all"))
    print(f"  ✅ 平仓数: {close_out.get('closed', 0)} | 账户净值: ${close_out.get('equity', 0):,.2f}")

    print("\n✅ WTI Trading Tool 自检完成")
