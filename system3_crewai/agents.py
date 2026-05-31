"""
agents.py — 全球机会情报系统 Agent 定义
=========================================
5个专职Agent，每天协作生成一份全球旅游及酒店行业机会情报报告

  Agent                  LLM              职责
  ─────────────────────  ───────────────  ────────────────────────────────
  GlobalProjectAgent     Perplexity       搜索全球旅游开发项目、基建招标
  HospitalityOpsAgent    Perplexity       酒店行业机会、管理合同、咨询RFP
  EventsTrackerAgent     GPT-4o           展览会、研讨会、投资论坛、峰会
  PolitEconAgent         Claude Sonnet    政治经济背景、政策、市场趋势分析
  ReportWriterAgent      Claude Sonnet    汇总所有信息，生成中英双语日报
"""

from crewai import Agent, LLM
from crewai_tools import FirecrawlSearchTool, FirecrawlScrapeWebsiteTool
import os

try:
    from tools.predicthq_tool import PredictHQTool
    _PREDICTHQ_OK = True
except ImportError:
    _PREDICTHQ_OK = False

try:
    from tools.demand_forecast_tool import DemandForecastTool
    _LGBM_OK = True
except ImportError:
    _LGBM_OK = False

try:
    from tools.amazon_forecast_tool import AmazonForecastTool
    _AWS_OK = True
except ImportError:
    _AWS_OK = False

try:
    from tools.ota_signal_tool import OTASignalTool
    _OTA_OK = True
except ImportError:
    _OTA_OK = False

try:
    from tools.deep_forecast_tool import DeepForecastTool
    _DEEP_OK = True
except ImportError:
    _DEEP_OK = False

try:
    from tools.crypto_trading_tool import (
        CryptoSignalTool, CryptoExecutionTool, CryptoPortfolioTool
    )
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

try:
    from tools.stock_index_tool import (
        StockIndexSignalTool, StockIndexRiskTool, StockIndexPortfolioTool
    )
    _SI_OK = True
except ImportError:
    _SI_OK = False

try:
    from tools.bond_tool import (
        BondYieldCurveTool, BondSignalTool, BondRiskTool
    )
    _BOND_OK = True
except ImportError:
    _BOND_OK = False

try:
    from tools.fx_trading_tool import (
        FXSignalTool, FXRiskTool, FXPaperTradeTool
    )
    _FX_OK = True
except ImportError:
    _FX_OK = False


def _is_valid(key: str) -> bool:
    return bool(key) and "your_" not in key and len(key) > 8


def _make_llm(model: str, key_env: str,
              fallback_model: str = "gpt-4o-mini",
              fallback_env: str = "OPENAI_API_KEY") -> LLM | None:
    key = os.getenv(key_env, "")
    if _is_valid(key):
        try:
            return LLM(model=model, api_key=key, temperature=0.2,
                       max_completion_tokens=3000)
        except Exception as e:
            print(f"  [LLM] {model} 失败: {e}")
    fb = os.getenv(fallback_env, "")
    if _is_valid(fb):
        try:
            return LLM(model=fallback_model, api_key=fb, temperature=0.2,
                       max_completion_tokens=3000)
        except Exception:
            pass
    return None


def _perplexity_llm() -> LLM | None:
    key = os.getenv("PERPLEXITY_API_KEY", "")
    if not _is_valid(key):
        return None
    try:
        return LLM(
            model="perplexity/sonar-pro",
            api_key=key,
            base_url="https://api.perplexity.ai",
            temperature=0.2,
            max_completion_tokens=1200,   # 限制输出长度，防止上下文超限
        )
    except Exception as e:
        print(f"  [LLM] Perplexity 失败: {e}")
        return None


