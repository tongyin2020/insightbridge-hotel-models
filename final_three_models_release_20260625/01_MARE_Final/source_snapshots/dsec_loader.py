"""
InsightBridge — 澳门酒店真实市场数据加载器
dsec_loader.py
================================================
数据来源：
  - DSEC / MGTO 历史月报（2020-2025）
  - MHA 月度会员酒店数据（2026新增）

用途分工：
  - MHA 当前月 ADR / Occupancy → 当前市场中心与需求强弱
  - DSEC 过去两年历史区间     → floor / ceiling 护栏边界

提供两类功能：
  A. DB 初始化 + 种子数据写入（首次运行一次即可）
  B. 查询接口，供以下模块调用：
       run_21d_harness.py  → compute_dynamic_base_price() + build_external_snapshot()
       mare/director pricing_engine.py → demand_score() 的 dsec_market_occ 输入

关键指标：
  occupancy_rate   入住率 (0.0-1.0)
  adr_mop          平均房价 MOP (Average Daily Rate)
  revpar_mop       RevPAR = occupancy × ADR (计算值)
"""

from __future__ import annotations
import sqlite3
import math
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "hotel_real_data.db"

# ══════════════════════════════════════════════════════════════════════════
#  历史数据（2020-2025，来源：澳门旅游局月报 PDF 提取）
#  格式：(year, month, all_occ, star5_occ, star4_occ, star3_occ,
#                      all_adr, star5_adr, star4_adr, star3_adr)
#  入住率单位：% (直接存储，查询时÷100)
#  ADR单位：MOP
# ══════════════════════════════════════════════════════════════════════════
_RAW_DATA = [
    # 2020
    (2020,1,78.5,85.0,61.5,74.6,1469,1734,764,1080),
    (2020,2,11.9,9.0,17.2,15.1,1086,1732,437,691),
    (2020,3,20.1,16.5,26.1,24.4,1105,1659,410,700),
    (2020,4,10.0,5.7,19.4,13.3,655,1265,302,385),
    (2020,5,9.3,5.3,17.6,12.0,698,1385,308,388),
    (2020,6,9.8,5.9,24.9,12.4,769,1331,320,499),
    (2020,7,10.5,6.7,22.6,14.4,841,1358,329,614),
    (2020,8,12.0,9.9,17.6,14.8,882,1230,316,673),
    (2020,9,16.4,12.6,26.7,22.1,789,1081,293,655),
    (2020,10,42.8,40.0,45.0,51.8,1039,1292,332,787),
    (2020,11,46.7,42.0,51.9,57.6,797,1050,279,600),
    (2020,12,56.8,54.3,61.9,63.5,913,1096,281,654),
    # 2021
    (2021,1,41.8,39.0,46.9,46.4,778,969,399,614),
    (2021,2,39.5,37.3,43.0,42.8,874,1088,482,674),
    (2021,3,57.7,52.6,67.3,65.2,755,929,446,592),
    (2021,4,66.0,62.9,74.4,71.9,832,962,528,621),
    (2021,5,68.2,67.1,68.9,71.5,949,1108,568,714),
    (2021,6,43.2,41.0,55.3,40.2,857,1054,463,636),
    (2021,7,64.4,62.9,66.8,68.2,903,1069,541,639),
    (2021,8,35.4,30.5,52.6,39.4,755,957,430,521),
    (2021,9,51.1,46.4,65.8,56.2,814,1006,445,581),
    (2021,10,42.0,35.3,73.0,43.4,735,938,431,484),
    (2021,11,63.5,60.9,68.9,68.7,895,1073,514,628),
    (2021,12,63.5,60.9,68.9,68.7,895,1073,514,628),  # 注：12月可能为11月重复
    # 2022（部分）
    (2022,1,47.9,44.3,63.1,51.7,807,970,448,554),
    (2022,2,55.3,54.5,62.5,52.1,876,1007,470,656),
    (2022,3,33.6,30.6,44.0,35.3,691,876,346,470),
    (2022,4,31.5,28.6,41.3,34.5,784,969,384,564),
    (2022,5,35.1,32.0,40.7,43.9,726,867,435,519),
    (2022,6,35.0,30.4,55.6,39.5,629,775,429,456),
    (2022,8,33.3,32.0,39.7,32.6,752,914,421,440),
    # 2022年9-12月 PDF为图表图片，暂缺
    # 2023（完整）
    (2023,1,74.0,73.9,70.1,79.0,1181,1371,807,871),
    (2023,2,78.8,77.2,81.1,84.7,1206,1376,903,891),
    (2023,3,79.1,78.7,75.8,83.8,1103,1204,947,948),
    (2023,4,86.2,85.8,80.1,94.6,1370,1527,1067,1038),
    (2023,5,84.8,85.1,74.9,93.7,1317,1482,978,926),
    (2023,6,89.0,89.1,82.3,96.2,1305,1491,921,952),
    (2023,7,93.0,92.9,89.9,97.2,1425,1618,961,1043),
    (2023,8,86.7,93.5,90.7,95.8,1397,1709,1043,1129),
    (2023,9,83.8,84.5,75.8,89.6,1408,1599,945,940),
    (2023,10,87.8,88.3,79.2,94.6,1426,1603,989,975),
    (2023,11,88.4,88.6,82.5,94.6,1401,1604,910,901),
    (2023,12,91.2,91.5,86.5,95.2,1552,1773,999,1022),
    # 2024（完整）
    (2024,1,91.4,92.2,84.6,95.4,1397,1598,875,924),
    (2024,2,89.7,90.9,82.4,92.0,1545,1732,1083,1053),
    (2024,3,89.3,90.7,81.2,91.6,1431,1634,886,941),
    (2024,4,88.8,89.8,80.6,92.9,1329,1478,870,911),
    (2024,5,88.5,90.5,81.3,94.3,1364,1537,927,902),
    (2024,6,90.1,91.1,81.9,94.1,1352,1557,881,914),
    (2024,7,94.3,94.9,89.0,97.2,1382,1552,960,949),
    (2024,8,96.1,96.5,92.9,97.8,1473,1653,1026,1026),
    (2024,9,90.9,91.9,83.2,94.8,1282,1442,861,892),
    (2024,10,93.5,94.3,89.4,96.9,1406,1576,1187,945),
    (2024,11,95.0,95.3,92.3,97.8,1395,1563,1155,981),
    (2024,12,95.0,95.5,91.8,97.3,1451,1624,1189,1017),
    # 2025（完整）
    (2025,1,94.7,95.2,91.6,97.5,1408,1550,1215,1033),
    (2025,2,94.6,95.2,90.7,98.1,1408,1582,1172,967),
    (2025,3,93.9,94.7,89.2,98.2,1287,1447,1071,881),
    (2025,4,92.6,93.5,88.0,96.5,1275,1442,1029,879),
    (2025,5,93.0,94.0,87.6,97.5,1371,1533,1149,959),
    (2025,6,92.9,94.2,87.0,97.6,1261,1454,1070,901),
    (2025,7,95.2,95.7,92.4,97.6,1370,1519,1130,971),
    (2025,8,96.6,96.8,95.2,97.9,1461,1616,1231,1016),
    (2025,9,89.4,90.8,83.9,91.1,1270,1415,1039,850),
    (2025,10,93.3,94.2,88.9,96.1,1413,1572,1181,931),
    (2025,11,93.9,94.2,91.2,97.2,1325,1481,1077,901),
    (2025,12,94.4,95.2,90.3,97.2,1392,1552,1132,949),
    # 2026（MHA 月报）
    (2026,1,94.8,95.8,90.1,97.7,1359.4,1522.5,1105.5,898.3),
    (2026,2,96.0,96.6,93.3,97.6,1416.7,1560.0,1233.5,949.8),
]


