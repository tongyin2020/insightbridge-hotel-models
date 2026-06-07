# InsightBridge 九大模型 数据因子完整审计报告

**报告时间**: 2026-06-05 11:00（修订：11:30）  
**审计范围**: 三大系统（Harness / Claude Simulation / CrewAI）共20个数据因子  
**付费API**: Firecrawl（5,506/6,500额度剩余）| Shifter（月付$20，正常）| BrightData（$200余额，需配置）

> ⚡ **修订说明（11:30）**：珠海溢出效应、口岸客流、OTA预订节奏三个因子在系统三（CrewAI）中已有完整 Firecrawl 实现代码（`firecrawl_scrapers.py`），之前标记为"降级模拟"是不准确的。实测确认4/5因子已获取真实数据，之前显示`0/4因子真实`是因为 Firecrawl 欠费导致 fallback，付费恢复后自动生效，无需代码修改。

---

## 一、全因子状态总览

| # | 因子名称 | 变量名 | 来源 | 当前状态 | 影响模型 |
|---|---------|--------|------|---------|---------|
| 1 | DSEC历史ADR | `dsec_cold_adr` | hotel_real_data.db | ✅ **正常** | 全9个 |
| 2 | DSEC需求信号 | `dsec_demand_signal` | hotel_real_data.db | ✅ **正常** | 全9个 |
| 3 | 天气 | `weather_celsius` | wttr.in | ✅ **正常** | 全9个 |
| 4 | 渡轮满座率 | `flight_ferry` | TurboJET + CotaiWaterJet | ✅ **正常** | 全9个 |
| 5 | 活动赛事密度 | `event_density` | Firecrawl → MGTO日历 | ✅ **正常** | 全9个 |
| 6 | 声誉情感修正 | `rep_adj` | hotel_real_data.db | ✅ **正常（冷启动）** | MARE/CRM |
| 7 | 弹性引擎 | `elasticity_used` | hotel_real_data.db | ✅ **正常** | MARE/ACQ |
| 8 | 访客统计 | `visitors_stats` | 静态编码（DSEC月报） | ⚠️ **静态（无实时）** | 全9个 |
| 9 | OTA官网BAR | `real_bar_avg` | hotel_real_data.db (Shifter采集) | 🔴 **停止采集（4天前）** | MARE/CRM |
| 10 | Booking.com 3★价格 | `booking_prices_3` | Playwright + Shifter代理 | 🔴 **不工作（需JS渲染）** | MARE/CRM |
| 11 | Booking.com 4-5★价格 | `upper_tier_adr` | Playwright + Shifter代理 | 🔴 **不工作（需JS渲染）** | ACQ/MARE |
| 12 | Agoda价格 | `agoda_rate` | hotel_real_data.db / Shifter | 🔴 **停止采集（4天前）** | MARE |
| 13 | 库存紧张信号 | `avail_level` | hotel_real_data.db | 🔴 **停止采集（5天前）** | MARE（价格溢价） |
| 14 | 珠海溢出效应 | `zhuhai_saturation` | Firecrawl搜索（系统三已实现） | ✅ **系统三真实数据** / ⚠️ 系统一/二场景模拟 | 全9个 |
| 15 | 口岸过境客流 | `border_flow` | Firecrawl搜索/TDM新闻（系统三已实现） | ✅ **系统三真实数据** / ⚠️ 系统一/二场景模拟 | 全9个 |
| 16 | OTA预订节奏 | `ota_booking_pace` | Firecrawl→Booking.com urgency（系统三已实现） | ✅ **系统三真实数据** / ⚠️ 系统一/二场景模拟 | 全9个 |
| 17 | IR赛事日历 | `ir_signal` | 04_IR_Event_Calendar.py | ⚠️ **运行状态未知** | 全9个 |
| 19 | BrightData代理 | — | BrightData API | 🔴 **有余额但认证失败** | 待配置 |
| 20 | hotel_data_collector | — | 自动采集脚本 | 🔴 **launchd退出码19968** | 影响9/10/11/12/13 |

**汇总：正常 10个 / 系统一/二降级模拟 3个 / 停止/失效 4个 / 永久停用 1个 / 待配置 1个 / 静态 1个**

