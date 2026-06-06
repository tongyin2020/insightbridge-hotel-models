#!/usr/bin/env python3
"""21-day local hybrid test harness for MARE + DirectorAI.

This script is designed to run on the user's Mac from a Python terminal.
It uses:
- Firecrawl for public web signals
- AgentOps for run monitoring
- Direct Python subprocess calls into the two backend model kernels

It intentionally avoids app auth, local database setup, and frontend coupling.
The goal is pre-pilot model hardening, not production deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlite3
import requests
from dotenv import load_dotenv

# ── 声誉情感引擎 + DSEC市场数据（hotel_collector 同目录）
_SENTIMENT_DIR = Path("/Users/tongyin/Desktop/InsightBridge_模型测试系统/hotel_collector")
if str(_SENTIMENT_DIR) not in sys.path:
    sys.path.insert(0, str(_SENTIMENT_DIR))
try:
    from sentiment_engine import get_reputation_signals as _get_rep_signals
    _SENTIMENT_OK = True
except ImportError:
    _SENTIMENT_OK = False
    def _get_rep_signals(hotel_id, tier, conn=None):
        return {"rep_adj": 0.0}

try:
    from dsec_loader import (
        init_and_seed as _dsec_init_and_seed,
        get_dsec_demand_signal as _dsec_demand_signal,
        get_market_adr as _dsec_market_adr,
        get_calibrated_season_multipliers as _dsec_season_mults,
    )
    _DSEC_OK = True
except ImportError:
    _DSEC_OK = False
    def _dsec_demand_signal(month, star, conn): return 0.0
    def _dsec_market_adr(month, star, conn, year=None): return 0.0
    def _dsec_season_mults(star, conn): return {"peak": 1.20, "shoulder": 1.0, "off_peak": 0.85}
    def _dsec_init_and_seed(conn): return 0

try:
    from elasticity_engine import optimize_price as _elasticity_optimize
    _ELASTICITY_OK = True
except ImportError:
    _ELASTICITY_OK = False
    def _elasticity_optimize(candidate_price, market_price, star, district="NAPE",
                              demand_level="NORMAL", season="normal", hotel_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            optimal_price=candidate_price, predicted_occupancy=0.72,
            predicted_revpar=candidate_price*0.72, baseline_revpar=market_price*0.72,
            true_lift_pct=0.0, elasticity_used=0.0, data_source="unavailable", search_steps=0
        )

# ── 自主获客模型（从 simulation_test 复用）─────────────────────────────────
_SIM_DIR = Path("/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test")
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))
try:
    from run_simulation import run_45star_test as _run_selfacq
    from data_fetchers.scenario_engine import SCENARIOS as _SIM_SCENARIOS
    from hotel_roster_76 import ALL_HOTELS_76 as _HOTELS_76
    _SELFACQ_OK = True
    _ROSTER_OK  = True
except ImportError as _e:
    _SELFACQ_OK = False
    _ROSTER_OK  = False
    def _run_selfacq(hotel, signal, real_data, scenario):
        return {"direct_offer_price": 0, "direct_wins_vs_ota": False, "error": str(_e)}
    _SIM_SCENARIOS = []
    _HOTELS_76     = []


UTC = timezone.utc

# ── 真实数据库路径（hotel_data_collector 写入的数据）
REAL_DB_PATH = Path("/Users/tongyin/Desktop/InsightBridge_模型测试系统/hotel_collector/hotel_real_data.db")

# ── 双层OTA折算系数（OTA价 × 此系数 ≈ 官网BAR）
# 3-4★ 大众市场：OTA溢价约18%，折算系数0.85
# 5★ 奢华市场：澳门五星直订大量补贴，OTA溢价约39%，折算系数0.72
OTA_TO_BAR_MASS    = 0.85   # 3-4★ 大众市场
OTA_TO_BAR_LUXURY  = 0.72   # 5★ 奢华市场
OTA_TO_BAR_RATIO   = OTA_TO_BAR_MASS   # 向后兼容保留

# ── 双层历史BAR vs OTA混合权重
# 大众市场：历史55% + OTA推算45%（对OTA市场波动更敏感）
# 奢华市场：历史70% + OTA推算30%（更多锚定自身历史，减少OTA依赖）
BAR_WEIGHT = {2: 0.55, 3: 0.55, 4: 0.55, 5: 0.70}
OTA_WEIGHT = {2: 0.45, 3: 0.45, 4: 0.45, 5: 0.30}


def _ota_to_bar_ratio(star: int) -> float:
    """按星级返回OTA→BAR折算系数"""
    return OTA_TO_BAR_LUXURY if star == 5 else OTA_TO_BAR_MASS


def compute_dynamic_base_price(hotel_id: str, star: int,
                                ota_snapshot_price: float,
                                month: int = None,
                                tier: str = None) -> float:
    """
    动态计算 base_price（含声誉情感修正）：
      Step A — 历史参考价（四层优先级）：
               层1 Shifter真实BAR(85%) + DSEC(15%) → 混合OTA权重
               层2 Shifter OTA折算BAR(85%) + DSEC(15%) → 混合OTA权重
               层3 冷启动：DSEC统计局(100%)
               层4 完全冷启动兜底：OTA估算×0.97
      Step B — 星级范围截断
      Step C — 声誉情感修正：base × (1 + rep_adj)，其中 rep_adj ∈ [-0.17, +0.17]
      Step D — 库存紧张溢价（avail_level: critical/low/moderate）

    OTA权重按需求档位差异化（淡季不跟价格战，旺季锚定自身BAR）：
      大众(2-4★): LOW=0.40 / NORMAL=0.50 / HIGH=0.25
      豪华(5★):   LOW=0.15 / NORMAL=0.30 / HIGH=0.20

    Args:
        hotel_id: 酒店ID（MAC_5DX_WYNN_001 格式）
        star: 星级（用于范围截断）
        ota_snapshot_price: 当前OTA均价（来自 build_external_snapshot）
        month: 月份(1-12)，None则用当前月
        tier: 星级档次字符串（用于竞对声誉对比），None时自动推导
    """
    if month is None:
        month = datetime.now().month

    # ── 推导 tier ─────────────────────────────────────────────────────────
    if tier is None:
        tier = {3: "3_star", 4: "4_star", 5: "5_star"}.get(star, "3_star")

    # ── 星级差异化OTA折算系数 ────────────────────────────────────────────
    ratio = _ota_to_bar_ratio(star)
    ota_estimate = ota_snapshot_price * ratio
    ota_estimate = max(ota_estimate, 100.0)   # 保底

    # ── 静态基础权重（后续按需求档位覆盖）────────────────────────────────
    w_bar = BAR_WEIGHT.get(star, 0.55)   # 历史BAR权重
    w_ota = OTA_WEIGHT.get(star, 0.45)   # OTA推算权重

    real_bar_avg  = None   # 来自hotel_real_data.db price_snapshots（轨道A：官网BAR）
    real_ota_avg  = None   # 来自hotel_real_data.db price_snapshots（轨道B：OTA竞对）
    dsec_adr_ref  = 0.0
    shared_conn: sqlite3.Connection | None = None

    if REAL_DB_PATH.exists():
        try:
            shared_conn = sqlite3.connect(str(REAL_DB_PATH), timeout=5)

            # ── 层1：Shifter采集的真实官网BAR（最近7天快照，同月份入住日期）
            row = shared_conn.execute("""
                SELECT AVG(official_bar), COUNT(*)
                FROM price_snapshots
                WHERE hotel_id = ?
                  AND official_bar > 200
                  AND source_ok = 1
                  AND CAST(strftime('%m', checkin_date) AS INTEGER) = ?
                  AND snapshot_time >= datetime('now', '-7 days')
            """, (hotel_id, month)).fetchone()
            if row and row[1] and row[1] >= 1:
                real_bar_avg = float(row[0])

            # ── 层2备用：Booking.com OTA竞对价（最近7天）
            row_ota = shared_conn.execute("""
                SELECT AVG(booking_rate), COUNT(*)
                FROM price_snapshots
                WHERE hotel_id = ?
                  AND booking_rate > 200
                  AND CAST(strftime('%m', checkin_date) AS INTEGER) = ?
                  AND snapshot_time >= datetime('now', '-7 days')
            """, (hotel_id, month)).fetchone()
            if row_ota and row_ota[1] and row_ota[1] >= 1:
                real_ota_avg = float(row_ota[0])

        except Exception:
            pass

    if _DSEC_OK and REAL_DB_PATH.exists():
        try:
            _dc = shared_conn or sqlite3.connect(str(REAL_DB_PATH), timeout=5)
            dsec_adr_ref = _dsec_market_adr(month, star, _dc)
            if not shared_conn:
                _dc.close()
        except Exception:
            pass

    # ── 需求档位差异化OTA权重（覆盖静态权重）────────────────────────────────
    # 淡季不跟随OTA价格战；旺季自身BAR主导，OTA权重反而降低
    # 大众(2-4★): LOW→0.40, NORMAL→0.50, HIGH→0.25
    # 豪华(5★):   LOW→0.15, NORMAL→0.30, HIGH→0.20
    _DEMAND_OTA_W = {
        ("mass",   "LOW"):    0.40, ("mass",   "NORMAL"): 0.40, ("mass",   "HIGH"): 0.25,
        ("luxury", "LOW"):    0.15, ("luxury", "NORMAL"): 0.30, ("luxury", "HIGH"): 0.20,
    }
    demand_level = "NORMAL"
    if _DSEC_OK and shared_conn:
        try:
            from dsec_loader import get_dsec_demand_signal as _dsec_sig
            sig = _dsec_sig(month, star, shared_conn)   # [-1, +1]
            demand_level = "HIGH" if sig > 0.15 else ("LOW" if sig < -0.15 else "NORMAL")
        except Exception:
            pass
    seg = "luxury" if star >= 5 else "mass"
    w_ota = _DEMAND_OTA_W.get((seg, demand_level), w_ota)
    w_bar = 1.0 - w_ota

    # ── 四层优先级定价参考（MakCorps已停用）────────────────────────────────
    # 层1：Shifter真实官网BAR → 75%BAR + 25%DSEC背景，再与OTA权重混合
    # 层2：Shifter真实OTA价折算BAR → 85%折算BAR + 15%DSEC，再与OTA权重混合
    # 层3：冷启动 — DSEC统计局100%作为唯一历史参考（MakCorps已停用，不再混合fallback）
    # 层4：完全冷启动兜底（无任何真实数据）
    if real_bar_avg is not None:
        # 层1：有Shifter真实BAR — 75%真实BAR + 25%DSEC市场背景
        historical_ref = (0.75 * real_bar_avg + 0.25 * dsec_adr_ref
                          if dsec_adr_ref > 0 else real_bar_avg)
        base = w_bar * historical_ref + w_ota * ota_estimate
    elif real_ota_avg is not None:
        # 层2：有Shifter OTA价 — 折算BAR：85%折算BAR + 15%DSEC
        ota_bar_est = real_ota_avg * ratio
        historical_ref = (0.75 * ota_bar_est + 0.25 * dsec_adr_ref
                          if dsec_adr_ref > 0 else ota_bar_est)
        base = w_bar * historical_ref + w_ota * ota_estimate
    elif dsec_adr_ref > 0:
        # 层3：冷启动 — DSEC统计局为唯一历史参考，不与MakCorps fallback混合
        base = dsec_adr_ref
    else:
        # 层4：完全冷启动兜底（无真实数据）
        base = ota_estimate * 0.97

    # ── Step B：星级范围截断 ───────────────────────────────────────────────
    clamp_ranges = {
        2: (200, 900), 3: (400, 1400),
        4: (800, 3000), 5: (1500, 8000)
    }
    lo, hi = clamp_ranges.get(star, (200, 8000))
    base = max(lo, min(hi, base))

    # ── Step C：声誉情感修正（review_sentiment + google_ratings 双源）──────
    # rep_adj ∈ [-0.17, +0.17]，由 Wilson 置信压缩后的 ΔR_t + 动量 M_t 合成
    # 冷启动（评论数不足）时 rep_adj = 0；google_rating 作为冷启动补充
    rep_adj = 0.0
    if _SENTIMENT_OK:
        try:
            signals = _get_rep_signals(hotel_id, tier, shared_conn)
            rep_adj = float(signals.get("rep_adj", 0.0))
        except Exception:
            pass

    base = base * (1.0 + rep_adj)

    # ── Step D：OTA库存紧张信号修正（inventory_signals → 需求溢价）──────────
    inv_adj = 0.0
    if shared_conn is not None:
        try:
            today_inv = shared_conn.execute("""
                SELECT avail_level, rooms_remaining
                FROM inventory_signals
                WHERE hotel_id = ?
                  AND captured_at >= datetime('now', '-24 hours')
                ORDER BY captured_at DESC LIMIT 1
            """, (hotel_id,)).fetchone()
            if today_inv:
                if today_inv[0] == "critical":
                    inv_adj = 0.12    # 仅剩1-2间：需求溢价+12%
                elif today_inv[0] == "low":
                    inv_adj = 0.07    # 剩3-9间：需求溢价+7%
                elif today_inv[0] == "moderate":
                    inv_adj = 0.03    # 剩10-19间：温和溢价+3%
        except Exception:
            pass

    if shared_conn is not None:
        try:
            shared_conn.close()
        except Exception:
            pass

    base = base * (1.0 + inv_adj)
    # 修正后再做一次截断（防止声誉+库存放大溢出范围）
    base = max(lo, min(hi, base))

    return round(base / 10) * 10   # 取整到10的倍数


@dataclass
class ExternalSnapshot:
    timestamp_utc: str
    event_density: float
    holiday: float
    weekend: float
    weather: float
    visitors_stats: float
    border_flow: float
    flight_ferry: float
    event_ticket_sales: float
    competitor_price: float
    upper_tier_adr: float
    ota_prices: dict[str, float]
    raw_event_source_ok: bool
    raw_market_source_ok: bool
    # DSEC 澳门统计局真实市场数据
    dsec_market_occ: float = 0.0        # 当月市场平均入住率（来自DSEC，0.0-1.0）
    dsec_demand_signal: float = 0.0     # 需求信号：当月相对多年均值的归一化偏差（-1到+1）
    dsec_cold_adr: dict = None          # 冷启动参考ADR，{3: mop, 4: mop, 5: mop}


@dataclass
class ScenarioDefinition:
    name: str
    category: str
    current_occupancy: float
    remaining_inventory: int
    total_rooms: int
    booking_velocity_24h: float
    days_to_arrival: int
    cancellation_rate: float
    elasticity_signal: float
    competitor_availability: float
    neighborhood_availability: float
    same_day_demand_score: float
    avg_clv: float
    repurchase_probability: float
    price_sensitivity: str
    churn_risk: float
    loyalty_tier: str
    guest_segment: str
    previous_price: float
    avg_30d_price: float
    historical_avg: float
    customer_historical_rate: float
    guest_satisfaction: float
    ota_commission_rate: float
    vip_discount_rate: float
    objective_mode: str


def now_utc() -> datetime:
    return datetime.now(UTC)


def mkdirp(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def init_agentops() -> None:
    key = os.getenv("AGENTOPS_API_KEY", "").strip()
    if not key:
        return
    try:
        import agentops

        agentops.init(api_key=key, default_tags=["hotel-model-staging", "pre-pilot"])
    except Exception as exc:
        print(f"[warn] AgentOps init failed: {exc}", file=sys.stderr)


def firecrawl_event_snapshot() -> tuple[bool, str]:
    # 1. 优先读 Monitor webhook 缓存（每2小时自动更新，节省积分）
    try:
        cache_resp = requests.get(
            "https://intelligence.insightbridge.global/api/monitor/latest/event_density",
            timeout=5
        )
        if cache_resp.status_code == 200:
            d = cache_resp.json()
            if d.get("signal") is not None and not d.get("stale", True):
                # Reconstruct markdown-like string from signal for score_event_markdown
                sig = d["signal"]
                raw = d.get("raw", "")
                # Build synthetic markdown to pass through score_event_markdown
                major_count = int(sig / 0.15) if sig > 0 else 0
                fake_md = " ".join(["Major Event"] * major_count) + " " + raw
                return True, fake_md
    except Exception:
        pass

    # 2. Fallback: direct Firecrawl API call
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return False, ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "url": "https://www.macaotourism.gov.mo/en/events/calendar",
                "formats": ["markdown"],
            },
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        markdown = (data.get("data") or {}).get("markdown", "")
        return bool(data.get("success")) and bool(markdown), markdown
    except Exception:
        return False, ""


def score_event_markdown(markdown: str) -> tuple[float, float]:
    if not markdown:
        return 0.0, 0.0
    major_hits = len(re.findall(r"\bMajor Event\b|Grand Prix|Fireworks|Dragon Boat|Chinese New Year", markdown, flags=re.I))
    holiday_hits = len(re.findall(r"\bPublic Holiday\b|National Day|Labour Day|Mid-Autumn|Chinese New Year", markdown, flags=re.I))
    event_density = min(1.0, 0.15 * major_hits + 0.05 * holiday_hits)
    event_ticket_sales = min(1.0, 0.12 * major_hits + 0.03 * holiday_hits)
    return round(event_density, 3), round(event_ticket_sales, 3)


def makcorps_market_snapshot() -> tuple[bool, dict[str, float], float, float]:
    """
    查询 MakCorps 多OTA价格对比。
    ⚠️ MakCorps订阅已到期/即将到期，停止API调用，直接返回fallback。
    fallback价格来自澳门DSEC统计局历史均价（已在build_external_snapshot中处理）。
    """
    # MakCorps订阅到期，不再调用API — fallback由 build_external_snapshot 处理
    # 清理(2026-06-01): 删除了63行死代码（MakCorps API调用逻辑），防止变量遮蔽和维护混乱
    return False, {}, 0.0, 0.0


def _fc_search_signal(query: str, wan_baseline: float = 20.0, wan_range: float = 15.0) -> tuple:
    """通用 Firecrawl 搜索信号提取，返回 (signal, source, raw)。"""
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return 0.0, "no_key", ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "limit": 5},
            timeout=20,
        )
        if resp.status_code != 200:
            return 0.0, "fc_error", ""
        items = (resp.json().get("data") or [])
        texts = " ".join(str(it.get("description") or it.get("markdown") or "") for it in items)
        nums = re.findall(r'(\d+(?:\.\d+)?)\s*[萬万]', texts)
        if nums:
            val = float(nums[0])
            sig = round(max(-1.0, min(1.0, (val - wan_baseline) / wan_range)), 3)
            return sig, "firecrawl_search", f"{val}万"
        pos = sum(1 for k in ["上升","增加","爆满","高峰","创新高"] if k in texts)
        neg = sum(1 for k in ["下降","减少","冷清","低迷"] if k in texts)
        if pos > neg: return 0.3, "fc_sentiment_pos", f"pos:{pos}"
        if neg > pos: return -0.2, "fc_sentiment_neg", f"neg:{neg}"
        return 0.0, "fc_no_number", ""
    except Exception as e:
        return 0.0, "fc_exception", str(e)[:40]


def _fc_scrape_signal(url: str) -> tuple:
    """Firecrawl scrape 单页，返回 (markdown, source)。"""
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return "", "no_key"
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=30,
        )
        if resp.status_code == 200:
            md = (resp.json().get("data") or {}).get("markdown", "")
            return md, "firecrawl_scrape"
        return "", f"http_{resp.status_code}"
    except Exception as e:
        return "", f"exception:{str(e)[:30]}"


def build_external_snapshot(ts: datetime) -> ExternalSnapshot:
    event_ok, markdown = firecrawl_event_snapshot()
    event_density, event_ticket_sales = score_event_markdown(markdown)
    market_ok, ota_prices, competitor_price, upper_tier_adr = makcorps_market_snapshot()

    weekend = 1.0 if ts.weekday() >= 5 else 0.0
    holiday = 0.7 if event_density >= 0.45 else 0.1

    # ── Firecrawl: border_flow (口岸客流) ──────────────────────────────────
    today = ts.strftime("%Y年%m月%d日")
    checkin = (ts + timedelta(days=1)).strftime("%Y-%m-%d")
    checkout = (ts + timedelta(days=2)).strftime("%Y-%m-%d")

    fc_border, fc_border_src, _ = _fc_search_signal(
        f"澳门口岸 过境 旅客 {today} 人数 统计", wan_baseline=20.0, wan_range=15.0)
    if fc_border_src not in ("no_key", "fc_error", "fc_exception", "fc_no_number"):
        border_flow = round(max(-1.0, min(1.0, fc_border)), 3)
    else:
        # 公式 fallback
        border_flow = round(min(1.0, 0.35 + weekend * 0.15 + event_density * 0.3), 3)

    # ── Firecrawl: zhuhai_saturation (珠海酒店饱和度) ──────────────────────
    fc_zhuhai, fc_zhuhai_src, _ = _fc_search_signal(
        f"珠海酒店 {checkin} 价格 今晚 预订", wan_baseline=0.0, wan_range=1.0)
    if fc_zhuhai_src not in ("no_key", "fc_error", "fc_exception"):
        zhuhai_sat = round(max(0.0, min(1.0, 0.35 + fc_zhuhai * 0.2)), 3)
    else:
        zhuhai_sat = round(0.3 + event_density * 0.15 + weekend * 0.1, 3)

    # ── Firecrawl: ota_booking_pace (Booking.com澳门可用房) ────────────────
    bcom_url = (f"https://www.booking.com/searchresults.html"
                f"?ss=Macau&checkin={checkin}&checkout={checkout}"
                f"&group_adults=2&no_rooms=1&sb=1")
    bcom_md, bcom_src = _fc_scrape_signal(bcom_url)
    if bcom_md and bcom_src == "firecrawl_scrape":
        avail_count = len(re.findall(r'MOP\s*[\d,]+', bcom_md))
        ota_pace = round(min(1.0, max(0.0, 1.0 - avail_count / 50.0)), 3)
    else:
        ota_pace = round(0.35 + event_density * 0.25 + weekend * 0.1, 3)

    # ── 其他信号 ────────────────────────────────────────────────────────────
    visitors_stats = round(min(1.0, 0.3 + event_density * 0.5), 3)
    flight_ferry = round(min(1.0, 0.25 + event_density * 0.35), 3)
    weather = 0.0

    if not market_ok:
        # 澳门4-5星酒店MOP均价 fallback（换算自USD×8：$142→1136, $163→1304）
        ota_prices = {"booking_com": 1136.0, "trip_com": 1160.0, "agoda": 1120.0}
        competitor_price = 1120.0   # MOP
        upper_tier_adr  = 1304.0   # MOP

    # MakCorps返回USD时转换为MOP（Director模型期望MOP；1 USD ≈ 8.06 MOP）
    if market_ok and competitor_price < 500:   # 500以下认为是USD价格
        USD_TO_MOP = 8.06
        competitor_price = round(competitor_price * USD_TO_MOP, 1)
        upper_tier_adr   = round(upper_tier_adr   * USD_TO_MOP, 1)
        ota_prices = {k: round(v * USD_TO_MOP, 1) for k, v in ota_prices.items()}

    # ── DSEC 澳门统计局市场数据 ──────────────────────────────────────────────
    # 按当前月份读取3/4/5★市场入住率和ADR（用最近3年2023-2025均值）
    month = ts.month
    dsec_market_occ = 0.0
    dsec_demand_signal = 0.0
    dsec_cold_adr = {3: 0.0, 4: 0.0, 5: 0.0}
    if _DSEC_OK and REAL_DB_PATH.exists():
        try:
            _dconn = sqlite3.connect(str(REAL_DB_PATH), timeout=5)
            # 用4★市场作为整体代表（4★介于大众和豪华之间）
            dsec_market_occ = _dsec_demand_signal(month, 4, _dconn)  # returns [-1,1]
            # 各星级需求信号（3★代表大众市场，5★代表豪华市场）
            dsec_demand_signal = round(
                0.4 * _dsec_demand_signal(month, 3, _dconn) +
                0.3 * _dsec_demand_signal(month, 4, _dconn) +
                0.3 * _dsec_demand_signal(month, 5, _dconn), 4
            )
            # 冷启动ADR参考：取最近可用年份的当月均价
            for star in (3, 4, 5):
                adr = _dsec_market_adr(month, star, _dconn)
                if adr > 0:
                    dsec_cold_adr[star] = round(adr, 0)
            _dconn.close()
        except Exception:
            pass

    # ── 修正(2026-06-01)：用DSEC星级专属ADR覆盖OTA混合fallback ─────────────
    # MakCorps停用后 competitor_price=1120（所有星级混合均值）→ 3★市场基准虚高
    # DSEC数据已按星级加载，用其覆盖，保留5%市场溢价（模拟市场而非历史均值）
    if not market_ok and dsec_cold_adr.get(3, 0) > 0:
        competitor_price = round(dsec_cold_adr[3] * 1.05, 0)   # 3★专属：DSEC ADR + 5%溢价
        upper_tier_adr   = round(dsec_cold_adr.get(5, 0) * 1.05 or 1304.0, 0)  # 5★专属
        ota_prices = {
            "booking_com": competitor_price,
            "trip_com":    round(competitor_price * 1.04, 0),
            "agoda":       round(competitor_price * 1.02, 0),
        }

    return ExternalSnapshot(
        timestamp_utc=ts.isoformat(),
        event_density=event_density,
        holiday=holiday,
        weekend=weekend,
        weather=weather,
        visitors_stats=visitors_stats,
        border_flow=border_flow,
        flight_ferry=flight_ferry,
        event_ticket_sales=event_ticket_sales,
        competitor_price=competitor_price,
        upper_tier_adr=upper_tier_adr,
        ota_prices=ota_prices,
        raw_event_source_ok=event_ok,
        raw_market_source_ok=market_ok,
        dsec_market_occ=dsec_market_occ,
        dsec_demand_signal=dsec_demand_signal,
        dsec_cold_adr=dsec_cold_adr,
    )


def scenario_catalog(base_price: float) -> list[ScenarioDefinition]:
    return [
        ScenarioDefinition("normal_weekday", "normal", 0.68, 120, 380, 18, 10, 0.10, 0.00, 0.45, 0.50, 0.42, 2800, 0.40, "medium", 0.20, "", "new", base_price * 0.99, base_price * 0.98, base_price * 0.97, base_price * 0.95, 4.2, 0.18, 0.10, "maximize_revenue"),
        ScenarioDefinition("weekend_pickup", "normal", 0.76, 85, 380, 26, 7, 0.09, 0.05, 0.40, 0.42, 0.60, 3200, 0.48, "medium", 0.18, "gold", "returning", base_price, base_price * 1.00, base_price * 0.99, base_price * 0.97, 4.3, 0.18, 0.10, "maximize_revenue"),
        ScenarioDefinition("festival_surge", "extreme", 0.93, 22, 380, 45, 2, 0.06, 0.12, 0.22, 0.18, 0.92, 5500, 0.62, "low", 0.10, "vip", "ota", base_price * 1.05, base_price * 1.04, base_price * 1.02, base_price * 0.98, 4.4, 0.20, 0.08, "maximize_profit"),
        ScenarioDefinition("soft_demand", "normal", 0.48, 210, 380, 8, 18, 0.16, -0.08, 0.62, 0.70, 0.25, 2200, 0.30, "high", 0.28, "", "new", base_price * 0.96, base_price * 0.97, base_price * 0.98, base_price * 0.94, 4.1, 0.18, 0.12, "maximize_revpar"),
        ScenarioDefinition("competitor_pressure", "adversarial", 0.61, 140, 380, 14, 9, 0.11, -0.03, 0.90, 0.64, 0.40, 2600, 0.35, "high", 0.22, "", "ota", base_price * 0.98, base_price * 0.99, base_price * 0.98, base_price * 0.95, 4.0, 0.18, 0.10, "maximize_revenue"),
        ScenarioDefinition("high_inventory", "normal", 0.44, 250, 380, 6, 4, 0.18, -0.10, 0.58, 0.76, 0.22, 2400, 0.32, "high", 0.25, "", "walk_in", base_price * 0.95, base_price * 0.96, base_price * 0.97, base_price * 0.93, 4.0, 0.17, 0.10, "maximize_revpar"),
        ScenarioDefinition("near_sellout", "extreme", 0.97, 6, 380, 52, 1, 0.04, 0.15, 0.15, 0.12, 0.98, 6800, 0.70, "low", 0.08, "platinum", "corporate", base_price * 1.08, base_price * 1.05, base_price * 1.03, base_price * 0.99, 4.5, 0.20, 0.06, "maximize_profit"),
        ScenarioDefinition("fairness_stress", "adversarial", 0.74, 95, 380, 24, 6, 0.09, 0.06, 0.38, 0.34, 0.66, 8000, 0.65, "medium", 0.20, "diamond", "returning", base_price * 0.92, base_price * 0.90, base_price * 0.88, base_price * 0.84, 4.2, 0.18, 0.08, "maximize_profit"),
        ScenarioDefinition("low_satisfaction_conflict", "adversarial", 0.86, 48, 380, 34, 3, 0.08, 0.08, 0.28, 0.24, 0.85, 4200, 0.44, "low", 0.22, "gold", "new", base_price * 1.01, base_price * 1.00, base_price * 0.99, base_price * 0.96, 3.1, 0.19, 0.09, "maximize_revenue"),
        ScenarioDefinition("dirty_data", "adversarial", 0.58, 999, 380, -5, 0, 0.55, 1.80, 1.40, -0.25, -0.10, -100, 1.40, "very_low", 1.20, "vip", "ota", 0, 0, 0, 0, 2.9, 0.30, 0.20, "maximize_direct_mix"),
        ScenarioDefinition("signal_conflict", "adversarial", 0.88, 30, 380, 12, 5, 0.22, -0.04, 0.80, 0.20, 0.30, 3600, 0.38, "high", 0.35, "", "new", base_price * 1.00, base_price * 1.01, base_price * 1.00, base_price * 0.97, 3.6, 0.20, 0.12, "maximize_direct_mix"),
    ]


def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def _tier_guardrails(base_price: float, star: int) -> tuple[float, float]:
    """按星级计算合理的 floor/ceiling，避免硬编码 750/1015 导致低端酒店价格被截断。"""
    # 以 base_price 为锚点，星级越高弹性越大
    ratios = {
        3: (0.83, 1.42),
        4: (0.80, 1.55),
        5: (0.75, 1.65),
    }
    lo, hi = ratios.get(star, (0.80, 1.45))
    return round(base_price * lo, 0), round(base_price * hi, 0)


def build_payload(snapshot: ExternalSnapshot, scenario: ScenarioDefinition, hotel_id: str, base_price: float, market_segment: str | None, star: int = 3) -> dict[str, Any]:
    # 修正(2026-06-01)：优先用DSEC星级专属ADR作为竞对基准价
    # snapshot.competitor_price 在MakCorps停用后=3★DSEC×1.05（已修正）
    # 但4★/5★仍需从dsec_cold_adr取各自的专属值，避免用3★价作为高端酒店基准
    _dsec_adr = (snapshot.dsec_cold_adr or {}).get(star, 0)
    competitor_price = round(_dsec_adr * 1.05, 0) if _dsec_adr > 0 else snapshot.competitor_price
    if scenario.name == "competitor_pressure":
        competitor_price = round(competitor_price * 0.90, 0)
    elif scenario.name == "near_sellout":
        competitor_price = round(competitor_price * 1.08, 0)
    elif scenario.name == "dirty_data":
        competitor_price = -50.0

    payload = {
        "hotel_id": hotel_id,
        "market_segment": market_segment,
        "base_price": round(base_price, 2),
        "season": "shoulder" if snapshot.event_density < 0.45 else "peak",
        "current_occupancy": scenario.current_occupancy,
        "competitor_price": round(competitor_price, 2),
        "competitor_availability": scenario.competitor_availability,
        "elasticity_signal": scenario.elasticity_signal,
        "holiday": snapshot.holiday,
        "event_ticket_sales": snapshot.event_ticket_sales,
        "weekend": snapshot.weekend,
        "border_flow": snapshot.border_flow,
        "visitors_stats": snapshot.visitors_stats,
        "flight_ferry": snapshot.flight_ferry,
        "zhuhai_saturation": 0.25 if scenario.category != "adversarial" else 0.65,
        "ota_booking_pace": 0.52 if scenario.category != "adversarial" else 0.18,
        "weather": snapshot.weather,
        "remaining_inventory": scenario.remaining_inventory,
        "total_rooms": scenario.total_rooms,
        "booking_velocity_24h": scenario.booking_velocity_24h,
        "days_to_arrival": scenario.days_to_arrival,
        "cancellation_rate": scenario.cancellation_rate,
        "guest_segment": scenario.guest_segment,
        "avg_clv": max(scenario.avg_clv, 0),
        "repurchase_probability": clamp(scenario.repurchase_probability, 0.0, 1.0),
        "price_sensitivity": scenario.price_sensitivity,
        "churn_risk": clamp(scenario.churn_risk, 0.0, 1.0),
        "loyalty_tier": scenario.loyalty_tier,
        "previous_price": max(scenario.previous_price, 0),
        "avg_30d_price": max(scenario.avg_30d_price, 0),
        "historical_avg": max(scenario.historical_avg, 0),
        "max_deviation_pct": 20.0,
        "customer_historical_rate": max(scenario.customer_historical_rate, 0),
        "upper_tier_adr": snapshot.upper_tier_adr,
        "neighborhood_availability": scenario.neighborhood_availability,
        "same_day_demand_score": scenario.same_day_demand_score,
        "event_density": snapshot.event_density,
        "ota_prices": snapshot.ota_prices,
        "ota_commission_rate": scenario.ota_commission_rate,
        "vip_discount_rate": scenario.vip_discount_rate,
        "guest_satisfaction": scenario.guest_satisfaction,
        "data_freshness_minutes": 15.0,
        # DSEC 澳门统计局需求信号（硬核市场数据，归一化到 -1~+1）
        "dsec_market_occ": round(snapshot.dsec_market_occ, 4),
    }
    # 按星级注入合理的 floor/ceiling（替代 pricing_engine 的硬编码 750/1015）
    floor_p, ceil_p = _tier_guardrails(base_price, star)
    payload["floor_price"] = floor_p
    payload["ceiling_price"] = ceil_p
    return payload


def run_python_snippet(cwd: Path, pythonpath: Path, script: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(pythonpath)
    proc = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        return False, {
            "returncode": proc.returncode,
            "stderr": proc.stderr[-4000:],
            "stdout": proc.stdout[-2000:],
        }
    try:
        return True, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, {"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


def run_mare(repo_path: Path, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    script = r"""
import json, sys
from types import SimpleNamespace
from app.services.pricing_engine import recommend
payload = json.loads(sys.argv[1])
data = SimpleNamespace(**payload)
# 用 payload 里的 floor/ceiling 创建 hotel_settings，不再传 None
hotel_settings = SimpleNamespace(
    floor_price=float(payload.get("floor_price", 750)),
    ceiling_price=float(payload.get("ceiling_price", 1015)),
)
result = recommend(data, hotel_settings)
print(json.dumps(result))
"""
    return run_python_snippet(repo_path / "api", repo_path / "api", script, payload)


def run_director(repo_path: Path, payload: dict[str, Any], objective_mode: str) -> tuple[bool, dict[str, Any]]:
    script = r"""
