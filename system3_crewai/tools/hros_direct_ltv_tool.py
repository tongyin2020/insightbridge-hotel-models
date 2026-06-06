"""
HROS V5 直销LTV工具 — 供 CrewAI Agent 调用
==============================================
P0修复：直销 LTV 现已包含折现因子（discount_rate=0.10）
"""
from __future__ import annotations
import sys, os

# 加载 hros 包
_hros_path = os.path.expanduser(
    "~/Desktop/InsightBridge_完整代码_专家审查V4/共用_基础组件")
if _hros_path not in sys.path:
    sys.path.insert(0, _hros_path)

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field

    class DirectLTVInput(BaseModel):
        direct_price: float = Field(..., description="直销报价（MOP）")
        ota_gross_price: float = Field(..., description="OTA标准价（MOP）")
        ota_commission_rate: float = Field(0.15, description="OTA佣金率（大众=0.15，豪华=0.185）")
        repeat_probability: float = Field(0.20, description="复购概率 0-1")
        future_margin: float = Field(700.0, description="未来单次复购毛利（MOP）")
        crm_value: float = Field(0.0, description="CRM附加价值（MOP）")
        acquisition_cost: float = Field(0.0, description="获客成本（MOP）")
        discount_rate: float = Field(0.10, description="折现率（默认10%年化）")

    class HROSDirectLTVTool(BaseTool):
        name: str = "hros_direct_ltv_calculator"
        description: str = (
            "计算直销LTV（生命周期价值）与OTA净收益对比，判断直销是否划算。"
            "V5版本已修复折现因子，避免高估直销价值。"
            "返回：direct_ltv, ota_net, advantage, direct_wins, discounted_future_value"
        )
        args_schema: type[DirectLTVInput] = DirectLTVInput

        def _run(self, direct_price: float, ota_gross_price: float,
                 ota_commission_rate: float = 0.15, repeat_probability: float = 0.20,
                 future_margin: float = 700.0, crm_value: float = 0.0,
                 acquisition_cost: float = 0.0, discount_rate: float = 0.10) -> str:
            from hros.direct_ltv_engine import DirectLTVEngine
            decision = DirectLTVEngine().evaluate_direct_offer(
                direct_price=direct_price,
                ota_gross_price=ota_gross_price,
                ota_commission_rate=ota_commission_rate,
                repeat_probability=repeat_probability,
                future_margin=future_margin,
                crm_value=crm_value,
                acquisition_cost=acquisition_cost,
                discount_rate=discount_rate,
            )
            result = (
                f"直销LTV: {decision.direct_ltv} MOP\n"
                f"OTA净收益: {decision.ota_net_revenue} MOP\n"
                f"直销优势: {decision.direct_advantage:+.2f} MOP\n"
                f"折现后复购收益: {decision.discounted_future_value} MOP（折现率{decision.discount_rate:.0%}）\n"
                f"结论: {'✅ 直销更优' if decision.direct_wins else '❌ OTA更优'}"
            )
            return result

except ImportError:
    class HROSDirectLTVTool:
        def __init__(self): print("[HROS] CrewAI未安装，HROSDirectLTVTool不可用")
