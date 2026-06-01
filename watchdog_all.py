"""
InsightBridge — 三套系统统一守护进程 watchdog_all.py
======================================================
监控 Claude版 / CrewAI版 / ChatGPT版 三套模型是否正常运行。
任一系统进程消失或数据库超过 2 小时未更新，自动重启并推送企业微信通知。

运行（后台持续）：
  nohup python3 watchdog_all.py > watchdog_all.log 2>&1 &

停止：
  kill $(cat /Users/tongyin/Desktop/InsightBridge_模型测试系统/watchdog_all.pid)
"""

from __future__ import annotations
import os, sys, time, signal, sqlite3, subprocess, logging, json
from pathlib import Path
from datetime import datetime

# ── 路径 ──────────────────────────────────────────────────────────────────
DESKTOP     = Path("/Users/tongyin/Desktop/InsightBridge_模型测试系统")
WECOM_PY    = Path("/Users/tongyin/Desktop/Hotel Model Rvisions/wecom_push.py")
WATCHDOG_PID = DESKTOP / "watchdog_all.pid"

# ── 被守护的三套系统定义 ──────────────────────────────────────────────────
SYSTEMS = {
    "Claude版": {
        "script":   "/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test/run_simulation.py",
        "cwd":      "/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test",
        "pid_file": "/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test/simulation.pid",
        "db":       "/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test/results.db",
        "db_table": "hourly_runs",
        "log_out":  "/Users/tongyin/Desktop/Hotel Model Rvisions/simulation_test/simulation_output.log",
        "total_h":  504,
    },
    "CrewAI版": {
        "script":   "/Users/tongyin/Desktop/Hotel Model Rvisions/crewai_simulation/main.py",
        "cwd":      "/Users/tongyin/Desktop/Hotel Model Rvisions/crewai_simulation",
        "pid_file": "/Users/tongyin/Desktop/Hotel Model Rvisions/crewai_simulation/crewai.pid",
        "db":       "/Users/tongyin/Desktop/Hotel Model Rvisions/crewai_simulation/crewai_results.db",
        "db_table": "hourly_runs",
        "log_out":  "/Users/tongyin/Desktop/Hotel Model Rvisions/crewai_simulation/crewai_sim.log",
        "total_h":  504,
    },
    "ChatGPT版": {
        "script":   "/Users/tongyin/hotel_model_staging/run_21d_harness.py",
        "cwd":      "/Users/tongyin/hotel_model_staging",
        "pid_file": None,          # launchd 管理，通过进程名检测
        "db":       None,          # 输出为 jsonl，检测最新文件时间
        "jsonl_dir":"/Users/tongyin/hotel_model_staging/hotel_model_staging_output",
        "log_out":  "/Users/tongyin/hotel_model_staging/logs/launchd.out.log",
        "total_h":  504,
    },
}

