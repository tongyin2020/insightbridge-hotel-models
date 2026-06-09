"""
澳门酒店 AI 模型 — 21天自动化模拟测试（真实数据版）
======================================================
真实数据来源：
  ✅ Booking.com 竞对房价（price_snapshots / Firecrawl / DSEC）
  ✅ Booking.com 4-5星均价 upper_tier_adr（price_snapshots / Firecrawl / DSEC）
  ✅ TurboJET渡轮满座率 → flight_ferry信号
  ✅ 澳门天气（Open-Meteo）
  ✅ 假日/周末（日历）
  ✅ 澳门活动密度（IR缓存 / Firecrawl）
  ✅ 访客统计（DSEC月报编码）
  ✅ 官网BAR + OTA竞对价：Shifter住宅代理每日3次采集（hotel_real_data.db）

运行方法：
    nohup python3 run_simulation.py > simulation.log 2>&1 &
    tail -f simulation.log
    kill $(cat simulation.pid)
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
import logging

import requests

_MODEL_ROOT = Path(__file__).resolve().parent.parent
if str(_MODEL_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODEL_ROOT))

from model_refinement import (
    EXTREME_CATEGORIES,
    NORMAL_CATEGORIES,
    apply_selfacq_profit_guard,
    compute_dual_score,
    get_director_feedback_signal,
    parse_pct_text,
    record_director_outcome,
)

# ── HROS V6 引擎（RevPAR优化 + 收益归因）────────────────────────
_V6_ENGINE_DIR = Path(__file__).resolve().parent.parent / "共用_HROS_V6引擎"
_V6_OK = False
try:
    import sys as _sys_v6
    if str(_V6_ENGINE_DIR) not in _sys_v6.path:
        _sys_v6.path.insert(0, str(_V6_ENGINE_DIR))
    from hros_v6.integration_adapter import apply_v6_to_mare_output
    from hros_v6.result_adapters import (
        adapt_director_to_v6_result,
        adapt_selfacq_to_v6_result,
        attach_revenue_attribution,
    )
    from hros_v6.revenue_decision_layer_v6 import RevenueDecisionLayerV6 as _V6Layer
    from hros_v6.crm_guardrails import CRMGuardrails as _CRMGuard
    from hros_v6.selfacq_engine_v6 import SelfACQEngineV6 as _SelfACQV6
    from hros_v6.revenue_attribution_engine import RevenueAttributionEngine as _AttrEngine
    _V6_OK = True
    print("✓ HROS V6 引擎已加载")
except Exception as _v6_err:
    print(f"⚠ HROS V6 未加载（继续V5）: {_v6_err}")

# ── 真实数据库路径（hotel_real_data.db）───────────────────────────────────────
_REAL_DB_PATH = _MODEL_ROOT / "hotel_collector" / "hotel_real_data.db"

# ── 双层OTA折算系数（OTA价 × 此系数 ≈ 官网BAR）
_OTA_TO_BAR_MASS    = 0.85   # 3-4★ 大众市场
_OTA_TO_BAR_LUXURY  = 0.72   # 5★ 奢华市场
_BAR_WEIGHT = {3: 0.55, 4: 0.55, 5: 0.70}   # 历史参考权重
_OTA_WEIGHT = {3: 0.45, 4: 0.45, 5: 0.30}   # 实时OTA权重

# ── 声誉情感引擎 + DSEC数据（hotel_collector目录）────────────────────────────
_COLLECTOR_DIR = _MODEL_ROOT / "hotel_collector"
if str(_COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(_COLLECTOR_DIR))

try:
    from sentiment_engine import get_reputation_signals as _get_rep_signals
    _SENTIMENT_OK = True
except ImportError:
    _SENTIMENT_OK = False
    def _get_rep_signals(hotel_id, tier, conn=None): return {"rep_adj": 0.0}

try:
    from dsec_loader import get_market_adr as _dsec_market_adr
    _DSEC_OK = True
except ImportError:
    _DSEC_OK = False
    def _dsec_market_adr(month, star, conn, year=None): return 0.0

try:
    from elasticity_engine import optimize_price as _elasticity_optimize
    _ELASTICITY_OK = True
except ImportError:
    _ELASTICITY_OK = False
    # 降级：返回候选价本身，lift 保持规则值
    def _elasticity_optimize(candidate_price, market_price, star, district="NAPE",
                              demand_level="NORMAL", season="normal", hotel_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            optimal_price=candidate_price, predicted_occupancy=0.72,
            predicted_revpar=candidate_price*0.72, baseline_revpar=market_price*0.72,
            predicted_trevpar=candidate_price*0.72, baseline_trevpar=market_price*0.72,
            true_lift_pct=0.0, revpar_lift_pct=0.0, elasticity_used=0.0,
            elasticity_profile="unavailable", max_price_premium=0.0, optimal_occupancy=0.72,
            ml_enabled=False, ml_elasticity_multiplier=1.0, ml_premium_delta=0.0,
            ml_occupancy_delta=0.0, ml_state_version=0,
            ancillary_profile="unavailable", ancillary_ratio_used=0.0,
            ancillary_margin_used=0.0, ancillary_per_occ=0.0,
            data_source="unavailable", search_steps=0
        )

try:
    from mare_ml_layer import score_mare_outcome as _score_mare_outcome, update_adjustments as _update_mare_adjustments
    _MARE_ML_FEEDBACK_OK = True
except ImportError:
    _MARE_ML_FEEDBACK_OK = False
    def _score_mare_outcome(result, anomalies):
        return False
    def _update_mare_adjustments(decision, success):
        return None


def compute_dynamic_base_price(hotel_id: str, star: int,
                                ota_snapshot_price: float,
                                month: int = None) -> float:
    """
    动态计算 base_price：
      Step A — 当前市场中心价：优先使用 MHA 最新月度 ADR
               3★+4★ → mass 市场
               5★    → luxury 市场
      Step B — 若 MHA 缺失，再降级到 DSEC 月度 ADR / OTA 估算
      Step C — 声誉情感修正 rep_adj ∈ [-0.17, +0.17]
      Step D — 库存紧张溢价（avail_level: critical/low/moderate）
    """
    if month is None:
        month = datetime.now().month

    tier = {3: "3_star", 4: "4_star", 5: "5_star"}.get(star, "3_star")
    ratio = _OTA_TO_BAR_LUXURY if star == 5 else _OTA_TO_BAR_MASS
    ota_estimate = max(ota_snapshot_price * ratio, 100.0)
    dsec_adr_ref  = 0.0
    mha_adr_ref   = 0.0
    shared_conn   = None

    if _REAL_DB_PATH.exists():
        try:
            shared_conn = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
        except Exception:
            pass

    if _DSEC_OK and _REAL_DB_PATH.exists():
        try:
            _dc = shared_conn or sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
            dsec_adr_ref = _dsec_market_adr(month, star, _dc)
            from dsec_loader import get_latest_market_adr as _mha_latest_adr
            mha_adr_ref = float(_mha_latest_adr(star, _dc) or 0.0)
            if not shared_conn:
                _dc.close()
        except Exception:
            pass

    if mha_adr_ref > 0:
        base = mha_adr_ref
    elif dsec_adr_ref > 0:
        base = dsec_adr_ref
    else:
        base = ota_estimate * 0.97

    # Step B：使用 DSEC 过去两年历史边界做区间截断
    settings = _hotel_settings({"star": star})
    lo, hi = float(settings.floor_price), float(settings.ceiling_price)
    base = max(lo, min(hi, base))

    # Step C：声誉情感修正（review_sentiment + google_ratings 双源）
    rep_adj = 0.0
    if _SENTIMENT_OK:
        try:
            signals = _get_rep_signals(hotel_id, tier, shared_conn)
            rep_adj = float(signals.get("rep_adj", 0.0))
        except Exception:
            pass

    base = base * (1.0 + rep_adj)

    # Step D：OTA库存紧张信号修正（inventory_signals → 需求溢价）
    inv_adj = 0.0
    if shared_conn:
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

    if shared_conn:
        try:
            shared_conn.close()
        except Exception:
            pass

    base = base * (1.0 + inv_adj)
    base = max(lo, min(hi, base))
    return round(base / 10) * 10

# ── 企业微信推送（非阻塞，不影响模拟运行）────────────────────────────────────
_last_critical_alert  = 0   # 防刷屏：CRITICAL 最多每2小时一次
_last_metrics_push    = 0   # 防刷屏：表现快报 最多每6小时一次

def _wecom_push_async(content: str):
    """旧企业微信通道已废弃，保留空实现以兼容历史调用。"""
    return None

def _normalize_anomalies(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [s.strip() for s in str(raw).split(";") if s.strip()]

def _summarize_hour_health(hour_results: list[tuple]) -> tuple[int, int, int]:
    total = len(hour_results)
    failures = 0
    anomalies = 0
    for _, _, _, raw in hour_results:
        issues = _normalize_anomalies(raw)
        if issues:
            anomalies += 1
        if any("EXCEPTION" in issue for issue in issues):
            failures += 1
    return total, failures, anomalies

_SHIFTER_MARKET_CACHE = Path(__file__).parent / "data" / "shifter_market_cache.json"
_SHIFTER_MARKET_CACHE_TTL = 86400  # 24小时缓存（市场均价日更即可）

def _fetch_shifter_market_prices() -> dict:
    """
    用 Shifter 住宅代理爬取 Agoda + Trip.com 澳门全市场价格。
    替代已停用的 BrightData 接口，提供相同的 {count/min/max/avg/p25/p75} 市场基准。

    策略（按优先级）：
    ① 本地24h缓存（data/shifter_market_cache.json）
    ② price_snapshots 中 booking_rate/agoda_rate 列（hotel_data_collector 已采集）
    ③ 实时 Agoda 澳门城市搜索页（Playwright + Shifter）
    """
    import time as _time

    # ── ① 检查本地缓存 ────────────────────────────────────────────────────────
    try:
        if _SHIFTER_MARKET_CACHE.exists():
            cached = json.loads(_SHIFTER_MARKET_CACHE.read_text(encoding="utf-8"))
            age = _time.time() - cached.get("_ts", 0)
            if age < _SHIFTER_MARKET_CACHE_TTL:
                return {k: v for k, v in cached.items() if not k.startswith("_")}
    except Exception:
        pass

    all_prices_mop: list[float] = []
    sources: list[str] = []

    # ── ② 从 price_snapshots 读取已采集 OTA 价格 ────────────────────────────
    try:
        if _REAL_DB_PATH.exists():
            conn = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
            # 取最近7天的 booking_rate 和 agoda_rate（不含异常低价）
            rows = conn.execute("""
                SELECT booking_rate, agoda_rate, star
                FROM price_snapshots
                WHERE snapshot_time >= datetime('now','-7 days')
                  AND (booking_rate > 200 OR agoda_rate > 200)
            """).fetchall()
            conn.close()
            for bcom, agoda, star in rows:
                if bcom and bcom > 200:
                    all_prices_mop.append(float(bcom))
                    if "Booking.com" not in sources:
                        sources.append("Booking.com")
                if agoda and agoda > 200:
                    all_prices_mop.append(float(agoda))
                    if "Agoda" not in sources:
                        sources.append("Agoda")
    except Exception:
        pass

    # ── ③ 若 OTA 列为空，用 Shifter 实时爬 Agoda 澳门城市搜索 ────────────────
    if not all_prices_mop:
        try:
            import os as _os
            from datetime import date as _date, timedelta as _td
            from playwright.sync_api import sync_playwright

            _su = _os.getenv("SHIFTER_USER", "")
            _sp = _os.getenv("SHIFTER_PASS", "")
            proxy_cfg = ({"server": "http://p.shifter.io:443",
                          "username": _su, "password": _sp}
                         if _su and _sp else None)

            checkin  = (_date.today() + _td(days=1)).isoformat()
            checkout = (_date.today() + _td(days=2)).isoformat()

            # Agoda 澳门全市搜索（city=8646 = Macau）
            agoda_url = (
                f"https://www.agoda.com/zh-hk/search"
                f"?city=8646&checkIn={checkin}&checkOut={checkout}"
                f"&rooms=1&adults=2&selectedCurrency=MOP"
            )
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True, proxy=proxy_cfg,
                    args=["--no-sandbox","--disable-blink-features=AutomationControlled",
                          "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    locale="zh-HK", viewport={"width": 1440, "height": 900},
                )
                page = ctx.new_page()
                page.goto(agoda_url, wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(4000)
                html = page.content()
                browser.close()

            # 从 Agoda 搜索结果提取 MOP 价格
            agoda_prices = []
            for m in re.finditer(r"MOP[\s\xa0]*([\d,]+)", html):
                try:
                    p = int(m.group(1).replace(",", ""))
                    if 200 < p < 20000:
                        agoda_prices.append(p)
                except (ValueError, TypeError):
                    pass
            if agoda_prices:
                all_prices_mop.extend(agoda_prices)
                sources.append("Agoda(live)")

        except Exception as _e:
            logger.warning(f"Shifter market sweep failed: {_e}")

    # ── 兜底：若所有来源均失败，返回空 ────────────────────────────────────────
    if not all_prices_mop:
        return {}

    all_prices_mop.sort()
    n = len(all_prices_mop)
    result = {
        "count":   n,
        "min":     all_prices_mop[0],
        "max":     all_prices_mop[-1],
        "avg":     round(sum(all_prices_mop) / n),
        "p25":     all_prices_mop[n // 4],
        "p75":     all_prices_mop[n * 3 // 4],
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "sources": "+".join(sorted(set(sources))),
    }

    # 写入本地缓存
    try:
        import time as _time2
        _SHIFTER_MARKET_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _SHIFTER_MARKET_CACHE.write_text(
            json.dumps({**result, "_ts": _time2.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    return result

def _load_market_benchmarks_by_star() -> dict:
    """
    从 price_snapshots 按星级分类计算真实市场基准价。
    返回：
      mass    — 3★+4★ 大众市场均价（MARE/CRM 对比基准）
      luxury  — 5★ 豪华市场均价（DirectorAI/SelfACQ 对比基准）
    每个子字典含 count/avg/min/max/p25/p75 字段。

    数据来源优先级：
    ① price_snapshots.official_bar（hotel_data_collector 实时采集）
    ② price_snapshots.booking_rate / agoda_rate（Shifter OTA采集）
    ③ _fetch_shifter_market_prices()（Agoda全市场扫描，24h缓存）
    """
    result = {"mass": {}, "luxury": {}}
    if not _REAL_DB_PATH.exists():
        # 若数据库不存在，直接走 Shifter 市场扫描
        shifter_bm = _fetch_shifter_market_prices()
        if shifter_bm.get("avg", 0) > 0:
            result["mass"] = shifter_bm   # 全市混合均价作为 mass 基准
        return result
    try:
        conn = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)

        def _seg_stats(star_filter: str) -> dict:
            # ── 优先：官网 BAR（已经过 A-1 星级验证过滤）
            rows = conn.execute(f"""
                SELECT official_bar FROM price_snapshots
                WHERE {star_filter} AND official_bar > 200 AND source_ok = 1
                ORDER BY official_bar
            """).fetchall()
            # ── 补充：OTA 采集价（Shifter Booking.com / Agoda）
            ota_rows = conn.execute(f"""
                SELECT booking_rate FROM price_snapshots
                WHERE {star_filter} AND booking_rate > 200
                UNION ALL
                SELECT agoda_rate   FROM price_snapshots
                WHERE {star_filter} AND agoda_rate   > 200
            """).fetchall()
            prices = [r[0] for r in rows] + [r[0] for r in ota_rows]
            if not prices:
                return {}
            prices.sort()
            n = len(prices)
            return {
                "count": n,
                "avg":   round(sum(prices) / n),
                "min":   prices[0],
                "max":   prices[-1],
                "p25":   prices[n // 4],
                "p75":   prices[n * 3 // 4],
            }

        result["mass"]   = _seg_stats("star IN (3, 4)")
        result["luxury"] = _seg_stats("star = 5")
        conn.close()

        # ── ③ 若 price_snapshots 覆盖不足，补充 Shifter 全市场扫描 ────────────
        if not result["mass"] or result["mass"].get("count", 0) < 3:
            shifter_bm = _fetch_shifter_market_prices()
            if shifter_bm.get("avg", 0) > 0:
                result["mass"] = shifter_bm
    except Exception:
        pass
    return result

def _alert_critical(hour: int, critical_list: list[str], avg_mare: float, avg_acq: float):
    """CRITICAL 告警推送（每2小时最多一次）"""
    global _last_critical_alert
    now = time.time()
    if now - _last_critical_alert < 7200:
        return
    _last_critical_alert = now
    psrs_items   = [c for c in critical_list if "PSRS" in c]
    other_items  = [c for c in critical_list if "PSRS" not in c]
    scenario_note = ""
    if psrs_items and not other_items:
        scenario_note = "\n> 📋 以上为 **PSRS_FAILURE 压力测试场景**（设计内），非真实系统故障\n"
    elif psrs_items:
        scenario_note = f"\n> 📋 其中 {len(psrs_items)} 条为PSRS压力测试场景（设计内）\n"
    details = "\n".join(f"- {c}" for c in critical_list[:5])
    msg = (
        f"## 🔴 酒店AI模型 — CRITICAL 告警\n"
        f"**第{hour+1}小时** | {datetime.now():%Y-%m-%d %H:%M}\n\n"
        f"**异常详情：**\n{details}\n"
        f"{scenario_note}\n"
        f"MARE均价: MOP {avg_mare:.0f}（3-4星） | 直销均价: MOP {avg_acq:.0f}（5星豪华）"
    )
    _wecom_push_async(msg)

def _push_metrics_snapshot(hour: int, avg_mare: float, avg_crm: float, avg_acq: float,
                            total_runs: int, failure_count: int, anomaly_count: int):
    """每6小时推送一次模型表现快报"""
    global _last_metrics_push
    now = time.time()
    if now - _last_metrics_push < 21600:   # 6小时冷却
        return
    _last_metrics_push = now

    bm = _load_market_benchmarks_by_star()
    mass_bm    = bm.get("mass", {})
    luxury_bm  = bm.get("luxury", {})
    market_section = ""
    if mass_bm or luxury_bm:
        rows = ""
        if mass_bm and mass_bm.get("avg", 0) > 0:
            mare_vs = (avg_mare - mass_bm["avg"]) / mass_bm["avg"] * 100
            trend_m = "📈 高于" if mare_vs > 2 else ("📉 低于" if mare_vs < -2 else "≈ 贴近")
            rows += (
                f"| 3-4★大众均价 | MOP {mass_bm['avg']} ({mass_bm['count']}条) "
                f"| MOP {avg_mare:.0f} | {trend_m} {abs(mare_vs):.1f}% |\n"
            )
        if luxury_bm and luxury_bm.get("avg", 0) > 0:
            acq_vs = (avg_acq - luxury_bm["avg"]) / luxury_bm["avg"] * 100
            trend_l = "📈 高于" if acq_vs > 2 else ("📉 低于" if acq_vs < -2 else "≈ 贴近")
            rows += (
                f"| 5★豪华均价 | MOP {luxury_bm['avg']} ({luxury_bm['count']}条) "
                f"| MOP {avg_acq:.0f} | {trend_l} {abs(acq_vs):.1f}% |\n"
            )
        if rows:
            market_section = (
                f"\n**📊 分市场对比（price_snapshots 真实快照）**\n"
                f"| 细分市场 | 真实均价 | 模型推荐 | 偏差 |\n"
                f"|---|---|---|---|\n"
                f"{rows}"
            )

    failure_rate = f"{(failure_count / total_runs * 100):.1f}%" if total_runs else "N/A"
    anomaly_rate = f"{(anomaly_count / total_runs * 100):.1f}%" if total_runs else "N/A"
    if failure_count > 0:
        status = f"🔴 失败 {failure_count}/{total_runs} | 异常 {anomaly_count}/{total_runs}"
    elif anomaly_count > 0:
        status = f"⚠️ 失败 0/{total_runs} | 异常 {anomaly_count}/{total_runs}"
    else:
        status = f"✅ 失败 0/{total_runs} | 异常 0/{total_runs}"
    day_num = (hour // 24) + 1
    msg = (
        f"## 📊 AI模型表现快报 — 第{day_num}天 H{hour+1}\n"
        f"**{datetime.now():%Y-%m-%d %H:%M}**\n\n"
        f"**定价推荐（当前小时）**\n"
        f"| 模型 | 均价 | 覆盖 |\n"
        f"|------|------|------|\n"
        f"| MARE（3★） | MOP {avg_mare:.0f} | {len(HOTELS_3_STAR)}家真实 |\n"
        f"| DirectorAI CRM（3★） | MOP {avg_crm:.0f} | {len(HOTELS_3_STAR)}家真实 |\n"
        f"| SelfACQ 直销（4-5★） | MOP {avg_acq:.0f} | {len(HOTELS_45_STAR)}家真实 |\n"
        f"{market_section}\n"
        f"运行健康：失败率 {failure_rate} | 异常率 {anomaly_rate}\n"
        f"注：异常率含护栏/压力测试告警，不等于程序崩溃\n"
        f"状态：{status} | 进度：{hour+1}/504小时 ({(hour+1)/504*100:.0f}%)"
    )
    _wecom_push_async(msg)

def _push_daily_summary(summary: dict):
    """每日汇总推送（含真实市场基准对比）"""
    if summary['failures'] > 0:
        health_icon = "🔴"
    elif summary['anomalies'] > 0:
        health_icon = "⚠️"
    else:
        health_icon = "✅"
    day = summary.get('day', 0) + 1

    bm = _load_market_benchmarks_by_star()
    mass_bm   = bm.get("mass", {})
    luxury_bm = bm.get("luxury", {})
    market_note = ""
    if mass_bm or luxury_bm:
        lines = []
        if mass_bm and mass_bm.get("avg", 0) > 0:
            mare_diff = summary['avg_mare_price'] - mass_bm['avg']
            arrow = "↑" if mare_diff > 0 else "↓"
            lines.append(
                f"3-4★大众基准 MOP {mass_bm['avg']} ({mass_bm['count']}条) | "
                f"MARE推荐 {arrow} {abs(mare_diff):.0f} MOP ({mare_diff/mass_bm['avg']*100:+.1f}%)"
            )
        if luxury_bm and luxury_bm.get("avg", 0) > 0:
            acq_diff = summary['avg_selfacq_offer'] - luxury_bm['avg']
            arrow = "↑" if acq_diff > 0 else "↓"
            lines.append(
                f"5★豪华基准 MOP {luxury_bm['avg']} ({luxury_bm['count']}条) | "
                f"SelfACQ推荐 {arrow} {abs(acq_diff):.0f} MOP ({acq_diff/luxury_bm['avg']*100:+.1f}%)"
            )
        if lines:
            market_note = "\n**分市场真实基准对比**\n" + "\n".join(lines) + "\n"

    # 估算 RevPAR 提升（3-4★ MARE vs 大众市场均价）
    revpar_note = ""
    if mass_bm and mass_bm.get("avg", 0) > 0:
        uplift_pct = (summary['avg_mare_price'] - mass_bm['avg']) / mass_bm['avg'] * 100
        if uplift_pct > 0:
            revpar_note = f"\n> 💡 MARE较3-4★市场均价高 **{uplift_pct:.1f}%**，预计 RevPAR 正向贡献\n"

    msg = (
        f"## 🏨 AI模型日报 — 第{day}天\n"
        f"**{summary['date']}**\n\n"
        f"**模型定价（日均）**\n"
        f"| 模型 | 均价 |\n|------|------|\n"
        f"| MARE（3★，{len(HOTELS_3_STAR)}家真实） | MOP {summary['avg_mare_price']} |\n"
        f"| DirectorAI CRM（3★） | MOP {summary['avg_crm_price']} |\n"
        f"| SelfACQ 直销（4-5★，{len(HOTELS_45_STAR)}家真实） | MOP {summary['avg_selfacq_offer']} |\n\n"
        f"**运行状态**\n"
        f"运行次数：{summary['runs']:,} | 失败：{summary['failures']} ({summary['failure_rate']:.1f}%) | "
        f"异常：{summary['anomalies']} ({summary['anomaly_rate']:.1f}%)\n"
        f"注：异常含护栏/压力测试告警，不等于程序崩溃\n"
        f"{market_note}{revpar_note}\n"
        f"{health_icon} {summary['health']} | 进度：{day}/21天"
    )
    _wecom_push_async(msg)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

# ── 环境设置 ──────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("MODEL_WEIGHTS_PATH", str(Path(__file__).parent / "data" / "model_weights.json"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")   # 加载主.env环境变量

from data_fetchers.real_data import get_all_real_signals
from data_fetchers.scenario_engine import get_scenario, get_scenario_stats, HotelScenario, SCENARIOS

# Bug Fix: pricing_engine.py 内部 from app.services.* 需要 mare_engine/api 在 sys.path
_MARE_API_PATH = str(Path(__file__).resolve().parent.parent / "system1_chatgpt_harness" / "mare_engine" / "api")
if _MARE_API_PATH not in sys.path:
    sys.path.insert(0, _MARE_API_PATH)

import pricing_engine as pe
from objective_modes import ObjectiveMode, apply_objective_adjustment, get_objective_weights
from recommendations import RecommendationRequest
from types import SimpleNamespace

# ── 市场组专属护栏配置（DSEC过去两年历史区间）───────────────────────────────
_STAR_GUARDRAIL = {
    3: SimpleNamespace(floor_price=700,  ceiling_price=1200),
    4: SimpleNamespace(floor_price=700,  ceiling_price=1200),
    5: SimpleNamespace(floor_price=1200, ceiling_price=2200),
}


def _market_group(star: int) -> str:
    return "luxury" if int(star or 0) >= 5 else "mass"


def _hotel_settings(hotel: dict):
    """返回酒店对应市场组的历史 floor / ceiling 护栏。"""
    if _REAL_DB_PATH.exists():
        try:
            from dsec_loader import get_market_group_floor_ceiling
            _dc = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
            floor_price, ceiling_price = get_market_group_floor_ceiling(hotel.get("star", 3), _dc)
            _dc.close()
            return SimpleNamespace(floor_price=float(floor_price), ceiling_price=float(ceiling_price))
        except Exception:
            pass
    return _STAR_GUARDRAIL.get(hotel.get("star", 3), _STAR_GUARDRAIL[3])

# ── 配置 ──────────────────────────────────────────────────────────────────────
TOTAL_HOURS = 21 * 24          # 504小时 = 21天
SLEEP_SECONDS = 3600           # 每小时一次（真实运行）
                                # 快速测试时改为 0

DB_PATH = Path(__file__).parent / "results.db"
PID_FILE = Path(__file__).parent / "simulation.pid"

def _build_hotel_roster() -> tuple[list[dict], list[dict]]:
    """
    生成澳门全市酒店名单（固定随机种子，结果可复现）。
    3星：73家；4-5星：280家。
    """
    rng = random.Random(2026)

    # ── 3星分布：(区域代码, 区域名, 星级, 数量, 最低价, 最高价, 最少房, 最多房)
    spec_3 = [
        ("TAIPA", "氹仔",     3, 25, 580, 950,  80, 220),
        ("NAPE",  "新口岸",   3, 20, 620, 980,  90, 250),
        ("INNER", "内港",     3, 18, 560, 900,  75, 200),
        ("COT",   "路凼",     3, 10, 700, 1050, 100, 280),
    ]  # 合计：25+20+18+10 = 73家

    types_3 = ["精品酒店", "商务酒店", "城市酒店", "服务式公寓"]

    hotels_3: list[dict] = []
    for dc, dcn, star, count, plo, phi, rlo, rhi in spec_3:
        for i in range(1, count + 1):
            hotels_3.append({
                "hotel_id":   f"MAC_{star}S_{dc}_{i:03d}",
                "name":       f"{dcn}{star}星{rng.choice(types_3)}{i:02d}",
                "base_price": float(round(rng.uniform(plo, phi) / 10) * 10),
                "total_rooms": rng.randint(rlo, rhi),
                "star":       star,
                "district":   dc,
            })

    # ── 4-5星分布
    spec_45 = [
        ("COTAI", "路凼",     5, 60, 2200, 5500, 300, 3000),
        ("COTAI", "路凼",     4, 50, 1100, 2100, 200,  800),
        ("NAPE",  "新口岸",   5, 35, 1800, 3500, 200,  600),
        ("NAPE",  "新口岸",   4, 40, 1000, 1900, 150,  500),
        ("TAIPA", "氹仔",     4, 30, 1000, 1800, 150,  450),
        ("TAIPA", "氹仔",     5, 25, 1600, 3000, 200,  700),
        ("HIST",  "历史城区", 4, 20,  950, 1700, 120,  400),
        ("HIST",  "历史城区", 5, 15, 1500, 2800, 150,  500),
        ("COL",   "路环",     5,  5, 2000, 4000, 100,  300),
    ]  # 合计：60+50+35+40+30+25+20+15+5 = 280家

    types_4 = ["大酒店", "国际酒店", "假日酒店", "皇庭酒店", "美居酒店"]
    types_5 = ["豪华酒店", "皇宫酒店", "君悦酒店", "四季酒店", "文华东方"]

    hotels_45: list[dict] = []
    for dc, dcn, star, count, plo, phi, rlo, rhi in spec_45:
        tnames = types_4 if star == 4 else types_5
        for i in range(1, count + 1):
            hotels_45.append({
                "hotel_id":   f"MAC_{star}S_{dc}_{i:03d}",
                "name":       f"澳门{dcn}{star}星{rng.choice(tnames)}{i:02d}",
                "base_price": float(round(rng.uniform(plo, phi) / 50) * 50),
                "total_rooms": rng.randint(rlo, rhi),
                "star":       star,
                "district":   dc,
            })

    return hotels_3, hotels_45


# ── 切换为澳门旅游局官方76家真实酒店 ─────────────────────────────────────────
try:
    from hotel_roster_76 import HOTELS_3STAR, HOTELS_45STAR, ALL_HOTELS_76
    HOTELS_3_STAR  = HOTELS_3STAR          # 18家3★真实酒店
    HOTELS_45_STAR = HOTELS_45STAR         # 58家4-5★真实酒店
    ALL_HOTELS     = ALL_HOTELS_76         # 76家合计
except ImportError:
    # 降级到原虚构名单（保底）
    HOTELS_3_STAR,  HOTELS_45_STAR = _build_hotel_roster()
    ALL_HOTELS = HOTELS_3_STAR + HOTELS_45_STAR

# 2026年澳门公众假期（影响border_flow和holiday因子）
MACAU_HOLIDAYS_2026 = {
    "05-01", "05-02", "05-03", "05-04", "05-05",  # 五一黄金周
    "06-19",                                        # 端午节
    "09-30", "10-01", "10-02", "10-03", "10-04",  # 国庆黄金周
    "12-20",                                        # 澳门回归纪念日
    "12-25",                                        # 圣诞
}


# ── 数据库初始化 ───────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS hourly_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT NOT NULL,
            sim_hour    INTEGER NOT NULL,
            hotel_id    TEXT NOT NULL,
            hotel_name  TEXT,
            model_type  TEXT NOT NULL,
            season      TEXT,
            input_json  TEXT,
            output_json TEXT,
            rec_price   REAL,
            demand_state TEXT,
            confidence  TEXT,
            exp_lift    TEXT,
            anomaly     TEXT,
            weather_celsius REAL,
            is_holiday  INTEGER,
            is_weekend  INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_summaries (
            day         INTEGER PRIMARY KEY,
            date_str    TEXT,
            avg_rec_price_23 REAL,
            avg_rec_price_45 REAL,
            anomaly_count INTEGER,
            total_runs   INTEGER,
            summary_json TEXT
        );

        CREATE TABLE IF NOT EXISTS anomaly_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT,
            hotel_id    TEXT,
            anomaly_type TEXT,
            detail      TEXT
        );

        CREATE TABLE IF NOT EXISTS attribution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT, sim_hour INTEGER,
            baseline_revpar REAL, optimized_revpar REAL,
            total_lift_pct REAL, attribution_json TEXT
        );

        CREATE TABLE IF NOT EXISTS model_selection_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            model_family TEXT,
            scenario_band TEXT,
            sample_size INTEGER,
            total_score REAL,
            profit_uplift_pct REAL,
            failure_rate_pct REAL,
            anomaly_rate_pct REAL,
            real_data_ratio REAL,
            explainability_ratio REAL,
            runtime_cost_ratio REAL,
            notes TEXT
        );
    """)
    conn.commit()
    return conn


