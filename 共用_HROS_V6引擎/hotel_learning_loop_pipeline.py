"""
HotelLearningLoop 周度校准流水线
=====================================
从模拟DB提取周度数据 → 构建WeeklyHotelRecord → 更新各酒店画像（指数平滑）
→ 将新baseline_adr写回JSON存储供下一周定价引擎使用。

调用时机：
  - System 1/2/3 每运行完 168 个模拟小时（= 1 模拟周）后调用一次
  - 也可手动调用 calibrate_all_hotels() 批量触发

数据路径（自动适配三个系统）：
  System 2 (Claude Simulation) : run_simulation.py  → results.db       (table: results, col: output_json)
  System 3 (CrewAI)            : main.py            → crewai_results.db (table: results, col: output_json)
  System 1 (GPT Harness)       : run_21d_harness.py → staging DB or CSV (支持CSV fallback)
"""

import json
import os          # P0 FIX: use os.replace() for cross-platform atomic file swap
import sqlite3
import csv
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional

# --------------------------------------------------------------------------- #
#  路径与常量
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve().parent          # 共用_HROS_V6引擎/
_PROFILE_STORE = _HERE / "hotel_profiles_v6.json"  # 酒店画像持久化存储

# 导入 V6 模块（_HERE 应已在 sys.path 中，但防护一下）
import sys as _sys
if str(_HERE) not in _sys.path:
    _sys.path.insert(0, str(_HERE))

from hros_v6.hotel_learning_loop import HotelLearningLoop
from hros_v6.schemas_v6 import WeeklyHotelRecord

logger = logging.getLogger("HotelLearningLoopPipeline")

# --------------------------------------------------------------------------- #
#  画像存储
# --------------------------------------------------------------------------- #

def load_profiles() -> dict:
    """从 JSON 文件加载所有酒店画像（thread-safe 读取）。"""
    if _PROFILE_STORE.exists():
        try:
            return json.loads(_PROFILE_STORE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("hotel_profiles_v6.json 损坏，从空画像重建")
    return {}


def save_profiles(profiles: dict) -> None:
    """原子写入：先写临时文件再替换，避免写到一半崩溃导致文件损坏。
    P0 FIX (Gemini): 使用 os.replace() 而非 Path.replace()，
    确保 Windows 上跨盘符替换不抛 FileExistsError。
    """
    tmp = _PROFILE_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(_PROFILE_STORE))   # atomic on POSIX; best-effort on Windows


# --------------------------------------------------------------------------- #
#  从 SQLite DB 提取周度记录
# --------------------------------------------------------------------------- #

def _parse_output_json(raw) -> Optional[dict]:
    """解析 output_json 字段（支持字符串或已解析字典）。"""
    try:
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return None


