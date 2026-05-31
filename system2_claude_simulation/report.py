"""查看14天测试结果报告"""
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "results.db"

if not DB.exists():
    print("数据库不存在，请先运行 run_simulation.py")
    raise SystemExit(1)

conn = sqlite3.connect(DB)
c = conn.cursor()

print("\n" + "="*70)
print("  澳门酒店AI模型 — 14天模拟测试报告")
print("="*70)

# ── 总体统计（修复：CRITICAL 计数覆盖所有模型类型）────────────────────────
total      = c.execute("SELECT COUNT(*) FROM hourly_runs").fetchone()[0]
criticals  = c.execute("SELECT COUNT(*) FROM anomaly_log WHERE detail LIKE 'CRITICAL%'").fetchone()[0]
warns      = c.execute("SELECT COUNT(*) FROM anomaly_log WHERE detail LIKE 'WARN%'").fetchone()[0]
logics     = c.execute("SELECT COUNT(*) FROM anomaly_log WHERE detail LIKE 'LOGIC%'").fetchone()[0]
exceptions = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE anomaly LIKE '%EXCEPTION%'").fetchone()[0]

print(f"\n  总运行次数: {total}  |  CRITICAL: {criticals}  |  WARN: {warns}  |  LOGIC: {logics}  |  崩溃: {exceptions}")
print(f"  异常率: {(criticals+warns+logics+exceptions)/max(total,1)*100:.1f}%")

# ── 异常类型明细（新增）──────────────────────────────────────────────────────
print("\n  【异常类型分布】")
breakdown = c.execute("""
    SELECT detail, COUNT(*) as cnt
    FROM anomaly_log
    GROUP BY detail
    ORDER BY cnt DESC
    LIMIT 12
""").fetchall()
bk_total = sum(r[1] for r in breakdown)
for detail, cnt in breakdown:
    pct = cnt / max(bk_total, 1) * 100
    tag = "🔴" if detail.startswith("CRITICAL") else ("⚠️" if detail.startswith("WARN") else "ℹ️")
    # 注：PSRS/CRM异常来自压力测试场景（PSRS_FAILURE等），属预期行为
    note = " ← 压力场景预期" if "PSRS" in detail or "CRM" in detail or "WhatsApp" in detail else ""
    print(f"  {tag} {cnt:6,}次 ({pct:5.1f}%)  {detail[:65]}{note}")

# ── 每日摘要 ─────────────────────────────────────────────────────────────────
print("\n  【每日摘要】")
print(f"  {'日期':<12} {'2-3星均价':>12} {'直销均价':>12} {'异常数':>8} {'状态':>8}")
print("  " + "-"*56)
rows = c.execute("SELECT date_str, avg_rec_price_23, avg_rec_price_45, anomaly_count, total_runs FROM daily_summaries ORDER BY day").fetchall()
for r in rows:
    status = "✓ OK" if r[3] == 0 else f"⚠ {r[3]}项"
    print(f"  {r[0]:<12} MOP {r[1]:>8.0f}  MOP {r[2]:>8.0f}  {r[3]:>8}  {status:>8}")

# ── 价格区间分析（2-3星）────────────────────────────────────────────────────
print("\n  【2-3星房价分布（MARE模型）】")
for hotel_id in c.execute("SELECT DISTINCT hotel_id FROM hourly_runs WHERE model_type='MARE_23_STAR'").fetchall():
    hid = hotel_id[0]
    stats = c.execute(
        "SELECT MIN(rec_price), MAX(rec_price), AVG(rec_price), COUNT(*) FROM hourly_runs WHERE hotel_id=? AND rec_price IS NOT NULL",
        (hid,)
    ).fetchone()
    if stats and stats[0]:
        print(f"  {hid}: 最低 MOP {stats[0]:.0f} / 均值 MOP {stats[2]:.0f} / 最高 MOP {stats[1]:.0f} ({stats[3]}次)")

# ── 直销胜率（4-5星）────────────────────────────────────────────────────────
print("\n  【4-5星直销模型胜率（DirectorAI）】")
any_45 = False
for hotel_id in c.execute("SELECT DISTINCT hotel_id FROM hourly_runs WHERE model_type='SELFACQ_45_STAR'").fetchall():
    hid = hotel_id[0]
    total_h = c.execute("SELECT COUNT(*) FROM hourly_runs WHERE hotel_id=? AND model_type='SELFACQ_45_STAR'", (hid,)).fetchone()[0]
    wins = c.execute(
        "SELECT COUNT(*) FROM hourly_runs WHERE hotel_id=? AND model_type='SELFACQ_45_STAR' AND output_json LIKE '%\"direct_wins_vs_ota\": true%'",
        (hid,)
    ).fetchone()[0]
    pct = wins / max(total_h, 1) * 100
    flag = "✅" if pct >= 80 else ("⚠️" if pct >= 50 else "❌")
    print(f"  {flag} {hid}: {wins}/{total_h} ({pct:.0f}%) 直销优于OTA")
    any_45 = True
if not any_45:
    print("  (暂无4-5星数据)")

# ── 主要异常记录 ─────────────────────────────────────────────────────────────
print("\n  【主要异常记录（最近10条）】")
anoms = c.execute("SELECT run_at, hotel_id, anomaly_type, detail FROM anomaly_log ORDER BY id DESC LIMIT 10").fetchall()
if not anoms:
    print("  (无异常)")
for a in anoms:
    print(f"  [{a[0]}] {a[1]} | {a[3][:80]}")

# ── 说明 ──────────────────────────────────────────────────────────────────────
print("""
  【报告说明】
  • CRITICAL/WARN 中含 PSRS/CRM/WhatsApp 项：来自压力测试场景
    （PSRS_FAILURE、MIXED_CRISIS 等），属模拟故障注入，为预期行为。
  • LOGIC 项（如有）：表示直销净收益低于OTA——已于2026-05-14修复。
  • 如需清除历史数据重新统计，删除 results.db 后重新运行即可。
""")

print("=" * 70 + "\n")
conn.close()
