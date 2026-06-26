from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from maml_compatible_model import MAMLCompatibleModel

BASE_DIR = Path(__file__).resolve().parent.parent
HOTEL_COLLECTOR_DIR = Path(__file__).resolve().parent
MODEL_REGISTRY_DIR = BASE_DIR / "model_registry" / "mare"
READINESS_DB_PATH = HOTEL_COLLECTOR_DIR / "maml_readiness.db"

_MAML_ENABLE_HOTELS = 200
_MAML_ENABLE_MARKETS = 3
_MAML_ENABLE_NEW_HOTELS_30D = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def market_tier_for_star(star: int | None) -> str:
    return "5_star" if int(star or 0) >= 5 else "3to4_star"


def _hotel_hash(hotel_id: str | None) -> str | None:
    if not hotel_id:
        return None
    return hashlib.sha1(hotel_id.encode("utf-8")).hexdigest()[:16]


def ensure_model_registry_layout() -> None:
    for subdir in ("v1.0", "meta"):
        (MODEL_REGISTRY_DIR / subdir).mkdir(parents=True, exist_ok=True)


def _ensure_monitor_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS new_hotel_onboarding_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_id TEXT NOT NULL UNIQUE,
            market_tier TEXT NOT NULL,
            onboarded_at TEXT NOT NULL,
            days_to_5pct_uplift INTEGER,
            days_to_10pct_uplift INTEGER,
            initial_model_source TEXT,
            onboarding_cost_hours REAL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS maml_training_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            market_tier TEXT NOT NULL,
            model_family TEXT NOT NULL,
            n_source_hotels INTEGER,
            n_meta_iterations INTEGER,
            avg_inner_loss REAL,
            meta_loss REAL,
            schema_version TEXT DEFAULT 'v1.0',
            notes TEXT
        );
        """
    )


def get_readiness_conn() -> sqlite3.Connection:
    ensure_model_registry_layout()
    conn = sqlite3.connect(READINESS_DB_PATH)
    _ensure_monitor_schema(conn)
    conn.commit()
    return conn


def ensure_reserved_schema(conn: sqlite3.Connection) -> None:
    ensure_model_registry_layout()
    _ensure_monitor_schema(conn)

    hourly_cols = {row[1] for row in conn.execute("PRAGMA table_info('hourly_runs')").fetchall()}
    if "meta_hotel_id_hash" not in hourly_cols:
        conn.execute("ALTER TABLE hourly_runs ADD COLUMN meta_hotel_id_hash TEXT DEFAULT NULL")
    if "meta_used_fast_adapt" not in hourly_cols:
        conn.execute("ALTER TABLE hourly_runs ADD COLUMN meta_used_fast_adapt INTEGER DEFAULT 0")

    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='guest_profile_aggregated'"
    ).fetchone()[0]:
        guest_cols = {row[1] for row in conn.execute("PRAGMA table_info('guest_profile_aggregated')").fetchall()}
        if "meta_hotel_embedding" not in guest_cols:
            conn.execute("ALTER TABLE guest_profile_aggregated ADD COLUMN meta_hotel_embedding BLOB DEFAULT NULL")
        if "meta_market_signature" not in guest_cols:
            conn.execute("ALTER TABLE guest_profile_aggregated ADD COLUMN meta_market_signature TEXT DEFAULT NULL")
        if "meta_schema_version" not in guest_cols:
            conn.execute("ALTER TABLE guest_profile_aggregated ADD COLUMN meta_schema_version TEXT DEFAULT 'v1.0'")

    conn.commit()


class MAMLReadinessMonitor:
    def __init__(self, db_path: Path | str = READINESS_DB_PATH):
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        _ensure_monitor_schema(conn)
        return conn

    def log_new_hotel_onboarded(
        self,
        hotel_id: str,
        market_tier: str,
        onboarding_cost_hours: float,
        initial_model_source: str = "cold_start",
        notes: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO new_hotel_onboarding_log (
                    hotel_id, market_tier, onboarded_at,
                    initial_model_source, onboarding_cost_hours, notes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    hotel_id,
                    market_tier,
                    _utc_now(),
                    initial_model_source,
                    onboarding_cost_hours,
                    notes,
                ),
            )

    def seed_existing_hotels(self, hotels: Iterable[dict[str, Any]], *, source: str) -> None:
        with self._conn() as conn:
            for hotel in hotels:
                hotel_id = str(hotel.get("hotel_id") or hotel.get("id") or "").strip()
                if not hotel_id:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO new_hotel_onboarding_log (
                        hotel_id, market_tier, onboarded_at,
                        initial_model_source, onboarding_cost_hours, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hotel_id,
                        market_tier_for_star(int(hotel.get("star") or 0)),
                        _utc_now(),
                        "existing_roster",
                        0.0,
                        f"Seeded from {source}",
                    ),
                )

    def check_activation_readiness(self) -> dict[str, Any]:
        with self._conn() as conn:
            n_hotels = conn.execute(
                "SELECT COUNT(DISTINCT hotel_id) FROM new_hotel_onboarding_log"
            ).fetchone()[0] or 0
            n_markets = conn.execute(
                "SELECT COUNT(DISTINCT market_tier) FROM new_hotel_onboarding_log"
            ).fetchone()[0] or 0
            n_recent = conn.execute(
                """
                SELECT COUNT(*) FROM new_hotel_onboarding_log
                WHERE onboarded_at > datetime('now', '-30 days')
                  AND initial_model_source NOT IN ('existing_roster')
                """
            ).fetchone()[0] or 0

        any_met = any(
            (
                n_hotels >= _MAML_ENABLE_HOTELS,
                n_markets >= _MAML_ENABLE_MARKETS,
                n_recent >= _MAML_ENABLE_NEW_HOTELS_30D,
            )
        )
        return {
            "hotel_count": int(n_hotels),
            "market_tier_count": int(n_markets),
            "new_hotels_30d": int(n_recent),
            "activation_thresholds": {
                "hotel_count": _MAML_ENABLE_HOTELS,
                "market_tier_count": _MAML_ENABLE_MARKETS,
                "new_hotels_30d": _MAML_ENABLE_NEW_HOTELS_30D,
            },
            "layer4_ready": bool(any_met),
            "layer4_enabled": False,
        }


def build_maml_metadata(
    *,
    hotel_id: str | None,
    star: int | None,
    profile_name: str,
    state_version: int,
    ml_enabled: bool,
    model: MAMLCompatibleModel,
) -> dict[str, Any]:
    monitor = MAMLReadinessMonitor()
    readiness = monitor.check_activation_readiness()
    return {
        "maml_reserved": True,
        "maml_layer4_enabled": False,
        "maml_fast_adapt_used": False,
        "maml_market_tier": market_tier_for_star(star),
        "maml_feature_schema_version": model.get_feature_schema().get("version", "v1.0"),
        "maml_profile_name": profile_name,
        "maml_state_version": int(state_version),
        "maml_meta_hotel_id_hash": _hotel_hash(hotel_id),
        "maml_readiness": readiness,
        "ml_layer_active": bool(ml_enabled),
    }
