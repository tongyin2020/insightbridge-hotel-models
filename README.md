# InsightBridge Final Three Models Workspace

当前主工作区已经切换为“最终三个模型”模式，不再使用旧的“三大系统九模型”运行框架。

## 当前唯一主模型

- `MARE` 房价模型
- `Director` 三模块集成模型
- `SelfACQ` 自主寻客模型

最终三模型正式目录：
- `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625`

## 当前主入口

- 运行最终三模型：
  - `python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/run_final_models_only.py`
- 检查最终三模型结果：
  - `python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/check_final_models.py`

## 运行说明

- 最终三模型现在使用自己的内嵌运行层：
  - `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/final_three_models_release_20260625/embedded_runtime`
- 旧三系统源码、旧检查脚本、旧日志和旧自动化入口已经退役，不再作为当前运行主线。

## 当前保留目录

- `final_three_models_release_20260625`
- `mare_etl`
- `reports`
- `model_registry`
- `db_archives`

以上目录仍然与最终三模型、训练链路或历史分析链路有关，因此保留在当前工作区。

## 本地归档区

以下历史运行产物和缓存已移入本地归档目录：
- `/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/workspace_archive_20260625`

该目录仅作本机历史留档，不参与当前最终三模型运行，也不会推送到 Git。

## Drive 归档

旧九模型完整退役归档已保存到 Google Drive：
- `InsightBridge_legacy_archives/legacy_retired_20260625.tar.gz`
