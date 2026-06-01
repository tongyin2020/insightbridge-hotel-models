#!/bin/bash
# InsightBridge — 每日数据抓取（Agoda OTA + IR 活动日历）
# 每天 09:30 和 22:00 由 launchd 触发

SCRIPT_DIR="/Users/tongyin/Desktop/Hotel Model Rvisions/partnership_docs"
LOG="$SCRIPT_DIR/brightdata_results/daily_cron.log"
PYTHON=/usr/bin/python3

echo "" >> "$LOG"
echo "=============================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') 开始每日抓取" >> "$LOG"

# ── Part 1: Agoda OTA 价格抓取 ── 已停用 ────────────────────────────────────
# ⚠️  2026-05-30 停用原因：Bright Data Dataset API 按记录收费（$131/次），成本过高
# ✅  OTA 价格已改用 MakCorps API（$350/月固定费用，覆盖 Booking.com/Agoda/Trip.com）
# ✅  MakCorps 配置：simulation_test/data_fetchers/makcorps_fetcher.py
# echo "$(date '+%H:%M:%S') [1/2] 提交 Agoda 任务..." >> "$LOG"
# $PYTHON "$SCRIPT_DIR/02_BrightData_Macau_Scraper.py" submit >> "$LOG" 2>&1
# sleep 1200
# $PYTHON "$SCRIPT_DIR/02_BrightData_Macau_Scraper.py" fetch >> "$LOG" 2>&1
echo "$(date '+%H:%M:%S') [跳过] Agoda BD抓取已停用，改用MakCorps" >> "$LOG"

# ── Part 2: IR 活动日历抓取（Playwright 抓取官网，无额外费用）────────────────
echo "$(date '+%H:%M:%S') [1/1] 抓取 IR 活动日历..." >> "$LOG"
$PYTHON "$SCRIPT_DIR/04_IR_Event_Calendar.py" --force >> "$LOG" 2>&1
echo "$(date '+%H:%M:%S') IR 活动日历完成" >> "$LOG"

echo "$(date '+%H:%M:%S') 每日抓取全部完成" >> "$LOG"
