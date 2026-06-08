"""
澳门酒店AI模型 — CrewAI + Firecrawl + AgentOps 并行模拟
==========================================================
与 simulation_test/run_simulation.py 并行运行，用于对比验证。

核心差异（相比Playwright版本）：
  ① 用Firecrawl尝试抓取 border_flow / zhuhai_saturation / ota_booking_pace
  ② 用AgentOps监控每次Agent运行的"思考链"
  ③ 每小时生成Agent分析报告（定性+定量）
  ④ 结果存入 crewai_results.db，与 results.db 可直接对比

运行方法：
    cd "/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/system3_crewai"
    pip install -r requirements.txt
    python3 main.py
"""

from __future__ import annotations

import json, math, os, random, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path

# ── 环境配置 ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
MODEL_DIR = BASE_DIR.parent  # InsightBridge_九大模型_v2026/

sys.path.insert(0, str(MODEL_DIR))
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env", override=True)   # override=True 确保.env优先于系统环境变量

os.environ.setdefault("MODEL_WEIGHTS_PATH",
                      str(MODEL_DIR / "system2_claude_simulation" / "data" / "model_weights.json"))
sys.path.insert(0, str(MODEL_DIR / "system2_claude_simulation"))

# ── AgentOps 初始化（监控开关）───────────────────────────────────────
USE_AGENTOPS = False
agentops_key = os.getenv("AGENTOPS_API_KEY", "")
agentops_enabled = os.getenv("ENABLE_AGENTOPS", "").strip().lower() in {"1", "true", "yes", "on"}
if agentops_enabled and agentops_key and agentops_key != "your_agentops_key_here":
    try:
        import agentops
        agentops.init(agentops_key, instrument_llm_calls=False, default_tags=["macau-hotel", "crewai", "simulation"])
        USE_AGENTOPS = True
        print("✓ AgentOps 监控已启动")
    except Exception as e:
        print(f"⚠ AgentOps 初始化失败（继续运行）: {e}")

# ── HROS V6 引擎 ─────────────────────────────────────────────────────────────
_V6_ENGINE_DIR = BASE_DIR.parent / "共用_HROS_V6引擎"
_V6_OK = False
try:
    import sys as _sys_v6
    if str(_V6_ENGINE_DIR) not in _sys_v6.path:
        _sys_v6.path.insert(0, str(_V6_ENGINE_DIR))
    from hros_v6.integration_adapter import apply_v6_to_mare_output as _v6_mare
    from hros_v6.revenue_decision_layer_v6 import RevenueDecisionLayerV6 as _V6Layer
    from hros_v6.crm_guardrails import CRMGuardrails as _CRMGuard
    from hros_v6.selfacq_engine_v6 import SelfACQEngineV6 as _SelfACQV6
    from hros_v6.revenue_attribution_engine import RevenueAttributionEngine as _AttrEngine
    from hotel_learning_loop_pipeline import calibrate_all_hotels as _calibrate_hotels
    from hotel_learning_loop_pipeline import get_baseline_adr as _get_baseline_adr
    _V6_OK = True
except Exception as _v6_err:
    pass

# ── CrewAI（可选：无OpenAI则跳过LLM推理）────────────────────────────
USE_LLM = os.getenv("OPENAI_API_KEY", "") not in ("", "your_openai_key_here")
USE_CREWAI = True
try:
    from crewai import Crew, Process
    from agents import build_agents
    from tasks import build_tasks
except ImportError as e:
    print(f"⚠ CrewAI未安装，将仅运行数据采集+模型部分: {e}")
    USE_CREWAI = False

# ── 导入现有模型（复用simulation_test里的全部代码）──────────────────
from data_fetchers.real_data import get_all_real_signals
from data_fetchers.scenario_engine import get_scenario, get_scenario_stats, SCENARIOS
from tools.firecrawl_scrapers import get_all_firecrawl_signals
# Bug Fix: pricing_engine.py 内部 from app.services.* 需要 mare_engine/api 在 sys.path
_MARE_API_PATH = str(BASE_DIR.parent / "system1_chatgpt_harness" / "mare_engine" / "api")
if _MARE_API_PATH not in sys.path:
    sys.path.insert(0, _MARE_API_PATH)

import pricing_engine as pe
from objective_modes import ObjectiveMode, get_objective_weights
from recommendations import RecommendationRequest

# ── 复用酒店名单生成器 ────────────────────────────────────────────────
from run_simulation import (
    _build_hotel_roster, _jitter, MACAU_HOLIDAYS_2026,
    run_23star_test, run_director_crm_test, run_45star_test,
    detect_anomalies,
    compute_dynamic_base_price,   # DSEC×75% + OTA×25% 动态base_price
)

