"""
compare_report.py — CrewAI版 vs Playwright基线 对比报告
========================================================
运行方法：
    python3 compare_report.py
"""

import sqlite3
from pathlib import Path
from datetime import datetime

BASE    = Path(__file__).parent
FC_DB   = BASE / "crewai_results.db"
PW_DB   = BASE.parent / "simulation_test" / "results.db"


def _conn(path):
    if not path.exists():
        return None
    c = sqlite3.connect(path)
    return c


def main():
    fc  = _conn(FC_DB)
    pw  = _conn(PW_DB)

    print(f"\n{'='*72}")
    print(f"  双轨对比报告 — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*72}")

    # ── Firecrawl数据采集成功率 ────────────────────────────────────
    if fc:
        rows = fc.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN border_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END) as border_ok,
                SUM(CASE WHEN zhuhai_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END) as zhuhai_ok,
                SUM(CASE WHEN ota_pace_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END) as pace_ok,
                SUM(CASE WHEN agoda_source NOT IN ('unavailable','simulated','failed') THEN 1 ELSE 0 END) as agoda_ok
            FROM firecrawl_signals
        """).fetchone()
        if rows and rows[0]:
            n = rows[0]
            print(f"\n【Firecrawl 数据采集成功率（共{n}小时）】")
            print(f"  border_flow    真实抓取: {rows[1]:3d}/{n} ({rows[1]/n:.0%})")
            print(f"  zhuhai_sat     真实抓取: {rows[2]:3d}/{n} ({rows[2]/n:.0%})")
            print(f"  ota_pace       真实抓取: {rows[3]:3d}/{n} ({rows[3]/n:.0%})")
            print(f"  Agoda价格      真实抓取: {rows[4]:3d}/{n} ({rows[4]/n:.0%})")
            overall = (rows[1]+rows[2]+rows[3]+rows[4]) / (n*4)
            print(f"  ── 新增因子总体覆盖率: {overall:.1%}")

    # ── MARE价格对比 ───────────────────────────────────────────────
    if fc and pw:
        fc_mare = fc.execute(
            "SELECT AVG(rec_price), MIN(rec_price), MAX(rec_price) FROM hourly_runs WHERE model_type='MARE_23_STAR_FC'"
        ).fetchone()
        pw_mare = pw.execute(
            "SELECT AVG(rec_price), MIN(rec_price), MAX(rec_price) FROM hourly_runs WHERE model_type='MARE_23_STAR'"
        ).fetchone()
        if fc_mare and pw_mare and fc_mare[0] and pw_mare[0]:
            diff = (fc_mare[0] - pw_mare[0]) / pw_mare[0] * 100
            print(f"\n【MARE 2-3星推荐价对比】")
            print(f"  CrewAI+FC版:  均价 MOP {fc_mare[0]:.0f}  (范围 {fc_mare[1]:.0f}-{fc_mare[2]:.0f})")
            print(f"  Playwright版: 均价 MOP {pw_mare[0]:.0f}  (范围 {pw_mare[1]:.0f}-{pw_mare[2]:.0f})")
            print(f"  差异: {diff:+.2f}%  ({'FC版更高' if diff>0 else 'FC版更低'})")
            if abs(diff) < 2:
                print(f"  ✓ 两版本价格高度一致（差异<2%），模型稳健")
            elif abs(diff) < 10:
                print(f"  ⚠ 中等差异，可能因border_flow真实值与模拟值不同")
            else:
                print(f"  ❗ 显著差异，需分析FC数据质量")

    # ── 自主获客胜率对比 ──────────────────────────────────────────
    if fc and pw:
        fc_acq = fc.execute(
            "SELECT output_json FROM hourly_runs WHERE model_type='SELFACQ_FC' LIMIT 1000"
        ).fetchall()
        import json
        fc_wins = sum(1 for r in fc_acq
                      if json.loads(r[0]).get("direct_wins_vs_ota", False))
        fc_total = len(fc_acq)

        pw_acq = pw.execute(
            "SELECT output_json FROM hourly_runs WHERE model_type='SELFACQ_45_STAR' LIMIT 1000"
        ).fetchall()
        pw_wins = sum(1 for r in pw_acq
                      if json.loads(r[0]).get("direct_wins_vs_ota", False))
        pw_total = len(pw_acq)

        if fc_total and pw_total:
            print(f"\n【自主获客 直销胜率对比】")
            print(f"  CrewAI+FC版:  {fc_wins}/{fc_total} ({fc_wins/fc_total:.1%})")
            print(f"  Playwright版: {pw_wins}/{pw_total} ({pw_wins/pw_total:.1%})")

    # ── 每日Firecrawl成功率趋势 ───────────────────────────────────
    if fc:
        print(f"\n【每日Firecrawl数据采集趋势】")
        print(f"  {'Day':>4}  {'border':>8}  {'zhuhai':>8}  {'pace':>8}  {'总覆盖':>8}")
        for day in range(21):
            start_h = day * 24
            end_h = start_h + 24
            r = fc.execute("""
                SELECT
                    SUM(CASE WHEN border_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN zhuhai_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN ota_pace_source NOT IN ('failed','simulated') THEN 1 ELSE 0 END),
                    COUNT(*)
                FROM firecrawl_signals WHERE sim_hour >= ? AND sim_hour < ?
            """, (start_h, end_h)).fetchone()
            if r and r[3]:
                n = r[3]
                cov = ((r[0] or 0)+(r[1] or 0)+(r[2] or 0))/(n*3)
                print(f"  Day{day+1:>2}  {(r[0] or 0):>5}/{n}({(r[0] or 0)/n:.0%})  "
                      f"{(r[1] or 0):>5}/{n}({(r[1] or 0)/n:.0%})  "
                      f"{(r[2] or 0):>5}/{n}({(r[2] or 0)/n:.0%})  "
                      f"{cov:.0%}")
            else:
                break

    print(f"\n{'='*72}")
    print(f"  数据库: {FC_DB}")
    print(f"  基线:   {PW_DB}")
    print(f"{'='*72}\n")

    for c in [fc, pw]:
        if c: c.close()


if __name__ == "__main__":
    main()
