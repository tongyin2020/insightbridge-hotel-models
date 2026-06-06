# HROS V6 数学逻辑验证文档
# 供 Wolfram Alpha / DeepSeek / Gemini 验证使用

---

## 模块一：指数平滑（HotelLearningLoop._smooth）

### 公式
```
new_value = old * (1 - α) + new_obs * α     α = 0.25
```

### 验证问题
1. 证明：从任意初值 x₀ 出发，若所有观测值恒为常数 C，序列收敛到 C。
   - 递推: xₙ = (1-α)xₙ₋₁ + α·C = (1-α)ⁿ·x₀ + C·[1-(1-α)ⁿ]
   - 当 n→∞ 且 0<α<1 时，(1-α)ⁿ→0，故 xₙ→C ✓

2. 半衰期计算：α=0.25 时，旧数据权重降到 50% 需要多少周？
   - (1-0.25)ⁿ = 0.5 → n = log(0.5)/log(0.75) ≈ 2.41 周
   - Wolfram: Solve[(0.75)^n = 0.5, n]

3. 4周后旧数据残余权重：(0.75)^4 = 0.316 (31.6%)

### 边界条件
- old=None → 返回 new（首次校准用观测值直接初始化）✓
- new=None → 返回 old（缺数据不更新）✓

---

## 模块二：弹性惩罚函数（ElasticityEngineV6.penalty）

### 分段函数定义
设 premium = (price - market_price) / market_price，e = effective_elasticity

```
penalty(premium, e) =
  premium < 0           : max(-0.35, 0.45·e·premium)
  0 ≤ premium ≤ 0.05    : 0.30·premium·e
  0.05 < premium ≤ 0.15 : [0.30·0.05 + 1.00·(premium - 0.05)]·e
  0.15 < premium ≤ 0.25 : [0.30·0.05 + 1.00·0.10 + 1.80·(premium - 0.15)]·e
  premium > 0.25        : [0.30·0.05 + 1.00·0.10 + 1.80·0.10 + 3.00·(premium - 0.25)]·e
```

### 连续性验证（分段点左右极限相等）

**breakpoint premium = 0.05：**
- 左极限: 0.30·0.05·e = 0.015·e
- 右极限（用第3段公式）: [0.015 + 1.00·(0.05-0.05)]·e = 0.015·e  ✓

**breakpoint premium = 0.15：**
- 第3段值: [0.015 + 1.00·0.10]·e = 0.115·e
- 第4段右极限: [0.015 + 0.10 + 1.80·(0.15-0.15)]·e = 0.115·e  ✓

**breakpoint premium = 0.25：**
- 第4段值: [0.015 + 0.10 + 1.80·0.10]·e = [0.015 + 0.10 + 0.18]·e = 0.295·e
- 第5段右极限: [0.015 + 0.10 + 0.18 + 3.00·0·e] = 0.295·e  ✓

### 斜率递增性验证（斜率: 0.30 → 1.00 → 1.80 → 3.00）
- 0.30 < 1.00 < 1.80 < 3.00 → 惩罚函数为凸函数 ✓
- 经济含义：超高定价时需求损失加速，符合酒店收益管理实践

### 占用率边界
```
occ = clamp(base_occ · (1 - penalty), 0.05, 0.98)
```
- 即便 penalty > 1（极端溢价），占用率不低于 5%（避免数学奇点）
- 即便 premium < 0（打折），占用率不超过 98%（物理上限）

---

## 模块三：RevPAR 离散优化（RevenueDecisionLayerV6.optimize）

### 目标函数
```
objective(p) = p·occ(p) + ancillary·occ(p) + clv·occ(p) + mkt_weight·occ(p)·market
             = occ(p)·[p + ancillary + clv + mkt_weight·market]
```

### 离散搜索范围
```
P = {floor, floor+step, floor+2·step, ..., ⌊ceiling/step⌋·step}
步长: 3★4★=10 MOP, 5★=50 MOP
```

### 为什么用离散搜索而非微积分求导？
- occ(p) 是分段线性函数（由弹性惩罚函数决定），在分段点不可导
- 离散搜索可精确处理分段点，不依赖梯度
- 酒店挂牌价本身就是离散整数（澳门酒店通常以 10 MOP 为单位）
- 搜索空间最大约 (2000-700)/10 = 130 个候选点，计算成本极低

### 提升率公式
```
lift_pct = (best_revpar - baseline_revpar) / baseline_revpar × 100%
baseline_revpar = market_price × base_occ
```

### 验证用例（3星级）
- market_price = 900, base_occ = 0.75, floor = 720, ceiling = 1500, step = 10
- base_elasticity = 0.85, demand_state = "NORMAL" → multiplier = 1.00 → e = 0.85
- 候选价格: 720, 730, ..., 1500
- baseline_revpar = 900 × 0.75 = 675 MOP/间夜
- 最优价格由 Wolfram 数值求解：argmax_{p} p·(0.75·(1-penalty(p,0.85)))

---

## 模块四：SelfACQ LTV（SelfACQEngineV6.evaluate）

### 公式分解
```
ota_net = ota_gross × (1 - commission_rate)

discounted_future = repeat_prob × future_margin / (1 + discount_rate)
  ← 单期折现：discount_rate = 10% per annum (简化为单期模型)

expected_direct_ltv = conversion_prob × (direct_price + discounted_future + crm_value)
                      - acquisition_cost - discount_cost

advantage = expected_direct_ltv - ota_net
direct_wins = (advantage > 0)
```

### 验证问题
1. 折现因子：以 10% 年折现率，1 年后 future_margin=500 的现值：
   PV = 500 / (1 + 0.10) = 454.55 MOP

