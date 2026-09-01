#!/usr/bin/env python3
"""策略回测 - 复用 wyckoff_strategies_manager.py 的 WyckoffStrategyManager

对多只股票应用策略管理器中的模拟盘纪律策略（策略4），
验证每个策略产生的信号在持有 horizon 天后的胜率与收益。
"""
import sys
import os
import json
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from datetime import datetime

sys.path.append('.')
from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.utils import normalize_symbol
from wyckoff.indicators import find_pivots, add_indicators
from wyckoff.events import detect_all
from wyckoff.ninetests import nine_tests
from wyckoff.vsa import vsa_classify
from wyckoff_strategies_manager import WyckoffStrategyManager


class StrategyBacktester:
    """策略回测器"""

    def __init__(self, data_dir="three_strategy_backtest_data"):
        self.data_dir = data_dir
        self.manager = WyckoffStrategyManager()
        self.all_trades = []

    def backtest_stock(self, symbol, datalen=500, horizon=20, cost=0.004,
                       entry_mode="next_open", confirm_pct=0.0,
                       stop_loss=None, take_profit=None, trail_atr=0.0):
        """回测单只股票，返回该股票各策略的交易记录

        执行规则（更贴近威科夫事件型信号的盈利方式）：
        - 信号去重：同一事件实例只开一笔仓，避免重叠信号虚增相关样本
        - 入场模式：
            * next_open: 信号次日开盘价入场
            * confirm:   等待突破确认——后续收盘价涨过
                          (信号日收盘价×(1+confirm_pct)) 才入场；确认窗口内未突破则放弃
        - 出场：
            * take_profit: 固定止盈（%）
            * stop_loss:   初始硬止损（%）
            * trail_atr:   ATR 移动止损（以入场后最高收盘回撤 n×ATR 离场）
            * 最迟持有 horizon 天

        Args:
            symbol: 股票代码
            datalen: 数据长度
            horizon: 最大持有周期（K线数）
            cost: 交易总成本（比例）
            entry_mode: "next_open" 或 "confirm"
            confirm_pct: 确认入场所需涨幅（0.0 表示仅次日开盘）
            stop_loss: 硬止损比例（如 0.08 表示 -8%）
            take_profit: 固定止盈比例（如 0.15 表示 +15%）
            trail_atr: ATR 移动止损倍数（0 表示不启用）
        """
        df = fetch_kline(symbol, datalen=datalen, scale=240)
        if len(df) < 150:
            return []

        trades = []
        seen = set()
        sample_points = range(90, len(df) - horizon - 10, 5)
        atr_vals = df["atr"].values if "atr" in df.columns else np.full(len(df), np.nan)

        for i in sample_points:
            wdf = df.iloc[:i + 1].copy()
            wdf = add_indicators(wdf, symbol=symbol)
            wpivots = find_pivots(wdf, order=6)
            wevents = detect_all(wdf, wpivots)
            nt = nine_tests(wdf, wevents, wpivots)
            vsa_labels = vsa_classify(wdf, scale=240)

            candidates = []
            for res in [
                self.manager.evaluate_strategy_4(df, i, wevents, nt, vsa_labels),
            ]:
                if res:
                    candidates.append(res)

            for res in candidates:
                # 信号去重：同一策略 + 同一事件实例(idx) 只开一笔
                ev = res.get("event", {})
                dedup_key = (res["strategy"], ev.get("idx"))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                trade = self._make_trade(
                    symbol, res, df, i, horizon, cost,
                    entry_mode=entry_mode, confirm_pct=confirm_pct,
                    stop_loss=stop_loss, take_profit=take_profit,
                    trail_atr=trail_atr, atr_vals=atr_vals,
                )
                if trade is not None:
                    trades.append(trade)

        return trades

    @staticmethod
    def _make_trade(symbol, signal_result, df, i, horizon, cost,
                    entry_mode="next_open", confirm_pct=0.0,
                    stop_loss=None, take_profit=None,
                    trail_atr=0.0, atr_vals=None):
        """模拟一笔多头交易，返回 None 表示未入场（确认失败）"""
        n = len(df)
        # ---- 入场确定 ----
        if entry_mode == "confirm":
            ref = df["close"].iloc[i]
            threshold = ref * (1 + confirm_pct)
            entry_idx = None
            for j in range(i + 1, min(i + 1 + 5, n)):
                if df["close"].iloc[j] > threshold:
                    entry_idx = j
                    break
            if entry_idx is None:
                return None  # 确认窗口内未突破，放弃
        else:  # next_open
            entry_idx = min(i + 1, n - 1)

        entry = df["open"].iloc[entry_idx]
        if entry <= 0:
            return None
        atr = atr_vals[entry_idx] if (atr_vals is not None and not np.isnan(atr_vals[entry_idx])) else 0.0

        exit_idx = min(entry_idx + horizon, n - 1)
        exit_p = df["close"].iloc[exit_idx]
        stop_price = None
        if stop_loss is not None:
            stop_price = entry * (1 - stop_loss)

        # ---- 逐日扫描出场 ----
        high_water = entry
        for j in range(entry_idx + 1, exit_idx + 1):
            high_water = max(high_water, df["close"].iloc[j])

            # 固定止盈（优先于止损，用盘中高点触发）
            if take_profit is not None and df["high"].iloc[j] >= entry * (1 + take_profit):
                exit_p = entry * (1 + take_profit)
                exit_idx = j
                break

            # ATR 移动止损（用收盘回撤）
            if trail_atr > 0 and atr > 0:
                trail_stop = high_water - trail_atr * atr
                if df["close"].iloc[j] < trail_stop:
                    exit_p = df["close"].iloc[j]
                    exit_idx = j
                    # 止损按更高者处理，避免止损后还按 entry 止损
                    stop_price = None
                    break

            # 硬止损（用盘中低点）
            if stop_price is not None and df["low"].iloc[j] <= stop_price:
                exit_p = stop_price
                exit_idx = j
                break

        ret = (exit_p / entry - 1) - cost
        return {
            "stock": symbol,
            "strategy": signal_result["strategy"],
            "confidence": signal_result.get("confidence", 0),
            "entry_date": str(df["day"].iloc[entry_idx]),
            "exit_date": str(df["day"].iloc[exit_idx]),
            "entry_price": float(entry),
            "exit_price": float(exit_p),
            "return_rate": float(ret),
            "details": signal_result.get("details", ""),
        }

    def run(self, stocks, datalen=500, horizon=20, cost=0.004,
            entry_mode="next_open", confirm_pct=0.0,
            stop_loss=None, take_profit=None, trail_atr=0.0):
        """回测多只股票，返回按策略分组的统计结果"""
        all_trades = []
        for stock in stocks:
            print(f"回测 {stock} ...", flush=True)
            try:
                trades = self.backtest_stock(
                    stock, datalen=datalen, horizon=horizon, cost=cost,
                    entry_mode=entry_mode, confirm_pct=confirm_pct,
                    stop_loss=stop_loss, take_profit=take_profit, trail_atr=trail_atr,
                )
                print(f"  -> {len(trades)} 笔交易")
                all_trades.extend(trades)
            except Exception as e:
                print(f"  -> 出错: {e}")
                import traceback; traceback.print_exc()
                continue

        self.all_trades = all_trades
        report = self._compute_report(datalen, horizon, cost, stocks)
        self._save_report(report)
        return report

    def _compute_report(self, datalen, horizon, cost, stocks):
        """按策略分组计算统计指标"""
        by_strategy = {}
        for t in self.all_trades:
            by_strategy.setdefault(t["strategy"], []).append(t)

        strategies = {
            "paper_discipline_bull": "策略4: 模拟盘纪律策略",
        }

        result = {}
        for key, name in strategies.items():
            trades = by_strategy.get(key, [])
            if not trades:
                result[key] = {"name": name, "trades": [], "total_trades": 0}
                continue
            rets = np.array([t["return_rate"] for t in trades])
            wins = rets[rets > 0]
            losses = rets[rets <= 0]
            cum = np.cumprod(1 + rets)
            peak = np.maximum.accumulate(cum)
            dd = (cum - peak) / peak
            result[key] = {
                "name": name,
                "total_trades": len(trades),
                "win_rate": float((rets > 0).mean()) * 100,
                "avg_return_per_trade": float(rets.mean()) * 100,
                "total_return": float(np.prod(1 + rets) - 1) * 100,
                "max_drawdown": float(dd.min()) * 100 if len(dd) else 0,
                "avg_win": float(wins.mean()) * 100 if len(wins) else 0,
                "avg_loss": float(losses.mean()) * 100 if len(losses) else 0,
                "profit_factor": float(np.abs(wins.sum() / losses.sum())) if len(losses) and losses.sum() != 0 else float("inf"),
                "avg_confidence": float(np.mean([t["confidence"] for t in trades])),
                "trades": trades,
            }

        return {
            "generated_at": datetime.now().isoformat(),
            "backtest_period": f"过去{datalen}天",
            "holding_horizon": f"{horizon}天",
            "cost": cost,
            "stocks_tested": len(stocks),
            "total_trades": len(self.all_trades),
            "strategies": result,
        }

    def _save_report(self, report):
        os.makedirs(self.data_dir, exist_ok=True)
        fn = os.path.join(
            self.data_dir,
            f"three_strategies_backtest_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已保存: {fn}")


