"""
tools/event_state_machine.py
═══════════════════════════════════════════════════════════════════════════════
InsightBridge — 事件驱动交易状态机（替代 SCAN_INTERVAL 轮询）

来源：两份设计报告（事件驱动高杠杆交易机器人实战改造报告 2026-05-08）

核心问题（旧设计）：
  SCAN_INTERVAL = 60秒 → 60秒才处理一次 → 远超30-180秒观察窗口
  任何模型延迟、阻塞调用、同步I/O都会把"确认窗口"拖死

新设计：事件驱动状态机
  IDLE → OBSERVE → CONFIRM → ENTRY → MANAGE → EXIT

状态转换规则：
  IDLE:    等待A类宏观事件触发
  OBSERVE: 事件发生后进入观察期，收集微观结构信号（不下单）
  CONFIRM: 观察期结束，综合判断是否满足入场条件
  ENTRY:   提交订单，等待ACK确认
  MANAGE:  持仓管理，实时监控退出信号
  EXIT:    执行退出，进入冷却期后回到IDLE

防线设计：
  - 风控检查在每个状态转换前执行（包括IDLE状态）
  - 任何HardStop触发 → 直接FLATTEN跳过正常流程
  - 观察期内不允许开仓（防止假突破第一脚追入）
"""

from __future__ import annotations

import time
import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

try:
    from tools.quant_core import (
        ASSET_CONFIGS, AssetObservationConfig,
        FalseBreakoutDetector, FalseBreakoutResult,
        BOCPDEngine, BOCPDResult,
        AggressionRatioEngine, AggressionSnapshot,
        OBISnapshot,
    )
    from tools.risk_guard import (
        HardStopController, AccountRiskState, HardStopDecision,
    )
    _TOOLS_OK = True
except ImportError:
    try:
        from quant_core import (
            ASSET_CONFIGS, AssetObservationConfig,
            FalseBreakoutDetector, FalseBreakoutResult,
            BOCPDEngine, BOCPDResult,
            AggressionRatioEngine, AggressionSnapshot,
            OBISnapshot,
        )
        from risk_guard import (
            HardStopController, AccountRiskState, HardStopDecision,
        )
        _TOOLS_OK = True
    except ImportError:
        _TOOLS_OK = False


# ══════════════════════════════════════════════════════════════════════════════
#  状态定义
# ══════════════════════════════════════════════════════════════════════════════

class TradeStage(Enum):
    IDLE    = auto()   # 等待事件
    OBSERVE = auto()   # 观察期（绝对不开仓）
    CONFIRM = auto()   # 确认期（综合判断）
    ENTRY   = auto()   # 下单等待ACK
    MANAGE  = auto()   # 持仓管理
    EXIT    = auto()   # 退出中
    COOLDOWN = auto()  # 冷却期（退出后等待）


# ══════════════════════════════════════════════════════════════════════════════
#  市场快照（由数据层提供）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MarketSnapshot:
    ts_ms:              float   # 时间戳（ms）
    price:              float   # 当前价格
    bid_price:          float   # 最优买价
    ask_price:          float   # 最优卖价
    current_spread:     float   # 当前点差
    baseline_spread:    float   # 基线点差（事件前）
    same_side_depth:    float   # 同向五档深度（名义USD）
    baseline_depth:     float   # 基线深度
    feed_lag_ms:        float   # 行情延迟（ms）
    book_desync:        bool    # 订单簿是否失同步
    obi_snap:           Optional[Any] = None    # OBISnapshot
    aggr_snap:          Optional[Any] = None    # AggressionSnapshot


@dataclass
class EventSignal:
    """宏观事件信号（由 EventScanner 提供）。"""
    event_id:        str
    event_type:      str   # "CPI" / "FOMC" / "NFP" / "WAR" / etc.
    tier:            str   # "A" / "B" / "C"（只交易A类）
    direction:       str   # "LONG" / "SHORT" / "WATCH"
    asset_class:     str   # "CRYPTO" / "FX" / "EQUITY_INDEX" / "BOND" / "COMMODITY"
    ts_trigger_ms:   float # 事件触发时间戳
    strength:        float # 0.0~1.0 事件强度估计