> 🔑 **重要说明**：因子14/15/16（珠海溢出、口岸客流、OTA预订节奏）在**系统三（CrewAI）已有完整 Firecrawl 实现并实测通过（4/5真实）**；系统一、系统二仍为场景模拟，属架构差异而非故障。

---

## 二、分类详细说明

### ✅ 正常运行（7个因子）

| 因子 | 实测结果 |
|------|---------|
| DSEC ADR（3★/4★/5★） | 3★=922 / 4★=957 / 5★=1,501 MOP（2023-2025均值）✅ |
| DSEC需求信号 | +0.0724（6月淡季正向微弱信号）✅ |
| 天气 | 30°C ✅ |
| 渡轮满座率 | TurboJET+CotaiWaterJet双源，今日满座0班 ✅ |
| 活动赛事密度 | Firecrawl→MGTO日历，A-Ma Festival等已抓取 ✅ |
| 声誉情感修正 | 3条review数据（冷启动），`rep_adj=0.0` ✅ |
| 弹性引擎 | hotel_real_data.db驱动，正常优化 ✅ |

---

### 🔴 最关键问题：hotel_data_collector 停止运行

**影响5个因子**：官网BAR价格、Booking.com价格、Agoda价格、库存信号、OTA竞对价

**症状**：
- `price_snapshots` 最后采集：**2026-06-01 16:41** (4天前)
- `inventory_signals` 最后采集：**2026-05-31 17:59** (5天前)
- launchd 退出码：**19968** (= errno 78 = 脚本内部Python异常)

**影响后果**：模型定价依赖DSEC ADR冷启动（固定值），无法反映市场实时变化。

---

### 🔴 需修复因子（7个）及解决方案

#### 问题1：hotel_data_collector 停止采集（影响最大）
**根因**：launchd 退出码19968，需查看脚本错误日志  
**解决方案**：重启并修复 hotel_data_collector，使用 **Shifter 代理**（已确认代理IP正常：131.255.22.95）

#### 问题2：Booking.com 3★/4★-5★ 实时价格（因子#10/11）
**现状**：Shifter代理HTTP请求返回202（Cloudflare Challenge），需JavaScript渲染  
**解决方案A（推荐）**：改用 **Firecrawl** 直接抓取 — 实测成功获取21个价格点
> ⚠️ 注意：Firecrawl返回结果含HKD价格混入，需过滤 < 400 MOP 的价格  
**解决方案B**：hotel_data_collector 通过 Playwright + Shifter（需修复collector）

#### 问题3：OTA预订节奏 `ota_booking_pace`（因子#16）
**现状**：当前以场景模拟替代该类外部竞对价信号  
**解决方案**：用 **Firecrawl** 抓取Booking.com搜索结果页的"仅剩X间"/"Sold Out"标签  
> 可提取真实预订紧张度信号，比场景模拟更准确

#### 问题4：珠海溢出效应 `zhuhai_saturation`（因子#14）
**现状**：降级到场景模拟  
**解决方案**：**Firecrawl 搜索**"珠海酒店今晚价格" — 实测返回3条结果，可解析CNY价格作为溢出指数

#### 问题5：口岸过境客流 `border_flow`（因子#15）
**现状**：降级到场景模拟（无任何商业实时API来源）  
**解决方案**：**Firecrawl 搜索**"澳门口岸过境旅客{today}" — 可获取DSEC/TDM新闻数据，精度有限但优于纯模拟

