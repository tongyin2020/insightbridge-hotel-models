from __future__ import annotations

import argparse
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = CURRENT_DIR.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from common_runtime import (
    build_market_context,
    detect_anomalies,
    get_scenario,
    prepare_hotel,
    run_3star_test,
    select_hotels,
    summarize_records,
    write_json,
)


def run_final_mare(hotel_id: str | None = None, sim_hour: int = 0, output_path: str | None = None) -> str:
    context = build_market_context(sim_hour=sim_hour)
    month = context["month"]
    real_data = context["real_data"]
    signal = context["signal"]

    records = []
    for idx, hotel in enumerate(select_hotels(hotel_id)):
        scenario = get_scenario(idx, sim_hour)
        prepared = prepare_hotel(hotel, real_data, month)
        if prepared["star"] >= 4 and real_data.get("upper_tier_adr_real"):
            rd = dict(real_data)
            rd["booking_prices_3"] = [real_data["upper_tier_adr_real"]]
        else:
            rd = real_data
        result = run_3star_test(prepared, signal, rd, scenario)
        issues = detect_anomalies(prepared, result, signal, "MARE_ALL_FC")
        records.append(
            {
                "hotel_id": prepared["hotel_id"],
                "hotel_name": prepared.get("name"),
                "star": prepared["star"],
                "scenario": scenario.name,
                "scenario_category": scenario.category,
                "issues": issues,
                "result": result,
            }
        )

    summary = summarize_records(records, "recommended_price")
    payload = {
        "model_family": "MARE",
        "selected_system": "System 3",
        "selected_model": "MARE_ALL_FC",
        "merge_points": [
            "retain System 3 main framework",
            "absorb System 1 conservative pricing style",
        ],
        "run_context": context,
        "summary": summary,
        "records": records,
    }

    output = Path(output_path) if output_path else Path(__file__).resolve().parent / "output_samples" / f"mare_final_{context['run_ts']}.json"
    write_json(output, payload)
    print(f"Final MARE complete: samples={summary['samples']} avg_price={summary['avg_price']} anomalies={summary['anomalies']}")
    return str(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run final MARE model")
    parser.add_argument("--hotel-id", default=None)
    parser.add_argument("--sim-hour", type=int, default=0)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_final_mare(args.hotel_id, args.sim_hour, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
