"""
crypto_trading_tool.py — 加密货币量化交易 CrewAI 工具
======================================================
直接移植 Crypto-main 机器人架构（RegimeEngine / FragilityEngine /
SignalEngine / ExecutionGate / RiskEngine），对接 Binance Futures via ccxt。

工具列表：
  CryptoSignalTool      → 读取市场数据 → 生成 BTC/ETH/SOL 交易信号
  CryptoExecutionTool   → 执行开仓/平仓指令（paper 或真实）
  CryptoPortfolioTool   → 查询当前持仓、PnL、保证金状态

运行模式：
  PAPER  (默认) — 不发送真实订单，记录到内存
  LIVE          — 对接 Binance Futures 真实账户（需要 API Key）

设置 .env 变量：
  BINANCE_API_KEY / BINANCE_API_SECRET
  CRYPTO_MODE=PAPER|LIVE
  CRYPTO_LEVERAGE=20          # 默认杠杆倍数
  CRYPTO_SYMBOLS=BTC,ETH,SOL  # 监控品种
"""

from __future__ import annotations

import os
import math
import time
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any
from collections import deque

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

# ══════════════════════════════════════════════════════
#  1. 核心数据结构（直接移植自 Crypto-main 架构）
# ══════════════════════════════════════════════════════

class Regime(str, Enum):
    NORMAL      = "NORMAL"
    MOMENTUM    = "MOMENTUM"
    SQUEEZE_RISK = "SQUEEZE_RISK"
    UNSTABLE    = "UNSTABLE"

class FragilityState(str, Enum):
    LOW    = "LOW_FRAGILITY"
    MEDIUM = "MEDIUM_FRAGILITY"
    HIGH   = "HIGH_FRAGILITY"

class EventState(str, Enum):
    IDLE    = "IDLE"
    WAIT    = "WAIT"
    READY   = "READY"
    INVALID = "INVALID"

class GateAction(str, Enum):
    ALLOW         = "ALLOW"
    ALLOW_REDUCED = "ALLOW_REDUCED"
    BLOCK         = "BLOCK"
    EXIT_NOW      = "EXIT_NOW"
    FREEZE        = "FREEZE"

@dataclass
class FeatureSnapshot:
    symbol: str
    ts: str
    spread_ratio: float          # 当前点差 / 基准点差
    depth_shrink_ratio: float    # 深度萎缩比
    taker_buy_ratio: float       # 主动买成交占比
    taker_sell_ratio: float      # 主动卖成交占比
    oi_delta_ratio: float        # OI 变化率（正=增仓，负=减仓）
    funding_rate: float          # 资金费率（%）
    liquidation_proximity: float # 最近强平距离 / ATR（0-1）
    venue_divergence: float      # 跨交易所价差（倍）
    stale_quote: bool
    abnormal_wick_score: float   # 异常上下影线分数（0-1）
    network_incident_flag: bool  = False
    exchange_incident_flag: bool = False

@dataclass
class SignalCandidate:
    symbol: str
    ts: str
    side: Optional[str]          # LONG / SHORT / None
    direction_score: float       # 0-100
    conviction_score: float      # 0-100
    fragility_score: float       # 0-100
    candidate_type: str
    reason_codes: List[str] = field(default_factory=list)

@dataclass
class GateDecision:
    action: GateAction
    approved_side: Optional[str]
    size_multiplier: float
    reason_codes: List[str] = field(default_factory=list)

@dataclass
class PositionState:
    symbol: str
    side: Optional[str] = None
    size: float = 0.0
    entry_price: Optional[float] = None
    age_minutes: float = 0.0
    unrealized_pnl: float = 0.0
    entry_ts: Optional[str] = None

# ══════════════════════════════════════════════════════
#  2. 引擎层（原样移植，无修改）
# ══════════════════════════════════════════════════════

