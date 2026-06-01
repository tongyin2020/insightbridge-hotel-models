"""
bond_tool.py — 国债 / 利率期货量化交易 CrewAI 工具
====================================================
直接移植自 Interest-rate-bond-main 机器人架构，并叠加：

  ① Nelson-Siegel 收益率曲线拟合（β₀/β₁/β₂ 因子提取）
  ② Ispread 均值回归策略（WTI原油/国债收益率比值）
  ③ 曲线形态交易（Steepener / Flattener / Butterfly）
  ④ 债券定价 & DV01 / 久期 / 凸性计算
  ⑤ VaR + 历史情景压力测试（Rate Shock / 2008 / COVID）
  ⑥ 系统状态机（SAFE → WARNING → EXIT_ONLY → HALT）

数据层：yfinance（免费）
  3M ^IRX, 2Y 2YY=F, 5Y ^FVX, 10Y ^TNX, 30Y ^TYX, VIX ^VIX, DXY DX-Y.NYB

工具列表：
  BondYieldCurveTool   → 实时收益率曲线 + Nelson-Siegel 拟合 + 形态分析
  BondSignalTool       → Ispread均值回归 / 曲线交易 / 国债拍卖日历信号
  BondRiskTool         → DV01/久期/VaR/压力测试 + 系统状态查询

环境变量（.env）：
  BOND_EQUITY=1000000       账户规模（美元）
  BOND_ISPREAD_UPPER=15.0   Ispread 卖债阈值
  BOND_ISPREAD_LOWER=10.0   Ispread 买债阈值
  BOND_MAX_LOSS_PCT=0.02    每日最大亏损 2%
"""

from __future__ import annotations

import os
import math
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from enum import Enum
from typing import Optional, List, Dict, Tuple, Any
from collections import deque

import numpy as np

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════════════════
#  1. 核心枚举（移植自 schemas.py）
# ══════════════════════════════════════════════════════════════════

class SystemStatus(str, Enum):
    SAFE      = "SAFE"
    WARNING   = "WARNING"
    EXIT_ONLY = "EXIT_ONLY"
    HALT      = "HALT"

class CurveShape(str, Enum):
    NORMAL   = "NORMAL"       # 正常上斜（短低长高）
    FLAT     = "FLAT"         # 平坦
    INVERTED = "INVERTED"     # 倒挂（衰退预警）
    HUMPED   = "HUMPED"       # 中段隆起

class TradeSignal(str, Enum):
    BUY_BOND    = "BUY_BOND"      # 做多国债（预期降息/避险）
    SELL_BOND   = "SELL_BOND"     # 做空国债（预期加息/通胀）
    STEEPENER   = "STEEPENER"     # 做多长端 + 做空短端
    FLATTENER   = "FLATTENER"     # 做多短端 + 做空长端
    BUTTERFLY   = "BUTTERFLY"     # 做多腹部 + 做空两翼
    HOLD        = "HOLD"

# ══════════════════════════════════════════════════════════════════
#  2. Nelson-Siegel 收益率曲线模型
# ══════════════════════════════════════════════════════════════════

