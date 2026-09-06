"""威科夫策略管理器 · CLI 演示入口 (拆分自 manager.main)。

仅作独立运行演示; 分析核心逻辑见 manager.analyze_stock / evaluators / candidates。
运行: python -m wyckoff.strategies.cli
"""

from wyckoff.strategies.manager import WyckoffStrategyManager


def main():
    """主函数 - 策略管理演示"""
    print("=== 威科夫策略管理系统（模拟盘纪律策略） ===")
    print()

    # 创建策略管理器
    manager = WyckoffStrategyManager()

    # 加载历史数据
    manager.load_performance_history()

    # 显示策略详情
    print("🔍 策略介绍:")
    print()

    strategy_details = manager.get_strategy_details()
    for key, strategy in strategy_details.items():
        print(f"🎯 {strategy['name']}")
        print(f"   描述: {strategy['description']}")
        print(f"   特点: {', '.join(strategy['characteristics'])}")
        print(f"   条件: {', '.join(strategy['conditions'])}")
        print()

    # 分析股票
    print("📊 正在分析股票...")
    test_stocks = ["sh600036", "sz000001", "sh601318"]

    analysis_results = []
    for stock in test_stocks:
        try:
            result = manager.analyze_stock(stock, datalen=1000)
            analysis_results.append(result)
        except Exception as e:
            print(f"分析 {stock} 时出错: {e}")
            analysis_results.append({"stock": stock, "error": str(e)})

    # 显示分析结果
    print("\n📈 分析结果汇总:")
    print("=" * 60)

    total_strategies = 0
    strategy_counts = {}

    for result in analysis_results:
        if "error" not in result:
            count = len(result["strategies_found"])
            total_strategies += count
            print(f"{result['stock']} ({result['name']}): 发现 {count} 个高胜率信号")

            # 统计各类策略
            for signal in result["strategies_found"]:
                strategy = signal.get("name") or signal["strategy"]
                strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        else:
            print(f"{result['stock']}: 错误 - {result['error']}")

    print(f"\n📋 总计发现高胜率信号: {total_strategies} 个")
    print("各策略分布:")
    for strategy, count in strategy_counts.items():
        print(f"  {strategy}: {count} 个")

    # 显示策略统计
    print("\n📊 策略统计信息:")
    stats = manager.get_strategy_statistics()
    if "message" not in stats:
        print(f"总分析次数: {stats['total_analyses']}")
        print("各策略出现次数:")
        for strategy, count in stats['strategy_totals'].items():
            print(f"  {strategy}: {count} 次")
    else:
        print(stats["message"])

    # 导出报告
    manager.export_strategy_report()

    print("\n✅ 系统功能总结:")
    print("1. 自动识别策略信号")
    print("2. 记录策略表现历史")
    print("3. 提供策略统计分析")
    print("4. 支持策略报告导出")
    print("5. 可扩展添加新策略")


if __name__ == "__main__":
    main()
