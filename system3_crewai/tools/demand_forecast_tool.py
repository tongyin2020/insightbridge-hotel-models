"""
demand_forecast_tool.py — LightGBM 需求预测工具
=================================================
用途：基于历史模拟数据 + 市场信号，预测未来7天的酒店需求指数
     输出高/中/低需求概率，直接指导 MARE 定价策略

无需 API Key，完全本地运行（免费）

模型输入特征：
  - 星期几（周末/工作日）
  - 月份（淡旺季）
  - 是否节假日
  - 天气信号
  - 竞对价格水平
  - 口岸过境量信号（border_flow）
  - PredictHQ 活动影响分（如有）

模型输出：
  - demand_level: HIGH / MEDIUM / LOW
  - demand_score: 0.0 - 1.0
  - recommended_pricing_action: 上调/持平/下调
  - confidence: 置信度
"""

from __future__ import annotations
import json
import math
from datetime import datetime, timedelta
from typing import Any

try:
    import numpy as np
    import lightgbm as lgb
    from sklearn.preprocessing import LabelEncoder
    _ML_OK = True
except ImportError:
    _ML_OK = False

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    _CREWAI_OK = True
except ImportError:
    _CREWAI_OK = False


# ── 模拟训练数据生成（基于澳门历史规律）───────────────────────────────
def _generate_training_data(n_samples: int = 2000):
    """
    生成澳门酒店需求历史模拟数据用于训练。
    实际部署时可替换为从 crewai_results.db 读取真实历史数据。
    """
    import random
    random.seed(42)

    X, y = [], []
    for _ in range(n_samples):
        month       = random.randint(1, 12)
        dow         = random.randint(0, 6)      # 0=Monday
        is_holiday  = 1 if random.random() < 0.12 else 0
        weather     = random.uniform(-1.0, 1.0)
        border_flow = random.uniform(-1.0, 1.0)
        comp_price  = random.uniform(200, 800)  # MOP
        event_score = random.uniform(0, 100)    # PredictHQ rank

        # 澳门需求规律
        base = 0.4
        if month in [10, 11, 12, 1, 2]: base += 0.25   # 旺季
        if month in [7, 8]:             base -= 0.10   # 暑热淡季
        if dow >= 5:                    base += 0.20   # 周末
        if is_holiday:                  base += 0.35   # 假日
        if weather > 0.3:               base += 0.10
        if border_flow > 0.5:           base += 0.15
        if event_score > 70:            base += 0.20   # 大型活动
        if comp_price > 600:            base += 0.05   # 竞对贵→需求集中

        noise  = random.gauss(0, 0.08)
        demand = max(0.0, min(1.0, base + noise))

        X.append([month, dow, is_holiday, weather, border_flow,
                  comp_price / 800.0, event_score / 100.0])
        y.append(1 if demand > 0.65 else (0 if demand < 0.35 else 2))
        # 1=HIGH, 0=LOW, 2=MEDIUM

    return X, y


# ── 全局模型缓存（只训练一次）─────────────────────────────────────────
_MODEL_CACHE = None

def _get_model():
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    if not _ML_OK:
        return None
    X, y = _generate_training_data(2000)
    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.int32)

    model = lgb.LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        num_leaves=31,
        max_depth=6,
        n_jobs=2,
        verbose=-1,
    )
    model.fit(X_arr, y_arr)
    _MODEL_CACHE = model
    return model


