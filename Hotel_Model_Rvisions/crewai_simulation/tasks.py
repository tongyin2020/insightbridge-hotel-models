"""
tasks.py — CrewAI Task 定义
每小时执行一轮，共5个任务，顺序执行。
"""

from crewai import Task


def build_tasks(agents: dict, hour: int, signal_context: str, fc_context: str) -> list[Task]:
    """
    构建当前小时的任务列表。
    signal_context: JSON字符串，包含实时市场信号（天气/渡轮/活动）
    fc_context: JSON字符串，包含Firecrawl抓取的增强信号
    """

    # ── Task 1：数据采集报告 ────────────────────────────────────────
    t1_data = Task(
        description=f"""
        当前小时：{hour}
        已有基础市场信号（Playwright抓取）：{signal_context}
        已有Firecrawl增强信号：{fc_context}

        请综合以上数据，生成本小时的完整市场信号报告，包括：
        1. 确认哪些信号来自真实数据（标注来源）
        2. 哪些仍需统计模拟（说明原因）
        3. border_flow、zhuhai_saturation、ota_booking_pace 的最终采用值及可信度
        4. 与上一小时相比是否有显著变化

        输出格式为JSON，包含所有9个需求因子的最终值和来源标注。
        """,
        expected_output=(
            "JSON格式的完整市场信号报告，包含9个因子值、来源标注、可信度评分(0-1)。"
            "示例: {\"border_flow\": {\"value\": 0.65, \"source\": \"firecrawl_fsm\", \"confidence\": 0.7}, ...}"
        ),
        agent=agents["data_harvester"],
    )

    # ── Task 2：MARE定价分析 ────────────────────────────────────────
    t2_mare = Task(
        description=f"""
        基于Task 1生成的市场信号，分析MARE房价优化模型对澳门2-3星酒店的定价影响。

        重点分析（无需逐家计算，由Python代码处理）：
        1. 本小时哪个场景类别（normal/peak/crisis/market_shock/stress）最多酒店被分配到？
        2. 当前 border_flow 信号（来自Firecrawl）vs 统计模拟值 —— 差异是否影响定价区间？
        3. 竞对价格（Booking.com实时）今日处于什么水平，对MARE价格上限有何影响？
        4. 本小时预计2-3星推荐价区间（低/中/高分位）

        本小时实际运行数据（来自Python模型）：{signal_context}
        """,
        expected_output=(
            "MARE定价分析报告：包含当前市场状态评估、border_flow对定价的影响量化、"
            "2-3星推荐价预测区间（MOP最低-最高）、与Playwright基线的比较摘要。"
        ),
        agent=agents["mare"],
        context=[t1_data],
    )

    # ── Task 3：CRM集成分析 ────────────────────────────────────────
    t3_crm = Task(
        description=f"""
        分析DirectorAI CRM/PSRS集成模型在本小时的测试结果。

        重点关注：
        1. 14个场景中，PSRS_FAILURE 和 HIGH_CANCELLATION_STORM 场景对CRM识别率的影响
        2. OTA_MONOPOLY 场景下（OTA占95%），直销转化路径是否完全中断？
        3. WhatsApp触达率在PSRS故障时的降幅（预期从94%→18%）
        4. 本小时异常检测触发次数分类（PSRS故障/WhatsApp中断/价格折扣过大）

        当前信号上下文：{signal_context}
        """,
        expected_output=(
            "CRM集成健康报告：各场景下的平均集成评分、PSRS故障率、"
            "WhatsApp送达率、异常触发分类统计。"
        ),
        agent=agents["crm"],
        context=[t1_data],
    )

    # ── Task 4：自主获客分析 ────────────────────────────────────────
    t4_acq = Task(
        description=f"""
        分析4-5星自主获客模型本小时的表现。

        重点验证：
        1. 在 PRICE_WAR 场景（竞对降价40%）下，直销净收益仍然优于OTA净收益吗？
        2. COMPETITOR_SPIKE 场景（竞对涨价80%）时，模型是否正确捕捉溢价机会？
        3. 路凼5星 vs 新口岸4星 —— 直销胜率有何差异？
        4. OTA参考价（来自Booking.com真实4-5星均价MOP {signal_context[:50]}...）对直销定价的约束

        目标验证：直销胜出率应在75%-90%之间（过高可能是折扣过大）
        """,
        expected_output=(
            "自主获客分析：直销胜率按场景类别分解、价格战场景下的收益保护分析、"
            "路凼vs新口岸的区域差异、与MakCorps数据接入后的改进预测。"
        ),
        agent=agents["selfacq"],
        context=[t1_data],
    )

    # ── Task 5：双轨对比分析 ────────────────────────────────────────
    t5_compare = Task(
        description=f"""
        对比分析 CrewAI+Firecrawl 版本与 Playwright 基线版本的差异。

        对比维度：
        1. 数据采集覆盖率：
           - Playwright版: weather✓ ferry✓ events✓ visitors✓ booking_prices✓
           - CrewAI+FC版: 以上全部 + 尝试 border_flow / zhuhai_sat / ota_pace
           - 本小时Firecrawl是否成功获取到额外真实数据？成功率？

        2. 若Firecrawl获取到真实border_flow（非0.0默认），
           与统计模拟值（基于时间/假日的估算）差异有多大？

        3. 对模型输出的影响：若border_flow真实值显著不同，
           MARE推荐价会有多少变化？（border_flow权重=0.18，影响较大）

        4. 结论：这21天测试结束后，Firecrawl方案能将真实数据覆盖率
           从当前的~40%提升到多少？

        Firecrawl增强信号：{fc_context}
        """,
        expected_output=(
            "双轨对比报告：数据覆盖率对比表、关键因子真实vs模拟差异量化、"
            "对模型输出影响的敏感性分析、21天后的数据策略建议。"
        ),
        agent=agents["analyst"],
        context=[t1_data, t2_mare, t3_crm, t4_acq],
    )

    return [t1_data, t2_mare, t3_crm, t4_acq, t5_compare]
