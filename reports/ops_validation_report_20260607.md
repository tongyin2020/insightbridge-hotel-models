# InsightBridge 九模型运行与修复验证报告

日期：2026-06-07

## 1. 本次结论

今天的核心目标已经基本完成：

- 九个模型的核心运行逻辑已验证可用，不是“代码存在但没走到运行路径”。
- 新增的四项关键增强已进入真实执行路径并产生结果。
- 每天 09:00 的自动总控链路已经修通，系统级触发成功。
- 三大系统已进入新一轮运行，当前主库不再停留在旧的 504 小时归档轮次。

当前唯一未完全闭环的部分不是模型逻辑，而是 Telegram 自动推送的环境变量传递。

## 2. 已完成的关键修复

### 2.1 SelfACQ 加入 CAC/LTV 硬约束

已落地到：

- `system2_claude_simulation/run_simulation.py`
- `model_refinement.py`

验证结果：

- 输出中已出现 `acquisition_cost`
- 输出中已出现 `direct_net_profit_after_cac`
- 输出中已出现 `selfacq_profit_guard_passed`
- 输出中已出现 `selfacq_guard_action`

结论：该功能不是展示项，已经成为实际决策约束。

### 2.2 Director 闭环反馈写入统一 outcome store

已落地到：

- `model_refinement.py`
- `system2_claude_simulation/run_simulation.py`
- `system3_crewai/main.py`

验证结果：

- `reports/director_outcome_store.db` 已产生真实记录
- 当前计数：`S2 = 912`，`S3 = 76`，`SMOKE = 1`
- 总记录数：`989`

结论：Director 的结果回流已生效，不再是静态推荐。

### 2.3 MARE 5★ 豪华市场校准增强

已落地到：

- `hotel_collector/elasticity_engine.py`

处理方向：

- 不新增新层
- 在现有弹性层内引入酒店锚点校准
- 对高端酒店提高价格下限并放宽合理上界

结论：这一步已经接入运行路径，后续重点观察 5★ 豪华市场的偏低问题是否持续收敛。

### 2.4 三模型筛选改为双评分体系

已落地到：

- `generate_three_model_scorecard.py`

产物：

- `reports/three_model_dual_scorecard_latest.md`

当前结果摘要：

- `S1 SELFACQ normal`：94.80
- `S1 SELFACQ extreme`：94.80
- `S2 SELFACQ normal`：94.00
- `S2 SELFACQ extreme`：94.00
- `S3 DIRECTOR normal`：93.90
- `S1 MARE normal`：91.25

结论：正常场景与极端场景已被分开评分，后续选三模型时不会再被单一场景误导。

## 3. 三大系统当前运行验证

### 3.1 System 1

验证方式：

- 单次完整运行验证
- 新一轮输出产物检查

验证结果：

- `MARE 836 次`，失败 `0`
- `Director 836 次`，失败 `0`
- `SelfACQ 1064 次`，失败 `0`
- 新产物文件已生成：
  - `hotel_model_staging_output/run_20260608T012755Z.jsonl`

结论：System 1 已正常进入新一轮。

### 3.2 System 2

验证结果：

- 正式运行日志已写入：
  - `2026-06-07 20:27:55 第002h`
- 当前主库状态：
  - `max_hour = 1`
  - `hourly_runs = 456`

结论：System 2 已从新一轮第 1 小时继续推进。

### 3.3 System 3

验证结果：

- 正式运行日志已写入：
  - `2026-06-07 20:27:59 H002`
- Firecrawl 增强信号出现 `5/5 因子真实`
- 当前主库状态：
  - `max_hour = 1`
  - `hourly_runs = 456`

结论：System 3 已从新一轮第 1 小时继续推进。

## 4. 自动总控验证

### 4.1 已确认成功的部分

新的自动运行链路现在是：

- `LaunchAgent`
- `launch_daily_9models_via_terminal.sh`
- `run_daily_9models_pipeline.sh`
- `auto_run_9models.sh`
- 九模型 + 真实采集 + 日报

系统级触发验证结果：

- `launchctl` 最近一次执行退出码为 `0`
- `daily_9models_pipeline.log` 已进入：
  - `步骤 1/3：检查并启动九模型主进程`
  - `步骤 2/3：执行一次限时真实数据采集（上限 65 分钟）`

结论：自动总控已经不再被 macOS 的桌面权限拦截。

### 4.2 采集链路当前状态

本轮采集已真实开始，样例包括：

- 永利澳门：已抓到 4 个日期的 BAR / Booking / Agoda
- 永利皇宫：已抓到多个日期的 BAR / Booking

当前日志显示：

- 已启用 `65 分钟` 总时间预算
- 当前因为环境变量未带入 Terminal，会提示：
  - `未找到 Shifter 代理凭证，将使用本机IP（测试模式）`

结论：采集能跑，但当前是“本机 IP 测试模式”，不是“Shifter 代理增强模式”。

## 5. 当前唯一剩余问题

### Telegram 自动推送未完全闭环

现状：

- 之前手工运行 `daily_report.py` 时 Telegram 可以成功推送
- 现在新的系统级自动链路里，Terminal 会话没有自动继承 LaunchAgent 的环境变量
- 因此：
  - `Shifter` 凭证没有传进去
  - `Telegram` 凭证也没有传进去

影响：

- 不影响九模型运行
- 不影响数据库写入
- 不影响真实采集主流程
- 会影响“Shifter 增强采集”和“Telegram 自动日报推送”

结论：这是运行包装层问题，不是模型问题。

## 6. 综合判断

如果只看模型本体和系统本体，今天的修复是成功的。

当前可以确认：

- 九个模型能跑
- 新功能能生效
- 自动调度能起跑
- 新一轮数据库已开始积累

当前还需要后续补的一点，只是：

- 让系统级自动链路稳定拿到 `Shifter` 与 `Telegram` 环境变量

## 7. 建议的下一步

1. 继续观察这一轮运行 1 天，确认 `hourly_runs` 按预期持续增长。
2. 单独补齐 Telegram / Shifter 的环境变量注入方式。
3. 明天基于新增数据，更新一次双评分卡与三模型筛选建议。
