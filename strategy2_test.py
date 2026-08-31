#!/usr/bin/env python3
"""
威科夫事件 + 高价值VSA标签策略 (Strategy 2) 测试脚本
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

def test_strategy_logic():
    """测试Strategy 2逻辑"""
    print("=== Strategy 2 逻辑测试 ===")
    
    # 测试股票
    symbol = "sh600036"  # 招商银行
    
    # 获取历史数据
    df = fetch_kline(symbol, datalen=250, scale=240)
    df = add_indicators(df, symbol=symbol)
    
    print(f"数据长度: {len(df)}")
    print(f"数据时间范围: {df['day'].min()} 到 {df['day'].max()}")
    
    # 测试特定时间点
    test_indices = [100, 120, 140, 160, 180]
    
    for i in test_indices:
        if i >= len(df) - 20:
            continue
            
        print(f"\n--- 测试索引 {i} ---")
        
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
        
        # 检查高价值事件
        high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"] and e.get("conf", 0) > 80]
        print(f"高价值事件数量: {len(high_events)}")
        if high_events:
            for e in high_events:
                print(f"  - {e['type']} (置信度: {e.get('conf', 0)})")
        
        # 检查高价值VSA标签
        high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
        high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa and s.get("vr", 0) >= 1.5]
        print(f"高价值VSA标签数量: {len(high_vsa)}")
        if high_vsa:
            for s in high_vsa:
                print(f"  - {s['label']} (量比: {s.get('vr', 0)})")
        
        # 应用策略2
        if nt["buy_passed"] >= 7:
            print("威科夫7项通过条件满足")
            if high_events:
                print("找到高价值事件")
                if high_vsa:
                    print("找到高价值VSA标签")
                    # 计算综合置信度
                    event_conf = max([e.get("conf", 0) for e in high_events])
                    vsa_conf = max([s.get("conf", 0) for s in high_vsa])
                    combined_conf = min(95, (event_conf * 0.6 + vsa_conf * 0.4))
                    print(f"事件置信度: {event_conf}")
                    print(f"VSA置信度: {vsa_conf}")
                    print(f"综合置信度: {combined_conf}")
                    if combined_conf >= 85:
                        print("SUCCESS: 策略2信号成立!")
                    else:
                        print("FAIL: 综合置信度不足")
                else:
                    print("FAIL: 未找到高价值VSA标签")
            else:
                print("FAIL: 未找到高价值事件")
        else:
            print("FAIL: 威科夫7项通过条件不满足")

def main():
    """主函数"""
    test_strategy_logic()

if __name__ == "__main__":
    main()