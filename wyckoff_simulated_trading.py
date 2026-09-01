#!/usr/bin/env python3
"""威科夫高胜率策略模拟盘选股系统

基于真实历史K线计算持有收益（不再用置信度公式捏造收益），
复用 wyckoff_strategies_manager 中优化后的模拟盘纪律策略评估逻辑，
统计策略在既定持有周期下的真实胜率与盈利。
"""

import numpy as np
from collections import defaultdict
import json
from datetime import datetime

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.utils import normalize_symbol
from wyckoff_strategies_manager import WyckoffStrategyManager


class SimulatedTradingSystem:
    """模拟盘选股系统"""

    def __init__(self, data_dir="simulated_trading_data"):
        self.data_dir = data_dir
        self.watchlist = []
        self.trading_log = []
        self.strategy_performance = defaultdict(list)
        # 复用优化后的策略管理器
        self.manager = WyckoffStrategyManager()

    def _find_best_signal(self, code, datalen=1000, horizon=20, cost=0.004):
        """扫描个股，返回最近的策略最优信号及真实持有收益

        返回 dict 或 None：{strategy, confidence, entry_idx, entry, exit, holding_return}
        """
        symbol = normalize_symbol(code)
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        if len(df) < 150:
            return None

        from wyckoff.indicators import find_pivots, add_indicators
        from wyckoff.events import detect_all
        from wyckoff.ninetests import nine_tests
        from wyckoff.vsa import vsa_classify

        # 扫描近端采样点（按事件/信号评估），去重每个事件实例
        seen = set()
        best = None
        start = max(90, len(df) - 100)
        for j in range(start, len(df) - horizon - 1, 5):
            wdf = df.iloc[:j + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)
            nt = nine_tests(wdf, wevents, wpivots)
            vsa_labels = vsa_classify(wdf, scale=240)

            candidates = [
                self.manager.evaluate_strategy_4(df, j, wevents, nt, vsa_labels),
            ]

            for res_ in candidates:
                if not res_:
                    continue
                ev = res_.get("event", {})
                if (res_["strategy"], ev.get("idx")) in seen:
                    continue
                seen.add((res_["strategy"], ev.get("idx")))

                entry = df["close"].iloc[j]
                exit_idx = min(j + horizon, len(df) - 1)
                exit_p = df["close"].iloc[exit_idx]
                holding_return = (exit_p / entry - 1) - cost

                # 保留置信度最高的信号
                if best is None or res_.get("confidence", 0) > best["confidence"]:
                    best = {
                        "strategy": res_["strategy"],
                        "confidence": res_.get("confidence", 0),
                        "details": res_.get("details", ""),
                        "entry_date": str(df["day"].iloc[j]),
                        "exit_date": str(df["day"].iloc[exit_idx]),
                        "entry": float(entry),
                        "exit": float(exit_p),
                        "holding_return": float(holding_return),
                        "horizon": horizon,
                    }
        return best

    def run_simulation(self, stock_pool, days=5, horizon=20, cost=0.004):
        """运行模拟交易，基于真实K线计算持有收益"""
        executed_trades = []

        for k, stock_code in enumerate(stock_pool):
            print(f"[{k + 1}/{len(stock_pool)}] 分析 {stock_code}...", flush=True)
            try:
                symbol = normalize_symbol(stock_code)
                best = self._find_best_signal(stock_code, horizon=horizon, cost=cost)
                if best is None:
                    continue

                trade = {
                    "stock": stock_code,
                    "name": fetch_name(symbol),
                    "strategy": best["strategy"],
                    "confidence": best["confidence"],
                    "details": best["details"],
                    "execution_time": datetime.now().isoformat(),
                    "entry_date": best["entry_date"],
                    "exit_date": best["exit_date"],
                    "entry": best["entry"],
                    "exit": best["exit"],
                    "holding_return": best["holding_return"],
                    "horizon": horizon,
                    "status": "executed",
                }
                executed_trades.append(trade)

                # 记录到分策略表现
                self.strategy_performance[trade["strategy"]].append(trade["holding_return"])
                print(f"  -> {trade['strategy']} 持有{best['horizon']}天 真实收益 {best['holding_return'] * 100:.2f}%")
            except Exception as e:
                print(f"  分析 {stock_code} 出错: {e}")

        self.trading_log.extend(executed_trades)
        return {
            "total_signals": len(executed_trades),
            "executed_trades": executed_trades,
            "total_return": sum(t["holding_return"] for t in executed_trades),
        }

    def get_performance_report(self):
        """基于真实持有收益统计胜率/盈利"""
        if not self.trading_log:
            return {"message": "暂无交易记录"}

        rets = [t["holding_return"] for t in self.trading_log]
        total_trades = len(rets)
        successful_trades = sum(1 for r in rets if r > 0)
        avg_return = np.mean(rets) * 100
        total_return = (np.prod([1 + r for r in rets]) - 1) * 100

        report = {
            "total_trades": total_trades,
            "successful_trades": successful_trades,
            "success_rate": (successful_trades / total_trades) * 100 if total_trades else 0,
            "average_return_percent": avg_return,
            "total_return_percent": total_return,
            "last_updated": datetime.now().isoformat(),
        }
        # 分策略统计
        by_strategy = defaultdict(list)
        for t in self.trading_log:
            by_strategy[t["strategy"]].append(t["holding_return"])
        report["strategy_performance"] = {}
        for strat, sr in by_strategy.items():
            n = len(sr)
            report["strategy_performance"][strat] = {
                "total_trades": n,
                "win_rate": (sum(1 for r in sr if r > 0) / n * 100) if n else 0,
                "avg_return_percent": float(np.mean(sr) * 100) if n else 0,
            }
        return report


