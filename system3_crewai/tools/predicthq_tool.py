"""
predicthq_tool.py — PredictHQ 活动事件数据工具
=================================================
用途：查询目标城市未来的大型活动（演唱会、赛事、展览、会议）
     作为需求激增的预测信号，直接影响 MARE 定价模型

PredictHQ 免费注册：https://www.predicthq.com/
申请 API Key 后填入 .env：PREDICTHQ_API_KEY=your_key_here

活动类别（category）说明：
  concerts      演唱会 / 音乐节
  sports        体育赛事
  conferences   会议 / 展览
  festivals     节庆活动
  expos         展览会
  community     社区活动
  public-holidays 公众假期
"""

from __future__ import annotations
import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    _OK = True
except ImportError:
    _OK = False


if _OK:

    class PredictHQInput(BaseModel):
        location: str = Field(
            default="Macau",
            description=(
                "搜索地点，例如：'Macau'、'Hong Kong'、'Dubai'、'Singapore'、"
                "'London'。可以是城市名或 'lat,lon' 格式的坐标。"
            )
        )
        days_ahead: int = Field(
            default=30,
            description="搜索未来多少天内的活动（默认30天，最多90天）"
        )
        categories: str = Field(
            default="concerts,sports,conferences,festivals,expos",
            description="活动类别，逗号分隔。可选：concerts,sports,conferences,festivals,expos,community"
        )
        min_rank: int = Field(
            default=50,
            description="最低影响力分数（0-100），过滤掉小型活动。默认50，只看中大型活动。"
        )

    class PredictHQTool(BaseTool):
        """
        PredictHQ 活动事件查询工具。
        查询指定城市未来的大型活动，用于预测酒店需求激增。
        """
        name: str = "predicthq_events"
        description: str = (
            "查询指定城市未来的大型活动数据（演唱会、赛事、展览、会议等），"
            "用于预测酒店需求激增和市场机会。\n"
            "适合以下场景：\n"
            "- 澳门/香港近期是否有大型赛事或演出？\n"
            "- 未来30天哪些城市有重要旅游行业展会？\n"
            "- 哪些活动会带来客流激增，需要调高房价？\n"
            "返回活动名称、时间、地点、影响力评分（phq_rank）。"
        )
        args_schema: type[PredictHQInput] = PredictHQInput

        def _run(self, location: str = "Macau", days_ahead: int = 30,
                 categories: str = "concerts,sports,conferences,festivals,expos",
                 min_rank: int = 50) -> str:

            api_key = os.getenv("PREDICTHQ_API_KEY", "")
            if not api_key or "your_" in api_key:
                return (
                    "⚠ PredictHQ 未配置（PREDICTHQ_API_KEY 未设置）\n"
                    "免费注册：https://www.predicthq.com/\n"
                    f"查询城市：{location}，未来{days_ahead}天"
                )

            today    = datetime.now().strftime("%Y-%m-%d")
            end_date = (datetime.now() + timedelta(days=min(days_ahead, 90))).strftime("%Y-%m-%d")

            params = {
                "q":           location,
                "start.gte":   today,
                "start.lte":   end_date,
                "category":    categories,
                "phq_rank.gte": str(min_rank),
                "sort":        "-phq_rank",
                "limit":       "10",
            }

            url = "https://api.predicthq.com/v1/events/?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                }
            )

            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return f"PredictHQ 查询失败: {e}"

            events = data.get("results", [])
            if not events:
                return f"✓ PredictHQ 查询完成：{location} 未来{days_ahead}天内无大型活动（rank≥{min_rank}）"

            lines = [f"📅 {location} 未来{days_ahead}天重要活动（共{len(events)}个）：\n"]
            for e in events:
                start    = e.get("start", "")[:10]
                end      = e.get("end", "")[:10]
                title    = e.get("title", "未知活动")
                cat      = e.get("category", "")
                rank     = e.get("phq_rank", 0)
                country  = e.get("country", "")
                # 影响级别
                if rank >= 80:   impact = "🔴 重大影响"
                elif rank >= 60: impact = "🟠 较大影响"
                else:            impact = "🟡 中等影响"

                lines.append(
                    f"{impact} [{rank}分] {title}\n"
                    f"   时间: {start}{'→'+end if end != start else ''}  |  "
                    f"类别: {cat}  |  {country}"
                )

            # 生成需求预测摘要
            high_impact = [e for e in events if e.get("phq_rank", 0) >= 70]
            if high_impact:
                lines.append(
                    f"\n⚡ 需求激增预警：发现 {len(high_impact)} 个高影响力活动，"
                    f"建议提前调高相关日期房价 15-30%"
                )

            return "\n".join(lines)

else:
    class PredictHQTool:  # type: ignore
        def __init__(self):
            print("  [PredictHQ] CrewAI 未安装，工具不可用")
