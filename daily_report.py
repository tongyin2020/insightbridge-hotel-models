#!/usr/bin/env python3
"""
InsightBridge AI 模型每日报告
============================
每天 09:00 由 launchd 触发，汇总三套系统、9个AI模型运行状态与KPI，
保存到桌面项目，并推送到 Telegram。

手动测试：
  python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/daily_report.py
"""

from __future__ import annotations
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# ── 路径配置 ──────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
SYS2_DB      = BASE_DIR / "system2_claude_simulation" / "results.db"
SYS3_DB      = BASE_DIR / "system3_crewai" / "crewai_results.db"
SYS1_OUTDIR  = BASE_DIR / "hotel_model_staging_output"
COLLECTOR_DB = BASE_DIR / "hotel_collector" / "hotel_real_data.db"
REPORT_DIR   = BASE_DIR / "reports" / "daily_model_reports"
LATEST_REPORT = BASE_DIR / "reports" / "latest_ai_model_daily_report.md"
TG_OWNER_FILE = Path.home() / "telegram_bot" / "owner_chat_id.txt"

TODAY = datetime.now().strftime("%Y-%m-%d")
NOW   = datetime.now().strftime("%Y-%m-%d %H:%M")

# ── 工具函数 ──────────────────────────────────────────────────────────────
def _pct(a, b) -> str:
    return f"{a/b*100:.1f}%" if b and b > 0 else "N/A"

def _mop(v) -> str:
    if v is None or v == 0:
        return "N/A"
    return f"MOP {int(v):,}"

def _tier_label(t) -> str:
    t = str(t)
    if "5_deluxe" in t: return "5★豪华"
    if "5_star"   in t: return "5★"
    if "4_star"   in t: return "4★"
    if "3_star"   in t: return "3★"
    return t

def _db(path: Path):
    """返回只读 SQLite 连接，或 None"""
    if not path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:
        return None

def _avg(lst):
    return sum(lst) / len(lst) if lst else None

