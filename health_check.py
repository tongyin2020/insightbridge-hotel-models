#!/usr/bin/env python3
"""
InsightBridge 九大模型 — 自动健康检查 + 自愈脚本
================================================
每天 06:00 / 18:00 由 launchd 触发。

检查项目：
  1. System 1 模拟进程是否运行（run_simulation.py）
  2. System 2 harness 是否运行（run_21d_harness.py）
  3. 守护进程 watchdog_all.py 是否运行
  4. results.db 是否在最近 2 小时内有写入
  5. hotel_real_data.db 是否在最近 4 小时内有更新
  6. nohup.out 是否有新的 CRITICAL 错误

自愈策略：
  ✅ 可自动修复：进程意外死亡 → 自动重启
  ✅ 可自动修复：数据库超时未更新 → 重启进程
  ✅ 可自动修复：Shifter 缓存过期 → 清除缓存强制刷新
  ⚠️ 需人工确认：代码导入错误 / DB 损坏 / 未知异常
                → 写入 health_issue.flag，停止自动干预，等待人工处理

日志：/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/health_check.log
问题标记：/Users/tongyin/Desktop/InsightBridge_九大模型_v2026/health_issue.flag
"""

from __future__ import annotations
import os, sys, time, subprocess, sqlite3, logging, json, signal
from pathlib import Path
from datetime import datetime, timedelta

# ── 路径常量 ─────────────────────────────────────────────────────────────────
BASE        = Path(__file__).resolve().parent
SIM1_DIR    = BASE / "system2_claude_simulation"
SIM1_SCRIPT = SIM1_DIR / "run_simulation.py"
SIM1_PID    = SIM1_DIR / "simulation.pid"
SIM1_DB     = SIM1_DIR / "results.db"
SIM1_LOG    = SIM1_DIR / "simulation_output.log"

SIM2_SCRIPT = BASE / "system1_chatgpt_harness/run_21d_harness.py"
REAL_DB     = BASE / "hotel_collector/hotel_real_data.db"
WATCHDOG_PY = BASE / "watchdog_all.py"
WATCHDOG_PID= BASE / "watchdog_all.pid"
SHIFTER_CACHE = SIM1_DIR / "data/shifter_market_cache.json"

HEALTH_LOG  = BASE / "health_check.log"
ISSUE_FLAG  = BASE / "health_issue.flag"

PYTHON      = "/opt/anaconda3/bin/python3"

# ── 日志配置 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=str(HEALTH_LOG),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("health")

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except Exception:
        return None


def db_last_write_minutes(db_path: Path) -> float | None:
    """返回数据库最后写入到现在的分钟数，失败返回 None。"""
    if not db_path.exists():
        return None
    try:
        mtime = db_path.stat().st_mtime
        return (time.time() - mtime) / 60
    except Exception:
        return None


def db_last_row_minutes(db_path: Path, table: str = "hourly_runs") -> float | None:
    """返回数据库最新一行写入到现在的分钟数。"""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(f"SELECT MAX(run_at) FROM {table}").fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        last = datetime.fromisoformat(row[0])
        return (datetime.now() - last).total_seconds() / 60
    except Exception:
        return None


