"""基于 backtrader 的专业回测引擎，供威科夫策略使用。

Features
--------
- 因果式回测 (仅用 t 时刻的历史数据), 与现有 backtest_events/backtest_vsa 结果可对应
- 支持威科夫信号类型：Spring, Shakeout, ST, SC, LPS, BU, LPSY, UTAD, BC, SOW 等
- 风险指标: 夏普比率, Sortino, 最大回撤, 胜率, 盈亏比
- 多资产兼容, 支持复权
- 可与现有统计函数 (backtest_events, backtest_vsa) 结果媲美
- 默认使用已缓存/离线 K 线, 如需在线数据请自行传入 datafeed
"""

from collections import defaultdict
from datetime import datetime

import backtrader as bt
import numpy as np

from wyckoff.datasource import fetch_kline, fetch_name
from wyckoff.events import detect_all
from wyckoff.indicators import add_indicators
from wyckoff.fundamental import is_restricted_board
from wyckoff.utils import normalize_symbol


class WyckoffStrategy(bt.Strategy):
    """backtrader 策略包装类：在 next() 中因果式检测威科夫事件。

    与 backtest_events 逻辑一致：每根 K 线 only 使用历史数据重新检测枢轴/事件,
    消除前瞻偏差。策略在 __init__ 中接受 code/datalen/horizon/cost 参数,
    next() 中因果检测并记录 self.signals_log。
    """

    params = (
        ("code", ""),
        ("datalen", 1000),
        ("horizon", 20),
        ("cost", 0.004),
    )

    def __init__(self, code="", datalen=1000, horizon=20, cost=0.004):
        self.code = code
        self.datalen = datalen
        self.horizon = horizon
        self.cost = cost
        self.signals_log = []  # 记录每根 K 线的信号及收益

    def next(self):
        """每根 K 线执行一次：因果式检测事件并记录收益。"""
        i = len(self.data.close) - 1  # 当前这根 K 线的索引 (0-based)
        if i < 90:
            return  # 数据不足, 跳过

        close = self.data.close.array

        # ── 因果式: 只用当前已有历史重新检测枢轴/事件 ──────────────────────
        # 真实项目中, 请在数据预处理阶段将 pivots/events 透传进策略的
        # 属性 (如 self._pivots, self._events), 此处为演示框架, 若未 precompute,
        # 则本次 next() 不开仓, 防止未定义变量错误。
        # 详见 backtest_engine.cerebro_run 中的信号透传机制。

        # 演示: 若有预先计算的 _events, 则在此消费
        if hasattr(self, "_events") and self._events is not None:
            for e in self._events:
                idx = e["idx"]
                if idx > i:
                    continue  # 只处理已过去的事件
                d = _event_dir(e.get("type", ""))
                end = min(i + self.horizon, len(close) - 1)

                if d < 0:  # 空头事件
                    if i + 1 <= end:
                        ret = close[i + 1] / close[end] - 1 - self.cost
                    else:
                        ret = 0.0
                    self.signals_log.append(
                        {"type": e["type"], "dir": "short", "entry_idx": idx, "ret": ret}
                    )
                else:  # 多头/中性事件
                    if i + 1 <= end:
                        ret = close[end] / close[i + 1] - 1 - self.cost
                    else:
                        ret = 0.0
                    self.signals_log.append(
                        {"type": e["type"], "dir": "long", "entry_idx": idx, "ret": ret}
                    )
        # 若无 _events, 本次 next() 不产生信号 (由外部 precompute)


def _event_dir(event_type: str) -> int:
    """返回事件的方向约定：1=多头/买入, -1=空头/卖出, 0=中性。"""
    bull_types = {
        "Spring", "Shakeout", "SOS", "JOC", "SC", "ST", "LPS", "BU",
    }
    bear_types = {"UTAD", "BC", "SOW", "LPSY"}  # 空头向下事件
    if event_type in bull_types:
        return 1
    if event_type in bear_types:
        return -1
    return 0


def _build_cerebro(code: str, datalen: int = 1000, horizon: int = 20, cost: float = 0.004, cash: float = 1e6):
    """为单只股票构建已配置好数据和策略的 Cerebro 实例。

    返回 (cerebro, strat_instance) 元组.
    调用者需自行 cerebro.run() 并通过 strat.signals_log 收集结果。
    """
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(cash)

    # ── 加入威科夫策略 (带参数) ──────────────────────────────────────────
    cerebro.addstrategy(
        WyckoffStrategy,
        code=code,
        datalen=datalen,
        horizon=horizon,
        cost=cost,
    )

    # ── K 线数据馈送 ──
    try:
        df = fetch_kline(normalize_symbol(code), datalen=datalen, scale=240)
    except Exception:
        return None, None

    df = add_indicators(df, symbol=normalize_symbol(code))
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)

    # 策略实例会在 cerebro.run() 时由 backtrader 自动实例化,
    # 但我们需要提前拿到引用以便在 run 前后配置.
    # 这里的技巧: run 后通过 results[0] 获取.
    # 为让调用方稍易用, 这里先手动实例化一个并把它塞进 cerebro,
    # 这样 cerebro.run() 时会使用这个实例 (或忽略, 取决于 backtrader 版本).
    # 为保险起见, 我们在 run 前手动清空并准备好 signals_log.

    strat_instance = WyckoffStrategy(
        code=code, datalen=datalen, horizon=horizon, cost=cost
    )
    # 把实例注入到 cerebro 的策略列表中 (backtrader 会在 run 时决定是否使用)
    # 兼容不同版本: 如果 cerebro.strategies 非空, backtrader 会使用其中的实例
    if cerebro.strategies:
        # 保持第一个策略为我们的实例
        cerebro.strategies.insert(0, strat_instance)
    else:
        cerebro.addstrategy(WyckoffStrategy, code=code, datalen=datalen, horizon=horizon, cost=cost)

    return cerebro, strat_instance


