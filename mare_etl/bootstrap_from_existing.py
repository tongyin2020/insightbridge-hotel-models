from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mare_etl_bootstrap")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
FEATURE_STORE_DB = Path(os.getenv("MARE_FEATURE_STORE", str(BASE_DIR / "feature_store.db")))
SCHEMA_SQL = BASE_DIR / "schema_feature_store.sql"
SOURCE_DIR = PROJECT_ROOT / "hotel_model_staging_output"

try:
    import sys
    _ROSTER_DIR = PROJECT_ROOT / "system2_claude_simulation"
    if str(_ROSTER_DIR) not in sys.path:
        sys.path.insert(0, str(_ROSTER_DIR))
    from hotel_roster_76 import ALL_HOTELS_76

    ALLOWED_HOTEL_IDS = {str(h.get("hotel_id")) for h in ALL_HOTELS_76 if h.get("hotel_id")}
except Exception:
    ALLOWED_HOTEL_IDS = set()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_schema() -> None:
    FEATURE_STORE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(FEATURE_STORE_DB)
    try:
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _load_rows(max_files: int | None = None) -> list[dict[str, Any]]:
    files = sorted(SOURCE_DIR.glob("run_*.jsonl"))
    if max_files:
        files = files[-max_files:]
    rows: list[dict[str, Any]] = []
    for file_path in files:
        with file_path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("model") != "mare" or not obj.get("ok"):
                    continue
                if ALLOWED_HOTEL_IDS and str(obj.get("hotel_id") or "") not in ALLOWED_HOTEL_IDS:
                    continue
                rows.append(obj)
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str) and value.endswith("%"):
            return float(value[:-1])
        return float(value)
    except Exception:
        return default


def _season_ord(season: str) -> int:
    return {"off_peak": 0, "shoulder": 1, "peak": 2, "super_peak": 3}.get(season, 1)


