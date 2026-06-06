"""
ota_signal_tool.py — OTA 预订前兆信号聚合工具
===============================================
整合多个 OTA 数据源，提取"游客预订前兆"信号，
提前 7-21 天感知需求变化，提高短期定价精度。

数据源优先级（自动降级）：
  1. Amadeus for Developers  → 酒店搜索量 + 航班需求（免费注册）
  2. MakCorps                → 竞对实时价格（已有Key）
  3. Google Trends (pytrends)→ 搜索趋势（免费，限速时跳过）
  4. 内置规则信号            → 节假日、汇率、季节（兜底）

环境变量（.env）：
  AMADEUS_CLIENT_ID     → https://developers.amadeus.com 免费注册
  AMADEUS_CLIENT_SECRET → 同上
  MAKCORPS_API_KEY      → 已有（69fad6dbbd5b9206d5e9129b）【付费计划，已开放 /city 分页 + /hotel + /roomtype】

输出：综合 OTA 信号分数（0-100）+ 各维度明细
"""

from __future__ import annotations
import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    _CREWAI_OK = True
except ImportError:
    _CREWAI_OK = False


# ══════════════════════════════════════════════════════════════════════
#  数据源 1: Amadeus — 酒店搜索量 & 航班需求（免费 test tier）
# ══════════════════════════════════════════════════════════════════════

_AMADEUS_TOKEN_CACHE: dict = {"token": None, "expires_at": 0}

def _get_amadeus_token() -> str | None:
    """获取 Amadeus OAuth2 access token（缓存30分钟）"""
    client_id     = os.getenv("AMADEUS_CLIENT_ID", "")
    client_secret = os.getenv("AMADEUS_CLIENT_SECRET", "")
    if not client_id or "your_" in client_id:
        return None

    now = time.time()
    if _AMADEUS_TOKEN_CACHE["token"] and now < _AMADEUS_TOKEN_CACHE["expires_at"]:
        return _AMADEUS_TOKEN_CACHE["token"]

    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        "https://test.api.amadeus.com/v1/security/oauth2/token",
        data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            token = resp.get("access_token")
            expires_in = resp.get("expires_in", 1800)
            _AMADEUS_TOKEN_CACHE["token"] = token
            _AMADEUS_TOKEN_CACHE["expires_at"] = now + expires_in - 60
            return token
    except Exception:
        return None


def _amadeus_hotel_search(city_code: str, checkin: str, checkout: str) -> dict | None:
    """
    查询 Amadeus 酒店可用率 & 价格区间
    city_code: IATA 城市代码，例如 MFM（澳门）、HKG（香港）、DXB（迪拜）
    """
    token = _get_amadeus_token()
    if not token:
        return None
    params = urllib.parse.urlencode({
        "cityCode":  city_code,
        "checkInDate": checkin,
        "checkOutDate": checkout,
        "adults": 2,
        "roomQuantity": 1,
        "currency": "USD",
        "bestRateOnly": "true",
        "page[limit]": 10,
    })
    url = f"https://test.api.amadeus.com/v3/shopping/hotel-offers?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _amadeus_flight_demand(origin: str, destination: str) -> dict | None:
    """
    查询 Amadeus 航班搜索量（Flight Inspiration Search）
    作为"旅游意图"的领先指标
    """
    token = _get_amadeus_token()
    if not token:
        return None
    params = urllib.parse.urlencode({
        "origin":   origin,
        "maxPrice": 500,
    })
    url = f"https://test.api.amadeus.com/v1/shopping/flight-destinations?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _parse_amadeus_signals(city_code: str, checkin: str, checkout: str) -> dict:
    """解析 Amadeus 数据，生成结构化信号"""
    result = {"source": "amadeus", "available": False, "signals": {}}

    hotel_data = _amadeus_hotel_search(city_code, checkin, checkout)
    if not hotel_data or "data" not in hotel_data:
        return result

    offers = hotel_data["data"]
    if not offers:
        return result

    prices = []
    for offer in offers:
        try:
            price = float(offer["offers"][0]["price"]["total"])
            prices.append(price)
        except (KeyError, IndexError, ValueError):
            continue

    if prices:
        avg_price   = sum(prices) / len(prices)
        min_price   = min(prices)
        max_price   = max(prices)
        avail_count = len(prices)

        # 可用率信号（可用酒店数越少→需求越高）
        avail_signal = max(0, 100 - avail_count * 8)

        # 价格压力信号（平均价格越高→市场需求越旺）
        price_signal = min(100, (avg_price / 200) * 50)  # 标准化到0-100

        result.update({
            "available": True,
            "signals": {
                "hotels_available":  avail_count,
                "avg_price_usd":     round(avg_price, 2),
                "min_price_usd":     round(min_price, 2),
                "max_price_usd":     round(max_price, 2),
                "availability_score": round(avail_signal, 1),  # 高=可用少=需求旺
                "price_pressure":    round(price_signal, 1),   # 高=价格贵=需求旺
                "composite_score":   round((avail_signal + price_signal) / 2, 1),
            }
        })

    return result