# ── 市场信号合成（真实数据 + 统计模拟）──────────────────────────────────────
def get_macau_market_signal(sim_hour: int, real_data: dict) -> dict:
    """
    合并真实抓取数据和统计模拟信号，生成完整市场信号。

    真实数据（来自 get_all_real_signals()）:
      weather, flight_ferry, event_ticket_sales, visitors_stats,
      competitor_price, competitor_availability, upper_tier_adr

    统计模拟（无法爬取）:
      border_flow, zhuhai_saturation, ota_booking_pace
    """
    now = datetime.now()
    hour_of_day = now.hour
    day_of_week = now.weekday()
    month = now.month
    date_str = now.strftime("%m-%d")

    is_weekend = day_of_week >= 5
    is_holiday = date_str in MACAU_HOLIDAYS_2026

    # ── 季节判断 ──────────────────────────────────────────────────────────
    season_map = {
        1: "off_peak", 2: "shoulder", 3: "shoulder",
        4: "peak",     5: "shoulder", 6: "off_peak",
        7: "off_peak", 8: "off_peak", 9: "shoulder",
        10: "peak",    11: "peak",    12: "super_peak",
    }
    season = season_map.get(month, "shoulder")
    if is_holiday:
        season = "super_peak"

    # ── 场景驱动因子（取代单一时间公式，覆盖全值域对抗测试）────────────
    # 按小时轮换14个场景：OVERFLOW_SQUEEZE(0.95)→DEMAND_COLLAPSE(-0.35)→...
    market_scenario = get_scenario(0, sim_hour)
    border_flow = round(max(-1.0, min(1.0,
                    market_scenario.sim_border_flow       + _jitter(0.0, 0.04))), 3)
    zhuhai_sat  = round(max( 0.0, min(1.0,
                    market_scenario.sim_zhuhai_saturation + _jitter(0.0, 0.03))), 3)

    # ota_booking_pace：统一由场景模拟驱动
    ota_pace = round(max(0.0, min(1.0,
                  market_scenario.sim_ota_booking_pace + _jitter(0.0, 0.03))), 3)
    ota_pace_source = f"scenario_{market_scenario.name}"

    # ── DSEC 历史需求信号（长期底盘）─────────────────────────────────────
    dsec_market_occ = 0.0
    if _DSEC_OK and _REAL_DB_PATH.exists():
        try:
            _dc = sqlite3.connect(str(_REAL_DB_PATH), timeout=5)
            from dsec_loader import get_dsec_demand_signal as _dsec_sig
            dsec_market_occ = round(
                0.4 * _dsec_sig(month, 3, _dc) +
                0.3 * _dsec_sig(month, 4, _dc) +
                0.3 * _dsec_sig(month, 5, _dc), 4
            )
            _dc.close()
        except Exception:
            pass

    # ── MHA 当前月需求信号（当前主锚）──────────────────────────────────────
    mha_mass_occ = float(real_data.get("mha_occ_mass", 0.0) or 0.0)
    mha_lux_occ = float(real_data.get("mha_occ_luxury", 0.0) or 0.0)
    mha_mass_sig = float(real_data.get("mha_signal_mass", 0.0) or 0.0)
    mha_lux_sig = float(real_data.get("mha_signal_luxury", 0.0) or 0.0)
    mha_market_occ = round(0.5 * mha_mass_sig + 0.5 * mha_lux_sig, 4)
    blended_market_demand_signal = round(0.4 * dsec_market_occ + 0.6 * mha_market_occ, 4)
    if mha_market_occ > 0.18:
        mha_demand_state = "HIGH"
    elif mha_market_occ < -0.12:
        mha_demand_state = "LOW"
    else:
        mha_demand_state = "NORMAL"

    # ── 合并信号 ──────────────────────────────────────────────────────────
    weather_c = real_data.get("weather_celsius", 25.0)

    return {
        "season": season,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "hour_of_day": hour_of_day,
        "day_of_week": day_of_week,

        # 真实数据
        "weather_signal":    real_data.get("weather", 0.0),
        "weather_celsius":   weather_c,
        "flight_ferry":      real_data.get("flight_ferry", 0.1),
        "event_ticket_sales":real_data.get("event_ticket_sales", 0.0),
        "visitors_stats":    real_data.get("visitors_stats", 0.0),
        "mha_occ_mass":      mha_mass_occ,
        "mha_occ_luxury":    mha_lux_occ,
        "mha_market_occ":    mha_market_occ,
        "blended_market_demand_signal": blended_market_demand_signal,
        "mha_demand_state":  mha_demand_state,

        # DSEC / MHA 需求信号
        "dsec_market_occ":   dsec_market_occ,

        # 混合信号（场景模拟）
        "border_flow":        border_flow,
        "ota_booking_pace":   min(1.0, ota_pace),
        "ota_pace_source":    ota_pace_source,
        "zhuhai_saturation":  zhuhai_sat,

        # 日历信号
        "holiday":  1.0 if is_holiday else (0.5 if is_weekend else 0.0),
        "weekend":  1.0 if is_weekend else 0.0,
    }


