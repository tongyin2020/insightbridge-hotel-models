# Final Three-Model Build Summary

## Final Selection

- `MARE`: `System 3 / MARE_ALL_FC`
- `Director`: `System 3 / DIRECTOR_CRM_ALL_FC`
- `SelfACQ`: `System 1 / SelfACQ`

## Build Principle

This release does not merely preserve the paper selection result. It converts the selected production logic into three directly runnable final-entry scripts:

- [01_MARE_Final/run_final_mare.py](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/01_MARE_Final/run_final_mare.py)
- [02_Director_Final/run_final_director.py](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/02_Director_Final/run_final_director.py)
- [03_SelfACQ_Final/run_final_selfacq.py](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/03_SelfACQ_Final/run_final_selfacq.py)

Each final entry keeps the selected winning system as the backbone while preserving access to the real project runtime:

- real signals from the existing Macau data stack
- Firecrawl-enhanced signal path used by System 3
- shared HROS V6 decision layers
- shared hotel roster and pricing modules
- existing MARE ML switch and learned state compatibility

## Verified Outputs

Validated top-level runner:

- [run_final_model_suite.py](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/run_final_model_suite.py)

Validated sample outputs:

- [01_MARE_Final/output_samples/mare_final_20260625T195340Z.json](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/01_MARE_Final/output_samples/mare_final_20260625T195340Z.json)
- [02_Director_Final/output_samples/director_final_20260625T195342Z.json](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/02_Director_Final/output_samples/director_final_20260625T195342Z.json)
- [03_SelfACQ_Final/output_samples/selfacq_final_20260625T195342Z.json](/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/03_SelfACQ_Final/output_samples/selfacq_final_20260625T195342Z.json)

## Notes

- The final package is saved directly inside the desktop master project folder.
- The package includes the uploaded final-selection documents, deployment checklist, benchmark reports, and source snapshots.
- The wrapper scripts are runnable independently and do not require manual `cd` into the package root.
