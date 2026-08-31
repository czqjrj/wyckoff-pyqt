#!/usr/bin/env python3
"""威科夫九大检验点回测工具 - 专门计算满足8项时的胜率"""

import numpy as np
from collections import defaultdict
import sys

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol


def analyze_8_of_9_wyckoff(code, datalen=500, horizon=20, cost=0.004):
    """分析满足8项检验点时的胜率
    
    Args:
        code: 股票代码
        datalen: 数据长度
        horizon: 回测持有周期
        cost: 交易成本
    
    Returns:
        dict: 8项检验点情况下的胜率统计
    """
    symbol = normalize_symbol(code)
    df = fetch_kline(symbol, datalen=datalen, scale=240)
    
    if len(df) < 150:
        return {"error": "数据不足", "note": "样本过短"}
    
    # 存储满足8项检验点的结果
    eight_pass_results = []
    
    # 从第90根K线开始回测（避免前期数据不足）
    for i in range(90, len(df) - horizon):
        # 用截至当前时刻的数据进行分析
        wdf = df.iloc[:i+1]
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
            
        # 如果买入侧满足8项或卖出侧满足8项，则记录
        if buy_passed >= 8:
            eight_pass_results.append({
                "type": "buy",
                "passed": buy_passed,
                "return": ret
            })
        elif sell_passed >= 8:
            eight_pass_results.append({
                "type": "sell", 
                "passed": sell_passed,
                "return": ret
            })
    
    # 统计满足8项的胜率
    if not eight_pass_results:
        return {"error": "没有满足8项检验点的情况", "count": 0}
    
    # 按类型分组统计
    buy_eight_results = [r for r in eight_pass_results if r["type"] == "buy"]
    sell_eight_results = [r for r in eight_pass_results if r["type"] == "sell"]
    
    # 计算胜率
    buy_win_rate = 0
    sell_win_rate = 0
    buy_avg_return = 0
    sell_avg_return = 0
    
    if buy_eight_results:
        buy_returns = [r["return"] for r in buy_eight_results]
        buy_win_rate = (np.array(buy_returns) > 0).mean() * 100
        buy_avg_return = np.mean(buy_returns) * 100
    
    if sell_eight_results:
        sell_returns = [r["return"] for r in sell_eight_results]
        sell_win_rate = (np.array(sell_returns) > 0).mean() * 100
        sell_avg_return = np.mean(sell_returns) * 100
    
    return {
        "total_eight_count": len(eight_pass_results),
        "buy_eight_count": len(buy_eight_results),
        "sell_eight_count": len(sell_eight_results),
        "buy_win_rate": buy_win_rate,
        "sell_win_rate": sell_win_rate,
        "buy_avg_return": buy_avg_return,
        "sell_avg_return": sell_avg_return,
        "buy_returns": buy_eight_results if buy_eight_results else [],
        "sell_returns": sell_eight_results if sell_eight_results else []
    }


if __name__ == "__main__":
    # 默认测试代码
    stock_code = "sh600036"
    
    # 如果提供了命令行参数，使用第一个参数作为股票代码
    if len(sys.argv) > 1:
        stock_code = sys.argv[1]
    
    print("=== 威科夫九大检验点回测 ===")
    print(f"股票代码: {stock_code}")
    print(f"回测周期: 20天")
    print(f"交易成本: 0.4%")
    print()
    
    # 分析满足8项检验点时的胜率
    result = analyze_8_of_9_wyckoff(stock_code)
    
    if "error" in result:
        print(f"分析失败: {result['error']}")
    else:
        print("满足8项检验点时的胜率分析:")
        print(f"总满足8项的情况数: {result['total_eight_count']}")
        print(f"买入侧满足8项: {result['buy_eight_count']} 次")
        print(f"卖出侧满足8项: {result['sell_eight_count']} 次")
        print()
        print("买入侧胜率:")
        print(f"  胜率: {result['buy_win_rate']:.2f}%")
        print(f"  平均收益: {result['buy_avg_return']:.2f}%")
        print()
        print("卖出侧胜率:")
        print(f"  胜率: {result['sell_win_rate']:.2f}%")
        print(f"  平均收益: {result['sell_avg_return']:.2f}%")