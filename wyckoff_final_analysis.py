#!/usr/bin/env python3
"""威科夫九大检验点7项年度回测 - 简洁版"""

import numpy as np
from collections import defaultdict
import sys

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol


def simple_annual_analysis(code, datalen=250, horizon=20, cost=0.004):
    """近一年7项回测分析"""
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
            "avg_return": np.mean(buy_returns) * 100
        }
    
    if seven_sell_results:
        sell_returns = [r["return"] for r in seven_sell_results]
        sell_stats = {
            "count": len(seven_sell_results),
            "win_rate": (np.array(sell_returns) > 0).mean() * 100,
            "avg_return": np.mean(sell_returns) * 100
        }
    
    return {
        "buy_stats": buy_stats,
        "sell_stats": sell_stats,
        "total_samples": len(all_results)
    }


if __name__ == "__main__":
    # 测试股票
    stock_code = "sh600036"
    
    print("=== 威科夫九大检验点7项年度回测 ===")
    print(f"股票: {stock_code}")
    print("数据周期: 近一年(约250个交易日)")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()
    
    result = simple_annual_analysis(stock_code, datalen=250)
    
    if "error" not in result:
        print("买入侧7项通过:")
        if result["buy_stats"]:
            print(f"  胜率: {result['buy_stats']['win_rate']:.1f}%")
            print(f"  平均收益: {result['buy_stats']['avg_return']:.2f}%")
            print(f"  样本数: {result['buy_stats']['count']}")
        else:
            print("  无符合条件的样本")
            
        print()
        print("卖出侧7项通过:")
        if result["sell_stats"]:
            print(f"  胜率: {result['sell_stats']['win_rate']:.1f}%")
            print(f"  平均收益: {result['sell_stats']['avg_return']:.2f}%")
            print(f"  样本数: {result['sell_stats']['count']}")
        else:
            print("  无符合条件的样本")
    else:
        print(f"错误: {result['error']}")