def _save_report(report: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_path = REPORT_DIR / f"ai_model_daily_report_{stamp}.md"
    saved_path.write_text(report, encoding="utf-8")
    LATEST_REPORT.write_text(report, encoding="utf-8")
    return saved_path

def _load_telegram_chat_id() -> str | None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if chat_id:
        return chat_id
    try:
        text = TG_OWNER_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None

def _telegram_send_text(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _load_telegram_chat_id()
    if not token or not chat_id:
        print("⚠️ Telegram 未配置完整，跳过文本推送")
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "-sS",
                "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "-d", f"chat_id={chat_id}",
                "-d", f"text={text}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and '"ok":true' in result.stdout
    except Exception as exc:
        print(f"⚠️ Telegram 文本推送异常: {exc}")
        return False

def _telegram_send_document(path: Path) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = _load_telegram_chat_id()
    if not token or not chat_id:
        print("⚠️ Telegram 未配置完整，跳过附件推送")
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl",
                "-sS",
                "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendDocument",
                "-F", f"chat_id={chat_id}",
                "-F", f"document=@{path}",
                "-F", f"caption=InsightBridge AI 模型日报 {TODAY}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode == 0 and '"ok":true' in result.stdout
    except Exception as exc:
        print(f"⚠️ Telegram 附件推送异常: {exc}")
        return False

def _build_telegram_brief(saved_path: Path) -> str:
    headline = _headline_section()
    brief_lines = []
    for raw_line in headline.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("###"):
            continue
        if line.startswith("- "):
            line = line[2:]
        brief_lines.append(line)
    brief_lines.append(f"完整报告已保存：{saved_path}")
    return "\n".join([
        f"🏨 InsightBridge AI 模型日报",
        NOW,
        "",
        *brief_lines,
    ])

def _days_since(ts_str: str) -> str:
    try:
        fd = datetime.fromisoformat(ts_str[:19])
        return str((datetime.now() - fd).days + 1)
    except Exception:
        return "?"

def _hourly_group_stats(conn, since_expr: str | None = None) -> dict[str, tuple]:
    where = f"WHERE run_at >= datetime('now','{since_expr}')" if since_expr else ""
    rows = conn.execute(f"""
        WITH dedup AS (
            SELECT *
            FROM hourly_runs
            WHERE id IN (
                SELECT MAX(id)
                FROM hourly_runs
                {where}
                GROUP BY sim_hour, hotel_id, model_type
            )
        )
        SELECT
          CASE
            WHEN model_type LIKE 'MARE%'     THEN 'MARE'
            WHEN model_type LIKE 'DIRECTOR%' THEN 'DIRECTOR'
            WHEN model_type LIKE 'SELFACQ%'  THEN 'SELFACQ'
            ELSE model_type
          END as grp,
          COUNT(*),
          AVG(rec_price),
          SUM(CASE WHEN anomaly LIKE '%EXCEPTION%' THEN 1 ELSE 0 END),
          SUM(CASE WHEN anomaly IS NOT NULL AND trim(anomaly) != '' THEN 1 ELSE 0 END),
          AVG(CASE WHEN exp_lift IS NOT NULL AND exp_lift != '' THEN
              CAST(REPLACE(REPLACE(exp_lift,'%',''),' ','') AS REAL) ELSE NULL END)
        FROM dedup
        GROUP BY grp
    """).fetchall()
    return {r[0]: r for r in rows}

def _collector_freshness() -> dict:
    conn = _db(COLLECTOR_DB)
    if not conn:
        return {"status": "unknown", "line": "采集库不可用"}
    last_row = conn.execute("""
        SELECT MAX(snapshot_time), COUNT(*), SUM(source_ok)
        FROM price_snapshots
        WHERE snapshot_time >= datetime('now','-24 hours')
    """).fetchone() or (None, 0, 0)
    latest_any = conn.execute("""
        SELECT MAX(snapshot_time) FROM price_snapshots
    """).fetchone()
    conn.close()

    last_24h, cnt_24h, ok_24h = last_row[0], last_row[1] or 0, last_row[2] or 0
    latest_any = latest_any[0] if latest_any else None
    if cnt_24h > 0:
        return {
            "status": "fresh",
            "line": f"采集新鲜：近24h {cnt_24h} 条，成功率 {_pct(ok_24h, cnt_24h)}，最近 {last_24h}",
        }
    if latest_any:
        return {
            "status": "stale",
            "line": f"采集过期：近24h 0 条，最近一次 {latest_any}",
        }
    return {"status": "empty", "line": "采集为空：price_snapshots 暂无记录"}

def _headline_section() -> str:
    freshness = _collector_freshness()

    s2 = _db(SYS2_DB)
    s3 = _db(SYS3_DB)
    s2_24 = _hourly_group_stats(s2, "-24 hours") if s2 else {}
    s3_24 = _hourly_group_stats(s3, "-24 hours") if s3 else {}
    if s2:
        s2.close()
    if s3:
        s3.close()

    def _pick(d: dict, key: str):
        return d.get(key, (key, 0, None, 0, 0, None))

    s2_m, s2_d, s2_a = _pick(s2_24, "MARE"), _pick(s2_24, "DIRECTOR"), _pick(s2_24, "SELFACQ")
    s3_m, s3_d, s3_a = _pick(s3_24, "MARE"), _pick(s3_24, "DIRECTOR"), _pick(s3_24, "SELFACQ")

    failure_lines = [
        f"S2 MARE { _pct(s2_m[3], s2_m[1]) } / CRM { _pct(s2_d[3], s2_d[1]) } / SelfACQ { _pct(s2_a[3], s2_a[1]) }",
        f"S3 MARE { _pct(s3_m[3], s3_m[1]) } / CRM { _pct(s3_d[3], s3_d[1]) } / SelfACQ { _pct(s3_a[3], s3_a[1]) }",
    ]
    anomaly_lines = [
        f"S2 MARE { _pct(s2_m[4], s2_m[1]) } / CRM { _pct(s2_d[4], s2_d[1]) } / SelfACQ { _pct(s2_a[4], s2_a[1]) }",
        f"S3 MARE { _pct(s3_m[4], s3_m[1]) } / CRM { _pct(s3_d[4], s3_d[1]) } / SelfACQ { _pct(s3_a[4], s3_a[1]) }",
    ]

    avg_lifts = [x[5] for x in (s2_m, s3_m, s2_d, s3_d) if x[5] is not None]
    lift_line = f"收益信号：近24h 可见 uplift 均值约 {sum(avg_lifts)/len(avg_lifts):.1f}%" if avg_lifts else "收益信号：近24h uplift 样本不足"

    freshness_icon = "✅" if freshness["status"] == "fresh" else ("⚠️" if freshness["status"] == "stale" else "🔴")
    return "\n".join([
        "### 🧭 今日结论",
        "",
        f"- {freshness_icon} {freshness['line']}",
        f"- 程序失败率：{failure_lines[0]}；{failure_lines[1]}",
        f"- 异常率：{anomaly_lines[0]}；{anomaly_lines[1]}",
        f"- {lift_line}",
        "- 口径说明：失败率只算 EXCEPTION；异常率包含护栏和压力测试告警。",
        "",
    ])

def _real_market_avgs() -> tuple:
    """
    返回两个独立市场的真实官网BAR均价（近7天，source_ok=1）：
      low_avg  = 3-4★市场（tiers: 3_star, 4_star）
      high_avg = 5★豪华市场（tiers: 5_star, 5_deluxe）

    数据优先级：
      1. price_snapshots.official_bar（官网BAR，最权威）
    """
    conn = _db(COLLECTOR_DB)
    low_avg = high_avg = None
    if conn:
        # ── 官网BAR价格（主数据源）
        rows = conn.execute("""
            SELECT tier, AVG(official_bar), COUNT(*)
            FROM price_snapshots
            WHERE source_ok=1 AND official_bar > 200
              AND snapshot_time >= datetime('now','-7 days')
            GROUP BY tier
        """).fetchall()
        rm = {r[0]: (r[1], r[2]) for r in rows}

        # 3-4★市场：仅使用官网BAR加权平均
        low_vals, low_cnts = [], []
        for t in ("3_star", "4_star"):
            if t in rm and rm[t][0] and rm[t][1] >= 3:      # 至少3条官网记录才用
                low_vals.append(rm[t][0] * rm[t][1])
                low_cnts.append(rm[t][1])
        if low_cnts:
            low_avg = sum(low_vals) / sum(low_cnts)

        # 5★豪华市场：仅使用官网BAR加权平均
        high_vals, high_cnts = [], []
        for t in ("5_star", "5_deluxe"):
            if t in rm and rm[t][0] and rm[t][1] >= 3:
                high_vals.append(rm[t][0] * rm[t][1])
                high_cnts.append(rm[t][1])
        if high_cnts:
            high_avg = sum(high_vals) / sum(high_cnts)

        conn.close()
    return low_avg, high_avg

def _vs_market(model_price, real_ref, market_label: str) -> str:
    """格式化模型价 vs 真实市场价对比"""
    if model_price and real_ref:
        diff  = model_price - real_ref
        pct   = diff / real_ref * 100
        arrow = "↑" if diff > 0 else "↓"
        return f"{market_label} {arrow}{abs(pct):.1f}%（真实均价 MOP {real_ref:.0f}）"
    return f"{market_label} 真实数据不足"


# ════════════════════════════════════════════════════════════════════════
#  模块一：数据采集健康度（76家酒店）
# ════════════════════════════════════════════════════════════════════════
def collector_section() -> str:
    conn = _db(COLLECTOR_DB)
    if not conn:
        return "### 📡 数据采集健康度\n⚠️ 数据库不可用\n"

    r24 = conn.execute("""
        SELECT COUNT(*), SUM(source_ok), COUNT(DISTINCT hotel_id), MAX(snapshot_time)
        FROM price_snapshots WHERE snapshot_time >= datetime('now','-24 hours')
    """).fetchone() or (0, 0, 0, None)
    total_24, ok_24, hotels_24, last_snap = r24[0], r24[1] or 0, r24[2], r24[3]

    r7 = conn.execute("""
        SELECT COUNT(*), SUM(source_ok), COUNT(DISTINCT hotel_id)
        FROM price_snapshots WHERE snapshot_time >= datetime('now','-7 days')
    """).fetchone() or (0, 0, 0)
    total_7, ok_7 = r7[0], r7[1] or 0

    latest_any = conn.execute("""
        SELECT MAX(snapshot_time) FROM price_snapshots
    """).fetchone()
    latest_any = latest_any[0] if latest_any else None

    tier_rows = conn.execute("""
        SELECT tier, COUNT(*), AVG(official_bar), MIN(official_bar), MAX(official_bar)
        FROM price_snapshots
        WHERE source_ok=1 AND official_bar > 200
          AND snapshot_time >= datetime('now','-7 days')
        GROUP BY tier ORDER BY tier DESC
    """).fetchall()

    err_rows = conn.execute("""
        SELECT substr(notes,1,50), COUNT(*) as cnt
        FROM price_snapshots
        WHERE source_ok=0 AND snapshot_time >= datetime('now','-24 hours')
          AND notes IS NOT NULL AND notes != ''
        GROUP BY substr(notes,1,50) ORDER BY cnt DESC LIMIT 3
    """).fetchall()

    acq_total = conn.execute("""
        SELECT COUNT(*) FROM acquisition_triggers
        WHERE triggered_at >= datetime('now','-7 days')
    """).fetchone()[0] or 0

    conn.close()

    lines = [
        "### 📡 数据采集健康度（76家酒店）\n",
        f"| 指标 | 最近24h | 最近7天 |",
        f"|------|---------|---------|",
        f"| 采集记录数 | {total_24} 条 | {total_7} 条 |",
        f"| 成功率 | {_pct(ok_24, total_24)} | {_pct(ok_7, total_7)} |",
        f"| 覆盖酒店数 | {hotels_24} / 76 | — |",
        f"| 最近采集时间 | {last_snap or 'N/A'} | {latest_any or 'N/A'} |",
        "",
    ]

    if tier_rows:
        lines.append("**实时市场均价（官网BAR，近7天有效数据）**")
        for tier, cnt, avg_bar, mn, mx in tier_rows:
            lines.append(f"- {_tier_label(tier)}：均 MOP {avg_bar:.0f}（{mn:.0f}–{mx:.0f}，{cnt} 条）")
        lines.append("")

    if err_rows:
        lines.append("**⚠️ 采集错误（近24h）**")
        for note, cnt in err_rows:
            lines.append(f"- {note}（{cnt} 次）")
    else:
        if total_24 == 0 and latest_any:
            lines.append(f"⚠️ 近24h 无新采集；最近一次成功/写库时间为 {latest_any}")
        else:
            lines.append("✅ 近24h 无采集错误")

    lines.append(f"\n寻客触发（近7天）：{acq_total} 次\n")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  模块二：系统一（ChatGPT Harness 版）
# ════════════════════════════════════════════════════════════════════════
def sys1_section() -> str:
    if not SYS1_OUTDIR.exists():
        return "### 🤖 系统一：ChatGPT Harness 版\n⚠️ 输出目录不可用\n"

    summaries = sorted(SYS1_OUTDIR.glob("summary_*.json"))
    jsonls    = sorted(SYS1_OUTDIR.glob("run_*.jsonl"))

    if not summaries:
        return "### 🤖 系统一：ChatGPT Harness 版\n⚠️ 无运行记录\n"

    s = json.loads(summaries[-1].read_text())
    cycles    = s.get("cycles_completed", 0)
    mare_runs = s.get("mare_runs", 0)
    dir_runs  = s.get("director_runs", 0)
    acq_runs  = s.get("selfacq_runs", 0)
    mare_fail = s.get("mare_failures", 0)
    dir_fail  = s.get("director_failures", 0)
    acq_fail  = s.get("selfacq_failures", 0)

    days_running = "?"
    if jsonls:
        try:
            first_ts = jsonls[0].stem[4:]  # e.g. 20260506T072512Z
            first_dt = datetime.strptime(first_ts[:15], "%Y%m%dT%H%M%S")
            days_running = (datetime.now() - first_dt).days + 1
        except Exception:
            pass

    # 从最新 JSONL 末尾抽样解析定价 KPI（tail 3000行，速度快）
    # 按市场分组：low=3-4★, high=5★豪华
    mare_low, mare_high = [], []
    dir_prices, acq_prices = [], []
    dir_savings, dir_savings_pct = [], []
    exp_lifts_low, exp_lifts_high = [], []

    if jsonls:
        try:
            tail_out = subprocess.check_output(
                ["tail", "-n", "3000", str(jsonls[-1])], timeout=10
            ).decode("utf-8", errors="ignore")
            for line in tail_out.splitlines():
                if not line.strip():
                    continue
                try:
                    d      = json.loads(line)
                    model  = d.get("model", "")
                    star   = d.get("hotel_star", 0)
                    result = d.get("result") or {}
                    price  = result.get("recommended_price")
                    lift   = result.get("expected_revenue_lift")
                    if price and float(price) > 50:
                        if model == "mare":
                            if star and int(star) >= 5:
                                mare_high.append(float(price))
                                if lift:
                                    try: exp_lifts_high.append(float(str(lift).replace("%","").strip()))
                                    except: pass
                            else:
                                mare_low.append(float(price))
                                if lift:
                                    try: exp_lifts_low.append(float(str(lift).replace("%","").strip()))
                                    except: pass
                        elif model == "director":
                            dir_prices.append(float(price))
                            ch = result.get("channel_pricing") or {}
                            sv = ch.get("direct_savings_vs_ota")
                            sp = ch.get("direct_savings_pct")
                            if sv: dir_savings.append(float(sv))
                            if sp: dir_savings_pct.append(float(sp))
                        elif model == "selfacq":
                            acq_prices.append(float(price))
                except Exception:
                    continue
        except Exception:
            pass

    mare_low_avg  = _avg(mare_low)
    mare_high_avg = _avg(mare_high)
    dir_avg       = _avg(dir_prices)
    acq_avg       = _avg(acq_prices)
    dir_sv        = _avg(dir_savings)
    dir_sp        = _avg(dir_savings_pct)
    lift_low_avg  = _avg(exp_lifts_low)
    lift_high_avg = _avg(exp_lifts_high)

    # 分市场真实均价
    real_low, real_high = _real_market_avgs()

    mare_lines = []
    if mare_low_avg or mare_high_avg:
        if mare_low_avg:
            mare_lines.append(
                f"- 3-4★市场推荐价：{_mop(mare_low_avg)} | "
                + _vs_market(mare_low_avg, real_low, "3-4★市场")
                + (f" | 收益提升 {lift_low_avg:.1f}%" if lift_low_avg else "")
            )
        if mare_high_avg:
            mare_lines.append(
                f"- 5★豪华市场推荐价：{_mop(mare_high_avg)} | "
                + _vs_market(mare_high_avg, real_high, "5★豪华市场")
                + (f" | 收益提升 {lift_high_avg:.1f}%" if lift_high_avg else "")
            )
    else:
        mare_lines.append("- 推荐价：N/A（样本不足）")

    return "\n".join([
        "### 🤖 系统一：ChatGPT Harness 版",
        "",
        "**① MARE 房价引擎**",
        f"- 总运行次数：{mare_runs:,} | 运行天数：{days_running} 天 | 失败率：{_pct(mare_fail, mare_runs)}",
        *mare_lines,
        "",
        "**② DirectorAI CRM（直销引流）**",
        f"- 总运行次数：{dir_runs:,} | 运行天数：{days_running} 天 | 失败率：{_pct(dir_fail, dir_runs)}",
        f"- 直销推荐均价：{_mop(dir_avg) if dir_avg else 'N/A'}",
        f"- 直销 vs OTA 节省：均 MOP {dir_sv:.0f}（{dir_sp:.1f}%）" if dir_sv else
            "- 直销 vs OTA 节省：N/A",
        "",
        "**③ SelfACQ 自主寻客**",
        f"- 总运行次数：{acq_runs:,} | 运行天数：{days_running} 天 | 失败率：{_pct(acq_fail, acq_runs)}",
        f"- 直销引导价均值：{_mop(acq_avg) if acq_avg else 'N/A'}",
        f"- 已完成循环：{cycles} 轮（每轮覆盖全部场景×76酒店）",
        "",
    ])


# ════════════════════════════════════════════════════════════════════════
#  模块三：系统二（Claude Simulation 版）
# ════════════════════════════════════════════════════════════════════════
def sys2_section() -> str:
    conn = _db(SYS2_DB)
    if not conn:
        return "### 🤖 系统二：Claude Simulation 版\n⚠️ 数据库不可用\n"

    total_row = conn.execute("""
        SELECT COUNT(*), MIN(run_at), MAX(sim_hour) FROM hourly_runs
    """).fetchone() or (0, None, 0)
    total_runs, first_run, max_hour = total_row
    days_running = _days_since(first_run) if first_run else "?"

    # 各模型统计：近24h为主，累计仅作参考
    mdata = _hourly_group_stats(conn, "-24 hours")
    tmap = _hourly_group_stats(conn)

    # 最新日汇总（daily_summaries 仅用 anomaly_count / total_runs；价格字段可能为0，不信任）
    lsum = conn.execute("""
        SELECT avg_rec_price_23, avg_rec_price_45, anomaly_count, total_runs
        FROM daily_summaries ORDER BY day DESC LIMIT 1
    """).fetchone()

    # 直接从 hourly_runs 计算各星级推荐价（兼容 hotel_star 列缺失的旧库）
    has_hotel_star = conn.execute(
        "SELECT COUNT(*) FROM pragma_table_info('hourly_runs') WHERE name='hotel_star'"
    ).fetchone()[0] > 0
    if has_hotel_star:
        tier_rows = conn.execute("""
            SELECT hotel_star,
                   AVG(CASE WHEN model_type LIKE 'MARE%' THEN rec_price END) as mare_avg
            FROM hourly_runs
            WHERE run_at >= datetime('now','-24 hours')
            GROUP BY hotel_star
        """).fetchall()
        low_prices  = [r[1] for r in tier_rows if r[0] in (3, 4) and r[1]]
        high_prices = [r[1] for r in tier_rows if r[0] in (5,)      and r[1]]
        p23_direct  = sum(low_prices)  / len(low_prices)  if low_prices  else None
        p45_direct  = sum(high_prices) / len(high_prices) if high_prices else None
    else:
        p23_direct = p45_direct = None

    conn.close()

    def _m(key):
        return mdata.get(key)

    def _t(key):
        return tmap.get(key, (key, 0, None, 0, 0, None))

    # 分市场真实均价
    real_low, real_high = _real_market_avgs()

    m = _m("MARE");     mt = _t("MARE")
    d = _m("DIRECTOR"); dt = _t("DIRECTOR")
    s = _m("SELFACQ");  st = _t("SELFACQ")

    # 优先使用直接计算值；回退到 daily_summaries（如果非零）；最后用整体 MARE avg
    p23_sum = lsum[0] if lsum and lsum[0] else None
    p45_sum = lsum[1] if lsum and lsum[1] else None
    mare_avg_all = m[2] if m else None
    p23 = p23_direct or p23_sum or mare_avg_all
    p45 = p45_direct or p45_sum or mare_avg_all

    # 寻客触发数据
    conn3 = _db(COLLECTOR_DB)
    acq_7d = {}
    if conn3:
        rows = conn3.execute("""
            SELECT action_label, COUNT(*) FROM acquisition_triggers
            WHERE triggered_at >= datetime('now','-7 days')
            GROUP BY action_label ORDER BY COUNT(*) DESC
        """).fetchall()
        acq_7d = {r[0]: r[1] for r in rows}
        conn3.close()
    acq_str = " | ".join([f"{k}×{v}" for k, v in list(acq_7d.items())[:4]]) if acq_7d else "暂无触发"

    return "\n".join([
        "### 🤖 系统二：Claude Simulation 版",
        "",
        "_说明：失败率仅统计 EXCEPTION；异常率包含护栏/压力测试告警，不等于程序崩溃。_",
        "",
        "**① MARE 房价引擎**",
        f"- 近24h：{m[1]:,} 次 | 失败率：{_pct(m[3], m[1]) if m else 'N/A'} | 异常率：{_pct(m[4], m[1]) if m else 'N/A'}",
        f"- 累计：{mt[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(mt[3], mt[1])} | 异常率：{_pct(mt[4], mt[1])}",
        f"- 3-4★市场推荐价：{_mop(p23)} | " + _vs_market(p23, real_low, "3-4★市场"),
        f"- 5★豪华市场推荐价：{_mop(p45)} | " + _vs_market(p45, real_high, "5★豪华市场"),
        f"- 综合收益提升：{f'{m[5]:.1f}%' if m and m[5] else 'N/A'}",
        f"- 模拟进度：第 {(max_hour or 0) + 1} / 504 小时（{((max_hour or 0) + 1) / 504 * 100:.0f}%）",
        "",
        "**② DirectorAI CRM（直销引流）**",
        f"- 近24h：{d[1]:,} 次 | 失败率：{_pct(d[3], d[1]) if d else 'N/A'} | 异常率：{_pct(d[4], d[1]) if d else 'N/A'}",
        f"- 累计：{dt[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(dt[3], dt[1])} | 异常率：{_pct(dt[4], dt[1])}",
        f"- 当前24h推荐均价：{_mop(d[2]) if d else 'N/A'}",
        f"- 收益提升估算：{f'{d[5]:.1f}%' if d and d[5] else 'N/A'}",
        "",
        "**③ SelfACQ 自主寻客**",
        f"- 近24h：{s[1]:,} 次 | 失败率：{_pct(s[3], s[1]) if s else 'N/A'} | 异常率：{_pct(s[4], s[1]) if s else 'N/A'}",
        f"- 累计：{st[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(st[3], st[1])} | 异常率：{_pct(st[4], st[1])}",
        f"- 当前24h直销引导价：{_mop(s[2]) if s else 'N/A'}",
        f"- 寻客触发（近7天）：{acq_str}",
        "",
    ])


# ════════════════════════════════════════════════════════════════════════
#  模块四：系统三（CrewAI 版）
# ════════════════════════════════════════════════════════════════════════
def sys3_section() -> str:
    conn = _db(SYS3_DB)
    if not conn:
        return "### 🤖 系统三：CrewAI 版\n⚠️ 数据库不可用\n"

    total_row = conn.execute("""
        SELECT COUNT(*), MIN(run_at), MAX(sim_hour) FROM hourly_runs
    """).fetchone() or (0, None, 0)
    total_runs, first_run, max_hour = total_row
    days_running = _days_since(first_run) if first_run else "?"

    mdata = _hourly_group_stats(conn, "-24 hours")
    tmap = _hourly_group_stats(conn)

    comp_row = conn.execute("""
        SELECT AVG(crewai_avg_mare), AVG(playwright_avg_mare),
               AVG(mare_diff_pct), AVG(fc_coverage_pct), COUNT(*)
        FROM comparison_log WHERE run_at >= datetime('now','-24 hours')
    """).fetchone()

    conn.close()

    def _m(key):
        return mdata.get(key)

    def _t(key):
        return tmap.get(key, (key, 0, None, 0, 0, None))

    m = _m("MARE");     mt = _t("MARE")
    d = _m("DIRECTOR"); dt = _t("DIRECTOR")
    s = _m("SELFACQ");  st = _t("SELFACQ")

    comp_str = "N/A"
    if comp_row and comp_row[4] and comp_row[4] > 0:
        cav, pav, dpct, cov, cnt = comp_row
        if dpct is not None and abs(dpct) < 9999:
            comp_str = f"偏差 {dpct:.2f}% | FC覆盖 {cov:.1f}% | {cnt} 次对比"
        else:
            comp_str = f"对比数据异常（{cnt} 次记录，数值待检查）"

    conn5 = _db(COLLECTOR_DB)
    acq_7d = {}
    if conn5:
        rows = conn5.execute("""
            SELECT action_label, COUNT(*) FROM acquisition_triggers
            WHERE triggered_at >= datetime('now','-7 days')
            GROUP BY action_label ORDER BY COUNT(*) DESC
        """).fetchall()
        acq_7d = {r[0]: r[1] for r in rows}
        conn5.close()
    acq_str = " | ".join([f"{k}×{v}" for k, v in list(acq_7d.items())[:4]]) if acq_7d else "暂无触发"

    # 分市场真实均价
    real_low, real_high = _real_market_avgs()
    # CrewAI 的 hourly_runs 目前无 hotel_star 分组（统一用 MARE_ALL_FC）
    # 若将来引入星级分组，可替换为 MARE_3_STAR_FC / MARE_45_STAR_FC
    m_low  = mdata.get("MARE_3_STAR_FC") or m   # 回退到 MARE_ALL_FC
    m_high = mdata.get("MARE_ALL_FC") or m

    return "\n".join([
        "### 🤖 系统三：CrewAI 版",
        "",
        "_说明：失败率仅统计 EXCEPTION；异常率包含护栏/压力测试告警，不等于程序崩溃。_",
        "",
        "**① MARE 房价引擎（FC整合版）**",
        f"- 近24h：{m[1]:,} 次 | 失败率：{_pct(m[3], m[1]) if m else 'N/A'} | 异常率：{_pct(m[4], m[1]) if m else 'N/A'}",
        f"- 累计：{mt[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(mt[3], mt[1])} | 异常率：{_pct(mt[4], mt[1])}",
        f"- 3-4★市场推荐价：{_mop(m_low[2]) if m_low else 'N/A'} | "
            + _vs_market(m_low[2] if m_low else None, real_low, "3-4★市场"),
        f"- 5★豪华市场推荐价：{_mop(m_high[2]) if m_high else 'N/A'} | "
            + _vs_market(m_high[2] if m_high else None, real_high, "5★豪华市场"),
        f"- 收益提升：{f'{m[5]:.1f}%' if m and m[5] else 'N/A'} | CrewAI验证：{comp_str}",
        f"- 模拟进度：第 {(max_hour or 0) + 1} / 504 小时",
        "",
        "**② DirectorAI CRM（直销引流）**",
        f"- 近24h：{d[1]:,} 次 | 失败率：{_pct(d[3], d[1]) if d else 'N/A'} | 异常率：{_pct(d[4], d[1]) if d else 'N/A'}",
        f"- 累计：{dt[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(dt[3], dt[1])} | 异常率：{_pct(dt[4], dt[1])}",
        f"- 当前24h推荐均价：{_mop(d[2]) if d else 'N/A'}",
        f"- 收益提升估算：{f'{d[5]:.1f}%' if d and d[5] else 'N/A'}",
        "",
        "**③ SelfACQ 自主寻客**",
        f"- 近24h：{s[1]:,} 次 | 失败率：{_pct(s[3], s[1]) if s else 'N/A'} | 异常率：{_pct(s[4], s[1]) if s else 'N/A'}",
        f"- 累计：{st[1]:,} 次 | 运行天数：{days_running} 天 | 失败率：{_pct(st[3], st[1])} | 异常率：{_pct(st[4], st[1])}",
        f"- 当前24h直销引导价：{_mop(s[2]) if s else 'N/A'}",
        f"- 寻客触发（近7天）：{acq_str}",
        "",
    ])


# ════════════════════════════════════════════════════════════════════════
#  主函数：组装并推送
# ════════════════════════════════════════════════════════════════════════
def build_report() -> str:
    header = (
        f"## 🏨 InsightBridge AI 模型日报\n"
        f"**{NOW} | 每日09:00自动推送**\n"
        f"三套系统 · 9个AI模型 · 76家澳门酒店\n"
        "\n---\n\n"
    )
    footer = (
        "\n---\n"
        f"*InsightBridge Global · 模型测试系统 v2.0 · {TODAY}*"
    )
    return (
        header
        + _headline_section()  + "\n---\n\n"
        + collector_section() + "\n---\n\n"
        + sys1_section()       + "\n---\n\n"
        + sys2_section()       + "\n---\n\n"
        + sys3_section()
        + footer
    )


if __name__ == "__main__":
    import traceback
    try:
        report = build_report()
        saved_path = _save_report(report)
        print(report)
        print(f"\n--- 已保存日报 ---\n{saved_path}")
        print("\n--- 正在推送 Telegram ---")
        brief_ok = _telegram_send_text(_build_telegram_brief(saved_path))
        doc_ok = _telegram_send_document(saved_path)
        if brief_ok and doc_ok:
            print("✅ Telegram 文本与附件推送完成")
        elif brief_ok or doc_ok:
            print("⚠️ Telegram 仅部分推送成功")
        else:
            print("⚠️ Telegram 推送未完成")
    except Exception as e:
        traceback.print_exc()
        print(f"❌ 报告生成失败: {e}")
        sys.exit(1)
