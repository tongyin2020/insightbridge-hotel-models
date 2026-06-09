#!/bin/bash
# CrewAI + Firecrawl + AgentOps 并行模拟 — 一键启动
# 用法：bash START_CREWAI.sh

cd "$(dirname "$0")"

echo ""
echo "======================================================"
echo "  澳门酒店AI — CrewAI+Firecrawl 并行模拟"
echo "======================================================"
echo ""

# 检查Python依赖
echo "检查依赖..."
python3 -c "import crewai" 2>/dev/null || {
    echo "安装依赖..."
    pip3 install -r requirements.txt -q
}

# 检查 .env 配置
if grep -q "your_agentops_key_here" .env; then
    echo "⚠  AgentOps未配置（可选）: 请在.env中填入AGENTOPS_API_KEY"
    echo "   去 https://app.agentops.ai 注册免费账号"
fi
ANTHROPIC_KEY=$(grep '^ANTHROPIC_API_KEY=' .env 2>/dev/null | cut -d= -f2-)
OPENAI_KEY=$(grep '^OPENAI_API_KEY=' .env 2>/dev/null | cut -d= -f2-)

if { [ -z "$ANTHROPIC_KEY" ] || [[ "$ANTHROPIC_KEY" == your_* ]]; } && \
   { [ -z "$OPENAI_KEY" ] || [[ "$OPENAI_KEY" == your_* ]]; }; then
    echo "ℹ  未检测到主力LLM Key: CrewAI将仅运行数据采集+模型部分（无LLM分析）"
    echo "   这对数据对比没有影响，LLM分析是可选功能"
fi

echo ""
echo "✓  数据采集: Firecrawl（border_flow/zhuhai/ota_pace）+ Playwright"
echo "✓  测试对象: 145家2-3星 + 280家4-5星 = 425家酒店"
echo "✓  每小时: 570次模型调用 + Firecrawl抓取"
echo "✓  运行: 21天 / 504小时"
echo "✓  对比基线: ../simulation_test/results.db"
echo ""

# 检查是否已在运行
if [ -f crewai.pid ]; then
    PID=$(cat crewai.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "⚠  已在运行 (PID: $PID)"
        echo "   查看进度: tail -f crewai_simulation.log"
        echo "   查看对比: python3 compare_report.py"
        exit 1
    fi
fi

# 启动
nohup python3 main.py > crewai_simulation.log 2>&1 &
PID=$!
echo $PID > crewai.pid

echo "✅ 已启动！PID: $PID"
echo ""
echo "常用命令："
echo "  实时进度:  tail -f crewai_simulation.log"
echo "  对比报告:  python3 compare_report.py"
echo "  停止:      kill \$(cat crewai.pid)"
echo ""
