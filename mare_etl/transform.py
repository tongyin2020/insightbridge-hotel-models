from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("mare_etl_transform")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STAGING_DIR = Path(os.getenv("MARE_ETL_STAGING", str(BASE_DIR / "staging")))
TRANSFORMED_DIR = Path(os.getenv("MARE_ETL_TRANSFORMED", str(BASE_DIR / "transformed")))
FEATURE_STORE_DB = Path(os.getenv("MARE_FEATURE_STORE", str(BASE_DIR / "feature_store.db")))

REQUIRED_COMPLETENESS = 0.80
Z_SCORE_THRESHOLD = 5.0


def load_staging_files(etl_run_id: str | None = None) -> dict[str, list[dict]]:
    pattern = f"{etl_run_id}__*.jsonl" if etl_run_id else "*.jsonl"
    files = list(STAGING_DIR.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no staging files matching {pattern}")
    by_source: dict[str, list[dict]] = defaultdict(list)
    for file_path in files:
        source = file_path.stem.split("__")[-1]
        with file_path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    by_source[source].append(json.loads(line))
    return by_source


def align_by_hotel_date_hour(by_source: dict[str, list[dict]]) -> pd.DataFrame:
    pms = pd.DataFrame(by_source.get("pms", []))
    if pms.empty:
        raise ValueError("PMS data required for training rows")
    pms = pms.rename(columns={"date": "feature_date"})
    if "hotel_star" not in pms.columns:
        pms["hotel_star"] = 3

    ota = pd.DataFrame(by_source.get("ota", []))
    if not ota.empty:
        ota = ota.rename(columns={"date": "feature_date"})
        ota_daily = ota.groupby("feature_date").agg({"competitor_price_avg": "mean", "ota_booking_pace": "mean"}).reset_index()
        pms = pms.merge(ota_daily, on="feature_date", how="left")

    for source_name, output_col in (("dsec", "dsec_market_occ"), ("mha", "mha_market_occ")):
        df = pd.DataFrame(by_source.get(source_name, []))
        if not df.empty:
            df = df.rename(columns={"date": "feature_date", "market_occ": output_col})
            pms = pms.merge(df[["feature_date", "hour", output_col]], on=["feature_date", "hour"], how="left")

    weather = pd.DataFrame(by_source.get("weather", []))
    if not weather.empty:
        weather = weather.rename(columns={"date": "feature_date"})
        pms = pms.merge(weather[["feature_date", "hour", "temperature", "rain_prob", "weather_score"]], on=["feature_date", "hour"], how="left")

    cal = pd.DataFrame(by_source.get("holiday_calendar", []))
    if not cal.empty:
        cal = cal.rename(columns={"date": "feature_date"})
        keep = [c for c in ["feature_date", "holiday", "is_holiday", "days_to_holiday", "days_to_next_event", "event_density", "event_ticket_sales"] if c in cal.columns]
        pms = pms.merge(cal[keep], on="feature_date", how="left")
    return pms


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    dt = pd.to_datetime(df["feature_date"])
    df["day_of_week"] = dt.dt.dayofweek
    df["day_of_month"] = dt.dt.day
    df["month"] = dt.dt.month
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    if "season" not in df.columns:
        df["season"] = np.where(df["month"].isin([1, 2]), "super_peak", np.where(df["month"].isin([10, 11, 12]), "peak", np.where(df["month"].isin([3, 4, 5, 9]), "shoulder", "off_peak")))
    season_map = {"off_peak": 0, "shoulder": 1, "peak": 2, "super_peak": 3}
    df["season_ord"] = df["season"].map(season_map).fillna(1).astype(int)
    df["peak_period_flag"] = df["season"].isin(["peak", "super_peak"]).astype(int)
    df["super_peak_flag"] = (df["season"] == "super_peak").astype(int)
    return df


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["hotel_id", "feature_date", "hour"]).reset_index(drop=True)
    df["occ_lag_24h"] = df.groupby("hotel_id")["occupancy"].shift(24)
    df["occ_lag_72h"] = df.groupby("hotel_id")["occupancy"].shift(72)
    df["occ_lag_7d"] = df.groupby("hotel_id")["occupancy"].shift(24 * 7)
    df["occ_rolling_7d_mean"] = df.groupby("hotel_id")["occupancy"].rolling(24 * 7, min_periods=1).mean().reset_index(drop=True)
    df["occ_rolling_30d_mean"] = df.groupby("hotel_id")["occupancy"].rolling(24 * 30, min_periods=1).mean().reset_index(drop=True)
    df["base_price_lag_24h"] = df.groupby("hotel_id")["base_price"].shift(24)
    return df


def add_price_features(df: pd.DataFrame) -> pd.DataFrame:
    df["competitor_price"] = df.get("competitor_price_avg", pd.Series(dtype=float))
    df["price_ratio"] = np.where(df["competitor_price"].fillna(0) > 0, df["base_price"] / df["competitor_price"], 1.0)
    return df


