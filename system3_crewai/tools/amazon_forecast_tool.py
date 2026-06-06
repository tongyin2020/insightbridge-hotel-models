"""
amazon_forecast_tool.py — AWS 需求预测与市场洞察工具
=====================================================
双轨策略：
  Track A: Amazon Forecast（若已有训练好的预测器）→ 查询未来酒店需求
  Track B: Amazon Bedrock（Claude via AWS）→ 基于市场信号的智能分析
  Fallback: 本地规则引擎（不需要任何 API）

AWS 凭证读取自环境变量：
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  AWS_DEFAULT_REGION（默认 us-east-1）

所有异常均被捕获，失败时优雅降级，不影响主流程。
"""

from __future__ import annotations
import os
import json
from datetime import datetime, timedelta
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    _BOTO3_OK = True
except ImportError:
    _BOTO3_OK = False

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    _CREWAI_OK = True
except ImportError:
    _CREWAI_OK = False


# ── AWS 客户端工厂（懒加载）────────────────────────────────────────────
def _aws_session():
    """创建 boto3 Session，优先使用 .env 中的凭证"""
    return boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


# ── Track A: Amazon Forecast 查询 ──────────────────────────────────────
def _query_amazon_forecast(location: str, days: int) -> str | None:
    """
    尝试查询已部署的 Amazon Forecast 预测器。
    若未部署或查询失败，返回 None（触发 Track B）。
    """
    if not _BOTO3_OK:
        return None
    try:
        session = _aws_session()
        fc_client = session.client("forecast")
        qr_client = session.client("forecastquery")

        # 列出已有预测器
        predictors = fc_client.list_predictors().get("Predictors", [])
        if not predictors:
            return None  # 尚未训练，走 Track B

        # 取最新活跃预测器
        active = [p for p in predictors if p.get("Status") == "ACTIVE"]
        if not active:
            return None

        # 列出已有预测
        forecasts = fc_client.list_forecasts().get("Forecasts", [])
        active_fc = [f for f in forecasts if f.get("Status") == "ACTIVE"]
        if not active_fc:
            return None

        forecast_arn = active_fc[0]["ForecastArn"]
        start_date = datetime.now().strftime("%Y-%m-%dT00:00:00")
        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")

        response = qr_client.query_forecast(
            ForecastArn=forecast_arn,
            Filters={"item_id": location.lower().replace(" ", "_")},
            StartDate=start_date,
            EndDate=end_date,
        )

        predictions = response.get("Forecast", {}).get("Predictions", {})
        if not predictions:
            return None

        lines = [f"📡 Amazon Forecast 预测结果（{location}，未来{days}天）：\n"]
        p50 = predictions.get("p50", [])
        p90 = predictions.get("p90", [])

        for i, (low, high) in enumerate(zip(p50[:days], p90[:days])):
            d = (datetime.now() + timedelta(days=i)).strftime("%m/%d")
            val = low.get("Value", 0)
            h_val = high.get("Value", 0)
            level = "🔴 HIGH" if val > 0.65 else ("🟡 MEDIUM" if val > 0.35 else "🟢 LOW")
            lines.append(f"  {d} → {level}  需求指数: {val:.2f}~{h_val:.2f}")

        lines.append(f"\n✓ 数据来源：Amazon Forecast（ARN: ...{forecast_arn[-20:]}）")
        return "\n".join(lines)

    except (ClientError, NoCredentialsError, Exception):
        return None  # 失败→ Track B


