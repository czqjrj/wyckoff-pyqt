#!/usr/bin/env python3
"""威科夫九大检验点综合分析工具"""

import sys
from collections import defaultdict

import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.utils import normalize_symbol


def comprehensive_wyckoff_analysis(code, datalen=500, horizon=20, cost=0.004):
    """综合威科夫九大检验点分析

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
        return {"error": "数据不足", "note": "样本过短"}

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

    # 特别关注8项的情况
    eight_buy_results = [r for r in all_results if r["buy_passed"] >= 8]
    eight_sell_results = [r for r in all_results if r["sell_passed"] >= 8]

    eight_buy_win_rate = 0
    eight_sell_win_rate = 0
    eight_buy_avg = 0
    eight_sell_avg = 0

    if eight_buy_results:
        buy_returns = [r["return"] for r in eight_buy_results]
        eight_buy_win_rate = (np.array(buy_returns) > 0).mean() * 100
        eight_buy_avg = np.mean(buy_returns) * 100

    if eight_sell_results:
        sell_returns = [r["return"] for r in eight_sell_results]
        eight_sell_win_rate = (np.array(sell_returns) > 0).mean() * 100
        eight_sell_avg = np.mean(sell_returns) * 100

    return {
        "total_samples": len(all_results),
        "buy_win_rates": buy_win_rates,
        "sell_win_rates": sell_win_rates,
        "eight_buy_win_rate": eight_buy_win_rate,
        "eight_sell_win_rate": eight_sell_win_rate,
        "eight_buy_avg": eight_buy_avg,
        "eight_sell_avg": eight_sell_avg,
        "eight_buy_count": len(eight_buy_results),
        "eight_sell_count": len(eight_sell_results)
    }


def get_wyckoff_recommendations(code):
    """获取威科夫分析建议"""
    result = comprehensive_wyckoff_analysis(code)

    recommendations = []

    if "error" in result:
        recommendations.append(f"分析失败: {result['error']}")
        return recommendations

    # 8项检验点胜率分析
    if result["eight_buy_win_rate"] > 0:
        recommendations.append(f"买入侧满足8项检验点胜率: {result['eight_buy_win_rate']:.1f}%")
        recommendations.append(f"买入侧平均收益: {result['eight_buy_avg']:.2f}%")
        recommendations.append(f"满足8项的次数: {result['eight_buy_count']}")

    if result["eight_sell_win_rate"] > 0:
        recommendations.append(f"卖出侧满足8项检验点胜率: {result['eight_sell_win_rate']:.1f}%")
        recommendations.append(f"卖出侧平均收益: {result['eight_sell_avg']:.2f}%")
        recommendations.append(f"满足8项的次数: {result['eight_sell_count']}")

    # 整体表现分析
    if result["buy_win_rates"]:
        max_buy_passed = max(result["buy_win_rates"].keys())
        max_buy_win = result["buy_win_rates"][max_buy_passed]["win_rate"]
        recommendations.append(f"最高通过数买入侧胜率: {max_buy_win:.1f}% ({max_buy_passed}项)")

    if result["sell_win_rates"]:
        max_sell_passed = max(result["sell_win_rates"].keys())
        max_sell_win = result["sell_win_rates"][max_sell_passed]["win_rate"]
        recommendations.append(f"最高通过数卖出侧胜率: {max_sell_win:.1f}% ({max_sell_passed}项)")

    # 建议
    if result["eight_buy_win_rate"] < 30 and result["eight_sell_win_rate"] < 30:
        recommendations.append("[警告] 8项检验点胜率偏低，建议结合其他技术指标使用")
    elif result["eight_buy_win_rate"] > 50 or result["eight_sell_win_rate"] > 50:
        recommendations.append("[建议] 8项检验点胜率较高，可考虑作为参考信号")
    else:
        recommendations.append("[提示] 8项检验点胜率中等，需谨慎使用")

    return recommendations


if __name__ == "__main__":
    # 默认测试代码
    stock_code = "sh600036"

    # 如果提供了命令行参数，使用第一个参数作为股票代码
    if len(sys.argv) > 1:
        stock_code = sys.argv[1]

    print("=== 威科夫九大检验点综合分析 ===")
    print(f"股票代码: {stock_code}")
    print()

    # 获取推荐建议
    recommendations = get_wyckoff_recommendations(stock_code)

    for rec in recommendations:
        print(rec)

    print()
    print("=== 详细统计数据 ===")
    result = comprehensive_wyckoff_analysis(stock_code)

    if "error" not in result:
        print(f"总回测样本数: {result['total_samples']}")
        print("各检验点通过数胜率:")
        for i in sorted(result["buy_win_rates"].keys()):
            stats = result["buy_win_rates"][i]
            print(f"  买入侧{i}项通过: {stats['win_rate']:.1f}% (样本数: {stats['count']})")

        print("各检验点通过数胜率:")
        for i in sorted(result["sell_win_rates"].keys()):
            stats = result["sell_win_rates"][i]
            print(f"  卖出侧{i}项通过: {stats['win_rate']:.1f}% (样本数: {stats['count']})")