def add_market_features(df: pd.DataFrame) -> pd.DataFrame:
    df["dsec_market_occ"] = df.get("dsec_market_occ", pd.Series(dtype=float)).fillna(0.0)
    df["mha_market_occ"] = df.get("mha_market_occ", pd.Series(dtype=float)).fillna(0.0)
    df["blended_market_demand_signal"] = df["dsec_market_occ"] * 0.4 + df["mha_market_occ"] * 0.6
    return df


def build_target(df: pd.DataFrame) -> pd.DataFrame:
    df["actual_occupancy"] = df["occupancy"]
    df["baseline_predicted"] = df.groupby(["hotel_id", "hour", "day_of_week"])["actual_occupancy"].transform(lambda s: s.rolling(window=4, min_periods=1).mean().shift(1))
    df["demand_target"] = (df["actual_occupancy"] - df["baseline_predicted"]).clip(-0.5, 0.5)
    df["target_finalized_at"] = datetime.utcnow().isoformat()
    return df


def apply_quality_checks(df: pd.DataFrame, etl_run_id: str) -> pd.DataFrame:
    candidate_cols = ["hotel_id", "feature_date", "hour", "base_price", "occupancy", "dsec_market_occ", "mha_market_occ"]
    df["data_completeness"] = df[candidate_cols].notna().sum(axis=1) / len(candidate_cols)
    df = df[df["data_completeness"] >= REQUIRED_COMPLETENESS].copy()
    numeric_cols = ["base_price", "competitor_price", "occupancy", "temperature", "event_density", "visitors_stats"]
    conn = sqlite3.connect(FEATURE_STORE_DB)
    try:
        for col in numeric_cols:
            if col not in df.columns:
                continue
            series = pd.to_numeric(df[col], errors="coerce")
            if series.std(ddof=0) in (0, np.nan) or series.isna().all():
                continue
            z = ((series - series.mean()) / series.std(ddof=0)).abs()
            flagged = df[z > Z_SCORE_THRESHOLD]
            for _, row in flagged.iterrows():
                conn.execute(
                    """
                    INSERT INTO etl_anomaly_log (
                        etl_run_id, hotel_id, feature_date, feature_name, feature_value, z_score, severity
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (etl_run_id, row.get("hotel_id"), row.get("feature_date"), col, float(row.get(col) or 0.0), float(z.loc[row.name]), "medium"),
                )
        conn.commit()
    finally:
        conn.close()
    return df


def log_transform_run(row: dict) -> None:
    conn = sqlite3.connect(FEATURE_STORE_DB)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO etl_run_log (
                etl_run_id, stage, mode, start_date, end_date, status,
                n_rows_in, n_rows_out, error_count, duration_sec, started_at, finished_at, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["etl_run_id"], row["stage"], row["mode"], row["start_date"], row["end_date"], row["status"],
                row["n_rows_in"], row["n_rows_out"], row["error_count"], row["duration_sec"], row["started_at"], row["finished_at"], row["notes"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_transform(etl_run_id: str | None = None) -> dict:
    started = datetime.utcnow()
    source_rows = load_staging_files(etl_run_id)
    raw_n = sum(len(v) for v in source_rows.values())
    df = align_by_hotel_date_hour(source_rows)
    df = add_time_features(df)
    df = add_lag_features(df)
    df = add_price_features(df)
    df = add_market_features(df)
    df = build_target(df)
    transform_run_id = f"transform_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    df["etl_run_id"] = transform_run_id
    df["hotel_id_te"] = 0.5
    df["border_flow"] = df.get("border_flow", pd.Series(dtype=float)).fillna(0.0)
    df["flight_ferry"] = df.get("flight_ferry", pd.Series(dtype=float)).fillna(0.0)
    df["visitors_stats"] = df.get("visitors_stats", pd.Series(dtype=float)).fillna(0.0)
    df["zhuhai_saturation"] = df.get("zhuhai_saturation", pd.Series(dtype=float)).fillna(0.0)
    df["temperature"] = df.get("temperature", pd.Series(dtype=float)).fillna(25.0)
    df["rain_prob"] = df.get("rain_prob", pd.Series(dtype=float)).fillna(0.0)
    df["weather_score"] = df.get("weather_score", pd.Series(dtype=float)).fillna(0.0)
    df["holiday"] = df.get("holiday", pd.Series(dtype=float)).fillna(0.0)
    df = apply_quality_checks(df, transform_run_id)
    TRANSFORMED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TRANSFORMED_DIR / f"{transform_run_id}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for record in df.to_dict(orient="records"):
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    finished = datetime.utcnow()
    result = {
        "etl_run_id": transform_run_id,
        "stage": "transform",
        "mode": "manual",
        "start_date": str(df["feature_date"].min()) if not df.empty else None,
        "end_date": str(df["feature_date"].max()) if not df.empty else None,
        "status": "success",
        "n_rows_in": raw_n,
        "n_rows_out": int(len(df)),
        "error_count": 0,
        "duration_sec": round((finished - started).total_seconds(), 2),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "notes": f"source_run={etl_run_id or 'all'}",
    }
    log_transform_run(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--etl-run-id")
    args = parser.parse_args()
    result = run_transform(args.etl_run_id)
    logger.info("transform result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