def main():
    """主函数 - 模拟盘选股演示"""
    print("=== 威科夫高胜率策略模拟盘选股系统 ===")
    print()

    sim_system = SimulatedTradingSystem()

    # 股票池
    stock_pool = [
        "sh600036", "sz000001", "sh601318", "sz000858",
        "sh600276", "sz002415", "sh600030", "sz300760",
        "sz300750", "sh600519", "sz000333", "sh601899",
        "sh600900", "sz000651", "sh600887",
    ]

    print(f"股票池包含 {len(stock_pool)} 只股票，开始真实收益模拟...")
    print()

    horizon = 20
    cost = 0.004
    result = sim_system.run_simulation(stock_pool, days=5, horizon=horizon, cost=cost)

    print(f"\n=== 模拟交易结果 (真实K线收益) ===")
    print(f"执行交易数量: {len(result['executed_trades'])}")
    print(f"总持有收益: {result['total_return'] * 100:.2f}%")

    perf_report = sim_system.get_performance_report()
    if "message" not in perf_report:
        print(f"\n=== 性能统计 ===")
        print(f"总交易数:   {perf_report['total_trades']}")
        print(f"成功交易:   {perf_report['successful_trades']}")
        print(f"真实胜率:   {perf_report['success_rate']:.1f}%")
        print(f"平均持有收益: {perf_report['average_return_percent']:.2f}%")
        print(f"累计收益:   {perf_report['total_return_percent']:.2f}%")
        print("\n分策略表现 (真实持有收益):")
        for strat, sp in perf_report["strategy_performance"].items():
            print(f"  {strat}: {sp['total_trades']} 笔, 胜率 {sp['win_rate']:.1f}%, "
                  f"平均 {sp['avg_return_percent']:.2f}%")

    # 保存结果
    try:
        with open("simulation_results.json", "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "stock_pool_size": len(stock_pool),
                "horizon": horizon,
                "cost": cost,
                "trades_executed": len(result["executed_trades"]),
                "total_return": result["total_return"],
                "performance": perf_report,
                "executed_trades": result["executed_trades"]
            }, f, ensure_ascii=False, indent=2)
        print("\n模拟结果已保存到 simulation_results.json")
    except Exception as e:
        print(f"保存结果失败: {e}")


if __name__ == "__main__":
    main()