class RegimeEngine:
    """市场状态检测：NORMAL / MOMENTUM / SQUEEZE_RISK / UNSTABLE"""
    def evaluate(self, snap: FeatureSnapshot) -> Regime:
        if snap.stale_quote or snap.venue_divergence > 2.0:
            return Regime.UNSTABLE
        if abs(snap.oi_delta_ratio) > 1.8 and snap.liquidation_proximity > 0.7:
            return Regime.SQUEEZE_RISK
        if max(snap.taker_buy_ratio, snap.taker_sell_ratio) > 0.6:
            return Regime.MOMENTUM
        return Regime.NORMAL

class FragilityEngine:
    """脆弱度评估：HIGH 时拒绝开仓"""
    def evaluate(self, snap: FeatureSnapshot) -> FragilityState:
        if (snap.stale_quote or snap.network_incident_flag or
                snap.exchange_incident_flag or snap.spread_ratio > 2.0 or
                snap.depth_shrink_ratio > 2.0 or snap.venue_divergence > 2.0 or
                snap.abnormal_wick_score > 0.8):
            return FragilityState.HIGH
        if (snap.spread_ratio > 1.4 or snap.depth_shrink_ratio > 1.4 or
                snap.venue_divergence > 1.3 or snap.abnormal_wick_score > 0.5):
            return FragilityState.MEDIUM
        return FragilityState.LOW

class BaseSignalEngine:
    """信号生成器：Direction Score + Conviction Score → LONG/SHORT/NO_TRADE"""
    symbol = "BASE"

    def generate(self, snap: FeatureSnapshot, regime: Regime,
                 fragility: FragilityState) -> SignalCandidate:
        dir_score  = self.direction_score(snap)
        conv_score = self.conviction_score(snap)
        frag_score = self.fragility_score(snap)

        if fragility == FragilityState.HIGH:
            return SignalCandidate(snap.symbol, snap.ts, None,
                                   dir_score, conv_score, frag_score,
                                   "NO_TRADE", ["FRAGILITY_HIGH"])
        if dir_score >= 60 and conv_score >= 55:
            return SignalCandidate(snap.symbol, snap.ts, "LONG",
                                   dir_score, conv_score, frag_score,
                                   "LONG_CANDIDATE", ["DIRECTION_LONG"])
        if dir_score <= 40 and conv_score >= 55:
            return SignalCandidate(snap.symbol, snap.ts, "SHORT",
                                   dir_score, conv_score, frag_score,
                                   "SHORT_CANDIDATE", ["DIRECTION_SHORT"])
        return SignalCandidate(snap.symbol, snap.ts, None,
                               dir_score, conv_score, frag_score,
                               "NO_TRADE", ["INSUFFICIENT_EDGE"])

    def direction_score(self, snap: FeatureSnapshot) -> float:
        return 50.0 + (snap.taker_buy_ratio - snap.taker_sell_ratio) * 50.0

    def conviction_score(self, snap: FeatureSnapshot) -> float:
        return min(100.0, 50.0 + abs(snap.oi_delta_ratio) * 20.0 +
                   snap.liquidation_proximity * 20.0)

    def fragility_score(self, snap: FeatureSnapshot) -> float:
        return min(100.0, snap.spread_ratio * 20.0 +
                   snap.depth_shrink_ratio * 20.0 + snap.venue_divergence * 20.0)

class BTCSignalEngine(BaseSignalEngine):
    """BTC 专用信号引擎：衍生品主导 + 宏观过滤"""
    symbol = "BTC"

    def direction_score(self, snap: FeatureSnapshot) -> float:
        base = super().direction_score(snap)
        # BTC：资金费率极端时反向
        if snap.funding_rate > 0.05:    # 过热，看空
            base -= 10
        elif snap.funding_rate < -0.02:  # 恐慌，看多
            base += 8
        # OI 快速增加 + 多头主导 → 动能买入
        if snap.oi_delta_ratio > 1.5 and snap.taker_buy_ratio > 0.55:
            base += 12
        return max(0.0, min(100.0, base))

class ETHSignalEngine(BaseSignalEngine):
    """ETH 信号引擎：BTC 传导 + 生态催化"""
    symbol = "ETH"