@dataclass
class TradeState:
    """当前交易状态快照。"""
    stage:           TradeStage = TradeStage.IDLE
    event:           Optional[EventSignal] = None
    entry_price:     float = 0.0
    position:        int   = 0       # +1=多 -1=空 0=无
    position_pnl:    float = 0.0     # 当前浮盈亏（%）
    entry_ts_ms:     float = 0.0
    observe_start_ms: float = 0.0
    confirm_start_ms: float = 0.0
    cooldown_end_ms: float = 0.0
    observe_ticks:   List[Any] = field(default_factory=list)
    exit_reason:     str = ""


# ══════════════════════════════════════════════════════════════════════════════
#  事件驱动状态机主类
# ══════════════════════════════════════════════════════════════════════════════

class EventStateMachine:
    """
    事件驱动交易状态机。

    设计目标：
    1. 替代 SCAN_INTERVAL 轮询，改为纯事件驱动
    2. 观察期内绝对不开仓（防假突破第一脚）
    3. 风控永远先于策略执行
    4. 所有状态转换均可审计

    与外部的接口：
        on_event(event)         → 宏观事件触发
        on_market(snapshot)     → 市场数据更新（主循环调用）
        on_risk(account_state)  → 账户风险状态更新
        on_fill(price, side)    → 成交回报
        register_broker(broker) → 注入执行接口

    Broker 接口要求（duck typing）：
        broker.open(direction, notional, price) → order_id
        broker.close(position) → None
        broker.reduce(position, pct) → None
    """

    def __init__(
        self,
        asset_class: str = "CRYPTO",
        equity:      float = 100000.0,
        max_loss_pct: float = 0.01,
        cooldown_s:  float = 120.0,    # 退出后冷却时间（秒）
        log_fn:      Optional[Callable] = None,
    ):
        self.asset_class  = asset_class
        self.equity       = equity
        self.cfg          = (ASSET_CONFIGS.get(asset_class, ASSET_CONFIGS["CRYPTO"])
                             if _TOOLS_OK else None)
        self.cooldown_s   = cooldown_s
        self._log         = log_fn or (lambda msg, **kw: print(f"[FSM] {msg}"))

        # 子模块
        self.hard_stop = (HardStopController(
            asset_class=asset_class,
            max_loss_pct=max_loss_pct)
            if _TOOLS_OK else None)

        self.fbd = (FalseBreakoutDetector(cfg=self.cfg)
                    if _TOOLS_OK and self.cfg else None)

        self.bocpd = (BOCPDEngine(
            hazard=1.0/200.0 if asset_class == "CRYPTO" else 1.0/350.0)
            if _TOOLS_OK else None)

        # 状态
        self._state     = TradeState()
        self._broker    = None
        self._account   = AccountRiskState(equity=equity) if _TOOLS_OK else None
        self._audit_log: deque = deque(maxlen=1000)

    # ── 外部接口 ──────────────────────────────────────────────────────────────

    def register_broker(self, broker: Any) -> None:
        """注入 Broker 执行接口。"""
        self._broker = broker

    def on_event(self, event: EventSignal) -> None:
        """宏观事件触发入口（由 EventScanner 调用）。"""
        if event.tier != "A":
            self._log(f"事件{event.event_id}等级{event.tier}，跳过（只交易A类）")
            return
        if self._state.stage != TradeStage.IDLE:
            self._log(f"当前状态{self._state.stage.name}，忽略新事件")
            return

        self._log(f"🔔 A类事件触发: {event.event_type} → 进入观察期")
        self._state.event          = event
        self._state.stage          = TradeStage.OBSERVE
        self._state.observe_start_ms = time.time() * 1000
        self._state.observe_ticks  = []
        self._audit("OBSERVE_START", event_type=event.event_type)

        # 重置BOCPD（新事件开始）
        if self.bocpd:
            self.bocpd.reset()

    def on_market(self, snap: MarketSnapshot) -> None:
        """
        市场数据更新入口（主循环每次收到行情时调用）。
        这是状态机的核心驱动函数。
        """
        now_ms = snap.ts_ms
        state  = self._state

        # ── 0. 风控永远先检查（任何状态都执行）──────────────────────────────
        acct = self._account
        if acct:
            acct.feed_lag_ms  = snap.feed_lag_ms
            acct.book_desync  = snap.book_desync
            if state.position != 0:
                acct.active_position = state.position
                acct.position_pnl_pct = state.position_pnl

        fbs_result   = None
        bocpd_result = None

        # 在持仓阶段更新假突破检测和BOCPD
        if state.stage in (TradeStage.MANAGE, TradeStage.ENTRY):
            if self.fbd and snap.obi_snap and snap.aggr_snap:
                elapsed = (now_ms - state.entry_ts_ms) / 1000.0
                fbs_result = self.fbd.evaluate(
                    obi_snap=snap.obi_snap,
                    aggr_snap=snap.aggr_snap,
                    current_spread=snap.current_spread,
                    baseline_spread=snap.baseline_spread,
                    same_side_depth=snap.same_side_depth,
                    baseline_same_side_depth=snap.baseline_depth,
                    position=state.position,
                    elapsed_s=elapsed,
                    current_r=state.position_pnl / max(snap.baseline_spread, 1e-9),
                )
            if self.bocpd:
                fs = self._calc_fragility(snap)
                bocpd_result = self.bocpd.update(fs)

        if self.hard_stop and acct:
            decision = self.hard_stop.check(acct, fbs_result, bocpd_result)
            if decision.action == "FLATTEN":
                self._force_exit(decision.reason, decision.message, snap)
                return
            elif decision.action == "REDUCE_50":
                if state.position != 0 and self._broker:
                    self._broker.reduce(state.position, 0.5)
                    self._audit("REDUCE_50", reason=decision.reason)

        # ── 1. 状态路由 ──────────────────────────────────────────────────────
        if state.stage == TradeStage.IDLE:
            pass   # 等待 on_event()

        elif state.stage == TradeStage.OBSERVE:
            self._handle_observe(snap, now_ms)

        elif state.stage == TradeStage.CONFIRM:
            self._handle_confirm(snap, now_ms, fbs_result)

        elif state.stage == TradeStage.MANAGE:
            self._handle_manage(snap, now_ms)

        elif state.stage == TradeStage.COOLDOWN:
            if now_ms >= state.cooldown_end_ms:
                self._state.stage = TradeStage.IDLE
                self._log("冷却期结束，回到IDLE")

    def on_fill(self, fill_price: float, side: str) -> None:
        """成交回报（Broker ACK 后调用）。"""
        if self._state.stage != TradeStage.ENTRY:
            return
        self._state.entry_price  = fill_price
        self._state.entry_ts_ms  = time.time() * 1000
        self._state.stage        = TradeStage.MANAGE
        self._log(f"✅ 成交确认: {side}@{fill_price:.4f} → 进入持仓管理")
        self._audit("ENTRY_FILLED", price=fill_price, side=side)

    def on_risk(self, account_state: Any) -> None:
        """账户风险状态更新（从 Broker 获取后调用）。"""
        if self._account and account_state:
            self._account = account_state

    # ── 状态处理函数 ──────────────────────────────────────────────────────────

    def _handle_observe(self, snap: MarketSnapshot, now_ms: float) -> None:
        """观察期处理：收集信号，绝对不开仓。"""
        cfg = self.cfg
        if not cfg:
            return

        state = self._state
        elapsed_s = (now_ms - state.observe_start_ms) / 1000.0

        # 收集观察快照
        state.observe_ticks.append({
            "ts": now_ms,
            "spread_mult": snap.current_spread / max(snap.baseline_spread, 1e-9),
            "obi": getattr(snap.obi_snap, "obi", 0.0) if snap.obi_snap else 0.0,
        })

        # 点差过高 → 继续观察
        spread_mult = snap.current_spread / max(snap.baseline_spread, 1e-9)
        if spread_mult > cfg.spread_block_mult:
            self._log(f"  观察中: 点差{spread_mult:.2f}x过高，等待...")
            return

        # 最短观察期内不进入确认
        if elapsed_s < cfg.obs_min_s:
            return

        # 最长观察期超时 → 放弃本次机会
        if elapsed_s > cfg.obs_max_s:
            self._log(f"⏱ 观察期超时({elapsed_s:.0f}s > {cfg.obs_max_s:.0f}s)，放弃")
            self._to_cooldown("OBSERVE_TIMEOUT")
            return

        # 进入确认期
        self._state.stage          = TradeStage.CONFIRM
        self._state.confirm_start_ms = now_ms
        self._log(f"📊 进入确认期 (观察了{elapsed_s:.0f}s)")
        self._audit("CONFIRM_START", elapsed_s=elapsed_s)

    def _handle_confirm(self, snap: MarketSnapshot, now_ms: float,
                        fbs_result: Any) -> None:
        """确认期：综合检查3/5条件是否满足入场。"""
        cfg   = self.cfg
        event = self._state.event
        if not cfg or not event:
            return

        elapsed_s = (now_ms - self._state.confirm_start_ms) / 1000.0

        # 超时不确认 → 放弃
        if elapsed_s > 30.0:
            self._log("⏱ 确认期超时，放弃本次机会")
            self._to_cooldown("CONFIRM_TIMEOUT")
            return

        obi_snap  = snap.obi_snap
        aggr_snap = snap.aggr_snap
        if not obi_snap or not aggr_snap:
            return

        # 假突破检测
        if fbs_result:
            gate = getattr(fbs_result, "entry_gate", "WAIT")
        else:
            # 简化版：直接用OBI判断
            gate = "ALLOW" if abs(obi_snap.obi) >= cfg.obi_entry else "WAIT"

        if gate == "BLOCK":
            self._log("🚫 确认失败: 微观结构BLOCK")
            self._to_cooldown("CONFIRM_BLOCKED")
        elif gate in ("ALLOW", "ALLOW_REDUCED"):
            direction = event.direction
            size_mult = 1.0 if gate == "ALLOW" else 0.5
            self._state.stage    = TradeStage.ENTRY
            self._state.position = 1 if direction == "LONG" else -1
            self._log(f"✅ 确认通过: {direction}, 仓位倍数{size_mult:.1f} → 下单")
            self._audit("ENTRY_ORDER", direction=direction, gate=gate)
            if self._broker:
                self._broker.open(direction, size_mult, snap.price)
        # gate == "WAIT": 继续等待

    def _handle_manage(self, snap: MarketSnapshot, now_ms: float) -> None:
        """持仓管理：更新浮盈亏。"""
        state = self._state
        if state.position == 0 or state.entry_price == 0:
            return
        price_chg = (snap.price - state.entry_price) / state.entry_price
        state.position_pnl = price_chg * state.position

    # ── 辅助函数 ──────────────────────────────────────────────────────────────

    def _force_exit(self, reason: str, message: str,
                    snap: Optional[MarketSnapshot] = None) -> None:
        """强制平仓并进入冷却期。"""
        if self._state.position != 0 and self._broker:
            self._broker.close(self._state.position)

        self._log(f"🚨 强制平仓: [{reason}] {message}")
        self._audit("FORCE_EXIT", reason=reason, message=message)
        self._to_cooldown(reason)

    def _to_cooldown(self, reason: str) -> None:
        """进入冷却期。"""
        now_ms = time.time() * 1000
        self._state.stage          = TradeStage.COOLDOWN
        self._state.cooldown_end_ms = now_ms + self.cooldown_s * 1000
        self._state.position        = 0
        self._state.exit_reason     = reason
        self._log(f"❄️  冷却期{self.cooldown_s:.0f}s: {reason}")

    def _calc_fragility(self, snap: MarketSnapshot) -> float:
        """计算脆弱度分数（BOCPD输入）。"""
        if snap.baseline_spread <= 0:
            return 0.0
        spread_stress = max(0.0, snap.current_spread / snap.baseline_spread - 1.0)
        depth_stress  = max(0.0, 1.0 - snap.same_side_depth / max(snap.baseline_depth, 1.0))
        lag_stress    = min(1.0, snap.feed_lag_ms / 1000.0)
        return float(0.4 * spread_stress + 0.4 * depth_stress + 0.2 * lag_stress)

    def _audit(self, event: str, **kwargs) -> None:
        """审计日志记录。"""
        entry = {"ts": time.time(), "event": event,
                 "stage": self._state.stage.name, **kwargs}
        self._audit_log.append(entry)

    # ── 状态查询 ──────────────────────────────────────────────────────────────

    @property
    def stage(self) -> TradeStage:
        return self._state.stage

    @property
    def is_in_position(self) -> bool:
        return self._state.position != 0

    def get_audit_log(self) -> List[dict]:
        return list(self._audit_log)

    def status(self) -> dict:
        s = self._state
        return {
            "stage":        s.stage.name,
            "position":     s.position,
            "position_pnl": round(s.position_pnl * 100, 3),
            "entry_price":  s.entry_price,
            "event_type":   s.event.event_type if s.event else None,
            "exit_reason":  s.exit_reason,
        }
