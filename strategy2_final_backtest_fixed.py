#!/usr/bin/env python3
"""
威科夫事件 + 高价值VSA标签策略 (Strategy 2) 最终回测报告
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

class Strategy2Backtester:
    """Strategy 2 回测器 - 威科夫事件 + 高价值VSA标签"""
    
    def __init__(self, data_dir="strategy2_backtest_data"):
        self.data_dir = data_dir
        self.trades = []
        
    def evaluate_strategy_2(self, df, i, wevents, nt, vsa_labels):
        """策略2: 威科夫事件 + 高价值VSA标签 (简化版)"""
        # Simplified condition: just check if we have high value events and VSA labels
        # We'll focus on detecting when we have any high-value conditions
        if nt["buy_passed"] >= 4:  # Reduced threshold
            # 检查是否有高价值事件
            high_events = [e for e in wevents if e["type"] in ["Spring", "Shakeout", "SOS", "JOC", "ST"] and e.get("conf", 0) > 80]
            if high_events:
                # 检查是否有高价值VSA标签 (relaxed condition)
                high_value_vsa = ["CHOC", "DEM", "SUP", "LPS", "ST", "Spring"]
                # Check for any VSA labels (we'll be more flexible with the VR condition)
                high_vsa = [s for s in vsa_labels if s["label"] in high_value_vsa]
                if high_vsa:
                    # Just check if we have some basic conditions met
                    return {
                        "strategy": "wyckoff_plus_vsa",
                        "signal": "combined_signal",
                        "confidence": 85,  # Fixed confidence for simplicity
                        "details": f"威科夫事件 + VSA标签: {high_events[0]['type']} + {high_vsa[0]['label']}",
                        "event": high_events[0],
                        "vsa": high_vsa[0]
                    }
        return None
    
    def calculate_returns(self, df, entry_idx, exit_idx, entry_price, exit_price):
        """计算收益率"""
        # 简单计算收益率（考虑交易成本）
        cost = 0.004  # 0.4% 交易成本
        if entry_price > 0 and exit_price > 0:
            # 多头交易：买入后卖出
            return (exit_price - entry_price) / entry_price - cost
        return 0.0
    
    def backtest_strategy_2(self, symbol, datalen=250, horizon=20, cost=0.004):
        """
        回测Strategy 2
        
        Args:
            symbol: 股票代码
            datalen: 数据长度（天）
            horizon: 持有周期（天）
            cost: 交易成本（百分比）
            
        Returns:
            dict: 回测结果
        """
        print(f"正在回测 {symbol}...")
        
        # 获取历史数据
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        df = add_indicators(df, symbol=symbol)
        
        if len(df) < 150:
            print(f"数据不足，无法回测 {symbol}")
            return {"error": "数据不足"}
        
        print(f"数据长度: {len(df)}")
        
        # 存储所有策略信号
        signals = []
        
        # 从第90根K线开始分析（避免初期数据不足）
        sample_points = list(range(90, min(len(df)-20, datalen), 10))
        print(f"分析点数量: {len(sample_points)}")
        
        signal_count = 0
        for i in sample_points:
            # 使用截至当前时刻的数据进行分析
            wdf = df.iloc[:i+1]
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)
            
            # 计算九大检验点
            nt = nine_tests(wdf, wevents, wpivots)
            
            # 获取VSA标签
            vsa_labels = vsa_classify(wdf, scale=240)
            
            # 应用策略2
            strategy2_result = self.evaluate_strategy_2(df, i, wevents, nt, vsa_labels)
            if strategy2_result:
                signals.append({
                    "timestamp": str(df.iloc[i]["day"]),  # Convert to string
                    "index": i,
                    "signal": strategy2_result,
                    "entry_price": df.iloc[i]["close"],
                    "confidence": strategy2_result["confidence"]
                })
                signal_count += 1
                
        print(f"{symbol} 找到 {signal_count} 个信号")
        
        # 执行交易
        trades = []
        for signal in signals:
            signal_idx = signal["index"]
            entry_price = signal["entry_price"]
            
            # 检查是否有足够的数据来执行交易
            if signal_idx + horizon < len(df):
                exit_idx = signal_idx + horizon
                exit_price = df.iloc[exit_idx]["close"]
                
                # 计算收益率
                return_rate = self.calculate_returns(df, signal_idx, exit_idx, entry_price, exit_price)
                
                trades.append({
                    "entry_date": str(signal["timestamp"]),  # Convert to string
                    "entry_price": entry_price,
                    "exit_date": str(df.iloc[exit_idx]["day"]),  # Convert to string
                    "exit_price": exit_price,
                    "return_rate": return_rate,
                    "confidence": signal["confidence"],
                    "signal_type": signal["signal"]["details"]
                })
        
        self.trades.extend(trades)
        
        # 计算性能指标
        if trades:
            total_return = sum(t["return_rate"] for t in trades)
            avg_return = total_return / len(trades) if trades else 0
            
            win_trades = [t for t in trades if t["return_rate"] > 0]
            win_rate = len(win_trades) / len(trades) * 100 if trades else 0
            
            # 计算最大回撤（简化版）
            cumulative_returns = [1.0]
            for trade in trades:
                cumulative_returns.append(cumulative_returns[-1] * (1 + trade["return_rate"]))
            
            max_drawdown = 0
            if len(cumulative_returns) > 1:
                peak = max(cumulative_returns)
                trough = min(cumulative_returns)
                if peak > 0:
                    max_drawdown = (peak - trough) / peak
            
            # 计算夏普比率（假设无风险利率为0）
            sharpe_ratio = 0
            if len(trades) > 1:
                returns = [t["return_rate"] for t in trades]
                std_dev = np.std(returns)
                if std_dev > 0:
                    sharpe_ratio = np.mean(returns) / std_dev
            
            return {
                "total_trades": len(trades),
                "win_rate": win_rate,
                "avg_return_per_trade": avg_return * 100,
                "total_return": total_return * 100,
                "max_drawdown": max_drawdown * 100,
                "sharpe_ratio": sharpe_ratio,
                "trades": trades
            }
        else:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "avg_return_per_trade": 0,
                "total_return": 0,
                "max_drawdown": 0,
                "sharpe_ratio": 0,
                "trades": []
            }
    
    def calculate_performance_metrics(self):
        """计算总体性能指标"""
        if not self.trades:
            return {}
            
        total_trades = len(self.trades)
        win_trades = [t for t in self.trades if t["return_rate"] > 0]
        loss_trades = [t for t in self.trades if t["return_rate"] <= 0]
        
        win_rate = len(win_trades) / total_trades * 100 if total_trades > 0 else 0
        avg_win = np.mean([t["return_rate"] for t in win_trades]) if win_trades else 0
        avg_loss = np.mean([t["return_rate"] for t in loss_trades]) if loss_trades else 0
        profit_factor = abs(sum([t["return_rate"] for t in win_trades]) / 
                           sum([t["return_rate"] for t in loss_trades])) if loss_trades else float('inf')
        
        total_return = sum(t["return_rate"] for t in self.trades)
        avg_return_per_trade = total_return / total_trades if total_trades > 0 else 0
        
        # 计算最大回撤
        cumulative_returns = [1.0]
        for trade in self.trades:
            cumulative_returns.append(cumulative_returns[-1] * (1 + trade["return_rate"]))
        
        max_drawdown = 0
        if len(cumulative_returns) > 1:
            peak = max(cumulative_returns)
            trough = min(cumulative_returns)
            if peak > 0:
                max_drawdown = (peak - trough) / peak
        
        # 计算夏普比率（假设无风险利率为0）
        sharpe_ratio = 0
        if len(self.trades) > 1:
            returns = [t["return_rate"] for t in self.trades]
            std_dev = np.std(returns)
            if std_dev > 0:
                sharpe_ratio = np.mean(returns) / std_dev
        
        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_return_per_trade": avg_return_per_trade * 100,
            "total_return": total_return * 100,
            "max_drawdown": max_drawdown * 100,
            "sharpe_ratio": sharpe_ratio,
            "profit_factor": profit_factor,
            "avg_win": avg_win * 100,
            "avg_loss": avg_loss * 100
        }
    
    def generate_report(self, stocks_list, datalen=250, horizon=20):
        """生成完整的回测报告"""
        print("开始生成回测报告...")
        
        # 回测多个股票
        stock_results = {}
        total_trades = 0
        
        for stock in stocks_list:
            try:
                result = self.backtest_strategy_2(stock, datalen, horizon)
                stock_results[stock] = result
                
                if "total_trades" in result:
                    total_trades += result["total_trades"]
                    
                print(f"{stock}: {result.get('total_trades', 0)} 笔交易")
                
            except Exception as e:
                print(f"回测 {stock} 时出错: {e}")
                stock_results[stock] = {"error": str(e)}
        
        # 计算整体性能指标
        overall_metrics = self.calculate_performance_metrics()
        
        # 生成报告
        report = {
            "report_generated_at": datetime.now().isoformat(),
            "backtest_period": f"过去{datalen}天",
            "holding_horizon": f"{horizon}天",
            "stocks_tested": len(stocks_list),
            "total_trades": total_trades,
            "strategy": "威科夫事件 + 高价值VSA标签 (Strategy 2) - 简化版",
            "metrics": overall_metrics,
            "stock_results": stock_results
        }
        
        # 保存报告
        report_filename = f"{self.data_dir}/strategy2_backtest_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            import os
            if not os.path.exists(self.data_dir):
                os.makedirs(self.data_dir)
                
            with open(report_filename, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
                
            print(f"回测报告已保存至: {report_filename}")
        except Exception as e:
            print(f"保存报告失败: {e}")
        
        return report

def main():
    """主函数 - 运行Strategy 2回测"""
    print("=== Strategy 2 回测框架 (简化版) ===")
    print("策略: 威科夫事件 + 高价值VSA标签")
    print()
    
    # 创建回测器
    backtester = Strategy2Backtester()
    
    # 测试股票列表（1年历史数据）
    # 选择一些活跃的A股作为测试样本
    test_stocks = [
        "sh600036",  # 招商银行
        "sh601318",  # 中国平安
        "sz000001",  # 平安银行
        "sh600000",  # 浦发银行
        "sz000858",  # 五粮液
        "sh600276",  # 恒瑞医药
        "sz002415",  # 海康威视
        "sh600104",  # 上汽集团
        "sz300760",  # 迈瑞医疗
        "sh600030",  # 中信证券
    ]
    
    print(f"测试股票数量: {len(test_stocks)}")
    print("测试股票列表:", test_stocks)
    print()
    
    # 运行回测
    report = backtester.generate_report(
        stocks_list=test_stocks,
        datalen=250,  # 1年数据
        horizon=20    # 持有20天
    )
    
    # 显示结果
    print("\n=== 回测结果摘要 ===")
    metrics = report["metrics"]
    print(f"总交易次数: {metrics.get('total_trades', 0)}")
    print(f"胜率: {metrics.get('win_rate', 0):.2f}%")
    print(f"平均单笔收益: {metrics.get('avg_return_per_trade', 0):.2f}%")
    print(f"总收益: {metrics.get('total_return', 0):.2f}%")
    print(f"最大回撤: {metrics.get('max_drawdown', 0):.2f}%")
    print(f"夏普比率: {metrics.get('sharpe_ratio', 0):.2f}")
    print(f"盈亏比: {metrics.get('profit_factor', 0):.2f}")
    
    # 保存详细结果
    detailed_report_filename = f"{backtester.data_dir}/strategy2_detailed_results.json"
    with open(detailed_report_filename, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\n详细结果已保存至: {detailed_report_filename}")
    
    print("\n回测完成！")

if __name__ == "__main__":
    main()