def _bootstrap_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    per_hotel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    hotel_meta: dict[str, dict[str, Any]] = {}

    for row in rows:
        hotel_id = str(row.get("hotel_id") or "")
        if not hotel_id:
            continue
        per_hotel[hotel_id].append(row)

    output_rows: list[dict[str, Any]] = []
    synthetic_start = datetime(2026, 1, 1, 0, 0, 0)

    for hotel_id, items in per_hotel.items():
        items.sort(key=lambda r: str(r.get("timestamp_utc") or ""))
        occ_hist: list[float] = []
        base_hist: list[float] = []
        for idx, row in enumerate(items):
            scenario = row.get("scenario") or {}
            external = row.get("external_snapshot") or {}
            result = row.get("result") or {}
            rec_log = result.get("recommendation_log") or {}

            ts = synthetic_start + timedelta(hours=idx)
            season = str(result.get("season") or external.get("season") or "shoulder")
            holiday = _safe_float(external.get("holiday"), 0.0)
            base_price = _safe_float(rec_log.get("base_price"), _safe_float(result.get("recommended_price"), 0.0))
            competitor_price = _safe_float(external.get("competitor_price"), base_price)
            actual_occ = _safe_float(scenario.get("current_occupancy"), _safe_float(result.get("predicted_occupancy"), 0.6))
            demand_score = _safe_float(result.get("demand_score"), 0.0)
            total_rooms = int(_safe_float(scenario.get("total_rooms"), 0))

            occ_lag_24h = occ_hist[-24] if len(occ_hist) >= 24 else actual_occ
            occ_lag_72h = occ_hist[-72] if len(occ_hist) >= 72 else occ_lag_24h
            occ_lag_7d = occ_hist[-168] if len(occ_hist) >= 168 else occ_lag_72h
            occ_roll_7d = sum(occ_hist[-168:]) / min(len(occ_hist), 168) if occ_hist else actual_occ
            occ_roll_30d = sum(occ_hist[-720:]) / min(len(occ_hist), 720) if occ_hist else actual_occ
            base_lag_24h = base_hist[-24] if len(base_hist) >= 24 else base_price
            baseline_predicted = occ_roll_7d

            record = {
                "hotel_id": hotel_id,
                "feature_date": ts.strftime("%Y-%m-%d"),
                "hour": ts.hour,
                "day_of_week": ts.weekday(),
                "day_of_month": ts.day,
                "month": ts.month,
                "is_weekend": 1 if ts.weekday() >= 5 else 0,
                "is_holiday": 1 if holiday > 0.5 else 0,
                "days_to_holiday": int(_safe_float(external.get("days_to_holiday"), 30)),
                "season": season,
                "season_ord": _season_ord(season),
                "peak_period_flag": 1 if season in ("peak", "super_peak") else 0,
                "super_peak_flag": 1 if season == "super_peak" else 0,
                "hotel_star": int(_safe_float(row.get("hotel_star"), 3)),
                "hotel_id_te": 0.5,
                "dsec_market_occ": _safe_float(external.get("dsec_market_occ"), _safe_float(external.get("dsec_demand_signal"), 0.0)),
                "mha_market_occ": _safe_float(external.get("mha_market_occ"), 0.0),
                "blended_market_demand_signal": _safe_float(external.get("blended_market_demand_signal"), 0.0),
                "occ_lag_24h": occ_lag_24h,
                "occ_lag_72h": occ_lag_72h,
                "occ_lag_7d": occ_lag_7d,
                "occ_rolling_7d_mean": occ_roll_7d,
                "occ_rolling_30d_mean": occ_roll_30d,
                "base_price": base_price,
                "competitor_price": competitor_price,
                "price_ratio": base_price / competitor_price if competitor_price > 0 else 1.0,
                "base_price_lag_24h": base_lag_24h,
                "border_flow": _safe_float(external.get("border_flow"), 0.0),
                "flight_ferry": _safe_float(external.get("flight_ferry"), 0.0),
                "visitors_stats": _safe_float(external.get("visitors_stats"), 0.0),
                "zhuhai_saturation": _safe_float(external.get("zhuhai_saturation"), 0.0),
                "event_ticket_sales": _safe_float(external.get("event_ticket_sales"), 0.0),
                "event_density": _safe_float(external.get("event_density"), 0.0),
                "days_to_next_event": int(_safe_float(external.get("days_to_next_event"), 30)),
                "weather_score": _safe_float(external.get("weather"), 0.0),
                "temperature": _safe_float(external.get("temperature"), 25.0),
                "rain_prob": _safe_float(external.get("rain_prob"), 0.0),
                "ota_booking_pace": _safe_float(external.get("ota_booking_pace"), 0.0),
                "holiday": holiday,
                "demand_target": demand_score,
                "actual_occupancy": actual_occ,
                "baseline_predicted": baseline_predicted,
                "target_finalized_at": _utcnow().isoformat(),
                "data_completeness": 1.0,
            }
            output_rows.append(record)
            occ_hist.append(actual_occ)
            base_hist.append(base_price)

            hotel_meta[hotel_id] = {
                "hotel_id": hotel_id,
                "hotel_name": row.get("hotel_id"),
                "hotel_star": int(_safe_float(row.get("hotel_star"), 3)),
                "market_segment": "5_star" if int(_safe_float(row.get("hotel_star"), 3)) >= 5 else "3-4_star",
                "total_rooms": total_rooms or None,
                "location": "Macau",
                "onboarded_at": _utcnow().strftime("%Y-%m-%d"),
                "is_active": 1,
            }

    return output_rows, hotel_meta