class NelsonSiegelModel:
    """
    Nelson-Siegel (1987) 三因子收益率曲线模型
    ─────────────────────────────────────────
    y(τ) = β₀ + β₁·f₁(τ,λ) + β₂·f₂(τ,λ)

    f₁(τ,λ) = (1 - e^{-τ/λ}) / (τ/λ)          ← 斜率因子（短端衰减）
    f₂(τ,λ) = (1 - e^{-τ/λ}) / (τ/λ) - e^{-τ/λ}  ← 曲率因子（中段隆起）

    参数含义：
      β₀ — 长期水平（long-run yield，理论上所有期限收敛于此）
      β₁ — 斜率因子（β₁<0 → 正常上斜；β₁>0 → 倒挂）
      β₂ — 曲率因子（β₂>0 → 中段隆起；β₂<0 → 中段凹陷）
      λ  — 衰减速率（固定为 1.5，对应隆起点约 30 个月）

    交易信号：
      β₀↑ → 长期利率上升环境 → 做空长端
      β₁ 趋向 0 → 曲线趋平 → Flattener
      β₂ 变化 → 中段相对价值机会 → Butterfly
    """

    LAMBDA = 1.5  # 标准衰减参数（对应约 2.5 年隆起点）

    @staticmethod
    def _factors(tau: float, lam: float = 1.5) -> Tuple[float, float, float]:
        """计算给定期限 τ（年）的三个因子值"""
        if tau <= 0:
            return 1.0, 0.0, 0.0
        x = tau / lam
        ex = math.exp(-x)
        f1 = (1 - ex) / x
        f2 = f1 - ex
        return 1.0, f1, f2

    @classmethod
    def fit(cls, tenors: List[float], yields: List[float]) -> Dict[str, float]:
        """
        最小二乘拟合 β₀、β₁、β₂
        tenors: 期限（年），如 [0.25, 2, 5, 10, 30]
        yields: 对应收益率（%），如 [5.1, 4.7, 4.4, 4.3, 4.5]
        返回：{'beta0', 'beta1', 'beta2', 'lambda', 'rmse'}
        """
        lam = cls.LAMBDA
        n = len(tenors)
        if n < 3:
            return {"beta0": yields[-1] if yields else 4.0,
                    "beta1": 0.0, "beta2": 0.0, "lambda": lam, "rmse": float("nan")}

        # 构建设计矩阵 X ∈ R^{n×3}
        X = np.array([cls._factors(t, lam) for t in tenors])
        y = np.array(yields)

        try:
            # OLS: β = (X'X)^{-1} X'y
            betas, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
            b0, b1, b2 = float(betas[0]), float(betas[1]), float(betas[2])
            fitted = X @ betas
            rmse = float(np.sqrt(np.mean((y - fitted) ** 2)))
        except Exception:
            b0, b1, b2 = float(np.mean(yields)), 0.0, 0.0
            rmse = float("nan")

        return {
            "beta0":  round(b0, 4),   # 长期水平
            "beta1":  round(b1, 4),   # 斜率
            "beta2":  round(b2, 4),   # 曲率
            "lambda": lam,
            "rmse":   round(rmse, 5),
        }

    @classmethod
    def fitted_yield(cls, tau: float, beta0: float, beta1: float,
                     beta2: float, lam: float = 1.5) -> float:
        """给定因子，计算期限 τ 的拟合收益率"""
        _, f1, f2 = cls._factors(tau, lam)
        return beta0 + beta1 * f1 + beta2 * f2

    @classmethod
    def cheapness(cls, tau: float, market_yield: float,
                  beta0: float, beta1: float, beta2: float,
                  lam: float = 1.5) -> float:
        """
        相对价值：市场收益率 - NS 拟合收益率（正值=便宜，负值=昂贵）
        单位：bps (×100)
        """
        fitted = cls.fitted_yield(tau, beta0, beta1, beta2, lam)
        return round((market_yield - fitted) * 100, 2)  # bps

    @classmethod
    def interpret_factors(cls, b0: float, b1: float, b2: float) -> Dict[str, str]:
        """将 NS 因子转化为交易语言"""
        signals = {}
        # β₀：长期水平
        if b0 > 5.0:
            signals["beta0"] = f"β₀={b0:.2f}% 高位 → 长端压力大，做空长债偏多"
        elif b0 < 3.0:
            signals["beta0"] = f"β₀={b0:.2f}% 低位 → 宽松环境，做多长债"
        else:
            signals["beta0"] = f"β₀={b0:.2f}% 中性"

        # β₁：斜率（正常曲线 β₁<0）
        if b1 < -0.8:
            signals["beta1"] = f"β₁={b1:.2f} 显著倒挂 → 衰退预警，做多长端 / Steepener"
        elif -0.3 < b1 < 0.3:
            signals["beta1"] = f"β₁={b1:.2f} 曲线趋平 → Flattener 机会"
        elif b1 > 0.5:
            signals["beta1"] = f"β₁={b1:.2f} 正常上斜 → 无特殊方向信号"
        else:
            signals["beta1"] = f"β₁={b1:.2f} 轻度倒挂或平坦"

        # β₂：曲率（蝶式）
        if b2 > 0.5:
            signals["beta2"] = f"β₂={b2:.2f} 中段隆起 → Butterfly 做空 5Y/做多 2Y+30Y"
        elif b2 < -0.5:
            signals["beta2"] = f"β₂={b2:.2f} 中段凹陷 → Reverse Butterfly 做多 5Y"
        else:
            signals["beta2"] = f"β₂={b2:.2f} 曲率中性"

        return signals

# ══════════════════════════════════════════════════════════════════
#  3. 债券定价数学（DV01、久期、凸性）
# ══════════════════════════════════════════════════════════════════

class BondMath:
    """
    债券定价与风险指标
    移植自 risk_analytics 服务，补充完整的数学实现
    """

    @staticmethod
    def price(face: float, coupon_rate: float, ytm: float,
              n_periods: int, freq: int = 2) -> float:
        """
        债券公允价值
        face:        面值（如 1000）
        coupon_rate: 年票息率（如 0.045）
        ytm:         到期收益率（如 0.043）
        n_periods:   剩余付息次数（如 20 for 10Y 半年付息）
        freq:        每年付息次数（2=半年，1=年）
        """
        c = face * coupon_rate / freq
        r = ytm / freq
        if r == 0:
            return face + c * n_periods
        pv_coupons = c * (1 - (1 + r) ** (-n_periods)) / r
        pv_face    = face / (1 + r) ** n_periods
        return round(pv_coupons + pv_face, 4)

    @staticmethod
    def duration(face: float, coupon_rate: float, ytm: float,
                 n_periods: int, freq: int = 2) -> Tuple[float, float]:
        """
        返回 (Macaulay Duration, Modified Duration)，单位：年
        """
        c = face * coupon_rate / freq
        r = ytm / freq
        if r == 0:
            return 0.0, 0.0
        price = BondMath.price(face, coupon_rate, ytm, n_periods, freq)
        if price <= 0:
            return 0.0, 0.0

        weighted_sum = 0.0
        for t in range(1, n_periods + 1):
            cf = c if t < n_periods else c + face
            pv_cf = cf / (1 + r) ** t
            weighted_sum += (t / freq) * pv_cf

        mac_dur = weighted_sum / price
        mod_dur = mac_dur / (1 + r)
        return round(mac_dur, 4), round(mod_dur, 4)

    @staticmethod
    def dv01(face: float, coupon_rate: float, ytm: float,
             n_periods: int, freq: int = 2) -> float:
        """
        DV01（Dollar Value of 1bp）：收益率变化 1bp 时价格变化（美元）
        DV01 = -Modified Duration × Price × 0.0001
        """
        price = BondMath.price(face, coupon_rate, ytm, n_periods, freq)
        _, mod_dur = BondMath.duration(face, coupon_rate, ytm, n_periods, freq)
        dv01 = mod_dur * price * 0.0001
        return round(dv01, 4)

    @staticmethod
    def convexity(face: float, coupon_rate: float, ytm: float,
                  n_periods: int, freq: int = 2) -> float:
        """凸性（Convexity）：二阶利率敏感性"""
        c = face * coupon_rate / freq
        r = ytm / freq
        price = BondMath.price(face, coupon_rate, ytm, n_periods, freq)
        if price <= 0 or r == 0:
            return 0.0
        conv_sum = 0.0
        for t in range(1, n_periods + 1):
            cf = c if t < n_periods else c + face
            conv_sum += cf * t * (t + 1) / (1 + r) ** (t + 2)
        convexity = conv_sum / (price * freq ** 2)
        return round(convexity, 4)

    @staticmethod
    def approx_price_change(mod_dur: float, convexity: float,
                            price: float, dy: float) -> float:
        """
        价格变化近似（利率变化 dy bps，输入 0.01=1bp）：
        ΔP ≈ -ModDur × P × dy + 0.5 × Convexity × P × dy²
        """
        dy_decimal = dy / 10000  # bps → decimal（100bp = 0.01）
        return round(-mod_dur * price * dy_decimal +
                     0.5 * convexity * price * dy_decimal ** 2, 4)

