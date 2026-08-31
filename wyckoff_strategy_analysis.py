#!/usr/bin/env python3
"""威科夫项目高胜率策略回测系统"""

import numpy as np
from collections import defaultdict
import sys

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify
from wyckoff.config import VSA_BULL, VSA_BEAR


def strategy_backtest(code, datalen=1000, horizon=20, cost=0.004):
    """高胜率策略回测"""
    
    symbol = normalize_symbol(code)
    df = fetch_kline(symbol, datalen=datalen, scale=240)
    # 添加必要的技术指标
    df = add_indicators(df, symbol=symbol)
    
    if len(df) < 150:
        return {"error": "数据不足"}
    
    # 存储所有策略结果
    strategies_results = {
        "event_signals": [],      # 威科夫事件信号
        "vsa_signals": [],        # VSA量价信号
        "fusion_signals": [],     # 融合信号
        "combined_signals": []    # 组合信号
    }
    
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
        
        # 获取VSA标签
        vsa_labels = vsa_classify(wdf, scale=240)
        
        # 计算收益
        end_idx = min(i + horizon, len(df) - 1)
        close = df["close"].values
        
        # 计算持有收益（费后）
        if end_idx < len(close):
            ret = (close[end_idx] / close[i + 1] - 1) - cost
        else:
            continue
            
        # 策略1: 威科夫事件信号
        event_signals = [e for e in wevents if e.get("confirmed") is True]
        if event_signals:
            strategies_results["event_signals"].append({
                "signal": event_signals[0]["type"],
                "return": ret,
                "passed": nt["buy_passed"] >= 7  # 7项通过作为买入信号
            })
        
        # 策略2: VSA量价信号
        bull_vsa = [s for s in vsa_labels if s["label"] in VSA_BULL]
        bear_vsa = [s for s in vsa_labels if s["label"] in VSA_BEAR]
        if bull_vsa:
            strategies_results["vsa_signals"].append({
                "signal": bull_vsa[0]["label"],
                "return": ret,
                "passed": nt["buy_passed"] >= 7
            })
        
        # 策略3: 融合信号（威科夫+VSA）
        if event_signals and bull_vsa:
            strategies_results["fusion_signals"].append({
                "signal": f"{event_signals[0]['type']}-{bull_vsa[0]['label']}",
                "return": ret,
                "passed": nt["buy_passed"] >= 7
            })
        
        # 策略4: 组合信号（高胜率事件+VSA）
        high_value_events = ["Spring", "Shakeout", "SOS", "JOC", "ST", "LPS", "BU"]
        high_value_vsa = ["CHOC", "DEM", "SUP"]
        
        event_match = any(e["type"] in high_value_events for e in event_signals)
        vsa_match = any(s["label"] in high_value_vsa for s in vsa_labels)
        
        if event_match and vsa_match:
            strategies_results["combined_signals"].append({
                "signal": "HIGH_VALUE_COMBINATION",
                "return": ret,
                "passed": nt["buy_passed"] >= 7
            })
    
    # 计算各策略胜率
    results = {}
    
    for strategy_name, signals in strategies_results.items():
        if signals:
            returns = [s["return"] for s in signals]
            win_rate = (np.array(returns) > 0).mean() * 100
            avg_return = np.mean(returns) * 100
            results[strategy_name] = {
                "count": len(signals),
                "win_rate": win_rate,
                "avg_return": avg_return
            }
    
    return {
        "total_samples": len(df),
        "strategies": results
    }


def format_strategy_results(result, stock_code):
    """格式化策略结果"""
    print(f"=== 高胜率策略回测结果 - {stock_code} ===")
    print("数据周期: 1000根K线")
    print("回测周期: 20天")
    print("交易成本: 0.4%")
    print()
    
    if "error" in result:
        print(f"错误: {result['error']}")
        return
    
    print("各策略表现:")
    print("策略名称          | 样本数 | 胜率(%) | 平均收益(%)")
    print("-" * 50)
    
    for strategy_name, stats in result["strategies"].items():
        if stats:
            strategy_display = strategy_name.replace("_", " ").title()
            print(f"{strategy_display:<15} | {stats['count']:6d} | {stats['win_rate']:7.1f} | {stats['avg_return']:10.2f}")


if __name__ == "__main__":
    # 测试股票
    stock_code = "sh600036"
    
    print("正在执行高胜率策略回测...")
    result = strategy_backtest(stock_code, datalen=1000)
    format_strategy_results(result, stock_code)
    
    print()
    print("=== 策略说明 ===")
    print("1. 威科夫事件信号策略：基于确认的威科夫事件信号")
    print("2. VSA量价信号策略：基于VSA量价分析标签")
    print("3. 融合信号策略：威科夫事件+VSA标签的组合")
    print("4. 组合信号策略：高胜率事件+高胜率VSA标签的组合")