# ── Phase 2 弹性引擎 ──────────────────────────────────────────────────
_COLLECTOR_DIR_CREWAI = str(MODEL_DIR / "hotel_collector")
if _COLLECTOR_DIR_CREWAI not in sys.path:
    sys.path.insert(0, _COLLECTOR_DIR_CREWAI)
try:
    from elasticity_engine import optimize_price as _elasticity_optimize
    _ELASTICITY_OK_CREWAI = True
except ImportError:
    _ELASTICITY_OK_CREWAI = False
    def _elasticity_optimize(candidate_price, market_price, star, district="NAPE",
                              demand_level="NORMAL", season="normal", hotel_id=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            optimal_price=candidate_price, predicted_occupancy=0.72,
            predicted_revpar=candidate_price*0.72, baseline_revpar=market_price*0.72,
            true_lift_pct=0.0, elasticity_used=0.0, data_source="unavailable", search_steps=0
        )

# ── 切换为澳门旅游局官方76家真实酒店 ─────────────────────────────────────────
_SIM_DIR_MAIN = str(MODEL_DIR / "system2_claude_simulation")
if _SIM_DIR_MAIN not in sys.path:
    sys.path.insert(0, _SIM_DIR_MAIN)
try:
    from hotel_roster_76 import HOTELS_3STAR as HOTELS_23_STAR, HOTELS_45STAR as HOTELS_45_STAR, ALL_HOTELS_76 as ALL_HOTELS
except ImportError:
    HOTELS_23_STAR, HOTELS_45_STAR = _build_hotel_roster()   # 降级保底
    ALL_HOTELS = HOTELS_23_STAR + HOTELS_45_STAR

# ── 配置 ──────────────────────────────────────────────────────────────
TOTAL_HOURS  = 21 * 24   # 504小时
SLEEP_SECS   = 3600
DB_PATH      = BASE_DIR / "crewai_results.db"
LOG_PATH     = BASE_DIR / "crewai_simulation.log"
PID_FILE     = BASE_DIR / "crewai.pid"