def build_records_from_sqlite(
    db_path: str,
    hotel_id: str,
    week_label: str,
    hour_start: int,
    hour_end: int,
) -> List[WeeklyHotelRecord]:
    """
    从 SQLite results 表提取 WeeklyHotelRecord 列表。

    表结构（System 2 / System 3 共用）:
        hotel_id TEXT, hour_index INTEGER, output_json TEXT/JSON
    output_json 字段至少包含:
        recommended_price / mare_price  (ADR代理值)
        predicted_occupancy             (占用率，0-1)
        room_type                       (可选)
        channel                         (可选)
    """
    records: List[WeeklyHotelRecord] = []
    path = Path(db_path)
    if not path.exists():
        logger.warning("DB 不存在: %s", db_path)
        return records

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only, avoids WAL lock contention
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 兼容 System 2 (results) 和 System 3 (results) 表名一致
        # P1 FIX (Gemini): verify hour_index column exists before querying
        cols = {r[1] for r in cur.execute("PRAGMA table_info(results)").fetchall()}
        if "hour_index" not in cols:
            logger.warning("results 表无 hour_index 列，读取全部记录")
            cur.execute("SELECT output_json FROM results WHERE hotel_id = ?", (hotel_id,))
        else:
            cur.execute(
                """
                SELECT output_json
                FROM results
                WHERE hotel_id = ?
                  AND hour_index BETWEEN ? AND ?
                """,
                (hotel_id, hour_start, hour_end),
            )

        for row in cur.fetchall():
            d = _parse_output_json(row["output_json"])
            if not d:
                continue

            price = d.get("recommended_price") or d.get("mare_price") or d.get("price")

            # P0 FIX (Gemini/DeepSeek): use explicit None-check instead of `or` chain
            # to avoid silently dropping valid zero-occupancy records.
            raw_occ = d.get("predicted_occupancy")
            if raw_occ is None:
                raw_occ = d.get("occupancy")
            occ = float(raw_occ) if raw_occ is not None else 0.0

            if not price or float(price) <= 0:
                continue
            # occ == 0 means zero occupancy: skip (no rooms sold → not meaningful for ADR)
            if occ <= 0:
                continue

            # P0 FIX (DeepSeek/Gemini): rooms_sold must be actual room count, not a 0~1 ratio.
            # We use occ as a fractional weight; rooms_available defaults to 1 because the DB
            # stores per-room-type results. Downstream ADR = Σrevenue / Σrooms_sold stays
            # semantically correct as long as all records share the same normalisation.
            # When rooms_available is known, pass it in via output_json["rooms_available"].
            rooms_available = float(d.get("rooms_available") or 1.0)
            rooms_sold_actual = occ * rooms_available   # actual rooms sold this hour

            records.append(
                WeeklyHotelRecord(
                    hotel_id   = hotel_id,
                    date       = week_label,
                    room_type  = d.get("room_type", "Standard"),
                    channel    = d.get("channel", "Direct"),
                    rooms_sold = rooms_sold_actual,        # actual (fractional) rooms
                    adr        = float(price),
                    revenue    = float(price) * rooms_sold_actual,
                    occupancy  = float(occ),
                )
            )
    except sqlite3.Error as e:
        logger.error("SQLite 读取失败 [%s]: %s", db_path, e)
    finally:
        if conn:
            conn.close()   # P1 FIX (Gemini): always close in finally to prevent connection leak

    return records


def build_records_from_csv(
    csv_path: str,
    hotel_id: str,
    week_label: str,
) -> List[WeeklyHotelRecord]:
    """
    System 1 (GPT Harness) CSV fallback。
    期望列: hotel_id, price, occupancy, room_type, channel
    """
    records: List[WeeklyHotelRecord] = []
    path = Path(csv_path)
    if not path.exists():
        return records

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("hotel_id", "").strip() != hotel_id:
                    continue
                price = float(row.get("price") or 0)
                raw_occ = row.get("occupancy")
                occ = float(raw_occ) if raw_occ not in (None, "") else 0.0
                if price <= 0 or occ <= 0:
                    continue
                rooms_avail = float(row.get("rooms_available") or 1.0)
                rooms_sold_actual = occ * rooms_avail
                records.append(
                    WeeklyHotelRecord(
                        hotel_id   = hotel_id,
                        date       = week_label,
                        room_type  = row.get("room_type", "Standard"),
                        channel    = row.get("channel", "Direct"),
                        rooms_sold = rooms_sold_actual,
                        adr        = price,
                        revenue    = price * rooms_sold_actual,
                        occupancy  = occ,
                    )
                )
    except Exception as e:
        logger.error("CSV 读取失败 [%s]: %s", csv_path, e)

    return records


# --------------------------------------------------------------------------- #
#  单酒店周度校准
# --------------------------------------------------------------------------- #