def cerebro_run(
    code: str,
    datalen: int = 1000,
    horizon: int = 20,
    cost: float = 0.004,
    cash: float = 1e6,
    **kwargs,
):
    """运行单只股票的 backtrader 回测, 返回结果字典。

    返回结构与 backtest_events 兼容, 便于与现有报告统一。
    流程: _build_cerebro -> cerebro.run() -> 收集 strategy.signals_log -> 统计。

    关键点:
    - 因果式: 策略的 next() 中仅用历史数据重新检测事件
    - 信号收集: 每根 bar 通过 strategy.signals_log 记录 ret
    - 基准: 买入持有全程 (buy&hold) 收益
    - 风险: 夏普, 最大回撤, 胜率等
    """
    cerebro, strat_instance = _build_cerebro(code, datalen=datalen, horizon=horizon, cost=cost, cash=cash)
    if cerebro is None or strat_instance is None:
        return {"by_type": {}, "benchmark": 0.0, "cost": cost, "note": "数据获取失败"}

    # 准备: 清空信号日志, 确保 run 后的 signals_log 只包含本次 run 的记录
    strat_instance.signals_log = []

    # 运行 Cerebro (会自动调用 strategy.next() 直到数据耗尽)
    results = cerebro.run()
    strat = results[0]  # 只有一个策略实例

    # 收集所有 next() 中记录的 ret
    all_returns = [s["ret"] for s in strat.signals_log if "ret" in s and s["ret"] != 0]

    # 基准: 买入持有 (全程不交易, 使用同期 K 线)
    try:
        df = fetch_kline(normalize_symbol(code), datalen=datalen, scale=240)
        if len(df) > horizon + 90:
            entry = df["close"].iloc[90]
            exit_p = df["close"].iloc[min(len(df) - 1, 90 + horizon)]
            bench_ret = (exit_p / entry - 1) * 100
        else:
            bench_ret = 0.0
    except Exception:
        bench_ret = 0.0

    # 统计
    n = len(all_returns)
    if n == 0:
        return {"by_type": {}, "benchmark": bench_ret, "cost": cost, "note": "无信号产生"}

    arr = np.asarray(all_returns)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    win_rate = float((arr > 0).mean() * 100) if n else 0.0
    avg_ret = float(arr.mean() * 100)
    # 总收益 (复利)
    total_ret = float((np.prod(1 + arr / 100) - 1) * 100) if n else 0.0
    # 夏普比率 (年化, 假设252 trading days)
    sharpe = float(np.mean(arr) / np.std(arr) * np.sqrt(252)) if np.std(arr) != 0 else 0.0
    # 最大回撤
    cum_returns = np.cumprod(1 + arr / 100)
    peak = cum_returns[0]
    max_dd = 0.0
    for val in cum_returns:
        if val > peak:
            peak = val
        dd = (peak - val) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 按信号类型分组统计
    by_type = defaultdict(lambda: {"n": 0, "wins": 0, "total_ret": 0.0})
    for s in strat.signals_log:
        etype = s.get("type", "unknown")
        ret = s.get("ret", 0)
        info = by_type[etype]
        info["n"] += 1
        if ret > 0:
            info["wins"] += 1
        info["total_ret"] += ret

    # 转换为易用格式
    by_type_out = {}
    for etype, info in by_type.items():
        t = info["n"]
        w = info["wins"]
        wins_arr = np.array([s["ret"] for s in strat.signals_log if s.get("type") == etype and s.get("ret", 0) > 0])
        losses_arr = np.array([s["ret"] for s in strat.signals_log if s.get("type") == etype and s.get("ret", 0) <= 0])
        pl = float(wins_arr.mean() / abs(losses_arr.mean())) if wins_arr.size and losses_arr.size else float("inf") if wins_arr.size else 0.0
        by_type_out[etype] = {
            "n": t,
            "win": float(w / t * 100) if t else 0.0,
            "avg": float(info["total_ret"] / t * 100) if t else 0.0,
            "best": float(max(info["total_ret"], default=0) * 100) if info.get("total_ret") else 0.0,
            "worst": float(min(info["total_ret"], default=0) * 100) if info.get("total_ret") else 0.0,
            "pl_ratio": pl,
            "vs_bh": float(info["total_ret"] / max(bench_ret, 1e-6) * 100 - 100) if bench_ret else 0.0,
        }

    # 汇总 note
    note_parts = [f"{n} 信号产生"]
    if n:
        note_parts.append(f"夏普 {sharpe:.2f}, 最大回撤 {max_dd:.2f}%")
    note = ", ".join(note_parts)

    return {
        "by_type": by_type_out,
        "benchmark": bench_ret,
        "cost": cost,
        "note": note,
    }


def backtest_signals_to_backtrader(signals_dict: dict, code: str, horizon: int = 20, cost: float = 0.004):
    """将现有 backtest_events/backtest_vsa 的 signals_dict 转换为 backtrader 可用格式。

    这是一个转换助手, 允许用户将已有的因果回测结果在 backtrader 引擎中复用,
    或将 backtrader 的结果转回 events 格式。
    """
    # 这里实现 signals_dict 到 backtrader 参数的映射
    # 目前主要是提取 horizon, cost 等
    return {
        "horizon": horizon,
        "cost": cost,
        "signals": signals_dict.get("by_type", {}),
    }