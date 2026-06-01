"""
makcorps_fetcher.py — MakCorps Hotel Price API 集成
====================================================
将 ota_booking_pace 因子从场景模拟升级为真实OTA价格数据。

工作原理：
  每4小时查询10家澳门代表性酒店的多平台实时价格，计算：
  ① 价格溢价率   — 实时均价 vs 历史基准，越高=需求越旺
  ② OTA可用平台数 — 越少=售罄越多=预订节奏越快
  ③ 平台间价格离散度 — 越大=动态调价越激烈=需求越高
  三指标加权合成 ota_booking_pace (0~1)

API配额管理：
  - 4小时SQLite缓存（避免重复调用）
  - 每次采样6家酒店 = 6次API调用
  - 21天 × 6次/4h = ~756次总调用
  - 免费试用：15次（仅供测试）
  - 推荐购买：Basic Plan (≥1000次/月)
"""

from __future__ import annotations
import os, re, json, time, sqlite3, requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# 加载主 .env（位于 Hotel Model Rvisions/.env）
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# ── 配置 ──────────────────────────────────────────────────────────────
API_KEY   = os.getenv("MAKCORPS_API_KEY", "")
BASE_URL  = "https://api.makcorps.com"
CACHE_DB  = Path(__file__).parent.parent / "makcorps_cache.db"
CACHE_TTL = 1 * 3600   # 1小时缓存，充分利用剩余配额（24次/天）

# ── 澳门代表性酒店（已通过Mapping API验证ID）─────────────────────────
# 覆盖2~5星，跨半岛/氹仔/路凼城/路环四区
MACAU_HOTELS = {
    # ── 5星 · Cotai路凼城 ──
    "studio_city":   {"id": "8331360",  "star": 5, "district": "COTAI",  "baseline_usd": 120},
    "jw_marriott":   {"id": "7807481",  "star": 5, "district": "COTAI",  "baseline_usd": 180},
    "w_macau":       {"id": "25444582", "star": 5, "district": "COTAI",  "baseline_usd": 200},
    # ── 5星 · 半岛/NAPE ──
    "sofitel":       {"id": "664580",   "star": 5, "district": "NAPE",   "baseline_usd": 160},
    "mandarin":      {"id": "1734004",  "star": 5, "district": "NAPE",   "baseline_usd": 220},
    # ── 4星 · 中档 ──
    "grand_coloane": {"id": "306252",   "star": 4, "district": "COL",    "baseline_usd": 110},
    "okura":         {"id": "2091060",  "star": 4, "district": "COTAI",  "baseline_usd": 140},
    # ── 3星 · 经济 ──
    "royal_macau":   {"id": "306251",   "star": 3, "district": "INNER",  "baseline_usd": 80},
    "harbourview":   {"id": "7810592",  "star": 3, "district": "INNER",  "baseline_usd": 70},
    "roosevelt":     {"id": "12275881", "star": 3, "district": "NAPE",   "baseline_usd": 90},
}

# 每次只采样6家（覆盖各星级），节省API配额
SAMPLE_KEYS = ["studio_city", "sofitel", "royal_macau", "harbourview", "jw_marriott", "okura"]