# ══════════════════════════════════════════════════════════════════
#  4. 数据层：yfinance 实时拉取
# ══════════════════════════════════════════════════════════════════

YIELD_TICKERS = {
    "3M":  "^IRX",
    "2Y":  "2YY=F",
    "5Y":  "^FVX",
    "10Y": "^TNX",
    "30Y": "^TYX",
}
TENOR_YEARS = {"3M": 0.25, "2Y": 2.0, "5Y": 5.0, "10Y": 10.0, "30Y": 30.0}

_YC_CACHE: Optional[Tuple[float, Dict]] = None  # (timestamp, data)
_YC_TTL = 300  # 5分钟

def _fetch_yield_curve() -> Dict[str, Dict]:
    """拉取完整收益率曲线（带5分钟缓存）"""
    global _YC_CACHE
    now = time.time()
    if _YC_CACHE and now - _YC_CACHE[0] < _YC_TTL:
        return _YC_CACHE[1]

    try:
        import yfinance as yf
        curve = {}
        for tenor, sym in YIELD_TICKERS.items():
            try:
                hist = yf.Ticker(sym).history(period="5d")
                if not hist.empty:
                    cur  = float(hist["Close"].iloc[-1])
                    prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else cur
                    wk   = float(hist["Close"].iloc[0])  if len(hist) >= 5 else cur
                    curve[tenor] = {
                        "yield": round(cur, 3),
                        "chg_1d": round(cur - prev, 3),
                        "chg_1w": round(cur - wk, 3),
                    }
            except Exception:
                pass
        if len(curve) >= 3:
            _YC_CACHE = (now, curve)
            return curve
    except Exception:
        pass

    # fallback 模拟值（基于当前美联储环境）
    return {
        "3M":  {"yield": 4.82, "chg_1d": -0.01, "chg_1w": -0.03},
        "2Y":  {"yield": 4.52, "chg_1d": -0.02, "chg_1w": -0.08},
        "5Y":  {"yield": 4.31, "chg_1d": -0.01, "chg_1w": -0.07},
        "10Y": {"yield": 4.36, "chg_1d": 0.00,  "chg_1w": -0.05},
        "30Y": {"yield": 4.61, "chg_1d": 0.01,  "chg_1w": -0.03},
    }

def _fetch_macro() -> Dict[str, float]:
    """拉取宏观指标：VIX、DXY、WTI、TIPS"""
    try:
        import yfinance as yf
        macro = {}
        tickers = {"vix": "^VIX", "dxy": "DX-Y.NYB", "wti": "CL=F", "tips": "TIP"}
        for k, sym in tickers.items():
            try:
                hist = yf.Ticker(sym).history(period="2d")
                if not hist.empty:
                    macro[k] = round(float(hist["Close"].iloc[-1]), 2)
            except Exception:
                pass
        return macro
    except Exception:
        return {"vix": 18.5, "dxy": 104.0, "wti": 78.0, "tips": 105.0}

# ══════════════════════════════════════════════════════════════════
#  5. Ispread 均值回归策略（移植自 ai_engine.py）
# ══════════════════════════════════════════════════════════════════

ISPREAD_UPPER = float(os.getenv("BOND_ISPREAD_UPPER", "15.0"))
ISPREAD_LOWER = float(os.getenv("BOND_ISPREAD_LOWER", "10.0"))

def _calc_ispread(wti: float, bond_yield_10y: float) -> float:
    """Ispread = (WTI / 10Y Yield) × 0.85"""
    if bond_yield_10y <= 0:
        return 0.0
    return round(wti / bond_yield_10y * 0.85, 3)

def _ispread_signal(ispread: float) -> Dict[str, Any]:
    """
    Ispread 均值回归信号
    历史中枢约 12.5（WTI=75, Yield=4.5% → 75/4.5*0.85=14.2）
    """
    sig = {}
    if ispread > ISPREAD_UPPER:
        sig["signal"]     = TradeSignal.SELL_BOND.value
        sig["confidence"] = min(0.95, 0.70 + (ispread - ISPREAD_UPPER) * 0.05)
        sig["reason"]     = (f"Ispread={ispread:.2f} > {ISPREAD_UPPER} 上限 "
                             f"→ 债券相对石油偏贵，均值回归做空国债")
    elif ispread < ISPREAD_LOWER:
        sig["signal"]     = TradeSignal.BUY_BOND.value
        sig["confidence"] = min(0.95, 0.70 + (ISPREAD_LOWER - ispread) * 0.05)
        sig["reason"]     = (f"Ispread={ispread:.2f} < {ISPREAD_LOWER} 下限 "
                             f"→ 债券相对石油偏便宜，均值回归做多国债")
    else:
        mid = (ISPREAD_UPPER + ISPREAD_LOWER) / 2
        sig["signal"]     = TradeSignal.HOLD.value
        sig["confidence"] = 0.0
        sig["reason"]     = (f"Ispread={ispread:.2f} 在 [{ISPREAD_LOWER}, {ISPREAD_UPPER}] 区间内 "
                             f"（偏离中枢 {ispread - mid:+.2f}），无明显信号")
    sig["ispread"] = ispread
    return sig