#### 问题6：BrightData $200余额（因子#19）
**现状**：认证失败（407 Auth failed）  
**根因**：BrightData代理格式为 `brd-customer-{customer_id}-zone-{zone}:{API_key}@brd.superproxy.io:22225`，但客户ID未知  
**解决方案**：登录 [brightdata.com](https://brightdata.com) → 查看 Customer ID → 配置正确的代理格式

#### 问题7：访客统计静态化（因子#8）
**现状**：硬编码2025年月度均值，无实时  
**解决方案**：DSEC每月10号发布月报，用 **Firecrawl** 定期抓取更新，或保持现状（影响很小）

---

## 三、Firecrawl 用量规划（每月6,500次）

| 用途 | 频率 | 每月用量 | 优先级 |
|------|------|---------|--------|
| Booking.com 3★价格（每天3次） | 3次/天 | **90次/月** | 🔴 最高 |
| Booking.com 4-5★价格（每天3次） | 3次/天 | **90次/月** | 🔴 最高 |
| MGTO活动日历 | 2次/天 | **60次/月** | ✅ 已配置 |
| OTA预订节奏（Booking.com urgency） | 4次/天 | **120次/月** | 🟡 高 |
| 珠海酒店价格搜索 | 3次/天 | **90次/月** | 🟡 中 |
| 口岸过境客流新闻 | 2次/天 | **60次/月** | 🟢 低 |
| **月度合计** | — | **~510次/月** | ✅ 远低于6,500上限 |

**结论**：6,500次/月绰绰有余，每月实际消耗约500次（仅8%），可放心使用。

---

## 四、BrightData $200 使用规划

BrightData 主要用途应是 **Playwright需要的重型JS渲染页面**（Firecrawl无法处理的场景）：

| 用途 | 预计每月用量 | 费用估算 |
|------|------------|---------|
| Booking.com 3★/4-5★ 实时价格（Playwright渲染） | ~5,400次/月 | ~$18/月 |
| Agoda澳门全市场价格 | ~180次/月 | ~$1/月 |
| 酒店官网BAR（hotel_data_collector） | ~2,200次/月 | ~$8/月 |
| **月度合计** | — | **~$27/月** |

**建议上限设置**：BrightData 控制台设置 **$40/月** 上限（留有缓冲），约7.5个月用完$200余额。

---

## 五、立即修复行动计划

### Step 1（立刻）：修复 hotel_data_collector
```bash
# 查看错误
launchctl list com.insightbridge.hotel_collector
# 手动运行查看错误
cd ~/Desktop/InsightBridge_模型测试系统/hotel_collector
python3 hotel_data_collector.py --once 2>&1 | head -30
```

### Step 2（立刻）：Booking.com价格切换到Firecrawl
在 `run_simulation.py` 和 `run_21d_harness.py` 中：
```python
# 替换 Playwright 抓取为 Firecrawl（已确认可用）
from data_fetchers.real_data import fetch_booking_prices  # 修改内部实现
# 过滤 < 400 MOP 的HKD混入价格
prices_3 = [p for p in prices if 400 <= p <= 2500]
```

### Step 3（1天内）：BrightData 配置
1. 登录 brightdata.com → 找到 Customer ID
2. 配置代理: `brd-customer-{ID}-zone-residential:{token}@brd.superproxy.io:22225`
3. 在 .env 添加：`BRIGHTDATA_USER=brd-customer-{ID}-zone-residential` / `BRIGHTDATA_PASS={token}`
4. **设置月度上限 $40**（控制台 → Billing → Monthly Cap）

### Step 4（已完成）：系统三三个因子 Firecrawl 已生效 ✅
- `ota_booking_pace` → ✅ `firecrawl_scrapers.py` 已实现，Firecrawl付费恢复后自动生效
- `zhuhai_saturation` → ✅ 同上，实测返回真实值 0.229
- `border_flow` → ✅ 同上，实测返回真实值 1.0
- **系统一/二**（可选）→ 可将 `firecrawl_scrapers.py` 接入，提升两系统数据质量

---

## 六、修复后预期改善

| 指标 | 当前 | 修复后预期 |
|------|------|----------|
| 实时数据覆盖率（系统三） | **50% (10/20因子)** ← 修正（含因子14/15/16） | **75% (15/20因子)** |
| MARE价格基准准确性 | DSEC静态值 | Booking.com实时市价 |
| 预订节奏信号 | 场景模拟 | Booking.com真实urgency |
| 系统二MARE正常率 | 81.5% | 预计85%+ |
| 系统一SelfACQ定价偏差 | +62% vs DSEC | 贴近实时市价后自然收窄 |

---

*报告生成：2026-06-05 11:00 | InsightBridge AI监控系统 | Claude Sonnet 4.6*
