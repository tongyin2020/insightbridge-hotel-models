# Source Snapshot Manifest

## MARE Final

Source snapshots for MARE are stored in:

- [01_MARE_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/01_MARE_Final/source_snapshots)

Backbone sources:

- `embedded_runtime/system3_crewai/tools/firecrawl_scrapers.py`
- `embedded_runtime/system2_claude_simulation/run_simulation.py`
- `embedded_runtime/system2_claude_simulation/pricing_engine.py`
- `embedded_runtime/system2_claude_simulation/objective_modes.py`
- `embedded_runtime/system2_claude_simulation/recommendations.py`
- `embedded_runtime/system2_claude_simulation/hotel_roster_76.py`
- `embedded_runtime/model_refinement.py`
- `embedded_runtime/system2_claude_simulation/data/model_weights.json`
- `embedded_runtime/system2_claude_simulation/data_fetchers/real_data.py`
- `embedded_runtime/system2_claude_simulation/data_fetchers/scenario_engine.py`
- `embedded_runtime/hotel_collector/elasticity_engine.py`
- `embedded_runtime/hotel_collector/dsec_loader.py`
- `embedded_runtime/hotel_collector/sentiment_engine.py`
- `embedded_runtime/hotel_collector/mare_ml_layer.py`
- `embedded_runtime/hotel_collector/maml_reserved.py`
- `embedded_runtime/hotel_collector/macau_ancillary_profiles.json`
- `embedded_runtime/hotel_collector/macau_static_revenue_profiles.json`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/integration_adapter.py`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/revenue_decision_layer_v6.py`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/elasticity_engine_v6.py`

## Director Final

Source snapshots for Director are stored in:

- [02_Director_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/02_Director_Final/source_snapshots)

Additional Director-specific sources:

- `embedded_runtime/共用_HROS_V6引擎/hros_v6/crm_guardrails.py`

Excluded from the final package copy because of artifact size and handoff clarity:

- `reports/director_outcome_store.db`
  Authoritative local path remains:
  `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/reports/director_outcome_store.db`

## SelfACQ Final

Source snapshots for SelfACQ are stored in:

- [03_SelfACQ_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/03_SelfACQ_Final/source_snapshots)

Backbone sources:

- `embedded_runtime/system2_claude_simulation/run_simulation.py`
- `embedded_runtime/model_refinement.py`
- `embedded_runtime/system2_claude_simulation/pricing_engine.py`
- `embedded_runtime/system2_claude_simulation/hotel_roster_76.py`
- `embedded_runtime/hotel_collector/sentiment_engine.py`
- `embedded_runtime/hotel_collector/mare_ml_layer.py`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/selfacq_engine_v6.py`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/revenue_attribution_engine.py`
- `embedded_runtime/共用_HROS_V6引擎/hros_v6/integration_adapter.py`
