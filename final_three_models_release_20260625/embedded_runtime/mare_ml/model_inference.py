from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

from mare_ml.feature_pipeline import FEATURE_NAMES, FeaturePipeline

logger = logging.getLogger("mare_inference")

try:
    import lightgbm as lgb
except Exception:  # pragma: no cover - runtime fallback only
    lgb = None

MODEL_REGISTRY = Path(
    os.getenv(
        "MARE_MODEL_REGISTRY",
        str(Path(__file__).resolve().parent / "model_registry"),
    )
)
HOT_RELOAD_INTERVAL_SEC = int(os.getenv("MARE_HOT_RELOAD_SEC", "300"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MAREDemandInference:
    _instance: "MAREDemandInference | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, registry: Path = MODEL_REGISTRY):
        self.registry = registry
        self._rw_lock = threading.RLock()
        self._model = None
        self._pipeline = None
        self._model_path: Path | None = None
        self._loaded_at: datetime | None = None
        self._meta: dict[str, Any] = {}
        self.load_latest()
        self._stop_event = threading.Event()
        self._reload_thread = threading.Thread(target=self._hot_reload_loop, daemon=True)
        self._reload_thread.start()

    @classmethod
    def singleton(cls) -> "MAREDemandInference":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def load_latest(self) -> None:
        if lgb is None:
            raise RuntimeError("lightgbm not installed")
        current_model = self.registry / "current.txt"
        current_pipeline = self.registry / "current_pipeline.pkl"
        if not current_model.exists() or not current_pipeline.exists():
            raise FileNotFoundError("current model or pipeline pointer missing")
        model_path = current_model.resolve()
        pipeline_path = current_pipeline.resolve()
        model = lgb.Booster(model_file=str(model_path))
        pipeline = FeaturePipeline.load(pipeline_path)
        meta_path = model_path.with_suffix(".meta.json")
        meta: dict[str, Any] = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        with self._rw_lock:
            self._model = model
            self._pipeline = pipeline
            self._model_path = model_path
            self._loaded_at = _utcnow()
            self._meta = meta

    def predict(self, data: Any) -> tuple[float, list[dict[str, Any]]]:
        with self._rw_lock:
            model = self._model
            pipeline = self._pipeline
            meta = dict(self._meta)
        if model is None or pipeline is None:
            raise RuntimeError("MARE ML model not loaded")
        X = pipeline.transform(data).reshape(1, -1)
        raw_score = float(model.predict(X)[0])
        # MARE's legacy demand_score is centered around a roughly [-1, 1]
        # business scale, not a pure probability. Keep the same operational
        # range so demand_state() and downstream adjustments remain compatible.
        score = max(-1.0, min(1.0, raw_score))
        breakdown = self._explain(model, X[0], top_k=8)
        breakdown.append(
            {
                "name": "_ml_meta",
                "raw_value": raw_score,
                "value_used": score,
                "contribution": 0.0,
                "meta": {
                    "model_version": meta.get("version"),
                    "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
                },
            }
        )
        return score, breakdown

    def _explain(self, model: Any, x: np.ndarray, top_k: int = 8) -> list[dict[str, Any]]:
        try:
            importances = np.asarray(model.feature_importance(importance_type="gain"), dtype=float)
            contributions = importances * x
            idx = np.argsort(-np.abs(contributions))[:top_k]
            return [
                {
                    "name": FEATURE_NAMES[int(i)],
                    "raw_value": float(x[int(i)]),
                    "value_used": float(x[int(i)]),
                    "contribution": round(float(contributions[int(i)]), 4),
                }
                for i in idx
            ]
        except Exception as exc:
            logger.warning("ML explanation failed: %s", exc)
            return []

    def _hot_reload_loop(self) -> None:
        last_mtime = self._model_path.stat().st_mtime if self._model_path and self._model_path.exists() else 0
        while not self._stop_event.wait(HOT_RELOAD_INTERVAL_SEC):
            try:
                current_model = self.registry / "current.txt"
                if not current_model.exists():
                    continue
                target = current_model.resolve()
                mtime = target.stat().st_mtime
                if mtime > last_mtime:
                    self.load_latest()
                    last_mtime = mtime
            except Exception as exc:
                logger.warning("ML hot reload skipped: %s", exc)

    def health(self) -> dict[str, Any]:
        with self._rw_lock:
            return {
                "loaded": self._model is not None,
                "model_path": str(self._model_path) if self._model_path else None,
                "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
                "version": self._meta.get("version"),
            }
