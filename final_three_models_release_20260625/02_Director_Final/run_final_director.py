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
    run_director_crm_test,
    select_hotels,
    summarize_records,
    write_json,
)


def run_final_director(hotel_id: str | None = None, sim_hour: int = 0, output_path: str | None = None) -> str:
    context = build_market_context(sim_hour=sim_hour)
    month = context["month"]
    real_data = context["real_data"]
    signal = context["signal"]

    records = []
    for idx, hotel in enumerate(select_hotels(hotel_id)):
        scenario = get_scenario(idx, sim_hour)
        prepared = prepare_hotel(hotel, real_data, month)
        result = run_director_crm_test(
            prepared,
            signal,
            real_data,
            scenario,
            source_system="FINAL_S3",
            run_at=context["run_at"],
            sim_hour=sim_hour,
        )
        issues = detect_anomalies(prepared, result, signal, "DIRECTOR_CRM_ALL_FC")
        result = dict(result)
        result["recommended_price"] = float(result.get("crm_adjusted_price") or 0.0)
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
        "model_family": "DIRECTOR",
        "selected_system": "System 3",
        "selected_model": "DIRECTOR_CRM_ALL_FC",
        "merge_points": [
            "retain System 3 revenue structure",
            "absorb System 1 and System 2 price-mouth stability",
        ],
        "run_context": context,
        "summary": summary,
        "records": records,
    }

    output = Path(output_path) if output_path else Path(__file__).resolve().parent / "output_samples" / f"director_final_{context['run_ts']}.json"
    write_json(output, payload)
    print(f"Final Director complete: samples={summary['samples']} avg_price={summary['avg_price']} anomalies={summary['anomalies']}")
    return str(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run final Director model")
    parser.add_argument("--hotel-id", default=None)
    parser.add_argument("--sim-hour", type=int, default=0)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_final_director(args.hotel_id, args.sim_hour, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