def build_agents() -> dict:
    fc_key = os.getenv("FIRECRAWL_API_KEY", "")
    search_tool    = FirecrawlSearchTool(api_key=fc_key)
    scrape_tool    = FirecrawlScrapeWebsiteTool(api_key=fc_key)
    predicthq_tool  = PredictHQTool() if (_PREDICTHQ_OK and
        _is_valid(os.getenv("PREDICTHQ_API_KEY", ""))) else None
    lgbm_tool       = DemandForecastTool() if _LGBM_OK else None
    aws_fc_tool     = AmazonForecastTool() if (_AWS_OK and
        _is_valid(os.getenv("AWS_ACCESS_KEY_ID", ""))) else None
    ota_tool        = OTASignalTool() if _OTA_OK else None
    deep_tool       = DeepForecastTool() if _DEEP_OK else None
    crypto_signal_tool  = CryptoSignalTool()    if _CRYPTO_OK else None
    crypto_exec_tool    = CryptoExecutionTool() if _CRYPTO_OK else None
    crypto_port_tool    = CryptoPortfolioTool() if _CRYPTO_OK else None
    si_signal_tool      = StockIndexSignalTool()    if _SI_OK else None
    si_risk_tool        = StockIndexRiskTool()      if _SI_OK else None
    si_portfolio_tool   = StockIndexPortfolioTool() if _SI_OK else None
    bond_yc_tool        = BondYieldCurveTool()      if _BOND_OK else None
    bond_signal_tool    = BondSignalTool()          if _BOND_OK else None
    bond_risk_tool      = BondRiskTool()            if _BOND_OK else None
    fx_signal_tool      = FXSignalTool()            if _FX_OK else None
    fx_risk_tool        = FXRiskTool()              if _FX_OK else None
    fx_trade_tool       = FXPaperTradeTool()        if _FX_OK else None
    extra_tools     = [t for t in [predicthq_tool, lgbm_tool, aws_fc_tool, ota_tool, deep_tool] if t]
    crypto_tools    = [t for t in [crypto_signal_tool, crypto_exec_tool, crypto_port_tool] if t]
    si_tools        = [t for t in [si_signal_tool, si_risk_tool, si_portfolio_tool] if t]
    bond_tools      = [t for t in [bond_yc_tool, bond_signal_tool, bond_risk_tool] if t]
    fx_tools        = [t for t in [fx_signal_tool, fx_risk_tool, fx_trade_tool] if t]

    # LLM 优先级：Claude（已验证有效）→ DeepSeek（便宜高效）→ GPT-4o（备用）
    llm_claude   = _make_llm("claude-sonnet-4-5", "ANTHROPIC_API_KEY",
                              "deepseek/deepseek-chat", "DEEPSEEK_API_KEY")
    llm_deepseek = _make_llm("deepseek/deepseek-chat", "DEEPSEEK_API_KEY",
                              "claude-sonnet-4-5", "ANTHROPIC_API_KEY")

    def _kw(llm):
        return {"llm": llm} if llm else {}

    # ── Agent 1：全球旅游开发项目搜索（GPT-4o + Firecrawl实时抓取）──
    global_project_agent = Agent(
        role="全球旅游开发项目情报专员",
        goal=(
            "每日搜索全球旅游规划、度假区开发、酒店建设、旅游基础设施"
            "等相关项目的最新动态，重点关注招标、可行性研究、项目融资、"
            "PPP合作等机会，覆盖亚太、中东、非洲、欧洲、美洲等主要市场。"
        ),
        backstory=(
            "您是InsightBridge Global的全球业务拓展情报分析师，"
            "专门追踪全球旅游开发项目机会。您熟悉世界银行、亚开行、"
            "各国旅游局发布的项目信息，能识别哪些项目需要管理咨询服务。"
        ),
        tools=[search_tool, scrape_tool],
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    )

    # ── Agent 2：酒店及咨询行业机会（Claude + Firecrawl）────────────
    hospitality_ops_agent = Agent(
        role="酒店与旅游咨询行业机会分析师",
        goal=(
            "搜索全球酒店管理合同机会、旅游战略规划咨询RFP、"
            "酒店品牌扩张计划、度假村特许经营招募，以及各类"
            "旅游咨询项目招标信息。同时追踪顶级咨询公司在旅游"
            "领域的最新布局，识别InsightBridge Global可参与的机会。"
        ),
        backstory=(
            "您是InsightBridge Global的业务开发顾问，深耕酒店管理"
            "咨询和旅游战略规划市场。您了解国际酒店品牌（万豪、希尔顿、"
            "洲际等）的扩张策略，也熟悉政府旅游局委托咨询项目的运作方式。"
        ),
        tools=[search_tool, scrape_tool],
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    )

    # ── Agent 3：活动与展会追踪（Claude）────────────────────────────
    events_tracker_agent = Agent(
        role="旅游行业活动与展会情报追踪官",
        goal=(
            "追踪未来30天内全球旅游和酒店行业的重要活动：\n"
            "- 行业展览会（ITB、ATM、FITUR、WTM等）\n"
            "- 投资论坛与路演（旅游投资峰会）\n"
            "- 政府旅游规划研讨会\n"
            "- 酒店行业峰会与颁奖典礼\n"
            "- 招商引资说明会\n"
            "提供活动名称、时间、地点、参与价值评估。"
        ),
        backstory=(
            "您是InsightBridge Global的市场活动情报专员，"
            "负责识别公司应该参与的行业活动。您评估每个活动的"
            "商业价值：潜在客户质量、网络效应、演讲/展览机会。"
        ),
        tools=[search_tool] + extra_tools,
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    )

    # ── Agent 4：政治经济背景分析（Claude Sonnet）───────────────────
    polit_econ_agent = Agent(
        role="旅游行业政治经济环境分析师",
        goal=(
            "分析影响全球旅游和酒店行业的政治经济因素：\n"
            "- 各国旅游政策新动向（签证放开、旅游专项资金）\n"
            "- 重大经济事件对旅游业的影响\n"
            "- 地缘政治变化带来的旅游市场机遇或风险\n"
            "- 新兴目的地市场崛起信号\n"
            "- 国际组织（UNWTO、WTTC）最新报告与预测\n"
            "为InsightBridge Global提供宏观战略判断。"
        ),
        backstory=(
            "您是InsightBridge Global的宏观战略顾问，"
            "专长将政治经济动态转化为旅游行业的商业洞察。"
            "您帮助公司判断哪些市场正在开放、哪些即将爆发，"
            "以便提前布局咨询服务。"
        ),
        tools=[search_tool],
        verbose=False,
        allow_delegation=False,
        **_kw(llm_claude),
    )

    # ── Agent 5：日报撰写（Claude Sonnet）───────────────────────────
    report_writer_agent = Agent(
        role="InsightBridge Global 全球机会日报编辑",
        goal=(
            "将以上4个Agent的搜索结果整合成一份专业、简洁、可执行的"
            "中文日报。报告结构清晰，突出最值得关注的3-5个机会，"
            "每条机会注明：来源、截止时间（如有）、建议行动。"
            "报告面向Dr. Tong Yin，风格专业但简明，避免冗长。"
        ),
        backstory=(
            "您是InsightBridge Global的首席报告编辑，负责将"
            "复杂的全球情报提炼成高管可读的每日简报。"
            "您的报告直接支持Dr. Tong Yin的业务决策，"
            "因此准确性和可操作性是最高标准。"
        ),
        tools=[],
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    )

    # ── Agent 6：加密货币量化交易（Claude Sonnet + Crypto Tools）───
    crypto_trading_agent = Agent(
        role="加密货币量化交易执行官",
        goal=(
            "运用多层量化信号体系（RegimeEngine / FragilityEngine / "
            "SignalEngine / ExecutionGate）实时分析 BTC、ETH、SOL 市场微结构，"
            "识别高置信度短线交易机会，执行开仓/平仓决策，管理风险敞口。\n"
            "核心职责：\n"
            "1. 运行 CryptoSignalTool 获取实时信号（方向分 + 确信度 + 执行门控）\n"
            "2. 仅在 gate_action=ALLOW 或 ALLOW_REDUCED 时提议开仓\n"
            "3. 监控持仓：时间止损（BTC≤35分钟）、分级风险止损\n"
            "4. 生成简洁的交易决策报告"
        ),
        backstory=(
            "您是InsightBridge Global的量化交易执行官，"
            "掌握多资产加密货币短线策略。您的机器人架构来自"
            "经过严格回测的六层系统：数据层→市场状态层→"
            "事件/脆弱度层→信号层→风险层→执行门控层。\n"
            "您严格遵守风险优先原则：每日最大亏损-5%触发自动停止；"
            "高脆弱度（HIGH_FRAGILITY）绝不开仓；"
            "每次平仓后执行5分钟冷静期。"
        ),
        tools=crypto_tools,
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    ) if crypto_tools else None

    # ── Agent 7：股指期货量化交易（Claude Sonnet + StockIndex Tools）──
    stock_index_agent = Agent(
        role="股指期货量化交易策略师",
        goal=(
            "运用两套量化策略系统分析全球股指期货市场（US：ES/NQ/YM/RTY；"
            "CN：IF/IC/IH/IM），识别高概率交易机会并管理风险：\n\n"
            "① 趋势跟随（Momentum）：\n"
            "  - 通过 StockIndexSignalTool(mode=momentum) 获取多品种信号\n"
            "  - 五条件确认：EMA对齐 + ADX>22 + RSI范围 + 量比 + VWAP位置\n"
            "  - 市场状态检测：TREND/NORMAL/BLOCKED/EVENT\n"
            "  - ATR-based 止损（1.5倍ATR）+ 时间止损（ES≤120分钟）\n\n"
            "② 统计套利（Stat Arb）：\n"
            "  - 通过 StockIndexSignalTool(mode=stat_arb) 运行Kalman协整分析\n"
            "  - Engle-Granger 协整检验 + 卡尔曼滤波动态β估计\n"
            "  - Z-Score 信号：|Z|>2 进场，|Z|<0.5 均值回归平仓\n\n"
            "风控约束（不可违反）：\n"
            "  - 交易前必须通过 StockIndexRiskTool 检查（kill_switch / 日损 / 连亏）\n"
            "  - 每日最大亏损 3%，连续亏损 4 笔自动停手"
        ),
        backstory=(
            "您是InsightBridge Global的股指期货量化策略师，"
            "精通趋势跟随与统计套利两套体系。\n"
            "趋势策略移植自WTI原油机器人（RegimeService+SignalService+RiskService）"
            "的量化确认逻辑；套利策略融合协整理论与卡尔曼滤波，"
            "动态追踪ES/NQ、IF/IC等高相关品种对的价差回归机会。\n"
            "您坚守'风控是独立守门人'原则：任何信号都必须通过"
            "独立风控检查才能执行。"
        ),
        tools=si_tools,
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    ) if si_tools else None

    # ── Agent 8：国债 / 利率期货量化交易（Claude Sonnet + Bond Tools）──
    bond_agent = Agent(
        role="国债与利率期货量化交易分析师",
        goal=(
            "运用 Nelson-Siegel 收益率曲线模型、Ispread 均值回归策略和"
            "全面债券风险分析体系，实时监控美国国债市场，识别高置信度"
            "利率交易机会并管理久期风险。\n\n"
            "核心职责：\n"
            "① 收益率曲线分析（BondYieldCurveTool）：\n"
            "  - Nelson-Siegel β₀/β₁/β₂ 因子提取（水平/斜率/曲率）\n"
            "  - 曲线形态判断：正常/倒挂/隆起，Steepener/Flattener 交易机会\n"
            "  - 实时 3M/2Y/5Y/10Y/30Y 收益率监控\n\n"
            "② 交易信号生成（BondSignalTool）：\n"
            "  - Ispread 均值回归：(WTI/10Y收益率)×0.85；>15 做空债券，<10 做多\n"
            "  - 黑天鹅检测：10Y收益率变化>12% 触发 HALT，>5% 触发 WARNING\n"
            "  - 国债拍卖日历：高冲击拍卖前后调整持仓\n\n"
            "③ 风险与压力测试（BondRiskTool）：\n"
            "  - DV01 / Modified Duration / Convexity 计算\n"
            "  - 历史情景：2008危机 / COVID 3月 / 加息冲击\n"
            "  - 历史模拟 VaR（95%/99%）+ CVaR"
        ),
        backstory=(
            "您是InsightBridge Global的固定收益量化分析师，"
            "专精美国国债市场和利率期货交易。\n"
            "您的分析框架直接移植自 Interest-rate-bond 机器人架构，"
            "叠加学术级 Nelson-Siegel 收益率曲线拟合与 Ispread 策略。\n"
            "您坚守固定收益交易的铁律：久期风险优先管理；"
            "黑天鹅事件（yield spike >12%）立即停止所有新单；"
            "拍卖日前后72小时谨慎操作，避免供给冲击。\n"
            "您的目标不只是捕捉收益，更是在利率波动周期中"
            "为投资组合提供精准的风险对冲方案。"
        ),
        tools=bond_tools,
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    ) if bond_tools else None

    # ── Agent 9：外汇量化交易（Claude Sonnet + FX Tools）────────────
    fx_agent = Agent(
        role="外汇量化交易执行官",
        goal=(
            "运用完整的外汇量化交易框架分析 AUD/USD、NZD/USD、EUR/USD、"
            "GBP/USD、USD/JPY 等主要货币对，识别高置信度趋势跟随与均值回归"
            "交易机会，并通过严格的执行闸门管理开平仓风险。\n\n"
            "核心职责：\n"
            "① 市场状态识别（FXSignalTool）：\n"
            "  - RegimeEngine：TREND(ADX>25) / RANGE / EVENT / UNSTABLE\n"
            "  - TREND 策略：price>SMA20>SMA50 + RSI<70 做多；反之做空\n"
            "  - RANGE 策略：价格贴近 Bollinger 下轨+RSI<35 做多；上轨+RSI>65 做空\n"
            "  - P0-P6 执行闸门：Kill→系统安全→市场恶化→冷却→事件→组合→时间止损→信号审批\n\n"
            "② 风险管理（FXRiskTool）：\n"
            "  - StrategyMonitor：连亏检测(连亏≥6冻结) + 渐进恢复(30%→50%→75%→GREEN)\n"
            "  - 日 VaR 监控（95%历史模拟）\n"
            "  - 高影响事件日历：FOMC/NFP/CPI/RBA/BOE/ECB/BOJ 决议前禁止开仓\n\n"
            "③ 纸盘执行（FXPaperTradeTool）：\n"
            "  - 开仓自动设置 ATR 止损/止盈（1:1.5 风险回报比）\n"
            "  - 40分钟时间止损（执行闸门 P5）\n"
            "  - 每日最大亏损 2% 触发日内停止\n\n"
            "风控铁律：\n"
            "  - 事件前 48h 对受影响品种禁止新开仓\n"
            "  - 连亏 4 笔降至 50% 风险；连亏 6 笔冻结\n"
            "  - 市场 ATR 飙升超 3.5 倍基线 → UNSTABLE，强制平仓"
        ),
        backstory=(
            "您是InsightBridge Global的外汇量化交易执行官，"
            "精通货币市场微结构和技术交易策略。\n"
            "您的交易系统直接移植自 Foreign-Currency-main 机器人架构，"
            "专注于 AUD/USD 和 NZD/USD 商品货币对（与WTI原油高度相关），"
            "同时覆盖 EUR/USD、GBP/USD、USD/JPY 主要对。\n"
            "您的风控哲学：执行闸门永远高于信号，连亏时强制降档而非扛单，"
            "高影响经济数据发布前主动回避市场。"
        ),
        tools=fx_tools,
        verbose=True,
        allow_delegation=False,
        **_kw(llm_claude),
    ) if fx_tools else None

    # 打印配置
    def label(llm):
        if llm is None: return "无LLM"
        m = getattr(llm, "model", "")
        if "sonar" in m or "perplexity" in m: return "Perplexity(联网)"
        if "claude" in m: return "Claude Sonnet"
        if "gpt-4o-mini" in m: return "GPT-4o-mini"
        if "gpt-4o" in m: return "GPT-4o"
        return m

    aws_status    = "✓ AWS已配置" if aws_fc_tool else "未配置"
    phq_status    = "✓ PredictHQ" if predicthq_tool else "未配置"
    lgb_status    = "✓ LightGBM" if lgbm_tool else "未安装"
    crypto_status = "✓ Binance Futures (PAPER)" if crypto_tools else "未安装"
    if _CRYPTO_OK and os.getenv("BINANCE_API_KEY", ""):
        crypto_status = "✓ Binance Futures (LIVE)"
    si_status   = "✓ yfinance ES/NQ/IF/IC" if si_tools else "未安装"
    bond_status = "✓ yfinance 国债收益率曲线" if bond_tools else "未安装"
    fx_status   = "✓ yfinance AUD/EUR/GBP/JPY" if fx_tools else "未安装"

    print(f"  ┌─ 全球机会情报系统 Agent 配置 {'─'*30}")
    print(f"  │  GlobalProject    → {label(global_project_agent.llm)} + Firecrawl搜索")
    print(f"  │  HospitalityOps   → {label(hospitality_ops_agent.llm)} + Firecrawl搜索")
    print(f"  │  EventsTracker    → {label(events_tracker_agent.llm)} + {phq_status}")
    print(f"  │  PolitEcon        → {label(polit_econ_agent.llm)}")
    print(f"  │  ReportWriter     → {label(report_writer_agent.llm)}")
    print(f"  │  CryptoTrading    → {label(crypto_trading_agent.llm if crypto_trading_agent else None)} + {crypto_status}")
    print(f"  │  StockIndex       → {label(stock_index_agent.llm if stock_index_agent else None)} + {si_status}")
    print(f"  │  BondAgent        → {label(bond_agent.llm if bond_agent else None)} + {bond_status}")
    print(f"  │  FXAgent          → {label(fx_agent.llm if fx_agent else None)} + {fx_status}")
    print(f"  │  需求预测工具     → {lgb_status} | {phq_status} | {aws_status}(Bedrock)")
    print(f"  └{'─'*54}")

    result = {
        "global_project":   global_project_agent,
        "hospitality_ops":  hospitality_ops_agent,
        "events_tracker":   events_tracker_agent,
        "polit_econ":       polit_econ_agent,
        "report_writer":    report_writer_agent,
    }
    if crypto_trading_agent:
        result["crypto_trading"] = crypto_trading_agent
    if stock_index_agent:
        result["stock_index"] = stock_index_agent
    if bond_agent:
        result["bond"] = bond_agent
    if fx_agent:
        result["fx"] = fx_agent
    return result