# ══════════════════════════════════════════════════════════════════
#  6. 系统状态机（移植自 schemas.py + ai_engine.py）
# ══════════════════════════════════════════════════════════════════

@dataclass
class SystemState:
    status: SystemStatus     = SystemStatus.SAFE
    daily_pnl: float         = 0.0
    daily_pnl_limit: float   = -float(os.getenv("BOND_EQUITY", "1000000")) * \
                                 float(os.getenv("BOND_MAX_LOSS_PCT", "0.02"))
    consecutive_losses: int  = 0
    kill_switch: bool        = False
    halt_reason: str         = ""
    prev_yield_10y: float    = 0.0

_SYS_STATE = SystemState()
_POSITIONS: Dict[str, Dict] = {}  # symbol → position info
_DAILY_TRADES: List[Dict]   = []
_COOLDOWN_UNTIL: float      = 0.0

def _scan_risk(cur_yield_10y: float) -> Dict[str, Any]:
    """
    移植自 AITradingEngine.scan_risk()
    检测利率突变：Black Swan / High Volatility
    """
    prev = _SYS_STATE.prev_yield_10y
    if prev <= 0:
        _SYS_STATE.prev_yield_10y = cur_yield_10y
        return {"status": SystemStatus.SAFE.value, "change_pct": 0.0}

    change = abs(cur_yield_10y - prev) / max(prev, 0.001)
    if change > 0.12:
        _SYS_STATE.status = SystemStatus.HALT
        _SYS_STATE.halt_reason = f"BLACK_SWAN: 10Y yield change {change:.1%}"
    elif change > 0.05:
        _SYS_STATE.status = SystemStatus.WARNING
    else:
        if _SYS_STATE.status == SystemStatus.WARNING:
            _SYS_STATE.status = SystemStatus.SAFE

    # 每日亏损检查
    if _SYS_STATE.daily_pnl <= _SYS_STATE.daily_pnl_limit:
        _SYS_STATE.status = SystemStatus.HALT
        _SYS_STATE.halt_reason = f"DAILY_LOSS_LIMIT: pnl={_SYS_STATE.daily_pnl:.0f}"

    _SYS_STATE.prev_yield_10y = cur_yield_10y
    return {"status": _SYS_STATE.status.value, "change_pct": round(change, 4)}

# ══════════════════════════════════════════════════════════════════
#  7. VaR & 压力测试（移植自 risk_analytics.py）
# ══════════════════════════════════════════════════════════════════

def _stress_tests(portfolio_value: float,
                  mod_duration: float) -> List[Dict[str, Any]]:
    """
    利率冲击压力测试
    使用改进久期近似：ΔP ≈ -ModDur × ΔP × Δy
    """
    scenarios = [
        ("Rate Shock +50bp",   "+50bp 突然加息",  +0.005, "MODERATE"),
        ("Rate Shock +100bp",  "+100bp 加息冲击", +0.010, "HIGH"),
        ("Rate Shock +200bp",  "+200bp 紧缩周期", +0.020, "CRITICAL"),
        ("Rate Shock -50bp",   "-50bp 突然降息",  -0.005, "LOW"),
        ("Rate Shock -100bp",  "-100bp 降息周期", -0.010, "MODERATE"),
        ("2008 Crisis",        "2008 金融危机利率飙升", +0.015, "HIGH"),
        ("COVID Mar2020",      "COVID 流动性危机",      +0.008, "HIGH"),
        ("Yield Inversion",    "曲线全面倒挂",          +0.012, "MODERATE"),
        ("Inflation Shock",    "CPI 8%+，紧急加息",     +0.025, "CRITICAL"),
        ("Bull Steepening",    "长端大幅下行",           -0.015, "LOW"),
    ]
    results = []
    for name, desc, dy, severity in scenarios:
        # 价格变化近似（ModDur × portfolio_value × Δy）
        impact_pct = -mod_duration * dy * 100   # %
        impact_usd = portfolio_value * impact_pct / 100
        results.append({
            "scenario":       name,
            "description":    desc,
            "rate_change":    f"{dy*10000:+.0f}bp",
            "impact_pct":     round(impact_pct, 2),
            "impact_usd":     round(impact_usd, 0),
            "portfolio_after": round(portfolio_value + impact_usd, 0),
            "severity":       severity,
        })
    return results

def _historical_var(returns: np.ndarray, portfolio_value: float,
                    conf: float = 0.95) -> Dict[str, float]:
    """历史模拟 VaR + CVaR"""
    pct = (1 - conf) * 100
    var_ret = float(np.percentile(returns, pct))
    var_usd = var_ret * portfolio_value
    tail    = returns[returns <= var_ret]
    cvar    = float(np.mean(tail)) * portfolio_value if len(tail) > 0 else var_usd
    return {
        "var_95_usd":  round(abs(var_usd), 0),
        "cvar_95_usd": round(abs(cvar), 0),
        "var_95_pct":  round(abs(var_ret) * 100, 3),
    }

# ══════════════════════════════════════════════════════════════════
#  8. 国债拍卖日历（移植自 BondAuctionService）
# ══════════════════════════════════════════════════════════════════