def _jitter(base: float, noise: float) -> float:
    return max(-1.0, min(1.0, base + random.gauss(0, noise)))


# ── 3星定价模型测试（MARE）────────────────────────────────────────────────
def run_3star_test(hotel: dict, signal: dict, real_data: dict,
                    scenario: HotelScenario) -> dict:
    """
    MARE 房价优化模型测试。
    外部因子来自实时抓取数据（Booking.com/天气/渡轮）。
    内部因子来自 scenario（入住率/客户画像/预订节奏），
    确保极端场景被系统性测试。
    """
    occupancy = scenario.occupancy

    # 竞对价格：Booking.com实时价格 × 场景调整系数
    real_prices = real_data.get("booking_prices_3", [])
    if real_prices:
        base_comp = float(sum(real_prices) / len(real_prices))
        competitor_price = max(200.0, base_comp * scenario.competitor_price_multiplier
                               * random.uniform(0.97, 1.03))
        competitor_availability = real_data.get("competitor_availability_real", 0.5)
    else:
        competitor_price = hotel["base_price"] * scenario.competitor_price_multiplier
        competitor_availability = max(0.05, 1.0 - occupancy - 0.10)

    upper_tier = real_data.get("upper_tier_adr_real") or None
    remaining = max(1, int(hotel["total_rooms"] * scenario.remaining_inventory_ratio))

    req = RecommendationRequest(
        hotel_id=hotel["hotel_id"],
        hotel_star=hotel.get("star", 3),   # 竞对权重差异化：3-4★ vs 5★
        season=signal["season"],
        base_price=hotel["base_price"],
        # 外部实时信号
        holiday=signal["holiday"],
        weekend=signal["weekend"],
        border_flow=signal["border_flow"],
        visitors_stats=signal["visitors_stats"],
        flight_ferry=signal["flight_ferry"],
        zhuhai_saturation=signal["zhuhai_saturation"],
        ota_booking_pace=signal["ota_booking_pace"],
        weather=signal["weather_signal"],
        event_ticket_sales=signal["event_ticket_sales"],
        # 竞对（实时+场景调整）
        competitor_price=competitor_price,
        competitor_availability=competitor_availability,
        upper_tier_adr=upper_tier,
        # 内部数据（来自scenario，非随机）
        current_occupancy=occupancy,
        remaining_inventory=remaining,
        total_rooms=hotel["total_rooms"],
        booking_velocity_24h=scenario.booking_velocity_24h,
        days_to_arrival=scenario.days_to_arrival,
        cancellation_rate=scenario.cancellation_rate,
        # DSEC / MHA 需求信号
        dsec_market_occ=signal.get("dsec_market_occ", 0.0),
        mha_market_occ=signal.get("mha_market_occ", 0.0),
        blended_market_demand_signal=signal.get("blended_market_demand_signal", 0.0),
    )
    result = pe.recommend(req, hotel_settings=_hotel_settings(hotel))

    # ── Phase 2：价格弹性 RevPAR 最优化 ──────────────────────────────────────
    if _ELASTICITY_OK and result.get("recommended_price", 0) > 0:
        mkt_price = (float(sum(real_prices) / len(real_prices))
                     if real_prices else hotel["base_price"])
        # 修复(2026-06-02 P3-A): 防止OTA淡季低价(≤500 MOP)将搜索基准拉至不合理水平
        # DSEC后疫情3★年均ADR≈950 MOP；低季节最低合理参考 = 950×0.70 = 665，取整680
        _guardrail = _hotel_settings(hotel)
        _mkt_floor = float(_guardrail.floor_price)
        _mkt_ceil = float(_guardrail.ceiling_price)
        mkt_price = min(max(mkt_price, _mkt_floor), _mkt_ceil)
        er = _elasticity_optimize(
            candidate_price = result["recommended_price"],
            market_price    = mkt_price,
            star            = hotel.get("star", 3),
            district        = hotel.get("district", "NAPE"),
            demand_level    = result.get("demand_state", signal.get("demand_state", "NORMAL")),
            season          = signal.get("season", "normal"),
            hotel_id        = hotel.get("hotel_id"),
        )
        result["recommended_price"]   = er.optimal_price
        result["predicted_occupancy"] = er.predicted_occupancy
        result["predicted_revpar"]    = er.predicted_revpar
        result["baseline_revpar"]     = er.baseline_revpar
        result["predicted_trevpar"]   = er.predicted_trevpar
        result["baseline_trevpar"]    = er.baseline_trevpar
        result["expected_revenue_lift"] = f"{er.true_lift_pct:+.1f}%"
        result["expected_room_revenue_lift"] = f"{er.revpar_lift_pct:+.1f}%"
        result["elasticity_used"]     = er.elasticity_used
        result["elasticity_profile"]  = er.elasticity_profile
        result["max_price_premium"]   = er.max_price_premium
        result["optimal_occupancy_target"] = er.optimal_occupancy
        result["ml_enabled"]          = er.ml_enabled
        result["ml_elasticity_multiplier"] = er.ml_elasticity_multiplier
        result["ml_premium_delta"]    = er.ml_premium_delta
        result["ml_occupancy_delta"]  = er.ml_occupancy_delta
        result["ml_state_version"]    = er.ml_state_version
        result["ancillary_profile"]   = er.ancillary_profile
        result["ancillary_ratio_used"] = er.ancillary_ratio_used
        result["ancillary_margin_used"] = er.ancillary_margin_used
        result["ancillary_per_occ"]   = er.ancillary_per_occ
        result["optimization_objective"] = "light_trevpar"
        result["elasticity_source"]   = er.data_source

    # ── Phase 3：HROS V6 RevPAR 真实弹性优化 ────────────────────────────────
    if _V6_OK and result.get("recommended_price", 0) > 0:
        try:
            _v6_mkt = (float(sum(real_prices) / len(real_prices))
                       if real_prices else hotel["base_price"])
            _guardrail = _hotel_settings(hotel)
            _v6_floor = float(_guardrail.floor_price)
            _v6_ceil  = max(float(_guardrail.ceiling_price), _v6_floor * 1.05)
            _v6_occ   = scenario.occupancy
            _v6_dq    = 1.0 if real_prices else 0.6
            result = apply_v6_to_mare_output(
                result,
                star_rating      = hotel.get("star", 3),
                market_price     = max(_v6_mkt, _v6_floor),
                base_occ         = _v6_occ,
                floor_price      = _v6_floor,
                ceiling_price    = _v6_ceil,
                demand_state     = result.get("demand_state", signal.get("demand_state", "NORMAL")),
                competitor_price = competitor_price,
                data_quality     = _v6_dq,
            )
            result["hotel_profile_version"] = "V6"
            result = attach_revenue_attribution(
                result,
                baseline_adr=max(_v6_mkt, _v6_floor),
                baseline_occ=_v6_occ,
                mare_adr=float(result.get("recommended_price", 0.0) or 0.0),
                mare_occ=float(result.get("predicted_occupancy", _v6_occ) or _v6_occ),
                rooms_available=1.0,
            )
        except Exception as _v6e:
            result["hotel_profile_version"] = "V5_fallback"

    return result


