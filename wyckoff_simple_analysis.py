#!/usr/bin/env python3
"""威科夫九大检验点回测结果 - 简化版"""


import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.utils import normalize_symbol


def simple_wyckoff_analysis(code, datalen=500, horizon=20, cost=0.004):
    """简单威科夫九大检验点分析

    Args:
        code: 股票代码
        datalen: 数据长度
        horizon: 回测持有周期
        cost: 交易成本

    Returns:
        dict: 分析结果
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
        "eight_buy_win_rate": eight_buy_win_rate,
        "eight_sell_win_rate": eight_sell_win_rate,
        "eight_buy_avg": eight_buy_avg,
        "eight_sell_avg": eight_sell_avg,
        "eight_buy_count": len(eight_buy_results),
        "eight_sell_count": len(eight_sell_results),
        "total_samples": len(all_results)
    }


if __name__ == "__main__":
    # 测试几个不同的股票
    test_stocks = ["sh600036", "sz000001", "sh601318"]

    print("=== 威科夫九大检验点回测结果 ===")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()

    for stock in test_stocks:
        print(f"股票: {stock}")
        result = simple_wyckoff_analysis(stock)

        if "error" not in result:
            print(f"  总样本数: {result['total_samples']}")
            print(f"  买入侧8项通过胜率: {result['eight_buy_win_rate']:.1f}% ({result['eight_buy_count']}次)")
            print(f"  卖出侧8项通过胜率: {result['eight_sell_win_rate']:.1f}% ({result['eight_sell_count']}次)")

            if result['eight_buy_win_rate'] > 0:
                print(f"  买入侧平均收益: {result['eight_buy_avg']:.2f}%")
            if result['eight_sell_win_rate'] > 0:
                print(f"  卖出侧平均收益: {result['eight_sell_avg']:.2f}%")
        else:
            print(f"  错误: {result['error']}")
        print()
