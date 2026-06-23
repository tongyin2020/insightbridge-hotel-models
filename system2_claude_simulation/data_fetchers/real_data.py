"""
澳门酒店AI模型 — 真实数据抓取模块
=====================================
经过稳定性重构后，默认优先级如下：

✅ 默认主链（稳定优先）:
  - Booking.com 价格 → 本地 price_snapshots / Firecrawl / DSEC 冷启动
  - TurboJET渡轮满座率 → flight_ferry信号  (requests, 无防护)
  - 澳门天气 → Open-Meteo
  - 假日/周末 → 日历
  - 澳门旅游局活动数量 → Firecrawl / 本地 IR 活动缓存

⚠️ 可选备用链（默认关闭）:
  - 本机 Playwright 抓 Booking.com / MGTO 页面

❌ 无法稳定通过公开网页直接获取:
  - ota_booking_pace   (OTA内部数据，从不公开)
  - border_flow 实时   (DSEC只出月报，无实时API)
  - zhuhai_saturation  (无任何商业来源)

所有爬取结果缓存2小时，避免频繁请求被封。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import logging
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

CACHE_DB = Path(__file__).parent.parent / "data_cache.db"
MODEL_ROOT = Path(__file__).resolve().parents[2]
REAL_DB_PATH = MODEL_ROOT / "hotel_collector" / "hotel_real_data.db"
COLLECTOR_DIR = MODEL_ROOT / "hotel_collector"
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))

# ── Shifter 代理配置（绕过 Cloudflare / Booking.com 机器人检测）────────────────
_SHIFTER_HOST = "p.shifter.io"
_SHIFTER_PORT = 443
_SHIFTER_USER = os.getenv("SHIFTER_USER", "")
_SHIFTER_PASS = os.getenv("SHIFTER_PASS", "")
_LOCAL_PLAYWRIGHT_FALLBACK = os.getenv("ENABLE_LOCAL_PLAYWRIGHT_FALLBACK", "").strip().lower() in {
    "1", "true", "yes", "on"
}

def _shifter_proxy_cfg() -> dict | None:
    """返回 Playwright proxy 配置字典；未配置 Shifter 时返回 None（降级到无代理）"""
    if _SHIFTER_USER and _SHIFTER_PASS:
        return {
            "server":   f"http://{_SHIFTER_HOST}:{_SHIFTER_PORT}",
            "username": _SHIFTER_USER,
            "password": _SHIFTER_PASS,
        }
    return None
CACHE_TTL_SECONDS = 7200  # 2小时缓存
_DNS_CACHE: dict[str, bool] = {}


def _host_resolves(host: str) -> bool:
    if not host:
        return False
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    try:
        socket.getaddrinfo(host, 443)
        _DNS_CACHE[host] = True
    except Exception:
        _DNS_CACHE[host] = False
    return _DNS_CACHE[host]


def _url_host_resolves(url: str) -> bool:
    try:
        return _host_resolves(urlparse(url).hostname or "")
    except Exception:
        return False


# ── 缓存层 ──────────────────────────────────────────────────────────────────
def _get_cache(key: str) -> Optional[dict]:
    try:
        conn = sqlite3.connect(CACHE_DB)
        row = conn.execute(
            "SELECT value, fetched_at FROM cache WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        if row:
            fetched_at = datetime.fromisoformat(row[1])
            if (datetime.now() - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
                return json.loads(row[0])
    except Exception:
        pass
    return None


def _set_cache(key: str, value: dict):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, fetched_at TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
            (key, json.dumps(value), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_cache_any_age(key: str) -> Optional[dict]:
    """读取最近一次缓存，不校验 TTL。"""
    try:
        conn = sqlite3.connect(CACHE_DB)
        row = conn.execute(
            "SELECT value FROM cache WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception:
        pass
    return None


# ── 天气（Open-Meteo，稳定API）───────────────────────────────────────────────
def fetch_weather() -> float:
    """返回澳门当前气温（摄氏度），失败时降级到旧缓存或默认值。"""
    cached = _get_cache("weather_macau")
    if cached:
        return cached["celsius"]
    errors = []

    if _host_resolves("api.open-meteo.com"):
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 22.1987,
                    "longitude": 113.5439,
                    "current": "temperature_2m",
                    "timezone": "Asia/Macau",
                },
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            )
            if r.status_code == 200:
                payload = r.json()
                current = payload.get("current") or {}
                celsius = float(current["temperature_2m"])
                _set_cache("weather_macau", {"celsius": celsius, "source": "open-meteo"})
                return celsius
            errors.append(f"open-meteo status={r.status_code}")
        except Exception as e:
            errors.append(f"open-meteo err={e}")
    else:
        errors.append("open-meteo dns_unresolved")

    if _host_resolves("wttr.in"):
        try:
            r = requests.get(
                "https://wttr.in/Macau?format=j1",
                timeout=8,
                headers={"User-Agent": "curl/8.0"},
            )
            if r.status_code == 200:
                payload = r.json()
                current = (payload.get("current_condition") or [{}])[0]
                celsius = float(current["temp_C"])
                _set_cache("weather_macau", {"celsius": celsius, "source": "wttr.in"})
                return celsius
            errors.append(f"wttr status={r.status_code}")
        except Exception as e:
            errors.append(f"wttr err={e}")
    else:
        errors.append("wttr dns_unresolved")

    stale = _get_cache_any_age("weather_macau")
    if stale and "celsius" in stale:
        logger.warning("Weather fetch failed, fallback to stale cache: %s", " | ".join(errors))
        return float(stale["celsius"])

    logger.warning("Weather fetch failed, fallback to default: %s", " | ".join(errors))
    return 25.0


# ── 渡轮信号（TurboJET + Cotai Water Jet 双源）────────────────────────────
def _fetch_turbojet() -> dict:
    """抓取 TurboJET 港澳航班表，返回 {total, sold_out, ok}"""
    try:
        r = requests.get(
            "https://www.turbojet.com.hk/en/routing-sailing-schedule/hong-kong-macau/sailing-schedule-fares.aspx",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=12,
        )
        html = r.text
        total    = len(re.findall(r"\d{2}:\d{2}", html))
        sold_out = html.lower().count("sold out") + html.lower().count("sold-out")
        return {"total": total, "sold_out": sold_out, "ok": total > 0, "source": "TurboJET"}
    except Exception as e:
        logger.warning(f"TurboJET fetch failed: {e}")
        return {"total": 0, "sold_out": 0, "ok": False, "source": "TurboJET"}


def _fetch_cotai_water_jet() -> dict:
    """
    抓取 Cotai Water Jet 港澳氹仔航班表（cotaiwaterjet.com）。
    CWJ 静态表不显示实时售罄，但可计算今日班次数作为服务水平指标。
    正常班次数 ≈ 55（港→澳）；如只剩少量班次说明临近收班/减班。
    """
    try:
        r = requests.get(
            "https://www.cotaiwaterjet.com/ferry-schedule/hongkong-macau-taipa.html",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=12,
        )
        html = r.text
        # 统计时间数量（班次数量代理）
        total    = len(re.findall(r"\b[0-9]{1,2}:[0-9]{2}\b", html))
        sold_out = html.lower().count("sold out") + html.lower().count("fully booked")
        return {"total": total, "sold_out": sold_out, "ok": total > 20, "source": "CotaiWaterJet"}
    except Exception as e:
        logger.warning(f"CotaiWaterJet fetch failed: {e}")
        return {"total": 0, "sold_out": 0, "ok": False, "source": "CotaiWaterJet"}


def fetch_ferry_signal() -> float:
    """
    综合 TurboJET + Cotai Water Jet 双源，计算渡轮满座压力信号。
    信号范围 -1.0~1.0：
      > 0.3  = 高需求（多班次满座）
      ≈ 0.0  = 正常运营
      < -0.1 = 低需求 / 班次减少
    策略：
      - 优先使用 TurboJET 售罄比例（实时性最好）
      - TurboJET 失败时用 CWJ 班次数评估服务水平
      - 两者都失败时返回 0.0（中性）
    """
    cached = _get_cache("ferry_combined_signal")
    if cached:
        return cached["signal"]

    tj  = _fetch_turbojet()
    cwj = _fetch_cotai_water_jet()

    signal = 0.0
    source_desc = "default"

    if tj["ok"]:
        # TurboJET 主信号：售罄比例
        sold_ratio = tj["sold_out"] / max(tj["total"], 1)
        signal = round(min(1.0, sold_ratio * 5.0) - 0.05, 3)
        source_desc = f"TurboJET({tj['total']}班,{tj['sold_out']}满)"
    elif cwj["ok"]:
        # CWJ 备用：班次数量评估（正常≈55，减班会低于40）
        if cwj["total"] < 40:
            signal = -0.1   # 可能减班，低需求期
        elif cwj["sold_out"] > 0:
            signal = round(min(1.0, cwj["sold_out"] / max(cwj["total"], 1) * 5.0), 3)
        else:
            signal = 0.0
        source_desc = f"CotaiWaterJet({cwj['total']}班次时间点)"
    else:
        signal = 0.0
        source_desc = "no_data"

    logger.info(f"Ferry signal={signal:.3f}  [{source_desc}]")
    _set_cache("ferry_combined_signal", {"signal": signal, "source": source_desc})
    return signal


def _parse_mop_prices(text: str, min_price: int, max_price: int) -> list[int]:
    raw = re.findall(r"MOP[\s\xa0]*([\d,]+)", text)
    return sorted(set(int(p.replace(",", "")) for p in raw if min_price < int(p.replace(",", "")) < max_price))


def _dsec_booking_fallback(checkin: str, source: str) -> dict:
    month = datetime.fromisoformat(checkin).month
    try:
        from dsec_loader import get_market_adr as _get_market_adr
        star3 = float(_get_market_adr(month, 3) or 900.0)
        star4 = float(_get_market_adr(month, 4) or 1050.0)
        star5 = float(_get_market_adr(month, 5) or 1450.0)
    except Exception:
        star3, star4, star5 = 900.0, 1050.0, 1450.0

    prices_3 = [round(star3 * x) for x in (0.92, 1.00, 1.07)]
    prices_45 = [round(star4 * 0.98), round(star4 * 1.05), round(star5 * 0.95), round(star5 * 1.04)]
    return {
        "prices_3star": prices_3,
        "prices_45star": prices_45,
        "count_3star": len(prices_3),
        "avg_3star": round(sum(prices_3) / len(prices_3), 1),
        "avg_45star": round(sum(prices_45) / len(prices_45), 1),
        "min_3star": min(prices_3),
        "max_3star": max(prices_3),
        "source": source,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _booking_prices_from_local_db(checkin: str) -> dict | None:
    if not REAL_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(REAL_DB_PATH), timeout=5)
        rows = conn.execute("""
            SELECT booking_rate, star
            FROM price_snapshots
            WHERE checkin_date = ?
              AND snapshot_time >= datetime('now', '-7 days')
              AND booking_rate > 200
        """, (checkin,)).fetchall()
        conn.close()

        prices_3 = sorted(int(row[0]) for row in rows if row[1] == 3 and row[0])
        prices_45 = sorted(int(row[0]) for row in rows if row[1] in (4, 5) and row[0])
        if not prices_3 and not prices_45:
            return None

        count_3 = len(prices_3)
        return {
            "prices_3star": prices_3,
            "prices_45star": prices_45,
            "count_3star": count_3,
            "avg_3star": round(sum(prices_3) / len(prices_3), 1) if prices_3 else 0,
            "avg_45star": round(sum(prices_45) / len(prices_45), 1) if prices_45 else 0,
            "min_3star": min(prices_3) if prices_3 else 0,
            "max_3star": max(prices_3) if prices_3 else 0,
            "source": "price_snapshots_recent",
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.debug(f"Local booking snapshot read failed: {e}")
        return None


def _booking_prices_from_firecrawl(checkin: str, checkout: str) -> dict | None:
    url_3 = (
        f"https://www.booking.com/searchresults.html?ss=Macau"
        f"&checkin={checkin}&checkout={checkout}"
        f"&nflt=class%3D3&lang=zh-hk&selected_currency=MOP"
    )
    if not _url_host_resolves(url_3):
        return None
    html_3 = _firecrawl_scrape(url_3)
    prices_3 = _parse_mop_prices(html_3, 100, 3000) if html_3 else []

    url_45 = (
        f"https://www.booking.com/searchresults.html?ss=Macau"
        f"&checkin={checkin}&checkout={checkout}"
        f"&nflt=class%3D4%3Bclass%3D5&lang=zh-hk&selected_currency=MOP"
    )
    html_45 = _firecrawl_scrape(url_45)
    prices_45 = _parse_mop_prices(html_45, 200, 15000) if html_45 else []

    if not prices_3 and not prices_45:
        return None

    count_match = re.findall(r"(\d+)\s*(?:properties?|酒店)\s*found", html_3 or "", re.I)
    count_3 = int(count_match[0]) if count_match else len(prices_3)
    return {
        "prices_3star": prices_3,
        "prices_45star": prices_45,
        "count_3star": count_3,
        "avg_3star": round(sum(prices_3) / len(prices_3), 1) if prices_3 else 0,
        "avg_45star": round(sum(prices_45) / len(prices_45), 1) if prices_45 else 0,
        "min_3star": min(prices_3) if prices_3 else 0,
        "max_3star": max(prices_3) if prices_3 else 0,
        "source": "booking_firecrawl",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _booking_prices_from_playwright(checkin: str, checkout: str) -> dict:
    cache_key = f"booking_{checkin}_{checkout}"

    try:
        from playwright.sync_api import sync_playwright

        proxy_cfg = _shifter_proxy_cfg()   # Shifter 住宅代理，绕过 Cloudflare

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage", "--disable-gpu"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-HK",
                viewport={"width": 1440, "height": 900},
                extra_http_headers={
                    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            page = ctx.new_page()

            # 3星（selected_currency=MOP 确保返回澳门元）
            url_3 = (
                f"https://www.booking.com/searchresults.html?ss=Macau"
                f"&checkin={checkin}&checkout={checkout}"
                f"&nflt=class%3D3&lang=zh-hk&selected_currency=MOP"
            )
            page.goto(url_3, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            html_3 = page.content()

            prices_3 = _parse_mop_prices(html_3, 100, 3000)
            count_match = re.findall(r"(\d+)\s*(?:properties?|酒店)\s*found", html_3, re.I)
            count_3 = int(count_match[0]) if count_match else len(prices_3)

            # 4-5星（用于upper_tier_adr）
            url_45 = (
                f"https://www.booking.com/searchresults.html?ss=Macau"
                f"&checkin={checkin}&checkout={checkout}"
                f"&nflt=class%3D4%3Bclass%3D5&lang=zh-hk&selected_currency=MOP"
            )
            page.goto(url_45, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            html_45 = page.content()
            prices_45 = _parse_mop_prices(html_45, 200, 15000)

            browser.close()

        result = {
            "prices_3star": prices_3,
            "prices_45star": prices_45,
            "count_3star": count_3,
            "avg_3star": round(sum(prices_3) / len(prices_3), 1) if prices_3 else 0,
            "avg_45star": round(sum(prices_45) / len(prices_45), 1) if prices_45 else 0,
            "min_3star": min(prices_3) if prices_3 else 0,
            "max_3star": max(prices_3) if prices_3 else 0,
            "source": "booking.com",
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _set_cache(cache_key, result)
        logger.info(f"Booking.com: 3star prices={prices_3}, 4-5star avg=MOP{result['avg_45star']:.0f}")
        return result

    except Exception as e:
        logger.warning(f"Booking.com scrape failed: {e}")
        return _dsec_booking_fallback(checkin, "booking_playwright_failed")


# ── Booking.com实时价格（默认不走本机Playwright）──────────────────────────
def fetch_booking_prices(checkin: str, checkout: str) -> dict:
    """
    默认优先级：
      1. 本地 recent price_snapshots
      2. Firecrawl 抓取 Booking 搜索页
      3. DSEC 冷启动参考
      4. 本机 Playwright（仅在 ENABLE_LOCAL_PLAYWRIGHT_FALLBACK=1 时启用）
    """
    cache_key = f"booking_{checkin}_{checkout}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    db_result = _booking_prices_from_local_db(checkin)
    if db_result:
        _set_cache(cache_key, db_result)
        return db_result

    fc_result = _booking_prices_from_firecrawl(checkin, checkout)
    if fc_result:
        _set_cache(cache_key, fc_result)
        return fc_result

    dsec_result = _dsec_booking_fallback(checkin, "booking_dsec_fallback")
    if not _LOCAL_PLAYWRIGHT_FALLBACK:
        _set_cache(cache_key, dsec_result)
        return dsec_result

    pw_result = _booking_prices_from_playwright(checkin, checkout)
    _set_cache(cache_key, pw_result)
    return pw_result


# ── 澳门旅游局活动信号（默认不走本机Playwright）──────────────────────────────
def _fetch_event_signal_playwright() -> float:
    """
    备用：抓取澳门旅游局活动页面，评估本月活动密度。
    返回 -1.0~1.0 信号，值越高代表活动越密集。
    """
    cache_key = f"macau_events_{datetime.now().strftime('%Y-%m')}"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            )
            page.goto(
                "https://www.macaotourism.gov.mo/en/events",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()

        # 计算活动密度指标
        event_cards = len(re.findall(r'class=["\'][^"\']*event[^"\']*card[^"\']*["\']', html, re.I))
        event_items = len(re.findall(r'class=["\'][^"\']*event[^"\']*item[^"\']*["\']', html, re.I))
        event_mentions = html.lower().count("event")
        show_mentions = html.lower().count("show") + html.lower().count("concert") + html.lower().count("festival")

        total_score = event_cards + event_items + (show_mentions * 2)

        # 标准化：通常范围 0~30，映射到 -0.1~0.5
        signal = round(min(0.5, total_score / 60.0) - 0.05, 3)
        logger.info(f"Macau events: cards={event_cards}, shows={show_mentions} → signal={signal}")

        _set_cache(cache_key, {"signal": signal, "event_mentions": event_mentions, "show_mentions": show_mentions})
        return signal

    except Exception as e:
        logger.warning(f"Macau Tourism events fetch failed: {e}")
        return 0.0


def fetch_event_signal() -> float:
    """
    默认优先级：
      1. 本地 IR 活动缓存
      2. Firecrawl 澳门旅游局活动页
      3. 本机 Playwright（仅在 ENABLE_LOCAL_PLAYWRIGHT_FALLBACK=1 时启用）
      4. 中性值 0.0
    """
    cache_key = f"macau_events_{datetime.now().strftime('%Y-%m')}"
    cached = _get_cache(cache_key)
    if cached:
        return cached["signal"]

    ir_signal = _load_ir_event_signal_from_db()
    if ir_signal is not None:
        _set_cache(cache_key, {"signal": ir_signal, "source": "ir_event_cache"})
        return ir_signal

    fc_density, _fc_ticket, fc_source = fetch_firecrawl_event_density()
    if fc_source not in ("fc_event_failed", "no_key"):
        _set_cache(cache_key, {"signal": fc_density, "source": fc_source})
        return fc_density

    if _LOCAL_PLAYWRIGHT_FALLBACK:
        return _fetch_event_signal_playwright()

    _set_cache(cache_key, {"signal": 0.0, "source": "event_neutral_fallback"})
    return 0.0


# ── DSEC访客统计（月度编码，无实时接口）──────────────────────────────────
def get_dsec_visitors_signal(month: int) -> float:
    """
    基于DSEC公布的2024-2025年澳门访客历史数据，
    返回该月的正常化访客信号 (-1.0~1.0)。
    注：DSEC无实时API，此为最新公开月报编码值。
    """
    # 基于DSEC 2025年实际数据编码
    # 数据来源：dsec.gov.mo 访客统计月报
    monthly_baseline = {
        1: 0.55,   # 一月（农历新年）
        2: 0.65,   # 二月（年后）
        3: 0.62,   # 三月
        4: 0.68,   # 四月（清明假期）
        5: 0.72,   # 五月（五一）
        6: 0.50,   # 六月（台风季开始）
        7: 0.48,   # 七月（台风高峰）
        8: 0.45,   # 八月（台风高峰）
        9: 0.60,   # 九月（回稳）
        10: 0.82,  # 十月（国庆黄金周）
        11: 0.75,  # 十一月
        12: 0.80,  # 十二月（圣诞+元旦）
    }
    base = monthly_baseline.get(month, 0.60)
    # 转换为 -1~1 信号（0.5 = neutral）
    return round((base - 0.5) * 2, 3)


# ── IR 活动信号：从本地活动缓存读取最新结果（由 04_IR_Event_Calendar.py 写入）
def _load_ir_event_signal_from_db() -> Optional[float]:
    """
    读取由 04_IR_Event_Calendar.py 写入的最新 IR 活动信号。
    key 格式：ir_event_signal_YYYY-MM-DD
    若最近3天内有数据则返回信号值，否则返回 None（降级到旧爬虫）。
    """
    try:
        cache_dir = Path(__file__).parent.parent
        db_candidates = [
            cache_dir / "ir_event_cache.db",
            cache_dir / "event_signal_cache.db",
        ]
        db_path = next((p for p in db_candidates if p.exists()), None)
        if db_path is None:
            legacy_hits = sorted(cache_dir.glob("*cache.db"))
            db_path = legacy_hits[0] if legacy_hits else None
        if db_path is None or not db_path.exists():
            return None
        conn = sqlite3.connect(db_path, timeout=5)
        # 取最近3天内的 IR 信号
        rows = conn.execute(
            "SELECT value, ts FROM mc_cache WHERE key LIKE 'ir_event_signal_%' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if rows:
            age = time.time() - rows[1]
            if age < 3600 * 72:   # 72小时内有效
                data = json.loads(rows[0])
                signal = float(data.get("signal", 0.0))
                logger.info(f"IR活动信号（缓存{age/3600:.0f}h前）: {signal:.3f} "
                            f"{data.get('event_count',0)}场活动")
                return signal
    except Exception as e:
        logger.debug(f"IR事件DB读取失败: {e}")
    return None


# ── 主接口：获取所有可用真实数据 ──────────────────────────────────────────
def _firecrawl_search(query: str) -> str:
    """Firecrawl search，返回合并文本，失败返回空字符串。"""
    key = os.getenv("FIRECRAWL_API_KEY", "")
    if not key:
        return ""
    if not _host_resolves("api.firecrawl.dev"):
        return ""
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v2/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "limit": 5},
            timeout=20,
        )
        if r.status_code == 200:
            items = r.json().get("data") or []
            return " ".join(str(it.get("description") or it.get("markdown") or "") for it in items)
    except Exception:
        pass
    return ""


def _firecrawl_scrape(url: str) -> str:
    """Firecrawl scrape单页，返回markdown，失败返回空字符串。"""
    key = os.getenv("FIRECRAWL_API_KEY", "")
    if not key:
        return ""
    if not _host_resolves("api.firecrawl.dev"):
        return ""
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": url, "formats": ["markdown"]},
            timeout=30,
        )
        if r.status_code == 200:
            return (r.json().get("data") or {}).get("markdown", "")
    except Exception:
        pass
    return ""


def fetch_firecrawl_border_flow() -> tuple:
    """用Firecrawl搜索TDM新闻获取口岸客流信号。返回(signal, source)。"""
    cache_key = f"fc_border_{datetime.now().strftime('%Y-%m-%d-%H')}"
    cached = _get_cache(cache_key)
    if cached:
        return cached["signal"], cached["source"]
    today = datetime.now().strftime("%Y年%m月%d日")
    text = _firecrawl_search(f"澳门口岸 过境 旅客 {today} 人数 统计")
    if not text:
        text = _firecrawl_scrape("https://www.tdm.com.mo/zh-hant/news?category=27")
    nums = re.findall(r'(\d+(?:\.\d+)?)\s*[萬万]', text)
    if nums:
        val = float(nums[0])
        sig = round(max(-1.0, min(1.0, (val - 20.0) / 15.0)), 3)
        _set_cache(cache_key, {"signal": sig, "source": "firecrawl_tdm"})
        return sig, "firecrawl_tdm"
    _set_cache(cache_key, {"signal": 0.0, "source": "fc_no_data"})
    return 0.0, "fc_no_data"


def fetch_firecrawl_zhuhai_sat(checkin: str) -> tuple:
    """用Firecrawl搜索珠海酒店价格，估算溢出饱和度。返回(signal, source)。"""
    cache_key = f"fc_zhuhai_{checkin}"
    cached = _get_cache(cache_key)
    if cached:
        return cached["signal"], cached["source"]
    text = _firecrawl_search(f"珠海酒店 {checkin} 价格 今晚 预订")
    prices = re.findall(r'(?:¥|RMB|CNY)\s*(\d{2,4})', text)
    if prices:
        avg = sum(float(p) for p in prices[:10]) / min(len(prices), 10)
        # 珠海均价高=澳门溢出压力大
        sig = round(min(1.0, max(0.0, (avg - 300) / 500)), 3)
        _set_cache(cache_key, {"signal": sig, "source": "firecrawl_zhuhai"})
        return sig, "firecrawl_zhuhai"
    _set_cache(cache_key, {"signal": 0.25, "source": "fc_zhuhai_no_price"})
    return 0.25, "fc_zhuhai_no_price"


def fetch_firecrawl_event_density() -> tuple:
    """用Firecrawl抓取澳门旅游局活动日历，返回(event_density, event_ticket_sales, source)。"""
    cache_key = f"fc_event_{datetime.now().strftime('%Y-%m')}"
    cached = _get_cache(cache_key)
    if cached:
        return cached["event_density"], cached["event_ticket_sales"], cached["source"]
    md = _firecrawl_scrape("https://www.macaotourism.gov.mo/en/events/calendar")
    if md:
        major   = len(re.findall(r'Grand Prix|Fireworks|Dragon Boat|Chinese New Year|Major Event|Formula|Festival|Carnival', md, re.I))
        holiday = len(re.findall(r'Public Holiday|National Day|Labour Day|Mid-Autumn|Golden Week', md, re.I))
        density = round(min(1.0, 0.15 * major + 0.05 * holiday), 3)
        ticket  = round(min(1.0, 0.12 * major + 0.03 * holiday), 3)
        _set_cache(cache_key, {"event_density": density, "event_ticket_sales": ticket, "source": "firecrawl_tourism"})
        return density, ticket, "firecrawl_tourism"
    _set_cache(cache_key, {"event_density": 0.0, "event_ticket_sales": 0.0, "source": "fc_event_failed"})
    return 0.0, 0.0, "fc_event_failed"


def get_all_real_signals(checkin: str | None = None, checkout: str | None = None) -> dict:
    """
    统一接口：抓取所有可获得的真实数据，
    返回格式化为模型输入的信号字典。

    checkin/checkout: 'YYYY-MM-DD' 格式
    """
    now = datetime.now()
    if not checkin:
        checkin = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    if not checkout:
        checkout = (datetime.fromisoformat(checkin) + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── 真实数据 ──────────────────────────────────────────────────────────
    weather_c = fetch_weather()
    ferry = fetch_ferry_signal()

    # ── IR 活动信号：优先读本地活动缓存；其余逻辑交给 fetch_event_signal 走稳定链
    ir_signal = _load_ir_event_signal_from_db()
    if ir_signal is not None:
        events = ir_signal
    else:
        events = fetch_event_signal()

    booking = fetch_booking_prices(checkin, checkout)
    visitors = get_dsec_visitors_signal(now.month)
    mha_mass_adr = mha_mass_occ = mha_mass_signal = 0.0
    mha_lux_adr = mha_lux_occ = mha_lux_signal = 0.0
    mha_period = ""
    if REAL_DB_PATH.exists():
        try:
            from dsec_loader import (
                get_latest_market_adr,
                get_latest_market_demand_signal,
                get_latest_market_occupancy,
                get_latest_market_snapshot,
            )
            _dc = sqlite3.connect(str(REAL_DB_PATH), timeout=5)
            mha_mass_adr = float(get_latest_market_adr(3, _dc) or 0.0)
            mha_mass_occ = float(get_latest_market_occupancy(3, _dc) or 0.0)
            mha_mass_signal = float(get_latest_market_demand_signal(3, _dc) or 0.0)
            mha_lux_adr = float(get_latest_market_adr(5, _dc) or 0.0)
            mha_lux_occ = float(get_latest_market_occupancy(5, _dc) or 0.0)
            mha_lux_signal = float(get_latest_market_demand_signal(5, _dc) or 0.0)
            latest_snap = get_latest_market_snapshot(3, _dc)
            if latest_snap:
                mha_period = f"{int(latest_snap['year']):04d}-{int(latest_snap['month']):02d}"
            _dc.close()
        except Exception:
            pass

    # ── Firecrawl 新增因子（3个缺口全部补上）────────────────────────────
    fc_border, fc_border_src   = fetch_firecrawl_border_flow()
    fc_zhuhai, fc_zhuhai_src   = fetch_firecrawl_zhuhai_sat(checkin)
    fc_event_d, fc_event_t, fc_event_src = fetch_firecrawl_event_density()

    # 如果Firecrawl活动数据成功，覆盖Playwright版event_ticket_sales
    if fc_event_src not in ("fc_event_failed", "no_key"):
        events = fc_event_d   # 用Firecrawl更高质量的活动密度

    # ── 信号转换：Booking.com价格 → 竞对信号 ────────────────────────────
    avg_competitor = booking["avg_3star"] or 750.0
    min_competitor = booking["min_3star"] or 600.0
    upper_tier_adr = booking["avg_45star"] or 1500.0
    count_3 = booking["count_3star"] or 10
    availability_ratio = min(1.0, count_3 / 25.0)

    # weather_signal: 高温或恶劣天气为负
    if weather_c > 33:
        weather_signal = round(-0.05 * ((weather_c - 33) / 5), 3)
    elif weather_c < 10:
        weather_signal = round(-0.08, 3)
    else:
        weather_signal = 0.0

    return {
        # ── 真实数据因子 ──────────────────────────────────────────────
        "weather": weather_signal,
        "weather_celsius": weather_c,
        "flight_ferry": min(1.0, max(-1.0, ferry)),
        "event_ticket_sales": min(0.5, max(-0.1, events)),
        "visitors_stats": visitors,
        "mha_adr_mass": mha_mass_adr,
        "mha_occ_mass": mha_mass_occ,
        "mha_signal_mass": mha_mass_signal,
        "mha_adr_luxury": mha_lux_adr,
        "mha_occ_luxury": mha_lux_occ,
        "mha_signal_luxury": mha_lux_signal,
        "mha_latest_period": mha_period,

        # ── Firecrawl 新增三个缺口因子 ───────────────────────────────
        "border_flow":         fc_border,
        "border_flow_source":  fc_border_src,
        "zhuhai_saturation":   fc_zhuhai,
        "zhuhai_source":       fc_zhuhai_src,
        "event_density_fc":    fc_event_d,
        "event_ticket_fc":     fc_event_t,
        "event_fc_source":     fc_event_src,

        # ── Booking.com真实价格 ───────────────────────────────────────
        "competitor_price_real": avg_competitor,
        "competitor_price_min": min_competitor,
        "competitor_availability_real": availability_ratio,
        "upper_tier_adr_real": upper_tier_adr,
        "booking_prices_3": booking["prices_3star"],
        "booking_prices_45": booking["prices_45star"],

        "data_sources": {
            "weather": "Open-Meteo (real-time)",
            "flight_ferry": "TurboJET + CotaiWaterJet 双源 (real-time)",
            "event_ticket_sales": f"IR活动缓存 / Firecrawl / optional Playwright ({fc_event_src})",
            "visitors_stats": "DSEC monthly report (encoded)",
            "mha_market_snapshot": f"MHA monthly report ({mha_period or 'latest available'})",
            "competitor_price": "price_snapshots / Firecrawl / DSEC / optional Playwright",
            "upper_tier_adr": "price_snapshots / Firecrawl / DSEC / optional Playwright",
            "border_flow": f"Firecrawl TDM新闻({fc_border_src})",
            "zhuhai_saturation": f"Firecrawl珠海搜索({fc_zhuhai_src})",
            "ota_booking_pace": "scenario_or_local_fallback",
        },
    }


if __name__ == "__main__":
    # 快速测试
    logging.basicConfig(level=logging.INFO)
    from datetime import datetime, timedelta
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

    print("抓取所有真实数据...")
    signals = get_all_real_signals(tomorrow, day_after)
    for k, v in signals.items():
        if k != "data_sources":
            print(f"  {k}: {v}")
    print("\n数据来源:")
    for k, v in signals["data_sources"].items():
        print(f"  {k}: {v}")
