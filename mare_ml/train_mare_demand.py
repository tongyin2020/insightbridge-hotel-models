from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

from mare_ml.feature_pipeline import FEATURE_NAMES, FeaturePipeline

logger = logging.getLogger("mare_train")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

try:
    import lightgbm as lgb
except Exception:  # pragma: no cover - runtime only
    lgb = None

try:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import TimeSeriesSplit
except Exception:  # pragma: no cover - runtime only
    TimeSeriesSplit = None
    mean_absolute_error = mean_squared_error = r2_score = None

FEATURE_STORE_DB = os.getenv(
    "MARE_FEATURE_STORE",
    str(Path(__file__).resolve().parent.parent / "mare_etl" / "feature_store.db"),
)
MODEL_REGISTRY = Path(
    os.getenv(
        "MARE_MODEL_REGISTRY",
        str(Path(__file__).resolve().parent / "model_registry"),
    )
)
TARGET_COL = "demand_target"


def _utcnow() -> datetime:
    return datetime.now(UTC)

LGBM_PARAMS = {
    "objective": "regression",
    "metric": "mae",
    "boosting_type": "gbdt",
    "num_leaves": 63,
    "learning_rate": 0.05,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.9,
    "bagging_freq": 5,
    "min_child_samples": 20,
    "lambda_l2": 0.1,
    "verbose": -1,
    "n_jobs": -1,
}


def _require_training_deps() -> None:
    if lgb is None or TimeSeriesSplit is None:
        raise RuntimeError("Training dependencies missing: install lightgbm and scikit-learn first")


def load_training_data(start_date: str, end_date: str, db_path: str = FEATURE_STORE_DB) -> pd.DataFrame:
    if not Path(db_path).exists():
        raise FileNotFoundError(f"feature store missing: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT * FROM daily_features
            WHERE feature_date BETWEEN ? AND ?
              AND demand_target IS NOT NULL
            ORDER BY feature_date, hotel_id, hour
            """,
            conn,
            params=[start_date, end_date],
        )
    finally:
        conn.close()
    if df.empty:
        raise ValueError("no training data in selected range")
    return df


def train_model(df: pd.DataFrame, pipeline: FeaturePipeline, n_splits: int = 5):
    _require_training_deps()
    df = df.sort_values(["feature_date", "hotel_id", "hour"]).reset_index(drop=True)
    X = pipeline.transform_batch(df)
    y = df[TARGET_COL].values
    tscv = TimeSeriesSplit(n_splits=n_splits)
    cv_scores = []
    for fold_idx, (train_idx, valid_idx) in enumerate(tscv.split(X)):
        train_set = lgb.Dataset(X[train_idx], y[train_idx], feature_name=FEATURE_NAMES)
        valid_set = lgb.Dataset(X[valid_idx], y[valid_idx], feature_name=FEATURE_NAMES, reference=train_set)
        booster = lgb.train(
            LGBM_PARAMS,
            train_set,
            num_boost_round=1000,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
        )
        pred = booster.predict(X[valid_idx])
        cv_scores.append(
            {
                "fold": fold_idx,
                "mae": float(mean_absolute_error(y[valid_idx], pred)),
                "rmse": float(np.sqrt(mean_squared_error(y[valid_idx], pred))),
                "r2": float(r2_score(y[valid_idx], pred)),
                "best_iter": int(booster.current_iteration()),
            }
        )
    full_train = lgb.Dataset(X, y, feature_name=FEATURE_NAMES)
    best_iter = max(int(np.median([s["best_iter"] for s in cv_scores])), 200)
    final_model = lgb.train(LGBM_PARAMS, full_train, num_boost_round=best_iter)
    metrics = {
        "cv_mae_mean": float(np.mean([s["mae"] for s in cv_scores])),
        "cv_rmse_mean": float(np.mean([s["rmse"] for s in cv_scores])),
        "cv_r2_mean": float(np.mean([s["r2"] for s in cv_scores])),
        "cv_folds": cv_scores,
        "n_train_samples": int(len(X)),
        "n_features": int(X.shape[1]),
        "final_n_iter": int(final_model.num_trees()),
    }
    return final_model, metrics


def get_next_version(registry: Path) -> int:
    registry.mkdir(parents=True, exist_ok=True)
    versions = []
    for path in registry.glob("mare_demand_v*.txt"):
        try:
            versions.append(int(path.stem.split("_v")[-1]))
        except Exception:
            pass
    return (max(versions) + 1) if versions else 1


def save_model(model: Any, pipeline: FeaturePipeline, metrics: dict, registry: Path = MODEL_REGISTRY) -> int:
    version = get_next_version(registry)
    model_path = registry / f"mare_demand_v{version}.txt"
    meta_path = registry / f"mare_demand_v{version}.meta.json"
    pipeline_path = registry / f"feature_pipeline_v{version}.pkl"
    model.save_model(str(model_path))
    pipeline.version = f"v{version}"
    pipeline.save(pipeline_path)
    meta = {
        "version": version,
        "trained_at": _utcnow().isoformat(),
        "feature_names": FEATURE_NAMES,
        "n_features": len(FEATURE_NAMES),
        "metrics": metrics,
        "params": LGBM_PARAMS,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    current_model = registry / "current.txt"
    current_pipeline = registry / "current_pipeline.pkl"
    if current_model.exists() or current_model.is_symlink():
        current_model.unlink()
    if current_pipeline.exists() or current_pipeline.is_symlink():
        current_pipeline.unlink()
    current_model.symlink_to(model_path.name)
    current_pipeline.symlink_to(pipeline_path.name)
    return version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["full", "incremental", "validate"], default="full")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--model-path")
    args = parser.parse_args()

    _require_training_deps()
    if args.mode == "validate":
        if not args.model_path:
            raise SystemExit("--model-path is required for validate mode")
        model_path = Path(args.model_path)
        logger.info("Validation mode is reserved; use holdout workflow with existing ETL data")
        logger.info("Model path: %s", model_path)
        return 0

    end_date = args.end or _utcnow().date().isoformat()
    start_date = args.start
    if not start_date:
        if args.mode == "incremental":
            start_date = (_utcnow().date() - timedelta(weeks=args.weeks)).isoformat()
        else:
            raise SystemExit("--start is required for full mode")

    df = load_training_data(start_date, end_date)
    pipeline = FeaturePipeline().fit(df, target_col=TARGET_COL)
    model, metrics = train_model(df, pipeline)
    version = save_model(model, pipeline, metrics)
    logger.info("Saved MARE demand model v%s", version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