CHECK_INTERVAL  = 300   # 每 5 分钟检查一次
STALL_THRESHOLD = 7200  # 2 小时无新数据 → 判定卡死

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.FileHandler(DESKTOP / "watchdog_all.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── WeCom 推送 ─────────────────────────────────────────────────────────────
def _push(msg: str):
    try:
        sys.path.insert(0, str(WECOM_PY.parent))
        from wecom_push import push_markdown
        push_markdown(msg)
    except Exception as e:
        log.warning(f"WeCom 推送失败: {e}")


# ── 进程检查 ───────────────────────────────────────────────────────────────
def _read_pid(cfg: dict) -> int | None:
    if cfg.get("pid_file"):
        try:
            return int(Path(cfg["pid_file"]).read_text().strip())
        except Exception:
            return None
    # ChatGPT版：从进程名扫描
    try:
        import subprocess as sp
        r = sp.run(["pgrep", "-f", "run_21d_harness.py"], capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().split() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None

def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _last_update(cfg: dict) -> float:
    """返回最近数据写入时间（epoch），失败返回 0"""
    if cfg.get("db"):
        try:
            conn = sqlite3.connect(cfg["db"], timeout=5)
            row = conn.execute(
                f"SELECT run_at FROM {cfg['db_table']} ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            pass
        return 0.0
    # ChatGPT版：检查 jsonl 最新文件
    if cfg.get("jsonl_dir"):
        import glob
        files = glob.glob(f"{cfg['jsonl_dir']}/run_*.jsonl")
        if files:
            return max(os.path.getmtime(f) for f in files)
    return 0.0


def _sim_progress(cfg: dict) -> tuple[int, int]:
    if cfg.get("db"):
        try:
            conn = sqlite3.connect(cfg["db"], timeout=5)
            h = conn.execute(
                f"SELECT MAX(sim_hour) FROM {cfg['db_table']}"
            ).fetchone()[0] or 0
            conn.close()
            return h, cfg["total_h"]
        except Exception:
            pass
    return 0, cfg["total_h"]


# ── 重启单个系统 ──────────────────────────────────────────────────────────
def _restart(name: str, cfg: dict, reason: str):
    log.warning(f"[{name}] 重启原因: {reason}")

    old_pid = _read_pid(cfg)
    if old_pid and _process_alive(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(3)
        except Exception:
            pass

    if name == "ChatGPT版":
        # ChatGPT版由 launchd + run_harness.sh 管理，直接调用 shell
        proc = subprocess.Popen(
            ["/bin/bash", "/Users/tongyin/hotel_model_staging/run_harness.sh"],
            start_new_session=True,
        )
        new_pid = proc.pid
    else:
        log_out = open(cfg["log_out"], "a")
        proc = subprocess.Popen(
            [sys.executable, cfg["script"]],
            cwd=cfg["cwd"],
            stdout=log_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        new_pid = proc.pid
        if cfg.get("pid_file"):
            Path(cfg["pid_file"]).write_text(str(new_pid))

    log.info(f"[{name}] 已重启，新 PID={new_pid}")

    done, total = _sim_progress(cfg)
    _push(
        f"## ⚠️ [{name}] 已自动重启\n"
        f"**{datetime.now():%Y-%m-%d %H:%M}**\n\n"
        f"- **原因：** {reason}\n"
        f"- **新 PID：** {new_pid}\n"
        f"- **当前进度：** H{done}/{total}（{done/total*100:.0f}%）"
    )


# ── 主循环 ────────────────────────────────────────────────────────────────
def main():
    WATCHDOG_PID.write_text(str(os.getpid()))
    log.info(f"=== 三系统守护进程启动，PID={os.getpid()} ===")
    _push(
        f"## 🛡️ InsightBridge 三系统守护进程已启动\n"
        f"**{datetime.now():%Y-%m-%d %H:%M}**\n\n"
        f"监控：Claude版 / CrewAI版 / ChatGPT版\n"
        f"每 {CHECK_INTERVAL//60} 分钟检查 · 卡死 {STALL_THRESHOLD//3600}h 自动重启"
    )

    fail_counts = {name: 0 for name in SYSTEMS}

    while True:
        time.sleep(CHECK_INTERVAL)
        now = time.time()
        status_lines = []

        for name, cfg in SYSTEMS.items():
            pid = _read_pid(cfg)
            alive = pid and _process_alive(pid)

            if not alive:
                fail_counts[name] += 1
                log.warning(f"[{name}] 进程不存在（PID={pid}），连续{fail_counts[name]}次")
                if fail_counts[name] >= 2:
                    _restart(name, cfg, f"进程 PID={pid} 消失")
                    fail_counts[name] = 0
                status_lines.append(f"❌ {name}: 进程消失")
                continue

            fail_counts[name] = 0
            last = _last_update(cfg)
            stall = now - last if last > 0 else 0
            done, total = _sim_progress(cfg)
            pct = done / total * 100 if total else 0

            log.info(
                f"[{name}] PID={pid} ✓ | "
                f"最后更新 {stall/60:.0f}m 前 | "
                f"进度 H{done}/{total} ({pct:.0f}%)"
            )

            if last > 0 and stall > STALL_THRESHOLD:
                _restart(name, cfg, f"{stall/3600:.1f}h 未写入数据（疑似卡死）")

            status_lines.append(
                f"✅ {name}: H{done}/{total} ({pct:.0f}%) | "
                f"最后更新 {stall/60:.0f}m前"
            )


if __name__ == "__main__":
    main()
