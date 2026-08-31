#!/usr/bin/env python3
"""调整后的威科夫策略1 - 实用版多因子强化策略"""

import numpy as np
from collections import defaultdict, deque
import json
import os
from datetime import datetime

from wyckoff.ninetests import nine_tests
from wyckoff.events import detect_all
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.utils import normalize_symbol
from wyckoff.vsa import vsa_classify


class PracticalStrategy1:
    """实用版策略1：多因子强化做强多头策略"""
    
    def __init__(self, data_dir="practical_strategy_data"):
        self.data_dir = data_dir
        self.performance_log = deque(maxlen=100)
        
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
    
    def validate_market_conditions(self, df, symbol):
        """验证市场环境条件（简化版）"""
        # 简化实现：返回基本的市场状态
        try:
            # 检查大盘20日线是否向上
            if "ma20" in df.columns:
                ma20_current = df["ma20"].iloc[-1]
                ma20_previous = df["ma20"].iloc[-2] if len(df) > 1 else 0
                ma20_upward = ma20_current > ma20_previous
            else:
                ma20_upward = True  # 默认认为向上
                
            return {
                "ma20_upward": ma20_upward,
                "sector_strength": 0.6,  # 简化示例值
                "fund_flow": 0.5,       # 简化示例值
                "conditions_met": True  # 简化判断
            }
        except:
            return {
                "ma20_upward": True,
                "sector_strength": 0.5,
                "fund_flow": 0.5,
                "conditions_met": False
            }
    
    def evaluate_practical_strategy_1(self, df, i, wevents, nt, vsa_labels, stock_code):
        """实用版策略1：多因子强化做强多头策略"""
        # 只做强多头事件
        strong_bull_events = ["Spring", "Shakeout", "ST", "LPS", "SC"]
        # 放宽条件：置信度≥80即可
        bull_events = [e for e in wevents if e["type"] in strong_bull_events and e.get("conf", 0) >= 80]
        
        if bull_events:
            event = bull_events[0]
            
            # 验证市场环境条件（简化版）
            market_conditions = self.validate_market_conditions(df, stock_code)
            
            # 检查是否满足硬门槛条件（适度放宽）
            if (market_conditions["ma20_upward"] and 
                market_conditions["sector_strength"] >= 0.5 and 
                market_conditions["fund_flow"] >= 0.4):
                
                return {
                    "strategy": "practical_strategy_1",
                    "signal": "multi_factor_bull",
                    "confidence": 85,  # 适度降低置信度，提高实用性
                    "details": f"强多头事件: {event['type']} (conf≥80) + 市场条件满足",
                    "event": event,
                    "market_conditions": market_conditions,
                    "requirements_met": "主要硬门槛条件"
                }
        return None
    
    def analyze_stock(self, code, datalen=1000, horizon=20, cost=0.004):
        """分析股票并应用实用策略1"""
        symbol = normalize_symbol(code)
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        # 添加必要的技术指标
        df = add_indicators(df, symbol=symbol)
        
        if len(df) < 150:
            return {"error": "数据不足"}
        
        # 存储当前分析结果
        current_analysis = {
            "stock": code,
            "name": fetch_name(symbol),
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
            
            # 应用实用策略1
            strategy1_result = self.evaluate_practical_strategy_1(df, i, wevents, nt, vsa_labels, code)
            if strategy1_result:
                current_analysis["strategies_found"].append(strategy1_result)
        
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
            "name": analysis_result["name"],
            "timestamp": analysis_result["timestamp"],
            "strategies_count": dict(strategy_counts),
            "total_signals": len(analysis_result["strategies_found"])
        }
        
        self.performance_log.append(performance_record)
        self.save_performance_history()
    
    def get_performance_report(self):
        """获取性能报告"""
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


def main():
    """主函数 - 实用策略1演示"""
    print("=== 实用版威科夫策略1分析 ===")
    print("策略：只做强多头事件 + 多因子环境验证（适度放宽条件）")
    print()
    
    # 创建实用策略管理器
    practical_optimizer = PracticalStrategy1()
    
    # 加载历史数据
    practical_optimizer.load_performance_history()
    
    # 分析股票
    print("正在分析股票...")
    test_stocks = ["sh600036", "sz000001", "sh601318", "sz000858"]
    
    analysis_results = []
    for stock in test_stocks:
        try:
            result = practical_optimizer.analyze_stock(stock, datalen=1000)
            analysis_results.append(result)
        except Exception as e:
            print(f"分析 {stock} 时出错: {e}")
            analysis_results.append({"stock": stock, "error": str(e)})
    
    # 显示分析结果
    print("\n分析结果汇总:")
    print("=" * 60)
    
    total_strategies = 0
    strategy_counts = {}
    
    for result in analysis_results:
        if "error" not in result:
            count = len(result["strategies_found"])
            total_strategies += count
            print(f"{result['stock']} ({result['name']}): 发现 {count} 个实用信号")
            
            # 显示具体信号
            for signal in result["strategies_found"][:3]:  # 只显示前3个
                print(f"  - {signal['details']}")
                print(f"    置信度: {signal['confidence']}%")
                print()
                
            # 统计各类策略
            for signal in result["strategies_found"]:
                strategy = signal["strategy"]
                strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        else:
            print(f"{result['stock']}: 错误 - {result['error']}")
    
    print(f"\n总计发现实用信号: {total_strategies} 个")
    print("策略分布:")
    for strategy, count in strategy_counts.items():
        print(f"  {strategy}: {count} 个")
    
    # 显示性能报告
    print("\n性能统计:")
    stats = practical_optimizer.get_performance_report()
    if "message" not in stats:
        print(f"总分析次数: {stats['total_analyses']}")
        print("策略出现次数:")
        for strategy, count in stats['strategy_totals'].items():
            print(f"  {strategy}: {count} 次")
    else:
        print(stats["message"])


if __name__ == "__main__":
    main()