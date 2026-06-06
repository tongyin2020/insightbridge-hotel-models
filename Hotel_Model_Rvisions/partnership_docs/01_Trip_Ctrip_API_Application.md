# 携程 / Trip.com API 申请指南 + 产品介绍信

---

## 一、申请路径（两条并行）

### 路径 A：Trip.com 开发者平台（国际版，首选）
**网址：** https://developers.trip.com/  
**适合：** 美国 LLC 主体，英文申请，审批较快

**步骤：**
1. 访问 https://connect.trip.com/ → 点击 "Sign Up" 注册企业开发者账号
2. 选择合作类型：**Technology Partner / Data Partner**（不是 Affiliate）
3. 填写企业信息（LLC 注册证明、EIN 税号、公司网站）
4. 提交"Use Case Description"——把下方的产品介绍信内容填入
5. 等待商务团队联系（通常 3–7 个工作日）

**所需材料清单：**
- [ ] 美国 LLC 注册证书（Certificate of Formation）
- [ ] EIN 税号文件
- [ ] 公司官网（insightbridge.global）
- [ ] 产品技术说明（见下方英文信）
- [ ] 数据使用目的声明（非商业测试阶段）
- [ ] 联系人姓名、职位、邮箱、电话

### 路径 B：携程开放平台（中国版，备用）
**网址：** https://open.ctrip.com/  
**适合：** 如需抓取中文端携程数据

**注意：** 中国版携程开放平台要求中国大陆或香港企业资质，LLC 申请较难。
建议优先走 Trip.com 国际版路径 A，如被拒再考虑通过香港合作方代申请。

---

## 二、产品介绍信（英文，用于 Trip.com 申请）

---

**Subject: Technology Partnership Application — Hotel AI Revenue Optimization Platform for Macau SAR Market**

**Date:** May 30, 2026

**To:** Partnership & Developer Relations Team  
Trip.com Group / Ctrip International

**From:** Dr. Tong Yin, Founder & CEO  
InsightBridge Global LLC  
insightbridge.global

---

Dear Trip.com Partnership Team,

I am writing to formally apply for API data access through the Trip.com Technology Partner Program. InsightBridge Global LLC is a US-registered hospitality intelligence company that has developed a suite of AI-driven revenue optimization models specifically designed for the Macau SAR hotel market.

### About InsightBridge Global

InsightBridge Global is the developer of three proprietary AI models purpose-built for Macau's unique hospitality landscape — one of the most competitive and price-volatile hotel markets in the world, with nearly 500 properties operating across all star categories within a 30 km² footprint.

Our three core models are:

**① MARE — Market-Adaptive Revenue Engine (2–3 Star Hotels)**
A dynamic pricing model designed for Macau's mid-market segment. MARE processes real-time competitive rate signals, occupancy curves, demand forecasts, local event density, weather data, and ferry/border crossing indicators to generate daily optimized room pricing recommendations. In our 21-day simulation across 145 Macau 2–3 star properties, MARE consistently delivered an estimated 4.6–5% RevPAR uplift over static weekly pricing strategies.

**② DirectorAI CRM — Integrated CRM/RMS/PMS Model (2–3 Star Hotels)**
A guest relationship and channel management intelligence layer that integrates CRM loyalty signals, PMS booking data, and dynamic pricing into a unified decision engine. This model addresses the "channel dependency trap" — helping smaller Macau hotels reduce OTA commission costs while building direct booking equity.

**③ SelfACQ — Autonomous Guest Acquisition Engine (4–5 Star Hotels)**
A direct booking optimization model for luxury and integrated resort properties. SelfACQ generates multi-channel pricing strategies with OTA/direct/VIP tiered pricing differentials, automated bundle package generation (6+ offer types per cycle), and forward-looking demand capture logic.

### Why We Need Trip.com API Data

Trip.com is the dominant OTA for Macau's core visitor segment — mainland Chinese travelers, Hong Kong day-trippers, and Southeast Asian leisure guests. This visitor base represents the primary revenue driver for Macau's 2–3 star hotel segment.

Our AI models currently integrate real-time competitive pricing data via established channels, but lack granular access to:

1. **Real-time rate and availability data** from Trip.com's Macau hotel listings, broken down by room type, cancellation policy, and meal plan
2. **Booking velocity signals** (search-to-book conversion proxies) to improve our demand forecasting accuracy for Macau's high-frequency peak/off-peak cycles
3. **Historical rate trend data** for model backtesting and calibration against actual market pricing over 12–24 month windows

### Intended Use and Data Governance

This API access is requested strictly for **model validation and accuracy improvement during our pre-commercial testing phase**. All data will be:
- Used exclusively for algorithmic model refinement within our secure development environment
- Not redistributed, resold, or shared with third parties
- Handled in compliance with all applicable data protection regulations (US, Macau SAR, GDPR principles)
- Subject to Trip.com's API terms of service and data usage policies

We are actively in discussion with **Shiji Group** (MGM Macau, Melco's PMS provider) regarding a formal RMS integration partnership, and anticipate transitioning to a fully licensed commercial data arrangement once our models complete validation and enter the market.

### Our Commitment to Partnership

InsightBridge is not an OTA competitor. We are a **revenue intelligence layer** that helps independent and chain hotels optimize their pricing strategy — a capability that ultimately drives more bookings toward all distribution channels, including Trip.com.

We would welcome the opportunity to discuss a data partnership arrangement, including reciprocal insights into pricing performance analytics that may benefit Trip.com's hotelier client relationships.

Please feel free to reach out at any time.

Sincerely,

**Dr. Tong Yin**  
Founder & CEO  
InsightBridge Global LLC  
📧 [your email]  
🌐 https://insightbridge.global  
📄 Company Profile: https://insightbridge.global/zh.html

---

## 三、备注

- 如 Trip.com 商务团队要求，可附上 21 天模拟测试的摘要报告（RevPAR 提升数据）
- 澳门本地 OTA：建议同步联系 **Agoda**（东南亚覆盖强）的 Connectivity Partner 项目，申请路径：https://connect.agoda.com/
- 美团酒店国际版暂无正式 API 开放计划，建议通过 Bright Data 补充该数据源