# ══════════════════════════════════════════════════════════════════════════
#  DB 初始化 + 种子数据
# ══════════════════════════════════════════════════════════════════════════

def init_and_seed(conn: sqlite3.Connection) -> int:
    """
    创建 dsec_monthly_stats 表并写入历史数据。
    幂等操作，重复调用安全（INSERT OR IGNORE）。
    返回写入记录数。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dsec_monthly_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            INTEGER NOT NULL,
            month           INTEGER NOT NULL,
            all_occ_pct     REAL,    -- 全市场入住率 %
            star5_occ_pct   REAL,    -- 5★ 入住率 %
            star4_occ_pct   REAL,    -- 4★ 入住率 %
            star3_occ_pct   REAL,    -- 3★ 入住率 %
            all_adr_mop     REAL,    -- 全市场 ADR (MOP)
            star5_adr_mop   REAL,    -- 5★ ADR (MOP)
            star4_adr_mop   REAL,    -- 4★ ADR (MOP)
            star3_adr_mop   REAL,    -- 3★ ADR (MOP)
            source          TEXT DEFAULT 'DSEC_MHA',
            UNIQUE(year, month)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dsec_ym ON dsec_monthly_stats(year, month)")
    conn.commit()

    inserted = 0
    for row in _RAW_DATA:
        yr, mo, a_occ, s5_occ, s4_occ, s3_occ, a_adr, s5_adr, s4_adr, s3_adr = row
        cur = conn.execute("""
            INSERT OR IGNORE INTO dsec_monthly_stats
                (year, month, all_occ_pct, star5_occ_pct, star4_occ_pct, star3_occ_pct,
                 all_adr_mop, star5_adr_mop, star4_adr_mop, star3_adr_mop)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (yr, mo, a_occ, s5_occ, s4_occ, s3_occ, a_adr, s5_adr, s4_adr, s3_adr))
        inserted += cur.rowcount
    conn.commit()
    return inserted