# ══════════════════════════════════════════════════════════════════════
#  数据源 2: MakCorps — 竞对实时价格（付费计划）
# ══════════════════════════════════════════════════════════════════════

# 代表性酒店 ID（含预算/中档/高档三层，通过 /mapping API 预取）
# 付费计划使用 /hotel 端点，每家酒店返回 15+ 供应商价格
HOTEL_IDS_BY_CITY: dict = {
    "macau":      ["664580",   "306252",   "1734004",  "7807481",  "25444582"],
    # Sofitel Ponte16, Grand Coloane, Mandarin Oriental, JW Marriott, W Macau
    "macao":      ["664580",   "306252",   "1734004",  "7807481",  "25444582"],
    "hong kong":  [],   # 动态通过 /mapping 获取
    "singapore":  [],
    "dubai":      [],
    "bangkok":    [],
    "tokyo":      [],
    "london":     [],
    "paris":      [],
    "sydney":     [],
    "new york":   [],
    "bali":       [],
}

# 城市名 → Makcorps GEO city_id（用于 /mapping 动态补充）
MAKCORPS_CITY_GEO_IDS: dict = {
    "macau":      "664891",
    "macao":      "664891",
    "hong kong":  "294217",
    "singapore":  "294265",
    "bangkok":    "293916",
    "tokyo":      "298184",
    "paris":      "187147",
    "sydney":     "255060",
    "bali":       "294226",
    "new york":   "60763",
    "london":     "186338",
}

_MAPPING_CACHE: dict = {}   # city → [hotel_id, ...]


def _makcorps_hotel_ids(city: str, api_key: str, max_hotels: int = 5) -> list[str]:
    """
    通过 /mapping 端点动态获取城市代表性酒店 ID（结果缓存）
    """
    city_lower = city.lower()
    # 优先使用预设 hotel IDs
    if HOTEL_IDS_BY_CITY.get(city_lower):
        return HOTEL_IDS_BY_CITY[city_lower][:max_hotels]
    # 缓存检查
    if city_lower in _MAPPING_CACHE:
        return _MAPPING_CACHE[city_lower][:max_hotels]
    # 调用 /mapping
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        q = city_lower.replace(" ", "+")
        url = f"https://api.makcorps.com/mapping?name={q}&api_key={api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        hotel_ids = [str(x["value"]) for x in data if x.get("type") == "HOTEL"][:max_hotels]
        _MAPPING_CACHE[city_lower] = hotel_ids
        return hotel_ids
    except Exception:
        return []


def _parse_makcorps_comparison(comparison: list) -> list[float]:
    """
    解析 /hotel 端点 comparison 响应，提取所有供应商价格
    格式: comparison[0] = [{vendor1, price1, Totalprice1, tax1}, {vendor2, ...}, ...]
    """
    prices = []
    vendor_list = comparison[0] if (comparison and isinstance(comparison[0], list)) else comparison
    for item in vendor_list:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            # 匹配 priceN（但排除 TotalpriceN）
            if k.startswith("price") and not k.startswith("Totalprice"):
                try:
                    clean = str(v).replace("$", "").replace(",", "").strip()
                    prices.append(float(clean))
                except (ValueError, AttributeError):
                    pass
    return prices


