#!/usr/bin/env python3
"""
威科夫事件 + 高价值VSA标签策略 (Strategy 2) 深度分析脚本
"""

import numpy as np
import pandas as pd
import json
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# 导入相关模块
import sys
sys.path.append('.')

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify

def analyze_strategy_conditions():
    """深入分析Strategy 2的条件"""
    print("=== Strategy 2 条件深度分析 ===")
    
    # 测试股票
    symbol = "sh600036"  # 招商银行
    
    # 获取历史数据
    df = fetch_kline(symbol, datalen=250, scale=240)
    df = add_indicators(df, symbol=symbol)
    
    print(f"数据长度: {len(df)}")
    print(f"数据时间范围: {df['day'].min()} 到 {df['day'].max()}")
    
    # 分析前几个关键点
    test_indices = [100, 120, 140, 160, 180]
    
    for i in test_indices:
        if i >= len(df) - 20:
            continue
            
        print(f"\n--- 分析索引 {i} ---")
        
        # 使用截至当前时刻的数据进行分析
        wdf = df.iloc[:i+1]
        wdf = add_indicators(wdf, symbol=symbol)
        wpivots = find_pivots(wdf, order=6)
        wevents = detect_all(wdf, wpivots)
        
        # 计算九大检验点
        nt = nine_tests(wdf, wevents, wpivots)
        
        # 获取VSA标签
        vsa_labels = vsa_classify(wdf, scale=240)
        
        print(f"威科夫9项通过数: {nt['buy_passed']}")
        print(f"威科夫9项详细结果:")
        for key, value in nt.items():
            if key != 'buy_passed':
                print(f"  {key}: {value}")
        
        # 检查高价值事件
        high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"] and e.get("conf", 0) > 80]
        print(f"高价值事件数量: {len(high_events)}")
        if high_events:
            for e in high_events:
                print(f"  - {e['type']} (置信度: {e.get('conf', 0)}, 价格: {e.get('price', 'N/A')})")
        else:
            print("  无高价值事件")
        
        # 检查所有VSA标签
        print(f"所有VSA标签数量: {len(vsa_labels)}")
        if vsa_labels:
            for s in vsa_labels[:5]:  # 只显示前5个
                print(f"  - {s['label']} (量比: {s.get('vr', 0)}, 置信度: {s.get('conf', 0)})")
        
        # 检查高价值VSA标签
        high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
        high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa and s.get("vr", 0) >= 1.5]
        print(f"高价值VSA标签数量: {len(high_vsa)}")
        if high_vsa:
            for s in high_vsa:
                print(f"  - {s['label']} (量比: {s.get('vr', 0)}, 置信度: {s.get('conf', 0)})")
        
        # Test relaxed conditions
        print("\n--- 松弛条件测试 ---")
        print(f"威科夫5项通过: {nt['buy_passed'] >= 5}")
        print(f"威科夫6项通过: {nt['buy_passed'] >= 6}")
        print(f"威科夫7项通过: {nt['buy_passed'] >= 7}")
        print(f"高价值事件存在: {len(high_events) > 0}")
        print(f"高价值VSA标签存在: {len(high_vsa) > 0}")
        
        # Show all events
        print(f"\n--- 所有事件 ---")
        for e in wevents[-5:]:  # 显示最后5个事件
            print(f"  - {e['type']} (置信度: {e.get('conf', 0)}, 价格: {e.get('price', 'N/A')})")

def main():
    """主函数"""
    analyze_strategy_conditions()

if __name__ == "__main__":
    main()