def restart_sim1() -> bool:
    """重启 System 1 模拟进程。"""
    try:
        proc = subprocess.Popen(
            [PYTHON, str(SIM1_SCRIPT)],
            cwd=str(SIM1_DIR),
            stdout=open(SIM1_LOG, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        SIM1_PID.write_text(str(proc.pid))
        log.info(f"System 1 已重启，新 PID={proc.pid}")
        return True
    except Exception as e:
        log.error(f"System 1 重启失败: {e}")
        return False


def restart_watchdog() -> bool:
    """重启守护进程。"""
    try:
        proc = subprocess.Popen(
            [PYTHON, str(WATCHDOG_PY)],
            cwd=str(BASE),
            stdout=open(BASE / "watchdog_all.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        WATCHDOG_PID.write_text(str(proc.pid))
        log.info(f"watchdog_all.py 已重启，新 PID={proc.pid}")
        return True
    except Exception as e:
        log.error(f"watchdog 重启失败: {e}")
        return False


def clear_shifter_cache():
    """清除 Shifter 市场价缓存，下次运行将重新抓取。"""
    if SHIFTER_CACHE.exists():
        SHIFTER_CACHE.unlink()
        log.info("Shifter 缓存已清除，下次运行将重新抓取市场价")


def flag_issue(summary: str, detail: str):
    """写入问题标记文件，通知人工介入。"""
    content = {
        "time":    _ts(),
        "summary": summary,
        "detail":  detail,
        "action":  "需要人工确认，自动健康检查已暂停自愈操作",
    }
    ISSUE_FLAG.write_text(
        json.dumps(content, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.warning(f"⚠️ 已写入问题标记: {summary}")


def check_nohup_errors() -> list[str]:
    """检查 nohup.out 最近 200 行是否有 CRITICAL 错误。"""
    if not SIM1_LOG.exists():
        return []
    try:
        lines = SIM1_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
        return [l for l in lines if "CRITICAL" in l or "Traceback" in l or "ImportError" in l]
    except Exception:
        return []


# ── 主检查逻辑 ────────────────────────────────────────────────────────────────
def run_health_check():
    log.info("=" * 60)
    log.info(f"健康检查开始 {_ts()}")
    issues_need_human = []
    auto_fixed = []

    # ── 检查 0：如果上次已写入 flag，先看是否被人工处理了 ─────────────────────
    if ISSUE_FLAG.exists():
        log.warning("发现上次遗留的问题标记文件（health_issue.flag），跳过自愈，等待人工处理")
        log.warning(f"标记内容: {ISSUE_FLAG.read_text()}")
        return

    # ── 检查 1：System 1 进程 ─────────────────────────────────────────────────
    pid1 = read_pid(SIM1_PID)
    if pid1 and pid_alive(pid1):
        log.info(f"✅ System 1 运行正常 (PID={pid1})")
    else:
        log.warning(f"⚠️ System 1 进程不存在 (pid_file={pid1})，尝试重启...")
        # 先检查 nohup 是否有严重错误
        errs = check_nohup_errors()
        if errs:
            flag_issue(
                "System 1 崩溃且日志含严重错误",
                f"最近错误: {errs[-3:]}",
            )
            issues_need_human.append("System 1 崩溃（含代码错误）")
        else:
            if restart_sim1():
                auto_fixed.append("System 1 进程已重启")
            else:
                flag_issue("System 1 重启失败", "请检查 SIM1_DIR 路径和 Python 环境")
                issues_need_human.append("System 1 重启失败")

    # ── 检查 2：System 2 harness（launchd 管理，不主动重启）────────────────────
    s2_running = bool(subprocess.run(
        ["pgrep", "-f", "run_21d_harness.py"],
        capture_output=True,
    ).stdout.strip())
    if s2_running:
        log.info("✅ System 2 harness 运行正常")
    else:
        log.warning("⚠️ System 2 harness 未运行，尝试通过 launchctl 重载...")
        subprocess.run(
            ["launchctl", "kickstart", "-k", "gui/501/com.tongyin.hotel-model-staging"],
            capture_output=True,
        )
        time.sleep(5)
        s2_now = bool(subprocess.run(
            ["pgrep", "-f", "run_21d_harness.py"],
            capture_output=True,
        ).stdout.strip())
        if s2_now:
            auto_fixed.append("System 2 harness 已通过 launchctl 重载")
            log.info("System 2 harness 重载成功")
        else:
            log.warning("System 2 launchctl 重载失败（可能为计划外停止，不标记为人工介入）")

    # ── 检查 3：守护进程 watchdog_all.py ────────────────────────────────────────
    wd_pid = read_pid(WATCHDOG_PID)
    if wd_pid and pid_alive(wd_pid):
        log.info(f"✅ watchdog_all.py 运行正常 (PID={wd_pid})")
    else:
        log.warning("⚠️ watchdog_all.py 未运行，自动重启...")
        if restart_watchdog():
            auto_fixed.append("watchdog_all.py 已重启")

    # ── 检查 4：results.db 更新时效 ─────────────────────────────────────────────
    db_age = db_last_row_minutes(SIM1_DB)
    if db_age is None:
        flag_issue("results.db 无法读取", "数据库可能损坏或路径错误")
        issues_need_human.append("results.db 无法读取")
    elif db_age > 130:   # 超过 2h10min（模拟每小时一次，允许10分钟缓冲）
        log.warning(f"⚠️ results.db 最新记录 {db_age:.0f} 分钟前，数据库已停止更新")
        pid1_now = read_pid(SIM1_PID)
        if pid1_now and pid_alive(pid1_now):
            # 进程在但 DB 没更新 — 可能卡住了
            log.warning("进程存在但 DB 未更新，强制重启 System 1...")
            os.kill(pid1_now, signal.SIGTERM)
            time.sleep(3)
            if restart_sim1():
                auto_fixed.append(f"System 1 因 DB 停更（{db_age:.0f}min）被强制重启")
    else:
        log.info(f"✅ results.db 最新记录 {db_age:.1f} 分钟前（正常）")

    # ── 检查 5：hotel_real_data.db（数据采集）更新时效 ────────────────────────
    real_db_age = db_last_write_minutes(REAL_DB)
    if real_db_age is None:
        log.warning("⚠️ hotel_real_data.db 不存在，数据采集可能从未运行")
    elif real_db_age > 240:  # 超过 4 小时
        log.warning(f"⚠️ hotel_real_data.db 已 {real_db_age:.0f} 分钟未更新，清除 Shifter 缓存...")
        clear_shifter_cache()
        auto_fixed.append(f"Shifter 缓存已清除（real_db 已 {real_db_age:.0f}min 未更新）")
    else:
        log.info(f"✅ hotel_real_data.db 最新写入 {real_db_age:.0f} 分钟前（正常）")

    # ── 汇总报告 ─────────────────────────────────────────────────────────────
    log.info("-" * 40)
    if auto_fixed:
        log.info(f"🔧 自动修复 {len(auto_fixed)} 项: {' | '.join(auto_fixed)}")
    if issues_need_human:
        log.warning(f"🚨 需人工处理 {len(issues_need_human)} 项: {' | '.join(issues_need_human)}")
    else:
        log.info("✅ 所有检查通过，无需人工介入")
    log.info(f"健康检查结束 {_ts()}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_health_check()