def run_weekly_calibration(
    hotel_id: str,
    db_path: str,
    week_index: int,          # 0-based: week 0 = hours 0-167, week 1 = 168-335 …
    learning_rate: float = 0.25,
    csv_fallback: Optional[str] = None,
) -> dict:
    """
    对单个酒店执行一次周度校准。
    返回更新后的酒店画像 dict（包含 baseline_adr, channel_mix, etc.）。

    指数平滑公式:
        new_baseline_adr = old * (1 - α) + weekly_adr * α
        其中 α = learning_rate = 0.25
    """
    loop = HotelLearningLoop()
    profiles = load_profiles()
    current_profile = profiles.get(hotel_id, {})

    # 1. 构建小时范围
    h_start = week_index * 168
    h_end   = h_start + 167
    week_label = f"sim_week_{week_index + 1:02d}"

    # 2. 提取记录（DB 优先，CSV 兜底）
    records = build_records_from_sqlite(db_path, hotel_id, week_label, h_start, h_end)
    if not records and csv_fallback:
        records = build_records_from_csv(csv_fallback, hotel_id, week_label)

    if not records:
        logger.info("[%s] 第 %d 周无数据，保留旧画像", hotel_id, week_index + 1)
        return current_profile

    # 3. 汇总 + 更新画像
    summary = loop.summarize(records)
    updated = loop.update_hotel_profile(current_profile, summary, learning_rate=learning_rate)

    # 4. 附加元数据
    updated["last_calibration_week"]  = week_label
    updated["last_calibration_ts"]    = datetime.now(timezone.utc).isoformat()
    updated["last_week_records_count"] = len(records)
    updated["last_week_adr"]           = round(summary.get("adr", 0.0), 2)
    updated["hotel_profile_version"]   = "V6"

    # 5. 持久化
    profiles[hotel_id] = updated
    save_profiles(profiles)

    logger.info(
        "[%s] 第 %d 周校准完成 | ADR %.0f→%.0f | 记录数 %d | 校准次数 %d",
        hotel_id, week_index + 1,
        current_profile.get("baseline_adr", 0.0),
        updated.get("baseline_adr", 0.0),
        len(records),
        updated.get("calibration_weeks", 1),
    )
    return updated


# --------------------------------------------------------------------------- #
#  批量校准（全部酒店）
# --------------------------------------------------------------------------- #

def calibrate_all_hotels(
    hotel_ids: List[str],
    db_path: str,
    week_index: int,
    learning_rate: float = 0.25,
    csv_fallback: Optional[str] = None,
) -> dict:
    """
    对所有酒店执行一次周度校准（顺序执行，因为 profiles JSON 是共享的）。
    返回 {hotel_id: updated_profile} 字典。

    推荐在每个模拟周结束时调用，例如：
        if sim_hour % 168 == 0 and sim_hour > 0:
            week_idx = sim_hour // 168 - 1
            calibrate_all_hotels(HOTEL_IDS, DB_PATH, week_idx)
    """
    results = {}
    for hid in hotel_ids:
        results[hid] = run_weekly_calibration(
            hotel_id       = hid,
            db_path        = db_path,
            week_index     = week_index,
            learning_rate  = learning_rate,
            csv_fallback   = csv_fallback,
        )
    return results


def get_baseline_adr(hotel_id: str, fallback: float = 1000.0) -> float:
    """
    在定价引擎中调用此函数获取最新 baseline_adr。
    如果画像不存在或未校准，返回 fallback 值。
    """
    profile = load_profiles().get(hotel_id, {})
    return float(profile.get("baseline_adr") or fallback)


def get_hotel_profile(hotel_id: str) -> dict:
    """返回完整的酒店画像 dict（供定价引擎读取 channel_mix 等）。"""
    return load_profiles().get(hotel_id, {})


# --------------------------------------------------------------------------- #
#  CLI 入口（手动触发用）
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HotelLearningLoop 手动触发")
    parser.add_argument("--db",      required=True, help="SQLite DB 路径")
    parser.add_argument("--hotels",  required=True, help="酒店ID逗号分隔，如 H401,H402")
    parser.add_argument("--week",    type=int, default=0, help="模拟周索引 (0-based)")
    parser.add_argument("--alpha",   type=float, default=0.25, help="指数平滑学习率")
    parser.add_argument("--csv",     default=None, help="CSV fallback 路径 (System 1)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    hotels = [h.strip() for h in args.hotels.split(",")]
    results = calibrate_all_hotels(hotels, args.db, args.week, args.alpha, args.csv)
    print(json.dumps(results, indent=2, ensure_ascii=False))