# ── 4-5星自主获客模型测试 ──────────────────────────────────────────────────────
def run_45star_test(hotel: dict, signal: dict, real_data: dict,
                    scenario: HotelScenario) -> dict:
    """
    测试自主获客/OTA脱依赖模型（4-5星）。
    核心验证：在不同ObjectiveMode下，模型是否能为高净值客户
    给出优于OTA标准定价的直销方案。
    场景注入：竞对价格乘数、入住率、客户画像均来自scenario。
    """
    from objective_modes import OBJECTIVE_PROFILES

    base = hotel["base_price"]
    occupancy = scenario.occupancy

    # OTA参考价：Booking.com实时4-5星均价 × 场景竞对系数
    real_45_avg = real_data.get("upper_tier_adr_real")
    if real_45_avg and real_45_avg > 0:
        ota_standard_price = max(base * 0.70,
                                 real_45_avg * scenario.competitor_price_multiplier
                                 * random.uniform(0.96, 1.04))
    else:
        ota_standard_price = base * scenario.competitor_price_multiplier * random.uniform(0.95, 1.05)

    # 客户画像：优先使用场景定义的细分和忠诚度
    segment = scenario.guest_segment
    clv = scenario.avg_clv
    loyalty = scenario.loyalty_tier

    # 对于4-5星专属VIP场景，如果场景是普通budget，映射到对应高端客群
    if segment == "budget" and hotel["star"] == 5:
        segment = "luxury_leisure"
        clv = max(clv, 8000)

    guest_profile = {
        "segment":     segment,
        "clv":         clv,
        "sensitivity": "low" if loyalty in ("platinum", "gold") else "medium",
        "loyalty":     loyalty,
    }

    # 根据细分市场选择目标模式
    if guest_profile["sensitivity"] == "low":
        mode = ObjectiveMode.MAXIMIZE_DIRECT_MIX
    elif occupancy < 0.65:
        mode = ObjectiveMode.MAXIMIZE_REVPAR
    else:
        mode = ObjectiveMode.MAXIMIZE_REVENUE

    weights = get_objective_weights(mode)

    # 计算直销调整幅度
    direct_bias = weights.direct_bias
    bundle_aggr = weights.bundle_aggressiveness

    # 直销价格逻辑：高净值 -> 专属折扣 + 增值服务抵消
    if guest_profile["loyalty"] in ("platinum", "gold"):
        direct_price_discount = 0.08 + direct_bias * 0.10
    else:
        direct_price_discount = 0.03 + direct_bias * 0.05

    # ── 修复：直销定价锚定真实市场价（而非 base_price）──────────────────
    # 当有真实OTA市场数据时，直销报价应与市场价竞争，而非与成本基准比较。
    # base_price 仅作为下限保护（防止直销报价低于运营成本）。
    if real_data.get("upper_tier_adr_real") and real_data["upper_tier_adr_real"] > 0:
        # 以OTA标准价为锚，给出有竞争力的直销优惠（通常低于OTA 3-8%）
        price_anchor = ota_standard_price
    else:
        price_anchor = base

    direct_offer_price = round(max(base * 1.05,                         # 不低于成本基准+5%
                                   price_anchor * (1.0 - direct_price_discount)))

    # 判断直销是否优于OTA（核心逻辑验证）
    ota_commission_rate = 0.18  # OTA佣金约18%
    ota_net_revenue = ota_standard_price * (1 - ota_commission_rate)
    direct_net_revenue = direct_offer_price  # 直销无佣金

    direct_wins = direct_net_revenue >= ota_net_revenue * 0.92

    result = {
        "hotel_id": hotel["hotel_id"],
        "scenario": scenario.name,
        "scenario_category": scenario.category,
        "guest_segment": guest_profile["segment"],
        "loyalty_tier": guest_profile["loyalty"],
        "objective_mode": mode.value,
        "ota_standard_price": round(ota_standard_price, 0),
        "ota_net_revenue": round(ota_net_revenue, 0),
        "direct_offer_price": direct_offer_price,
        "direct_net_revenue": round(direct_net_revenue, 0),
        "direct_wins_vs_ota": direct_wins,
        "direct_bias": direct_bias,
        "bundle_aggressiveness": bundle_aggr,
        "occupancy": occupancy,
        "demand_high": signal["is_holiday"] or signal["is_weekend"] or signal.get("mha_demand_state") == "HIGH",
    }

    result = apply_selfacq_profit_guard(result, hotel, scenario)
    direct_offer_price = float(result.get("direct_offer_price", direct_offer_price))
    direct_net_revenue = float(result.get("direct_net_revenue", direct_net_revenue))
    direct_wins = bool(result.get("direct_wins_vs_ota", direct_wins))

    # ── Phase 2：弹性引擎验证 SelfACQ 直销价格合理性 ─────────────────────────
    if _ELASTICITY_OK and direct_offer_price > 0:
        mkt_price = float(real_data.get("upper_tier_adr_real") or base)
        er = _elasticity_optimize(
            candidate_price = direct_offer_price,
            market_price    = mkt_price,
            star            = hotel.get("star", 4),
            district        = hotel.get("district", "NAPE"),
            demand_level    = "HIGH" if result["demand_high"] else signal.get("mha_demand_state", "NORMAL"),
            season          = signal.get("season", "normal"),
            hotel_id        = hotel.get("hotel_id"),
        )
        result["elasticity_validated_price"] = er.optimal_price
        result["predicted_occupancy"]        = er.predicted_occupancy
        result["revpar_lift_vs_market"]      = f"{er.revpar_lift_pct:+.1f}%"
        result["elasticity_used"]            = er.elasticity_used

    # ── Phase 3：V6 SelfACQ LTV 评估 ─────────────────────────────────────────
    if _V6_OK:
        try:
            _v6_acq_out = _SelfACQV6().evaluate(
                direct_price          = direct_offer_price,
                ota_gross_price       = ota_standard_price,
                ota_commission_rate   = ota_commission_rate,
                direct_conversion_prob= 0.25 if loyalty in ("platinum","gold") else 0.15,
                repeat_probability    = {"platinum":0.55,"gold":0.40,"silver":0.25,"bronze":0.12,"none":0.05}.get(loyalty, 0.05),
                future_margin         = hotel["base_price"] * 0.35,
                acquisition_cost      = 80.0 if loyalty == "none" else 20.0,
            )
            result["selfacq_v6_ota_net"]       = _v6_acq_out["ota_net_revenue"]
            result["selfacq_v6_direct_ltv"]    = _v6_acq_out["expected_direct_ltv"]
            result["selfacq_v6_advantage"]     = _v6_acq_out["direct_advantage"]
            result["selfacq_v6_wins"]          = _v6_acq_out["direct_wins"]
            result["hotel_profile_version"]    = "V6"
            result = adapt_selfacq_to_v6_result(result)
            result = attach_revenue_attribution(
                result,
                baseline_adr=ota_standard_price,
                baseline_occ=occupancy,
                mare_adr=ota_standard_price,
                mare_occ=occupancy,
                selfacq_incremental_value=max(0.0, direct_net_revenue - ota_net_revenue),
                rooms_available=1.0,
            )
        except Exception:
            result["hotel_profile_version"]    = "V5_fallback"

    return result