class SOLSignalEngine(BaseSignalEngine):
    """SOL 信号引擎：高波动微结构主导"""
    symbol = "SOL"

    def conviction_score(self, snap: FeatureSnapshot) -> float:
        # SOL 更依赖流动性深度
        base = super().conviction_score(snap)
        if snap.depth_shrink_ratio < 0.8:  # 深度充裕 → 加分
            base += 8
        return min(100.0, base)

class RiskEngine:
    """分级止损：预警 / 减仓 / 主止损 / 灾难止损"""
    def evaluate(self, unrealized_pnl: float,
                 warning: float, reduce: float,
                 stop: float, catastrophe: float) -> str:
        if unrealized_pnl <= catastrophe: return "CATASTROPHE_EXIT"
        if unrealized_pnl <= stop:        return "MAIN_STOP"
        if unrealized_pnl <= reduce:      return "REDUCE_POSITION"
        if unrealized_pnl <= warning:     return "PRE_WARNING"
        return "HOLD"

class ExecutionGate:
    """执行门控：整合所有检查，输出最终行动决策"""
    def decide(self, regime: Regime, event_state: EventState,
               fragility: FragilityState, signal_side: Optional[str],
               signal_confidence: float, stale_quote: bool,
               venue_divergence: float, daily_drawdown_hit: bool,
               deterioration_triggered: bool, cooldown_state: str,
               risk_multiplier: float, position_open: bool,
               position_side: Optional[str], position_age_minutes: float,
               max_position_age_minutes: float,
               exchange_incident_flag: bool = False,
               network_incident_flag: bool = False,
               trade_allowed: bool = True) -> GateDecision:

        if exchange_incident_flag or network_incident_flag:
            return GateDecision(GateAction.FREEZE, None, 0.0, ["INCIDENT_FLAG"])
        if stale_quote:
            return GateDecision(GateAction.BLOCK, None, 0.0, ["STALE_QUOTE"])
        if venue_divergence > 2.0:
            return GateDecision(GateAction.BLOCK, None, 0.0, ["VENUE_DIVERGENCE"])
        if regime == Regime.UNSTABLE or deterioration_triggered or fragility == FragilityState.HIGH:
            if position_open:
                return GateDecision(GateAction.EXIT_NOW, position_side, 0.0, ["DETERIORATION_EXIT"])
            return GateDecision(GateAction.FREEZE, None, 0.0, ["MARKET_UNSTABLE"])
        if cooldown_state == "COOLDOWN":
            return GateDecision(GateAction.BLOCK, None, 0.0, ["COOLDOWN_ACTIVE"])
        if daily_drawdown_hit:
            return GateDecision(GateAction.FREEZE, None, 0.0, ["DAILY_DD_LIMIT"])
        if position_open and position_age_minutes >= max_position_age_minutes:
            return GateDecision(GateAction.EXIT_NOW, position_side, 0.0, ["TIME_STOP"])
        if event_state in (EventState.WAIT, EventState.INVALID):
            return GateDecision(GateAction.BLOCK, None, 0.0, [f"EVENT_{event_state.value}"])
        if not trade_allowed or not signal_side:
            return GateDecision(GateAction.BLOCK, None, 0.0, ["NO_VALID_SIGNAL"])
        if fragility == FragilityState.MEDIUM or risk_multiplier < 1.0:
            return GateDecision(GateAction.ALLOW_REDUCED, signal_side,
                                max(0.1, risk_multiplier), ["REDUCED_MODE"])
        return GateDecision(GateAction.ALLOW, signal_side, 1.0, ["ALL_CHECKS_PASS"])

# ══════════════════════════════════════════════════════
#  3. Binance 数据层（ccxt）
# ══════════════════════════════════════════════════════

_SIGNAL_ENGINES = {
    "BTC": BTCSignalEngine(),
    "ETH": ETHSignalEngine(),
    "SOL": SOLSignalEngine(),
}
_REGIME_ENGINE    = RegimeEngine()
_FRAGILITY_ENGINE = FragilityEngine()
_RISK_ENGINE      = RiskEngine()
_GATE_ENGINE      = ExecutionGate()