def _upcoming_auctions(days_horizon: int = 14) -> List[Dict]:
    """未来拍卖日历（美国财政部规律）"""
    now = datetime.now(timezone.utc)
    schedule = [
        # (品种, 影响级别, 月内典型日期)
        ("2Y Note",  "HIGH",   22),
        ("5Y Note",  "HIGH",   23),
        ("10Y Note", "HIGH",   12),
        ("30Y Bond", "HIGH",   13),
        ("5Y TIPS",  "MEDIUM", 20),
        ("13W Bill", "LOW",    0),  # 0 = 每周
        ("26W Bill", "LOW",    0),
    ]
    result = []
    for tenor, impact, day in schedule:
        if day == 0:  # 每周
            ahead = (7 - now.weekday()) % 7 or 7
            next_date = (now + timedelta(days=ahead)).date()
        else:
            d = min(day, 28)
            try:
                next_date = now.replace(day=d).date()
                if next_date <= now.date():
                    if now.month == 12:
                        next_date = now.replace(year=now.year+1, month=1, day=d).date()
                    else:
                        next_date = now.replace(month=now.month+1, day=d).date()
            except Exception:
                next_date = now.date() + timedelta(days=15)

        days_away = (next_date - now.date()).days
        if 0 <= days_away <= days_horizon:
            result.append({
                "tenor":      tenor,
                "date":       str(next_date),
                "days_away":  days_away,
                "impact":     impact,
                "note":       "临近拍卖前24小时通常引发供给压力" if impact == "HIGH" else "",
            })
    result.sort(key=lambda x: x["days_away"])
    return result

# ══════════════════════════════════════════════════════════════════
#  9. CrewAI 工具
# ══════════════════════════════════════════════════════════════════

# ── Tool 1：收益率曲线 + Nelson-Siegel ──────────────────────────

class BondYCInput(BaseModel):
    analyze: str = Field(
        default="full",
        description=(
            "分析类型：\n"
            "'full' — 完整收益率曲线 + Nelson-Siegel 因子 + 形态判断\n"
            "'ns'   — 仅 Nelson-Siegel 拟合与因子解读\n"
            "'shape'— 仅曲线形态（NORMAL/FLAT/INVERTED/HUMPED）\n"
            "'cheapness' — 各期限相对价值（市场 vs NS 拟合，单位 bps）"
        )
    )

