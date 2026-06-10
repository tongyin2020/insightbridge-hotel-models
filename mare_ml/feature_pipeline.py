from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FEATURE_NAMES: list[str] = [
    "hour", "day_of_week", "day_of_month", "month",
    "is_weekend", "is_holiday", "days_to_holiday",
    "season_ord", "peak_period_flag", "super_peak_flag",
    "hotel_star", "hotel_id_te",
    "dsec_market_occ", "mha_market_occ", "blended_market_demand_signal",
    "occ_lag_24h", "occ_lag_72h", "occ_lag_7d",
    "occ_rolling_7d_mean", "occ_rolling_30d_mean",
    "base_price", "competitor_price", "price_ratio", "base_price_lag_24h",
    "border_flow", "flight_ferry", "visitors_stats", "zhuhai_saturation",
    "event_ticket_sales", "event_density", "days_to_next_event",
    "weather_score", "temperature", "rain_prob",
    "ota_booking_pace", "holiday",
]

SEASON_ORDINAL = {"off_peak": 0, "shoulder": 1, "peak": 2, "super_peak": 3}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _request_to_dict(request: Any) -> dict[str, Any]:
    if isinstance(request, dict):
        return dict(request)
    if hasattr(request, "model_dump"):
        return request.model_dump()
    if hasattr(request, "dict"):
        return request.dict()
    return {k: getattr(request, k) for k in dir(request) if not k.startswith("_")}


@dataclass
class FeaturePipeline:
    hotel_id_target_mean: dict[str, float] = field(default_factory=dict)
    global_target_mean: float = 0.5
    feature_means: dict[str, float] = field(default_factory=dict)
    trained_at: str | None = None
    version: str = "v1"

    def fit(self, df: pd.DataFrame, target_col: str = "demand_target") -> "FeaturePipeline":
        self.global_target_mean = float(df[target_col].mean())
        self.hotel_id_target_mean = df.groupby("hotel_id")[target_col].mean().to_dict()
        for feat in FEATURE_NAMES:
            self.feature_means[feat] = float(df[feat].mean()) if feat in df.columns else 0.0
        self.trained_at = _utcnow().isoformat()
        return self

    def transform(self, data: Any) -> np.ndarray:
        d = _request_to_dict(data)
        dt = self._parse_target_date(d)
        holiday_flag = float(d.get("holiday", 0.0) or 0.0)
        season = str(d.get("season", "shoulder"))
        competitor_price = float(d.get("competitor_price", 0.0) or 0.0)
        base_price = float(d.get("base_price", 0.0) or 0.0)
        hotel_id = str(d.get("hotel_id", "") or "")
        hotel_id_te = float(self.hotel_id_target_mean.get(hotel_id, self.global_target_mean))

        vec = np.array(
            [
                float(dt.hour),
                float(dt.weekday()),
                float(dt.day),
                float(dt.month),
                1.0 if dt.weekday() >= 5 else 0.0,
                1.0 if holiday_flag > 0.5 else 0.0,
                float(d.get("days_to_holiday", 30) or 30),
                float(SEASON_ORDINAL.get(season, 1)),
                1.0 if season in ("peak", "super_peak") else 0.0,
                1.0 if season == "super_peak" else 0.0,
                float(d.get("hotel_star", 3) or 3),
                hotel_id_te,
                float(d.get("dsec_market_occ", 0.0) or 0.0),
                float(d.get("mha_market_occ", 0.0) or 0.0),
                float(d.get("blended_market_demand_signal", 0.0) or 0.0),
                float(d.get("occ_lag_24h", self.feature_means.get("occ_lag_24h", 0.6))),
                float(d.get("occ_lag_72h", self.feature_means.get("occ_lag_72h", 0.6))),
                float(d.get("occ_lag_7d", self.feature_means.get("occ_lag_7d", 0.6))),
                float(d.get("occ_rolling_7d_mean", self.feature_means.get("occ_rolling_7d_mean", 0.6))),
                float(d.get("occ_rolling_30d_mean", self.feature_means.get("occ_rolling_30d_mean", 0.6))),
                base_price,
                competitor_price,
                base_price / competitor_price if competitor_price > 0 else 1.0,
                float(d.get("base_price_lag_24h", base_price) or base_price),
                float(d.get("border_flow", 0.0) or 0.0),
                float(d.get("flight_ferry", 0.0) or 0.0),
                float(d.get("visitors_stats", 0.0) or 0.0),
                float(d.get("zhuhai_saturation", 0.0) or 0.0),
                float(d.get("event_ticket_sales", 0.0) or 0.0),
                float(d.get("event_density", 0.0) or 0.0),
                float(d.get("days_to_next_event", 30) or 30),
                float(d.get("weather", d.get("weather_score", 0.0)) or 0.0),
                float(d.get("temperature", 25.0) or 25.0),
                float(d.get("rain_prob", 0.0) or 0.0),
                float(d.get("ota_booking_pace", 0.0) or 0.0),
                holiday_flag,
            ],
            dtype=np.float32,
        )
        return vec

    def transform_batch(self, df: pd.DataFrame) -> np.ndarray:
        return np.stack([self.transform(row.to_dict()) for _, row in df.iterrows()])

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)
        path.with_suffix(".meta.json").write_text(
            json.dumps(
                {
                    "trained_at": self.trained_at,
                    "version": self.version,
                    "feature_names": FEATURE_NAMES,
                    "n_features": len(FEATURE_NAMES),
                    "n_hotels_seen": len(self.hotel_id_target_mean),
                    "global_target_mean": self.global_target_mean,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "FeaturePipeline":
        with open(path, "rb") as fh:
            return pickle.load(fh)

    @staticmethod
    def _parse_target_date(d: dict[str, Any]) -> datetime:
        ds = d.get("target_date") or d.get("check_in_date")
        if not ds:
            return _utcnow()
        try:
            return datetime.fromisoformat(str(ds))
        except Exception:
            return _utcnow()