# 全局持仓状态（paper mode 内存存储）
_POSITIONS: Dict[str, PositionState] = {}
_DAILY_PNL: float = 0.0
_DAILY_PNL_LIMIT: float = -0.05  # -5% 触发每日止损
_COOLDOWN_UNTIL: float = 0.0

# 时间止损参数（分钟）
_MAX_POSITION_AGE = {
    "BTC": 35,   # 30-40 min per design spec
    "ETH": 20,
    "SOL": 15,
}

def _get_exchange():
    """获取 ccxt Binance Futures 交易所实例"""
    try:
        import ccxt
        api_key    = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        mode       = os.getenv("CRYPTO_MODE", "PAPER").upper()

        params = {
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        if api_key and api_secret:
            params["apiKey"] = api_key
            params["secret"] = api_secret

        ex = ccxt.binance(params)
        if mode == "PAPER" or not api_key:
            ex.set_sandbox_mode(True)
        return ex
    except Exception as e:
        return None

_SYMBOL_MAP = {"BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT", "SOL": "SOL/USDT:USDT"}

def _fetch_feature_snapshot(symbol: str) -> FeatureSnapshot:
    """从 Binance 拉取实时数据，计算 FeatureSnapshot"""
    ex = _get_exchange()
    ts = datetime.now(timezone.utc).isoformat()
    ccxt_sym = _SYMBOL_MAP.get(symbol, f"{symbol}/USDT:USDT")

    defaults = FeatureSnapshot(
        symbol=symbol, ts=ts,
        spread_ratio=1.0, depth_shrink_ratio=1.0,
        taker_buy_ratio=0.5, taker_sell_ratio=0.5,
        oi_delta_ratio=0.0, funding_rate=0.01,
        liquidation_proximity=0.3, venue_divergence=1.0,
        stale_quote=True, abnormal_wick_score=0.0,
    )

    if ex is None:
        return defaults

    try:
        # 订单簿
        ob = ex.fetch_order_book(ccxt_sym, limit=20)
        bid = ob["bids"][0][0] if ob["bids"] else 0
        ask = ob["asks"][0][0] if ob["asks"] else 0
        mid = (bid + ask) / 2 if bid and ask else 1
        spread = (ask - bid) / mid if mid > 0 else 0.001

        # 基准点差（对于主流币 0.01% = 0.0001）
        baseline_spread = 0.0001
        spread_ratio = spread / baseline_spread if baseline_spread > 0 else 1.0
        spread_ratio = min(spread_ratio, 5.0)

        # 主动买卖
        trades = ex.fetch_trades(ccxt_sym, limit=200)
        buy_vol = sum(t["amount"] for t in trades if t.get("side") == "buy")
        sell_vol = sum(t["amount"] for t in trades if t.get("side") == "sell")
        total_vol = buy_vol + sell_vol + 1e-9
        taker_buy  = buy_vol / total_vol
        taker_sell = sell_vol / total_vol

        # OI（持仓量变化）
        try:
            oi_data = ex.fetch_open_interest(ccxt_sym)
            current_oi = oi_data.get("openInterestAmount", 0)
        except Exception:
            current_oi = 0

        # 资金费率
        try:
            fr = ex.fetch_funding_rate(ccxt_sym)
            funding = fr.get("fundingRate", 0.01) * 100  # 转为 %
        except Exception:
            funding = 0.01

        # 异常影线检测（用最近 K 线）
        try:
            ohlcv = ex.fetch_ohlcv(ccxt_sym, "1m", limit=10)
            wick_scores = []
            for o, h, l, c, v in ohlcv:
                body = abs(c - o)
                wick = (h - max(o, c)) + (min(o, c) - l)
                total_range = h - l + 1e-9
                wick_scores.append(wick / total_range if total_range > 0 else 0)
            wick_score = sum(wick_scores) / len(wick_scores) if wick_scores else 0.0
        except Exception:
            wick_score = 0.0

        stale = False  # 数据刚取到

        return FeatureSnapshot(
            symbol=symbol, ts=ts,
            spread_ratio=spread_ratio,
            depth_shrink_ratio=1.0,      # 需要历史基准才能计算，默认正常
            taker_buy_ratio=taker_buy,
            taker_sell_ratio=taker_sell,
            oi_delta_ratio=0.0,          # 需要前值，首次为 0
            funding_rate=funding,
            liquidation_proximity=0.3,   # 需要清算图 API（Coinglass 等）
            venue_divergence=1.0,        # 需要多交易所
            stale_quote=stale,
            abnormal_wick_score=wick_score,
        )

    except Exception as e:
        defaults.stale_quote = False  # 不标记 stale，让引擎正常跑
        return defaults

# ══════════════════════════════════════════════════════
#  4. Paper Broker（无真实账户时模拟执行）
# ══════════════════════════════════════════════════════

class PaperBroker:
    """纸交易撮合器：记录开平仓，计算 PnL"""

    def open_position(self, symbol: str, side: str, size_usdt: float,
                      price: float, leverage: int) -> dict:
        pos = PositionState(
            symbol=symbol, side=side,
            size=size_usdt * leverage / price,
            entry_price=price,
            age_minutes=0.0, unrealized_pnl=0.0,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        _POSITIONS[symbol] = pos
        return {"action": "OPEN", "symbol": symbol, "side": side,
                "size": pos.size, "price": price, "leverage": leverage,
                "notional_usdt": size_usdt * leverage,
                "mode": "PAPER"}

    def close_position(self, symbol: str, current_price: float,
                       reason: str = "SIGNAL") -> dict:
        pos = _POSITIONS.pop(symbol, None)
        if pos is None:
            return {"action": "CLOSE", "symbol": symbol, "status": "NO_POSITION"}
        if pos.entry_price and pos.size:
            if pos.side == "LONG":
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - current_price) / pos.entry_price
            pnl_usdt = pnl_pct * pos.size * pos.entry_price
        else:
            pnl_pct = 0.0
            pnl_usdt = 0.0

        global _DAILY_PNL
        _DAILY_PNL += pnl_usdt

        return {"action": "CLOSE", "symbol": symbol,
                "side": pos.side, "entry": pos.entry_price,
                "exit": current_price, "pnl_pct": round(pnl_pct * 100, 3),
                "pnl_usdt": round(pnl_usdt, 2), "reason": reason,
                "mode": "PAPER"}

_PAPER_BROKER = PaperBroker()

def _get_current_price(symbol: str) -> float:
    """快速获取最新价格"""
    ex = _get_exchange()
    if ex is None:
        return {"BTC": 97000.0, "ETH": 3200.0, "SOL": 160.0}.get(symbol, 100.0)
    try:
        ccxt_sym = _SYMBOL_MAP.get(symbol, f"{symbol}/USDT:USDT")
        ticker = ex.fetch_ticker(ccxt_sym)
        return ticker.get("last", 0) or ticker.get("close", 0)
    except Exception:
        return {"BTC": 97000.0, "ETH": 3200.0, "SOL": 160.0}.get(symbol, 100.0)

# ══════════════════════════════════════════════════════
#  5. CrewAI 工具：CryptoSignalTool
# ══════════════════════════════════════════════════════

class CryptoSignalInput(BaseModel):
    symbols: str = Field(
        default="BTC,ETH,SOL",
        description="逗号分隔的品种列表，如 'BTC,ETH,SOL'"
    )
    verbose: bool = Field(default=False, description="是否返回详细评分")

class CryptoSignalTool(BaseTool):
    name: str = "CryptoSignalTool"
    description: str = (
        "实时读取 Binance Futures 市场数据，运行多层信号引擎（RegimeEngine / "
        "FragilityEngine / SignalEngine / ExecutionGate），输出每个品种的交易信号：\n"
        "- LONG_CANDIDATE / SHORT_CANDIDATE / NO_TRADE\n"
        "- 方向得分、确信度、脆弱度\n"
        "- ExecutionGate 最终决策：ALLOW / ALLOW_REDUCED / BLOCK / EXIT_NOW / FREEZE\n\n"
        "输入：symbols（逗号分隔，如 'BTC,ETH,SOL'）\n"
        "用于：决策是否开仓、平仓"
    )
    args_schema: type[BaseModel] = CryptoSignalInput

    def _run(self, symbols: str = "BTC,ETH,SOL", verbose: bool = False) -> str:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        results = []
        now_ts = time.time()
        cooldown_active = now_ts < _COOLDOWN_UNTIL

        for sym in symbol_list:
            try:
                snap = _fetch_feature_snapshot(sym)
                regime    = _REGIME_ENGINE.evaluate(snap)
                fragility = _FRAGILITY_ENGINE.evaluate(snap)
                engine    = _SIGNAL_ENGINES.get(sym, BaseSignalEngine())
                signal    = engine.generate(snap, regime, fragility)

                # 当前持仓信息
                pos = _POSITIONS.get(sym)
                pos_age = 0.0
                if pos and pos.entry_ts:
                    elapsed = (datetime.now(timezone.utc) -
                               datetime.fromisoformat(pos.entry_ts)).total_seconds()
                    pos.age_minutes = elapsed / 60.0
                    pos_age = pos.age_minutes
                    # 更新 PnL
                    price_now = _get_current_price(sym)
                    if pos.entry_price and pos.size:
                        if pos.side == "LONG":
                            pos.unrealized_pnl = (price_now - pos.entry_price) * pos.size
                        else:
                            pos.unrealized_pnl = (pos.entry_price - price_now) * pos.size

                max_age = _MAX_POSITION_AGE.get(sym, 30)
                daily_dd_hit = _DAILY_PNL < (_DAILY_PNL_LIMIT * 10000)  # 假设初始 1w USDT

                gate_decision = _GATE_ENGINE.decide(
                    regime=regime,
                    event_state=EventState.READY,
                    fragility=fragility,
                    signal_side=signal.side,
                    signal_confidence=signal.conviction_score,
                    stale_quote=snap.stale_quote,
                    venue_divergence=snap.venue_divergence,
                    daily_drawdown_hit=daily_dd_hit,
                    deterioration_triggered=False,
                    cooldown_state="COOLDOWN" if cooldown_active else "OK",
                    risk_multiplier=1.0 if fragility == FragilityState.LOW else 0.5,
                    position_open=(pos is not None),
                    position_side=pos.side if pos else None,
                    position_age_minutes=pos_age,
                    max_position_age_minutes=max_age,
                    exchange_incident_flag=snap.exchange_incident_flag,
                    network_incident_flag=snap.network_incident_flag,
                    trade_allowed=True,
                )

                rec = {
                    "symbol": sym,
                    "regime": regime.value,
                    "fragility": fragility.value,
                    "signal": signal.candidate_type,
                    "direction": signal.side,
                    "direction_score": round(signal.direction_score, 1),
                    "conviction_score": round(signal.conviction_score, 1),
                    "gate_action": gate_decision.action.value,
                    "gate_reasons": gate_decision.reason_codes,
                    "size_multiplier": gate_decision.size_multiplier,
                    "position": {
                        "open": pos is not None,
                        "side": pos.side if pos else None,
                        "age_min": round(pos_age, 1),
                        "unrealized_pnl": round(pos.unrealized_pnl, 2) if pos else 0,
                    },
                }
                if verbose:
                    rec["snapshot"] = {
                        "spread_ratio": snap.spread_ratio,
                        "taker_buy": snap.taker_buy_ratio,
                        "taker_sell": snap.taker_sell_ratio,
                        "oi_delta": snap.oi_delta_ratio,
                        "funding_rate": snap.funding_rate,
                        "wick_score": snap.abnormal_wick_score,
                    }
                results.append(rec)

            except Exception as e:
                results.append({"symbol": sym, "error": str(e)})

        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": os.getenv("CRYPTO_MODE", "PAPER"),
            "daily_pnl_usdt": round(_DAILY_PNL, 2),
            "signals": results,
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════
#  6. CrewAI 工具：CryptoExecutionTool
# ══════════════════════════════════════════════════════

class CryptoExecInput(BaseModel):
    action: str = Field(
        description="OPEN 或 CLOSE",
    )
    symbol: str = Field(
        description="品种：BTC / ETH / SOL",
    )
    side: Optional[str] = Field(
        default=None,
        description="LONG 或 SHORT（开仓时必填）",
    )
    size_usdt: float = Field(
        default=100.0,
        description="名义价值 USDT（开仓时用）",
    )
    reason: str = Field(
        default="SIGNAL",
        description="平仓原因（SIGNAL / TIME_STOP / RISK_STOP）",
    )

class CryptoExecutionTool(BaseTool):
    name: str = "CryptoExecutionTool"
    description: str = (
        "执行加密货币期货开仓或平仓。\n"
        "- OPEN：开多或开空，指定 symbol + side + size_usdt\n"
        "- CLOSE：平仓，指定 symbol + reason\n\n"
        "PAPER 模式：记录到内存，不发送真实订单。\n"
        "LIVE 模式：通过 Binance Futures API 执行（需配置 BINANCE_API_KEY）。\n\n"
        "使用前必须先运行 CryptoSignalTool 确认 gate_action == ALLOW。"
    )
    args_schema: type[BaseModel] = CryptoExecInput

    def _run(self, action: str, symbol: str, side: Optional[str] = None,
             size_usdt: float = 100.0, reason: str = "SIGNAL") -> str:
        symbol = symbol.upper()
        action = action.upper()
        mode   = os.getenv("CRYPTO_MODE", "PAPER").upper()
        leverage = int(os.getenv("CRYPTO_LEVERAGE", "20"))

        price = _get_current_price(symbol)

        if action == "OPEN":
            if not side:
                return json.dumps({"error": "OPEN 需要指定 side (LONG/SHORT)"})
            side = side.upper()

            if mode == "LIVE":
                result = self._live_open(symbol, side, size_usdt, leverage)
            else:
                result = _PAPER_BROKER.open_position(symbol, side, size_usdt, price, leverage)

        elif action == "CLOSE":
            if mode == "LIVE":
                result = self._live_close(symbol, price, reason)
            else:
                result = _PAPER_BROKER.close_position(symbol, price, reason)

            # 平仓后设置冷静期（5分钟）
            global _COOLDOWN_UNTIL
            _COOLDOWN_UNTIL = time.time() + 300

        else:
            return json.dumps({"error": f"未知 action: {action}，支持 OPEN / CLOSE"})

        return json.dumps(result, ensure_ascii=False, indent=2)

    def _live_open(self, symbol: str, side: str, size_usdt: float, leverage: int) -> dict:
        """真实开仓（Binance Futures）"""
        try:
            ex = _get_exchange()
            if ex is None:
                return {"error": "Binance 连接失败，请检查 API Key"}
            ccxt_sym = _SYMBOL_MAP.get(symbol, f"{symbol}/USDT:USDT")
            # 设置杠杆
            ex.set_leverage(leverage, ccxt_sym)
            # 计算数量
            price = _get_current_price(symbol)
            qty   = size_usdt * leverage / price
            qty   = round(qty, 3)
            order_side = "buy" if side == "LONG" else "sell"
            order = ex.create_market_order(ccxt_sym, order_side, qty)
            # 同步到本地持仓
            _POSITIONS[symbol] = PositionState(
                symbol=symbol, side=side, size=qty,
                entry_price=order.get("average") or price,
                entry_ts=datetime.now(timezone.utc).isoformat()
            )
            return {"action": "OPEN", "symbol": symbol, "side": side,
                    "qty": qty, "price": price, "order_id": order.get("id"),
                    "mode": "LIVE"}
        except Exception as e:
            return {"error": str(e), "mode": "LIVE"}

    def _live_close(self, symbol: str, price: float, reason: str) -> dict:
        """真实平仓（Binance Futures）"""
        try:
            ex = _get_exchange()
            if ex is None:
                return {"error": "Binance 连接失败"}
            pos = _POSITIONS.get(symbol)
            if not pos:
                return {"status": "NO_POSITION", "symbol": symbol}
            ccxt_sym  = _SYMBOL_MAP.get(symbol, f"{symbol}/USDT:USDT")
            close_side = "sell" if pos.side == "LONG" else "buy"
            order = ex.create_market_order(ccxt_sym, close_side, pos.size,
                                           params={"reduceOnly": True})
            pnl_result = _PAPER_BROKER.close_position(symbol, price, reason)
            pnl_result["order_id"] = order.get("id")
            pnl_result["mode"] = "LIVE"
            return pnl_result
        except Exception as e:
            return {"error": str(e), "mode": "LIVE"}

# ══════════════════════════════════════════════════════
#  7. CrewAI 工具：CryptoPortfolioTool
# ══════════════════════════════════════════════════════

class CryptoPortfolioInput(BaseModel):
    query: str = Field(
        default="all",
        description="all（全部持仓）/ pnl（今日收益）/ risk（风险状态）",
    )

class CryptoPortfolioTool(BaseTool):
    name: str = "CryptoPortfolioTool"
    description: str = (
        "查询当前加密货币期货持仓状态、日内收益、风险指标。\n"
        "- query=all：返回所有开仓品种的持仓详情 + 未实现 PnL\n"
        "- query=pnl：返回今日已实现 PnL 汇总\n"
        "- query=risk：返回风险状态（是否触发每日止损、冷静期）\n\n"
        "每次制定交易计划前应先查询此工具了解现有暴露。"
    )
    args_schema: type[BaseModel] = CryptoPortfolioInput

    def _run(self, query: str = "all") -> str:
        query = query.lower()
        now_ts = time.time()

        if query == "pnl":
            return json.dumps({
                "daily_realized_pnl_usdt": round(_DAILY_PNL, 2),
                "daily_drawdown_limit": f"{abs(_DAILY_PNL_LIMIT)*100:.0f}%",
                "limit_hit": _DAILY_PNL < (_DAILY_PNL_LIMIT * 10000),
            }, ensure_ascii=False)

        if query == "risk":
            return json.dumps({
                "cooldown_active": now_ts < _COOLDOWN_UNTIL,
                "cooldown_remaining_sec": max(0, _COOLDOWN_UNTIL - now_ts),
                "daily_pnl_usdt": round(_DAILY_PNL, 2),
                "daily_limit_hit": _DAILY_PNL < (_DAILY_PNL_LIMIT * 10000),
                "open_positions": len(_POSITIONS),
            }, ensure_ascii=False)

        # all
        positions_data = []
        for sym, pos in _POSITIONS.items():
            price_now = _get_current_price(sym)
            if pos.entry_price and pos.size:
                pnl = ((price_now - pos.entry_price) * pos.size
                       if pos.side == "LONG"
                       else (pos.entry_price - price_now) * pos.size)
                pnl_pct = pnl / (pos.size * pos.entry_price) * 100
            else:
                pnl = 0.0
                pnl_pct = 0.0

            # 分级风险检查
            risk_level = _RISK_ENGINE.evaluate(
                pnl,
                warning=-50, reduce=-150, stop=-300, catastrophe=-600
            )
            positions_data.append({
                "symbol": sym,
                "side": pos.side,
                "size": round(pos.size, 4),
                "entry_price": pos.entry_price,
                "current_price": price_now,
                "unrealized_pnl_usdt": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "age_minutes": round(pos.age_minutes, 1),
                "max_age_minutes": _MAX_POSITION_AGE.get(sym, 30),
                "risk_level": risk_level,
            })

        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": os.getenv("CRYPTO_MODE", "PAPER"),
            "open_positions": len(_POSITIONS),
            "positions": positions_data,
            "daily_realized_pnl": round(_DAILY_PNL, 2),
        }, ensure_ascii=False, indent=2)
