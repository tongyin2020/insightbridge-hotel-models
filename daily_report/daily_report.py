#!/usr/bin/env python3
"""
InsightBridge AI 模型每日报告
============================
每天 09:00 由 launchd 触发，汇总三套系统、9个AI模型运行状态与KPI，
通过企业微信机器人推送一次。

手动测试：
  python3 /Users/tongyin/Desktop/InsightBridge_九大模型_v2026/daily_report.py
"""

from __future__ import annotations
import sys, json, sqlite3, subprocess
from pathlib import Path
from datetime import datetime

# ── 路径配置 ──────────────────────────────────────────────────────────────
WECOM_PY     = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/Hotel_Model_Rvisions/wecom_push.py")
SYS2_DB      = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/system2_claude_simulation/results.db")
SYS3_DB      = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/system3_crewai/crewai_results.db")
SYS1_OUTDIR  = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_model_staging_output")
COLLECTOR_DB = Path("/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/hotel_collector/hotel_real_data.db")

TODAY = datetime.now().strftime("%Y-%m-%d")
NOW   = datetime.now().strftime("%Y-%m-%d %H:%M")

# ── 工具函数 ──────────────────────────────────────────────────────────────
def push(msg: str):
    sys.path.insert(0, str(WECOM_PY.parent))
    from wecom_push import push_markdown
    push_markdown(msg)

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

def _days_since(ts_str: str) -> str:
    try:
        fd = datetime.fromisoformat(ts_str[:19])
        return str((datetime.now() - fd).days + 1)
    except Exception:
        return "?"

