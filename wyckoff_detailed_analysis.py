#!/usr/bin/env python3
"""威科夫九大检验点回测结果 - 详细版"""

from collections import defaultdict

import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.utils import normalize_symbol


def detailed_wyckoff_analysis(code, datalen=500, horizon=20, cost=0.004):
    """详细的威科夫九大检验点分析

    Args:
        code: 股票代码
        datalen: 数据长度
        horizon: 回测持有周期
        cost: 交易成本

    Returns:
        dict: 详细分析结果
    """
    symbol = normalize_symbol(code)
    df = fetch_kline(symbol, datalen=datalen, scale=240)
    # 添加必要的技术指标
    df = add_indicators(df, symbol=symbol)

    if len(df) < 150:
        return {"error": "数据不足"}

    # 存储所有检验点通过情况
    all_results = []

    # 从第90根K线开始回测
    for i in range(90, len(df) - horizon):
        # 用截至当前时刻的数据进行分析
        wdf = df.iloc[:i+1]
        # 添加必要的技术指标
        wdf = add_indicators(wdf, symbol=symbol)
        wpivots = find_pivots(wdf, order=6)
        wevents = detect_all(wdf, wpivots)

        # 计算九大检验点
        nt = nine_tests(wdf, wevents, wpivots)

        # 计算买入和卖出侧通过的检验点数量
        buy_passed = nt["buy_passed"]
        sell_passed = nt["sell_passed"]

        # 计算收益
        end_idx = min(i + horizon, len(df) - 1)
        close = df["close"].values

        # 计算持有收益（费后）
        if end_idx < len(close):
            ret = (close[end_idx] / close[i + 1] - 1) - cost
        else:
            continue

        # 存储所有结果
        all_results.append({
            "buy_passed": buy_passed,
            "sell_passed": sell_passed,
            "return": ret
        })

    if not all_results:
        return {"error": "没有有效回测数据"}

    # 统计各通过数的胜率
    buy_stats = defaultdict(list)
    sell_stats = defaultdict(list)

    for item in all_results:
        buy_stats[item["buy_passed"]].append(item["return"])
        sell_stats[item["sell_passed"]].append(item["return"])

    # 计算各个通过数的胜率
    buy_win_rates = {}
    for passed, returns in buy_stats.items():
        if len(returns) >= 3:
            win_rate = (np.array(returns) > 0).mean() * 100
            avg_return = np.mean(returns) * 100
            buy_win_rates[passed] = {
                "count": len(returns),
                "win_rate": win_rate,
                "avg_return": avg_return
            }

    sell_win_rates = {}
    for passed, returns in sell_stats.items():
        if len(returns) >= 3:
            win_rate = (np.array(returns) > 0).mean() * 100
            avg_return = np.mean(returns) * 100
            sell_win_rates[passed] = {
                "count": len(returns),
                "win_rate": win_rate,
                "avg_return": avg_return
            }

    return {
        "total_samples": len(all_results),
        "buy_win_rates": buy_win_rates,
        "sell_win_rates": sell_win_rates,
        "buy_passed_distribution": dict(buy_stats),
        "sell_passed_distribution": dict(sell_stats)
    }


def format_table(result, stock_code):
    """格式化输出表格"""
    print(f"\n=== 威科夫九大检验点回测结果 - {stock_code} ===")
    print(f"总样本数: {result['total_samples']}")
    print()

    print("买入侧检验点通过数胜率统计:")
    print("通过数 | 样本数 | 胜率(%) | 平均收益(%)")
    print("-" * 40)

    # 按通过数排序
    for passed in sorted(result["buy_win_rates"].keys()):
        stats = result["buy_win_rates"][passed]
        print(f"{passed:4d} | {stats['count']:6d} | {stats['win_rate']:7.1f} | {stats['avg_return']:10.2f}")

    print()
    print("卖出侧检验点通过数胜率统计:")
    print("通过数 | 样本数 | 胜率(%) | 平均收益(%)")
    print("-" * 40)

    # 按通过数排序
    for passed in sorted(result["sell_win_rates"].keys()):
        stats = result["sell_win_rates"][passed]
        print(f"{passed:4d} | {stats['count']:6d} | {stats['win_rate']:7.1f} | {stats['avg_return']:10.2f}")


if __name__ == "__main__":
    # 测试几个不同的股票
    test_stocks = ["sh600036", "sz000001", "sh601318"]

    print("=== 威科夫九大检验点详细回测 ===")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()

    for stock in test_stocks:
        print(f"正在分析: {stock}")
        result = detailed_wyckoff_analysis(stock)

        if "error" not in result:
            format_table(result, stock)
        else:
            print(f"  错误: {result['error']}")
        print("-" * 50)
