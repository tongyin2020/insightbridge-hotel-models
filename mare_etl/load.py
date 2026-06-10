from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("mare_etl_load")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
TRANSFORMED_DIR = Path(os.getenv("MARE_ETL_TRANSFORMED", str(BASE_DIR / "transformed")))
FEATURE_STORE_DB = Path(os.getenv("MARE_FEATURE_STORE", str(BASE_DIR / "feature_store.db")))
LOADED_MARKER_DIR = Path(os.getenv("MARE_ETL_LOADED_MARKER", str(BASE_DIR / "loaded")))

WRITABLE_FIELDS = [
    "hotel_id", "feature_date", "hour",
    "day_of_week", "day_of_month", "month", "is_weekend", "is_holiday", "days_to_holiday",
    "season", "season_ord", "peak_period_flag", "super_peak_flag",
    "hotel_star", "hotel_id_te",
    "dsec_market_occ", "mha_market_occ", "blended_market_demand_signal",
    "occ_lag_24h", "occ_lag_72h", "occ_lag_7d",
    "occ_rolling_7d_mean", "occ_rolling_30d_mean",
    "base_price", "competitor_price", "price_ratio", "base_price_lag_24h",
    "border_flow", "flight_ferry", "visitors_stats", "zhuhai_saturation",
    "event_ticket_sales", "event_density", "days_to_next_event",
    "weather_score", "temperature", "rain_prob",
    "ota_booking_pace", "holiday",
    "demand_target", "actual_occupancy", "baseline_predicted", "target_finalized_at",
    "etl_run_id", "data_completeness",
]


def _coerce(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def _load_single_file(file_path: Path, conn: sqlite3.Connection) -> int:
    placeholders = ",".join(["?"] * len(WRITABLE_FIELDS))
    sql = f"INSERT OR REPLACE INTO daily_features ({','.join(WRITABLE_FIELDS)}) VALUES ({placeholders})"
    batch = []
    n = 0
    with file_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            record = json.loads(line)
            batch.append(tuple(_coerce(record.get(field)) for field in WRITABLE_FIELDS))
            if len(batch) >= 500:
                conn.executemany(sql, batch)
                n += len(batch)
                batch = []
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    return n


def load_records(transform_run_id: str | None = None, all_pending: bool = False) -> dict:
    LOADED_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    if all_pending:
        files = [f for f in TRANSFORMED_DIR.glob("transform_*.jsonl") if not (LOADED_MARKER_DIR / f"{f.stem}.done").exists()]
    elif transform_run_id:
        files = [TRANSFORMED_DIR / f"{transform_run_id}.jsonl"]
    else:
        raise ValueError("transform_run_id or all_pending required")

    if not files:
        return {"n_files": 0, "n_rows": 0}

    conn = sqlite3.connect(FEATURE_STORE_DB)
    started = datetime.utcnow()
    total_rows = 0
    try:
        for file_path in files:
            if not file_path.exists():
                continue
            loaded = _load_single_file(file_path, conn)
            total_rows += loaded
            (LOADED_MARKER_DIR / f"{file_path.stem}.done").touch()
        conn.commit()
    finally:
        conn.close()
    return {"n_files": len(files), "n_rows": total_rows, "duration_sec": round((datetime.utcnow() - started).total_seconds(), 2)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transform-run-id")
    parser.add_argument("--all-pending", action="store_true")
    args = parser.parse_args()
    result = load_records(args.transform_run_id, args.all_pending)
    logger.info("load result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

