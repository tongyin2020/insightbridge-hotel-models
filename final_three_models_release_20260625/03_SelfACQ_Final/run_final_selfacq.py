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
    run_45star_test,
    select_hotels,
    summarize_records,
    write_json,
)


def run_final_selfacq(hotel_id: str | None = None, sim_hour: int = 0, output_path: str | None = None) -> str:
    context = build_market_context(sim_hour=sim_hour)
    month = context["month"]
    real_data = context["real_data"]
    signal = context["signal"]

    records = []
    for idx, hotel in enumerate(select_hotels(hotel_id)):
        scenario = get_scenario(idx, sim_hour)
        prepared = prepare_hotel(hotel, real_data, month)
        result = run_45star_test(prepared, signal, real_data, scenario)
        issues = detect_anomalies(prepared, result, signal, "SELFACQ_ALL")
        result = dict(result)
        result["recommended_price"] = float(result.get("direct_offer_price") or result.get("recommended_price") or 0.0)
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
        "model_family": "SELFACQ",
        "selected_system": "System 1",
        "selected_model": "SelfACQ",
        "execution_note": "System 1 was selected as the best-performing harness; SelfACQ runtime core remains the shared hotel engine used by the harness.",
        "merge_points": [
            "retain System 1 highest-yield backbone",
            "borrow System 2 migration stability where useful",
        ],
        "run_context": context,
        "summary": summary,
        "records": records,
    }

    output = Path(output_path) if output_path else Path(__file__).resolve().parent / "output_samples" / f"selfacq_final_{context['run_ts']}.json"
    write_json(output, payload)
    print(f"Final SelfACQ complete: samples={summary['samples']} avg_price={summary['avg_price']} anomalies={summary['anomalies']}")
    return str(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run final SelfACQ model")
    parser.add_argument("--hotel-id", default=None)
    parser.add_argument("--sim-hour", type=int, default=0)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_final_selfacq(args.hotel_id, args.sim_hour, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
