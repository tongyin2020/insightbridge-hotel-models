"""
InsightBridge — Bright Data 澳门酒店价格抓取器 v3
=================================================
两阶段设计：
  Phase 1 (submit): 提交抓取任务，保存 snapshot_id
  Phase 2 (fetch):  等任务完成后取回数据，写入 makcorps_cache.db

运行方式：
  python3 02_BrightData_Macau_Scraper.py submit   # 提交任务
  python3 02_BrightData_Macau_Scraper.py fetch    # 取回结果（提交后 10-30 分钟）
  python3 02_BrightData_Macau_Scraper.py          # 自动：提交 → 等 20 分钟 → 取回

数据集（来自 Bright Data Scrapers Library）：
  Trip.com  Hotels — discover by location    gd_mb7q8vuuej1nso8j2
  Agoda Properties Listings with Pricing     gd_mdyifvvo181pcrpew6

费用：$1.50 / 1,000 条记录
"""

from __future__ import annotations
import json, os, time, sys, requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

# ── 配置 ───────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

TOKEN      = os.getenv("BRIGHT_DATA_TOKEN", "")
SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"
RESULTS_DIR  = Path(__file__).parent / "brightdata_results"
SNAPSHOT_FILE = RESULTS_DIR / "pending_snapshots.json"
RESULTS_DIR.mkdir(exist_ok=True)

DATASET = {
    # Agoda 含价格搜索 — 按地点+日期搜索（返回含实时价格的酒店列表）
    # 覆盖国际/东南亚客群视角（36家澳门酒店，USD定价）
    "agoda_pricing": {
        "id":    "gd_mdyifvvo181pcrpew6",
        "params": "&type=discover_new&discover_by=search_input",
    },
    # Trip.com 搜索含价格（gd_mb7vkuzsszex4nit6）
    # 注：需要 Scraper API 权限，当前 API key 为 Dataset 权限，暂不支持
    # 如需开通请联系 Bright Data 升级 API key 权限
}

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type":  "application/json",
}


# ══════════════════════════════════════════════════════════════════
# Phase 1 — 提交任务
# ══════════════════════════════════════════════════════════════════

def submit_all_jobs(checkin: str, checkout: str) -> dict:
    """提交今晚的抓取任务，返回 {label: snapshot_id}"""
    snapshots = {}

    checkin_iso  = f"{checkin}T00:00:00.000Z"
    checkout_iso = f"{checkout}T00:00:00.000Z"

    # ① Agoda 含价格搜索（国际/东南亚客群视角）
    d = DATASET["agoda_pricing"]
    url = f"{SCRAPE_URL}?dataset_id={d['id']}&notify=false&include_errors=true{d['params']}"
    payload = {"input": [{"url": "https://www.agoda.com", "location": "Macau",
                          "check_in": checkin_iso, "check_out": checkout_iso,
                          "adults": 2, "currency": "", "country": ""}]}
    snap_id = _submit(url, payload, "Agoda pricing")
    if snap_id:
        snapshots[f"agoda_{checkin}"] = snap_id

    return snapshots


