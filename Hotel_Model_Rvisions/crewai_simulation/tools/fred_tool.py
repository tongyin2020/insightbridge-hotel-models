"""
fred_tool.py — FRED 宏观经济数据 CrewAI 工具
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Federal Reserve Economic Data（圣路易斯联储）
80万+经济序列，实时宏观环境监测

FREDMacroTool 支持操作：
  macro_snapshot  — 一键拉取核心宏观指标快照（利率/通胀/就业/GDP等）
  series          — 拉取任意 FRED 序列最新 N 条数据
  search          — 搜索 FRED 序列关键词
  yield_curve     — 美债收益率曲线（2Y/5Y/10Y/30Y + 曲线形态判断）
  rate_regime     — 利率周期判断（紧缩/宽松/中性）
  inflation       — 通胀面板（CPI/PCE/PPI/5Y盈亏平衡）
  labor           — 就业面板（NFP/失业率/职位空缺/薪资）
  credit          — 信用面板（HY利差/IG利差/TED利差）
  housing         — 房地产面板（房价/开工/销售/抵押贷款利率）
  leading         — 领先指标（ISM/Conference Board/PMI等）

数据用途：
  - 债券信号（yield curve inversion → 衰退风险）
  - 股指宏观过滤（rate_regime + ISM → 顺周期/逆周期仓位）
  - FX 驱动因子（美元指数 + 利差）
  - 酒店/旅游需求宏观背景
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── 核心序列字典 ────────────────────────────────────────────────────
SERIES_MAP: Dict[str, Dict[str, str]] = {
    # 利率
    "FEDFUNDS":   {"name": "Fed Funds Rate",        "cat": "rates"},
    "DGS2":       {"name": "2Y Treasury Yield",     "cat": "rates"},
    "DGS5":       {"name": "5Y Treasury Yield",     "cat": "rates"},
    "DGS10":      {"name": "10Y Treasury Yield",    "cat": "rates"},
    "DGS30":      {"name": "30Y Treasury Yield",    "cat": "rates"},
    "T10Y2Y":     {"name": "10Y-2Y Spread",         "cat": "rates"},
    "T10Y3M":     {"name": "10Y-3M Spread",         "cat": "rates"},
    "TB3MS":      {"name": "3M T-Bill",             "cat": "rates"},
    # 通胀
    "CPIAUCSL":   {"name": "CPI (All Items)",       "cat": "inflation"},
    "CPILFESL":   {"name": "Core CPI",              "cat": "inflation"},
    "PCEPI":      {"name": "PCE Price Index",       "cat": "inflation"},
    "PCEPILFE":   {"name": "Core PCE",              "cat": "inflation"},
    "PPIFIS":     {"name": "PPI Final Demand",      "cat": "inflation"},
    "T5YIE":      {"name": "5Y Breakeven Inflation","cat": "inflation"},
    "T10YIE":     {"name": "10Y Breakeven Inflation","cat": "inflation"},
    # 就业
    "UNRATE":     {"name": "Unemployment Rate",     "cat": "labor"},
    "PAYEMS":     {"name": "Nonfarm Payrolls",      "cat": "labor"},
    "JOLTS":      {"name": "Job Openings",          "cat": "labor"},   # alias
    "JTSJOL":     {"name": "Job Openings (JOLTS)",  "cat": "labor"},
    "AHETOTSL":   {"name": "Avg Hourly Earnings",   "cat": "labor"},
    "ICSA":       {"name": "Initial Jobless Claims","cat": "labor"},
    # GDP / 增长
    "GDP":        {"name": "Real GDP",              "cat": "growth"},
    "GDPC1":      {"name": "Real GDP (Quarterly)",  "cat": "growth"},
    "INDPRO":     {"name": "Industrial Production", "cat": "growth"},
    "RETAILSMNSA":{"name": "Retail Sales",          "cat": "growth"},
    # 信用 / 利差
    "BAMLH0A0HYM2":{"name": "HY OAS Spread",       "cat": "credit"},
    "BAMLC0A0CM":  {"name": "IG OAS Spread",        "cat": "credit"},
    "TEDRATE":     {"name": "TED Spread",           "cat": "credit"},
    "DBAA":        {"name": "Moody's Baa Yield",    "cat": "credit"},
    "DAAA":        {"name": "Moody's Aaa Yield",    "cat": "credit"},
    # 领先指标
    "UMCSENT":    {"name": "U Michigan Sentiment",  "cat": "leading"},
    "PERMIT":     {"name": "Building Permits",      "cat": "leading"},
    "NEWORDER":   {"name": "Mfg New Orders",        "cat": "leading"},
    "M2SL":       {"name": "M2 Money Supply",       "cat": "leading"},
    # 房地产
    "MORTGAGE30US":{"name": "30Y Mortgage Rate",    "cat": "housing"},
    "CSUSHPISA":   {"name": "Case-Shiller Home Price","cat": "housing"},
    "HOUST":       {"name": "Housing Starts",       "cat": "housing"},
    "HSN1F":       {"name": "New Home Sales",       "cat": "housing"},
    # 美元 / FX
    "DTWEXBGS":   {"name": "USD Broad Index",       "cat": "fx"},
    "DEXUSEU":    {"name": "USD/EUR",               "cat": "fx"},
    "DEXJPUS":    {"name": "JPY/USD",               "cat": "fx"},
    # VIX / 市场情绪
    "VIXCLS":     {"name": "VIX Close",             "cat": "sentiment"},
}

# ── 宏观快照核心序列 ────────────────────────────────────────────────
SNAPSHOT_SERIES = [
    "FEDFUNDS", "DGS2", "DGS10", "T10Y2Y",
    "CPIAUCSL", "CPILFESL", "T5YIE",
    "UNRATE", "PAYEMS", "ICSA",
    "BAMLH0A0HYM2", "TEDRATE",
    "VIXCLS", "DTWEXBGS",
]


def _get_fred():
    """获取 fredapi.Fred 实例（动态读取 env，兼容 dotenv 延迟加载）"""
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        raise ValueError("FRED_API_KEY 未配置，请在 .env 中设置")
    from fredapi import Fred
    return Fred(api_key=key)


import time as _time

def _fred_get(fred, series_id: str, retries: int = 3, delay: float = 1.2):
    """带重试的 FRED 序列拉取（规避速率限制间歇 500 错误）"""
    for attempt in range(retries):
        try:
            s = fred.get_series(series_id)
            return s.dropna()
        except Exception as e:
            if attempt < retries - 1:
                _time.sleep(delay * (attempt + 1))
            else:
                logger.debug(f"[FRED] {series_id} 失败({attempt+1}/{retries}): {e}")
                return None
    return None


def _fetch_latest(fred, series_id: str, n: int = 1) -> Optional[float]:
    """拉取全序列，取最新非空值"""
    s = _fred_get(fred, series_id)
    if s is None or len(s) == 0:
        return None
    return round(float(s.iloc[-1]), 4)


def _fetch_series(fred, series_id: str, periods: int = 12) -> List[Dict]:
    """拉取最近 N 个观测值"""
    s = _fred_get(fred, series_id)
    if s is None:
        return [{"error": f"{series_id} 获取失败"}]
    s = s.tail(periods)
    return [
        {"date": str(idx.date()), "value": round(float(v), 4)}
        for idx, v in s.items()
    ]


def _yoy_change(fred, series_id: str) -> Optional[float]:
    """同比变动（年化百分比）"""
    try:
        s = _fred_get(fred, series_id)
        if s is None:
            return None
        if len(s) < 13:
            return None
        latest = float(s.iloc[-1])
        year_ago = float(s.iloc[-13])
        if year_ago == 0:
            return None
        return round((latest - year_ago) / abs(year_ago) * 100, 2)
    except Exception:
        return None


def _yield_curve_shape(spread_10y2y: Optional[float], spread_10y3m: Optional[float]) -> str:
    """判断收益率曲线形态"""
    s = spread_10y2y if spread_10y2y is not None else spread_10y3m
    if s is None:
        return "UNKNOWN"
    if s < -0.5:
        return "DEEPLY_INVERTED"   # 深度倒挂，强衰退信号
    if s < 0:
        return "INVERTED"          # 倒挂
    if s < 0.5:
        return "FLAT"              # 平坦
    if s < 1.5:
        return "NORMAL"            # 正常
    return "STEEP"                 # 陡峭，经济扩张信号


def _rate_regime(fed_funds: Optional[float], t10y: Optional[float],
                 core_cpi_yoy: Optional[float]) -> dict:
    """
    利率周期判断
    real_rate = 10Y yield - core CPI yoy
    """
    if fed_funds is None:
        return {"regime": "UNKNOWN", "note": "数据不足"}

    regime = "NEUTRAL"
    note   = []

    if fed_funds > 4.5:
        regime = "RESTRICTIVE"
        note.append("Fed Funds > 4.5% → 紧缩周期")
    elif fed_funds < 2.0:
        regime = "ACCOMMODATIVE"
        note.append("Fed Funds < 2.0% → 宽松周期")

    real_rate = None
    if t10y is not None and core_cpi_yoy is not None:
        real_rate = round(t10y - core_cpi_yoy, 2)
        if real_rate > 1.5:
            note.append(f"实际利率 {real_rate:.2f}% → 限制性")
        elif real_rate < 0:
            note.append(f"实际利率 {real_rate:.2f}% → 负实际利率（风险资产友好）")

    return {
        "regime":       regime,
        "fed_funds":    fed_funds,
        "real_rate_10y": real_rate,
        "core_cpi_yoy": core_cpi_yoy,
        "notes":        note,
    }


# ══════════════════════════════════════════════════════════════════
#  CrewAI 工具
# ══════════════════════════════════════════════════════════════════

class FREDInput(BaseModel):
    action: str = Field(
        default="macro_snapshot",
        description=(
            "'macro_snapshot' — 核心宏观指标一键快照（利率/通胀/就业/信用）\n"
            "'series'         — 拉取指定 FRED 序列（需提供 series_id）\n"
            "'search'         — 搜索 FRED 序列（需提供 keywords）\n"
            "'yield_curve'    — 美债收益率曲线 + 形态判断\n"
            "'rate_regime'    — 利率周期判断（紧缩/宽松/中性）\n"
            "'inflation'      — 通胀面板（CPI/PCE/PPI/盈亏平衡）\n"
            "'labor'          — 就业面板（NFP/失业率/薪资/初申）\n"
            "'credit'         — 信用面板（HY利差/IG利差/TED利差）\n"
            "'housing'        — 房地产面板（房价/开工/抵押贷款）\n"
            "'leading'        — 领先指标（情绪/许可证/M2）\n"
            "'fx'             — 美元 + 汇率面板"
        )
    )
    series_id: str = Field(default="", description="FRED 序列 ID，如 'DGS10'、'UNRATE'（action=series时使用）")
    keywords: str  = Field(default="", description="搜索关键词（action=search时使用），如 'unemployment rate'")
    periods: int   = Field(default=12, description="拉取最近N个观测值（action=series时使用，默认12）")
    include_history: bool = Field(default=False, description="是否包含历史时序数据（默认False，只返回最新值）")


class FREDMacroTool(BaseTool):
    name: str        = "FREDMacroTool"
    description: str = (
        "FRED 宏观经济数据工具（圣路易斯联储，80万+序列）。\n"
        "• macro_snapshot — 核心宏观快照：Fed Funds / 收益率曲线 / CPI / 就业 / 信用利差\n"
        "• yield_curve    — 美债全曲线（2Y/5Y/10Y/30Y）+ 倒挂/平坦/陡峭判断\n"
        "• rate_regime    — 利率周期（紧缩/宽松/中性）+ 实际利率\n"
        "• inflation      — CPI/PCE/PPI/盈亏平衡通胀面板\n"
        "• labor          — NFP/失业率/JOLTS/初申/薪资面板\n"
        "• credit         — HY利差/IG利差/TED利差/Baa-Aaa信用面板\n"
        "• housing        — 30Y抵押贷款/Case-Shiller/开工/新屋销售\n"
        "• leading        — 密歇根情绪/建筑许可/M2/制造业新订单\n"
        "• fx             — USD宽基指数 / EUR / JPY\n"
        "• series         — 拉取任意FRED序列（需提供series_id）\n"
        "• search         — 搜索FRED序列（需提供keywords）\n\n"
        "使用场景：债券信号补充 | 股指宏观过滤 | FX驱动分析 | 酒店需求背景"
    )
    args_schema: type[BaseModel] = FREDInput

    def _run(self, action: str = "macro_snapshot", series_id: str = "",
             keywords: str = "", periods: int = 12,
             include_history: bool = False) -> str:
        try:
            fred = _get_fred()
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

        ts = datetime.now(timezone.utc).isoformat()

        try:
            # ── macro_snapshot ─────────────────────────────────────
            if action == "macro_snapshot":
                out = {"timestamp": ts, "source": "FRED", "data": {}}
                for sid in SNAPSHOT_SERIES:
                    _time.sleep(0.3)   # 避免 FRED 速率限制
                    val = _fetch_latest(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out["data"][sid] = {
                        "name":  info.get("name", sid),
                        "value": val,
                        "cat":   info.get("cat", ""),
                    }
                # 计算衍生指标
                t10 = out["data"].get("DGS10", {}).get("value")
                t2  = out["data"].get("DGS2",  {}).get("value")
                ff  = out["data"].get("FEDFUNDS", {}).get("value")
                spread = round(t10 - t2, 2) if (t10 and t2) else None
                out["derived"] = {
                    "10y2y_spread":    spread,
                    "curve_shape":     _yield_curve_shape(spread, None),
                    "rate_regime_note": _rate_regime(ff, t10, None)["regime"],
                }
                return json.dumps(out, ensure_ascii=False, indent=2)

            # ── series ─────────────────────────────────────────────
            elif action == "series":
                if not series_id:
                    return json.dumps({"error": "请提供 series_id，如 'DGS10'"})
                data = _fetch_series(fred, series_id.upper(), periods)
                info = SERIES_MAP.get(series_id.upper(), {})
                return json.dumps({
                    "series_id": series_id.upper(),
                    "name":      info.get("name", series_id),
                    "periods":   periods,
                    "data":      data,
                    "latest":    data[-1] if data else None,
                }, ensure_ascii=False, indent=2)

            # ── search ─────────────────────────────────────────────
            elif action == "search":
                if not keywords:
                    return json.dumps({"error": "请提供 keywords"})
                results = fred.search(keywords, limit=10)
                out = []
                for _, row in results.iterrows():
                    out.append({
                        "id":         row.get("id", ""),
                        "title":      row.get("title", ""),
                        "frequency":  row.get("frequency_short", ""),
                        "units":      row.get("units_short", ""),
                        "last_updated": str(row.get("last_updated", "")),
                    })
                return json.dumps({"keywords": keywords, "results": out},
                                  ensure_ascii=False, indent=2)

            # ── yield_curve ────────────────────────────────────────
            elif action == "yield_curve":
                yield_series = {
                    "3M": "TB3MS", "2Y": "DGS2", "5Y": "DGS5",
                    "10Y": "DGS10", "30Y": "DGS30",
                }
                curve = {}
                for tenor, sid in yield_series.items():
                    val = _fetch_latest(fred, sid)
                    curve[tenor] = val

                spread_10y2y = None
                if curve.get("10Y") and curve.get("2Y"):
                    spread_10y2y = round(curve["10Y"] - curve["2Y"], 3)
                spread_10y3m = None
                if curve.get("10Y") and curve.get("3M"):
                    spread_10y3m = round(curve["10Y"] - curve["3M"], 3)

                shape = _yield_curve_shape(spread_10y2y, spread_10y3m)

                # 解读
                interpretation = {
                    "DEEPLY_INVERTED": "🔴 深度倒挂 — 强烈衰退信号，历史准确率90%+，通常领先衰退12-18个月",
                    "INVERTED":        "🟠 收益率曲线倒挂 — 衰退预警，建议减少风险资产敞口",
                    "FLAT":            "🟡 曲线平坦 — 经济增速放缓，利率见顶信号",
                    "NORMAL":          "🟢 正常斜率 — 经济扩张，风险资产友好",
                    "STEEP":           "🔵 曲线陡峭 — 经济复苏早期，通胀预期上升",
                    "UNKNOWN":         "⚪ 数据不足",
                }.get(shape, "")

                return json.dumps({
                    "timestamp":      ts,
                    "yield_curve":    curve,
                    "spreads": {
                        "10Y_2Y": spread_10y2y,
                        "10Y_3M": spread_10y3m,
                    },
                    "shape":          shape,
                    "interpretation": interpretation,
                    "trading_signal": {
                        "bonds":    "LONG" if shape in ("DEEPLY_INVERTED", "INVERTED") else "NEUTRAL",
                        "equities": "CAUTION" if shape in ("DEEPLY_INVERTED", "INVERTED") else "OK",
                    },
                }, ensure_ascii=False, indent=2)

            # ── rate_regime ────────────────────────────────────────
            elif action == "rate_regime":
                ff    = _fetch_latest(fred, "FEDFUNDS")
                t10   = _fetch_latest(fred, "DGS10")
                t2    = _fetch_latest(fred, "DGS2")
                corecpi_yoy = _yoy_change(fred, "CPILFESL")
                corepce_yoy = _yoy_change(fred, "PCEPILFE")
                regime_info = _rate_regime(ff, t10, corecpi_yoy)
                spread = round(t10 - t2, 3) if (t10 and t2) else None
                return json.dumps({
                    "timestamp":     ts,
                    "rate_regime":   regime_info,
                    "fed_funds":     ff,
                    "10y_yield":     t10,
                    "2y_yield":      t2,
                    "10y_2y_spread": spread,
                    "curve_shape":   _yield_curve_shape(spread, None),
                    "core_cpi_yoy":  corecpi_yoy,
                    "core_pce_yoy":  corepce_yoy,
                    "implication": (
                        "股指：" + (
                            "风险资产承压，谨慎" if regime_info["regime"] == "RESTRICTIVE"
                            else "流动性充裕，有利风险资产" if regime_info["regime"] == "ACCOMMODATIVE"
                            else "中性，跟随数据"
                        )
                    ),
                }, ensure_ascii=False, indent=2)

            # ── inflation ──────────────────────────────────────────
            elif action == "inflation":
                sids = ["CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PPIFIS", "T5YIE", "T10YIE"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    yoy = _yoy_change(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val, "yoy_pct": yoy}
                    if include_history:
                        out[sid]["history"] = _fetch_series(fred, sid, periods)
                # 通胀判断
                core_cpi = out.get("CPILFESL", {}).get("yoy_pct")
                inflation_status = (
                    "ABOVE_TARGET" if core_cpi and core_cpi > 3.0 else
                    "AT_TARGET"    if core_cpi and 1.5 < core_cpi <= 3.0 else
                    "BELOW_TARGET" if core_cpi else "UNKNOWN"
                )
                return json.dumps({
                    "timestamp":        ts,
                    "inflation_status": inflation_status,
                    "fed_target":       "2.0%",
                    "data":             out,
                }, ensure_ascii=False, indent=2)

            # ── labor ──────────────────────────────────────────────
            elif action == "labor":
                sids = ["UNRATE", "PAYEMS", "JTSJOL", "AHETOTSL", "ICSA"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    yoy = _yoy_change(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val, "yoy_pct": yoy}
                    if include_history:
                        out[sid]["history"] = _fetch_series(fred, sid, periods)
                unrate = out.get("UNRATE", {}).get("latest")
                labor_status = (
                    "TIGHT"   if unrate and unrate < 4.0 else
                    "NORMAL"  if unrate and unrate < 5.5 else
                    "WEAK"    if unrate else "UNKNOWN"
                )
                return json.dumps({
                    "timestamp":    ts,
                    "labor_status": labor_status,
                    "data":         out,
                }, ensure_ascii=False, indent=2)

            # ── credit ─────────────────────────────────────────────
            elif action == "credit":
                sids = ["BAMLH0A0HYM2", "BAMLC0A0CM", "TEDRATE", "DBAA", "DAAA"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val}
                    if include_history:
                        out[sid]["history"] = _fetch_series(fred, sid, periods)
                hy = out.get("BAMLH0A0HYM2", {}).get("latest")
                credit_stress = (
                    "HIGH"   if hy and hy > 600 else
                    "MEDIUM" if hy and hy > 400 else
                    "LOW"    if hy else "UNKNOWN"
                )
                return json.dumps({
                    "timestamp":     ts,
                    "credit_stress": credit_stress,
                    "hy_spread_bps": hy,
                    "signal": "RISK_OFF" if credit_stress == "HIGH" else "RISK_ON" if credit_stress == "LOW" else "NEUTRAL",
                    "data":          out,
                }, ensure_ascii=False, indent=2)

            # ── housing ────────────────────────────────────────────
            elif action == "housing":
                sids = ["MORTGAGE30US", "CSUSHPISA", "HOUST", "HSN1F"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    yoy = _yoy_change(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val, "yoy_pct": yoy}
                    if include_history:
                        out[sid]["history"] = _fetch_series(fred, sid, periods)
                return json.dumps({"timestamp": ts, "data": out},
                                  ensure_ascii=False, indent=2)

            # ── leading ────────────────────────────────────────────
            elif action == "leading":
                sids = ["UMCSENT", "PERMIT", "NEWORDER", "M2SL"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    yoy = _yoy_change(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val, "yoy_pct": yoy}
                    if include_history:
                        out[sid]["history"] = _fetch_series(fred, sid, periods)
                return json.dumps({"timestamp": ts, "data": out},
                                  ensure_ascii=False, indent=2)

            # ── fx ─────────────────────────────────────────────────
            elif action == "fx":
                sids = ["DTWEXBGS", "DEXUSEU", "DEXJPUS"]
                out = {}
                for sid in sids:
                    val = _fetch_latest(fred, sid)
                    info = SERIES_MAP.get(sid, {})
                    out[sid] = {"name": info.get("name", sid), "latest": val}
                return json.dumps({"timestamp": ts, "data": out},
                                  ensure_ascii=False, indent=2)

            else:
                return json.dumps({"error": f"未知 action: {action}。支持：macro_snapshot/series/search/yield_curve/rate_regime/inflation/labor/credit/housing/leading/fx"})

        except Exception as e:
            logger.error(f"[FREDMacroTool] {e}", exc_info=True)
            return json.dumps({"error": str(e)}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
#  全局实例
# ══════════════════════════════════════════════════════════════════
_FRED_TOOL = FREDMacroTool()


# ══════════════════════════════════════════════════════════════════
#  自检
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    from dotenv import load_dotenv
    load_dotenv()
    t = FREDMacroTool()

    print("[1] macro_snapshot")
    r = json.loads(t._run(action="macro_snapshot"))
    data = r.get("data", {})
    print(f"  Fed Funds: {data.get('FEDFUNDS',{}).get('value')}%")
    print(f"  10Y:       {data.get('DGS10',{}).get('value')}%")
    print(f"  2Y:        {data.get('DGS2',{}).get('value')}%")
    print(f"  10Y-2Y:    {r.get('derived',{}).get('10y2y_spread')}%  → {r.get('derived',{}).get('curve_shape')}")
    print(f"  VIX:       {data.get('VIXCLS',{}).get('value')}")
    print(f"  HY Spread: {data.get('BAMLH0A0HYM2',{}).get('value')} bps")

    print()
    print("[2] yield_curve")
    r2 = json.loads(t._run(action="yield_curve"))
    print(f"  Curve: {r2.get('yield_curve')}")
    print(f"  Shape: {r2.get('shape')}  → {r2.get('interpretation','')[:40]}")
    print(f"  Bonds: {r2.get('trading_signal',{}).get('bonds')}")

    print()
    print("[3] rate_regime")
    r3 = json.loads(t._run(action="rate_regime"))
    print(f"  Regime: {r3.get('rate_regime',{}).get('regime')}")
    print(f"  Real Rate 10Y: {r3.get('rate_regime',{}).get('real_rate_10y')}%")
    print(f"  Core CPI YoY: {r3.get('core_cpi_yoy')}%")

    print()
    print("[4] credit")
    r4 = json.loads(t._run(action="credit"))
    print(f"  HY Spread: {r4.get('hy_spread_bps')} bps  stress={r4.get('credit_stress')}")
    print(f"  Signal: {r4.get('signal')}")

    print()
    print("[5] series — FEDFUNDS")
    r5 = json.loads(t._run(action="series", series_id="FEDFUNDS", periods=6))
    print(f"  Latest 6: {[d['value'] for d in r5.get('data', [])]}")
    print()
    print("✅ FREDMacroTool 自检完成")