# ── Track B: Amazon Bedrock 智能分析 ────────────────────────────────────
def _query_bedrock(location: str, days: int, context: dict) -> str | None:
    """
    使用 Amazon Bedrock 做需求分析，按优先级尝试多个模型：
      1. anthropic.claude-3-5-haiku-20241022-v1:0  (最新 Haiku)
      2. anthropic.claude-3-haiku-20240307-v1:0    (旧版 Haiku)
      3. meta.llama4-scout-17b-instruct-v1:0       (Llama 4，无需审批)
      4. amazon.nova-lite-v1:0                     (Amazon Nova，无需审批)
    """
    if not _BOTO3_OK:
        return None

    prompt_text = f"""You are a hotel demand analyst for {location}.
Analyze the following market signals and predict hotel demand for the next {days} days:

Market signals:
- Border/Visitor flow: {context.get('border_flow', 0.3)} (-1=very low, 1=very high)
- Weather: {context.get('weather', 0)} (-1=bad, 1=great)
- Competitor avg price: {context.get('competitor_price', 450)} MOP
- Major event score: {context.get('event_score', 0)}/100
- Holiday flag: {'Yes' if context.get('is_holiday') else 'No'}
- Current month: {datetime.now().strftime('%B')}
- Day of week: {datetime.now().strftime('%A')}

Provide:
1. Daily demand forecast (HIGH/MEDIUM/LOW) for next {days} days with dates
2. Overall pricing recommendation (raise/hold/lower by what %)
3. Key risk factors
4. One-sentence market opportunity insight

Be concise. Use bullet points. Start with the daily forecast table."""

    session = _aws_session()
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    bedrock = session.client("bedrock-runtime", region_name=region)

    # (model_id, body_builder, text_extractor, display_name)
    candidates = [
        (
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            lambda p: json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "messages": [{"role": "user", "content": p}]
            }),
            lambda r: r["content"][0]["text"],
            "Claude 3.5 Haiku",
        ),
        (
            "anthropic.claude-3-haiku-20240307-v1:0",
            lambda p: json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "messages": [{"role": "user", "content": p}]
            }),
            lambda r: r["content"][0]["text"],
            "Claude 3 Haiku",
        ),
        (
            "meta.llama4-scout-17b-instruct-v1:0",
            lambda p: json.dumps({"prompt": p, "max_gen_len": 600, "temperature": 0.2}),
            lambda r: r.get("generation", ""),
            "Llama 4 Scout",
        ),
        (
            "amazon.nova-lite-v1:0",
            lambda p: json.dumps({
                "messages": [{"role": "user", "content": [{"text": p}]}]
            }),
            lambda r: r["output"]["message"]["content"][0]["text"],
            "Amazon Nova Lite",
        ),
    ]

    for model_id, build_body, extract_text, model_name in candidates:
        try:
            response = bedrock.invoke_model(
                modelId=model_id,
                body=build_body(prompt_text),
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            text = extract_text(result)
            if text:
                return f"🤖 Amazon Bedrock [{model_name}] 需求分析（{location}）：\n\n{text}"
        except Exception:
            continue  # 尝试下一个模型

    return None  # 所有模型均失败 → Fallback


# ── Fallback: 本地规则引擎 ──────────────────────────────────────────────
def _local_forecast(location: str, days: int, context: dict) -> str:
    """最后兜底：简单规则引擎，无需任何外部 API"""
    month = datetime.now().month
    dow = datetime.now().weekday()

    base = 0.45
    if month in [10, 11, 12, 1, 2]: base += 0.20
    if month in [7, 8]: base -= 0.10
    if dow >= 4: base += 0.15
    if context.get("is_holiday"): base += 0.30
    if context.get("border_flow", 0) > 0.5: base += 0.15
    if context.get("event_score", 0) > 70: base += 0.20

    base = max(0.1, min(0.9, base))
    level = "🔴 HIGH（高需求）" if base > 0.65 else ("🟡 MEDIUM（中等）" if base > 0.35 else "🟢 LOW（低需求）")

    lines = [f"📊 本地规则引擎预测（{location}，未来{days}天）：\n"]
    for i in range(days):
        d = (datetime.now() + timedelta(days=i)).strftime("%m/%d")
        weekend_boost = 0.10 if (datetime.now() + timedelta(days=i)).weekday() >= 5 else 0
        score = min(0.95, base + weekend_boost)
        lv = "🔴 HIGH" if score > 0.65 else ("🟡 MED" if score > 0.35 else "🟢 LOW")
        lines.append(f"  {d} → {lv}  (score: {score:.2f})")

    action = "建议提价 10-20%" if base > 0.65 else ("维持现价" if base > 0.35 else "考虑适当降价")
    lines.append(f"\n💡 建议：{action}")
    lines.append("ℹ️  (本地规则模式 — 配置 AWS 凭证可启用 Bedrock 高精度分析)")
    return "\n".join(lines)


# ── CrewAI Tool 定义 ────────────────────────────────────────────────────
if _CREWAI_OK:

    class AmazonForecastInput(BaseModel):
        location: str = Field(
            default="Macau",
            description="预测城市，例如 'Macau'、'Hong Kong'、'Dubai'、'Singapore'"
        )
        days: int = Field(
            default=7,
            description="预测天数（1-30天）"
        )
        border_flow: float = Field(
            default=0.3,
            description="口岸/访客流量信号：-1.0（极低）到 1.0（极高）"
        )
        weather: float = Field(
            default=0.0,
            description="天气信号：-1.0（极差）到 1.0（极好）"
        )
        competitor_price: float = Field(
            default=450.0,
            description="竞对平均价格（MOP）"
        )
        event_score: float = Field(
            default=0.0,
            description="活动影响分（0-100），有大型活动时填入"
        )
        is_holiday: int = Field(
            default=0,
            description="是否节假日：1=是，0=否"
        )

    class AmazonForecastTool(BaseTool):
        """
        AWS 需求预测工具（三级降级策略）：
        Amazon Forecast → Amazon Bedrock → 本地规则引擎
        """
        name: str = "amazon_forecast"
        description: str = (
            "使用 AWS 预测酒店/旅游目的地的需求水平（三级降级）：\n"
            "1. Amazon Forecast（若已部署预测器）→ 历史数据训练的精准预测\n"
            "2. Amazon Bedrock Claude → 基于市场信号的 AI 智能分析\n"
            "3. 本地规则引擎 → 无需 API 的兜底预测\n\n"
            "适合场景：\n"
            "- 预测目的地城市未来7天酒店需求\n"
            "- 结合活动、节假日、天气、竞对价格做综合判断\n"
            "- 为 MARE 定价模型提供 AWS 级别的前瞻需求信号"
        )
        args_schema: type[AmazonForecastInput] = AmazonForecastInput

        def _run(self, location: str = "Macau", days: int = 7,
                 border_flow: float = 0.3, weather: float = 0.0,
                 competitor_price: float = 450.0, event_score: float = 0.0,
                 is_holiday: int = 0) -> str:

            ctx = {
                "border_flow": border_flow,
                "weather": weather,
                "competitor_price": competitor_price,
                "event_score": event_score,
                "is_holiday": is_holiday,
            }

            # Track A: Amazon Forecast
            result = _query_amazon_forecast(location, days)
            if result:
                return result

            # Track B: Amazon Bedrock
            result = _query_bedrock(location, days, ctx)
            if result:
                return result

            # Fallback: 本地规则
            return _local_forecast(location, days, ctx)

else:
    class AmazonForecastTool:  # type: ignore
        def __init__(self):
            print("  [AmazonForecast] CrewAI 未安装，工具不可用")