# ── 数据库初始化 ───────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hourly_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT,
            sim_hour     INTEGER,
            hotel_id     TEXT,
            hotel_name   TEXT,
            model_type   TEXT,
            season       TEXT,
            input_json   TEXT,
            output_json  TEXT,
            rec_price    REAL,
            demand_state TEXT,
            confidence   TEXT,
            exp_lift     TEXT,
            anomaly      TEXT,
            weather_c    REAL,
            is_holiday   INTEGER,
            is_weekend   INTEGER
        );

        -- Firecrawl专属表：记录每小时抓取结果
        CREATE TABLE IF NOT EXISTS firecrawl_signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at           TEXT,
            sim_hour         INTEGER,
            border_flow_val  REAL,
            border_source    TEXT,
            zhuhai_val       REAL,
            zhuhai_source    TEXT,
            ota_pace_val     REAL,
            ota_pace_source  TEXT,
            agoda_avg        REAL,
            agoda_source     TEXT,
            raw_json         TEXT
        );

        -- 每小时CrewAI分析报告
        CREATE TABLE IF NOT EXISTS crewai_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT,
            sim_hour    INTEGER,
            report_json TEXT,
            duration_s  REAL
        );

        -- 与Playwright基线的对比记录
        CREATE TABLE IF NOT EXISTS comparison_log (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at               TEXT,
            sim_hour             INTEGER,
            -- MARE价格对比
            crewai_avg_mare      REAL,
            playwright_avg_mare  REAL,
            mare_diff_pct        REAL,
            -- border_flow对比
            crewai_border        REAL,
            simulated_border     REAL,
            border_diff          REAL,
            -- 数据覆盖率
            fc_coverage_pct      REAL,
            notes                TEXT
        );
    """)
    conn.commit()
    return conn


# ── 市场信号生成（复用simulation_test逻辑，合并FC增强）────────────────
def get_market_signal(sim_hour: int, real_data: dict, fc_data: dict) -> dict:
    now = datetime.now()
    hour_of_day = now.hour
    day_of_week = now.weekday()
    month = now.month
    date_str = now.strftime("%m-%d")

    is_weekend = day_of_week >= 5
    is_holiday = date_str in MACAU_HOLIDAYS_2026

    season_map = {1:"off_peak",2:"shoulder",3:"shoulder",4:"peak",5:"shoulder",
                  6:"off_peak",7:"off_peak",8:"off_peak",9:"shoulder",
                  10:"peak",11:"peak",12:"super_peak"}
    season = "super_peak" if is_holiday else season_map.get(month, "shoulder")

    # 按小时轮换场景，作为所有FC抓取失败时的场景对抗fallback
    market_sc = get_scenario(0, sim_hour)   # h_idx=0 → 仅由sim_hour决定场景

    # border_flow：优先用Firecrawl真实值，降级到场景对抗模拟
    fc_border = fc_data.get("border_flow_fc")
    fc_border_src = fc_data.get("border_flow_source", "simulated")
    if fc_border is not None and fc_border_src not in ("simulated", "fallback"):
        border_flow = round(max(-1.0, min(1.0, fc_border)), 3)
        border_source = fc_border_src
    else:
        # 场景覆盖全值域：OVERFLOW_SQUEEZE=0.95, MIXED_CRISIS=-0.50, NORMAL_OPS=0.45 ...
        border_flow = round(max(-1.0, min(1.0,
                        market_sc.sim_border_flow + _jitter(0.0, 0.04))), 3)
        border_source = f"scenario_{market_sc.name}"

    # zhuhai_saturation：优先Firecrawl，降级到场景
    fc_zhuhai = fc_data.get("zhuhai_saturation_fc")
    fc_zhuhai_src = fc_data.get("zhuhai_source", "simulated")
    if fc_zhuhai is not None and fc_zhuhai_src not in ("simulated", "fallback"):
        zhuhai_sat = round(max(0.0, min(1.0, fc_zhuhai)), 3)
        zhuhai_source = fc_zhuhai_src
    else:
        zhuhai_sat = round(max(0.0, min(1.0,
                       market_sc.sim_zhuhai_saturation + _jitter(0.0, 0.03))), 3)
        zhuhai_source = f"scenario_{market_sc.name}"

    # ota_booking_pace：优先 Firecrawl → 场景模拟
    fc_pace     = fc_data.get("ota_booking_pace_fc")
    fc_pace_src = fc_data.get("ota_pace_source", "simulated")
    if fc_pace is not None and fc_pace_src not in ("simulated", "fallback"):
        ota_pace        = round(max(0.0, min(1.0, fc_pace)), 3)
        ota_pace_source = fc_pace_src
    else:
        ota_pace        = round(max(0.0, min(1.0,
                          market_sc.sim_ota_booking_pace + _jitter(0.0, 0.03))), 3)
        ota_pace_source = f"scenario_{market_sc.name}"

    # ── DSEC 历史需求信号 ────────────────────────────────────────────────
    dsec_market_occ = 0.0
    try:
        import sqlite3 as _sq
        _RDBP = MODEL_DIR / "hotel_collector" / "hotel_real_data.db"
        if _RDBP.exists():
            _dc = _sq.connect(str(_RDBP), timeout=5)
            from dsec_loader import get_dsec_demand_signal as _dsec_sig
            dsec_market_occ = round(
                0.4 * _dsec_sig(month, 3, _dc) +
                0.3 * _dsec_sig(month, 4, _dc) +
                0.3 * _dsec_sig(month, 5, _dc), 4
            )
            _dc.close()
    except Exception:
        pass

    # ── MHA 当前月需求信号 ────────────────────────────────────────────────
    mha_mass_occ = float(real_data.get("mha_occ_mass", 0.0) or 0.0)
    mha_lux_occ = float(real_data.get("mha_occ_luxury", 0.0) or 0.0)
    mha_mass_sig = float(real_data.get("mha_signal_mass", 0.0) or 0.0)
    mha_lux_sig = float(real_data.get("mha_signal_luxury", 0.0) or 0.0)
    mha_market_occ = round(0.5 * mha_mass_sig + 0.5 * mha_lux_sig, 4)
    if mha_market_occ > 0.18:
        mha_demand_state = "HIGH"
    elif mha_market_occ < -0.12:
        mha_demand_state = "LOW"
    else:
        mha_demand_state = "NORMAL"

    # event_density：优先Firecrawl真实抓取，降级到real_data或场景值
    fc_event_density = fc_data.get("event_density_fc")
    fc_event_src     = fc_data.get("event_source", "simulated")
    fc_event_ticket  = fc_data.get("event_ticket_sales_fc", 0.0)
    if fc_event_density is not None and fc_event_src not in ("simulated", "fallback", "unavailable"):
        event_density_val  = round(max(0.0, min(1.0, fc_event_density)), 3)
        event_ticket_val   = round(max(0.0, min(1.0, fc_event_ticket)), 3)
        event_source       = fc_event_src
    else:
        event_density_val  = real_data.get("event_ticket_sales", 0.0)  # real_data已有
        event_ticket_val   = event_density_val
        event_source       = "real_data_fallback"

    return {
        "season": season,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "hour_of_day": hour_of_day,
        # 真实数据信号
        "weather_signal":     real_data.get("weather", 0.0),
        "weather_celsius":    real_data.get("weather_celsius", 25.0),
        "flight_ferry":       real_data.get("flight_ferry", 0.1),
        "event_ticket_sales": event_ticket_val,
        "event_density":      event_density_val,
        "event_source":       event_source,
        "visitors_stats":     real_data.get("visitors_stats", 0.0),
        "mha_occ_mass":       mha_mass_occ,
        "mha_occ_luxury":     mha_lux_occ,
        "mha_market_occ":     mha_market_occ,
        "mha_demand_state":   mha_demand_state,
        # DSEC / MHA 需求信号
        "dsec_market_occ":    dsec_market_occ,
        # 混合信号（FC真实 or 统计模拟）
        "border_flow":        border_flow,
        "border_flow_source": border_source,
        "zhuhai_saturation":  zhuhai_sat,
        "zhuhai_source":      zhuhai_source,
        "ota_booking_pace":   min(1.0, ota_pace),
        "ota_pace_source":    ota_pace_source,
        # 日历
        "holiday": 1.0 if is_holiday else (0.5 if is_weekend else 0.0),
        "weekend": 1.0 if is_weekend else 0.0,
    }


# ── 与Playwright基线对比 ──────────────────────────────────────────────
def read_playwright_baseline(hour: int) -> dict:
    """从results.db读取同小时的Playwright版本结果"""
    baseline_db = MODEL_DIR / "simulation_test" / "results.db"
    if not baseline_db.exists():
        return {}
    try:
        conn = sqlite3.connect(str(baseline_db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT AVG(rec_price) FROM hourly_runs WHERE model_type LIKE 'MARE_ALL%' AND sim_hour=?",
            (hour,)
        ).fetchone()
        conn.close()
        return {"avg_mare": rows[0] if rows and rows[0] else 0.0}
    except Exception:
        return {}


# ── 主循环 ────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*70}")
    print(f"  澳门酒店AI模型 — CrewAI + Firecrawl + AgentOps 并行模拟")
    print(f"  启动时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  3★（澳门旅游局官方）: {len(HOTELS_23_STAR)}家  |  4-5★（澳门旅游局官方）: {len(HOTELS_45_STAR)}家  |  合计: {len(ALL_HOTELS)}家")
    print(f"  CrewAI: {'启用' if USE_CREWAI else '未安装'}  |  "
          f"AgentOps: {'启用' if USE_AGENTOPS else '未配置'}  |  "
          f"LLM: {'GPT-4o-mini' if USE_LLM else '关闭（仅运行模型）'}")
    print(f"  Firecrawl: {'已配置' if os.getenv('FIRECRAWL_API_KEY','')[:3]=='fc-' else '未配置'}")
    print(f"  目标: border_flow / zhuhai_saturation / ota_booking_pace 真实数据")
    print(f"{'='*70}\n")

    PID_FILE.write_text(str(os.getpid()))
    conn = init_db()

    agents = build_agents() if USE_CREWAI else {}

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
        run_at = run_start.strftime("%Y-%m-%d %H:%M:%S")

        checkin  = (run_start + timedelta(days=1)).strftime("%Y-%m-%d")
        checkout = (run_start + timedelta(days=2)).strftime("%Y-%m-%d")

        # ── 步骤1：Playwright基础数据（复用现有）────────────────────
        try:
            real_data = get_all_real_signals(checkin, checkout)
        except Exception as e:
            print(f"  [WARN] Playwright数据抓取失败: {e}")
            real_data = {}

        # ── 步骤2：Firecrawl增强数据（新增！）───────────────────────
        try:
            fc_data = get_all_firecrawl_signals(checkin, checkout)
        except Exception as e:
            print(f"  [WARN] Firecrawl数据抓取失败: {e}")
            fc_data = {}

        # 记录FC抓取结果
        conn.execute(
            "INSERT INTO firecrawl_signals VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (run_at, hour,
             fc_data.get("border_flow_fc", 0.0), fc_data.get("border_flow_source", "failed"),
             fc_data.get("zhuhai_saturation_fc", 0.0), fc_data.get("zhuhai_source", "failed"),
             fc_data.get("ota_booking_pace_fc", 0.0), fc_data.get("ota_pace_source", "failed"),
             fc_data.get("agoda_avg_23", 0.0), fc_data.get("agoda_source", "failed"),
             json.dumps(fc_data))
        )

        # ── 步骤3：生成合并市场信号 ──────────────────────────────────
        signal = get_market_signal(hour, real_data, fc_data)
        weather_c = signal["weather_celsius"]

        # ── 步骤4：运行三个模型（425家全量酒店，14场景轮换）──────────
        hour_results = []
        mare_prices, crm_scores, acq_wins = [], [], 0

        # 动态base_price所需的实时OTA参考价
        _cur_month = run_start.month
        # 修正(2026-06-01): real_data.py 返回 "booking_prices_3"，非 "booking_prices_23"
        _ota_ref_23 = float(sum(real_data["booking_prices_3"]) / len(real_data["booking_prices_3"])) \
            if real_data.get("booking_prices_3") else 1000.0
        _ota_ref_45 = float(real_data["upper_tier_adr_real"]) \
            if real_data.get("upper_tier_adr_real") else 2000.0

        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            # 动态计算base_price：真实BAR/OTA + DSEC（替代随机数）
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_23
            hotel = dict(hotel)
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, _cur_month
            )
            # 4-5星酒店MARE用高端竞对价格
            if hotel["star"] >= 4 and real_data.get("upper_tier_adr_real"):
                rd = dict(real_data); rd["booking_prices_3"] = [real_data["upper_tier_adr_real"]]
            else:
                rd = real_data
            try:
                r = run_23star_test(hotel, signal, rd, scenario)
                # ── Phase 2：弹性引擎 RevPAR 最优化 ──────────────────────
                if _ELASTICITY_OK_CREWAI and r.get("recommended_price", 0) > 0:
                    mkt_price = float(_ota_ref_45 if hotel["star"] >= 4 else _ota_ref_23)
                    # 修复(2026-06-02 P3-A): 防止OTA淡季低价将弹性搜索基准拉至不合理水平
                    _mkt_floor = {3: 680, 4: 900, 5: 1400}
                    mkt_price = max(mkt_price, _mkt_floor.get(hotel.get("star", 3), 680))
                    er = _elasticity_optimize(
                        candidate_price = r["recommended_price"],
                        market_price    = mkt_price,
                        star            = hotel["star"],
                        district        = hotel.get("district", "NAPE"),
                        demand_level    = r.get("demand_state", signal.get("demand_state", "NORMAL")),
                        season          = signal.get("season", "normal"),
                        hotel_id        = hotel.get("hotel_id"),
                    )
                    r["recommended_price"]   = er.optimal_price
                    r["predicted_occupancy"] = er.predicted_occupancy
                    r["predicted_revpar"]    = er.predicted_revpar
                    r["expected_revenue_lift"] = f"+{er.true_lift_pct:.1f}%"
                    r["elasticity_used"]     = er.elasticity_used
                    # ── V6 RevPAR 真实弹性优化 ──────────────────────────────
                    if _V6_OK and r.get("recommended_price", 0) > 0:
                        try:
                            _v6_mkt = float(_ota_in) if _ota_in else hotel["base_price"]
                            _v6_floor = {3: 680, 4: 900, 5: 1400}.get(hotel.get("star", 3), 680)
                            r = _v6_mare(
                                r,
                                star_rating      = hotel.get("star", 3),
                                market_price     = max(_v6_mkt, _v6_floor),
                                base_occ         = signal.get("occupancy", 0.72),
                                floor_price      = _v6_floor,
                                ceiling_price    = max(r["recommended_price"] * 2.0, _v6_floor * 4),
                                demand_state     = r.get("demand_state", signal.get("demand_state", "NORMAL")),
                                competitor_price = None,
                                data_quality     = 1.0 if rd.get("booking_prices_3") else 0.6,
                            )
                            r["hotel_profile_version"] = "V6"
                        except Exception:
                            r["hotel_profile_version"] = "V5_fallback"
                anom = detect_anomalies(hotel, r, signal, "MARE_ALL")
                rp = r.get("recommended_price", 0)
                mare_prices.append(rp)
                conn.execute(
                    "INSERT INTO hourly_runs VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_at, hour, hotel["hotel_id"], hotel["name"], "MARE_ALL_FC",
                     signal["season"],
                     json.dumps({"scenario": scenario.name,
                                 "scenario_category": scenario.category,
                                 "border_source": signal["border_flow_source"],
                                 "zhuhai_source": signal["zhuhai_source"],
                                 "ota_source": signal["ota_pace_source"]}),
                     json.dumps({"recommended_price": rp,
                                 "demand_state": r.get("demand_state"),
                                 "confidence": r.get("confidence"),
                                 "predicted_occupancy": r.get("predicted_occupancy"),
                                 "predicted_revpar": r.get("predicted_revpar"),
                                 "elasticity_used": r.get("elasticity_used")}),
                     rp, r.get("demand_state"), r.get("confidence"),
                     r.get("expected_revenue_lift"), "; ".join(anom),
                     weather_c, int(signal["is_holiday"]), int(signal["is_weekend"]))
                )
                hour_results.append(("MARE", rp, anom))
            except Exception as e:
                hour_results.append(("MARE", 0, [f"ERR:{e}"]))

        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_23
            hotel = dict(hotel)
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, _cur_month
            )
            try:
                r = run_director_crm_test(
                    hotel,
                    signal,
                    real_data,
                    scenario,
                    source_system="S3",
                    run_at=run_at,
                    sim_hour=hour,
                )
                anom = detect_anomalies(hotel, r, signal, "DIRECTOR_CRM_ALL")
                cp = r.get("crm_adjusted_price", 0)
                crm_scores.append(r.get("integration_score", 0))
                conn.execute(
                    "INSERT INTO hourly_runs VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_at, hour, hotel["hotel_id"], hotel["name"], "DIRECTOR_CRM_ALL_FC",
                     signal["season"],
                     json.dumps({"scenario": scenario.name,
                                 "scenario_category": scenario.category,
                                 "psrs_health": scenario.psrs_health}),
                     json.dumps({"crm_adjusted_price": cp,
                                 "psrs_status": r.get("psrs_status"),
                                 "integration_score": r.get("integration_score"),
                                 "channel": r.get("channel")}),
                     # 修正(2026-06-01): confidence列错误写入loyalty_tier，改为integration_score分级
                     cp, r.get("psrs_status"),
                     ("High"   if r.get("integration_score", 0) >= 0.60 else
                      "Medium" if r.get("integration_score", 0) >= 0.35 else "Low"),
                     str(r.get("upsell_revenue", 0)), "; ".join(anom),
                     weather_c, int(signal["is_holiday"]), int(signal["is_weekend"]))
                )
                hour_results.append(("CRM", cp, anom))
            except Exception as e:
                hour_results.append(("CRM", 0, [f"ERR:{e}"]))

        for h_idx, hotel in enumerate(ALL_HOTELS):
            scenario = get_scenario(h_idx, hour)
            _ota_in = _ota_ref_45 if hotel["star"] >= 4 else _ota_ref_23
            hotel = dict(hotel)
            hotel["base_price"] = compute_dynamic_base_price(
                hotel["hotel_id"], hotel["star"], _ota_in, _cur_month
            )
            try:
                r = run_45star_test(hotel, signal, real_data, scenario)
                # ── V6 SelfACQ LTV 评估 ─────────────────────────────────
                if _V6_OK:
                    try:
                        _loyalty = r.get("loyalty_tier", "none")
                        _v6acq = _SelfACQV6().evaluate(
                            direct_price           = r.get("direct_offer_price", r.get("recommended_price", 0)),
                            ota_gross_price        = r.get("ota_standard_price", hotel["base_price"] * 1.15),
                            ota_commission_rate    = 0.18,
                            direct_conversion_prob = 0.25 if _loyalty in ("platinum","gold") else 0.15,
                            repeat_probability     = {"platinum":0.55,"gold":0.40,"silver":0.25,"bronze":0.12,"none":0.05}.get(_loyalty, 0.05),
                            future_margin          = hotel["base_price"] * 0.35,
                            acquisition_cost       = 80.0 if _loyalty == "none" else 20.0,
                        )
                        r["selfacq_v6_direct_ltv"] = _v6acq["expected_direct_ltv"]
                        r["selfacq_v6_advantage"]  = _v6acq["direct_advantage"]
                        r["selfacq_v6_wins"]       = _v6acq["direct_wins"]
                    except Exception:
                        pass
                anom = detect_anomalies(hotel, r, signal, "SELFACQ_ALL")
                dp = r.get("direct_offer_price", 0)
                if r.get("direct_wins_vs_ota"): acq_wins += 1
                conn.execute(
                    "INSERT INTO hourly_runs VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_at, hour, hotel["hotel_id"], hotel["name"], "SELFACQ_ALL_FC",
                     signal["season"],
                     json.dumps({"scenario": scenario.name,
                                 "scenario_category": scenario.category,
                                 "competitor_mult": scenario.competitor_price_multiplier}),
                     json.dumps({"direct_offer_price": dp,
                                 "ota_standard_price": r.get("ota_standard_price"),
                                 "direct_wins_vs_ota": r.get("direct_wins_vs_ota")}),
                     # 修正(2026-06-01): confidence列错误写入loyalty_tier，改为直销胜出置信度
                     dp, "HIGH" if r.get("demand_high") else "NORMAL",
                     ("High"   if r.get("direct_wins_vs_ota") and
                                  r.get("direct_net_revenue", 0) > r.get("ota_net_revenue", 0) * 1.05
                      else "Medium" if r.get("direct_wins_vs_ota")
                      else "Low"),
                     f"+{r.get('revpar_lift_vs_market', '0%')}", "; ".join(anom),
                     weather_c, int(signal["is_holiday"]), int(signal["is_weekend"]))
                )
                hour_results.append(("ACQ", dp, anom))
            except Exception as e:
                hour_results.append(("ACQ", 0, [f"ERR:{e}"]))

        conn.commit()

        # ── 步骤5：与Playwright基线对比 ──────────────────────────────
        baseline = read_playwright_baseline(hour)
        avg_mare = sum(mare_prices) / len(mare_prices) if mare_prices else 0
        avg_crm  = sum(crm_scores) / len(crm_scores) if crm_scores else 0
        mare_diff = ((avg_mare - baseline.get("avg_mare", avg_mare)) /
                     max(1, baseline.get("avg_mare", avg_mare)) * 100) if baseline else 0

        # 计算Firecrawl真实抓取覆盖率（scenario_*和simulated均算fallback，不计入✓）
        _bad = ("simulated", "fallback", "failed", "unavailable")
        fc_real_count = sum([
            0 if fc_data.get("border_flow_source","simulated") in _bad else 1,
            0 if fc_data.get("zhuhai_source","simulated") in _bad else 1,
            0 if fc_data.get("ota_pace_source","simulated") in _bad else 1,
            0 if fc_data.get("agoda_source","unavailable") in _bad else 1,
        ])
        # scenario_*覆盖率另计（S=场景对抗，不是真实数据但也不是随机噪声）
        fc_scenario_count = sum([
            1 if signal["border_flow_source"].startswith("scenario_") else 0,
            1 if signal["zhuhai_source"].startswith("scenario_") else 0,
            1 if signal["ota_pace_source"].startswith("scenario_") else 0,
        ])
        fc_coverage = fc_real_count / 4.0  # 4个目标因子（仅真实抓取）

        conn.execute(
            "INSERT INTO comparison_log VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
            (run_at, hour,
             avg_mare, baseline.get("avg_mare", 0), round(mare_diff, 2),
             fc_data.get("border_flow_fc", 0), signal["border_flow"], 0,
             round(fc_coverage * 100, 1),
             f"border:{signal['border_flow_source']} zhuhai:{signal['zhuhai_source']} pace:{signal['ota_pace_source']}")
        )
        # ── V6 需求状态审计 + 收益归因 ──────────────────────────────────────────
        if _V6_OK:
            try:
                _mare_p = [p for t,p,_ in hour_results if t=="MARE" and p]
                if _mare_p:
                    _audit = _V6Layer().validate_demand_state_logic(
                        high_avg   = sum(_mare_p)/len(_mare_p),
                        normal_avg = 901.0 * 1.05,
                    )
                    if _audit.get("flag"):
                        logging.warning("[V6-AUDIT H%d] HIGH<NORMAL %.1f%%", hour, _audit["gap_pct"])
            except Exception:
                pass
        conn.commit()

        # ── 步骤6：CrewAI分析（每6小时运行一次，节省LLM配额）────────
        crewai_report = {}
        if USE_CREWAI and USE_LLM and (hour % 6 == 0):
            try:
                t0 = time.time()
                signal_ctx = json.dumps({
                    "season": signal["season"],
                    "border_flow": signal["border_flow"],
                    "border_source": signal["border_flow_source"],
                    "weather": weather_c,
                    "is_holiday": signal["is_holiday"],
                    "avg_mare_price": round(avg_mare),
                }, ensure_ascii=False)
                fc_ctx = json.dumps({
                    "border_fc": fc_data.get("border_flow_fc"),
                    "border_src": fc_data.get("border_flow_source"),
                    "zhuhai_fc": fc_data.get("zhuhai_saturation_fc"),
                    "zhuhai_src": fc_data.get("zhuhai_source"),
                    "pace_fc": fc_data.get("ota_booking_pace_fc"),
                    "pace_src": fc_data.get("ota_pace_source"),
                    "agoda_avg": fc_data.get("agoda_avg_23"),
                }, ensure_ascii=False)

                tasks = build_tasks(agents, hour, signal_ctx, fc_ctx)
                crew = Crew(
                    agents=list(agents.values()),
                    tasks=tasks,
                    process=Process.sequential,
                    verbose=False,
                )
                crew_result = crew.kickoff()
                crewai_report = {"output": str(crew_result), "hour": hour}
                duration = time.time() - t0

                conn.execute(
                    "INSERT INTO crewai_reports VALUES (NULL,?,?,?,?)",
                    (run_at, hour, json.dumps(crewai_report), round(duration, 1))
                )
                conn.commit()
            except Exception as e:
                print(f"  [CrewAI] 分析失败（继续运行）: {e}")

        # ── HotelLearningLoop 周度校准（每168小时 = 1模拟周）────────────
        if _V6_OK and (hour + 1) % 168 == 0:
            _week_idx = (hour + 1) // 168 - 1
            _all_hids = [h["hotel_id"] for h in ALL_HOTELS]
            try:
                _cal_res = _calibrate_hotels(_all_hids, str(DB_PATH), _week_idx)
                _updated = sum(1 for v in _cal_res.values()
                               if v.get("last_calibration_week") == f"sim_week_{_week_idx+1:02d}")
                _sample_adr = next(
                    (v.get("baseline_adr") for v in _cal_res.values() if v.get("baseline_adr")), 0
                )
                print(f"\n  [V6学习] 第{_week_idx+1}周校准 | 更新{_updated}/{len(_all_hids)}家 "
                      f"| 样本ADR≈{_sample_adr:.0f} MOP | 状态写入 hotel_profiles_v6.json")
            except Exception as _e:
                print(f"  [V6学习] 周度校准失败（不影响主流程）: {_e}")

        # ── 进度输出 ──────────────────────────────────────────────────
        anomaly_n = sum(1 for _, _, a in hour_results if a)
        def _fc_icon(src: str) -> str:
            if src.startswith("scenario_"): return "S"  # 场景对抗
            if src in ("statistical_simulation", "simulated", "fallback", "failed"): return "~"
            return "✓"  # Firecrawl真实抓取
        fc_icons = {
            "border": _fc_icon(signal["border_flow_source"]),
            "zhuhai": _fc_icon(signal["zhuhai_source"]),
            "pace":   _fc_icon(signal["ota_pace_source"]),
        }
        # border/zhuhai/pace场景名（取前10字符）—— 同 get_market_signal 内逻辑
        _hour_sc = get_scenario(0, hour)
        sc_label = _hour_sc.name[:10]
        print(
            f"[{run_at}] H{hour+1:03d} "
            f"{weather_c:.0f}°C {signal['season']:10s} "
            f"{'假日' if signal['is_holiday'] else '平日'} | "
            f"MARE:{avg_mare:.0f}(Δ{mare_diff:+.1f}%) "
            f"CRM:{avg_crm:.2f} ACQ胜:{acq_wins/len(HOTELS_45_STAR):.0%} | "
            f"FC[border{fc_icons['border']} zhuhai{fc_icons['zhuhai']} pace{fc_icons['pace']}] "
            f"真实{fc_coverage:.0%} 场景{fc_scenario_count}/3({sc_label})"
            + (f" [异常:{anomaly_n}]" if anomaly_n else ""),
            flush=True,
        )

        # ── 等待下一小时 ──────────────────────────────────────────────
        if hour < TOTAL_HOURS - 1:
            elapsed = (datetime.now() - run_start).total_seconds()
            sleep_t = max(0, SLEEP_SECS - elapsed)
            time.sleep(sleep_t)

    # ── 最终报告 ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  CrewAI并行模拟完成 [{datetime.now():%Y-%m-%d %H:%M:%S}]")
    c = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM hourly_runs").fetchone()[0]
    fc_success = c.execute(
        "SELECT COUNT(*) FROM firecrawl_signals WHERE border_source != 'failed' AND border_source != 'simulated'"
    ).fetchone()[0]
    print(f"  总模型调用: {total:,}")
    print(f"  Firecrawl成功抓取border_flow次数: {fc_success}/504")
    print(f"  对比报告: python3 compare_report.py")
    print(f"{'='*70}\n")

    if USE_AGENTOPS:
        try:
            import agentops
            agentops.end_session("Success")
        except Exception:
            pass

    PID_FILE.unlink(missing_ok=True)
    conn.close()


if __name__ == "__main__":
    main()