# ── DirectorAI CRM集成模型测试（3星）────────────────────────────────────────
def run_director_crm_test(hotel: dict, signal: dict, real_data: dict,
                          scenario: HotelScenario,
                          source_system: str = "S2",
                          run_at: str | None = None,
                          sim_hour: int = -1) -> dict:
    """
    测试DirectorAI CRM/PSRS集成模型在3星酒店的表现。
    内部数据（渠道分布/PSRS状态/CRM识别率/客户忠诚度）来自scenario，
    覆盖"数据缺失"到"系统故障"到"理想状态"的全谱运营条件。
    """
    occupancy = scenario.occupancy

    # ── 渠道来源（使用场景定义的分布，而非固定随机）──────────────────
    channel_labels = ["direct_web", "ota_booking", "ota_agoda",
                      "walk_in", "whatsapp_direct", "phone_call"]
    channel = random.choices(channel_labels, weights=list(scenario.channel_weights), k=1)[0]

    # ── 回头客与忠诚度（来自场景，体现不同客群结构）─────────────────
    loyalty_tier = scenario.loyalty_tier
    is_returning = loyalty_tier != "none"
    clv_estimate = round(scenario.avg_clv * random.uniform(0.85, 1.15))

    # ── CRM匹配率（场景可强制覆盖，测试CRM失效/故障场景）────────────
    if scenario.crm_match_rate_override is not None:
        crm_match_prob = scenario.crm_match_rate_override
    else:
        base_rate = 0.55 if is_returning else 0.08
        crm_match_prob = min(0.92, base_rate * (1.12 if signal["is_holiday"] else 1.0))
    feedback_signal = get_director_feedback_signal(hotel["hotel_id"])
    crm_match_prob = max(0.02, min(0.96, crm_match_prob + feedback_signal.match_rate_delta))
    crm_matched = random.random() < crm_match_prob

    loyalty_alias = {
        "vip": "platinum",
        "diamond": "platinum",
    }
    loyalty_key = loyalty_alias.get(loyalty_tier, loyalty_tier)

    # ── 增值销售（满房/无库存场景则无法升房）────────────────────────
    upsell_menu = {
        "room_upgrade":      {"prob": 0.22, "revenue": 150},
        "late_checkout":     {"prob": 0.32, "revenue": 80},
        "breakfast_package": {"prob": 0.38, "revenue": 120},
        "airport_transfer":  {"prob": 0.18, "revenue": 200},
    }
    upsell_type, upsell_accepted, upsell_revenue = None, False, 0
    if occupancy < 0.95 and scenario.remaining_inventory_ratio > 0.02:
        upsell_type = random.choice(list(upsell_menu.keys()))
        accept_p = upsell_menu[upsell_type]["prob"]
        loyalty_bonus = {"platinum": 1.45, "gold": 1.35, "silver": 1.18,
                         "bronze": 1.08, "none": 1.0}[loyalty_key]
        upsell_accepted = random.random() < min(0.75, accept_p * loyalty_bonus)
        upsell_revenue = upsell_menu[upsell_type]["revenue"] if upsell_accepted else 0

    # ── PSRS状态（场景驱动：healthy/degraded/error）──────────────────
    if scenario.psrs_health == "healthy":
        psrs_status = random.choices(["synced", "pending", "error"], weights=[88, 9, 3])[0]
    elif scenario.psrs_health == "degraded":
        psrs_status = random.choices(["synced", "pending", "error"], weights=[45, 40, 15])[0]
    else:
        psrs_status = random.choices(["synced", "pending", "error"], weights=[8, 25, 67])[0]

    # ── WhatsApp触达（PSRS故障时推送中断）───────────────────────────
    whatsapp_eligible = (channel in ("whatsapp_direct", "direct_web")
                         or (crm_matched and loyalty_tier not in ("none",)))
    whatsapp_sent = whatsapp_eligible
    delivery_rate = {"synced": 0.94, "pending": 0.62, "error": 0.18}[psrs_status]
    whatsapp_delivered = whatsapp_sent and random.random() < delivery_rate

    # ── 直销佣金节省 ──────────────────────────────────────────────────
    direct_ch = ("direct_web", "whatsapp_direct", "phone_call")
    ota_commission_saved = round(hotel["base_price"] * 0.185) if channel in direct_ch else 0

    # ── CRM忠诚度调价 ─────────────────────────────────────────────────
    discount_map = {"platinum": 0.10, "gold": 0.08, "silver": 0.04, "bronze": 0.02, "none": 0.0}
    if feedback_signal.samples >= 6:
        discount_map = {
            key: max(0.0, min(0.12, value + feedback_signal.discount_bias_delta))
            for key, value in discount_map.items()
        }
    crm_adjusted_price = round(hotel["base_price"] * (1.0 - discount_map[loyalty_key]))

    # ── V6 CRM 护栏：防止折扣过深摧毁 ADR ───────────────────────────────────
    if _V6_OK:
        try:
            _crm_proposed = discount_map[loyalty_key]
            _crm_incr = max(0.0, clv_estimate - hotel["base_price"] * 0.5)
            _crm_guard = _CRMGuard().cap_discount(
                star_rating       = hotel.get("star", 3),
                proposed_discount = _crm_proposed,
                incremental_value = _crm_incr,
            )
            crm_adjusted_price = round(hotel["base_price"] * (1.0 - _crm_guard["discount"]))
            result_crm_guardrail = _crm_guard
        except Exception:
            result_crm_guardrail = {"discount": discount_map[loyalty_key], "reason": "V6_error"}
    else:
        result_crm_guardrail = {"discount": discount_map[loyalty_key], "reason": "V5_only"}

    # ── 集成健康评分（0-1）────────────────────────────────────────────
    integration_score = round(
        (0.40 if crm_matched else 0.0) +
        (0.25 if psrs_status == "synced" else 0.08 if psrs_status == "pending" else 0.0) +
        (0.20 if whatsapp_delivered else 0.0) +
        (0.15 if upsell_accepted else 0.05),
        3,
    )

    result = {
        "hotel_id":             hotel["hotel_id"],
        "scenario":             scenario.name,
        "scenario_category":    scenario.category,
        "channel":              channel,
        "is_returning_guest":   is_returning,
        "loyalty_tier":         loyalty_tier,
        "clv_estimate":         clv_estimate,
        "crm_matched":          crm_matched,
        "upsell_type":          upsell_type,
        "upsell_accepted":      upsell_accepted,
        "upsell_revenue":       upsell_revenue,
        "psrs_status":          psrs_status,
        "whatsapp_sent":        whatsapp_sent,
        "whatsapp_delivered":   whatsapp_delivered,
        "crm_adjusted_price":   crm_adjusted_price,
        "ota_commission_saved": ota_commission_saved,
        "integration_score":    integration_score,
        "occupancy":            occupancy,
        "demand_high":          signal["is_holiday"] or signal["is_weekend"] or signal.get("mha_demand_state") == "HIGH",
        "crm_v6_guardrail":     result_crm_guardrail,
        "director_feedback_avg": round(feedback_signal.avg_score, 4),
        "director_feedback_samples": feedback_signal.samples,
    }

    # ── Phase 2：弹性引擎验证 CRM 调价的 RevPAR 合理性 ───────────────────────
    if _ELASTICITY_OK and crm_adjusted_price > 0:
        mkt_price = hotel["base_price"]   # CRM以base_price为锚
        er = _elasticity_optimize(
            candidate_price = crm_adjusted_price,
            market_price    = mkt_price,
            star            = hotel.get("star", 3),
            district        = hotel.get("district", "NAPE"),
            demand_level    = "HIGH" if result["demand_high"] else "NORMAL",
            season          = signal.get("season", "normal"),
            hotel_id        = hotel.get("hotel_id"),
        )
        result["elasticity_revpar"]       = er.predicted_revpar
        result["elasticity_lift_pct"]     = er.true_lift_pct
        result["elasticity_used"]         = er.elasticity_used
        # CRM价格本身不覆盖（CRM有其忠诚度折扣逻辑），仅记录RevPAR验证结果

    if _V6_OK:
        result = adapt_director_to_v6_result(result)
        result = attach_revenue_attribution(
            result,
            baseline_adr=hotel["base_price"],
            baseline_occ=occupancy,
            mare_adr=hotel["base_price"],
            mare_occ=occupancy,
            crm_incremental_value=max(
                0.0,
                float(upsell_revenue or 0.0)
                + float(ota_commission_saved or 0.0)
                + max(0.0, float(crm_adjusted_price - hotel["base_price"])) * occupancy,
            ),
            rooms_available=1.0,
        )

    record_director_outcome(
        source_system=source_system,
        run_at=run_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        sim_hour=sim_hour,
        hotel_id=hotel["hotel_id"],
        scenario_name=scenario.name,
        scenario_category=scenario.category,
        base_price=float(hotel["base_price"]),
        crm_adjusted_price=float(crm_adjusted_price),
        integration_score=float(integration_score),
        crm_matched=crm_matched,
        psrs_status=psrs_status,
        whatsapp_delivered=whatsapp_delivered,
        upsell_revenue=float(upsell_revenue),
        ota_commission_saved=float(ota_commission_saved),
        payload=result,
    )

    return result