if _CREWAI_OK:

    class DemandForecastInput(BaseModel):
        target_date: str = Field(
            default="",
            description="预测日期，格式 YYYY-MM-DD。留空则预测今天起未来7天。"
        )
        border_flow: float = Field(
            default=0.3,
            description="口岸过境量信号，-1.0（极低）到 1.0（极高）"
        )
        weather: float = Field(
            default=0.0,
            description="天气信号，-1.0（极差）到 1.0（极好）"
        )
        competitor_price: float = Field(
            default=450.0,
            description="竞对平均价格（MOP），用于判断市场价格水平"
        )
        event_score: float = Field(
            default=0.0,
            description="PredictHQ 活动影响分（0-100），有大型活动时填入"
        )
        is_holiday: int = Field(
            default=0,
            description="是否节假日：1=是，0=否"
        )

    class DemandForecastTool(BaseTool):
        """
        LightGBM 酒店需求预测工具（本地运行，免费）。
        预测未来7天的需求水平，输出定价建议。
        """
        name: str = "demand_forecast_lgbm"
        description: str = (
            "使用 LightGBM 机器学习模型预测澳门酒店未来7天需求水平。\n"
            "输入：日期、天气、口岸流量、竞对价格、活动评分\n"
            "输出：HIGH/MEDIUM/LOW 需求预测 + 定价行动建议\n"
            "适合以下场景：\n"
            "- 下周末是否是高需求期？应该提价多少？\n"
            "- 结合 PredictHQ 活动数据，预测节假日需求\n"
            "- 为 MARE 定价模型提供前瞻性需求判断"
        )
        args_schema: type[DemandForecastInput] = DemandForecastInput

        def _run(self, target_date: str = "", border_flow: float = 0.3,
                 weather: float = 0.0, competitor_price: float = 450.0,
                 event_score: float = 0.0, is_holiday: int = 0) -> str:

            if not _ML_OK:
                return "❌ LightGBM 未安装，运行：pip install lightgbm numpy scikit-learn"

            model = _get_model()
            if model is None:
                return "❌ 模型训练失败"

            # 确定预测起始日期
            if target_date:
                try:
                    start = datetime.strptime(target_date, "%Y-%m-%d")
                except ValueError:
                    start = datetime.now()
            else:
                start = datetime.now()

            # 澳门节假日（简化版）
            HOLIDAYS = {
                "01-01", "01-22", "01-23", "01-24",  # 元旦/春节
                "04-04", "05-01", "06-02", "09-29",  # 清明/劳动/端午/国庆前夜
                "10-01", "10-02", "10-03",            # 国庆
                "10-11", "11-02", "12-08", "12-20",   # 重阳/追思/圣母/澳门回归
                "12-24", "12-25",                      # 圣诞
            }

            results  = []
            labels   = {1: "🔴 HIGH（高需求）", 0: "🟢 LOW（低需求）", 2: "🟡 MEDIUM（中等需求）"}
            actions  = {1: "建议上调价格 15-25%", 0: "建议下调或维持价格", 2: "维持当前价格"}

            for i in range(7):
                d      = start + timedelta(days=i)
                month  = d.month
                dow    = d.weekday()
                h_flag = 1 if d.strftime("%m-%d") in HOLIDAYS else is_holiday
                weekend = 1 if dow >= 5 else 0

                features = np.array([[
                    month,
                    dow,
                    h_flag,
                    weather,
                    border_flow,
                    competitor_price / 800.0,
                    event_score / 100.0,
                ]], dtype=np.float32)

                proba = model.predict_proba(features)[0]
                pred  = int(model.predict(features)[0])
                conf  = float(max(proba))

                # 定价建议
                if pred == 1:      price_adj = "+15% ~ +25%"
                elif pred == 0:    price_adj = "-5% ~ -15%"
                else:              price_adj = "±0% ~ +5%"

                day_label = "周末" if weekend else "工作日"
                hol_label = " 🎉节假日" if h_flag else ""

                results.append(
                    f"  {d.strftime('%m/%d')}（{day_label}{hol_label}）→ "
                    f"{labels[pred]}  置信度:{conf:.0%}  定价:{price_adj}"
                )

            # 总结
            high_days = sum(1 for r in results if "HIGH" in r)
            summary   = (
                f"⚡ 未来7天有 {high_days} 天高需求，建议提前锁定高价策略"
                if high_days >= 3 else
                f"📊 未来7天需求平稳，维持常规定价策略"
            )

            output = [
                f"🔮 LightGBM 需求预测（{start.strftime('%Y-%m-%d')} 起7天）",
                f"   输入：border_flow={border_flow:.2f} | 天气={weather:.2f} | "
                f"竞对价={competitor_price:.0f}MOP | 活动分={event_score:.0f}",
                "",
            ] + results + ["", summary]

            return "\n".join(output)

else:
    class DemandForecastTool:  # type: ignore
        def __init__(self):
            print("  [LightGBM] CrewAI 未安装")