def bootstrap_feature_store(max_files: int | None = None) -> dict[str, Any]:
    ensure_schema()
    rows = _load_rows(max_files=max_files)
    if not rows:
        raise RuntimeError("No bootstrap source rows found")
    feature_rows, hotel_meta = _bootstrap_records(rows)
    etl_run_id = f"bootstrap_{_utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    for row in feature_rows:
        row["etl_run_id"] = etl_run_id

    conn = sqlite3.connect(FEATURE_STORE_DB)
    try:
        conn.execute("DELETE FROM daily_features")
        conn.executemany(
            f"""
            INSERT OR REPLACE INTO daily_features (
                hotel_id, feature_date, hour, day_of_week, day_of_month, month,
                is_weekend, is_holiday, days_to_holiday, season, season_ord,
                peak_period_flag, super_peak_flag, hotel_star, hotel_id_te,
                dsec_market_occ, mha_market_occ, blended_market_demand_signal,
                occ_lag_24h, occ_lag_72h, occ_lag_7d, occ_rolling_7d_mean,
                occ_rolling_30d_mean, base_price, competitor_price, price_ratio,
                base_price_lag_24h, border_flow, flight_ferry, visitors_stats,
                zhuhai_saturation, event_ticket_sales, event_density,
                days_to_next_event, weather_score, temperature, rain_prob,
                ota_booking_pace, holiday, demand_target, actual_occupancy,
                baseline_predicted, target_finalized_at, etl_run_id, data_completeness
            ) VALUES ({','.join(['?'] * 45)})
            """,
            [
                (
                    r["hotel_id"], r["feature_date"], r["hour"], r["day_of_week"], r["day_of_month"], r["month"],
                    r["is_weekend"], r["is_holiday"], r["days_to_holiday"], r["season"], r["season_ord"],
                    r["peak_period_flag"], r["super_peak_flag"], r["hotel_star"], r["hotel_id_te"],
                    r["dsec_market_occ"], r["mha_market_occ"], r["blended_market_demand_signal"],
                    r["occ_lag_24h"], r["occ_lag_72h"], r["occ_lag_7d"], r["occ_rolling_7d_mean"],
                    r["occ_rolling_30d_mean"], r["base_price"], r["competitor_price"], r["price_ratio"],
                    r["base_price_lag_24h"], r["border_flow"], r["flight_ferry"], r["visitors_stats"],
                    r["zhuhai_saturation"], r["event_ticket_sales"], r["event_density"],
                    r["days_to_next_event"], r["weather_score"], r["temperature"], r["rain_prob"],
                    r["ota_booking_pace"], r["holiday"], r["demand_target"], r["actual_occupancy"],
                    r["baseline_predicted"], r["target_finalized_at"], r["etl_run_id"], r["data_completeness"],
                )
                for r in feature_rows
            ],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO hotel_metadata (
                hotel_id, hotel_name, hotel_star, market_segment, total_rooms, location, onboarded_at, is_active
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    meta["hotel_id"], meta["hotel_name"], meta["hotel_star"], meta["market_segment"],
                    meta["total_rooms"], meta["location"], meta["onboarded_at"], meta["is_active"],
                )
                for meta in hotel_meta.values()
            ],
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO etl_run_log (
                etl_run_id, stage, mode, start_date, end_date, status,
                n_rows_in, n_rows_out, error_count, duration_sec, started_at, finished_at, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                etl_run_id, "all", "manual",
                min(r["feature_date"] for r in feature_rows),
                max(r["feature_date"] for r in feature_rows),
                "success",
                len(rows), len(feature_rows), 0, 0.0,
                _utcnow().isoformat(), _utcnow().isoformat(),
                "Bootstrap from existing S1 MARE run outputs with pseudo-label demand_target=demand_score",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "etl_run_id": etl_run_id,
        "source_rows": len(rows),
        "feature_rows": len(feature_rows),
        "hotels": len(hotel_meta),
        "date_min": min(r["feature_date"] for r in feature_rows),
        "date_max": max(r["feature_date"] for r in feature_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-files", type=int, default=None, help="Use latest N run_*.jsonl files; default uses all available history")
    args = parser.parse_args()
    result = bootstrap_feature_store(max_files=args.max_files)
    logger.info("bootstrap result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