# ── CrewAI兼容别名（run_23star_test已重命名为run_3star_test）────────────────────
run_23star_test = run_3star_test


# ── 异常检测 ───────────────────────────────────────────────────────────────────
def detect_anomalies(hotel: dict, result: dict, signal: dict, model_type: str) -> list[str]:
    anomalies = []

    if model_type == "MARE_ALL":
        rec_price = result.get("recommended_price", 0)
        base = hotel["base_price"]
        star = hotel.get("star", 3)
        floor_warn = {3: 350, 4: 650, 5: 1000}.get(star, 350)

        if rec_price <= 0:
            anomalies.append("CRITICAL: 推荐价格为零或负数")
        if rec_price > base * 2.5:
            anomalies.append(f"WARN: 价格异常高 MOP {rec_price} (>{base*2.5:.0f} = 2.5x基础价)")
        if rec_price < base * 0.5:
            anomalies.append(f"WARN: 价格异常低 MOP {rec_price} (<{base*0.5:.0f} = 0.5x基础价)")
        if rec_price < floor_warn:
            anomalies.append(f"WARN: 价格低于{star}★市场底线 MOP {rec_price} (底线={floor_warn})")

        # Guardrail违规
        violations = result.get("guardrail_report", {}).get("violations", [])
        if violations:
            anomalies.append(f"GUARDRAIL: {len(violations)}项规则违反: {[v.get('rule','?') for v in violations[:3]]}")

    elif model_type == "DIRECTOR_CRM_ALL":
        if result.get("psrs_status") == "error":
            anomalies.append("CRITICAL: PSRS系统同步失败—预订数据未入库")
        if result.get("integration_score", 1.0) < 0.25:
            anomalies.append(f"WARN: CRM集成评分过低 {result.get('integration_score')}")
        crm_price = result.get("crm_adjusted_price", 0)
        base = hotel["base_price"]
        if crm_price < base * 0.85:
            anomalies.append(f"WARN: CRM忠诚价格折扣异常 MOP {crm_price} (低于基础价85%)")
        if result.get("whatsapp_sent") and not result.get("whatsapp_delivered"):
            anomalies.append("WARN: WhatsApp发送失败—客户触达中断")

    elif model_type == "SELFACQ_ALL":
        if not result.get("direct_wins_vs_ota", True):
            anomalies.append("LOGIC: 直销净收益低于OTA净收益—自主获客模型失效")
        dp = result.get("direct_offer_price", 0)
        base = hotel["base_price"]
        if dp < base * 0.60:
            anomalies.append(f"WARN: 直销报价过低 MOP {dp} (低于基础价60%)")

    return anomalies


