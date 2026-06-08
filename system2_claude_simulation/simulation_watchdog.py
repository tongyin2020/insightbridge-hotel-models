"""
InsightBridge — 模拟系统守护进程
===================================
监控 run_simulation.py 是否正在运行。
若进程消失或数据库停止更新（超过 2 小时），自动重启。

运行方式（后台持续运行）：
  nohup python3 simulation_watchdog.py > watchdog.log 2>&1 &

停止方式：
  kill $(cat watchdog.pid)
"""

import os, sys, time, signal, sqlite3, subprocess, logging
from pathlib import Path
from datetime import datetime

# ── 路径配置 ───────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
SIM_SCRIPT   = BASE_DIR / "run_simulation.py"
PID_FILE     = BASE_DIR / "simulation.pid"
WATCHDOG_PID = BASE_DIR / "watchdog.pid"
DB_PATH      = BASE_DIR / "results.db"
LOG_PATH     = BASE_DIR / "watchdog.log"

# ── 参数 ───────────────────────────────────────────────────────────────────
CHECK_INTERVAL  = 300    # 每 5 分钟检查一次
STALL_THRESHOLD = 7200   # 数据库超过 2 小时未更新 → 判定为卡死

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# ── 旧通知接口（已停用，仅保留兼容入口）──────────────────────────────────────
def _push(msg: str):
    return None


# ── 进程检查 ───────────────────────────────────────────────────────────────
def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None

def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

def _db_last_update() -> float:
    """返回数据库最后一条记录的时间戳（epoch），失败返回 0"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        row  = conn.execute(
            "SELECT run_at FROM hourly_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
    except Exception:
        pass
    return 0.0

def _sim_progress() -> tuple[int, int]:
    """返回 (completed_hours, total_hours)"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        h = conn.execute(
            "SELECT MAX(sim_hour) FROM hourly_runs"
        ).fetchone()[0] or 0
        conn.close()
        return h, 504
    except Exception:
        return 0, 504


# ── 重启模拟 ───────────────────────────────────────────────────────────────
def _restart_simulation(reason: str):
    log.warning(f"重启原因: {reason}")

    # 先杀掉残留进程
    old_pid = _read_pid()
    if old_pid and _process_alive(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(3)
        except Exception:
            pass

    # 后台启动新进程
    proc = subprocess.Popen(
        [sys.executable, str(SIM_SCRIPT)],
        cwd=str(BASE_DIR),
        stdout=open(BASE_DIR / "sim_stdout.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    new_pid = proc.pid
    PID_FILE.write_text(str(new_pid))
    log.info(f"模拟已重启，新 PID={new_pid}")

    done, total = _sim_progress()
    _push(
        f"## ⚠️ 模拟系统已自动重启\n"
        f"**{datetime.now():%Y-%m-%d %H:%M}**\n\n"
        f"- **原因：** {reason}\n"
        f"- **旧 PID：** {old_pid}\n"
        f"- **新 PID：** {new_pid}\n"
        f"- **当前进度：** H{done}/504（{done/504*100:.0f}%）\n\n"
        f"> 模型将从头继续运行，请注意数据库可能累积重复记录"
    )


# ── 主循环 ─────────────────────────────────────────────────────────────────
def main():
    # 写入 watchdog 自身 PID
    WATCHDOG_PID.write_text(str(os.getpid()))
    log.info(f"Watchdog 启动，PID={os.getpid()}，检查间隔={CHECK_INTERVAL}s")

    _push(
        f"## 🛡️ 模拟守护进程已启动\n"
        f"**{datetime.now():%Y-%m-%d %H:%M}**\n\n"
        f"每 {CHECK_INTERVAL//60} 分钟检查一次，卡死超过 {STALL_THRESHOLD//3600} 小时自动重启"
    )

    consecutive_failures = 0

    while True:
        time.sleep(CHECK_INTERVAL)

        pid = _read_pid()
        now = time.time()

        # ① 检查进程是否存活
        if pid is None or not _process_alive(pid):
            consecutive_failures += 1
            log.warning(f"模拟进程不存在（PID={pid}），连续失败 {consecutive_failures} 次")
            if consecutive_failures >= 2:   # 连续2次确认才重启（避免误报）
                _restart_simulation(f"进程 PID={pid} 不存在")
                consecutive_failures = 0
            continue

        consecutive_failures = 0   # 进程存活，重置计数

        # ② 检查数据库是否卡死
        last_update = _db_last_update()
        stall_secs  = now - last_update if last_update > 0 else 0

        done, total = _sim_progress()
        log.info(
            f"进程 PID={pid} ✓ | DB 最后更新 {stall_secs/60:.0f}m 前 | "
            f"进度 H{done}/{total} ({done/total*100:.0f}%)"
        )

        if last_update > 0 and stall_secs > STALL_THRESHOLD:
            _restart_simulation(
                f"数据库 {stall_secs/3600:.1f} 小时未更新（疑似卡死）"
            )

        # ③ 模拟完成，停止守护
        if done >= total:
            log.info("模拟已完成 504 小时，Watchdog 退出")
            _push(
                f"## ✅ 模拟守护进程退出\n"
                f"模拟已完成全部 504 小时，Watchdog 正常退出。"
            )
            WATCHDOG_PID.unlink(missing_ok=True)
            break


if __name__ == "__main__":
    main()
