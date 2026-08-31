#!/usr/bin/env python3
"""威科夫九大检验点大规模回测 - 多股票组合分析"""

import numpy as np
from collections import defaultdict
import sys

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol


def large_scale_analysis(stocks, datalen=1000, horizon=20, cost=0.004):
    """大规模多股票回测分析
    
    Args:
        stocks: 股票代码列表
        datalen: 数据长度 
        horizon: 回测持有周期
        cost: 交易成本
    
    Returns:
        dict: 综合分析结果
    """
    all_results = []
    stock_stats = {}
    
    for stock_code in stocks:
        print(f"正在分析 {stock_code}...")
        try:
            symbol = normalize_symbol(stock_code)
            df = fetch_kline(symbol, datalen=datalen, scale=240)
            # 添加必要的技术指标
            df = add_indicators(df, symbol=symbol)
            
            if len(df) < 150:
                print(f"  {stock_code} 数据不足")
                continue
            
            # 存储该股票的检验点通过情况
            stock_results = []
            
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
                stock_results.append({
                    "stock": stock_code,
                    "buy_passed": buy_passed,
                    "sell_passed": sell_passed,
                    "return": ret
                })
            
            if stock_results:
                stock_stats[stock_code] = len(stock_results)
                all_results.extend(stock_results)
                print(f"  {stock_code} 完成，共 {len(stock_results)} 个样本")
            else:
                print(f"  {stock_code} 无有效样本")
                
        except Exception as e:
            print(f"  {stock_code} 分析出错: {str(e)}")
    
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
    
    # 特别关注7项的情况
    seven_buy_results = [r for r in all_results if r["buy_passed"] >= 7]
    seven_sell_results = [r for r in all_results if r["sell_passed"] >= 7]
    
    seven_buy_win_rate = 0
    seven_sell_win_rate = 0
    seven_buy_avg = 0
    seven_sell_avg = 0
    
    if seven_buy_results:
        buy_returns = [r["return"] for r in seven_buy_results]
        seven_buy_win_rate = (np.array(buy_returns) > 0).mean() * 100
        seven_buy_avg = np.mean(buy_returns) * 100
    
    if seven_sell_results:
        sell_returns = [r["return"] for r in seven_sell_results]
        seven_sell_win_rate = (np.array(sell_returns) > 0).mean() * 100
        seven_sell_avg = np.mean(sell_returns) * 100
    
    return {
        "total_samples": len(all_results),
        "stock_counts": stock_stats,
        "buy_win_rates": buy_win_rates,
        "sell_win_rates": sell_win_rates,
        "seven_buy_win_rate": seven_buy_win_rate,
        "seven_sell_win_rate": seven_sell_win_rate,
        "seven_buy_avg": seven_buy_avg,
        "seven_sell_avg": seven_sell_avg,
        "seven_buy_count": len(seven_buy_results),
        "seven_sell_count": len(seven_sell_results)
    }


def format_large_scale_results(result):
    """格式化大规模分析结果"""
    print("\n=== 大规模多股票回测分析结果 ===")
    print(f"总样本数: {result['total_samples']}")
    print("分析的股票:")
    for stock, count in result['stock_counts'].items():
        print(f"  {stock}: {count} 个样本")
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
    
    print()
    print("=== 关键结果 ===")
    print(f"买入侧7项通过: {result['seven_buy_win_rate']:.1f}% 胜率, 平均收益 {result['seven_buy_avg']:.2f}%")
    print(f"卖出侧7项通过: {result['seven_sell_win_rate']:.1f}% 胜率, 平均收益 {result['seven_sell_avg']:.2f}%")
    print(f"买入侧7项样本数: {result['seven_buy_count']}")
    print(f"卖出侧7项样本数: {result['seven_sell_count']}")


if __name__ == "__main__":
    # 多只股票进行大规模回测
    test_stocks = [
        "sh600036",  # 招商银行
        "sz000001",  # 平安银行
        "sh601318",  # 中国平安
        "sz000858",  # 五粮液
        "sh600276",  # 恒瑞医药
        "sz002415",  # 海康威视
        "sh600030",  # 中信证券
        "sz300760",  # 迈瑞医疗
    ]
    
    print("=== 威科夫九大检验点大规模回测 ===")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print("数据周期: 1000根K线")
    print()
    
    result = large_scale_analysis(test_stocks, datalen=1000, horizon=20)
    
    if "error" not in result:
        format_large_scale_results(result)
    else:
        print(f"错误: {result['error']}")