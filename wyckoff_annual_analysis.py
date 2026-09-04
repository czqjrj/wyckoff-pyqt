#!/usr/bin/env python3
"""威科夫九大检验点7项回测 - 近一年实盘收益分析"""


import numpy as np

from wyckoff.datasource import fetch_kline
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators, find_pivots
from wyckoff.ninetests import nine_tests
from wyckoff.utils import normalize_symbol


def annual_backtest_analysis(code, datalen=250, horizon=20, cost=0.004):
    """近一年回测分析

    Args:
        code: 股票代码
        datalen: 数据长度 (约250个交易日，即一年)
        horizon: 回测持有周期
        cost: 交易成本

    Returns:
        dict: 年度回测结果
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
            "return": ret,
            "date": df.index[i].strftime('%Y-%m-%d') if hasattr(df.index, 'strftime') else str(i)
        })

    if not all_results:
        return {"error": "没有有效回测数据"}

    # 筛选出7项通过的情况
    seven_buy_results = [r for r in all_results if r["buy_passed"] >= 7]
    seven_sell_results = [r for r in all_results if r["sell_passed"] >= 7]

    # 计算统计信息
    buy_stats = {}
    sell_stats = {}

    if seven_buy_results:
        buy_returns = [r["return"] for r in seven_buy_results]
        buy_stats = {
            "count": len(seven_buy_results),
            "win_rate": (np.array(buy_returns) > 0).mean() * 100,
            "avg_return": np.mean(buy_returns) * 100,
            "max_return": max(buy_returns) * 100,
            "min_return": min(buy_returns) * 100,
            "std_dev": np.std(buy_returns) * 100
        }

    if seven_sell_results:
        sell_returns = [r["return"] for r in seven_sell_results]
        sell_stats = {
            "count": len(seven_sell_results),
            "win_rate": (np.array(sell_returns) > 0).mean() * 100,
            "avg_return": np.mean(sell_returns) * 100,
            "max_return": max(sell_returns) * 100,
            "min_return": min(sell_returns) * 100,
            "std_dev": np.std(sell_returns) * 100
        }

    return {
        "total_samples": len(all_results),
        "seven_buy_stats": buy_stats,
        "seven_sell_stats": sell_stats,
        "seven_buy_results": seven_buy_results,
        "seven_sell_results": seven_sell_results
    }


def format_annual_results(result, stock_code):
    """格式化年度结果"""
    print(f"=== 威科夫九大检验点7项年度回测 - {stock_code} ===")
    print("数据周期: 近一年(约250个交易日)")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()

    if "error" in result:
        print(f"错误: {result['error']}")
        return

    print("买入侧7项通过统计:")
    if result["seven_buy_stats"]:
        stats = result["seven_buy_stats"]
        print(f"  样本数: {stats['count']}")
        print(f"  胜率: {stats['win_rate']:.1f}%")
        print(f"  平均收益: {stats['avg_return']:.2f}%")
        print(f"  最大收益: {stats['max_return']:.2f}%")
        print(f"  最小收益: {stats['min_return']:.2f}%")
        print(f"  收益标准差: {stats['std_dev']:.2f}%")
    else:
        print("  无数据")

    print()
    print("卖出侧7项通过统计:")
    if result["seven_sell_stats"]:
        stats = result["seven_sell_stats"]
        print(f"  样本数: {stats['count']}")
        print(f"  胜率: {stats['win_rate']:.1f}%")
        print(f"  平均收益: {stats['avg_return']:.2f}%")
        print(f"  最大收益: {stats['max_return']:.2f}%")
        print(f"  最小收益: {stats['min_return']:.2f}%")
        print(f"  收益标准差: {stats['std_dev']:.2f}%")
    else:
        print("  无数据")
        if result["seven_sell_results"]:
            print(f"  但有{len(result['seven_sell_results'])}个样本")


if __name__ == "__main__":
    # 测试股票
    stock_code = "sh600036"

    print("正在执行近一年回测...")
    result = annual_backtest_analysis(stock_code, datalen=250)
    format_annual_results(result, stock_code)