# ══════════════════════════════════════════════════════════════════════════
#  查询接口
# ══════════════════════════════════════════════════════════════════════════

def _occ_col(star: int) -> str:
    return {5: "star5_occ_pct", 4: "star4_occ_pct", 3: "star3_occ_pct"}.get(star, "all_occ_pct")

def _adr_col(star: int) -> str:
    return {5: "star5_adr_mop", 4: "star4_adr_mop", 3: "star3_adr_mop"}.get(star, "all_adr_mop")


def _market_group(star: int) -> str:
    return "luxury" if int(star or 0) >= 5 else "mass"


def _historical_window(conn: sqlite3.Connection) -> tuple[int, int]:
    """
    统一历史参考窗口：
      起点固定为 2023（后疫情恢复期）
      终点固定为最新年份的上一年
    若库内最高年份 <= 2025，则终点使用该最新年份本身。
    """
    latest_year_row = conn.execute("SELECT MAX(year) FROM dsec_monthly_stats").fetchone()
    latest_year = int(latest_year_row[0] or 2025)
    hist_end = latest_year - 1 if latest_year >= 2026 else latest_year
    hist_start = 2023
    if hist_end < hist_start:
        hist_end = hist_start
    return hist_start, hist_end


def _percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    if len(values) == 1:
        return float(values[0])
    q = max(0.0, min(1.0, q))
    ordered = sorted(float(v) for v in values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _group_monthly_metric(row: tuple, metric: str, market_group: str) -> float | None:
    if not row:
        return None
    all_occ, star5_occ, star4_occ, star3_occ, all_adr, star5_adr, star4_adr, star3_adr = row
    if market_group == "luxury":
        return float(star5_occ if metric == "occ" else star5_adr)
    if metric == "occ":
        return round((float(star3_occ) + float(star4_occ)) / 2.0, 4)
    return round((float(star3_adr) + float(star4_adr)) / 2.0, 4)


def get_market_occupancy(month: int, star: int,
                         conn: Optional[sqlite3.Connection] = None,
                         year: int = None) -> Optional[float]:
    """
    返回指定月份、星级的市场入住率（0.0-1.0）。
    year=None 时取最近两年（2024-2025）均值，代表"正常化市场水平"。
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        col = _occ_col(star)
        if year:
            row = conn.execute(f"SELECT {col} FROM dsec_monthly_stats WHERE year=? AND month=?",
                               (year, month)).fetchone()
            return float(row[0]) / 100.0 if row and row[0] else None
        else:
            hist_start, hist_end = _historical_window(conn)
            row = conn.execute(
                f"SELECT AVG({col}) FROM dsec_monthly_stats WHERE month=? AND year BETWEEN ? AND ?",
                (month, hist_start, hist_end)
            ).fetchone()
            return float(row[0]) / 100.0 if row and row[0] else None
    finally:
        if _owns:
            conn.close()


def get_market_adr(month: int, star: int,
                   conn: Optional[sqlite3.Connection] = None,
                   year: int = None) -> Optional[float]:
    """
    返回指定月份、星级的市场平均房价（MOP）。
    year=None 时取2023-2025均值（代表后疫情正常化水平）。
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        col = _adr_col(star)
        if year:
            row = conn.execute(f"SELECT {col} FROM dsec_monthly_stats WHERE year=? AND month=?",
                               (year, month)).fetchone()
            return round(float(row[0]), 1) if row and row[0] else None
        else:
            hist_start, hist_end = _historical_window(conn)
            row = conn.execute(
                f"SELECT AVG({col}) FROM dsec_monthly_stats WHERE month=? AND year BETWEEN ? AND ?",
                (month, hist_start, hist_end)
            ).fetchone()
            return round(float(row[0]), 1) if row and row[0] else None
    finally:
        if _owns:
            conn.close()


def get_latest_market_snapshot(
    star: int,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, float | int | str] | None:
    """
    返回最新一期可用 MHA / DSEC 月度快照。
    3★+4★ 视为 mass 市场；5★ 视为 luxury 市场。
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        row = conn.execute(
            """
            SELECT year, month,
                   all_occ_pct, star5_occ_pct, star4_occ_pct, star3_occ_pct,
                   all_adr_mop, star5_adr_mop, star4_adr_mop, star3_adr_mop,
                   source
            FROM dsec_monthly_stats
            ORDER BY year DESC, month DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        market_group = _market_group(star)
        metrics_row = row[2:10]
        occ_pct = _group_monthly_metric(metrics_row, "occ", market_group)
        adr_mop = _group_monthly_metric(metrics_row, "adr", market_group)
        if occ_pct is None or adr_mop is None:
            return None
        return {
            "year": int(row[0]),
            "month": int(row[1]),
            "market_group": market_group,
            "occupancy": round(float(occ_pct) / 100.0, 4),
            "adr": round(float(adr_mop), 1),
            "source": str(row[10] or "DSEC_MHA"),
        }
    finally:
        if _owns:
            conn.close()


def get_latest_market_adr(star: int, conn: Optional[sqlite3.Connection] = None) -> Optional[float]:
    snap = get_latest_market_snapshot(star, conn)
    return float(snap["adr"]) if snap else None


def get_latest_market_occupancy(star: int, conn: Optional[sqlite3.Connection] = None) -> Optional[float]:
    snap = get_latest_market_snapshot(star, conn)
    return float(snap["occupancy"]) if snap else None


def get_market_group_floor_ceiling(
    star: int,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[float, float]:
    """
    用统一历史窗口内的月度 ADR 分布给市场组生成 floor / ceiling。
    规则：
      - 3★+4★ 共用一个 mass 区间
      - 5★ 单独一个 luxury 区间
      - floor   = 历史月度 ADR 分布的 P20
      - ceiling = 历史月度 ADR 分布的 P80
    说明：
      不再使用年度极值平均法，避免被个别极端月份拉偏。
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        hist_start, hist_end = _historical_window(conn)
        rows = conn.execute(
            """
            SELECT year,
                   all_occ_pct, star5_occ_pct, star4_occ_pct, star3_occ_pct,
                   all_adr_mop, star5_adr_mop, star4_adr_mop, star3_adr_mop
            FROM dsec_monthly_stats
            WHERE year BETWEEN ? AND ?
            ORDER BY year, month
            """,
            (hist_start, hist_end),
        ).fetchall()
        market_group = _market_group(star)
        monthly_values: list[float] = []
        for row in rows:
            adr = _group_monthly_metric(row[1:], "adr", market_group)
            if adr is None:
                continue
            monthly_values.append(float(adr))

        if not monthly_values:
            if market_group == "luxury":
                return 1200.0, 2200.0
            return 700.0, 1200.0

        floor_price = round(_percentile(monthly_values, 0.20), 1)
        ceiling_price = round(_percentile(monthly_values, 0.80), 1)
        if ceiling_price <= floor_price:
            ceiling_price = floor_price * 1.15
        return floor_price, ceiling_price
    finally:
        if _owns:
            conn.close()


def get_latest_market_demand_signal(star: int, conn: Optional[sqlite3.Connection] = None) -> float:
    """
    当前 MHA 入住率相对过去两年同市场整体均值的标准化信号，返回 [-1, 1]。
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        snap = get_latest_market_snapshot(star, conn)
        if not snap:
            return 0.0
        market_group = _market_group(star)
        latest_occ_pct = float(snap["occupancy"]) * 100.0
        hist_start, hist_end = _historical_window(conn)
        rows = conn.execute(
            """
            SELECT all_occ_pct, star5_occ_pct, star4_occ_pct, star3_occ_pct,
                   all_adr_mop, star5_adr_mop, star4_adr_mop, star3_adr_mop
            FROM dsec_monthly_stats
            WHERE year BETWEEN ? AND ?
            """,
            (hist_start, hist_end),
        ).fetchall()
        values = [float(_group_monthly_metric(row, "occ", market_group)) for row in rows]
        values = [v for v in values if v > 0]
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        std = math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))
        if std < 0.1:
            return 0.0
        signal = (latest_occ_pct - mean) / std
        return round(max(-1.0, min(1.0, signal)), 4)
    finally:
        if _owns:
            conn.close()