def _makcorps_prices(city: str, checkin: str, checkout: str) -> dict | None:
    """
    查询 MakCorps 竞对价格（付费计划 — /hotel 端点，15+ 供应商）
    流程：
      1. 获取城市代表性酒店 ID（预设 / /mapping 动态获取）
      2. 为每家酒店调用 /hotel 端点
      3. 聚合所有供应商价格，输出价格分布统计
    """
    api_key = os.getenv("MAKCORPS_API_KEY", "")
    if not api_key or "your_" in api_key:
        return None

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    hotel_ids = _makcorps_hotel_ids(city, api_key)
    if not hotel_ids:
        return None

    all_prices: list[float] = []
    hotels_queried = 0
    hotel_benchmarks = []

    for hid in hotel_ids:
        url = (f"https://api.makcorps.com/hotel"
               f"?hotelid={hid}&rooms=1&adults=2"
               f"&checkin={checkin}&checkout={checkout}"
               f"&cur=USD&api_key={api_key}")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read())

            # 配额检测
            if isinstance(data, dict):
                msg = data.get("message", "")
                if "Limit" in msg or "quota" in msg.lower():
                    break

            comp = data.get("comparison", [])
            hotel_prices = _parse_makcorps_comparison(comp)
            if hotel_prices:
                all_prices.extend(hotel_prices)
                hotels_queried += 1
                hotel_benchmarks.append({
                    "hotel_id":    hid,
                    "min_price":   round(min(hotel_prices), 2),
                    "avg_price":   round(sum(hotel_prices)/len(hotel_prices), 2),
                    "vendors_cnt": len(hotel_prices),
                })
        except Exception:
            continue

    if not all_prices:
        return None

    ps = sorted(all_prices)
    n  = len(ps)
    return {
        "city":            city,
        "checkin":         checkin,
        "checkout":        checkout,
        "hotels_queried":  hotels_queried,
        "vendors_sampled": n,
        "min_price_usd":   round(ps[0],    2),
        "p25_price_usd":   round(ps[n//4], 2),
        "median_price_usd":round(ps[n//2], 2),
        "p75_price_usd":   round(ps[3*n//4],2),
        "avg_price_usd":   round(sum(ps)/n, 2),
        "max_price_usd":   round(ps[-1],   2),
        "hotel_benchmarks": hotel_benchmarks,
        "currency":        "USD",
        "endpoint":        "/hotel",
        "plan":            "paid",
    }


# ══════════════════════════════════════════════════════════════════════
#  数据源 3: Google Trends — 搜索量趋势（带限速保护）
# ══════════════════════════════════════════════════════════════════════

def _google_trends_signal(location: str) -> dict | None:
    """查询 Google Trends 搜索热度（免费，可能被限速）"""
    try:
        from pytrends.request import TrendReq
        keywords = [f"{location} hotel", f"{location} travel"]
        pt = TrendReq(hl="en-US", tz=480, timeout=(10, 30), retries=1, backoff_factor=0.5)
        pt.build_payload(keywords[:1], timeframe="today 1-m", geo="")
        df = pt.interest_over_time()
        if df.empty:
            return None
        recent_avg = float(df[keywords[0]].tail(7).mean())
        prev_avg   = float(df[keywords[0]].iloc[-14:-7].mean())
        trend      = round(((recent_avg - prev_avg) / max(prev_avg, 1)) * 100, 1)
        return {
            "keyword":    keywords[0],
            "recent_avg": round(recent_avg, 1),
            "trend_pct":  trend,  # 正=搜索量上升（预订意图增强）
        }
    except Exception:
        return None  # 限速时静默跳过


# ══════════════════════════════════════════════════════════════════════
#  数据源 4: 内置规则信号（兜底，无需 API）
# ══════════════════════════════════════════════════════════════════════

CITY_IATA = {
    "macau": "MFM", "macao": "MFM",
    "hong kong": "HKG", "dubai": "DXB",
    "singapore": "SIN", "bangkok": "BKK",
    "tokyo": "TYO", "london": "LON",
}

MACRO_SIGNALS = {
    # 月份 → 澳门基础需求系数
    1: 0.85, 2: 0.90, 3: 0.65, 4: 0.70,
    5: 0.65, 6: 0.60, 7: 0.55, 8: 0.55,
    9: 0.70, 10: 0.85, 11: 0.90, 12: 0.95,
}

def _builtin_signal(location: str, days_ahead: int) -> dict:
    """无 API 的内置信号：季节 + 节假日 + 提前预订窗口"""
    target = datetime.now() + timedelta(days=days_ahead // 2)
    month  = target.month
    dow    = target.weekday()

    seasonal = MACRO_SIGNALS.get(month, 0.7)
    weekend_boost  = 0.15 if dow >= 4 else 0.0

    # 提前预订信号：7-14天前搜索最多（订单转化高峰）
    booking_window = 1.0 if 7 <= days_ahead <= 14 else (0.8 if days_ahead <= 7 else 0.6)

    score = min(100, (seasonal + weekend_boost) * booking_window * 100)
    return {
        "seasonal_factor":  round(seasonal, 2),
        "weekend_boost":    weekend_boost > 0,
        "booking_window":   f"{days_ahead}天前（{'黄金窗口' if 7 <= days_ahead <= 14 else '标准'}）",
        "builtin_score":    round(score, 1),
    }


# ══════════════════════════════════════════════════════════════════════
#  综合信号聚合器
# ══════════════════════════════════════════════════════════════════════

def _aggregate_signals(location: str, days_ahead: int) -> dict:
    """聚合所有数据源，生成综合 OTA 信号分数"""
    checkin  = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    checkout = (datetime.now() + timedelta(days=days_ahead + 1)).strftime("%Y-%m-%d")
    city_code = CITY_IATA.get(location.lower(), "MFM")

    signals  = {}
    scores   = []
    sources  = []

    # 1. Amadeus
    amadeus = _parse_amadeus_signals(city_code, checkin, checkout)
    if amadeus["available"]:
        s = amadeus["signals"]
        signals["amadeus"] = s
        scores.append(s["composite_score"])
        sources.append("Amadeus✓")
    else:
        sources.append("Amadeus✗")

    # 2. MakCorps
    mc = _makcorps_prices(location, checkin, checkout)
    if mc and "error" not in str(mc):
        signals["makcorps"] = mc
        sources.append("MakCorps✓")
    else:
        quota_msg = "（额度耗尽，需升级）" if mc and "quota" in str(mc) else "（未配置）"
        sources.append(f"MakCorps✗{quota_msg}")

    # 3. Google Trends
    gt = _google_trends_signal(location)
    if gt:
        signals["google_trends"] = gt
        trend_score = min(100, 50 + gt["trend_pct"])
        scores.append(trend_score)
        sources.append("GoogleTrends✓")
    else:
        sources.append("GoogleTrends✗(限速)")

    # 4. 内置信号（始终可用）
    builtin = _builtin_signal(location, days_ahead)
    signals["builtin"] = builtin
    scores.append(builtin["builtin_score"])
    sources.append("内置规则✓")

    # 综合分数
    composite = round(sum(scores) / len(scores), 1) if scores else 50.0
    level = "🔴 HIGH" if composite >= 65 else ("🟡 MEDIUM" if composite >= 35 else "🟢 LOW")

    return {
        "composite_score": composite,
        "demand_level":    level,
        "checkin_date":    checkin,
        "sources_used":    sources,
        "signals":         signals,
    }


# ══════════════════════════════════════════════════════════════════════
#  CrewAI Tool
# ══════════════════════════════════════════════════════════════════════

if _CREWAI_OK:

    class OTASignalInput(BaseModel):
        location: str = Field(
            default="Macau",
            description="目标城市（英文），例如 Macau、Hong Kong、Dubai、Singapore"
        )
        days_ahead: int = Field(
            default=7,
            description="预测提前天数（预订前兆窗口），推荐 7-21 天"
        )
        output_detail: str = Field(
            default="summary",
            description="输出详细度：'summary'=摘要（默认）, 'full'=全部信号明细"
        )

    class OTASignalTool(BaseTool):
        """
        OTA 预订前兆信号聚合工具。
        整合 Amadeus / MakCorps / Google Trends / 内置规则，
        生成综合需求意图分数，比当日数据提前 7-21 天捕捉信号。
        """
        name: str = "ota_signal"
        description: str = (
            "聚合多个 OTA 平台的预订前兆信号，预测未来酒店需求。\n"
            "数据源：Amadeus（酒店搜索量/价格）+ MakCorps（竞对价格）"
            "+ Google Trends（搜索意图）+ 季节规则\n"
            "适合场景：\n"
            "- 距入住还有 7-21 天，判断需求是否会激增\n"
            "- 当前搜索量是否在上升（预订意图增强信号）\n"
            "- 竞对价格是否已开始上调（市场先行信号）\n"
            "- 为 MARE 定价模型提供'早鸟'需求信号"
        )
        args_schema: type[OTASignalInput] = OTASignalInput

        def _run(self, location: str = "Macau",
                 days_ahead: int = 7,
                 output_detail: str = "summary") -> str:

            data = _aggregate_signals(location, days_ahead)
            score   = data["composite_score"]
            level   = data["demand_level"]
            checkin = data["checkin_date"]
            sources = data["sources_used"]
            signals = data["signals"]

            # 定价建议
            if score >= 70:   price_action = "⬆ 建议提价 15-25%（需求信号强烈）"
            elif score >= 55: price_action = "⬆ 建议提价 5-15%（需求偏强）"
            elif score >= 35: price_action = "➡ 维持现价（需求平稳）"
            else:             price_action = "⬇ 考虑优惠促销（需求偏弱）"

            lines = [
                f"📡 OTA 预订前兆信号 — {location} ({checkin} 入住，{days_ahead}天前信号)",
                f"   综合分数: {score}/100  {level}",
                f"   定价建议: {price_action}",
                f"   数据来源: {' | '.join(sources)}",
                "",
            ]

            # Amadeus 详情
            if "amadeus" in signals:
                s = signals["amadeus"]
                lines += [
                    "🏨 Amadeus 酒店市场信号：",
                    f"   可用酒店数: {s['hotels_available']} 家  "
                    f"均价: ${s['avg_price_usd']}  "
                    f"区间: ${s['min_price_usd']}-${s['max_price_usd']}",
                    f"   可用压力分: {s['availability_score']}/100  "
                    f"价格压力分: {s['price_pressure']}/100",
                    "",
                ]

            # Google Trends 详情
            if "google_trends" in signals:
                gt = signals["google_trends"]
                trend_arrow = "📈" if gt["trend_pct"] > 5 else ("📉" if gt["trend_pct"] < -5 else "➡")
                lines += [
                    f"🔍 Google Trends：'{gt['keyword']}'",
                    f"   近7天均值: {gt['recent_avg']}  "
                    f"趋势: {trend_arrow} {gt['trend_pct']:+.1f}%",
                    "",
                ]

            # MakCorps
            if "makcorps" in signals and output_detail == "full":
                lines += ["💰 MakCorps 竞对数据：已获取（详见 full 模式）", ""]

            # 内置信号
            builtin = signals.get("builtin", {})
            lines += [
                f"📅 季节信号：旺季系数 {builtin.get('seasonal_factor', 0.7):.0%}  "
                f"{'周末加成 ✓' if builtin.get('weekend_boost') else '工作日'}  "
                f"预订窗口: {builtin.get('booking_window', '')}",
            ]

            # 配置提示
            if not os.getenv("AMADEUS_CLIENT_ID") or "your_" in os.getenv("AMADEUS_CLIENT_ID", "x"):
                lines += [
                    "",
                    "💡 提示：注册 Amadeus 免费账号可解锁酒店搜索量信号：",
                    "   https://developers.amadeus.com → Self-Service → 免费注册",
                ]

            return "\n".join(lines)

else:
    class OTASignalTool:  # type: ignore
        def __init__(self):
            print("  [OTASignal] CrewAI 未安装")
