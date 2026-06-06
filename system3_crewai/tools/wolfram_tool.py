"""
wolfram_tool.py — Wolfram Alpha 精确计算工具
=============================================
供 MAREPricingAgent / AnalystAgent 使用，用于：
  - 酒店定价的精确数学计算（弹性曲线、边际收益）
  - 统计分析（标准差、置信区间、回归）
  - 汇率换算（MOP / HKD / CNY / USD）
  - 市场数据验证（公式核验、单位换算）

需要在 .env 中配置：
  WOLFRAM_APP_ID=your_wolfram_app_id_here
  （免费账号可申请，地址：https://developer.wolframalpha.com/）
"""

from __future__ import annotations
import os
import urllib.parse
import urllib.request
import json
from typing import Any

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    _CREWAI_TOOLS_OK = True
except ImportError:
    _CREWAI_TOOLS_OK = False


if _CREWAI_TOOLS_OK:

    class WolframInput(BaseModel):
        query: str = Field(
            ...,
            description=(
                "发送给 Wolfram Alpha 的数学/统计/换算查询，使用英文或数学表达式。"
                "示例：'standard deviation of [450, 520, 480, 610, 395]'，"
                "'convert 520 MOP to HKD'，"
                "'price elasticity formula with demand change 15% price change 10%'"
            )
        )

    class WolframAlphaTool(BaseTool):
        """
        Wolfram Alpha 精确计算工具。
        适用于需要精确数值结果的数学、统计和换算任务。
        """
        name: str = "wolfram_alpha_calculator"
        description: str = (
            "使用 Wolfram Alpha 进行精确的数学计算、统计分析和单位换算。"
            "适合以下场景：\n"
            "- 酒店定价弹性计算（需求变化率/价格变化率）\n"
            "- 统计计算（均值、标准差、置信区间、百分位）\n"
            "- 货币换算（MOP、HKD、CNY、USD之间互转）\n"
            "- 收益计算（RevPAR、ADR、毛利率）\n"
            "- 验证AI推算的数值是否准确\n"
            "注意：查询请用英文或数学公式，结果更精确。"
        )
        args_schema: type[WolframInput] = WolframInput

        def _run(self, query: str) -> str:
            app_id = os.getenv("WOLFRAM_APP_ID", "")
            if not app_id or "your_" in app_id:
                return (
                    "⚠ Wolfram Alpha 未配置（WOLFRAM_APP_ID 未设置）。\n"
                    "请在 .env 中填入 App ID，获取地址：https://developer.wolframalpha.com/\n"
                    f"您的计算查询：{query}"
                )

            try:
                encoded = urllib.parse.quote(query)
                url = (
                    f"https://api.wolframalpha.com/v2/query"
                    f"?input={encoded}"
                    f"&appid={app_id}"
                    f"&output=JSON"
                    f"&format=plaintext"
                    f"&podtitle=Result,Decimal+approximation,Exact+result"
                )

                req = urllib.request.Request(url, headers={"User-Agent": "InsightBridge/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                result = data.get("queryresult", {})
                if not result.get("success"):
                    # 尝试备用接口（短答案）
                    return self._short_answer(query, app_id)

                # 提取各 Pod 的文本答案
                pods = result.get("pods", [])
                lines = []
                for pod in pods:
                    title = pod.get("title", "")
                    for sub in pod.get("subpods", []):
                        text = sub.get("plaintext", "").strip()
                        if text:
                            lines.append(f"[{title}] {text}")

                if lines:
                    return "\n".join(lines[:8])   # 最多返回8行
                return self._short_answer(query, app_id)

            except Exception as e:
                return f"Wolfram Alpha 查询失败: {e}\n查询内容: {query}"

        def _short_answer(self, query: str, app_id: str) -> str:
            """使用 Short Answers API 作为备用"""
            try:
                encoded = urllib.parse.quote(query)
                url = (
                    f"https://api.wolframalpha.com/v1/result"
                    f"?i={encoded}&appid={app_id}"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "InsightBridge/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    answer = resp.read().decode("utf-8")
                return f"结果: {answer}"
            except Exception as e:
                return f"无法获取 Wolfram 答案: {e}"

else:
    # CrewAI 未安装时的占位符
    class WolframAlphaTool:  # type: ignore
        """占位符，CrewAI安装后自动生效"""
        def __init__(self):
            print("  [Wolfram] CrewAI未安装，WolframAlphaTool不可用")