2. 损益平衡条件（advantage = 0）：
   conversion_prob × (direct_price + discounted_future + crm_value) = ota_net + acquisition_cost + discount_cost
   
3. 参数敏感性分析（Wolfram）：
   对 repeat_probability 求偏导：
   ∂(expected_direct_ltv)/∂(repeat_prob) = conversion_prob × future_margin / (1 + discount_rate)
   → 每增加 1% 复购概率，LTV 增加 conversion_prob × 4.545 MOP（future_margin=500,r=10%）

---

## 模块五：收益归因分解（RevenueAttributionEngine.attribute）

### 公式
```
baseline_revpar    = baseline_adr × baseline_occ
mare_revpar        = mare_adr × mare_occ
mare_lift          = mare_revpar - baseline_revpar

crm_lift           = crm_incremental_value / rooms_available
selfacq_lift       = selfacq_incremental_value / rooms_available

total_lift         = mare_lift + crm_lift + selfacq_lift
optimized_revpar   = baseline_revpar + total_lift

total_lift_pct     = total_lift / baseline_revpar × 100%
```

### 数学结构分析
这是一个**加法分解**（Additive Decomposition）而非乘法分解：
- MARE 贡献 = 定价优化收益（直接乘积效果）
- CRM 贡献 = 关系营销增量（per-room 摊销）
- SelfACQ 贡献 = 直销获客净收益（per-room 摊销）

### 验证（Wolfram 数值验证）
```
baseline_adr=1000, baseline_occ=0.75 → baseline_revpar = 750
mare_adr=1080, mare_occ=0.74       → mare_revpar = 799.2 → mare_lift = 49.2
crm_incremental_value=200, rooms=50 → crm_lift = 4.0
selfacq_incremental_value=150, rooms=50 → selfacq_lift = 3.0
total_lift = 56.2, optimized_revpar = 806.2
total_lift_pct = 56.2/750 × 100 = 7.49%
```

---

## 模块六：风险评分与置信度

### 风险评分
```
risk_raw = max(0, premium-0.05)×180 + max(0, 0.70-occ)×80
         + max(0, 0.45-pace)×60 + max(0, 0.60-quality)×35
risk_score = min(100, risk_raw / 3)
```

### 置信度
```
confidence = clamp(95 - 0.30×risk + 0.05×quality×100, 40, 95)
```

### 验证
- 理想场景（premium=0, occ=0.85, pace=0.6, quality=1.0）：
  risk_raw = 0 + 0 + 0 + 0 = 0 → risk = 0
  confidence = clamp(95 - 0 + 5, 40, 95) = 95% ✓
  
- 压力场景（premium=0.30, occ=0.55, pace=0.30, quality=0.40）：
  risk_raw = (0.30-0.05)×180 + (0.70-0.55)×80 + (0.45-0.30)×60 + (0.60-0.40)×35
           = 45 + 12 + 9 + 7 = 73 → risk = 73/3 = 24.3
  confidence = clamp(95 - 7.29 + 2, 40, 95) = 89.7% ✓

---

## 模块七：HotelLearningLoop 流水线正确性

### 管道流程
```
Week i DB records
    ↓ build_records_from_sqlite()
WeeklyHotelRecord[]
    ↓ HotelLearningLoop.summarize()
{adr, channel_mix, room_type_adr, ...}
    ↓ HotelLearningLoop.update_hotel_profile(α=0.25)
updated_profile {baseline_adr(new)}
    ↓ save_profiles()  [atomic tmp-file replace]
hotel_profiles_v6.json
    ↓ get_baseline_adr(hotel_id)
float → 下一周 RevenueDecisionLayerV6.optimize() 的 market_signal.market_price 参照
```

### 校准状态机
```
calibration_weeks < 4 → status = "learning"
calibration_weeks ≥ 4 → status = "first_calibration"
```
经济含义：≥4 周（≥28 天）才开始生效为定价基准，避免噪声数据过早影响策略。

---

## 潜在问题清单（请 Gemini 重点检查）

1. **rooms_sold 语义混淆**：当前用 `occupancy`（0~1）作为 `rooms_sold`，导致 ADR 计算时分母是小数而非整数间数。
   - ADR = revenue / rooms_sold = (price × occ) / occ = price → ADR 恒等于价格，失去意义。
   - **修复建议**：rooms_sold 应为 occ × total_rooms，或归一化为单间处理。

2. **channel_mix 直接替换而非平滑**：update_hotel_profile 中 channel_mix 是直接赋值，不走指数平滑。
   - 若某周某渠道无记录，该渠道 mix 会清零（数据稀疏时不稳定）。
   - **修复建议**：对 channel_mix 也应用指数平滑。

3. **calibration_status 阈值硬编码**：`>= 4` 周触发 first_calibration，但模拟周期可能只有 3 周（504小时 = 3周）。
   - 实际模拟完成后只能到 3 次校准，状态永远是 "learning"。
   - **修复建议**：临时降至 >= 3，或通过参数传入。

4. **DB 事务并发**：SQLite WAL 模式下，模拟进程正在写入时流水线读取，可能读到不完整事务。
   - **修复建议**：使用 `conn.execute("PRAGMA wal_checkpoint")` 或加读锁。

5. **hour_index 列存在性**：System 3 (CrewAI) 的 results 表可能没有 hour_index 列。
   - **修复建议**：先 PRAGMA table_info 确认列名存在，否则 fallback 到无条件取全部记录。

6. **JSON 原子写入**：tmp.replace() 在 Windows 跨盘符时不原子，macOS/Linux 下正常。当前系统无风险。

7. **learning_rate=0.25 固定**：未来酒店数据量大时应动态调整（数据越多，α 越小）。
   - 建议：α = 1/(n+1) 的自适应 Welford 均值法，但需要保存历史计数。
