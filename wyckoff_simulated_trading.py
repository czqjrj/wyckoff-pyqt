#!/usr/bin/env python3
"""威科夫高胜率策略模拟盘选股系统

基于真实历史K线计算持有收益（不再用置信度公式捏造收益），
复用 wyckoff_strategies_manager 中优化后的模拟盘纪律策略评估逻辑，
统计策略在既定持有周期下的真实胜率与盈利。
"""

import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.fundamental import is_restricted_board, universe
from wyckoff.utils import normalize_symbol
from wyckoff_strategies_manager import WyckoffStrategyManager


class SimulatedTradingSystem:
    """模拟盘选股系统"""

    def __init__(self, data_dir="simulated_trading_data"):
        self.data_dir = data_dir
        self.watchlist = []
        self.trading_log = []
        self.strategy_performance = defaultdict(list)
        self._data_cache = {}  # cache: symbol -> df
        # 复用优化后的策略管理器
        self.manager = WyckoffStrategyManager()

    def _find_best_signal(self, code, datalen=1000, horizon=20, cost=0.004):
        """扫描个股，返回最近的策略最优信号及真实持有收益

        返回 dict 或 None：{strategy, confidence, entry_idx, entry, exit, holding_return}
        """
        symbol = normalize_symbol(code)

        # 使用缓存的 K 线数据，避免重复下载
        if symbol not in self._data_cache:
            self._data_cache[symbol] = fetch_kline(symbol, datalen=datalen, scale=240)
        df = self._data_cache[symbol]

        if len(df) < 150:
            return None

        from wyckoff.events import detect_all
        from wyckoff.indicators import add_indicators, find_pivots
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
                self.manager.evaluate_strategy_value_accumulation(
                    wdf, j, wevents, wpivots),
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

    def _process_stock(self, k, stock_code, horizon, cost):
        """处理单只股票的模拟交易"""
        try:
            best = self._find_best_signal(stock_code, horizon=horizon, cost=cost)
            if best is None:
                return k, stock_code, None

            symbol = normalize_symbol(stock_code)
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
            return k, stock_code, trade
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"分析 {stock_code} 出错: {type(e).__name__}: {e}", exc_info=True)
            return k, stock_code, None

    def run_simulation(self, stock_pool, days=5, horizon=20, cost=0.004):
        """运行模拟交易，基于真实K线计算持有收益"""
        executed_trades = []

        with ThreadPoolExecutor(max_workers=min(len(stock_pool), 4)) as executor:
            futures = {executor.submit(self._process_stock, k, code, horizon, cost): k
                       for k, code in enumerate(stock_pool)}
            for future in as_completed(futures):
                k, stock_code, trade = future.result()
                if trade is not None:
                    executed_trades.append(trade)
                    self.strategy_performance[trade["strategy"]].append(trade["holding_return"])
                    print(f"  -> {trade['strategy']} 持有{horizon}天 真实收益 {trade['holding_return'] * 100:.2f}%")

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


def main_board_universe(cap: int | None = None):
    """构建「沪深主板」默认扫描范围。

    复用代码库统一的 universe 入口 (成交额Top活跃股优先, 断网回退本地全A抽样),
    过滤掉创业板(300/301)、科创板(688/689)、北交所(bj), 仅保留
    上海主板(sh60x/61x/603/605) 与 深圳主板(sz000/001)。

    参数:
        cap: 上限只数 (None=不做截断, 取主板全量)。为避免模拟盘逐股拉K线耗时,
             默认传入 cap 控制；None 用于需要全量主板清单的场合。

    返回: 清洗后的主板代码列表 (带 sh/sz 前缀)。
    """
    codes, _ = universe(2000)
    if not codes:
        return []

    main_board = []
    for c in codes:
        sym = str(c).lower()
        if sym.startswith("bj"):
            continue
        if sym.startswith(("sh", "sz")):
            c6 = sym[2:]
        else:
            c6 = sym[-6:]
        if not (c6.isdigit() and len(c6) == 6):
            continue
        if is_restricted_board(c6):
            continue
        # 沪深主板: sh 以 6 开头(60x), sz 以 0 开头(00x)
        pref = "sh" if c6[0] == "6" else "sz" if c6[0] == "0" else ""
        if not pref:
            continue
        main_board.append(pref + c6)

    # 去重保序
    seen = set()
    uniq = []
    for c in main_board:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    if cap is not None and cap > 0:
        return uniq[:cap]
    return uniq


def main():
    """主函数 - 模拟盘选股演示"""
    print("=== 威科夫高胜率策略模拟盘选股系统 ===")
    print()

    sim_system = SimulatedTradingSystem()

    # 默认扫描范围: 沪深主板 (动态拉取活跃主板股, 断网回退本地抽样)
    # 为控制逐股拉K线耗时, 默认取主板活跃 Top-N, 可通过 --cap 调整。
    cap = 100
    stock_pool = main_board_universe(cap)

    if not stock_pool:
        print("无法获取沪深主板股票池, 退出。")
        return

    print(f"默认扫描范围: 沪深主板 (共 {len(stock_pool)} 只活跃主板股, cap={cap})")
    print("开始真实收益模拟...")
    print()

    horizon = 20
    cost = 0.004
    result = sim_system.run_simulation(stock_pool, days=5, horizon=horizon, cost=cost)

    print("\n=== 模拟交易结果 (真实K线收益) ===")
    print(f"执行交易数量: {len(result['executed_trades'])}")
    print(f"总持有收益: {result['total_return'] * 100:.2f}%")

    perf_report = sim_system.get_performance_report()
    if "message" not in perf_report:
        print("\n=== 性能统计 ===")
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