def get_seasonal_profile(star: int,
                         conn: Optional[sqlite3.Connection] = None) -> dict[int, dict]:
    """
    返回12个月的历史均值画像（使用统一历史窗口，代表恢复后的正常水平）。
    {1: {"occ": 0.926, "adr": 1373, "revpar": 1271, "season_mult": 0.94}, ...}
    season_mult = adr / annual_avg_adr（相对年均值的季节乘数）
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        hist_start, hist_end = _historical_window(conn)
        occ_col = _occ_col(star)
        adr_col = _adr_col(star)
        rows = conn.execute(f"""
            SELECT month, AVG({occ_col}), AVG({adr_col})
            FROM dsec_monthly_stats
            WHERE year BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, (hist_start, hist_end)).fetchall()

        if not rows:
            return {}

        # 计算年均ADR（基准）
        annual_adr = sum(r[2] for r in rows if r[2]) / len(rows)

        profile = {}
        for mo, occ, adr in rows:
            occ_f = (occ or 0) / 100.0
            adr_f = float(adr or 0)
            revpar = occ_f * adr_f
            season_mult = round(adr_f / annual_adr, 3) if annual_adr > 0 else 1.0
            profile[int(mo)] = {
                "occ":          round(occ_f, 3),
                "adr":          round(adr_f, 1),
                "revpar":       round(revpar, 1),
                "season_mult":  season_mult,
            }
        return profile
    finally:
        if _owns:
            conn.close()