# ── SQLite缓存 ────────────────────────────────────────────────────────
def _cache_get(key: str):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS mc_cache "
                     "(key TEXT PRIMARY KEY, value TEXT, ts REAL)")
        row = conn.execute("SELECT value, ts FROM mc_cache WHERE key=?", (key,)).fetchone()
        conn.close()
        if row and (time.time() - row[1]) < CACHE_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_set(key: str, value):
    try:
        conn = sqlite3.connect(CACHE_DB)
        conn.execute("CREATE TABLE IF NOT EXISTS mc_cache "
                     "(key TEXT PRIMARY KEY, value TEXT, ts REAL)")
        conn.execute("INSERT OR REPLACE INTO mc_cache VALUES (?,?,?)",
                     (key, json.dumps(value), time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── 单酒店价格查询 ────────────────────────────────────────────────────
def _fetch_hotel_prices(hotel_id: str, checkin: str, checkout: str) -> dict:
    """
    返回示例:
    {
      "vendors": ["Booking.com", "Agoda.com", "Trip.com"],
      "prices_usd": [116.0, 104.0, 108.0],
      "sold_out_vendors": 2,      # 平台总数最多19 - 有价格3 = 售罄16... 实际用可用数
      "available_count": 3,
      "min_usd": 104.0,
      "max_usd": 116.0,
      "avg_usd": 109.3,
      "ok": True
    }
    """
    if not API_KEY or API_KEY == "your_makcorps_key_here":
        return {"ok": False, "reason": "no_key"}

    try:
        resp = requests.get(
            f"{BASE_URL}/hotel",
            params={"hotelid": hotel_id, "cur": "USD", "rooms": 1, "adults": 2,
                    "checkin": checkin, "checkout": checkout, "api_key": API_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            return {"ok": False, "reason": f"http_{resp.status_code}"}

        data = resp.json()
        comparison = data.get("comparison", [[]])[0]   # 第一个房型
        vendors, prices = [], []
        for item in comparison:
            for i in range(1, 20):          # MakCorps返回最多19个平台 (vendor1~vendor19)
                v = item.get(f"vendor{i}", "")
                p = item.get(f"price{i}", "")
                if v and p:
                    p_clean = re.sub(r"[^\d.]", "", str(p))
                    if p_clean:
                        vendors.append(v)
                        prices.append(float(p_clean))
        if not prices:
            return {"ok": False, "reason": "no_prices"}
        return {
            "ok": True,
            "vendors": vendors,
            "prices_usd": prices,
            "available_count": len(prices),
            "min_usd": min(prices),
            "max_usd": max(prices),
            "avg_usd": round(sum(prices) / len(prices), 2),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── 澳门OTA预订节奏主函数 ────────────────────────────────────────────
def fetch_ota_booking_pace_makcorps(checkin: str, checkout: str) -> dict:
    """
    返回 ota_booking_pace 信号字典，直接替换场景模拟值。

    返回格式:
    {
      "signal": 0.72,           # 0~1，可直接用于MARE模型
      "source": "makcorps",     # 或 "makcorps_cached" / "no_key" / "quota_exceeded"
      "hotels_sampled": 6,
      "hotels_ok": 4,
      "avg_price_usd": 142.5,
      "avg_premium_pct": 18.3,  # 对比基准溢价%
      "avg_vendors": 3.2,       # 平均可用OTA平台数
      "detail": {...}
    }
    """
    cache_key = f"makcorps_pace_{checkin}_{datetime.now().hour:02d}"  # 按小时分key，确保每小时刷新
    cached = _cache_get(cache_key)
    if cached:
        cached["source"] = "makcorps_cached"
        return cached

    if not API_KEY or API_KEY == "your_makcorps_key_here":
        return {"signal": 0.35, "source": "no_key", "hotels_ok": 0}

    results = []
    for key in SAMPLE_KEYS:
        hotel = MACAU_HOTELS[key]
        r = _fetch_hotel_prices(hotel["id"], checkin, checkout)
        if r["ok"]:
            baseline = hotel["baseline_usd"]
            avg_p    = r["avg_usd"]
            premium  = (avg_p - baseline) / max(baseline, 1)   # 溢价率
            avail    = r["available_count"]                      # 可用平台数（高=库存充足）
            # 可用平台数：通常4~7家 = 正常；1~2家 = 紧张；0 = 售罄
            avail_score = max(0.0, 1.0 - (avail - 1) / 6.0)    # 越少平台 → 越高分
            results.append({
                "key": key,
                "star": hotel["star"],
                "avg_usd": avg_p,
                "premium": premium,
                "avail_count": avail,
                "avail_score": avail_score,
            })

    if not results:
        return {"signal": 0.35, "source": "makcorps_failed", "hotels_ok": 0}

    # ── 综合得分 ─────────────────────────────────────────────────────
    # 权重：价格溢价(50%) + 平台稀缺(35%) + 星级调权(15%不独立，在price体现)
    avg_premium    = sum(r["premium"]     for r in results) / len(results)
    avg_avail_sc   = sum(r["avail_score"] for r in results) / len(results)
    avg_price_usd  = sum(r["avg_usd"]     for r in results) / len(results)
    avg_vendors    = sum(r["avail_count"] for r in results) / len(results)

    # 溢价归一：-20%=0.0, 0%=0.35, +50%=0.85, +100%=1.0
    premium_score = max(0.0, min(1.0, 0.35 + avg_premium * 0.9))
    pace_signal   = round(0.50 * premium_score + 0.50 * avg_avail_sc, 3)
    pace_signal   = max(0.0, min(1.0, pace_signal))

    out = {
        "signal":          pace_signal,
        "source":          "makcorps",
        "hotels_sampled":  len(SAMPLE_KEYS),
        "hotels_ok":       len(results),
        "avg_price_usd":   round(avg_price_usd, 2),
        "avg_premium_pct": round(avg_premium * 100, 1),
        "avg_vendors":     round(avg_vendors, 1),
        "detail":          results,
    }
    _cache_set(cache_key, out)
    return out


# ── 账户配额检查 ──────────────────────────────────────────────────────
def check_quota() -> dict:
    """检查MakCorps账户剩余配额，返回 {limit, used, remaining, validity_days}"""
    if not API_KEY or API_KEY == "your_makcorps_key_here":
        return {"error": "no_key"}
    try:
        r = requests.get(f"{BASE_URL}/account", params={"api_key": API_KEY}, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── 独立测试 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    checkin  = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    checkout = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  MakCorps OTA预订节奏测试  {checkin}")
    print(f"{'='*60}")

    quota = check_quota()
    print(f"\n账户状态: {quota}")

    result = fetch_ota_booking_pace_makcorps(checkin, checkout)
    print(f"\nOTA预订节奏信号: {result['signal']:.3f}  (来源: {result['source']})")
    print(f"采样酒店: {result.get('hotels_ok',0)}/{result.get('hotels_sampled',0)} 家成功")
    print(f"平均价格: USD {result.get('avg_price_usd',0):.1f}  "
          f"溢价: {result.get('avg_premium_pct',0):+.1f}%  "
          f"平均可用平台: {result.get('avg_vendors',0):.1f} 个")

    if "detail" in result:
        print("\n各酒店明细:")
        for d in result["detail"]:
            print(f"  {d['key']:15s} {d['star']}★  "
                  f"USD{d['avg_usd']:6.0f}  "
                  f"溢价{d['premium']:+.0%}  "
                  f"可用{d['avail_count']}个平台  "
                  f"稀缺评分{d['avail_score']:.2f}")
    print()