# ── 每日汇总 ───────────────────────────────────────────────────────────────────
def write_daily_summary(conn: sqlite3.Connection, day: int):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    start_hour = day * 24
    end_hour = start_hour + 24

    c = conn.cursor()
    rows_mare = c.execute(
        "SELECT rec_price FROM hourly_runs WHERE model_type IN ('MARE_3_STAR','MARE_ALL') AND sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchall()
    rows_crm = c.execute(
        "SELECT rec_price FROM hourly_runs WHERE model_type IN ('DIRECTOR_CRM_3_STAR','DIRECTOR_CRM_ALL') AND sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchall()
    rows_acq = c.execute(
        "SELECT rec_price FROM hourly_runs WHERE model_type IN ('SELFACQ_45_STAR','SELFACQ_ALL') AND sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchall()
    anomaly_count = c.execute(
        "SELECT COUNT(*) FROM hourly_runs WHERE anomaly != '' AND sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchone()[0]
    failure_count = c.execute(
        "SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%EXCEPTION%' AND sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchone()[0]
    total_runs = c.execute(
        "SELECT COUNT(*) FROM hourly_runs WHERE sim_hour BETWEEN ? AND ?",
        (start_hour, end_hour - 1),
    ).fetchone()[0]

    avg_mare = sum(r[0] for r in rows_mare if r[0]) / len(rows_mare) if rows_mare else 0
    avg_crm  = sum(r[0] for r in rows_crm  if r[0]) / len(rows_crm)  if rows_crm  else 0
    avg_acq  = sum(r[0] for r in rows_acq  if r[0]) / len(rows_acq)  if rows_acq  else 0

    score_rows = c.execute(
        """
        SELECT model_type, output_json, anomaly, exp_lift
        FROM hourly_runs
        WHERE sim_hour BETWEEN ? AND ?
        """,
        (start_hour, end_hour - 1),
    ).fetchall()

    score_buckets: dict[tuple[str, str], list[tuple[dict, str, str]]] = {}
    for model_type, output_json, anomaly, exp_lift in score_rows:
        try:
            out = json.loads(output_json or "{}")
        except Exception:
            out = {}
        scenario_category = out.get("scenario_category", "normal")
        scenario_band = "extreme" if scenario_category in EXTREME_CATEGORIES else "normal"
        model_family = (
            "MARE" if "MARE" in model_type
            else "DIRECTOR" if "DIRECTOR" in model_type
            else "SELFACQ"
        )
        score_buckets.setdefault((model_family, scenario_band), []).append((out, anomaly or "", exp_lift or ""))

    selection_scores = []
    heuristics = {
        "MARE": {"real": 0.78, "explain": 0.86, "cost": 0.84},
        "DIRECTOR": {"real": 0.72, "explain": 0.92, "cost": 0.90},
        "SELFACQ": {"real": 0.68, "explain": 0.84, "cost": 0.91},
    }
    for (model_family, scenario_band), items in sorted(score_buckets.items()):
        sample_size = len(items)
        failure_n = sum(1 for _, anomaly, _ in items if "EXCEPTION" in anomaly)
        anomaly_n = sum(1 for _, anomaly, _ in items if anomaly.strip())
        uplifts = []
        for out, _, exp_lift in items:
            if model_family == "SELFACQ":
                ota_profit = float(out.get("ota_net_profit") or out.get("ota_net_revenue") or 0.0)
                direct_profit = float(out.get("direct_net_profit_after_cac") or out.get("direct_net_revenue") or 0.0)
                if ota_profit > 0:
                    uplifts.append((direct_profit - ota_profit) / ota_profit * 100.0)
            else:
                lift_val = parse_pct_text(out.get("expected_revenue_lift") or exp_lift)
                uplifts.append(lift_val)
        profit_uplift_pct = sum(uplifts) / len(uplifts) if uplifts else 0.0
        failure_rate_pct = failure_n / max(sample_size, 1) * 100.0
        anomaly_rate_pct = anomaly_n / max(sample_size, 1) * 100.0
        total_score = compute_dual_score(
            profit_uplift_pct=profit_uplift_pct,
            failure_rate_pct=failure_rate_pct,
            anomaly_rate_pct=anomaly_rate_pct,
            real_data_ratio=heuristics[model_family]["real"],
            explainability_ratio=heuristics[model_family]["explain"],
            runtime_cost_ratio=heuristics[model_family]["cost"],
        )
        notes = f"{model_family}:{scenario_band}"
        c.execute(
            """
            INSERT INTO model_selection_scores (
                run_at, model_family, scenario_band, sample_size, total_score,
                profit_uplift_pct, failure_rate_pct, anomaly_rate_pct,
                real_data_ratio, explainability_ratio, runtime_cost_ratio, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                date_str,
                model_family,
                scenario_band,
                sample_size,
                total_score,
                round(profit_uplift_pct, 2),
                round(failure_rate_pct, 2),
                round(anomaly_rate_pct, 2),
                heuristics[model_family]["real"],
                heuristics[model_family]["explain"],
                heuristics[model_family]["cost"],
                notes,
            ),
        )
        selection_scores.append(
            {
                "model_family": model_family,
                "scenario_band": scenario_band,
                "sample_size": sample_size,
                "total_score": total_score,
                "profit_uplift_pct": round(profit_uplift_pct, 2),
                "failure_rate_pct": round(failure_rate_pct, 2),
                "anomaly_rate_pct": round(anomaly_rate_pct, 2),
            }
        )

    summary = {
        "day":                    day + 1,
        "date":                   date_str,
        "runs":                   total_runs,
        "failures":               failure_count,
        "anomalies":              anomaly_count,
        "failure_rate":           round((failure_count / total_runs * 100), 1) if total_runs else 0.0,
        "anomaly_rate":           round((anomaly_count / total_runs * 100), 1) if total_runs else 0.0,
        "avg_mare_price":         round(avg_mare, 1),
        "avg_crm_price":          round(avg_crm, 1),
        "avg_selfacq_offer":      round(avg_acq, 1),
        "hotels_3star":           len(HOTELS_3_STAR),
        "hotels_45star":          len(HOTELS_45_STAR),
        "health":                 "OK" if anomaly_count == 0 else f"{anomaly_count} issues",
        "selection_scores":       selection_scores,
    }

    c.execute(
        "INSERT OR REPLACE INTO daily_summaries VALUES (?,?,?,?,?,?,?)",
        (day, date_str, round(avg_mare, 1), round(avg_acq, 1), anomaly_count, total_runs, json.dumps(summary)),
    )
    conn.commit()
    return summary


# ── 主循环 ────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 澳门酒店AI模型模拟测试启动")
    print(f"  目标：{TOTAL_HOURS}小时 ({TOTAL_HOURS // 24}天)")
    print(f"  3★酒店（澳门旅游局官方）：{len(HOTELS_3_STAR)}家 (每小时MARE+CRM各{len(HOTELS_3_STAR)}次)")
    print(f"  4-5★酒店（澳门旅游局官方）：{len(HOTELS_45_STAR)}家 (每小时自主获客{len(HOTELS_45_STAR)}次)")
    total_calls = (len(HOTELS_3_STAR) * 2 + len(HOTELS_45_STAR)) * TOTAL_HOURS
    print(f"  21天总调用次数：{total_calls:,}次")
    print(get_scenario_stats())
    print(f"  结果数据库：{DB_PATH}")
    print()

    # 写PID文件（方便kill）
    PID_FILE.write_text(str(os.getpid()))

    conn = init_db()

    # ── 断点续传：从数据库中最大 sim_hour+1 开始 ──────────────────────────────
    resume_hour = 0
    try:
        row = conn.execute("SELECT MAX(sim_hour) FROM hourly_runs").fetchone()
        if row and row[0] is not None:
            resume_hour = int(row[0]) + 1
            print(f"  [RESUME] 检测到已有数据，从第{resume_hour}小时继续（已完成{resume_hour}h/{TOTAL_HOURS}h）")
    except Exception:
        pass

    # P1 FIX: 模拟完成后重启不产生空循环 CPU空转
    if resume_hour >= TOTAL_HOURS:
        print(f"[DONE] 模拟已全部完成（{TOTAL_HOURS}小时），正常退出。")
        conn.close()
        sys.exit(0)

    for hour in range(resume_hour, TOTAL_HOURS):
        run_start = datetime.now()
        run_at_str = run_start.strftime("%Y-%m-%d %H:%M:%S")

        # 抓取实时数据（天气、渡轮、Booking.com房价、活动）
        checkin = (run_start + timedelta(days=1)).strftime("%Y-%m-%d")
        checkout = (run_start + timedelta(days=2)).strftime("%Y-%m-%d")
        try:
            real_data = get_all_real_signals(checkin, checkout)
        except Exception as e:
            print(f"  [WARN] 实时数据抓取失败，使用默认值: {e}")
            real_data = {}

        # 生成市场信号（合并真实数据+统计模拟）
        signal = get_macau_market_signal(hour, real_data)
        weather_c = real_data.get("weather_celsius", 25.0)

        # 入住率由各酒店的场景决定（scenario.occupancy），不再全局统一计算
        hour_results = []

        def _insert(model_type, hotel, out_json, rec_price, demand_state, conf, lift, anomalies):
            conn.execute(
                "INSERT INTO hourly_runs VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_at_str, hour, hotel["hotel_id"], hotel["name"], model_type,
                 signal["season"],
                 json.dumps({k: v for k, v in signal.items() if k != "weather_celsius"}),
                 json.dumps(out_json),
                 rec_price, demand_state, conf, lift,
                 "; ".join(anomalies),
                 weather_c, int(signal["is_holiday"]), int(signal["is_weekend"])),
            )
            if anomalies:
                for a in anomalies:
                    conn.execute("INSERT INTO anomaly_log VALUES (NULL,?,?,?,?)",
                                 (run_at_str, hotel["hotel_id"], model_type, a))

        def _insert_err(model_type, hotel, exc):
            msg = f"EXCEPTION: {type(exc).__name__}: {exc}"
            conn.execute(
                "INSERT INTO hourly_runs VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_at_str, hour, hotel["hotel_id"], hotel["name"], model_type,
                 signal["season"], "{}", "{}",
                 None, "ERROR", "None", "N/A",
                 msg,
                 weather_c, int(signal["is_holiday"]), int(signal["is_weekend"])),
            )
            hour_results.append((model_type, hotel["hotel_id"], 0, [msg]))

        # ── 动态base_price：真实BAR/OTA + DSEC背景 ────────────────────────
        cur_month = run_start.month
        # 从real_data获取市场OTA参考价（作为实时OTA输入）
        _ota_ref_3 = float(sum(real_data["booking_prices_3"]) / len(real_data["booking_prices_3"])) \
            if real_data.get("booking_prices_3") else 1000.0
        _ota_ref_45 = float(real_data["upper_tier_adr_real"]) \
            if real_data.get("upper_tier_adr_real") else 2000.0

        # ── 模型1：MARE房价优化（全部76家 × 14场景轮换）────────────────
        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            # 每小时动态计算base_price（替代固定随机数）
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_3
            hotel = dict(hotel)  # 浅拷贝，不修改原始列表
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, cur_month
            )
            # 4-5星酒店用高端竞对价格（upper_tier_adr）替代3星基准
            if hotel["star"] >= 4 and real_data.get("upper_tier_adr_real"):
                rd = dict(real_data)
                rd["booking_prices_3"] = [real_data["upper_tier_adr_real"]]
            else:
                rd = real_data
            try:
                result = run_3star_test(hotel, signal, rd, scenario)
                anomalies = detect_anomalies(hotel, result, signal, "MARE_ALL")
                if _MARE_ML_FEEDBACK_OK and result.get("ml_enabled"):
                    try:
                        from mare_ml_layer import MareMLDecision
                        decision = MareMLDecision(
                            profile_name=str(result.get("elasticity_profile") or "unknown"),
                            elasticity_multiplier=float(result.get("ml_elasticity_multiplier") or 1.0),
                            premium_delta=float(result.get("ml_premium_delta") or 0.0),
                            occupancy_delta=float(result.get("ml_occupancy_delta") or 0.0),
                            state_version=int(result.get("ml_state_version") or 0),
                        )
                        _update_mare_adjustments(decision, _score_mare_outcome(result, anomalies))
                    except Exception:
                        pass
                rec_price = result.get("recommended_price", 0)
                mare_output = dict(result)
                mare_output.update({
                    "scenario": scenario.name,
                    "scenario_category": scenario.category,
                    "occupancy": scenario.occupancy,
                    "days_to_arrival": scenario.days_to_arrival,
                    "competitor_mult": scenario.competitor_price_multiplier,
                })
                _insert("MARE_ALL", hotel, mare_output,
                        rec_price, result.get("demand_state"),
                        result.get("confidence"), result.get("expected_revenue_lift"),
                        anomalies)
                hour_results.append(("MARE", hotel["hotel_id"], rec_price, anomalies))
            except Exception as e:
                _insert_err("MARE_ALL", hotel, e)

        # ── 模型2：DirectorAI CRM/PSRS集成（全部76家 × 14场景）────────
        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_3
            hotel = dict(hotel)
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, cur_month
            )
            try:
                result = run_director_crm_test(
                    hotel,
                    signal,
                    real_data,
                    scenario,
                    source_system="S2",
                    run_at=run_at_str,
                    sim_hour=hour,
                )
                anomalies = detect_anomalies(hotel, result, signal, "DIRECTOR_CRM_ALL")
                crm_price = result.get("crm_adjusted_price", 0)
                crm_output = dict(result)
                crm_output.update({
                    "scenario_category": scenario.category,
                    "psrs_health_input": scenario.psrs_health,
                })
                _insert("DIRECTOR_CRM_ALL", hotel, crm_output,
                        crm_price, signal.get("demand_state", "NORMAL"),
                        ("High" if result.get("integration_score", 0) >= 0.7
                         else "Medium" if result.get("integration_score", 0) >= 0.4
                         else "Low"),
                        str(result.get("upsell_revenue", 0)),
                        anomalies)
                hour_results.append(("CRM", hotel["hotel_id"], crm_price, anomalies))
            except Exception as e:
                _insert_err("DIRECTOR_CRM_ALL", hotel, e)

        # ── 模型3：自主获客/OTA脱依赖（全部76家 × 14场景）──────────────
        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_3
            hotel = dict(hotel)
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, cur_month
            )
            try:
                result = run_45star_test(hotel, signal, real_data, scenario)
                anomalies = detect_anomalies(hotel, result, signal, "SELFACQ_ALL")
                direct_price = result.get("direct_offer_price", 0)
                selfacq_output = dict(result)
                selfacq_output.update({
                    "scenario": scenario.name,
                    "scenario_category": scenario.category,
                    "competitor_mult": scenario.competitor_price_multiplier,
                })
                _insert("SELFACQ_ALL", hotel, selfacq_output,
                        direct_price,
                        "HIGH" if result.get("demand_high") else "NORMAL",
                        ("High" if (result.get("direct_wins_vs_ota") and
                                    result.get("loyalty_tier") in ("platinum", "gold"))
                         else "Medium" if result.get("direct_wins_vs_ota")
                         else "Low"),
                        "N/A",
                        anomalies)
                hour_results.append(("ACQ", hotel["hotel_id"], direct_price, anomalies))
            except Exception as e:  # noqa
                _insert_err("SELFACQ_ALL", hotel, e)

        # ── V6 收益归因 + 需求状态逻辑审计 ─────────────────────────────────────
        if _V6_OK:
            try:
                # 收益归因
                _dsec_base_adr = {3: 901.0, 4: 1070.0, 5: 1454.0}
                _dsec_base_occ = 0.72
                _mare_avg  = sum(p for t,_,p,_ in hour_results if t=="MARE" and p) / max(1, sum(1 for t,_,p,_ in hour_results if t=="MARE" and p))
                _crm_avg   = sum(p for t,_,p,_ in hour_results if t=="CRM"  and p) / max(1, sum(1 for t,_,p,_ in hour_results if t=="CRM"  and p))
                _acq_avg   = sum(p for t,_,p,_ in hour_results if t=="ACQ"  and p) / max(1, sum(1 for t,_,p,_ in hour_results if t=="ACQ"  and p))
                _attr = _AttrEngine().attribute(
                    baseline_adr             = _dsec_base_adr.get(3, 901.0),
                    baseline_occ             = _dsec_base_occ,
                    mare_adr                 = _mare_avg,
                    mare_occ                 = _dsec_base_occ * 0.98,
                    crm_incremental_value    = max(0, _crm_avg - _dsec_base_adr[3]) * _dsec_base_occ,
                    selfacq_incremental_value= max(0, _acq_avg - _dsec_base_adr[3]) * _dsec_base_occ,
                    rooms_available          = len(ALL_HOTELS),
                )
                # 需求状态审计
                _audit = _V6Layer().validate_demand_state_logic(
                    high_avg   = _mare_avg,
                    normal_avg = _dsec_base_adr[3] * 1.05,
                )
                if _audit.get("flag"):
                    print(f"  [V6-AUDIT] 需求状态定价异常: HIGH均价低于NORMAL基准 {_audit['gap_pct']:+.1f}%")
                conn.execute(
                    "INSERT OR IGNORE INTO attribution_log VALUES (NULL,?,?,?,?,?,?)",
                    (run_at_str, hour,
                     round(_attr["baseline_revpar"],2), round(_attr["optimized_revpar"],2),
                     round(_attr["total_lift_pct"],2), json.dumps(_attr))
                )
            except Exception as _ae:
                pass

        conn.commit()

        # ── 控制台进度输出 ────────────────────────────────────────────────
        total_runs_hour, failure_count, anomaly_count = _summarize_hour_health(hour_results)
        prices_mare = [p for t, _, p, _ in hour_results if t == "MARE" and p]
        prices_crm  = [p for t, _, p, _ in hour_results if t == "CRM"  and p]
        prices_acq  = [p for t, _, p, _ in hour_results if t == "ACQ"  and p]
        avg_mare = sum(prices_mare) / len(prices_mare) if prices_mare else 0
        avg_crm  = sum(prices_crm)  / len(prices_crm)  if prices_crm  else 0
        avg_acq  = sum(prices_acq)  / len(prices_acq)  if prices_acq  else 0

        status_icon = "⚠" if anomaly_count else "✓"
        print(
            f"[{run_at_str}] 第{hour+1:03d}h {status_icon} "
            f"{weather_c:.0f}°C {signal['season']:10s} "
            f"{'假日' if signal['is_holiday'] else '平日'} | "
            f"MARE:{avg_mare:.0f} CRM:{avg_crm:.0f} ACQ:{avg_acq:.0f}"
            + (f" [异常:{anomaly_count}]" if anomaly_count else ""),
            flush=True,
        )

        # ── CRITICAL 告警推送（后台，不阻塞）────────────────────────────────
        criticals_this_hour = [
            a for _, _, _, anomalies in hour_results
            for a in (anomalies if isinstance(anomalies, list) else [anomalies])
            if a and "CRITICAL" in str(a)
        ]
        if criticals_this_hour:
            _alert_critical(hour, criticals_this_hour, avg_mare, avg_acq)

        # ── 模型表现快报（每6小时，不阻塞）──────────────────────────────────
        if (hour + 1) % 6 == 0:
            _push_metrics_snapshot(hour, avg_mare, avg_crm, avg_acq,
                                   total_runs_hour, failure_count, anomaly_count)

        # ── 每日汇总（整点小时写一次）────────────────────────────────────
        if (hour + 1) % 24 == 0:
            day_num = (hour + 1) // 24 - 1
            summary = write_daily_summary(conn, day_num)
            summary['day'] = day_num
            print(f"\n{'='*72}")
            print(f"  第 {day_num+1:02d} 天日报  {summary['date']}")
            print(f"  运行次数: {summary['runs']:,}  |  异常: {summary['anomalies']}  |  状态: {summary['health']}")
            print(f"  MARE房价优化   (73家3星): 均价 MOP {summary['avg_mare_price']}")
            print(f"  DirectorAI CRM (73家3星): CRM调价 MOP {summary['avg_crm_price']}")
            print(f"  自主获客       (280家4-5星): 直销价 MOP {summary['avg_selfacq_offer']}")
            print(f"{'='*72}\n")
            # 每日汇总推送企业微信（后台，不阻塞）
            _push_daily_summary(summary)

        # ── 等待下一小时 ──────────────────────────────────────────────────
        if hour < TOTAL_HOURS - 1:
            elapsed = (datetime.now() - run_start).total_seconds()
            sleep_time = max(0, SLEEP_SECONDS - elapsed)
            time.sleep(sleep_time)

    # ── 最终报告 ──────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  21天模拟测试完成 [{datetime.now():%Y-%m-%d %H:%M:%S}]")
    print(f"  数据库位置: {DB_PATH}")
    print()
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM hourly_runs").fetchone()[0]
    mare_n = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE model_type IN ('MARE_3_STAR','MARE_ALL')").fetchone()[0]
    crm_n  = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE model_type IN ('DIRECTOR_CRM_3_STAR','DIRECTOR_CRM_ALL')").fetchone()[0]
    acq_n  = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE model_type IN ('SELFACQ_45_STAR','SELFACQ_ALL')").fetchone()[0]
    errors = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%CRITICAL%'").fetchone()[0]
    warns  = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%WARN%'").fetchone()[0]
    print(f"  总运行次数: {total:,}")
    print(f"    ├ MARE房价优化      (3星): {mare_n:,}次")
    print(f"    ├ DirectorAI CRM   (3星): {crm_n:,}次")
    print(f"    └ 自主获客OTA脱依赖 (4-5星): {acq_n:,}次")
    print(f"  严重异常(CRITICAL): {errors}")
    print(f"  警告(WARN): {warns}")
    _exc_sql = "SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%EXCEPTION%'"
    exceptions = c.execute(_exc_sql).fetchone()[0]
    print(f"  功能性异常(EXCEPTION): {exceptions}")
    print(f"\n  运行下列命令查看完整报告:")
    print(f"    python3 report.py")
    print(f"{'='*70}\n")

    # ── 21天完成通知推送 ──────────────────────────────────────────────────
    _wecom_push_async(
        f"## 🎉 酒店AI模型 — 21天模拟测试完成！\n"
        f"**完成时间：** {datetime.now():%Y-%m-%d %H:%M}\n\n"
        f"| 项目 | 数量 |\n|------|------|\n"
        f"| 总运行次数 | {total:,} |\n"
        f"| MARE（3星） | {mare_n:,} |\n"
        f"| DirectorAI CRM | {crm_n:,} |\n"
        f"| 自主获客（4-5星） | {acq_n:,} |\n"
        f"| CRITICAL 异常 | {errors} |\n"
        f"| WARN 警告 | {warns} |\n\n"
        f"运行 `python3 report.py` 查看完整报告"
    )
    time.sleep(3)  # 等后台推送完成

    PID_FILE.unlink(missing_ok=True)
    conn.close()


if __name__ == "__main__":
    main()
