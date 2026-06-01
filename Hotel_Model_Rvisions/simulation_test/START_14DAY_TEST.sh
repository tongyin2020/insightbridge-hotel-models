#!/bin/bash
# 21天真实模拟测试 — 一键启动脚本
# 用法：在Terminal中运行: bash START_21DAY_TEST.sh

cd "$(dirname "$0")"

echo ""
echo "=========================================="
echo "  澳门酒店AI模型 21天自动化测试"
echo "=========================================="
echo ""

# 检查是否已在运行
if [ -f simulation.pid ]; then
    PID=$(cat simulation.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "⚠️  测试已在运行中 (PID: $PID)"
        echo "   查看进度: tail -f simulation.log"
        echo "   停止测试: kill $PID"
        exit 1
    else
        rm -f simulation.pid
    fi
fi

# 备份旧数据库（如果存在）
if [ -f results.db ]; then
    BACKUP="results_backup_$(date +%Y%m%d_%H%M%S).db"
    mv results.db "$BACKUP"
    echo "ℹ️  已备份旧数据库: $BACKUP"
fi

echo "✓  启动21天测试（每小时一次，共504次）"
echo "✓  测试对象：4家2-3星 + 3家4-5星 = 每小时7次调用"
echo "✓  总计运行次数：3,528次"
echo "✓  实时数据：澳门气象（wttr.in）"
echo "✓  结果存储：results.db"
echo ""
echo "运行日志: simulation.log"
echo ""

# 后台启动
nohup python3 run_simulation.py > simulation.log 2>&1 &
PID=$!
echo $PID > simulation.pid

echo "✅ 已启动！PID: $PID"
echo ""
echo "常用命令："
echo "  查看实时进度:  tail -f simulation.log"
echo "  查看当前报告:  python3 report.py"
echo "  停止测试:      kill $PID  (或: kill \$(cat simulation.pid))"
echo ""
