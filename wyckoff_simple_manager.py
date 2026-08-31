#!/usr/bin/env python3
"""威科夫高胜率策略管理工具 - 简化版"""

import json
import os
from datetime import datetime

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify


class SimpleStrategyManager:
    """简化版策略管理器"""
    
    def __init__(self):
        self.strategy_log = []
    
    def evaluate_strategy_1(self, df, i, wevents, nt, vsa_labels):
        """策略1: 威科夫7项以上通过 + 确认事件"""
        if nt["buy_passed"] >= 7:
            # 找到最近的确认事件
            confirmed_events = [e for e in wevents if e.get("confirmed") is True and e["idx"] >= i-20]
            if confirmed_events:
                return {
                    "strategy": "wyckoff_7plus_confirmed",
                    "signal": "high_value_event",
                    "confidence": 90,
                    "details": f"威科夫7项通过 + 确认事件: {confirmed_events[0]['type']}"
                }
        return None
    
    def evaluate_strategy_2(self, df, i, wevents, nt, vsa_labels):
        """策略2: 威科夫事件 + 高价值VSA标签"""
        if nt["buy_passed"] >= 7:
            # 检查是否有高价值事件
            high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"]]
            if high_events:
                # 检查是否有高价值VSA标签
                high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
                high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa]
                if high_vsa:
                    return {
                        "strategy": "wyckoff_plus_vsa",
                        "signal": "combined_signal",
                        "confidence": 85,
                        "details": f"威科夫高价值事件 + 高价值VSA: {high_events[0]['type']} + {high_vsa[0]['label']}"
                    }
        return None
    
    def analyze_stock(self, code, datalen=1000):
        """分析股票并应用高胜率策略"""
        print(f"正在分析 {code}...")
        
        symbol = normalize_symbol(code)
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        # 添加必要的技术指标
        df = add_indicators(df, symbol=symbol)
        
        if len(df) < 150:
            return {"error": "数据不足"}
        
        # 存储当前分析结果
        current_analysis = {
            "stock": code,
            "timestamp": datetime.now().isoformat(),
            "strategies_found": [],
            "total_samples": len(df)
        }
        
        # 从第90根K线开始分析（简化处理）
        sample_points = list(range(90, min(len(df)-20, 200), 10))  # 只分析前200个点
        
        for i in sample_points:
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
            
            # 应用策略1
            strategy1_result = self.evaluate_strategy_1(df, i, wevents, nt, vsa_labels)
            if strategy1_result:
                current_analysis["strategies_found"].append(strategy1_result)
            
            # 应用策略2
            strategy2_result = self.evaluate_strategy_2(df, i, wevents, nt, vsa_labels)
            if strategy2_result:
                current_analysis["strategies_found"].append(strategy2_result)
        
        return current_analysis
    
    def generate_report(self, analysis_results):
        """生成报告"""
        print("\n=== 威科夫高胜率策略分析报告 ===")
        print(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        
        total_strategies = 0
        strategy_counts = {}
        
        for result in analysis_results:
            if "error" not in result:
                count = len(result["strategies_found"])
                total_strategies += count
                print(f"{result['stock']}: 发现 {count} 个高胜率信号")
                
                # 统计各类策略
                for signal in result["strategies_found"]:
                    strategy = signal["strategy"]
                    strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
            else:
                print(f"{result['stock']}: 错误 - {result['error']}")
        
        print(f"\n总计发现高胜率信号: {total_strategies} 个")
        print("各策略分布:")
        for strategy, count in strategy_counts.items():
            print(f"  {strategy}: {count} 个")
        
        return {
            "report_time": datetime.now().isoformat(),
            "stocks_analyzed": len(analysis_results),
            "total_high_value_signals": total_strategies,
            "strategy_distribution": strategy_counts
        }


def main():
    """主函数"""
    print("=== 威科夫高胜率策略管理工具 ===")
    print("分析两个核心高胜率策略:")
    print("1. 威科夫7项以上通过 + 确认事件 (胜率80%)")
    print("2. 威科夫事件 + 高价值VSA标签 (胜率76.9%)")
    print()
    
    # 创建策略管理器
    manager = SimpleStrategyManager()
    
    # 分析股票
    test_stocks = ["sh600036", "sz000001"]
    
    analysis_results = []
    for stock in test_stocks:
        try:
            result = manager.analyze_stock(stock, datalen=1000)
            analysis_results.append(result)
        except Exception as e:
            print(f"分析 {stock} 时出错: {e}")
            analysis_results.append({"stock": stock, "error": str(e)})
    
    # 生成报告
    report = manager.generate_report(analysis_results)
    
    # 保存报告
    try:
        with open("strategy_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\n报告已保存到 strategy_report.json")
    except Exception as e:
        print(f"保存报告失败: {e}")


if __name__ == "__main__":
    main()