def get_dsec_demand_signal(month: int, star: int,
                           conn: Optional[sqlite3.Connection] = None) -> float:
    """
    返回 DSEC 市场需求信号 ∈ [-1, 1]，供 demand_score() 使用。

    计算逻辑：
      signal = (该月历史均值 - 历史年均) / 该月历史标准差
      clamped to [-1, 1]

    含义：
      +1.0 = 这个月份历史上显著高于年均
      0.0  = 这个月份历史上大致等于年均
      -1.0 = 这个月份历史上显著低于年均
    """
    _owns = conn is None
    if _owns:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        col = _occ_col(star)
        hist_start, hist_end = _historical_window(conn)

        # 计算该月的历史均值和标准差（统一窗口，同月口径）
        rows = conn.execute(
            f"SELECT {col} FROM dsec_monthly_stats WHERE month=? AND year BETWEEN ? AND ?",
            (month, hist_start, hist_end)
        ).fetchall()
        if not rows:
            return 0.0
        values = [float(r[0]) for r in rows if r[0] is not None]
        if not values:
            return 0.0
        month_mean = sum(values) / len(values)
        if len(values) < 2:
            return 0.0
        month_std = math.sqrt(sum((x - month_mean) ** 2 for x in values) / len(values))
        if month_std < 0.1:
            return 0.0

        # 历史年均：用于判断这个月份相对全年是偏旺还是偏淡
        all_vals = conn.execute(
            f"SELECT {col} FROM dsec_monthly_stats WHERE year BETWEEN ? AND ? AND {col} IS NOT NULL",
            (hist_start, hist_end)
        ).fetchall()
        all_v = [float(r[0]) for r in all_vals]
        if not all_v:
            return 0.0
        all_mean = sum(all_v) / len(all_v)
        signal = (month_mean - all_mean) / month_std
        return round(max(-1.0, min(1.0, signal)), 4)
    finally:
        if _owns:
            conn.close()


