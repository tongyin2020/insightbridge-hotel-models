#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR_DEFAULT = BASE_DIR / "hotel_model_staging_output"
PID_FILE = SCRIPT_DIR / "s1_harness.pid"

for path in (
    BASE_DIR,
    BASE_DIR / "system2_claude_simulation",
    BASE_DIR / "hotel_collector",
    SCRIPT_DIR,
    SCRIPT_DIR / "mare_engine" / "api",
):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=True)
load_dotenv(SCRIPT_DIR / ".env", override=True)

from hotel_roster_76 import ALL_HOTELS_76 as ALL_HOTELS
from system2_claude_simulation.data_fetchers.real_data import get_all_real_signals
from system2_claude_simulation.data_fetchers.scenario_engine import (  # noqa: E402
    HotelScenario,
    SCENARIOS,
)
from system2_claude_simulation.run_simulation import (  # noqa: E402
    compute_dynamic_base_price,
    detect_anomalies,
    get_macau_market_signal,
    run_3star_test,
    run_45star_test,
    run_director_crm_test,
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


@dataclass(frozen=True)
class HarnessScenario:
    name: str
    category: str
    current_occupancy: float
    booking_velocity_24h: int
    days_to_arrival: int
    cancellation_rate: float
    competitor_mult: float
    competitor_availability: float
    same_day_demand_score: float
    avg_clv: float
    repurchase_probability: float
    price_sensitivity: str
    churn_risk: float
    loyalty_tier: str
    guest_segment: str
    guest_satisfaction: float
    neighborhood_availability: float = 0.5
    elasticity_signal: float = 0.0
    ota_commission_rate: float = 0.18
    vip_discount_rate: float = 0.10
    objective_mode: str = "maximize_revenue"

    def to_internal(self, total_rooms: int, hotel_star: int, base_price: float) -> HotelScenario:
        remaining_inventory = max(1, round(total_rooms * max(0.01, 1.0 - self.current_occupancy)))
        remaining_ratio = max(0.0, min(1.0, remaining_inventory / max(total_rooms, 1)))
        if self.loyalty_tier in ("platinum", "gold"):
            psrs_health = "healthy"
        elif self.category == "adversarial":
            psrs_health = "degraded"
        else:
            psrs_health = "healthy"
        crm_override = 0.85 if self.loyalty_tier in ("platinum", "gold") else (0.20 if self.category == "adversarial" else None)
        channel_weights = (
            (28, 24, 18, 8, 18, 4) if self.guest_segment in ("repeat", "vip", "corporate")
            else (10, 42, 28, 8, 8, 4)
        )
        sim_border_flow = max(-1.0, min(1.0, (self.same_day_demand_score - 0.5) * 2.0))
        sim_zhuhai = max(0.0, min(1.0, self.neighborhood_availability))
        sim_ota = max(0.0, min(1.0, self.same_day_demand_score + 0.08))
        previous_price = round(base_price * (0.97 if self.category == "normal" else 1.03), 2)
        return HotelScenario(
            name=self.name,
            description_cn=self.name,
            occupancy=self.current_occupancy,
            booking_velocity_24h=max(0.0, min(1.0, self.booking_velocity_24h / 24.0)),
            days_to_arrival=self.days_to_arrival,
            cancellation_rate=self.cancellation_rate,
            remaining_inventory_ratio=remaining_ratio,
            guest_segment=(
                "corporate" if self.guest_segment == "corporate"
                else "luxury_leisure" if self.guest_segment in ("vip", "premium")
                else "budget"
            ),
            avg_clv=self.avg_clv,
            loyalty_tier=self.loyalty_tier or "none",
            churn_risk=self.churn_risk,
            previous_price=previous_price,
            competitor_price_multiplier=self.competitor_mult,
            psrs_health=psrs_health,
            crm_match_rate_override=crm_override,
            channel_weights=channel_weights,
            sim_border_flow=sim_border_flow,
            sim_zhuhai_saturation=sim_zhuhai,
            sim_ota_booking_pace=sim_ota,
            category=self.category,
        )

    def to_output(self, total_rooms: int, hotel_star: int, base_price: float) -> dict[str, Any]:
        remaining_inventory = max(1, round(total_rooms * max(0.01, 1.0 - self.current_occupancy)))
        prev_price = round(base_price * (0.97 if self.category == "normal" else 1.03), 2)
        hist_avg = round(base_price * 0.96, 2)
        return {
            "name": self.name,
            "category": self.category,
            "current_occupancy": self.current_occupancy,
            "remaining_inventory": remaining_inventory,
            "total_rooms": total_rooms,
            "booking_velocity_24h": self.booking_velocity_24h,
            "days_to_arrival": self.days_to_arrival,
            "cancellation_rate": self.cancellation_rate,
            "elasticity_signal": self.elasticity_signal,
            "competitor_availability": self.competitor_availability,
            "neighborhood_availability": self.neighborhood_availability,
            "same_day_demand_score": self.same_day_demand_score,
            "avg_clv": self.avg_clv,
            "repurchase_probability": self.repurchase_probability,
            "price_sensitivity": self.price_sensitivity,
            "churn_risk": self.churn_risk,
            "loyalty_tier": self.loyalty_tier,
            "guest_segment": self.guest_segment,
            "previous_price": prev_price,
            "avg_30d_price": round(prev_price * 0.99, 2),
            "historical_avg": hist_avg,
            "customer_historical_rate": round(hist_avg * 0.98, 2),
            "guest_satisfaction": self.guest_satisfaction,
            "ota_commission_rate": self.ota_commission_rate,
            "vip_discount_rate": self.vip_discount_rate,
            "objective_mode": self.objective_mode,
        }


S1_SCENARIOS: list[HarnessScenario] = [
    HarnessScenario("normal_weekday", "normal", 0.68, 18, 10, 0.10, 1.00, 0.45, 0.42, 2800, 0.40, "medium", 0.20, "", "new", 4.2),
    HarnessScenario("weekend_pickup", "normal", 0.74, 20, 4, 0.08, 1.05, 0.38, 0.58, 3200, 0.46, "medium", 0.16, "silver", "repeat", 4.3),
    HarnessScenario("festival_surge", "extreme", 0.93, 23, 1, 0.03, 1.28, 0.15, 0.90, 5200, 0.62, "low", 0.08, "gold", "premium", 4.5),
    HarnessScenario("soft_demand", "normal", 0.46, 10, 14, 0.17, 0.92, 0.66, 0.24, 1900, 0.26, "high", 0.42, "", "new", 4.0),
    HarnessScenario("competitor_pressure", "adversarial", 0.61, 14, 6, 0.12, 0.86, 0.54, 0.36, 2100, 0.33, "high", 0.34, "", "new", 4.1),
    HarnessScenario("high_inventory", "normal", 0.39, 9, 18, 0.14, 0.95, 0.72, 0.22, 1800, 0.22, "high", 0.38, "", "new", 4.0),
    HarnessScenario("near_sellout", "extreme", 0.96, 22, 0, 0.02, 1.18, 0.08, 0.86, 4500, 0.55, "low", 0.06, "gold", "repeat", 4.4),
    HarnessScenario("fairness_stress", "adversarial", 0.71, 17, 2, 0.09, 1.14, 0.34, 0.64, 3600, 0.48, "medium", 0.18, "bronze", "repeat", 3.7),
    HarnessScenario("low_satisfaction_conflict", "adversarial", 0.58, 12, 7, 0.13, 0.98, 0.49, 0.31, 2000, 0.28, "high", 0.44, "", "new", 3.2),
    HarnessScenario("dirty_data", "adversarial", 0.67, 16, 5, 0.11, 1.02, 0.40, 0.40, 2600, 0.35, "medium", 0.24, "silver", "repeat", 4.1),
    HarnessScenario("signal_conflict", "adversarial", 0.76, 19, 3, 0.15, 1.10, 0.29, 0.67, 3400, 0.41, "medium", 0.27, "gold", "corporate", 4.2),
]


def build_external_snapshot(ts_utc: str, signal: dict[str, Any], real_data: dict[str, Any]) -> dict[str, Any]:
    ota_prices = {
        "booking_com": round(float(real_data.get("competitor_price_real") or 0.0), 1),
        "trip_com": round(float(real_data.get("competitor_price_real") or 0.0) * 1.02, 1) if real_data.get("competitor_price_real") else 0.0,
        "agoda": round(float(real_data.get("competitor_price_min") or real_data.get("competitor_price_real") or 0.0), 1),
    }
    return {
        "timestamp_utc": ts_utc,
        "event_density": float(real_data.get("event_density_fc") or real_data.get("event_ticket_fc") or 0.0),
        "holiday": signal.get("holiday", 0.0),
        "weekend": signal.get("weekend", 0.0),
        "weather": signal.get("weather_signal", 0.0),
        "visitors_stats": signal.get("visitors_stats", 0.0),
        "border_flow": signal.get("border_flow", 0.0),
        "flight_ferry": signal.get("flight_ferry", 0.0),
        "event_ticket_sales": signal.get("event_ticket_sales", 0.0),
        "competitor_price": float(real_data.get("competitor_price_real") or 0.0),
        "upper_tier_adr": float(real_data.get("upper_tier_adr_real") or 0.0),
        "ota_prices": ota_prices,
        "raw_event_source_ok": bool(real_data.get("event_fc_source")),
        "raw_market_source_ok": bool(real_data.get("booking_prices_3") or real_data.get("booking_prices_45")),
        "dsec_market_occ": signal.get("dsec_market_occ", 0.0),
        "dsec_demand_signal": signal.get("dsec_market_occ", 0.0),
        "mha_market_occ": signal.get("mha_market_occ", 0.0),
        "blended_market_demand_signal": signal.get("blended_market_demand_signal", 0.0),
        "dsec_cold_adr": {"3": 922.0, "4": 957.0, "5": 1501.0},
    }


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except Exception:
        pass


def run_cycle(cycle_no: int, jsonl_path: Path, summary_path: Path, counters: dict[str, Any]) -> None:
    run_ts = datetime.now(timezone.utc).isoformat()
    checkin = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    checkout = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        real_data = get_all_real_signals(checkin, checkout)
    except Exception:
        real_data = {}

    signal = get_macau_market_signal(cycle_no - 1, real_data)
    snapshot = build_external_snapshot(run_ts, signal, real_data)
    month = datetime.now().month

    with jsonl_path.open("a", encoding="utf-8") as fh:
        for hotel in ALL_HOTELS:
            hotel_base = dict(hotel)
            ota_ref = float(real_data.get("upper_tier_adr_real") or 2000.0) if hotel["star"] >= 4 else float(real_data.get("competitor_price_real") or 1000.0)
            hotel_base["base_price"] = compute_dynamic_base_price(hotel["hotel_id"], hotel["star"], ota_ref, month)

            for scenario in S1_SCENARIOS:
                internal = scenario.to_internal(hotel_base["total_rooms"], hotel_base["star"], hotel_base["base_price"])
                scenario_payload = scenario.to_output(hotel_base["total_rooms"], hotel_base["star"], hotel_base["base_price"])

                for model_name, runner in (
                    ("mare", lambda: run_3star_test(dict(hotel_base), signal, real_data, internal)),
                    ("director", lambda: run_director_crm_test(dict(hotel_base), signal, real_data, internal, source_system="S1", run_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sim_hour=cycle_no - 1)),
                ):
                    counters[f"{model_name}_runs"] += 1
                    try:
                        result = runner()
                        if model_name == "director":
                            result["recommended_price"] = float(result.get("crm_adjusted_price") or result.get("recommended_price") or 0.0)
                            result.setdefault("predicted_occupancy", scenario.current_occupancy)
                            result.setdefault("predicted_revpar", round(result["recommended_price"] * scenario.current_occupancy, 2))
                            result.setdefault("expected_revenue_lift", f"{float(result.get('elasticity_lift_pct') or 0.0):+.2f}%")
                        issues = detect_anomalies(hotel_base, result, signal, "MARE_ALL" if model_name == "mare" else "DIRECTOR_CRM_ALL")
                        if model_name == "mare" and _MARE_ML_FEEDBACK_OK and result.get("ml_enabled"):
                            try:
                                from mare_ml_layer import MareMLDecision
                                decision = MareMLDecision(
                                    profile_name=str(result.get("elasticity_profile") or "unknown"),
                                    elasticity_multiplier=float(result.get("ml_elasticity_multiplier") or 1.0),
                                    premium_delta=float(result.get("ml_premium_delta") or 0.0),
                                    occupancy_delta=float(result.get("ml_occupancy_delta") or 0.0),
                                    state_version=int(result.get("ml_state_version") or 0),
                                )
                                _update_mare_adjustments(decision, _score_mare_outcome(result, issues))
                            except Exception:
                                pass
                        payload = {
                            "timestamp_utc": run_ts,
                            "cycle": cycle_no,
                            "model": model_name,
                            "hotel_id": hotel["hotel_id"],
                            "hotel_star": hotel["star"],
                            "scenario": scenario_payload,
                            "external_snapshot": snapshot,
                            "ok": True,
                            "issues": issues,
                            "result": result,
                        }
                    except Exception as exc:
                        counters[f"{model_name}_failures"] += 1
                        issue = f"EXCEPTION: {type(exc).__name__}: {exc}"
                        counters["issue_counts"][issue] = counters["issue_counts"].get(issue, 0) + 1
                        payload = {
                            "timestamp_utc": run_ts,
                            "cycle": cycle_no,
                            "model": model_name,
                            "hotel_id": hotel["hotel_id"],
                            "hotel_star": hotel["star"],
                            "scenario": scenario_payload,
                            "external_snapshot": snapshot,
                            "ok": False,
                            "issues": [issue],
                            "result": {},
                        }
                    else:
                        for issue in payload["issues"]:
                            counters["issue_counts"][issue] = counters["issue_counts"].get(issue, 0) + 1
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

            for scenario in SCENARIOS:
                counters["selfacq_runs"] += 1
                try:
                    result = run_45star_test(dict(hotel_base), signal, real_data, scenario)
                    result["recommended_price"] = float(result.get("direct_offer_price") or result.get("recommended_price") or 0.0)
                    result.setdefault("expected_revenue_lift", result.get("revpar_lift_vs_market") or "0.0%")
                    issues = detect_anomalies(hotel_base, result, signal, "SELFACQ_ALL")
                    payload = {
                        "timestamp_utc": run_ts,
                        "cycle": cycle_no,
                        "model": "selfacq",
                        "hotel_id": hotel["hotel_id"],
                        "hotel_star": hotel["star"],
                        "scenario": scenario.name,
                        "external_snapshot": snapshot,
                        "ok": True,
                        "issues": issues,
                        "result": result,
                    }
                except Exception as exc:
                    counters["selfacq_failures"] += 1
                    issue = f"EXCEPTION: {type(exc).__name__}: {exc}"
                    counters["issue_counts"][issue] = counters["issue_counts"].get(issue, 0) + 1
                    payload = {
                        "timestamp_utc": run_ts,
                        "cycle": cycle_no,
                        "model": "selfacq",
                        "hotel_id": hotel["hotel_id"],
                        "hotel_star": hotel["star"],
                        "scenario": scenario.name,
                        "external_snapshot": snapshot,
                        "ok": False,
                        "issues": [issue],
                        "result": {},
                    }
                else:
                    for issue in payload["issues"]:
                        counters["issue_counts"][issue] = counters["issue_counts"].get(issue, 0) + 1
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    counters["cycles_completed"] = cycle_no
    summary_path.write_text(json.dumps(counters, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="InsightBridge System 1 21-day harness")
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--cycles", type=int, default=0, help="0 means run continuously")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="Run one cycle and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = args.output_dir / f"run_{stamp}.jsonl"
    summary_path = args.output_dir / f"summary_{stamp}.json"
    counters = {
        "cycles_completed": 0,
        "mare_runs": 0,
        "director_runs": 0,
        "selfacq_runs": 0,
        "mare_failures": 0,
        "director_failures": 0,
        "selfacq_failures": 0,
        "issue_counts": {},
    }

    stop_requested = False

    def _handle_stop(signum, frame):  # noqa: ANN001
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    write_pid()
    try:
        cycle_no = 0
        max_cycles = 1 if args.dry_run else args.cycles
        while not stop_requested:
            cycle_no += 1
            run_cycle(cycle_no, jsonl_path, summary_path, counters)
            if max_cycles and cycle_no >= max_cycles:
                break
            if stop_requested:
                break
            time.sleep(max(args.interval_seconds, 1))
    finally:
        remove_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