import json, sys
from types import SimpleNamespace
from app.core.pricing_engine import recommend
payload = json.loads(sys.argv[1])
objective_mode = payload.pop("_objective_mode", "maximize_revenue")
hotel_settings = SimpleNamespace(
    floor_price=float(payload.get("floor_price", 750)),
    ceiling_price=float(payload.get("ceiling_price", 1015)),
)
result = recommend(payload, hotel_settings, objective_mode=objective_mode)
print(json.dumps(result))
"""
    payload = dict(payload)
    payload["_objective_mode"] = objective_mode
    return run_python_snippet(repo_path / "backend", repo_path / "backend", script, payload)


def evaluate_result(name: str, result: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    price = result.get("recommended_price")
    if not isinstance(price, (int, float)):
        issues.append("missing_recommended_price")
        return issues
    if price <= 0:
        issues.append("non_positive_price")
    if price > 100000:
        issues.append("implausibly_high_price")
    report = result.get("guardrail_report") or {}
    final_price = report.get("final_price")
    if isinstance(final_price, (int, float)) and final_price <= 0:
        issues.append("invalid_guardrail_final_price")
    if name == "director":
        cp = result.get("channel_pricing") or {}
        ota = cp.get("ota_price")
        direct = cp.get("direct_price")
        vip = cp.get("vip_price")
        if all(isinstance(x, (int, float)) for x in (ota, direct, vip)):
            if not (ota > direct >= vip):
                issues.append("channel_hierarchy_broken")
    return issues


def write_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 21-day hybrid model staging tests.")
    parser.add_argument("--days", type=int, default=int(os.getenv("RUN_DAYS", "21")))
    parser.add_argument("--interval-seconds", type=int, default=int(os.getenv("RUN_INTERVAL_SECONDS", "3600")))
    parser.add_argument("--cycles", type=int, default=0, help="Override days/interval and run a fixed number of cycles.")
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "./hotel_model_staging_output"))
    parser.add_argument("--base-price-mare", type=float, default=820.0)
    parser.add_argument("--base-price-director", type=float, default=1541.0)
    parser.add_argument("--mare-hotel-id", default="macau_midscale")
    parser.add_argument("--director-hotel-id", default="macau_luxury_direct")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    init_agentops()
    args = parse_args()

    mare_repo = Path(os.getenv("MARE_REPO_PATH", "")).expanduser()
    director_repo = Path(os.getenv("DIRECTOR_REPO_PATH", "")).expanduser()
    if not mare_repo.exists():
        print(f"[fatal] MARE_REPO_PATH not found: {mare_repo}", file=sys.stderr)
        return 2
    if not director_repo.exists():
        print(f"[fatal] DIRECTOR_REPO_PATH not found: {director_repo}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve()
    mkdirp(output_dir)
    run_id = now_utc().strftime("%Y%m%dT%H%M%SZ")
    log_path = output_dir / f"run_{run_id}.jsonl"
    summary_path = output_dir / f"summary_{run_id}.json"

    total_cycles = args.cycles if args.cycles > 0 else max(1, int((args.days * 86400) / max(args.interval_seconds, 1)))
    scenarios_mare = scenario_catalog(args.base_price_mare)
    scenarios_director = scenario_catalog(args.base_price_director)

    # ── 澳门旅游局官方76家真实酒店名单 ──────────────────────────────────────
    if _ROSTER_OK and _HOTELS_76:
        ALL_HOTELS_GPT = _HOTELS_76          # 76家官方真实酒店
    else:
        # 降级：保留原虚构名单（仅当hotel_roster_76导入失败时）
        import random as _rnd
        _rng = _rnd.Random(2026)
        _spec_3 = [
            ("TAIPA","氹仔",3,25,580,950,80,220),("NAPE","新口岸",3,20,620,980,90,250),
            ("INNER","内港",3,18,560,900,75,200),("COT","路凼",3,10,700,1050,100,280),
        ]
        _spec_45 = [
            ("COTAI","路凼",5,60,2200,5500,300,3000),("COTAI","路凼",4,50,1100,2100,200,800),
            ("NAPE","新口岸",5,35,1800,3500,200,600),("NAPE","新口岸",4,40,1000,1900,150,500),
            ("TAIPA","氹仔",4,30,1000,1800,150,450),("TAIPA","氹仔",5,25,1600,3000,200,700),
            ("HIST","历史城区",4,20,950,1700,120,400),("HIST","历史城区",5,15,1500,2800,150,500),
            ("COL","路环",5,5,2000,4000,100,300),
        ]
        ALL_HOTELS_GPT = []
        for dc,dcn,star,cnt,plo,phi,rlo,rhi in _spec_3 + _spec_45:
            for i in range(1, cnt+1):
                ALL_HOTELS_GPT.append({
                    "hotel_id": f"MAC_{star}S_{dc}_{i:03d}",
                    "star": star,
                    "base_price": float(round(_rng.uniform(plo, phi) / (10 if star<=3 else 50)) * (10 if star<=3 else 50)),
                    "total_rooms": _rng.randint(rlo, rhi),
                    "market_segment": "macau_luxury_direct" if star >= 4 else "macau_3star_plus",
                })
    print(f"[harness] 酒店名单: {len(ALL_HOTELS_GPT)}家 ({'澳门旅游局官方76家' if _ROSTER_OK else '虚构425家(降级)'})")

    global_counts = {
        "cycles_completed": 0,
        "mare_runs": 0,
        "director_runs": 0,
        "selfacq_runs": 0,
        "mare_failures": 0,
        "director_failures": 0,
        "selfacq_failures": 0,
        "issue_counts": {},
    }

    for cycle in range(total_cycles):
        ts = now_utc()
        snapshot = build_external_snapshot(ts)

        # ── MARE：对全部76家酒店 × 所有场景 ─────────────────────────────
        for hotel in ALL_HOTELS_GPT:
            # 动态计算 base_price：历史BAR(60%) + OTA推算(40%) + 声誉修正
            # 有真实数据时自动切换，冷启动时用OTA估算；声誉冷启动时 rep_adj=0
            # 修正(2026-06-01)：优先使用DSEC星级专属ADR×1.05作为compute_dynamic_base_price的OTA参考
            # 避免3★酒店使用混合OTA均价1120（含4-5★权重）→ 推高base_price → 推高弹性搜索上限
            _dsec_ref = (snapshot.dsec_cold_adr or {}).get(hotel["star"], 0)
            ota_ref = round(_dsec_ref * 1.05, 0) if _dsec_ref > 0 else (
                snapshot.competitor_price if hotel["star"] <= 3 else snapshot.upper_tier_adr
            )
            _tier = {3: "3_star", 4: "4_star", 5: "5_star"}.get(hotel["star"], "3_star")
            base = compute_dynamic_base_price(
                hotel_id=hotel["hotel_id"],
                star=hotel["star"],
                ota_snapshot_price=max(ota_ref, 1.0),
                month=ts.month,
                tier=_tier,
            )
            for scenario in scenarios_mare:
                sc = scenario  # each hotel gets all scenarios
                payload = build_payload(snapshot, sc, hotel["hotel_id"], base, None, star=hotel["star"])
                # 4-5星：将竞对价基准换成upper_tier_adr，保留场景内的相对调整比例
                if hotel["star"] >= 4 and snapshot.competitor_price and snapshot.competitor_price > 0:
                    ratio = payload["competitor_price"] / snapshot.competitor_price
                    payload["competitor_price"] = round(snapshot.upper_tier_adr * ratio, 2)
                payload["total_rooms"] = hotel["total_rooms"]
                ok, result = run_mare(mare_repo, payload)
                global_counts["mare_runs"] += 1
                issues = [] if not ok else evaluate_result("mare", result)
                if not ok or issues:
                    global_counts["mare_failures"] += 1
                for issue in issues:
                    global_counts["issue_counts"][issue] = global_counts["issue_counts"].get(issue, 0) + 1

                # ── Phase 2：弹性引擎 RevPAR 最优化 ──────────────────────────
                if _ELASTICITY_OK and ok and result.get("recommended_price", 0) > 0:
                    # 修正(2026-06-01)：使用 base（DSEC/历史-based，星级专属）作为市场基准
                    # snapshot.competitor_price 是 makcorps 混合价，3★场景下=1120 MOP
                    # 会导致弹性引擎搜索上限=1600，推出不合理的高价
                    # base 已经是该酒店 DSEC+历史BAR 的加权均价，更能代表真实市场定位
                    mkt_price = float(base if base and base > 0 else snapshot.competitor_price)
                    _occ = sc.current_occupancy   # occupancy comes from scenario, not snapshot
                    er = _elasticity_optimize(
                        candidate_price = result["recommended_price"],
                        market_price    = mkt_price,
                        star            = hotel["star"],
                        district        = hotel.get("district", "NAPE"),
                        demand_level    = ("HIGH" if _occ > 0.80
                                           else "LOW" if _occ < 0.55
                                           else "NORMAL"),
                        season          = ("peak" if snapshot.holiday > 0 else "normal"),
                        hotel_id        = hotel["hotel_id"],
                    )
                    result["recommended_price"]   = er.optimal_price
                    result["predicted_occupancy"] = er.predicted_occupancy
                    result["predicted_revpar"]    = er.predicted_revpar
                    result["expected_revenue_lift"] = f"+{er.true_lift_pct:.1f}%"
                    result["elasticity_used"]     = er.elasticity_used
                    result["elasticity_source"]   = er.data_source

                write_jsonl(log_path, {
                    "timestamp_utc": ts.isoformat(),
                    "cycle": cycle + 1,
                    "model": "mare",
                    "hotel_id": hotel["hotel_id"],
                    "hotel_star": hotel["star"],
                    "scenario": asdict(sc),
                    "external_snapshot": asdict(snapshot),
                    "ok": ok,
                    "issues": issues,
                    "result": result,
                })

        # ── Director：对全部76家酒店 × 所有场景 ─────────────────────────
        for hotel in ALL_HOTELS_GPT:
            # 修正(2026-06-01)：优先使用DSEC星级专属ADR×1.05作为compute_dynamic_base_price的OTA参考
            # 避免3★酒店使用混合OTA均价1120（含4-5★权重）→ 推高base_price → 推高弹性搜索上限
            _dsec_ref = (snapshot.dsec_cold_adr or {}).get(hotel["star"], 0)
            ota_ref = round(_dsec_ref * 1.05, 0) if _dsec_ref > 0 else (
                snapshot.competitor_price if hotel["star"] <= 3 else snapshot.upper_tier_adr
            )
            _tier = {3: "3_star", 4: "4_star", 5: "5_star"}.get(hotel["star"], "3_star")
            base = compute_dynamic_base_price(
                hotel_id=hotel["hotel_id"],
                star=hotel["star"],
                ota_snapshot_price=max(ota_ref, 1.0),
                month=ts.month,
                tier=_tier,
            )
            for scenario in scenarios_director:
                payload = build_payload(snapshot, scenario, hotel["hotel_id"], base,
                                        hotel["market_segment"], star=hotel["star"])
                # 4-5星：将竞对价基准换成upper_tier_adr，保留场景内的相对调整比例
                if hotel["star"] >= 4 and snapshot.competitor_price and snapshot.competitor_price > 0:
                    ratio = payload["competitor_price"] / snapshot.competitor_price
                    payload["competitor_price"] = round(snapshot.upper_tier_adr * ratio, 2)
                payload["total_rooms"] = hotel["total_rooms"]
                ok, result = run_director(director_repo, payload, scenario.objective_mode)
                global_counts["director_runs"] += 1
                issues = [] if not ok else evaluate_result("director", result)
                if not ok or issues:
                    global_counts["director_failures"] += 1
                for issue in issues:
                    global_counts["issue_counts"][issue] = global_counts["issue_counts"].get(issue, 0) + 1
                write_jsonl(log_path, {
                    "timestamp_utc": ts.isoformat(),
                    "cycle": cycle + 1,
                    "model": "director",
                    "hotel_id": hotel["hotel_id"],
                    "hotel_star": hotel["star"],
                    "scenario": asdict(scenario),
                    "external_snapshot": asdict(snapshot),
                    "ok": ok,
                    "issues": issues,
                    "result": result,
                })

        # ── 自主获客集成模型（SELFACQ）：全部76家酒店 × 14标准场景 ────────────
        if _SELFACQ_OK and _SIM_SCENARIOS:
            # 将 ExternalSnapshot 转换为 run_45star_test 所需格式
            _signal = {
                "is_holiday": snapshot.holiday > 0.3,
                "is_weekend": snapshot.weekend > 0.3,
                "weather_celsius": 27,
                "season": "shoulder",
                "dsec_market_occ": snapshot.dsec_market_occ,
            }
            _real_data = {
                "upper_tier_adr_real": snapshot.upper_tier_adr or 0.0,
                "booking_prices_3": [],
            }
            for hotel in ALL_HOTELS_GPT:
                # 修正(2026-06-01)：优先使用DSEC星级专属ADR×1.05作为compute_dynamic_base_price的OTA参考
                # 避免3★酒店使用混合OTA均价1120（含4-5★权重）→ 推高base_price → 推高弹性搜索上限
                _dsec_ref = (snapshot.dsec_cold_adr or {}).get(hotel["star"], 0)
                ota_ref = round(_dsec_ref * 1.05, 0) if _dsec_ref > 0 else (
                    snapshot.competitor_price if hotel["star"] <= 3 else snapshot.upper_tier_adr
                )
                _tier = {3: "3_star", 4: "4_star", 5: "5_star"}.get(hotel["star"], "3_star")
                base = compute_dynamic_base_price(
                    hotel_id=hotel["hotel_id"],
                    star=hotel["star"],
                    ota_snapshot_price=max(ota_ref, 1.0),
                    month=ts.month,
                    tier=_tier,
                )
                hotel_with_base = dict(hotel)
                hotel_with_base["base_price"] = base
                for sc in _SIM_SCENARIOS:
                    try:
                        result = _run_selfacq(hotel_with_base, _signal, _real_data, sc)
                        ok = True
                        issues = []
                        if result.get("direct_offer_price", 0) <= 0:
                            issues.append("non_positive_price")
                        if "error" in result:
                            issues.append("selfacq_error")
                    except Exception as _ex:
                        ok = False
                        result = {"error": str(_ex)}
                        issues = ["selfacq_exception"]
                    global_counts["selfacq_runs"] += 1
                    if not ok or issues:
                        global_counts["selfacq_failures"] += 1
                    for issue in issues:
                        global_counts["issue_counts"][issue] = global_counts["issue_counts"].get(issue, 0) + 1
                    write_jsonl(log_path, {
                        "timestamp_utc": ts.isoformat(),
                        "cycle": cycle + 1,
                        "model": "selfacq",
                        "hotel_id": hotel["hotel_id"],
                        "hotel_star": hotel["star"],
                        "scenario": sc.name,
                        "external_snapshot": asdict(snapshot),
                        "ok": ok,
                        "issues": issues,
                        "result": result,
                    })

        global_counts["cycles_completed"] += 1
        summary_path.write_text(json.dumps(global_counts, indent=2, ensure_ascii=False), encoding="utf-8")

        if args.dry_run:
            break
        if cycle < total_cycles - 1:
            time.sleep(args.interval_seconds)

    print(f"completed_cycles={global_counts['cycles_completed']}")
    print(f"log_path={log_path}")
    print(f"summary_path={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ══════════════════════════════════════════════════════════════════════════════
# HROS V5 集成：为 Harness 记录追加 V5 对比字段
# ══════════════════════════════════════════════════════════════════════════════

def enrich_with_hros_v5(record: dict, snapshot) -> dict:
    """
    给 Harness JSONL 记录追加 HROS V5 字段，用于新旧算法对比。
    不修改原有字段，全部新增 _v5 后缀。
    """
    try:
        import sys, os
        _p = os.path.expanduser(
            "~/Desktop/InsightBridge_HROS_V5_Final/共用_HROS_V5引擎")
        if _p not in sys.path:
            sys.path.insert(0, _p)

        from hros.direct_ltv_engine import DirectLTVEngine
        from hros.risk_engine import calculate_price_risk
        from hros.opportunity_engine import calculate_opportunity_score

        price = record.get("result", {}).get("recommended_price") or \
                record.get("result", {}).get("direct_offer_price", 0)
        market = float(getattr(snapshot, "competitor_price", 0) or price or 1000)

        # Direct LTV V5（含折现）
        occ = float(getattr(snapshot, "current_occupancy", 0.72))
        result = record.get("result", {})
        if "direct_offer_price" in result and result["direct_offer_price"]:
            ota_gross = result.get("ota_standard_price", market)
            ltv_dec = DirectLTVEngine().evaluate_direct_offer(
                direct_price=float(result["direct_offer_price"]),
                ota_gross_price=float(ota_gross or market),
                ota_commission_rate=0.185 if getattr(snapshot, "star", 4) >= 5 else 0.15,
                repeat_probability=0.20,
                future_margin=700.0,
                discount_rate=0.10,
            )
            record["direct_ltv_v5"] = ltv_dec.direct_ltv
            record["direct_advantage_v5"] = ltv_dec.direct_advantage
            record["discounted_future_value_v5"] = ltv_dec.discounted_future_value

        # Risk & Opportunity V5
        signals = {
            "event_density":     float(getattr(snapshot, "event_ticket_sales", 0) or 0),
            "border_flow":       float(getattr(snapshot, "border_flow", 0) or 0),
            "ota_booking_pace":  float(getattr(snapshot, "ota_booking_pace", 0.5) or 0.5),
            "is_holiday":        bool(getattr(snapshot, "holiday", 0)),
            "is_weekend":        bool(getattr(snapshot, "weekend", 0)),
            "occupancy":         occ,
        }
        record["risk_score_v5"] = calculate_price_risk(
            price=float(price or market),
            market_price=market,
            predicted_occ=occ,
            ota_booking_pace=signals["ota_booking_pace"],
        )
        record["opportunity_score_v5"] = calculate_opportunity_score(signals)

    except Exception:
        pass
    return record
