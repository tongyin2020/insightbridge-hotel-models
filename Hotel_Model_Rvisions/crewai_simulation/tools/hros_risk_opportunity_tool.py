"""
HROS V5 风险与机会评分工具 — 供 CrewAI Agent 调用
V5修复：风险分归一化（/3），机会分权重调整
"""
from __future__ import annotations
import sys, os

_hros_path = os.path.expanduser(
    "~/Desktop/InsightBridge_完整代码_专家审查V4/共用_基础组件")
if _hros_path not in sys.path:
    sys.path.insert(0, _hros_path)

try:
    from crewai.tools import BaseTool

    class HROSRiskOpportunityTool(BaseTool):
        name: str = "hros_risk_opportunity_scorer"
        description: str = (
            "计算酒店价格风险分（0-100）和需求机会分（0-100）。"
            "风险分V5已归一化（原始分/3），避免极端场景全部显示100。"
            "机会分V5权重已调整，避免过早满分。"
        )

        def _run(self, price: float, market_price: float, predicted_occ: float,
                 ota_pace: float = 0.5, competitor_price: float = None,
                 event_density: float = 0.0, border_flow: float = 0.0,
                 is_holiday: bool = False, is_weekend: bool = False) -> str:
            from hros.risk_engine import calculate_price_risk
            from hros.opportunity_engine import calculate_opportunity_score

            risk = calculate_price_risk(price, market_price, predicted_occ,
                                        ota_pace, competitor_price)
            opp = calculate_opportunity_score({
                "event_density": event_density, "border_flow": border_flow,
                "ota_booking_pace": ota_pace, "is_holiday": is_holiday,
                "is_weekend": is_weekend,
            })
            return (f"价格风险分（V5归一化）: {risk}/100\n"
                    f"需求机会分（V5权重）: {opp}/100\n"
                    f"综合判断: {'⚠️ 高风险' if risk > 60 else '✅ 风险可控'} | "
                    f"{'🔥 高机会' if opp > 70 else '📊 普通机会'}")

except ImportError:
    class HROSRiskOpportunityTool:
        def __init__(self): print("[HROS] CrewAI未安装")
