# InsightBridge 三模型双评分卡

生成时间：2026-06-17 01:57

| System | Model | 场景带 | 样本 | 总分 | 收益提升 | 失败率 | 异常率 |
|---|---|---:|---:|---:|---:|---:|---:|
| S1 | DIRECTOR | normal | 44308 | 82.13 | 2.59% | 0.00% | 23.73% |
| S1 | MARE | normal | 44308 | 79.64 | 5.71% | 0.00% | 57.13% |
| S1 | SELFACQ | extreme | 40280 | 94.80 | 33.72% | 0.00% | 0.00% |
| S1 | SELFACQ | normal | 16112 | 94.80 | 33.47% | 0.00% | 0.00% |
| S2 | DIRECTOR | extreme | 12564 | 86.78 | 8.63% | 0.00% | 27.63% |
| S2 | DIRECTOR | normal | 8564 | 86.85 | 6.25% | 0.00% | 11.35% |
| S2 | MARE | extreme | 12564 | 69.75 | -1.26% | 0.00% | 71.26% |
| S2 | MARE | normal | 8564 | 74.01 | -1.63% | 0.00% | 40.44% |
| S2 | SELFACQ | extreme | 12564 | 94.00 | 23.87% | 0.00% | 0.00% |
| S2 | SELFACQ | normal | 8564 | 94.00 | 26.50% | 0.00% | 0.00% |
| S3 | DIRECTOR | extreme | 12511 | 90.94 | 25.85% | 0.00% | 28.07% |
| S3 | DIRECTOR | normal | 8541 | 93.40 | 44.27% | 0.00% | 11.68% |
| S3 | MARE | extreme | 12511 | 78.10 | 6.81% | 0.00% | 71.73% |
| S3 | MARE | normal | 8541 | 82.55 | 6.78% | 0.00% | 41.86% |
| S3 | SELFACQ | extreme | 12511 | 81.85 | 0.00% | 0.00% | 0.00% |
| S3 | SELFACQ | normal | 8541 | 81.85 | 0.00% | 0.00% | 0.00% |

## 当前最佳组合

1. `S1 SELFACQ normal` — 94.80
2. `S1 SELFACQ extreme` — 94.80
3. `S2 SELFACQ normal` — 94.00
4. `S2 SELFACQ extreme` — 94.00
5. `S3 DIRECTOR normal` — 93.40
6. `S3 DIRECTOR extreme` — 90.94
