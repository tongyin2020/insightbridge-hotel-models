# Source Snapshot Manifest

## MARE Final

Source snapshots for MARE are stored in:

- [01_MARE_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/01_MARE_Final/source_snapshots)

Backbone sources:

- `system3_crewai/main.py`
- `system3_crewai/tools/firecrawl_scrapers.py`
- `system2_claude_simulation/run_simulation.py`
- `system2_claude_simulation/pricing_engine.py`
- `system2_claude_simulation/objective_modes.py`
- `system2_claude_simulation/recommendations.py`
- `system2_claude_simulation/hotel_roster_76.py`
- `system2_claude_simulation/model_refinement.py`
- `system2_claude_simulation/data/model_weights.json`
- `system2_claude_simulation/data_fetchers/real_data.py`
- `system2_claude_simulation/data_fetchers/scenario_engine.py`
- `hotel_collector/elasticity_engine.py`
- `hotel_collector/dsec_loader.py`
- `hotel_collector/sentiment_engine.py`
- `hotel_collector/mare_ml_layer.py`
- `hotel_collector/maml_reserved.py`
- `hotel_collector/macau_ancillary_profiles.json`
- `hotel_collector/macau_static_revenue_profiles.json`
- `共用_HROS_V6引擎/hros_v6/integration_adapter.py`
- `共用_HROS_V6引擎/hros_v6/revenue_decision_layer_v6.py`
- `共用_HROS_V6引擎/hros_v6/elasticity_engine_v6.py`

## Director Final

Source snapshots for Director are stored in:

- [02_Director_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/02_Director_Final/source_snapshots)

Additional Director-specific sources:

- `共用_HROS_V6引擎/hros_v6/crm_guardrails.py`

Excluded from the final package copy because of artifact size and handoff clarity:

- `reports/director_outcome_store.db`
  Authoritative local path remains:
  `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/reports/director_outcome_store.db`

## SelfACQ Final

Source snapshots for SelfACQ are stored in:

- [03_SelfACQ_Final/source_snapshots](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/03_SelfACQ_Final/source_snapshots)

Backbone sources:

- `system1_chatgpt_harness/run_21d_harness.py`
- `system2_claude_simulation/run_simulation.py`
- `system2_claude_simulation/model_refinement.py`
- `system2_claude_simulation/pricing_engine.py`
- `system2_claude_simulation/hotel_roster_76.py`
- `hotel_collector/sentiment_engine.py`
- `hotel_collector/mare_ml_layer.py`
- `共用_HROS_V6引擎/hros_v6/selfacq_engine_v6.py`
- `共用_HROS_V6引擎/hros_v6/revenue_attribution_engine.py`
- `共用_HROS_V6引擎/hros_v6/integration_adapter.py`
