"""
InsightBridge — MakCorps 小时级价格采集器
makcorps_hourly_collector.py
================================================
专门采集 MakCorps OTA多平台价格，每小时运行一次（由 launchd 触发）。
仅使用 MakCorps API，不调用任何付费代理（Shifter/Bright Data）。

目的：
  1. 在 MakCorps 订阅到期前尽量多采集历史价格数据
  2. 为 compute_dynamic_base_price() 提供更密集的 OTA 参考价格
  3. 覆盖未来14天的入住日期，建立前向价格序列

数据存入：hotel_real_data.db → makcorps_snapshots 表

手动运行：
  python3 makcorps_hourly_collector.py          # 全量76家
  python3 makcorps_hourly_collector.py --test   # 只测前3家
  python3 makcorps_hourly_collector.py --quick  # 只抓今明两天（最快）

launchd 配置：
  /Library/LaunchAgents/com.insightbridge.makcorps_hourly.plist
  → 每整点运行一次（00:00~23:00）
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── 动态加载 HOTELS_76（从 hotel_data_collector 导入，含完整76家酒店）
try:
    _collector_dir = Path(__file__).parent
    if str(_collector_dir) not in sys.path:
        sys.path.insert(0, str(_collector_dir))
    from hotel_data_collector import HOTELS_76 as _HOTELS_76
    _HOTELS_76_LOADED = True
except Exception as _e:
    _HOTELS_76_LOADED = False
    _HOTELS_76 = []
    # 导入失败时将在下方回退到硬编码列表，日志在 log 初始化后打印

# ── 路径 & 配置 ────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
ENV_FILE  = Path("/Users/tongyin/Desktop/Hotel Model Rvisions/.env")
DB_PATH   = BASE_DIR / "hotel_real_data.db"
LOG_PATH  = BASE_DIR / "makcorps_hourly.log"

load_dotenv(ENV_FILE)

MAKCORPS_API_KEY = os.getenv("MAKCORPS_API_KEY", "").strip()
USD_TO_MOP = 8.06   # 汇率：1 USD ≈ 8.06 MOP

# ── 采集未来多少天的入住价格（MakCorps需要至少提前2天，建立前向价格序列）
CHECKIN_OFFSETS = list(range(2, 32))   # D+2 到 D+31，共30个日期

# ── 每家酒店API调用间隔（防止速率限制）
INTER_HOTEL_SLEEP = 2.0      # 秒
INTER_DATE_SLEEP  = 0.5      # 秒

# ── 日志 ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAKCORPS] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# 延迟输出 HOTELS_76 导入状态（log 初始化后）
if not _HOTELS_76_LOADED:
    log.warning(f"hotel_data_collector.HOTELS_76 导入失败，将使用硬编码回退列表（{len(_MAC_HOTELS_FALLBACK)}家）: {_e}")

# ══════════════════════════════════════════════════════════════════════════
#  76家酒店的 booking_com_id 索引
#  优先从 hotel_data_collector.HOTELS_76 动态加载，失败则回退硬编码列表
# ══════════════════════════════════════════════════════════════════════════

# ── 6家市场参考酒店（harness 原始配置，固定保留）
_MKT_HOTELS: list[dict] = [
    {"id": "MKT_5DX_STUD",  "cn": "新濠影汇(市场)",  "bcom": "8331360",  "star": 5, "verified": True},
    {"id": "MKT_5DX_SOFI",  "cn": "索菲特(市场)",    "bcom": "664580",   "star": 5, "verified": True},
    {"id": "MKT_3ST_ROYA",  "cn": "皇家(市场)",      "bcom": "306251",   "star": 3, "verified": True},
    {"id": "MKT_4ST_HARB",  "cn": "港景(市场)",      "bcom": "7810592",  "star": 4, "verified": True},
    {"id": "MKT_5DX_JWMA",  "cn": "JW万豪(市场)",    "bcom": "7807481",  "star": 5, "verified": True},
    {"id": "MKT_5ST_OKUR",  "cn": "大仓(市场)",      "bcom": "2091060",  "star": 5, "verified": True},
]

# ── 76家正式酒店的硬编码回退列表（仅在 import 失败时使用）
_MAC_HOTELS_FALLBACK: list[dict] = [
    {"id": "MAC_5DX_WYNN_001",  "cn": "永利澳门",        "bcom": "191433",   "star": 5, "verified": False},
    {"id": "MAC_5DX_WYNN_002",  "cn": "永利皇宫",        "bcom": "4082985",  "star": 5, "verified": False},
    {"id": "MAC_5DX_NUWA_003",  "cn": "颐居",            "bcom": "2075668",  "star": 5, "verified": False},
    {"id": "MAC_5DX_NOLM_004",  "cn": "新东方置地",      "bcom": "309099",   "star": 5, "verified": False},
    {"id": "MAC_5DX_GRAN_005",  "cn": "新葡京",          "bcom": "236934",   "star": 5, "verified": False},
    {"id": "MAC_5DX_MGMM_006",  "cn": "澳门美高梅",      "bcom": "308424",   "star": 5, "verified": False},
    {"id": "MAC_5DX_MGMC_007",  "cn": "美高梅天悦",      "bcom": "5327601",  "star": 5, "verified": False},
    {"id": "MAC_5DX_BANN_008",  "cn": "澳门四季",        "bcom": "432041",   "star": 5, "verified": False},
    {"id": "MAC_5DX_VENE_009",  "cn": "威尼斯人",        "bcom": "9156126",  "star": 5, "verified": False},
    {"id": "MAC_5DX_PARR_010",  "cn": "巴黎人",          "bcom": "5327600",  "star": 5, "verified": False},
    {"id": "MAC_5DX_SAND_011",  "cn": "金沙城中心",      "bcom": "357005",   "star": 5, "verified": False},
    {"id": "MAC_5DX_STUD_012",  "cn": "新濠影汇",        "bcom": "6648451",  "star": 5, "verified": False},
    {"id": "MAC_5ST_SHGR_013",  "cn": "上海滩豪庭",      "bcom": "295480",   "star": 5, "verified": False},
    {"id": "MAC_5ST_SOFI_014",  "cn": "索菲特澳门",      "bcom": "1056659",  "star": 5, "verified": False},
    {"id": "MAC_5ST_SHGD_017",  "cn": "澳门喜来登",      "bcom": "3753337",  "star": 5, "verified": False},
    {"id": "MAC_5ST_CROW_018",  "cn": "皇冠度假酒店",    "bcom": "1819613",  "star": 5, "verified": False},
    {"id": "MAC_5ST_OKUR_019",  "cn": "大仓酒店",        "bcom": "1819616",  "star": 5, "verified": False},
    {"id": "MAC_5ST_HARD_020",  "cn": "硬石酒店",        "bcom": "1819615",  "star": 5, "verified": False},
]

# ── 动态构建 MAC_ 列表（从 HOTELS_76 提取 booking_com_id + cn）
if _HOTELS_76_LOADED and _HOTELS_76:
    _mac_hotels_dynamic: list[dict] = [
        {
            "id":       h["id"],
            "cn":       h["cn"],
            "bcom":     h["booking_com_id"],
            "star":     h["star"],
            "verified": False,
        }
        for h in _HOTELS_76
        if h.get("booking_com_id")
    ]
else:
    _mac_hotels_dynamic = _MAC_HOTELS_FALLBACK

# ── 最终采集列表：6家市场参考 + 76家正式酒店（去重）
HOTEL_IDS: list[dict] = _MKT_HOTELS + _mac_hotels_dynamic


# ══════════════════════════════════════════════════════════════════════════
#  DB：makcorps_snapshots 表
# ══════════════════════════════════════════════════════════════════════════
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS makcorps_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_id        TEXT NOT NULL,          -- InsightBridge内部ID
            booking_com_id  TEXT NOT NULL,          -- Booking.com酒店ID
            star            INTEGER,
            snapshot_time   TEXT NOT NULL,          -- 采集时间 ISO
            checkin_date    TEXT NOT NULL,          -- 入住日期 YYYY-MM-DD
            checkout_date   TEXT NOT NULL,          -- 离店日期
            currency        TEXT DEFAULT 'MOP',
            vendor_prices   TEXT,                   -- JSON {"Booking.com": 1200, ...}
            min_ota_price   REAL,                   -- 当日最低OTA价（MOP）
            max_ota_price   REAL,                   -- 当日最高OTA价（MOP）
            avg_ota_price   REAL,                   -- 当日平均OTA价（MOP）
            price_count     INTEGER DEFAULT 0,      -- 有效价格数量
            api_ok          INTEGER DEFAULT 0,      -- API是否成功
            UNIQUE(hotel_id, checkin_date, snapshot_time)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mkc_hotel_checkin
        ON makcorps_snapshots(hotel_id, checkin_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mkc_time
        ON makcorps_snapshots(snapshot_time)
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════
#  MakCorps API 调用
# ══════════════════════════════════════════════════════════════════════════
def _parse_price(raw) -> float | None:
    """解析 MakCorps 价格字段（支持 "MOP 1200", "$148", "1200" 等格式）"""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    # 去掉货币符号
    s = re.sub(r"[^0-9.]", "", s)
    try:
        v = float(s)
        return v if v > 10 else None   # 低于10的价格是噪音
    except ValueError:
        return None


def fetch_hotel_prices(bcom_id: str, checkin: str, checkout: str) -> tuple[bool, dict]:
    """
    调用 MakCorps /hotel 端点，返回 (ok, vendor_prices_dict)
    vendor_prices_dict: {"Booking.com": 1200.0, "Agoda": 1180.0, ...}  (MOP)
    """
    if not MAKCORPS_API_KEY:
        return False, {}
    try:
        resp = requests.get(
            "https://api.makcorps.com/hotel",
            params={
                "api_key":  MAKCORPS_API_KEY,
                "hotelid":  bcom_id,
                "cur":      "USD",     # MOP格式解析困难，用USD后乘汇率
                "rooms":    1,
                "adults":   2,
                "checkin":  checkin,
                "checkout": checkout,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return False, {}

        data = resp.json()
        flat = [item for block in data.get("comparison", [])
                if isinstance(block, list)
                for item in block
                if isinstance(item, dict)]

        vendor_prices: dict[str, float] = {}
        for item in flat:
            for idx in range(1, 20):
                vendor = item.get(f"vendor{idx}", "")
                price  = item.get(f"price{idx}")
                if not vendor or price is None:
                    continue
                parsed = _parse_price(price)
                if parsed and parsed > 0:
                    # USD → MOP 转换
                    mop = round(parsed * USD_TO_MOP, 1)
                    vendor_key = str(vendor).strip()
                    if vendor_key not in vendor_prices or mop < vendor_prices[vendor_key]:
                        vendor_prices[vendor_key] = mop

        return bool(vendor_prices), vendor_prices

    except Exception as e:
        log.debug(f"MakCorps fetch error ({bcom_id}, {checkin}): {e}")
        return False, {}


def save_snapshot(conn: sqlite3.Connection, hotel: dict,
                  checkin: str, checkout: str,
                  vendor_prices: dict, snap_time: str, ok: bool) -> None:
    prices = list(vendor_prices.values())
    min_p = round(min(prices), 1) if prices else None
    max_p = round(max(prices), 1) if prices else None
    avg_p = round(sum(prices) / len(prices), 1) if prices else None
    try:
        conn.execute("""
            INSERT OR IGNORE INTO makcorps_snapshots
                (hotel_id, booking_com_id, star, snapshot_time,
                 checkin_date, checkout_date, currency,
                 vendor_prices, min_ota_price, max_ota_price, avg_ota_price,
                 price_count, api_ok)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            hotel["id"], hotel["bcom"], hotel["star"], snap_time,
            checkin, checkout, "MOP",
            json.dumps(vendor_prices, ensure_ascii=False),
            min_p, max_p, avg_p, len(prices), int(ok),
        ))
        conn.commit()
    except Exception as e:
        log.debug(f"DB save error: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  主采集循环
# ══════════════════════════════════════════════════════════════════════════
def run_collection(hotels: list[dict], quick: bool = False) -> None:
    if not MAKCORPS_API_KEY:
        log.error("MAKCORPS_API_KEY 未配置，退出")
        return

    conn = init_db()
    today = datetime.now().date()
    snap_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    offsets = [7, 14] if quick else CHECKIN_OFFSETS   # quick mode: D+7和D+14
    checkin_dates = [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in offsets]

    log.info(f"=== MakCorps小时采集开始 | {snap_time} | {len(hotels)}家酒店 × {len(checkin_dates)}天 ===")

    ok_count = fail_count = 0
    for i, hotel in enumerate(hotels, 1):
        log.info(f"[{i:02d}/{len(hotels)}] {hotel['cn']} (bcom={hotel['bcom']}, {hotel['star']}★)")

        for checkin in checkin_dates:
            checkout = (datetime.strptime(checkin, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

            ok, vendor_prices = fetch_hotel_prices(hotel["bcom"], checkin, checkout)
            save_snapshot(conn, hotel, checkin, checkout, vendor_prices, snap_time, ok)

            if ok:
                prices = list(vendor_prices.values())
                avg = round(sum(prices) / len(prices)) if prices else 0
                log.info(f"  ✅ {checkin}: {len(prices)}平台 avg=MOP {avg} min=MOP {min(prices):.0f}")
                ok_count += 1
            else:
                log.info(f"  ⚠️ {checkin}: 无数据")
                fail_count += 1

            time.sleep(INTER_DATE_SLEEP)

        time.sleep(INTER_HOTEL_SLEEP)

    total = ok_count + fail_count
    pct = f"{ok_count/total*100:.1f}%" if total else "N/A"
    log.info(f"=== 采集完成 | {ok_count}/{total} ({pct}) | DB: {DB_PATH} ===")
    conn.close()


# ══════════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="InsightBridge MakCorps小时级采集器")
    parser.add_argument("--test",  action="store_true", help="只测前3家酒店")
    parser.add_argument("--quick", action="store_true", help="只抓今明两天（快速测试）")
    args = parser.parse_args()

    hotels = HOTEL_IDS
    if args.test:
        hotels = HOTEL_IDS[:3]
        log.info("🧪 测试模式：前3家")

    run_collection(hotels, quick=args.quick)
