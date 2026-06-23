# InsightBridge 三模型双评分卡

生成时间：2026-06-18 11:08

| System | Model | 场景带 | 样本 | 总分 | 收益提升 | 失败率 | 异常率 |
|---|---|---:|---:|---:|---:|---:|---:|
| S1 | DIRECTOR | normal | 71896 | 82.08 | 2.54% | 0.00% | 23.69% |
| S1 | MARE | normal | 71896 | 79.65 | 5.82% | 0.00% | 57.83% |
| S1 | SELFACQ | extreme | 65360 | 94.80 | 34.18% | 0.00% | 0.00% |
| S1 | SELFACQ | normal | 26144 | 94.80 | 34.02% | 0.00% | 0.00% |
| S2 | DIRECTOR | extreme | 13639 | 86.36 | 8.14% | 0.00% | 27.19% |
| S2 | DIRECTOR | normal | 9997 | 86.29 | 5.70% | 0.00% | 11.42% |
| S2 | MARE | extreme | 13639 | 69.70 | -1.02% | 0.00% | 73.20% |
| S2 | MARE | normal | 9997 | 74.52 | -1.08% | 0.00% | 40.69% |
| S2 | SELFACQ | extreme | 13639 | 94.00 | 23.39% | 0.00% | 0.00% |
| S2 | SELFACQ | normal | 9997 | 94.00 | 27.71% | 0.00% | 0.00% |
| S3 | DIRECTOR | extreme | 13586 | 90.98 | 25.02% | 0.00% | 27.79% |
| S3 | DIRECTOR | normal | 9974 | 93.40 | 44.25% | 0.00% | 11.67% |
| S3 | MARE | extreme | 13586 | 77.83 | 6.82% | 0.00% | 73.61% |
| S3 | MARE | normal | 9974 | 82.55 | 6.81% | 0.00% | 42.09% |
| S3 | SELFACQ | extreme | 13586 | 81.85 | 0.00% | 0.00% | 0.00% |
| S3 | SELFACQ | normal | 9974 | 81.85 | 0.00% | 0.00% | 0.00% |

## 当前最佳组合

1. `S1 SELFACQ normal` — 94.80
2. `S1 SELFACQ extreme` — 94.80
3. `S2 SELFACQ normal` — 94.00
4. `S2 SELFACQ extreme` — 94.00
5. `S3 DIRECTOR normal` — 93.40
6. `S3 DIRECTOR extreme` — 90.98
