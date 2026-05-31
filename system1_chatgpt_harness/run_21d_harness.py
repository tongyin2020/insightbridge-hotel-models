#!/usr/bin/env python3
"""21-day local hybrid test harness for MARE + DirectorAI.

This script is designed to run on the user's Mac from a Python terminal.
It uses:
- Firecrawl for public web signals
- MakCorps for OTA market prices
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

import requests
from dotenv import load_dotenv


UTC = timezone.utc


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


@dataclass
class HotelTarget:
    hotel_id: str
    hotel_name: str
    market_segment: str
    star_band: str
    base_price: float
    total_rooms: int
    hotel_city: str = "Macau"


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
    key = os.getenv("MAKCORPS_API_KEY", "").strip()
    if not key:
        return False, {}, 0.0, 0.0

    hotel_name = os.getenv("HOTEL_NAME", "The Venetian Macao")
    hotel_city = os.getenv("HOTEL_CITY", "Macau")
    checkin = os.getenv("CHECKIN_DATE", "2026-06-10")
    checkout = os.getenv("CHECKOUT_DATE", "2026-06-11")
    currency = os.getenv("CURRENCY", "USD")

    try:
        map_resp = requests.get(
            "https://api.makcorps.com/mapping",
            params={"api_key": key, "name": hotel_name, "city": hotel_city},
            timeout=30,
        )
        map_resp.raise_for_status()
        matches = map_resp.json()
        if not matches:
            return False, {}, 0.0, 0.0

        hotel_id = str(matches[0].get("value") or matches[0].get("id") or "").strip()
        if not hotel_id:
            return False, {}, 0.0, 0.0

        hotel_resp = requests.get(
            "https://api.makcorps.com/hotel",
            params={
                "api_key": key,
                "hotelid": hotel_id,
                "cur": currency,
                "rooms": 1,
                "adults": 2,
                "checkin": checkin,
                "checkout": checkout,
            },
            timeout=45,
        )
        hotel_resp.raise_for_status()
        payload = hotel_resp.json()
        comparison = payload.get("comparison", [])
        flat: list[dict[str, Any]] = []
        if isinstance(comparison, list):
            for block in comparison:
                if isinstance(block, list):
                    for item in block:
                        if isinstance(item, dict):
                            flat.append(item)

        ota_prices: dict[str, float] = {}
        parsed_prices: list[float] = []
        for item in flat:
            for idx in range(1, 8):
                vendor_key = f"vendor{idx}"
                price_key = f"price{idx}"
                vendor = item.get(vendor_key)
                price = item.get(price_key)
                if not vendor or price is None:
                    continue
                try:
                    numeric = float(str(price).replace("$", "").replace(",", "").strip())
                except ValueError:
                    continue
                ota_prices[str(vendor).lower().replace(".", "_")] = numeric
                parsed_prices.append(numeric)

        if not parsed_prices:
            return False, {}, 0.0, 0.0

        competitor_price = min(parsed_prices)
        upper_tier_adr = max(parsed_prices)
        return True, ota_prices, round(competitor_price, 2), round(upper_tier_adr, 2)
    except Exception:
        return False, {}, 0.0, 0.0


def build_external_snapshot(ts: datetime) -> ExternalSnapshot:
    event_ok, markdown = firecrawl_event_snapshot()
    event_density, event_ticket_sales = score_event_markdown(markdown)
    market_ok, ota_prices, competitor_price, upper_tier_adr = makcorps_market_snapshot()

    weekend = 1.0 if ts.weekday() >= 5 else 0.0
    holiday = 0.7 if event_density >= 0.45 else 0.1

    # Until dedicated weather / visitor / border / airport feeds are added,
    # keep these as disciplined proxies instead of pretending they are real.
    visitors_stats = round(min(1.0, 0.3 + event_density * 0.5), 3)
    border_flow = round(min(1.0, 0.35 + weekend * 0.15 + event_density * 0.3), 3)
    flight_ferry = round(min(1.0, 0.25 + event_density * 0.35), 3)
    weather = 0.0

    if not market_ok:
        ota_prices = {"booking_com": 142.0, "trip_com": 145.0, "agoda": 140.0}
        competitor_price = 140.0
        upper_tier_adr = 163.0

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


def load_hotel_universe(config_path: Path) -> dict[str, list[HotelTarget]]:
    if not config_path.exists():
        raise FileNotFoundError(f"Hotel universe file not found: {config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    universe: dict[str, list[HotelTarget]] = {"mare_hotels": [], "director_hotels": []}
    for key in universe.keys():
        for item in raw.get(key, []):
            universe[key].append(HotelTarget(**item))
    return universe


def clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def build_payload(snapshot: ExternalSnapshot, scenario: ScenarioDefinition, hotel_id: str, base_price: float, market_segment: str | None) -> dict[str, Any]:
    competitor_price = snapshot.competitor_price
    if scenario.name == "competitor_pressure":
        competitor_price *= 0.90
    elif scenario.name == "near_sellout":
        competitor_price *= 1.08
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
    }
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
result = recommend(data, None)
print(json.dumps(result))
"""
    return run_python_snippet(repo_path / "api", repo_path / "api", script, payload)


def run_director(repo_path: Path, payload: dict[str, Any], objective_mode: str) -> tuple[bool, dict[str, Any]]:
    script = r"""
import json, sys
from app.core.pricing_engine import recommend
payload = json.loads(sys.argv[1])
objective_mode = payload.pop("_objective_mode", "maximize_revenue")
result = recommend(payload, None, objective_mode=objective_mode)
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
    parser.add_argument("--hotel-universe", default="./hotel_universe.json")
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
    universe = load_hotel_universe(Path(args.hotel_universe).expanduser())

    total_cycles = args.cycles if args.cycles > 0 else max(1, int((args.days * 86400) / max(args.interval_seconds, 1)))

    global_counts = {
        "cycles_completed": 0,
        "mare_runs": 0,
        "director_runs": 0,
        "mare_failures": 0,
        "director_failures": 0,
        "issue_counts": {},
    }

    for cycle in range(total_cycles):
        ts = now_utc()
        for hotel in universe["mare_hotels"]:
            snapshot = build_external_snapshot(ts)
            scenarios_mare = scenario_catalog(hotel.base_price)
            for scenario in scenarios_mare:
                payload = build_payload(snapshot, scenario, hotel.hotel_id, hotel.base_price, hotel.market_segment)
                payload["total_rooms"] = hotel.total_rooms
                ok, result = run_mare(mare_repo, payload)
                global_counts["mare_runs"] += 1
                issues = [] if not ok else evaluate_result("mare", result)
                if not ok or issues:
                    global_counts["mare_failures"] += 1
                for issue in issues:
                    global_counts["issue_counts"][issue] = global_counts["issue_counts"].get(issue, 0) + 1
                write_jsonl(log_path, {
                    "timestamp_utc": ts.isoformat(),
                    "cycle": cycle + 1,
                    "model": "mare",
                    "hotel": asdict(hotel),
                    "scenario": asdict(scenario),
                    "external_snapshot": asdict(snapshot),
                    "ok": ok,
                    "issues": issues,
                    "result": result,
                })

        for hotel in universe["director_hotels"]:
            snapshot = build_external_snapshot(ts)
            scenarios_director = scenario_catalog(hotel.base_price)
            for scenario in scenarios_director:
                payload = build_payload(snapshot, scenario, hotel.hotel_id, hotel.base_price, hotel.market_segment)
                payload["total_rooms"] = hotel.total_rooms
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
                    "hotel": asdict(hotel),
                    "scenario": asdict(scenario),
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
