# MARE v3.2 Fusion Plan

This is not a small MARE patch.

The current MARE codebase had no ETL pipeline, no feature store, and no ML inference path. The v3.2 work therefore adds two new subsystems beside the existing rule-based `pricing_engine.py`:

1. `mare_ml/`
   Side-by-side LightGBM demand scoring with router-based fallback.
2. `mare_etl/`
   Independent extract-transform-load pipeline backed by `feature_store.db`.

The legacy `demand_score()` path remains intact. Production risk is controlled by:

- `MARE_USE_ML=0` by default
- `MARE_USE_ML_RATIO` for grey rollout
- automatic fallback to the rule path on any ML exception

DB views are intentionally deferred. They do not block ETL, training, or inference.

