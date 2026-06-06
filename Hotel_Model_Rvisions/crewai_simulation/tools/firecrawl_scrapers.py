"""
firecrawl_scrapers.py — 用 Firecrawl v4 尝试抓取 Playwright 无法获取的数据
===========================================================================
重点攻克三个"缺口"因子：
  border_flow     (权重0.18) — 口岸过境客流
  zhuhai_sat      (权重0.12) — 珠海酒店饱和度（溢出效应）
  ota_booking_pace(权重0.12) — OTA平台实时预订节奏

架构升级（2026-06）：
  优先从 Firecrawl Monitor webhook 缓存读取（每2小时自动更新，节省积分）
  Monitor 端点：https://intelligence.insightbridge.global/api/monitor/latest/{key}
  直接API调用作为备用（Monitor缓存超2小时或无数据时）
"""

from __future__ import annotations
import os, re, json, time, sqlite3, requests as _requests
from datetime import datetime, timedelta
from pathlib import Path

# ── Monitor 缓存端点 ──────────────────────────────────────────────────────────
MONITOR_BASE = "https://intelligence.insightbridge.global/api/monitor/latest"
MONITOR_STALE_SECS = 7200   # 2小时内认为新鲜

def _read_monitor_cache(key: str) -> dict | None:
    """
    从 Firecrawl Monitor webhook 缓存读取最新信号。
    返回 None 表示无数据或数据过期，调用方应降级到直接API。
    """
    try:
        r = _requests.get(f"{MONITOR_BASE}/{key}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data.get("signal") is not None and not data.get("stale", True):
                return data
    except Exception:
        pass
    return None

try:
    from firecrawl import Firecrawl   # firecrawl-py v4+
    FIRECRAWL_OK = True
except ImportError:
    FIRECRAWL_OK = False
    import logging as _log
    _log.getLogger(__name__).warning("firecrawl-py 未安装，所有FC信号将降级为模拟值")

CACHE_DB  = Path(__file__).parent.parent / "crewai_cache.db"
CACHE_TTL = 3600  # 1小时内复用缓存，节省免费额度


def _cache_get(key: str):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
        row  = conn.execute("SELECT value, ts FROM cache WHERE key=?", (key,)).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_set(key: str, value):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
        conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)",
                     (key, json.dumps(value), time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_app():
    if not FIRECRAWL_OK:
        return None
    key = os.getenv("FIRECRAWL_API_KEY", "")
    if not key or "your_key" in key:
        return None
    return Firecrawl(api_key=key)


def _search(app, query: str, limit: int = 5) -> list[dict]:
    """统一的search调用，兼容v4 API返回格式"""
    try:
        r = app.search(query, limit=limit)
        # v4 返回 SearchData 对象，.web 是结果列表
        items = getattr(r, "web", None) or getattr(r, "data", None) or []
        results = []
        for item in items:
            if hasattr(item, "__dict__"):
                results.append(item.__dict__)
            elif isinstance(item, dict):
                results.append(item)
        return results
    except Exception:
        return []


def _scrape(app, url: str, wait_ms: int = 3000) -> str:
    """统一的scrape调用，返回markdown文本"""
    try:
        r = app.scrape(url, formats=["markdown"])
        if hasattr(r, "markdown"):
            return r.markdown or ""
        if isinstance(r, dict):
            return r.get("markdown", "")
        return str(r)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
#  因子1：border_flow — 口岸过境量代理 (权重0.18)
# ══════════════════════════════════════════════════════════════════════
def fetch_border_flow_signal() -> dict:
    # 1. 优先读 Monitor webhook 缓存（每2小时自动更新，免费）
    monitor_data = _read_monitor_cache("border_flow")
    if monitor_data:
        return {"signal": monitor_data["signal"], "source": monitor_data["source"],
                "raw": monitor_data.get("raw",""), "method": "monitor_cache"}

    # 2. 本地缓存
    cached = _cache_get("border_flow")
    if cached:
        return cached

    app = _get_app()
    result = {"signal": 0.0, "source": "simulated", "raw": "", "method": "fallback"}
    if not app:
        return result

    # 策略A：搜索今日口岸客流报道
    try:
        today = datetime.now().strftime("%Y年%m月%d日")
        items = _search(app, f"澳门口岸 过境 旅客 {today} 人数 统计", limit=5)
        texts = []
        for item in items:
            txt = (item.get("description") or item.get("markdown") or
                   item.get("content") or item.get("snippet") or "")
            texts.append(str(txt)[:500])
        combined = " ".join(texts)

        nums = re.findall(r'(\d+(?:\.\d+)?)\s*万', combined)
        if nums:
            val = float(nums[0])
            signal = max(-1.0, min(1.0, (val - 20.0) / 15.0))
            result = {"signal": round(signal, 3), "source": "firecrawl_search",
                      "raw": f"{val}万人次", "method": "text_extract"}
            _cache_set("border_flow", result)
            return result

        # 有搜索结果但没有数字 → 用文本情绪判断
        neg_kw = ["下降", "减少", "冷清", "低迷"]
        pos_kw = ["上升", "增加", "爆满", "高峰", "创新高"]
        pos_n = sum(1 for k in pos_kw if k in combined)
        neg_n = sum(1 for k in neg_kw if k in combined)
        if pos_n > neg_n:
            result = {"signal": 0.3, "source": "firecrawl_sentiment",
                      "raw": f"正面关键词:{pos_n}", "method": "sentiment"}
        elif neg_n > pos_n:
            result = {"signal": -0.2, "source": "firecrawl_sentiment",
                      "raw": f"负面关键词:{neg_n}", "method": "sentiment"}
        elif items:
            result = {"signal": 0.0, "source": "firecrawl_search_no_number",
                      "raw": f"有{len(items)}条结果但无数字", "method": "no_extract"}
    except Exception as e:
        result["raw"] = f"err:{e}"

    # 策略B：抓取TDM/澳广视新闻（澳门官方媒体，经常报道客流）
    if result["source"] == "simulated":
        try:
            md = _scrape(app, "https://www.tdm.com.mo/zh-hant/news?category=27")
            nums = re.findall(r'(\d+(?:\.\d+)?)\s*万\s*(?:人次|旅客)', md)
            if nums:
                val = float(nums[0])
                signal = max(-1.0, min(1.0, (val - 20.0) / 15.0))
                result = {"signal": round(signal, 3), "source": "tdm_news_fc",
                          "raw": f"{val}万人次", "method": "news_scrape"}
        except Exception as e:
            result["raw"] += f"|tdm_err:{e}"

    _cache_set("border_flow", result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  因子2：zhuhai_saturation — 珠海酒店饱和度 (权重0.12)
# ══════════════════════════════════════════════════════════════════════
def fetch_zhuhai_saturation_signal(checkin: str, checkout: str) -> dict:
    cache_key = f"zhuhai_{checkin}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    app = _get_app()
    result = {"signal": 0.35, "avg_price_rmb": 0.0, "source": "simulated"}
    if not app:
        return result

    # 策略A：搜索珠海酒店价格
    try:
        items = _search(app, f"珠海酒店 {checkin} 预订 价格 今晚", limit=8)
        prices_rmb = []
        for item in items:
            txt = str(item.get("description") or item.get("content") or "")
            found = re.findall(r'[¥￥]\s*(\d{3,4})', txt)
            found += re.findall(r'(\d{3,4})\s*元/晚', txt)
            found += re.findall(r'(\d{3,4})\s*元起', txt)
            prices_rmb.extend([int(p) for p in found if 150 < int(p) < 2500])

        if prices_rmb:
            avg = sum(prices_rmb) / len(prices_rmb)
            # 珠海3星平日约280-350元，节假日500-700元
            # 归一化：300=低饱和(0.2)，600=高饱和(0.85)
            signal = 0.20 + (avg - 300) / 375 * 0.65
            result = {"signal": round(max(0.0, min(1.0, signal)), 3),
                      "avg_price_rmb": round(avg, 1),
                      "source": "firecrawl_search",
                      "sample_count": len(prices_rmb)}
            _cache_set(cache_key, result)
            return result
    except Exception as e:
        result["error"] = str(e)

    # 策略B：直接搜索"珠海酒店价格高"类新闻（高价=高饱和）
    try:
        items2 = _search(app, f"珠海 {checkin} 酒店 爆满 满房", limit=4)
        if len(items2) >= 2:
            result = {"signal": 0.7, "avg_price_rmb": 0.0,
                      "source": "firecrawl_saturation_news",
                      "sample_count": len(items2)}
        elif items2:
            result = {"signal": 0.5, "avg_price_rmb": 0.0,
                      "source": "firecrawl_saturation_news",
                      "sample_count": len(items2)}
    except Exception:
        pass

    _cache_set(cache_key, result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  因子3：ota_booking_pace — OTA预订节奏 (权重0.12)
# ══════════════════════════════════════════════════════════════════════
def fetch_ota_booking_pace_signal(checkin: str) -> dict:
    cache_key = f"ota_pace_{checkin}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    app = _get_app()
    result = {"signal": 0.35, "urgency_count": 0, "source": "simulated"}
    if not app:
        return result

    # 策略A：抓取Booking.com澳门搜索页面，统计"仅剩X间"紧迫标签
    try:
        tomorrow = (datetime.strptime(checkin, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        url = (f"https://www.booking.com/searchresults.html"
               f"?ss=Macau&checkin={checkin}&checkout={tomorrow}"
               f"&nflt=class%3D2%3Bclass%3D3%3Bclass%3D4%3Bclass%3D5")
        md = _scrape(app, url, wait_ms=5000)

        urgency_patterns = [
            r'only\s+\d+\s+(?:room|left)',
            r'仅剩\s*\d+\s*间',
            r'high\s+demand',
            r'sold\s+out',
            r'last\s+(?:room|chance)',
            r'\d+\s+people\s+looking',
        ]
        urgency_count = sum(len(re.findall(p, md, re.IGNORECASE)) for p in urgency_patterns)
        sold_out = len(re.findall(r'sold.?out|unavailable', md, re.IGNORECASE))
        total_hotels = max(1, len(re.findall(r'MOP\s*[\d,]+', md)))

        pace = min(1.0, urgency_count * 0.05 + sold_out / total_hotels * 0.5)
        if md and len(md) > 10000:  # 确认抓到了完整Booking.com搜索结果页（通常>100KB）
            result = {"signal": round(max(0.0, pace), 3),
                      "urgency_count": urgency_count,
                      "sold_out": sold_out,
                      "source": "booking_fc"}
            _cache_set(cache_key, result)
            return result
    except Exception as e:
        result["error"] = str(e)

    # 策略B：搜索澳门酒店预订紧张新闻
    try:
        items = _search(app, f"澳门酒店 {checkin} 爆满 抢房 预订紧张", limit=4)
        signal = min(1.0, 0.25 + len(items) * 0.12)
        if items:
            result = {"signal": round(signal, 3),
                      "news_count": len(items),
                      "source": "firecrawl_pace_news"}
    except Exception:
        pass

    _cache_set(cache_key, result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  补充：Agoda澳门价格（Playwright版未能获取）
# ══════════════════════════════════════════════════════════════════════
def fetch_agoda_prices(checkin: str, checkout: str) -> dict:
    cache_key = f"agoda_{checkin}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    app = _get_app()
    result = {"prices_23star": [], "avg_23star": 0.0, "source": "unavailable"}
    if not app:
        return result

    try:
        # 搜索Agoda澳门酒店价格信息
        items = _search(app, f"Agoda 澳门酒店 {checkin} 价格 MOP", limit=6)
        prices = []
        for item in items:
            txt = str(item.get("description") or item.get("content") or "")
            for match in re.finditer(r'(?:MOP|HK\$|HKD)\s*[,.]?\s*(\d{3,4})', txt):
                p = int(match.group(1).replace(",", ""))
                if 200 < p < 2000:
                    prices.append(p)

        if prices:
            result = {"prices_23star": prices[:10],
                      "avg_23star": round(sum(prices[:10]) / min(len(prices), 10), 1),
                      "source": "agoda_fc_search",
                      "count": len(prices)}
    except Exception as e:
        result["error"] = str(e)

    _cache_set(cache_key, result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  因子5：event_density — 澳门旅游局活动密度 (System1已有，System3补充)
# ══════════════════════════════════════════════════════════════════════
def fetch_event_density_signal() -> dict:
    """从澳门旅游局活动日历抓取活动密度信号。"""
    cached = _cache_get("event_density")
    if cached:
        return cached

    app = _get_app()
    result = {"event_density": 0.0, "event_ticket_sales": 0.0,
              "source": "simulated", "raw": ""}
    if not app:
        return result

    try:
        md = _scrape(app, "https://www.macaotourism.gov.mo/en/events/calendar")
        if md:
            major   = len(re.findall(
                r'Grand Prix|Fireworks|Dragon Boat|Chinese New Year|Major Event|Formula|Festival|Carnival',
                md, re.I))
            holiday = len(re.findall(
                r'Public Holiday|National Day|Labour Day|Mid-Autumn|Golden Week', md, re.I))
            density = round(min(1.0, 0.15 * major + 0.05 * holiday), 3)
            ticket  = round(min(1.0, 0.12 * major + 0.03 * holiday), 3)
            result  = {"event_density": density, "event_ticket_sales": ticket,
                       "source": "firecrawl_tourism", "raw": f"major:{major} holiday:{holiday}"}
    except Exception as e:
        result["raw"] = f"err:{e}"

    _cache_set("event_density", result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  汇总入口 — 5个因子全部覆盖
# ══════════════════════════════════════════════════════════════════════
def get_all_firecrawl_signals(checkin: str, checkout: str) -> dict:
    """整合所有Firecrawl抓取结果，返回可直接用于模型的信号字典。"""
    print(f"  [FC] 抓取增强信号 {checkin}...", end=" ", flush=True)

    border = fetch_border_flow_signal()
    zhuhai = fetch_zhuhai_saturation_signal(checkin, checkout)
    pace   = fetch_ota_booking_pace_signal(checkin)
    agoda  = fetch_agoda_prices(checkin, checkout)
    event  = fetch_event_density_signal()

    # 统计真实抓取成功数（5因子）
    real = [s for s in [border["source"], zhuhai["source"],
                         pace["source"], agoda["source"], event["source"]]
            if s not in ("simulated", "unavailable", "fallback")]
    print(f"{len(real)}/5因子真实 | "
          f"border={border['signal']}({border['source'][:10]}) "
          f"zhuhai={zhuhai['signal']}({zhuhai['source'][:10]}) "
          f"pace={pace['signal']}({pace['source'][:10]}) "
          f"event={event['event_density']}({event['source'][:10]})", flush=True)

    return {
        "border_flow_fc":       border["signal"],
        "border_flow_source":   border["source"],
        "zhuhai_saturation_fc": zhuhai["signal"],
        "zhuhai_source":        zhuhai["source"],
        "ota_booking_pace_fc":  pace["signal"],
        "ota_pace_source":      pace["source"],
        "agoda_prices_23":      agoda.get("prices_23star", []),
        "agoda_avg_23":         agoda.get("avg_23star", 0.0),
        "agoda_source":         agoda["source"],
        "event_density_fc":     event["event_density"],
        "event_ticket_sales_fc":event["event_ticket_sales"],
        "event_source":         event["source"],
        "_border_detail":       border,
        "_zhuhai_detail":       zhuhai,
        "_pace_detail":         pace,
        "_event_detail":        event,
    }
