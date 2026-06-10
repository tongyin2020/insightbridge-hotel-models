# MARE ML

This package adds the v3.2 demand-model path for MARE.

Current behavior:

- Rule-based `demand_score()` still exists and remains the default.
- `demand_score_router()` can switch to ML when `MARE_USE_ML=1`.
- If `lightgbm` or a trained model is unavailable, the router falls back automatically.

Files:

- `feature_pipeline.py`: 36-feature pipeline shared by training and inference
- `model_inference.py`: singleton inference service with hot reload
- `train_mare_demand.py`: training entrypoint for `feature_store.db`