def _submit(url: str, payload: dict, label: str) -> str | None:
    """提交一个任务，返回 snapshot_id（失败返回 None）"""
    try:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=120)
        if not resp.ok:
            print(f"  ⚠️  [{label}] {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        snap_id = data.get("snapshot_id", "")
        if snap_id:
            print(f"  ✅ [{label}] submitted → {snap_id}")
            return snap_id
        print(f"  ⚠️  [{label}] no snapshot_id: {data}")
        return None
    except Exception as e:
        print(f"  ⚠️  [{label}] submit error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# Phase 2 — 取回结果
# ══════════════════════════════════════════════════════════════════

def fetch_all(snapshots: dict, date_str: str) -> dict:
    """取回所有已完成的 snapshot，返回 {label: [records]}"""
    results = {}
    for label, snap_id in snapshots.items():
        status = _check_status(snap_id)
        if status != "ready":
            print(f"  ⏳ [{label}] status={status}, 稍后再试")
            continue
        records = _fetch_snapshot(snap_id, label)
        if records:
            results[label] = records

    if results:
        out_path = RESULTS_DIR / f"{date_str}_macau_prices.json"
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 结果已保存: {out_path}")
        _print_price_summary(results)
        _export_to_db(results, date_str)
    return results


def _check_status(snap_id: str) -> str:
    try:
        r = requests.get(
            f"https://api.brightdata.com/datasets/v3/progress/{snap_id}",
            headers=HEADERS, timeout=15)
        return r.json().get("status", "unknown") if r.ok else "error"
    except:
        return "error"


def _fetch_snapshot(snap_id: str, label: str) -> list[dict]:
    try:
        r = requests.get(
            f"https://api.brightdata.com/datasets/v3/snapshot/{snap_id}?format=json",
            headers=HEADERS, timeout=60)
        if not r.ok:
            print(f"  ⚠️  [{label}] fetch error {r.status_code}")
            return []
        data = r.json()
        records = data if isinstance(data, list) else data.get("results", [])
        print(f"  ✅ [{label}] {len(records)} records")
        return records
    except Exception as e:
        print(f"  ⚠️  [{label}] fetch exception: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 价格提取 + 展示
# ══════════════════════════════════════════════════════════════════

def _print_price_summary(results: dict):
    print("\n── 价格摘要 ──────────────────────────────────────────")
    for label, records in results.items():
        prices = _extract_prices(records, label)
        if prices:
            lo, hi = prices[0]["price_usd"], prices[-1]["price_usd"]
            src = "Trip.com" if "trip" in label.lower() else "Agoda"
            print(f"\n  [{label}] {src} | {len(prices)} 家有价格 | USD {lo:.0f}–{hi:.0f} "
                  f"(MOP {lo*8:.0f}–{hi*8:.0f})")
            for p in prices[:8]:
                print(f"    {p['name']:40s} USD {p['price_usd']:6.0f}  MOP {p['price_usd']*8:6.0f}")
        else:
            keys = list(records[0].keys())[:8] if records else []
            print(f"\n  [{label}] {len(records)} 条记录，无价格字段 | 字段: {keys}")


def _extract_prices(records: list[dict], label: str = "") -> list[dict]:
    """
    统一提取价格，返回 list[{name, price_usd, source}]
    支持：
      - Agoda 嵌套结构 pricing[].offers[].price.final_price_per_night（USD）
      - Trip.com 搜索平铺字段 price / min_price / lowest_price（CNY 或 USD）
      - Trip.com 嵌套 room_info[].price_info.price / discount_price
    """
    is_trip = "trip" in label.lower()
    prices = []

    for h in records:
        name = (h.get("name") or h.get("hotel_name") or
                h.get("title") or h.get("property_name") or
                h.get("hotelName") or "")
        price_usd = None

        # ── Agoda 嵌套定价（pricing[].offers[].price.final_price_per_night）────
        for room in (h.get("pricing") or []):
            for offer in (room.get("offers") or []):
                p = offer.get("price", {}) or {}
                v = p.get("final_price_per_night") or p.get("initial_price_per_night")
                curr = p.get("currency", "USD")
                if v and float(v) > 0:
                    usd = float(v) if curr == "USD" else float(v) / 8
                    if price_usd is None or usd < price_usd:
                        price_usd = usd

        # ── Trip.com 平铺字段（price/min_price/lowest_price/discounted_price）──
        if price_usd is None:
            for field in ("discounted_price", "lowest_price", "min_price", "price",
                          "lowest_price_usd", "price_usd"):
                v = h.get(field)
                if v:
                    try:
                        fv = float(str(v).replace(",", "").strip())
                        if fv > 0:
                            # Trip.com 默认 CNY，若字段名含 usd 则已是 USD
                            if "usd" in field.lower() or fv < 500:
                                price_usd = fv          # 看起来已是 USD
                            else:
                                price_usd = fv / 7.2    # CNY → USD（近似）
                            break
                    except (ValueError, TypeError):
                        pass

        # ── Trip.com 嵌套 room_info / rooms_info ────────────────────────────
        if price_usd is None:
            for room in (h.get("room_info") or h.get("rooms_info") or h.get("rooms") or []):
                if not isinstance(room, dict):
                    continue
                pi = room.get("price_info") or room.get("priceInfo") or {}
                for pf in ("discount_price", "price", "current_price", "lowest_price"):
                    v = pi.get(pf)
                    if v:
                        try:
                            fv = float(str(v).replace(",", "").strip())
                            if fv > 0:
                                price_usd = fv / 7.2    # Trip.com CNY
                                break
                        except (ValueError, TypeError):
                            pass
                if price_usd:
                    break

        if price_usd is None or price_usd <= 0 or not name:
            continue

        source = "trip" if is_trip else "agoda"
        prices.append({"name": str(name)[:40], "price_usd": round(price_usd, 2),
                       "source": source})

    prices.sort(key=lambda x: x["price_usd"])
    return prices


# ══════════════════════════════════════════════════════════════════
# 写入 makcorps_cache.db
# ══════════════════════════════════════════════════════════════════

def _export_to_db(results: dict, date_str: str):
    """
    把 Agoda + Trip.com 价格信号写入 makcorps_cache.db。
    写三个 key：
      brightdata_agoda_{date}   — Agoda 单独信号
      brightdata_trip_{date}    — Trip.com 单独信号（有数据才写）
      brightdata_pace_{date}    — 两者合并信号（模型主用）
    """
    import sqlite3
    db_path = Path(__file__).parent.parent / "simulation_test" / "makcorps_cache.db"
    if not db_path.exists():
        print("  ℹ️  makcorps_cache.db 不存在，跳过")
        return

    agoda_prices_usd: list[float] = []
    trip_prices_usd:  list[float] = []

    for label, records in results.items():
        extracted = _extract_prices(records, label)
        for p in extracted:
            if p["source"] == "agoda":
                agoda_prices_usd.append(p["price_usd"])
            else:
                trip_prices_usd.append(p["price_usd"])

    if not agoda_prices_usd and not trip_prices_usd:
        print("  ⚠️  两个来源均无价格数据，跳过 DB 写入")
        return

    conn = sqlite3.connect(db_path)
    ts   = time.time()

    def _make_signal(prices: list[float], source: str, extra: str = "") -> dict:
        avg = sum(prices) / len(prices)
        prices_s = sorted(prices)
        return {
            "signal":         round(min(1.0, avg / 300), 4),
            "source":         source,
            "hotels_sampled": len(prices),
            "avg_price_usd":  round(avg, 2),
            "avg_price_mop":  round(avg * 8, 0),
            "min_usd":        round(prices_s[0], 2),
            "max_usd":        round(prices_s[-1], 2),
            "p25_usd":        round(prices_s[len(prices_s) // 4], 2),
            "p75_usd":        round(prices_s[len(prices_s) * 3 // 4], 2),
            "detail":         f"BrightData {source} {len(prices)} Macau hotels{extra}",
        }

    # ① Agoda 信号
    if agoda_prices_usd:
        sig = _make_signal(agoda_prices_usd, "agoda")
        conn.execute("INSERT OR REPLACE INTO mc_cache (key, value, ts) VALUES (?, ?, ?)",
                     (f"brightdata_agoda_{date_str}", json.dumps(sig), ts))
        print(f"  📊 Agoda   signal={sig['signal']:.3f}  avg={sig['avg_price_usd']:.0f} USD "
              f"(MOP {sig['avg_price_mop']:.0f})  {sig['hotels_sampled']}家")

    # ② Trip.com 信号
    if trip_prices_usd:
        sig = _make_signal(trip_prices_usd, "trip")
        conn.execute("INSERT OR REPLACE INTO mc_cache (key, value, ts) VALUES (?, ?, ?)",
                     (f"brightdata_trip_{date_str}", json.dumps(sig), ts))
        print(f"  📊 Trip.com signal={sig['signal']:.3f}  avg={sig['avg_price_usd']:.0f} USD "
              f"(MOP {sig['avg_price_mop']:.0f})  {sig['hotels_sampled']}家")

    # ③ 合并信号（模型主用）— 两个来源各占 50% 权重（有则用，无则全用另一个）
    all_prices = agoda_prices_usd + trip_prices_usd
    combined = _make_signal(
        all_prices, "brightdata",
        f" (Agoda:{len(agoda_prices_usd)} Trip:{len(trip_prices_usd)})"
    )
    conn.execute("INSERT OR REPLACE INTO mc_cache (key, value, ts) VALUES (?, ?, ?)",
                 (f"brightdata_pace_{date_str}", json.dumps(combined), ts))
    print(f"  📊 合并     signal={combined['signal']:.3f}  avg={combined['avg_price_usd']:.0f} USD "
          f"(MOP {combined['avg_price_mop']:.0f})  共{combined['hotels_sampled']}家")

    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

def main():
    if not TOKEN:
        print("❌ 请在 .env 设置 BRIGHT_DATA_TOKEN")
        return

    today    = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    checkin  = date_str
    checkout = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"

    print(f"\n{'='*60}")
    print(f"  InsightBridge × Bright Data  |  {date_str}  |  mode={mode}")
    print(f"{'='*60}\n")

    if mode in ("submit", "auto"):
        print("── Phase 1: 提交任务 ──────────────────────────────────")
        snapshots = submit_all_jobs(checkin, checkout)
        if snapshots:
            SNAPSHOT_FILE.write_text(
                json.dumps({"date": date_str, "snapshots": snapshots}, indent=2))
            print(f"\n  Snapshot IDs 已保存: {SNAPSHOT_FILE}")
        if mode == "submit":
            print("\n  任务已提交。等 15-30 分钟后运行:")
            print(f"  python3 {Path(__file__).name} fetch")
            return

    if mode in ("fetch", "auto"):
        print("\n── Phase 2: 取回结果 ──────────────────────────────────")
        # 加载 snapshot IDs
        if SNAPSHOT_FILE.exists():
            saved = json.loads(SNAPSHOT_FILE.read_text())
            snapshots = saved.get("snapshots", {})
            date_str  = saved.get("date", date_str)
        else:
            print("  ⚠️  找不到 pending_snapshots.json，请先运行 submit")
            return

        if mode == "auto":
            # 等待 20 分钟后取回
            print("  等待 20 分钟让任务完成...")
            for i in range(20):
                time.sleep(60)
                print(f"  {i+1}/20 分钟...")
                # 提前检查是否都完成了
                all_ready = all(_check_status(sid) == "ready"
                                for sid in snapshots.values())
                if all_ready:
                    print("  所有任务已完成！")
                    break

        fetch_all(snapshots, date_str)


if __name__ == "__main__":
    main()
