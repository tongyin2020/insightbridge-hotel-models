# InsightBridge Final Three Models Release

This package is the formal convergence bundle for the final three optimized models selected from the nine-model comparison:

- `MARE` final baseline: `System 3`
- `Director` final baseline: `System 3`
- `SelfACQ` final baseline: `System 1`

Package location:
- `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625`

## Structure

- `01_MARE_Final/`
- `02_Director_Final/`
- `03_SelfACQ_Final/`
- `04_Final_Selection_Report/`
- `05_Config_and_Prompts/`
- `06_Evaluation_and_Benchmark/`
- `07_Deployment_Checklist/`

## Main commands

- Final MARE:
  - `python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/01_MARE_Final/run_final_mare.py`
- Final Director:
  - `python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/02_Director_Final/run_final_director.py`
- Final SelfACQ:
  - `python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/03_SelfACQ_Final/run_final_selfacq.py`

## Notes

- This is a real code bundle inside the desktop project, not only a selection memo.
- The final three models now run from an embedded runtime under `embedded_runtime/` so they no longer need the old top-level three-system launch flow.
- Each final model folder also contains source snapshots for archival and handoff.