def main():
    print("=== 策略回测（模拟盘纪律策略） ===")
    print("策略来源: wyckoff_strategies_manager.WyckoffStrategyManager")
    print()

    stocks = [
        "sh600036", "sz000001", "sh601318", "sh600000", "sz000858",
        "sh600276", "sz002415", "sh600104", "sz300760", "sh600030",
        "sz300750", "sh600519", "sz000333", "sh601899", "sh688981",
        "sz002594", "sh600900", "sh601012", "sz000651", "sh600887",
        "sh600036", "sz002415", "sh601166", "sz000725", "sh600028",
        "sh601088", "sz002230", "sh600809", "sz300059", "sh600585",
    ]
    stocks = list(dict.fromkeys(stocks))[:30]

    backtester = StrategyBacktester()

    # 交易执行规则扫描：探索提高胜率的组合（突破入场 + 止盈止损 + ATR移动止损）
    param_sweeps = [
        # (label, horizon, entry_mode, confirm_pct, stop_loss, take_profit, trail_atr)
        ("固定持有 H=20", 20, "next_open", 0.0, None, None, 0.0),
        ("突破2% H=20", 20, "confirm", 0.02, None, None, 0.0),
        ("突破3% 硬止损8%", 20, "confirm", 0.03, 0.08, None, 0.0),
        ("突破2% 止盈15%", 20, "confirm", 0.02, None, 0.15, 0.0),
        ("突破2% +ATR移动 1.5x", 20, "confirm", 0.02, None, None, 1.5),
        ("突破2% +ATR2x +止损8%", 25, "confirm", 0.02, 0.08, None, 2.0),
        ("突破1.5% +ATR2x +止盈20%", 30, "confirm", 0.015, None, 0.20, 2.0),
    ]

    summary = []
    for label, horizon, em, cp, sl, tp, ta in param_sweeps:
        print(f"\n>>> 执行规则: {label}")
        report = backtester.run(stocks=stocks, datalen=500, horizon=horizon, cost=0.004,
                                entry_mode=em, confirm_pct=cp, stop_loss=sl,
                                take_profit=tp, trail_atr=ta)
        total_trades = 0
        total_win = 0
        for key, s in report["strategies"].items():
            total_trades += s["total_trades"]
            total_win += s["total_trades"] * s.get("win_rate", 0.0)
        composite_wr = total_win / total_trades * 100 if total_trades else 0
        summary.append({
            "label": label,
            "total_trades": total_trades,
            "composite_win_rate": composite_wr,
            "per_strategy": {k: {"n": v["total_trades"], "wr": v.get("win_rate", 0.0)}
                             for k, v in report["strategies"].items()},
        })
        # 展示各策略明细一屏
        for k, v in report["strategies"].items():
            if v["total_trades"]:
                print(f"    {v['name']}: n={v['total_trades']} 胜率={v.get('win_rate',0):.1f}% "
                      f"平均={v.get('avg_return_per_trade',0):.2f}% 总收益={v.get('total_return',0):.2f}%")

    print("\n\n==== 执行规则扫描结果对比 ====")
    print(f"{'规则':<26}{'交易数':>7}{'综合胜率':>10}")
    for s in summary:
        print(f"{s['label']:<26}{s['total_trades']:>7}{s['composite_win_rate']:>9.2f}%")

    candidates = [s for s in summary if s["total_trades"] >= 10]
    if candidates:
        best = max(candidates, key=lambda s: s["composite_win_rate"])
        print(f"\n推荐执行规则: {best['label']} (综合胜率 {best['composite_win_rate']:.2f}%, {best['total_trades']} 笔)")
        for k, v in best["per_strategy"].items():
            print(f"  {k}: {v['n']} 笔, 胜率 {v['wr']:.2f}%")
    else:
        print("\n样本量不足（<10笔），无法给出可信推荐。")


if __name__ == "__main__":
    main()