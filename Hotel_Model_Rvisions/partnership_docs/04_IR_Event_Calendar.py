"""
InsightBridge — 澳门 IR 活动日历抓取器
========================================
抓取澳门主要综合娱乐场（IR）近期活动，生成 event_signal 写入模型。

数据源（按优先级）：
  1. 威尼斯人/Cotai Arena  venetianmacao.com/entertainment.html  ← 澳门最大演唱会场地
  2. Galaxy Macau          galaxymacau.com/ticketing/event-list/
  3. Wynn Macau / Palace   wynnresortsmacau.com
  4. MGM Cotai / Macau     mgm.mo/en/entertainment
  5. 澳门文化中心 (CCM)    ccm.gov.mo
  6. Klook Macau           klook.com/en-US/macau-sar-activities/

信号定义：
  event_signal = 0.0 ~ 1.0
    0.0  = 未来7天无大型活动
    0.3  = 有小型/常驻节目
    0.6  = 有中型演唱会/颁奖礼（500-5000人）
    1.0  = 有超大型演唱会/拳击赛/节庆（>5000人）

运行方式：
  python3 04_IR_Event_Calendar.py
  python3 04_IR_Event_Calendar.py --days 14   # 看未来14天
"""

from __future__ import annotations
import json, re, sqlite3, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 配置 ──────────────────────────────────────────────────────────────────────
RESULTS_DIR = Path(__file__).parent / "brightdata_results"
RESULTS_DIR.mkdir(exist_ok=True)
CACHE_TTL   = 3600 * 6   # 6小时缓存（活动信息变化慢）

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 大型活动关键词（含→权重）
BIG_EVENT_KW = {
    # 超大型（演唱会/拳击/格斗）→ 高权重
    "concert": 0.9, "演唱会": 0.9, "live show": 0.8, "boxing": 0.9,
    "ufc": 1.0, "mma": 0.9, "fight night": 1.0, "world tour": 0.9,
    "grand prix": 1.0, "formula": 0.9,
    # 中型（颁奖礼/嘉年华）→ 中权重
    "award": 0.6, "ceremony": 0.6, "awards": 0.6, "gala": 0.6,
    "festival": 0.7, "carnival": 0.6, "championship": 0.7,
    "exhibition": 0.4, "showcase": 0.4,
    # 常驻节目 → 低权重
    "residency": 0.3, "art": 0.25, "performance": 0.3,
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def _parse_date_str(s: str) -> Optional[datetime]:
    """尝试解析各种日期字符串，返回 datetime 或 None"""
    s = s.strip()
    # ISO 格式
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            pass
    # "May 30, 2026" / "May 30 2026" / "30 May 2026"
    m = re.match(r'(\w+)\s+(\d{1,2}),?\s*(\d{4})', s, re.I)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                pass
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s, re.I)
    if m:
        mon = MONTH_MAP.get(m.group(2).lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                pass
    # "May 30" (无年份 → 猜当前年或下一年)
    m = re.match(r'(\w+)\s+(\d{1,2})$', s, re.I)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            now = datetime.now()
            try:
                dt = datetime(now.year, mon, int(m.group(2)))
                if dt < now - timedelta(days=30):
                    dt = datetime(now.year + 1, mon, int(m.group(2)))
                return dt
            except ValueError:
                pass
    return None


def _event_weight(text: str) -> float:
    """根据关键词估算活动权重"""
    text_lower = text.lower()
    best = 0.0
    for kw, w in BIG_EVENT_KW.items():
        if kw in text_lower:
            best = max(best, w)
    return best


def _score_events(events: list[dict], window_days: int = 7) -> dict:
    """
    给事件列表打分，返回信号值和摘要。
    events: list of {title, date_str, venue, weight}
    """
    now   = datetime.now()
    cutoff = now + timedelta(days=window_days)
    active = []

    for ev in events:
        dt = _parse_date_str(ev.get("date_str", ""))
        if dt and now - timedelta(days=1) <= dt <= cutoff:
            ev["date"] = dt
            active.append(ev)

    if not active:
        return {"signal": 0.0, "event_count": 0, "top_events": [], "window_days": window_days}

    # 最高单项权重 × 数量衰减因子
    weights = sorted([ev.get("weight", 0.3) for ev in active], reverse=True)
    signal  = weights[0]
    for i, w in enumerate(weights[1:], 1):
        signal = max(signal, signal * 0.85 + w * 0.15)  # 边际递减

    top = sorted(active, key=lambda e: e.get("weight", 0), reverse=True)[:5]
    return {
        "signal":      round(min(1.0, signal), 3),
        "event_count": len(active),
        "top_events":  [{"title": e["title"], "date": e["date"].strftime("%Y-%m-%d"),
                         "venue": e.get("venue",""), "weight": e.get("weight",0)} for e in top],
        "window_days": window_days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 各 IR 抓取函数（Playwright）
# ══════════════════════════════════════════════════════════════════════════════

def _scrape_with_playwright(url: str, wait_selector: str = None,
                             wait_ms: int = 3000) -> str:
    """用 Playwright 渲染页面，返回完整 HTML"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=8000)
                except Exception:
                    pass
            else:
                page.wait_for_timeout(wait_ms)
            html = page.content()
        finally:
            browser.close()
    return html


def scrape_venetian_sands(window_days: int = 7) -> list[dict]:
    """
    抓取 威尼斯人澳门（Venetian Arena / Cotai Arena）演出日程。
    方法：GET entertainment.html → 收集所有 /entertainment/[slug].html 链接
         → 逐页提取 og:title + 日期（最多12页，节省时间）
    无需 Playwright（静态 HTML 可直接抓取）
    """
    import requests as req
    print("  抓取 Venetian/Sands Cotai Arena...", end=" ", flush=True)
    events = []
    BASE = "https://www.venetianmacao.com"
    try:
        r = req.get(f"{BASE}/entertainment.html",
                    headers={"User-Agent": UA}, timeout=12)
        html = r.text

        # 找所有活动页链接
        slugs = list(dict.fromkeys(
            re.findall(r'/entertainment/([a-zA-Z0-9_-]{3,60})\.html', html)
        ))[:12]   # 最多12个活动，避免请求过多

        for slug in slugs:
            try:
                rr = req.get(f"{BASE}/entertainment/{slug}.html",
                             headers={"User-Agent": UA}, timeout=10)
                ph = rr.text

                # 提取标题：先试 og:title，不行用 <title> 标签
                og_t = (re.search(r'content="([^"]+)"\s+property="og:title"', ph)
                        or re.search(r'property="og:title"\s+content="([^"]+)"', ph)
                        or re.search(r'<title>([^<]+)</title>', ph))
                title = og_t.group(1).strip() if og_t else ""
                # 去掉 " | Entertainment | The Venetian Macao" 等尾部描述
                title = re.sub(r'\s*[|–—]\s*(?:Entertainment|Exhibition|The Venetian|Macao).*$',
                               '', title, flags=re.I).strip()
                # 去掉 "The Venetian Macao Proudly Presents: " 前缀
                title = re.sub(r'^(?:The\s+Venetian\s+Macao\s+(?:Proudly\s+)?Presents?:?\s*)',
                               '', title, flags=re.I).strip()

                # 提取日期（各种格式）
                dates_found = re.findall(
                    r'(?:January|February|March|April|May|June|July|August|'
                    r'September|October|November|December)\s+\d{1,2}(?:,?\s*20[2-9]\d)?'
                    r'|20[2-9]\d-\d{2}-\d{2}',
                    ph, re.I
                )
                date_str = dates_found[0] if dates_found else ""

                if title and len(title) > 4:
                    events.append({
                        "title":    title,
                        "date_str": date_str,
                        "venue":    "Venetian Arena / Cotai",
                        "weight":   _event_weight(title) or 0.6,  # 进 Cotai Arena 默认中等权重
                    })
            except Exception:
                pass

        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


def scrape_galaxy(window_days: int = 7) -> list[dict]:
    """抓取 Galaxy Macau 活动列表"""
    print("  抓取 Galaxy Macau...", end=" ", flush=True)
    events = []
    try:
        html = _scrape_with_playwright(
            "https://www.galaxymacau.com/ticketing/event-list/",
            wait_selector=".event-card, .event-item, article",
            wait_ms=4000
        )
        # 提取活动标题和日期
        titles = re.findall(
            r'(?:class="[^"]*(?:title|heading|name)[^"]*"[^>]*>|"title"\s*:\s*")([^<"]{5,100})',
            html, re.I
        )
        dates  = re.findall(
            r'(?:class="[^"]*date[^"]*"[^>]*>|"date[^"]*"\s*:\s*")([^<"]{4,30})',
            html, re.I
        )
        # 也从 JSON-LD 里找
        json_ld_blocks = re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.DOTALL
        )
        for block in json_ld_blocks:
            try:
                data = json.loads(block)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = item.get("name") or item.get("headline") or ""
                    d = (item.get("startDate") or item.get("date") or "")[:10]
                    if t and d:
                        events.append({
                            "title": t, "date_str": d, "venue": "Galaxy Macau",
                            "weight": _event_weight(t) or 0.3
                        })
            except Exception:
                pass

        # 合并 title+date 提取
        for i, title in enumerate(titles[:20]):
            title = re.sub(r'\s+', ' ', title).strip()
            if len(title) < 4:
                continue
            date_str = dates[i] if i < len(dates) else ""
            events.append({
                "title": title, "date_str": date_str, "venue": "Galaxy Macau",
                "weight": _event_weight(title) or 0.3
            })
        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


def scrape_wynn(window_days: int = 7) -> list[dict]:
    """抓取 Wynn Macau + Wynn Palace 活动"""
    print("  抓取 Wynn Macau/Palace...", end=" ", flush=True)
    events = []
    try:
        html = _scrape_with_playwright(
            "https://www.wynnresortsmacau.com/en/wynn-macau/",
            wait_ms=3000
        )
        # Wynn 用 JS 框架，从 HTML 提取活动文字+日期
        titles = re.findall(
            r'(?:title|headline|event_name)[\":\s]+([A-Z][^\",\\\\]{8,100})', html
        )
        dates  = re.findall(
            r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,?\s*202\d)?',
            html
        )
        for i, title in enumerate(titles[:30]):
            title = re.sub(r'\s+', ' ', title).strip()
            if not title or len(title) < 5:
                continue
            date_str = dates[i] if i < len(dates) else ""
            events.append({
                "title": title, "date_str": date_str, "venue": "Wynn Macau/Palace",
                "weight": _event_weight(title) or 0.25
            })
        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


def scrape_mgm(window_days: int = 7) -> list[dict]:
    """抓取 MGM Macau + MGM Cotai 活动"""
    print("  抓取 MGM Macau/Cotai...", end=" ", flush=True)
    events = []
    try:
        html = _scrape_with_playwright(
            "https://www.mgm.mo/en/entertainment",
            wait_selector=".event, .show, .entertainment",
            wait_ms=4000
        )
        # 从 CDN 路径猜活动（/Events_XXX_2026_名称.jpg → 活动名）
        cdn_events = re.findall(
            r'Events[_\-]([^/_\s\"\'.]{3,60})_?(?:202[56])?',
            html, re.I
        )
        for ev in list(dict.fromkeys(cdn_events))[:15]:
            title = re.sub(r'[_%]', ' ', ev).strip()
            if len(title) > 4:
                events.append({
                    "title": title, "date_str": "", "venue": "MGM Macau/Cotai",
                    "weight": _event_weight(title) or 0.3
                })

        # 找结构化文字
        titles = re.findall(
            r'(?:\"title\"|\"name\"|\"headline\"):\s*\"([^\"]{8,80})\"', html
        )
        dates  = re.findall(
            r'\"(?:date|startDate|endDate)\":\s*\"(\d{4}-\d{2}-\d{2})', html
        )
        for i, title in enumerate(titles[:15]):
            events.append({
                "title": title, "date_str": dates[i] if i < len(dates) else "",
                "venue": "MGM Macau/Cotai", "weight": _event_weight(title) or 0.25
            })
        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


def scrape_ccm(window_days: int = 7) -> list[dict]:
    """抓取澳门文化中心演出表（静态页面，不需要 Playwright）"""
    print("  抓取 CCM 澳门文化中心...", end=" ", flush=True)
    import requests as req
    events = []
    try:
        r = req.get("https://www.ccm.gov.mo/eng/show.php",
                    headers={"User-Agent": UA}, timeout=12)
        html = r.text
        # CCM 是简单 HTML，找演出名+日期
        rows = re.findall(
            r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.I
        )
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.I)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(cells) >= 2:
                date_str = cells[0] if cells else ""
                title    = cells[1] if len(cells) > 1 else ""
                if title and len(title) > 3:
                    events.append({
                        "title": title, "date_str": date_str, "venue": "CCM",
                        "weight": _event_weight(title) or 0.3
                    })
        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


# ══════════════════════════════════════════════════════════════════════════════
# 主函数
# ══════════════════════════════════════════════════════════════════════════════

def scrape_klook(window_days: int = 7) -> list[dict]:
    """抓取 Klook 澳门近期活动（静态 + JS，有真实日期）"""
    print("  抓取 Klook Macau...", end=" ", flush=True)
    import requests as req
    events = []
    try:
        headers = {"User-Agent": UA,
                   "Accept-Language": "en-US,en;q=0.9"}
        r = req.get("https://www.klook.com/en-US/macau-sar-activities/",
                    headers=headers, timeout=12, allow_redirects=True)
        html = r.text
        # Klook 有 Next.js 数据
        nd_match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if nd_match:
            try:
                nd = json.loads(nd_match.group(1))
                # 递归搜索活动列表
                def _extract(obj, depth=0):
                    if depth > 8 or not obj: return
                    if isinstance(obj, list):
                        for item in obj:
                            _extract(item, depth+1)
                    elif isinstance(obj, dict):
                        title = obj.get("title") or obj.get("name") or obj.get("activityTitle") or ""
                        date  = obj.get("date") or obj.get("startDate") or obj.get("eventDate") or ""
                        if title and len(str(title)) > 5:
                            events.append({
                                "title": str(title)[:80],
                                "date_str": str(date)[:20] if date else "",
                                "venue": "Klook Macau",
                                "weight": _event_weight(str(title)) or 0.2
                            })
                        for v in obj.values():
                            _extract(v, depth+1)
                _extract(nd)
            except Exception:
                pass

        # 补充：直接从 HTML 抓标题
        titles = re.findall(r'"title"\s*:\s*"([^"]{8,80})"', html)
        dates  = re.findall(r'202[56]-\d{2}-\d{2}', html)
        for i, t in enumerate(list(dict.fromkeys(titles))[:15]):
            events.append({
                "title": t, "date_str": dates[i] if i < len(dates) else "",
                "venue": "Klook Macau", "weight": _event_weight(t) or 0.2
            })

        print(f"找到 {len(events)} 条")
    except Exception as e:
        print(f"失败: {e}")
    return events


def fetch_ir_event_signal(window_days: int = 7, force: bool = False) -> dict:
    """
    抓取所有 IR 活动，返回合并信号。
    结果缓存6小时到 brightdata_results/ir_events_cache.json。
    """
    cache_file = RESULTS_DIR / "ir_events_cache.json"

    # 检查缓存
    if not force and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - cached.get("fetched_ts", 0)
            if age < CACHE_TTL:
                print(f"  ℹ️  使用缓存（{age/3600:.1f}h 前）")
                return cached
        except Exception:
            pass

    print(f"\n── IR 活动日历抓取（未来{window_days}天）──")
    t0 = time.time()

    all_events: list[dict] = []
    all_events.extend(scrape_venetian_sands(window_days))   # ← 威尼斯人/Cotai Arena（无需Playwright）
    all_events.extend(scrape_ccm(window_days))
    all_events.extend(scrape_klook(window_days))
    all_events.extend(scrape_wynn(window_days))
    all_events.extend(scrape_mgm(window_days))
    all_events.extend(scrape_galaxy(window_days))

    scored = _score_events(all_events, window_days)
    result = {
        "signal":       scored["signal"],
        "event_count":  scored["event_count"],
        "top_events":   scored["top_events"],
        "total_raw":    len(all_events),
        "window_days":  window_days,
        "fetched_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fetched_ts":   time.time(),
        "elapsed_s":    round(time.time() - t0, 1),
    }

    # 保存结果
    date_str = datetime.now().strftime("%Y-%m-%d")
    out_file = RESULTS_DIR / f"{date_str}_ir_events.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    _export_event_signal_to_db(result, date_str)
    return result


def _export_event_signal_to_db(result: dict, date_str: str):
    """写入本地事件信号缓存，供模拟模型读取"""
    db_path = Path(__file__).parent.parent / "simulation_test" / "event_signal_cache.db"
    if not db_path.exists():
        return
    signal_data = {
        "signal":      result["signal"],
        "source":      "ir_events",
        "event_count": result["event_count"],
        "top_events":  result["top_events"][:3],
        "detail":      f"IR活动 {result['event_count']}场（未来{result['window_days']}天）",
    }
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO mc_cache (key, value, ts) VALUES (?, ?, ?)",
                 (f"ir_event_signal_{date_str}", json.dumps(signal_data), time.time()))
    conn.commit()
    conn.close()
    print(f"  📅 IR活动信号 signal={result['signal']:.3f}  未来{result['window_days']}天 {result['event_count']}场活动  已写入DB")


def print_summary(result: dict):
    print(f"\n{'='*60}")
    print(f"  IR 活动日历  {result['fetched_at']}")
    print(f"{'='*60}")
    print(f"  抓取原始条目: {result['total_raw']}条")
    print(f"  未来{result['window_days']}天活动:  {result['event_count']}场")
    print(f"  活动信号值:   {result['signal']:.3f}  ", end="")
    if   result['signal'] >= 0.8: print("🔴 超大型活动（演唱会/格斗赛）")
    elif result['signal'] >= 0.6: print("🟠 大型活动（演唱会/颁奖礼）")
    elif result['signal'] >= 0.3: print("🟡 中型活动")
    elif result['signal'] > 0.0:  print("🟢 小型/常驻节目")
    else:                          print("⚪ 无活动")
    if result['top_events']:
        print(f"\n  近期主要活动:")
        for ev in result['top_events']:
            print(f"    [{ev['date']}] {ev['title'][:50]}  ({ev['venue']})  w={ev['weight']:.2f}")
    print(f"\n  耗时: {result['elapsed_s']}s")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    days = 7
    force = False
    for arg in sys.argv[1:]:
        if arg.startswith("--days"):
            days = int(arg.split("=")[-1]) if "=" in arg else int(sys.argv[sys.argv.index(arg)+1])
        if arg == "--force":
            force = True

    result = fetch_ir_event_signal(window_days=days, force=force)
    print_summary(result)
