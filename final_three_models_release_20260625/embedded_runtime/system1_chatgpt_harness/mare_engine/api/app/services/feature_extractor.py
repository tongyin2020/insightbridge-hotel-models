from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("mare.feature_extractor")

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

FEATURE_STORE_DB = Path(
    os.getenv(
        "MARE_FEATURE_STORE",
        str(_PROJECT_ROOT / "mare_etl" / "feature_store.db"),
    )
)


def _request_to_dict(request: Any) -> dict:
    if isinstance(request, dict):
        return dict(request)
    if hasattr(request, "model_dump"):
        return request.model_dump()
    if hasattr(request, "dict"):
        return request.dict()
    return {k: getattr(request, k) for k in dir(request) if not k.startswith("_")}


def enrich_request_with_lags(request: Any) -> dict:
    base = _request_to_dict(request)
    hotel_id = base.get("hotel_id")
    if not hotel_id or not FEATURE_STORE_DB.exists():
        return base

    try:
        conn = sqlite3.connect(FEATURE_STORE_DB)
        try:
            rows = conn.execute(
                """
                SELECT actual_occupancy
                FROM daily_features
                WHERE hotel_id = ?
                  AND actual_occupancy IS NOT NULL
                  AND feature_date >= date('now', '-30 day')
                ORDER BY feature_date DESC, hour DESC
                LIMIT 720
                """,
                (hotel_id,),
            ).fetchall()
        finally:
            conn.close()
        occs = [r[0] for r in rows if r[0] is not None]
        if occs:
            base.setdefault("occ_lag_24h", occs[min(23, len(occs) - 1)])
            if len(occs) >= 72:
                base.setdefault("occ_lag_72h", occs[71])
            if len(occs) >= 24 * 7:
                base.setdefault("occ_lag_7d", occs[24 * 7 - 1])
                base.setdefault("occ_rolling_7d_mean", sum(occs[:24 * 7]) / (24 * 7))
            if len(occs) >= 24 * 30:
                base.setdefault("occ_rolling_30d_mean", sum(occs[:24 * 30]) / (24 * 30))
    except Exception as exc:
        logger.warning("feature store lag lookup skipped: %s", exc)
    return base

