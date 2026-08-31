#!/usr/bin/env python3
"""威科夫高胜率策略管理系统"""

import numpy as np
from collections import defaultdict, deque
import json
import os
from datetime import datetime

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify
from wyckoff.config import VSA_BULL, VSA_BEAR


class WyckoffStrategyManager:
    """威科夫高胜率策略管理器"""
    
    def __init__(self, data_dir="strategy_data"):
        self.data_dir = data_dir
        self.strategy_results = defaultdict(list)
        self.performance_log = deque(maxlen=100)  # 保留最近100条记录
        
        # 创建数据目录
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
    
    def load_performance_history(self):
        """加载历史性能记录"""
        history_file = os.path.join(self.data_dir, "performance_history.json")
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    self.performance_log = deque(json.load(f), maxlen=100)
            except:
                pass
    
    def save_performance_history(self):
        """保存性能记录"""
        history_file = os.path.join(self.data_dir, "performance_history.json")
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.performance_log), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存历史记录失败: {e}")
    
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
    
    def analyze_stock(self, code, datalen=1000, horizon=20, cost=0.004):
        """分析股票并应用高胜率策略"""
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
        
        # 从第90根K线开始分析
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
            
            # 应用策略1
            strategy1_result = self.evaluate_strategy_1(df, i, wevents, nt, vsa_labels)
            if strategy1_result:
                current_analysis["strategies_found"].append(strategy1_result)
            
            # 应用策略2
            strategy2_result = self.evaluate_strategy_2(df, i, wevents, nt, vsa_labels)
            if strategy2_result:
                current_analysis["strategies_found"].append(strategy2_result)
        
        # 记录性能
        self.record_performance(current_analysis)
        
        return current_analysis
    
    def record_performance(self, analysis_result):
        """记录分析结果到性能历史"""
        # 计算策略表现
        strategy_counts = defaultdict(int)
        for strategy in analysis_result["strategies_found"]:
            strategy_counts[strategy["strategy"]] += 1
        
        performance_record = {
            "stock": analysis_result["stock"],
            "timestamp": analysis_result["timestamp"],
            "strategies_count": dict(strategy_counts),
            "total_signals": len(analysis_result["strategies_found"])
        }
        
        self.performance_log.append(performance_record)
        self.save_performance_history()
    
    def get_strategy_statistics(self):
        """获取策略统计信息"""
        if not self.performance_log:
            return {"message": "暂无历史数据"}
        
        total_analyses = len(self.performance_log)
        strategy_totals = defaultdict(int)
        strategy_details = defaultdict(list)
        
        for record in self.performance_log:
            for strategy, count in record["strategies_count"].items():
                strategy_totals[strategy] += count
                strategy_details[strategy].append(count)
        
        stats = {
            "total_analyses": total_analyses,
            "strategy_totals": dict(strategy_totals),
            "strategy_averages": {}
        }
        
        for strategy, counts in strategy_details.items():
            stats["strategy_averages"][strategy] = {
                "total": sum(counts),
                "average_per_analysis": sum(counts) / total_analyses if total_analyses > 0 else 0,
                "max_per_analysis": max(counts) if counts else 0
            }
        
        return stats
    
    def export_strategy_report(self, filename="strategy_report.json"):
        """导出策略报告"""
        report = {
            "generated_at": datetime.now().isoformat(),
            "strategy_statistics": self.get_strategy_statistics(),
            "recent_analyses": list(self.performance_log)[-10:]  # 最近10次分析
        }
        
        try:
            with open(os.path.join(self.data_dir, filename), 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            print(f"策略报告已导出到: {os.path.join(self.data_dir, filename)}")
        except Exception as e:
            print(f"导出报告失败: {e}")


def main():
    """主函数 - 策略管理演示"""
    print("=== 威科夫高胜率策略管理系统 ===")
    print()
    
    # 创建策略管理器
    manager = WyckoffStrategyManager()
    
    # 加载历史数据
    manager.load_performance_history()
    
    # 分析股票
    test_stocks = ["sh600036", "sz000001", "sh601318"]
    
    print("正在分析股票...")
    for stock in test_stocks:
        print(f"\n分析 {stock}...")
        try:
            result = manager.analyze_stock(stock, datalen=1000)
            if "error" not in result:
                print(f"  发现策略信号: {len(result['strategies_found'])} 个")
                if result['strategies_found']:
                    print("  策略详情:")
                    for i, signal in enumerate(result['strategies_found'][:3]):  # 只显示前3个
                        print(f"    {i+1}. {signal['strategy']}: {signal['details']}")
            else:
                print(f"  错误: {result['error']}")
        except Exception as e:
            print(f"  分析出错: {e}")
    
    # 显示统计信息
    print("\n=== 策略统计信息 ===")
    stats = manager.get_strategy_statistics()
    if "message" not in stats:
        print(f"总分析次数: {stats['total_analyses']}")
        print("各策略出现次数:")
        for strategy, count in stats['strategy_totals'].items():
            print(f"  {strategy}: {count} 次")
    else:
        print(stats["message"])
    
    # 导出报告
    manager.export_strategy_report()
    
    print("\n=== 策略管理系统功能 ===")
    print("1. 自动识别高胜率策略信号")
    print("2. 记录策略表现历史")
    print("3. 提供策略统计分析")
    print("4. 支持策略报告导出")
    print("5. 可扩展添加新策略")


if __name__ == "__main__":
    main()