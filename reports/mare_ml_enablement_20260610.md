# MARE ML Enablement Report

Date: 2026-06-10

## What Was Completed

- Enabled `MARE_USE_ML=1` by default in the three runtime entrypoints:
  - `system1_chatgpt_harness/run_21d_harness.py`
  - `system2_claude_simulation/run_simulation.py`
  - `system3_crewai/main.py`
- Added a bootstrap pipeline that converts historical System 1 MARE outputs into ML training rows.
- Filtered bootstrap history to the official 76-hotel roster only.
- Trained LightGBM demand models and promoted the latest model to:
  - `mare_ml/model_registry/current.txt`
  - `mare_ml/model_registry/current_pipeline.pkl`

## Current Model State

- Active model: `mare_demand_v3.txt`
- Active pipeline: `feature_pipeline_v3.pkl`
- Training rows: `384,578`
- Hotels covered: `76`
- Synthetic feature date range: `2026-01-01` to `2026-08-02`

## Validation Results

### System 1

- Real single-cycle harness smoke test completed successfully.
- Output folder:
  - `reports/ml_smoke_20260610_s1/`
- Summary:
  - `mare_runs = 836`
  - `director_runs = 836`
  - `selfacq_runs = 1064`
  - `mare_failures = 0`
  - `director_failures = 0`
  - `selfacq_failures = 0`
- ML confirmation:
  - `836 / 836` MARE rows carried `_v32_path = lightgbm`

### System 2

- Formal `run_3star_test()` smoke runs completed successfully across multiple hotels.
- Each checked MARE result carried:
  - `_v32_path`
  - `meta = lightgbm`

### System 3

- Runtime entrypoint confirmed to default to `MARE_USE_ML=1`.
- System 3 reuses System 2 pricing logic for MARE routing, so the same ML path is active there.

## Important Note

- The ML path is now real and active, but it still preserves rule-based fallback.
- If model loading or inference fails, MARE will automatically return to the legacy rule path instead of breaking runtime execution.