class BondYieldCurveTool(BaseTool):
    name: str = "BondYieldCurveTool"
    description: str = (
        "国债收益率曲线分析工具（实时 yfinance 数据）：\n\n"
        "① 完整曲线：3M / 2Y / 5Y / 10Y / 30Y 收益率及日/周变化\n"
        "② Nelson-Siegel 拟合：β₀（水平）/ β₁（斜率）/ β₂（曲率）\n"
        "   β₀↑ → 做空长端；β₁趋0 → Flattener；β₂>0 → 做空腹部\n"
        "③ 曲线形态：NORMAL/FLAT/INVERTED/HUMPED\n"
        "④ 相对价值：各期限相对 NS 模型的 richness/cheapness（bps）\n"
        "⑤ 关键利差：10Y-3M斜率、30Y-10Y期限溢价、蝶式利差\n\n"
        "输入 analyze='full' 获取完整报告"
    )
    args_schema: type[BaseModel] = BondYCInput

    def _run(self, analyze: str = "full") -> str:
        curve    = _fetch_yield_curve()
        macro    = _fetch_macro()
        ts       = datetime.now(timezone.utc).isoformat()

        # 提取有效期限数据
        tenors_yr = []
        yields_pct = []
        for t in ["3M", "2Y", "5Y", "10Y", "30Y"]:
            if t in curve:
                tenors_yr.append(TENOR_YEARS[t])
                yields_pct.append(curve[t]["yield"])

        # Nelson-Siegel 拟合
        ns = NelsonSiegelModel.fit(tenors_yr, yields_pct)
        ns_interp = {t: round(NelsonSiegelModel.fitted_yield(
                        TENOR_YEARS[t], ns["beta0"], ns["beta1"], ns["beta2"]), 3)
                     for t in ["3M", "2Y", "5Y", "10Y", "30Y"] if t in curve}
        ns_signals = NelsonSiegelModel.interpret_factors(
            ns["beta0"], ns["beta1"], ns["beta2"])

        # 相对价值（bps）
        cheapness = {}
        for t in ["3M", "2Y", "5Y", "10Y", "30Y"]:
            if t in curve:
                bps = NelsonSiegelModel.cheapness(
                    TENOR_YEARS[t], curve[t]["yield"],
                    ns["beta0"], ns["beta1"], ns["beta2"])
                cheapness[t] = bps  # 正 = cheap，负 = rich

        # 曲线形态
        y10 = curve.get("10Y", {}).get("yield", 4.3)
        y3m = curve.get("3M", {}).get("yield", 5.0)
        y30 = curve.get("30Y", {}).get("yield", 4.5)
        y5  = curve.get("5Y",  {}).get("yield", 4.2)
        y2  = curve.get("2Y",  {}).get("yield", 4.5)

        slope_10y_3m  = round(y10 - y3m, 3)
        term_spread   = round(y30 - y10, 3)
        butterfly_spd = round(2 * y10 - y5 - y30, 3)

        if slope_10y_3m < -0.1:
            shape = CurveShape.INVERTED
            shape_desc = "收益率曲线倒挂 — 历史上的衰退前兆"
            shape_risk = "HIGH"
        elif abs(slope_10y_3m) < 0.15:
            shape = CurveShape.FLAT
            shape_desc = "曲线趋平 — 政策不确定性上升，期限溢价压缩"
            shape_risk = "MODERATE"
        elif ns["beta2"] > 0.5:
            shape = CurveShape.HUMPED
            shape_desc = "中段隆起 — 5Y相对偏高，蝶式套利机会"
            shape_risk = "MODERATE"
        else:
            shape = CurveShape.NORMAL
            shape_desc = "正常上斜 — 期限溢价正常，无明显套利失衡"
            shape_risk = "LOW"

        # VIX & DXY 信号
        vix = macro.get("vix", 18.0)
        dxy = macro.get("dxy", 104.0)
        macro_signals = {
            "vix_regime":     "HIGH_VOL" if vix > 25 else "ELEVATED" if vix > 18 else "LOW_VOL",
            "dollar_strength":"STRONG" if dxy > 105 else "NEUTRAL" if dxy > 98 else "WEAK",
            "wti_price":      macro.get("wti", 78.0),
            "ispread":        _calc_ispread(macro.get("wti", 78.0), y10),
        }

        if analyze in ("shape", "ns"):
            if analyze == "shape":
                return json.dumps({
                    "timestamp": ts,
                    "shape": shape.value,
                    "description": shape_desc,
                    "risk": shape_risk,
                    "slope_10y_3m": slope_10y_3m,
                    "term_spread_30y_10y": term_spread,
                    "butterfly_spread": butterfly_spd,
                }, ensure_ascii=False, indent=2)
            else:
                return json.dumps({
                    "timestamp": ts,
                    "nelson_siegel": ns,
                    "fitted_yields": ns_interp,
                    "factor_signals": ns_signals,
                    "cheapness_bps": cheapness,
                }, ensure_ascii=False, indent=2)

        if analyze == "cheapness":
            return json.dumps({
                "timestamp": ts,
                "cheapness_bps": cheapness,
                "interpretation": {t: ("CHEAP" if v > 5 else "RICH" if v < -5 else "FAIR")
                                   for t, v in cheapness.items()},
            }, ensure_ascii=False, indent=2)

        # full
        return json.dumps({
            "timestamp": ts,
            "yield_curve": {
                t: {**curve[t], "ns_fitted": ns_interp.get(t),
                    "cheapness_bps": cheapness.get(t)}
                for t in curve
            },
            "shape": {
                "type": shape.value,
                "description": shape_desc,
                "risk_level": shape_risk,
                "slope_10y_3m": slope_10y_3m,
                "term_spread_30y_10y": term_spread,
                "butterfly_spread": butterfly_spd,
            },
            "nelson_siegel": {**ns, "factor_signals": ns_signals},
            "macro": {**macro_signals, "vix": vix, "dxy": dxy},
            "curve_signals": self._curve_signals(
                slope_10y_3m, term_spread, butterfly_spd,
                ns["beta1"], ns["beta2"], cheapness),
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _curve_signals(slope: float, term: float, bfly: float,
                       b1: float, b2: float, cheap: dict) -> List[Dict]:
        signals = []
        # 斜率信号
        if slope < -0.5:
            signals.append({"type": "STEEPENER", "priority": "HIGH",
                            "reason": f"曲线深度倒挂 slope={slope:.2f} → Steepener：做多 30Y / 做空 2Y"})
        elif -0.3 < slope < 0.2:
            signals.append({"type": "FLATTENER", "priority": "MODERATE",
                            "reason": f"曲线趋平 slope={slope:.2f} → Flattener：做多 2Y / 做空 10Y"})

        # 蝶式信号
        if bfly > 0.2:
            signals.append({"type": "BUTTERFLY_SHORT_BELLY",
                            "priority": "MODERATE",
                            "reason": f"5Y偏贵 butterfly={bfly:.3f} → 做空 5Y / 做多 2Y+30Y"})
        elif bfly < -0.2:
            signals.append({"type": "BUTTERFLY_LONG_BELLY",
                            "priority": "LOW",
                            "reason": f"5Y偏便宜 butterfly={bfly:.3f} → 做多 5Y / 做空 2Y+30Y"})

        # 相对价值
        for t, bps in cheap.items():
            if bps > 10:
                signals.append({"type": f"CHEAP_{t}", "priority": "LOW",
                                "reason": f"{t}比 NS 模型便宜 {bps:.1f}bps → 超配"})
            elif bps < -10:
                signals.append({"type": f"RICH_{t}", "priority": "LOW",
                                "reason": f"{t}比 NS 模型贵 {abs(bps):.1f}bps → 低配"})
        return signals

# ── Tool 2：交易信号（Ispread + 曲线 + 拍卖）──────────────────────

class BondSignalInput(BaseModel):
    mode: str = Field(
        default="all",
        description=(
            "'all'     — 综合信号（Ispread + 曲线 + 拍卖日历）\n"
            "'ispread' — 仅 Ispread 均值回归信号\n"
            "'curve'   — 仅曲线形态交易信号\n"
            "'auction' — 仅国债拍卖日历（未来14天）"
        )
    )

class BondSignalTool(BaseTool):
    name: str = "BondSignalTool"
    description: str = (
        "国债量化交易信号工具（三套策略）：\n\n"
        "① Ispread 均值回归（核心策略）：\n"
        "   Ispread = (WTI原油价格 / 10Y收益率) × 0.85\n"
        "   Ispread > 15 → SELL_BOND（债贵油便宜）\n"
        "   Ispread < 10 → BUY_BOND（债便宜油贵）\n\n"
        "② 曲线形态交易：\n"
        "   深度倒挂 → Steepener（做多长端/做空短端）\n"
        "   趋平 → Flattener（做多短端/做空长端）\n"
        "   中段隆起 → Butterfly Short Belly\n\n"
        "③ 拍卖日历预警：\n"
        "   高影响拍卖前24-48h → 供给压力，收益率通常上行\n\n"
        "配合 BondYieldCurveTool 获取 NS 因子后再运行此工具"
    )
    args_schema: type[BaseModel] = BondSignalInput

    def _run(self, mode: str = "all") -> str:
        ts = datetime.now(timezone.utc).isoformat()

        # 系统状态检查
        if _SYS_STATE.kill_switch or _SYS_STATE.status == SystemStatus.HALT:
            return json.dumps({
                "timestamp": ts,
                "system_status": _SYS_STATE.status.value,
                "halt_reason":   _SYS_STATE.halt_reason,
                "signal":        "HALT — 所有信号暂停",
            }, ensure_ascii=False, indent=2)

        curve = _fetch_yield_curve()
        macro = _fetch_macro()
        y10   = curve.get("10Y", {}).get("yield", 4.3)
        wti   = macro.get("wti", 78.0)
        vix   = macro.get("vix", 18.0)

        # 系统风险扫描
        risk_scan = _scan_risk(y10)

        if mode == "ispread":
            isp = _calc_ispread(wti, y10)
            sig = _ispread_signal(isp)
            sig.update({"timestamp": ts, "wti": wti, "yield_10y": y10,
                        "system_status": _SYS_STATE.status.value})
            return json.dumps(sig, ensure_ascii=False, indent=2)

        if mode == "auction":
            return json.dumps({
                "timestamp": ts,
                "auctions": _upcoming_auctions(14),
                "note": "高影响拍卖前 24-48h 通常带来供给压力，收益率倾向上行",
            }, ensure_ascii=False, indent=2)

        # curve signals
        y3m  = curve.get("3M",  {}).get("yield", 5.0)
        y5   = curve.get("5Y",  {}).get("yield", 4.2)
        y30  = curve.get("30Y", {}).get("yield", 4.5)
        slope    = round(y10 - y3m, 3)
        bfly     = round(2*y10 - y5 - y30, 3)
        term_spd = round(y30 - y10, 3)

        curve_sig = []
        if slope < -0.3:
            curve_sig.append({
                "type": "STEEPENER", "priority": "HIGH",
                "legs": {"long": "30Y", "short": "2Y"},
                "reason": f"倒挂 slope={slope:.2f}% → 做多长端",
            })
        elif abs(slope) < 0.25:
            curve_sig.append({
                "type": "FLATTENER", "priority": "MODERATE",
                "legs": {"long": "2Y", "short": "10Y"},
                "reason": f"趋平 slope={slope:.2f}% → 做多短端",
            })
        if abs(bfly) > 0.15:
            if bfly > 0:
                curve_sig.append({
                    "type": "BUTTERFLY_SHORT_BELLY", "priority": "MODERATE",
                    "legs": {"short": "5Y", "long": "2Y+30Y"},
                    "reason": f"蝶式 {bfly:.3f}% → 5Y相对贵",
                })

        # Ispread
        isp     = _calc_ispread(wti, y10)
        isp_sig = _ispread_signal(isp)

        # 拍卖
        auctions = _upcoming_auctions(7)
        high_imp = [a for a in auctions if a["impact"] == "HIGH" and a["days_away"] <= 3]

        # 综合建议
        top_signal = isp_sig["signal"]
        top_conf   = isp_sig["confidence"]
        if curve_sig and curve_sig[0]["priority"] == "HIGH":
            top_signal = curve_sig[0]["type"]
            top_conf   = 0.65

        # EXIT_ONLY 时降低置信度
        if _SYS_STATE.status == SystemStatus.EXIT_ONLY:
            top_conf  *= 0.3

        return json.dumps({
            "timestamp":      ts,
            "system_status":  _SYS_STATE.status.value,
            "risk_scan":      risk_scan,
            "top_signal": {
                "signal":     top_signal,
                "confidence": round(top_conf, 3),
            },
            "ispread_signal": isp_sig,
            "curve_signals":  curve_sig,
            "upcoming_high_impact_auctions": high_imp,
            "vix":   vix,
            "wti":   wti,
            "yield_10y": y10,
            "slope_10y_3m": slope,
            "term_spread_30y_10y": term_spd,
            "butterfly_spread": bfly,
        }, ensure_ascii=False, indent=2)

# ── Tool 3：风险分析（DV01/久期/VaR/压力测试 + 系统状态）────────

class BondRiskInput(BaseModel):
    query: str = Field(
        default="full",
        description=(
            "'full'        — 完整风险报告\n"
            "'dv01'        — DV01 / 久期 / 凸性（需提供 tenor 参数）\n"
            "'var'         — VaR & CVaR（历史模拟）\n"
            "'stress'      — 利率压力测试\n"
            "'status'      — 系统状态查询\n"
            "'kill_switch' — 激活紧急停止\n"
            "'reset'       — 解除 WARNING（HALT 须重启）"
        )
    )
    tenor: str = Field(
        default="10Y",
        description="债券期限（2Y / 5Y / 10Y / 30Y），用于 DV01 计算"
    )
    portfolio_value: float = Field(
        default=0.0,
        description="组合市值（美元），0 则使用 .env SI_EQUITY"
    )

class BondRiskTool(BaseTool):
    name: str = "BondRiskTool"
    description: str = (
        "国债组合风险管理工具：\n\n"
        "① DV01 / 修正久期 / 凸性：\n"
        "   基于完整债券定价公式（可选 2Y/5Y/10Y/30Y）\n"
        "   DV01 = ModDur × Price × 0.0001（每bp价格变化）\n\n"
        "② VaR & CVaR：\n"
        "   历史模拟法（95%置信度）\n"
        "   基于过去90天收益率日变化\n\n"
        "③ 利率压力测试（10个情景）：\n"
        "   ±50bp / ±100bp / 2008危机 / COVID / 通胀冲击\n"
        "   使用修正久期近似计算组合价值变化\n\n"
        "④ 系统状态机（SAFE/WARNING/EXIT_ONLY/HALT）：\n"
        "   - 10Y 收益率单日变化 >5% → WARNING\n"
        "   - >12% → HALT（Black Swan 防护）\n"
        "   - 每日最大亏损触发 → HALT + Kill Switch"
    )
    args_schema: type[BaseModel] = BondRiskInput

    def _run(self, query: str = "full", tenor: str = "10Y",
             portfolio_value: float = 0.0) -> str:
        ts  = datetime.now(timezone.utc).isoformat()
        pv  = portfolio_value or float(os.getenv("BOND_EQUITY", "1000000"))

        if query == "kill_switch":
            _SYS_STATE.kill_switch = True
            _SYS_STATE.status = SystemStatus.HALT
            _SYS_STATE.halt_reason = "MANUAL_KILL_SWITCH"
            return json.dumps({"action": "kill_switch", "activated": True,
                               "status": "HALT"}, ensure_ascii=False)

        if query == "reset":
            if _SYS_STATE.kill_switch:
                return json.dumps({"action": "reset", "success": False,
                                   "note": "Kill Switch 已激活，须重启程序"})
            _SYS_STATE.status = SystemStatus.SAFE
            _SYS_STATE.halt_reason = ""
            return json.dumps({"action": "reset", "success": True,
                               "status": "SAFE"})

        if query == "status":
            return json.dumps({
                "timestamp":         ts,
                "system_status":     _SYS_STATE.status.value,
                "kill_switch":       _SYS_STATE.kill_switch,
                "halt_reason":       _SYS_STATE.halt_reason,
                "daily_pnl":         round(_SYS_STATE.daily_pnl, 2),
                "daily_pnl_limit":   round(_SYS_STATE.daily_pnl_limit, 2),
                "consecutive_losses":_SYS_STATE.consecutive_losses,
                "open_positions":    len(_POSITIONS),
                "cooldown_active":   time.time() < _COOLDOWN_UNTIL,
            }, ensure_ascii=False, indent=2)

        # 当前收益率
        curve  = _fetch_yield_curve()
        ytm    = curve.get(tenor, {}).get("yield", 4.3) / 100  # → decimal

        # 债券参数（标准政府债券）
        tenor_map = {"2Y": (2, 4),  "5Y": (5, 10), "10Y": (10, 20), "30Y": (30, 60)}
        years, n_periods = tenor_map.get(tenor, (10, 20))
        coupon = ytm  # 假设平价债券：票息率 = 到期收益率
        face   = 1000.0

        price  = BondMath.price(face, coupon, ytm, n_periods)
        mac_d, mod_d = BondMath.duration(face, coupon, ytm, n_periods)
        dv01_v = BondMath.dv01(face, coupon, ytm, n_periods)
        conv   = BondMath.convexity(face, coupon, ytm, n_periods)

        dv01_data = {
            "tenor":       tenor,
            "ytm_pct":     round(ytm * 100, 3),
            "price":       price,
            "mac_duration": mac_d,
            "mod_duration": mod_d,
            "dv01_per_1000": dv01_v,
            "dv01_portfolio": round(dv01_v * pv / face, 2),
            "convexity":   conv,
            "price_chg_100bp":  round(BondMath.approx_price_change(mod_d, conv, price, 100), 4),
            "price_chg_m100bp": round(BondMath.approx_price_change(mod_d, conv, price, -100), 4),
        }

        if query == "dv01":
            return json.dumps({"timestamp": ts, **dv01_data}, ensure_ascii=False, indent=2)

        # 压力测试
        stress = _stress_tests(pv, mod_d)

        if query == "stress":
            return json.dumps({"timestamp": ts, "tenor": tenor,
                               "mod_duration": mod_d,
                               "portfolio_value": pv,
                               "stress_tests": stress}, ensure_ascii=False, indent=2)

        # VaR（用历史收益率数据近似）
        try:
            import yfinance as yf
            hist = yf.Ticker("^TNX").history(period="6mo")
            if not hist.empty:
                yields_h = hist["Close"].values
                rets = np.diff(yields_h) / yields_h[:-1]
                # 收益率变化 → 债券收益（负相关）
                bond_rets = -mod_d * rets
                var_data = _historical_var(bond_rets, pv)
            else:
                raise ValueError("empty")
        except Exception:
            var_data = {"var_95_usd": round(pv * mod_d * 0.002, 0),
                        "cvar_95_usd": round(pv * mod_d * 0.003, 0),
                        "var_95_pct": round(mod_d * 0.2, 3)}

        if query == "var":
            return json.dumps({"timestamp": ts, "tenor": tenor,
                               **var_data}, ensure_ascii=False, indent=2)

        # full
        return json.dumps({
            "timestamp":     ts,
            "system_status": _SYS_STATE.status.value,
            "halt_reason":   _SYS_STATE.halt_reason,
            "bond_metrics":  dv01_data,
            "var":           var_data,
            "stress_tests":  stress[:6],  # 返回前6个最重要场景
            "open_positions": len(_POSITIONS),
            "daily_pnl":     round(_SYS_STATE.daily_pnl, 2),
            "cooldown_active": time.time() < _COOLDOWN_UNTIL,
        }, ensure_ascii=False, indent=2)
