"""模拟盘统计层: 净值/收益统计/绩效分析 (equity/stats/advanced_stats/signal_stats_text)。"""

import statistics
import time

import numpy as np

import wyckoff.paper as paper

from ._params import SLIP_SELL


def _fee_factors(amount: float = 100000.0):
    """(买费率, 卖费率): 在代表性成交金额上的 A股明细费率。

    佣金/过户费/印花税按月付明细计算后折算为单边费率, 供统计口径展示;
    代表性金额取 10 万元, 规避最低佣金 5 元对小单的失真。
    """
    b = paper.fee_buy(amount) / amount
    s = paper.fee_sell(amount) / amount
    return b, s


def net_cost_rate():
    """含买入费用(单边成本)的每股成本系数 = 1 + 代表性买入费率。

    全面适配A股: 用明细费率 (佣金+过户费) 折算, 替代扁平 cost。
    """
    cb, _ = _fee_factors()
    return 1.0 + cb


def float_ret(buy_px, last):
    """当前持仓净浮盈 (%)。

    按当前价即时卖出 (扣卖滑点 + 卖出费用) 后的净收益,
    相对买入含费成本 (买入价含买滑点 + 买入费用)。
    费率按代表性成交金额的明细费率折算 (统计口径)。
    """
    buy_px = float(buy_px)
    if buy_px <= 0:
        return 0.0
    cb, cs = _fee_factors()
    sell_net = float(last) * (1 - SLIP_SELL) * (1 - cs)
    cost = buy_px * (1 + cb)
    return sell_net / cost - 1


def equity(st, df_by_code):
    """总资产 = 现金 + 持仓市值 (未扣未实现卖出费/滑点, 市值口径)。"""
    mv = 0.0
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else pos.get("last", pos["buy_px"])
        mv += px * pos["qty"]
    return st["cash"] + mv


def _record_equity(st, day=None):
    """按交易日 upsert 一条账户净值快照到 equity_hist (同一天多次周期只保留最新)。

    资金曲线与账户概览以这里记录的快照为准: 持仓开仓后每个周期都会刷新,
    避免曲线只能看到平仓点、与现状脱节。
    """
    ts = (day or time.strftime("%Y-%m-%d"))[:10]
    hist = st.get("equity_hist")
    if hist is None:
        hist = st["equity_hist"] = []
    for h in hist:
        if h.get("ts") == ts:
            h["equity"] = round(equity(st, {}), 2)
            h["cash"] = round(st["cash"], 2)
            return
    hist.append({
        "ts": ts,
        "cash": round(st["cash"], 2),
        "equity": round(equity(st, {}), 2),
    })


