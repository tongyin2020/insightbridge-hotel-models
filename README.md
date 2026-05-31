# InsightBridge 澳门酒店AI模型系统 v2026

备份日期：2026-05-31

## 系统架构：3个AI系统 × 3个模型 = 9个核心模型

---

## System 1 — ChatGPT/Harness 驱动
**目录：** `system1_chatgpt_harness/`

| 文件 | 模型 |
|------|------|
| `run_21d_harness.py` | MARE定价 + DirectorAI CRM + SelfACQ 获客（三合一运行器）|
| `hotel_universe.json` | 76家酒店数据宇宙 |

---

## System 2 — Claude 驱动仿真系统
**目录：** `system2_claude_simulation/`

| 文件 | 功能 |
|------|------|
| `run_simulation.py` | 主运行器（MARE_ALL / MARE_23_STAR / DIRECTOR / SELFACQ）|
| `pricing_engine.py` | MARE定价引擎核心 |
| `recommendations.py` | 推荐生成器 |
| `report.py` | 报告生成 |
| `hotel_roster_76.py` | 76家酒店名单 |
| `objective_modes.py` | 目标模式配置 |

---

## System 3 — CrewAI 多智能体系统
**目录：** `system3_crewai/`

| 文件 | 功能 |
|------|------|
| `main.py` | 主运行器（MARE_ALL_FC / MARE_23_STAR_FC / DIRECTOR / SELFACQ）|
| `agents.py` | 智能体定义（MARE Agent / CRM Agent / ACQ Agent）|
| `tasks.py` | 任务定义与调度 |

---

## 数据采集层
**目录：** `hotel_collector/`

| 文件 | 功能 |
|------|------|
| `hotel_data_collector.py` | 76家酒店真实数据采集器（每天09:00/22:00）|
| `acquisition_mdp.py` | 客户获取MDP决策引擎 |
| `sentiment_engine.py` | 声誉情感分析引擎 |

---

## 每日报告层
**目录：** `daily_report/`

| 文件 | 功能 |
|------|------|
| `daily_report.py` | 每日09:00微信推送（9个模型全覆盖）|

---

## 市场分级说明

| 市场 | 星级 | 参考价来源 |
|------|------|-----------|
| 2-3-4★市场 | 2/3/4星级酒店 | price_snapshots tier = 3_star + 4_star |
| 5★豪华市场 | 5星豪华 | price_snapshots tier = 5_star + 5_deluxe |

---

*InsightBridge Global — Macau Hotel AI Revenue Management System*
