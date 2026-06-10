from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("mare_etl_extract")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
STAGING_DIR = Path(os.getenv("MARE_ETL_STAGING", str(BASE_DIR / "staging")))
FEATURE_STORE_DB = Path(os.getenv("MARE_FEATURE_STORE", str(BASE_DIR / "feature_store.db")))
DATA_SOURCES_YAML = Path(os.getenv("MARE_DATA_SOURCES", str(BASE_DIR / "data_sources.yaml")))
SCHEMA_SQL = BASE_DIR / "schema_feature_store.sql"


def _ensure_store() -> None:
    FEATURE_STORE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(FEATURE_STORE_DB)
    try:
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _date_range(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def load_config() -> dict:
    if not DATA_SOURCES_YAML.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(DATA_SOURCES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


class DataSourceAdapter:
    source_name = "abstract"
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        raise NotImplementedError


class PMSAdapter(DataSourceAdapter):
    source_name = "pms"
    def __init__(self, config: dict):
        self.config = config
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        for _ in _date_range(start, end):
            return iter([])
        return iter([])


class OTAAdapter(DataSourceAdapter):
    source_name = "ota"
    def __init__(self, config: dict):
        self.config = config
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        for d in _date_range(start, end):
            yield {"source": "ota", "date": d.isoformat(), "competitor_price_avg": None, "ota_booking_pace": None, "raw_payload": {}}


class DSECAdapter(DataSourceAdapter):
    source_name = "dsec"
    def __init__(self, config: dict):
        self.config = config
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        for d in _date_range(start, end):
            for h in range(24):
                yield {"source": "dsec", "date": d.isoformat(), "hour": h, "market_occ": None, "raw_payload": {}}


class MHAAdapter(DataSourceAdapter):
    source_name = "mha"
    def __init__(self, config: dict):
        self.config = config
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        for d in _date_range(start, end):
            for h in range(24):
                yield {"source": "mha", "date": d.isoformat(), "hour": h, "market_occ": None, "raw_payload": {}}


class WeatherAdapter(DataSourceAdapter):
    source_name = "weather"
    def __init__(self, config: dict):
        self.config = config
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        for d in _date_range(start, end):
            for h in range(24):
                yield {"source": "weather", "date": d.isoformat(), "hour": h, "temperature": None, "rain_prob": None, "weather_score": None, "raw_payload": {}}


class HolidayCalendarAdapter(DataSourceAdapter):
    source_name = "holiday_calendar"
    def __init__(self, config: dict):
        self.calendar_path = Path(config.get("calendar_path", str(BASE_DIR / "data" / "macau_calendar.json")))
    def fetch(self, start: date, end: date) -> Iterator[dict]:
        calendar = {}
        if self.calendar_path.exists():
            try:
                calendar = json.loads(self.calendar_path.read_text(encoding="utf-8"))
            except Exception:
                calendar = {}
        for d in _date_range(start, end):
            entry = calendar.get(d.isoformat(), {})
            yield {
                "source": "holiday_calendar",
                "date": d.isoformat(),
                "holiday": entry.get("holiday", 0),
                "is_holiday": entry.get("is_holiday", 0),
                "days_to_holiday": entry.get("days_to_holiday", 30),
                "days_to_next_event": entry.get("days_to_next_event", 30),
                "event_density": entry.get("event_density", 0.0),
                "event_ticket_sales": entry.get("event_ticket_sales", 0.0),
                "raw_payload": entry,
            }


ADAPTERS = {
    "pms": PMSAdapter,
    "ota": OTAAdapter,
    "dsec": DSECAdapter,
    "mha": MHAAdapter,
    "weather": WeatherAdapter,
    "holiday_calendar": HolidayCalendarAdapter,
}


def _log_run(row: dict) -> None:
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


def run_extract(start: date, end: date, sources: list[str] | None = None, mode: str = "manual") -> dict:
    _ensure_store()
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    etl_run_id = f"extract_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    cfg = load_config()
    use_sources = sources or list(ADAPTERS.keys())
    started = datetime.utcnow()
    total_rows = 0
    errors = 0
    for source in use_sources:
        adapter = ADAPTERS[source](cfg.get(source, {}))
        out_path = STAGING_DIR / f"{etl_run_id}__{source}.jsonl"
        with out_path.open("w", encoding="utf-8") as fh:
            try:
                count = 0
                for row in adapter.fetch(start, end):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    count += 1
                total_rows += count
            except Exception as exc:
                errors += 1
                logger.warning("extract failed for %s: %s", source, exc)
    finished = datetime.utcnow()
    result = {
        "etl_run_id": etl_run_id,
        "stage": "extract",
        "mode": mode,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "status": "partial" if errors else "success",
        "n_rows_in": 0,
        "n_rows_out": total_rows,
        "error_count": errors,
        "duration_sec": round((finished - started).total_seconds(), 2),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "notes": "Adapter skeletons; real PMS/OTA credentials still required",
    }
    _log_run(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["incremental", "backfill"], default="incremental")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--source", action="append")
    args = parser.parse_args()
    end = datetime.utcnow().date() - timedelta(days=1) if args.mode == "incremental" else datetime.fromisoformat(args.end).date()
    start = end if args.mode == "incremental" else datetime.fromisoformat(args.start).date()
    result = run_extract(start, end, args.source, mode=args.mode)
    logger.info("extract result: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