# ── 收益统计 ─────────────────────────────────────────────
def stats(st):
    """账户/策略统计。返回 dict (供 UI/报告)。

    包含基础统计 + 高级绩效指标:
    - 夏普比率, 索提诺比率, 卡尔马比率
    - 年化收益/波动率
    - 回撤分析 (最大回撤、平均回撤、回撤持续时间)
    - 交易分布统计
    - 风险调整收益指标
    """
    closed = st["closed"]
    out = {
        "cash": round(st["cash"], 2),
        "n_positions": len(st["positions"]),
        "n_closed": len(closed),
        "n_orders": len(st["orders"]),
        "n_pending": len(st["pending"]),
        "n_cond_active": sum(1 for c in st.get("conditions", [])
                             if c.get("status") == "active"),
        "n_cond_done": sum(1 for c in st.get("conditions", [])
                           if c.get("status") == "done"),
        "total_return": 0.0,
        "win_rate": None,
        "pl_ratio": None,
        "avg_ret": None,
        "best": None,
        "worst": None,
        "max_drawdown": None,
        # 高级绩效指标
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "calmar_ratio": None,
        "annual_return": None,
        "annual_volatility": None,
        "downside_volatility": None,
        "avg_drawdown": None,
        "max_drawdown_duration": None,
        "recovery_factor": None,
        "profit_factor": None,
        "expectancy": None,
        "kelly_fraction": None,
        "by_type": {},
        "by_reason": {},
        "by_sector": {},
        "by_strategy": {},
        "monthly_returns": {},
        "trade_distribution": {},
    }
    rets = [c["ret"] for c in closed if c.get("ret") is not None]
    if rets:
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out["win_rate"] = round(len(wins) / len(rets), 4)
        out["avg_ret"] = round(statistics.mean(rets), 4)
        out["best"] = round(max(rets), 4)
        out["worst"] = round(min(rets), 4)
        if losses:
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = abs(statistics.mean(losses))
            out["pl_ratio"] = round(avg_win / avg_loss, 3) if avg_loss > 0 else None
            out["profit_factor"] = round(sum(wins) / abs(sum(losses)), 3) if losses else None
            # 期望值
            out["expectancy"] = round(out["win_rate"] * avg_win - (1 - out["win_rate"]) * avg_loss, 4)
            # Kelly 分数
            if avg_loss > 0 and avg_win > 0:
                out["kelly_fraction"] = round((out["win_rate"] * avg_win - (1 - out["win_rate"]) * avg_loss) / avg_win, 4)
        # 分类型
        by_type = {}
        for c in closed:
            t = c.get("type", "?")
            b = by_type.setdefault(t, {"n": 0, "rets": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
        for t, b in by_type.items():
            rs = b["rets"]
            b["avg"] = round(statistics.mean(rs), 4)
            b["win"] = round(sum(1 for r in rs if r > 0) / len(rs), 4)
            if any(r > 0 for r in rs) and any(r <= 0 for r in rs):
                b["pl_ratio"] = round(
                    statistics.mean([r for r in rs if r > 0]) / abs(statistics.mean([r for r in rs if r <= 0])), 3)
            else:
                b["pl_ratio"] = 0
            b.pop("rets", None)
        out["by_type"] = by_type
        # 平仓原因分布
        by_reason = {}
        for c in closed:
            r = c.get("reason", "?")
            by_reason.setdefault(r, {"n": 0, "avg": []})["n"] += 1
            by_reason[r]["avg"].append(c["ret"])
        for r, b in by_reason.items():
            b["avg"] = round(statistics.mean(b["avg"]), 4)
        out["by_reason"] = by_reason
        # 行业分布
        by_sector = {}
        for c in closed:
            sec = c.get("sector", "未知")
            b = by_sector.setdefault(sec, {"n": 0, "rets": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
        for sec, b in by_sector.items():
            rs = b["rets"]
            b["avg"] = round(statistics.mean(rs), 4)
            b["win"] = round(sum(1 for r in rs if r > 0) / len(rs), 4)
            b.pop("rets", None)
        out["by_sector"] = by_sector
        # 策略分层 (模拟盘三策略并线: 纪律 / 左侧买点 / 价值吸筹) — 盈利能力口径。
        # cum 用 (1+r) 连乘再减一, 反映真实累计, 避免简单累加被单笔大单主导。
        by_strategy = {}
        for c in closed:
            s = c.get("strategy", "") or "未知"
            b = by_strategy.setdefault(s, {"n": 0, "rets": [], "bars": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
            b["bars"].append(int(c.get("bars", 0) or 0))
        for s, b in by_strategy.items():
            rs = b["rets"]
            wins = [r for r in rs if r > 0]
            losses = [r for r in rs if r <= 0]
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = abs(statistics.mean(losses)) if losses else 0.0
            cum = 1.0
            for r in rs:
                cum *= 1.0 + r
            b["win"] = round(len(wins) / len(rs), 4)
            b["avg"] = round(statistics.mean(rs), 4)
            b["cum"] = round(cum - 1.0, 4)
            b["pl_ratio"] = round(avg_win / avg_loss, 3) if avg_loss > 0 else None
            b["expectancy"] = round(
                (len(wins) / len(rs)) * avg_win
                - (1 - len(wins) / len(rs)) * avg_loss, 4)
            b["avg_hold"] = round(statistics.mean(b["bars"]), 1) if b["bars"] else 0.0
            b.pop("rets", None)
            b.pop("bars", None)
        out["by_strategy"] = by_strategy
        # 交易分布统计
        out["trade_distribution"] = {
            "n_wins": len(wins),
            "n_losses": len(losses),
            "avg_win": round(statistics.mean(wins), 4) if wins else 0,
            "avg_loss": round(statistics.mean(losses), 4) if losses else 0,
            "largest_win": round(max(wins), 4) if wins else 0,
            "largest_loss": round(min(losses), 4) if losses else 0,
            "avg_hold_bars": round(statistics.mean([c.get("bars", 0) for c in closed]), 1),
        }

    # 总收益率: 当前总资产 (现金+持仓市值) 相对初始模拟资金, 含未平仓浮盈亏
    init = float(paper._CUR["init_cash"])
    mv = sum(float(p.get("last", p["buy_px"])) * p["qty"] for p in st["positions"])
    eq_now = out["cash"] + round(mv, 2)
    out["equity"] = round(eq_now, 2)
    out["total_return"] = round(eq_now / init - 1, 4) if init else 0.0

    hist = st.get("equity_hist") or []
    eqs = [h.get("equity", init) for h in hist] if hist else []
    if eqs:
        # 计算日收益率序列
        daily_rets = []
        for i in range(1, len(eqs)):
            if eqs[i-1] > 0:
                daily_rets.append(eqs[i] / eqs[i-1] - 1)

        if daily_rets and np is not None:
            arr = np.asarray(daily_rets, dtype=float)
            # 年化收益率 (假设日线, 250 个交易日)
            out["annual_return"] = round(float(arr.mean() * 250), 4)
            # 年化波动率
            out["annual_volatility"] = round(float(arr.std() * np.sqrt(250)), 4)
            # 下行波动率 (仅负收益)
            neg_rets = arr[arr < 0]
            out["downside_volatility"] = round(float(neg_rets.std() * np.sqrt(250)), 4) if len(neg_rets) > 1 else 0.0

            # 夏普比率 (假设无风险利率 3%)
            rf = 0.03 / 250  # 日无风险利率
            excess = arr - rf
            if excess.std() > 0:
                out["sharpe_ratio"] = round(float(excess.mean() / excess.std() * np.sqrt(250)), 3)

            # 索提诺比率
            if out["downside_volatility"] and out["downside_volatility"] > 0:
                out["sortino_ratio"] = round(float((arr.mean() - rf) * 250 / out["downside_volatility"]), 3)

            # 最大回撤 (累积净值口径: 峰值为累计净值的滚动最高, 而非单日因子的最高)
            cum = np.cumprod(arr + 1)
            peak = np.maximum.accumulate(cum)
            dd = cum / peak - 1
            out["max_drawdown"] = round(float(dd.min()), 4)

            # 卡尔马比率
            if out["max_drawdown"] and out["max_drawdown"] < 0:
                out["calmar_ratio"] = round(out["annual_return"] / abs(out["max_drawdown"]), 3)

            # 平均回撤
            out["avg_drawdown"] = round(float(dd[dd < 0].mean()), 4) if any(dd < 0) else 0.0

            # 最大回撤持续时间
            in_dd = dd < 0
            if any(in_dd):
                dd_starts = np.where(np.diff(np.concatenate(([False], in_dd))))[0]
                dd_ends = np.where(np.diff(np.concatenate((in_dd, [False]))))[0]
                if len(dd_starts) == len(dd_ends):
                    durations = dd_ends - dd_starts
                    out["max_drawdown_duration"] = int(durations.max()) if len(durations) > 0 else 0

            # 恢复因子 = 总净收益 / 最大回撤
            if out["max_drawdown"] and out["max_drawdown"] < 0:
                out["recovery_factor"] = round(out["total_return"] / abs(out["max_drawdown"]), 3)

    return out