def _real_market_avgs() -> tuple:
    """
    返回两个独立市场的真实官网BAR均价（近7天，source_ok=1）：
      low_avg  = 2-3-4★市场（tiers: 3_star, 4_star）
      high_avg = 5★豪华市场（tiers: 5_star, 5_deluxe）
    """
    conn = _db(COLLECTOR_DB)
    low_avg = high_avg = None
    if conn:
        rows = conn.execute("""
            SELECT tier, AVG(official_bar), COUNT(*)
            FROM price_snapshots
            WHERE source_ok=1 AND official_bar > 200
              AND snapshot_time >= datetime('now','-7 days')
            GROUP BY tier
        """).fetchall()
        rm = {r[0]: (r[1], r[2]) for r in rows}
        # 2-3-4★市场：加权平均 3_star + 4_star
        low_vals, low_cnts = [], []
        for t in ("3_star", "4_star"):
            if t in rm and rm[t][0]:
                low_vals.append(rm[t][0] * rm[t][1])
                low_cnts.append(rm[t][1])
        if low_cnts:
            low_avg = sum(low_vals) / sum(low_cnts)
        # 5★豪华市场：加权平均 5_star + 5_deluxe
        high_vals, high_cnts = [], []
        for t in ("5_star", "5_deluxe"):
            if t in rm and rm[t][0]:
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
        f"| 最近采集时间 | {last_snap or 'N/A'} | — |",
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
    # 按市场分组：low=2-3-4★, high=5★豪华
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
                f"- 2-3-4★市场推荐价：{_mop(mare_low_avg)} | "
                + _vs_market(mare_low_avg, real_low, "2-3-4★市场")
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

    # 各模型 24h 统计
    model_rows = conn.execute("""
        SELECT model_type, COUNT(*), AVG(rec_price),
               SUM(CASE WHEN anomaly LIKE 'EXCEPTION%' THEN 1 ELSE 0 END),
               AVG(CASE WHEN exp_lift IS NOT NULL AND exp_lift != '' THEN
                   CAST(REPLACE(REPLACE(exp_lift,'%',''),' ','') AS REAL) ELSE NULL END)
        FROM hourly_runs WHERE run_at >= datetime('now','-24 hours')
        GROUP BY model_type
    """).fetchall()
    mdata = {r[0]: r for r in model_rows}

    # 全量汇总（跨所有变体，MARE_ALL + MARE_23_STAR 合并）
    tot_rows = conn.execute("""
        SELECT
          CASE
            WHEN model_type LIKE 'MARE%'     THEN 'MARE'
            WHEN model_type LIKE 'DIRECTOR%' THEN 'DIRECTOR'
            WHEN model_type LIKE 'SELFACQ%'  THEN 'SELFACQ'
            ELSE model_type
          END as grp,
          COUNT(*),
          SUM(CASE WHEN anomaly LIKE 'EXCEPTION%' THEN 1 ELSE 0 END)
        FROM hourly_runs GROUP BY grp
    """).fetchall()
    tmap = {r[0]: (r[1], r[2]) for r in tot_rows}

    # 最新日汇总
    lsum = conn.execute("""
        SELECT avg_rec_price_23, avg_rec_price_45, anomaly_count, total_runs
        FROM daily_summaries ORDER BY day DESC LIMIT 1
    """).fetchone()

    conn.close()

    def _m(key):
        # 优先 *_ALL 变体，回退到其他包含 key 的
        all_key = f"{key}_ALL"
        for k, v in mdata.items():
            if k == all_key: return v
        for k, v in mdata.items():
            if key in k: return v
        return None

    def _t(key):
        return tmap.get(key, (0, 0))

    # 分市场真实均价
    real_low, real_high = _real_market_avgs()

    m = _m("MARE");     tot, tot_a   = _t("MARE")
    d = _m("DIRECTOR"); tot_d, tot_da = _t("DIRECTOR")
    s = _m("SELFACQ");  tot_s, tot_sa = _t("SELFACQ")

    p23 = lsum[0] if lsum else None
    p45 = lsum[1] if lsum else None

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
        "**① MARE 房价引擎**",
        f"- 总运行次数：{tot:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_a, tot)}",
        f"- 2-3-4★市场推荐价：{_mop(p23)} | " + _vs_market(p23, real_low, "2-3-4★市场"),
        f"- 5★豪华市场推荐价：{_mop(p45)} | " + _vs_market(p45, real_high, "5★豪华市场"),
        f"- 综合收益提升：{f'{m[4]:.1f}%' if m and m[4] else 'N/A'}",
        f"- 模拟进度：第 {(max_hour or 0) + 1} / 504 小时（{((max_hour or 0) + 1) / 504 * 100:.0f}%）",
        "",
        "**② DirectorAI CRM（直销引流）**",
        f"- 总运行次数：{tot_d:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_da, tot_d)}",
        f"- 当前24h推荐均价：{_mop(d[2]) if d else 'N/A'}",
        f"- 收益提升估算：{f'{d[4]:.1f}%' if d and d[4] else 'N/A'}",
        "",
        "**③ SelfACQ 自主寻客**",
        f"- 总运行次数：{tot_s:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_sa, tot_s)}",
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

    model_rows = conn.execute("""
        SELECT model_type, COUNT(*), AVG(rec_price),
               SUM(CASE WHEN anomaly LIKE 'EXCEPTION%' THEN 1 ELSE 0 END),
               AVG(CASE WHEN exp_lift IS NOT NULL AND exp_lift != '' THEN
                   CAST(REPLACE(REPLACE(exp_lift,'%',''),' ','') AS REAL) ELSE NULL END)
        FROM hourly_runs WHERE run_at >= datetime('now','-24 hours')
        GROUP BY model_type
    """).fetchall()
    mdata = {r[0]: r for r in model_rows}

    tot_rows = conn.execute("""
        SELECT
          CASE
            WHEN model_type LIKE 'MARE%'     THEN 'MARE'
            WHEN model_type LIKE 'DIRECTOR%' THEN 'DIRECTOR'
            WHEN model_type LIKE 'SELFACQ%'  THEN 'SELFACQ'
            ELSE model_type
          END as grp,
          COUNT(*),
          SUM(CASE WHEN anomaly LIKE 'EXCEPTION%' THEN 1 ELSE 0 END)
        FROM hourly_runs GROUP BY grp
    """).fetchall()
    tmap = {r[0]: (r[1], r[2]) for r in tot_rows}

    comp_row = conn.execute("""
        SELECT AVG(crewai_avg_mare), AVG(playwright_avg_mare),
               AVG(mare_diff_pct), AVG(fc_coverage_pct), COUNT(*)
        FROM comparison_log WHERE run_at >= datetime('now','-24 hours')
    """).fetchone()

    conn.close()

    def _m(key):
        # 优先 *_ALL_FC 变体，回退到其他
        all_key = f"{key}_ALL_FC"
        for k, v in mdata.items():
            if k == all_key: return v
        all_key2 = f"{key}_ALL"
        for k, v in mdata.items():
            if k == all_key2: return v
        for k, v in mdata.items():
            if key in k: return v
        return None

    def _t(key):
        return tmap.get(key, (0, 0))

    m = _m("MARE");     tot, tot_a   = _t("MARE")
    d = _m("DIRECTOR"); tot_d, tot_da = _t("DIRECTOR")
    s = _m("SELFACQ");  tot_s, tot_sa = _t("SELFACQ")

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
    # CrewAI 的 hourly_runs 目前无 hotel_star 分组，用 MARE_23_STAR_FC vs MARE_ALL_FC 区分
    m_low  = mdata.get("MARE_23_STAR_FC")
    m_high = mdata.get("MARE_ALL_FC") or m

    return "\n".join([
        "### 🤖 系统三：CrewAI 版",
        "",
        "**① MARE 房价引擎（FC整合版）**",
        f"- 总运行次数：{tot:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_a, tot)}",
        f"- 2-3-4★市场推荐价：{_mop(m_low[2]) if m_low else 'N/A'} | "
            + _vs_market(m_low[2] if m_low else None, real_low, "2-3-4★市场"),
        f"- 5★豪华市场推荐价：{_mop(m_high[2]) if m_high else 'N/A'} | "
            + _vs_market(m_high[2] if m_high else None, real_high, "5★豪华市场"),
        f"- 收益提升：{f'{m[4]:.1f}%' if m and m[4] else 'N/A'} | CrewAI验证：{comp_str}",
        f"- 模拟进度：第 {(max_hour or 0) + 1} / 504 小时",
        "",
        "**② DirectorAI CRM（直销引流）**",
        f"- 总运行次数：{tot_d:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_da, tot_d)}",
        f"- 当前24h推荐均价：{_mop(d[2]) if d else 'N/A'}",
        f"- 收益提升估算：{f'{d[4]:.1f}%' if d and d[4] else 'N/A'}",
        "",
        "**③ SelfACQ 自主寻客**",
        f"- 总运行次数：{tot_s:,} | 运行天数：{days_running} 天 | 失败率：{_pct(tot_sa, tot_s)}",
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
        print(report)
        print("\n--- 正在推送企业微信 ---")
        push(report)
        print("✅ 推送完成")
    except Exception as e:
        traceback.print_exc()
        print(f"❌ 报告生成失败: {e}")
        sys.exit(1)