def get_calibrated_season_multipliers(star: int,
                                      conn: Optional[sqlite3.Connection] = None) -> dict[str, float]:
    """
    从DSEC数据自动校准季节乘数，格式兼容 pricing_engine.py 的 season_multipliers。
    返回: {"peak": 1.xx, "shoulder": 1.0, "off_peak": 0.xx}

    方法：
      peak     = 月份中ADR最高3个月的均值 / 年均ADR
      off_peak = 月份中ADR最低3个月的均值 / 年均ADR
      shoulder = 1.0（其余月份，作为基准）
    """
    profile = get_seasonal_profile(star, conn)
    if not profile:
        return {"peak": 1.15, "shoulder": 1.0, "off_peak": 0.92}

    adrs = sorted(profile.values(), key=lambda x: x["adr"])
    annual_avg = sum(v["adr"] for v in profile.values()) / len(profile)

    bottom3 = [v["adr"] for v in adrs[:3]]
    top3    = [v["adr"] for v in adrs[-3:]]

    peak     = round(sum(top3)    / len(top3)    / annual_avg, 3)
    off_peak = round(sum(bottom3) / len(bottom3) / annual_avg, 3)

    return {"peak": peak, "shoulder": 1.0, "off_peak": off_peak}


# ══════════════════════════════════════════════════════════════════════════
#  一次性初始化入口
# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    n = init_and_seed(conn)
    print(f"✅ 写入 {n} 条 DSEC 历史数据")

    for star in [3, 4, 5]:
        profile = get_seasonal_profile(star, conn)
        mults   = get_calibrated_season_multipliers(star, conn)
        adr_now = get_market_adr(6, star, conn)   # 6月
        occ_now = get_market_occupancy(6, star, conn)
        sig_now = get_dsec_demand_signal(6, star, conn)
        print(f"\n{star}★ 市场:")
        print(f"  6月均值 → 入住率 {occ_now*100:.1f}% | ADR MOP {adr_now:.0f}")
        print(f"  DSEC需求信号: {sig_now:+.3f}")
        print(f"  季节乘数: {json.dumps(mults)}")
    conn.close()
