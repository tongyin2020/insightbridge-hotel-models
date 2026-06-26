from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PACKAGE_ROOT / "embedded_runtime"
OUTPUT_TS_FMT = "%Y%m%dT%H%M%SZ"

for path in (
    RUNTIME_ROOT,
    RUNTIME_ROOT / "system2_claude_simulation",
    RUNTIME_ROOT / "system3_crewai",
    RUNTIME_ROOT / "hotel_collector",
    RUNTIME_ROOT / "system1_chatgpt_harness" / "mare_engine" / "api",
    RUNTIME_ROOT / "共用_HROS_V6引擎",
):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

load_dotenv(RUNTIME_ROOT / "system3_crewai" / ".env", override=True)
load_dotenv(RUNTIME_ROOT / "system2_claude_simulation" / ".env", override=True)

os.environ.setdefault("MARE_USE_ML", "1")
os.environ.setdefault(
    "MODEL_WEIGHTS_PATH",
    str(RUNTIME_ROOT / "system2_claude_simulation" / "data" / "model_weights.json"),
)

from system2_claude_simulation.data_fetchers.real_data import get_all_real_signals
from system2_claude_simulation.data_fetchers.scenario_engine import get_scenario
from system2_claude_simulation.hotel_roster_76 import ALL_HOTELS_76 as ALL_HOTELS
from system2_claude_simulation.run_simulation import (
    MACAU_HOLIDAYS_2026,
    _jitter,
    compute_dynamic_base_price,
    detect_anomalies,
    run_3star_test,
    run_45star_test,
    run_director_crm_test,
)
from system3_crewai.tools.firecrawl_scrapers import get_all_firecrawl_signals


def _safe_dsec_signal(month: int) -> float:
    try:
        from dsec_loader import get_dsec_demand_signal as _dsec_sig

        db_path = RUNTIME_ROOT / "hotel_collector" / "hotel_real_data.db"
        if not db_path.exists():
            return 0.0
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return round(
                0.4 * _dsec_sig(month, 3, conn)
                + 0.3 * _dsec_sig(month, 4, conn)
                + 0.3 * _dsec_sig(month, 5, conn),
                4,
            )
        finally:
            conn.close()
    except Exception:
        return 0.0


def build_system3_market_signal(sim_hour: int, real_data: dict[str, Any], fc_data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now()
    hour_of_day = now.hour
    day_of_week = now.weekday()
    month = now.month
    date_str = now.strftime("%m-%d")

    is_weekend = day_of_week >= 5
    is_holiday = date_str in MACAU_HOLIDAYS_2026

    season_map = {
        1: "off_peak",
        2: "shoulder",
        3: "shoulder",
        4: "peak",
        5: "shoulder",
        6: "off_peak",
        7: "off_peak",
        8: "off_peak",
        9: "shoulder",
        10: "peak",
        11: "peak",
        12: "super_peak",
    }
    season = "super_peak" if is_holiday else season_map.get(month, "shoulder")
    market_sc = get_scenario(0, sim_hour)

    fc_border = fc_data.get("border_flow_fc")
    fc_border_src = fc_data.get("border_flow_source", "simulated")
    if fc_border is not None and fc_border_src not in ("simulated", "fallback"):
        border_flow = round(max(-1.0, min(1.0, fc_border)), 3)
        border_source = fc_border_src
    else:
        border_flow = round(max(-1.0, min(1.0, market_sc.sim_border_flow + _jitter(0.0, 0.04))), 3)
        border_source = f"scenario_{market_sc.name}"

    fc_zhuhai = fc_data.get("zhuhai_saturation_fc")
    fc_zhuhai_src = fc_data.get("zhuhai_source", "simulated")
    if fc_zhuhai is not None and fc_zhuhai_src not in ("simulated", "fallback"):
        zhuhai_sat = round(max(0.0, min(1.0, fc_zhuhai)), 3)
        zhuhai_source = fc_zhuhai_src
    else:
        zhuhai_sat = round(max(0.0, min(1.0, market_sc.sim_zhuhai_saturation + _jitter(0.0, 0.03))), 3)
        zhuhai_source = f"scenario_{market_sc.name}"

    fc_pace = fc_data.get("ota_booking_pace_fc")
    fc_pace_src = fc_data.get("ota_pace_source", "simulated")
    if fc_pace is not None and fc_pace_src not in ("simulated", "fallback"):
        ota_pace = round(max(0.0, min(1.0, fc_pace)), 3)
        ota_pace_source = fc_pace_src
    else:
        ota_pace = round(max(0.0, min(1.0, market_sc.sim_ota_booking_pace + _jitter(0.0, 0.03))), 3)
        ota_pace_source = f"scenario_{market_sc.name}"

    dsec_market_occ = _safe_dsec_signal(month)

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

    fc_event_density = fc_data.get("event_density_fc")
    fc_event_src = fc_data.get("event_source", "simulated")
    fc_event_ticket = fc_data.get("event_ticket_sales_fc", 0.0)
    if fc_event_density is not None and fc_event_src not in ("simulated", "fallback", "unavailable"):
        event_density_val = round(max(0.0, min(1.0, fc_event_density)), 3)
        event_ticket_val = round(max(0.0, min(1.0, fc_event_ticket)), 3)
        event_source = fc_event_src
    else:
        event_density_val = real_data.get("event_ticket_sales", 0.0)
        event_ticket_val = event_density_val
        event_source = "real_data_fallback"

    return {
        "season": season,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "hour_of_day": hour_of_day,
        "weather_signal": real_data.get("weather", 0.0),
        "weather_celsius": real_data.get("weather_celsius", 25.0),
        "flight_ferry": real_data.get("flight_ferry", 0.1),
        "event_ticket_sales": event_ticket_val,
        "event_density": event_density_val,
        "event_source": event_source,
        "visitors_stats": real_data.get("visitors_stats", 0.0),
        "mha_occ_mass": float(real_data.get("mha_occ_mass", 0.0) or 0.0),
        "mha_occ_luxury": float(real_data.get("mha_occ_luxury", 0.0) or 0.0),
        "mha_market_occ": mha_market_occ,
        "blended_market_demand_signal": blended_market_demand_signal,
        "mha_demand_state": mha_demand_state,
        "dsec_market_occ": dsec_market_occ,
        "border_flow": border_flow,
        "border_flow_source": border_source,
        "zhuhai_saturation": zhuhai_sat,
        "zhuhai_source": zhuhai_source,
        "ota_booking_pace": min(1.0, ota_pace),
        "ota_pace_source": ota_pace_source,
        "holiday": 1.0 if is_holiday else (0.5 if is_weekend else 0.0),
        "weekend": 1.0 if is_weekend else 0.0,
    }


def build_market_context(sim_hour: int = 0, days_ahead: int = 1) -> dict[str, Any]:
    now = datetime.now()
    checkin = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    checkout = (now + timedelta(days=days_ahead + 1)).strftime("%Y-%m-%d")
    try:
        real_data = get_all_real_signals(checkin, checkout)
    except Exception:
        real_data = {}
    try:
        fc_data = get_all_firecrawl_signals(checkin, checkout)
    except Exception:
        fc_data = {}
    signal = build_system3_market_signal(sim_hour, real_data, fc_data)
    return {
        "run_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "run_ts": now.strftime(OUTPUT_TS_FMT),
        "month": now.month,
        "real_data": real_data,
        "fc_data": fc_data,
        "signal": signal,
    }


def select_hotels(hotel_id: str | None = None) -> list[dict[str, Any]]:
    if hotel_id:
        return [hotel for hotel in ALL_HOTELS if hotel["hotel_id"] == hotel_id]
    return list(ALL_HOTELS)


def prepare_hotel(hotel: dict[str, Any], real_data: dict[str, Any], month: int) -> dict[str, Any]:
    hotel_copy = dict(hotel)
    ota_ref_23 = float(sum(real_data["booking_prices_3"]) / len(real_data["booking_prices_3"])) if real_data.get("booking_prices_3") else 1000.0
    ota_ref_45 = float(real_data.get("upper_tier_adr_real") or 2000.0)
    ota_in = ota_ref_45 if hotel_copy["star"] >= 4 else ota_ref_23
    hotel_copy["base_price"] = compute_dynamic_base_price(hotel_copy["hotel_id"], hotel_copy["star"], ota_in, month)
    return hotel_copy


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_records(records: list[dict[str, Any]], price_key: str) -> dict[str, Any]:
    if not records:
        return {"samples": 0, "avg_price": 0.0, "anomalies": 0}
    priced = [float((r.get("result") or {}).get(price_key, 0.0) or 0.0) for r in records]
    anomalies = sum(1 for r in records if r.get("issues"))
    return {
        "samples": len(records),
        "avg_price": round(sum(priced) / len(priced), 1),
        "anomalies": anomalies,
    }
