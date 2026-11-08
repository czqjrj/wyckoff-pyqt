"""状态分机械仓位规则的 walk-forward 回测。

只用于回答一个问题: **“状态分到底有没有区分度?”**
- 仓位规则是人为设定的机械映射, 不是优化出来的最优参数。
- 状态分只使用当日及以前数据; 仓位在收盘后决定, 应用到下一交易日收益。
- 包含单边成本 (默认 5bp), 并与 MA200 过滤、买入持有对照。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

DEFAULT_COST_BPS = 5.0


def exposure_from_score(score: float | None) -> float:
    """状态分 → 目标仓位 (0-0.8)。机械规则, 非最优。"""
    try:
        v = float(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    if v >= 70:
        return 0.8
    if v >= 50:
        return 0.6
    if v >= 30:
        return 0.4
    if v >= 20:
        return 0.2
    return 0.0


def overlay_exposure(score: float | None, ma_ok: bool) -> float:
    """MA200 打底 + 状态分只做风险折扣 (0-1)。

    - MA200 下方: 空仓 (0);
    - MA200 上方: 状态分越高仓位越高, 但不再加杠杆。
    这是机械 overlay, 不是优化参数。
    """
    if not ma_ok:
        return 0.0
    try:
        v = float(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.2
    if not math.isfinite(v):
        return 0.2
    if v >= 50:
        return 1.0
    if v >= 30:
        return 0.8
    if v >= 20:
        return 0.5
    return 0.2


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = equity / np.where(peak == 0, 1, peak) - 1
    return float(np.min(dd))


def _metrics(rets: np.ndarray, exposure: np.ndarray, turnover: np.ndarray) -> dict:
    rets = np.asarray(rets, dtype=float)
    n = len(rets)
    if n == 0:
        return {"n": 0}
    equity = np.cumprod(1 + rets)
    total = float(equity[-1] - 1)
    years = n / 252.0
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and 1 + total > 0 else None
    vol = float(np.std(rets, ddof=1) * math.sqrt(252)) if n > 1 else 0.0
    sharpe = (
        (float(np.mean(rets)) / float(np.std(rets, ddof=1)) * math.sqrt(252))
        if n > 1 and np.std(rets, ddof=1) > 0
        else None
    )
    mdd = _max_drawdown(equity)
    calmar = cagr / abs(mdd) if cagr is not None and mdd < 0 else None
    active = rets[exposure > 0]
    win = float(np.mean(active > 0)) if len(active) else None
    return {
        "n": n,
        "total_return": round(total * 100, 2),
        "cagr": round(cagr * 100, 2) if cagr is not None else None,
        "vol": round(vol * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown": round(mdd * 100, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "win_rate": round(win * 100, 1) if win is not None else None,
        "avg_exposure": round(float(np.mean(exposure)) * 100, 1),
        "turnover": round(float(np.sum(turnover)), 2),
        "trades": int(np.sum(turnover > 1e-9)),
    }


def _apply_exposure(
    exp: np.ndarray, rets: np.ndarray, cost_bps: float
) -> tuple[np.ndarray, np.ndarray]:
    """收盘定仓位, 下一日生效; 返回 (策略收益, 换手)。"""
    exp_prev = np.concatenate([[0.0], exp[:-1]])
    turnover = np.abs(np.diff(np.concatenate([[0.0], exp_prev])))
    cost = turnover * cost_bps / 10000.0
    return exp_prev * rets - cost, turnover


def _rolling_compare(
    overlay_ret: np.ndarray,
    base_ret: np.ndarray,
    dates: list[str],
    window: int = 252,
    step: int = 63,
) -> dict:
    """滚动窗口对比 overlay 和 MA200, 看 overlay 是否稳定加分。"""
    n = len(overlay_ret)
    rows = []
    if n >= window:
        for end in range(window, n + 1, step):
            start = end - window
            ov = float(np.prod(1 + overlay_ret[start:end]) - 1)
            ma = float(np.prod(1 + base_ret[start:end]) - 1)
            rows.append(
                {
                    "start": dates[start],
                    "end": dates[end - 1],
                    "overlay": round(ov * 100, 2),
                    "ma200": round(ma * 100, 2),
                    "excess": round((ov - ma) * 100, 2),
                }
            )
    excess = [r["excess"] for r in rows]
    return {
        "window": window,
        "step": step,
        "n": len(rows),
        "win_rate": round(float(np.mean([x > 0 for x in excess])) * 100, 1) if excess else None,
        "avg_excess": round(float(np.mean(excess)), 2) if excess else None,
        "rows": rows,
    }


def backtest_state(f: pd.DataFrame, cost_bps: float = DEFAULT_COST_BPS) -> dict:
    """对包含 close/regime_score/ma200 的日线做机械规则回测。"""
    n = len(f)
    if n < 60:
        return {"ok": False, "reason": "样本不足"}
    close = pd.to_numeric(f["close"], errors="coerce").to_numpy(dtype=float)
    score = pd.to_numeric(f["regime_score"], errors="coerce").to_numpy(dtype=float)
    if "ma200" in f:
        ma200 = pd.to_numeric(f["ma200"], errors="coerce").to_numpy(dtype=float)
    else:
        ma200 = pd.Series(close).rolling(200, min_periods=120).mean().to_numpy()

    rets = np.zeros(n, dtype=float)
    rets[1:] = close[1:] / close[:-1] - 1

    exp = np.array([exposure_from_score(s) for s in score], dtype=float)
    strat_ret, turnover = _apply_exposure(exp, rets, cost_bps)
    exp_prev = np.concatenate([[0.0], exp[:-1]])

    ma_ok = np.isfinite(ma200) & (close > ma200)
    exp_ma = np.where(ma_ok, 1.0, 0.0)
    ma_ret, ma_turn = _apply_exposure(exp_ma, rets, cost_bps)
    exp_ma_prev = np.concatenate([[0.0], exp_ma[:-1]])

    exp_overlay = np.array(
        [overlay_exposure(s, bool(m)) for s, m in zip(score, ma_ok)], dtype=float
    )
    overlay_ret, overlay_turn = _apply_exposure(exp_overlay, rets, cost_bps)
    exp_overlay_prev = np.concatenate([[0.0], exp_overlay[:-1]])

    exp_bh = np.ones(n, dtype=float)
    bh_ret, bh_turn = _apply_exposure(exp_bh, rets, 0.0)
    exp_bh_prev = np.concatenate([[0.0], exp_bh[:-1]])

    dates = pd.to_datetime(f["day"]).dt.strftime("%Y-%m-%d").tolist()
    years = pd.to_datetime(f["day"]).dt.year.to_numpy()
    yearly = []
    for year in sorted(set(years.tolist())):
        mask = years == year
        if not mask.any():
            continue
        yearly.append(
            {
                "year": int(year),
                "strategy": round((np.prod(1 + strat_ret[mask]) - 1) * 100, 2),
                "overlay": round((np.prod(1 + overlay_ret[mask]) - 1) * 100, 2),
                "ma200": round((np.prod(1 + ma_ret[mask]) - 1) * 100, 2),
                "buy_hold": round((np.prod(1 + bh_ret[mask]) - 1) * 100, 2),
            }
        )

    eq_strat = np.cumprod(1 + strat_ret)
    eq_overlay = np.cumprod(1 + overlay_ret)
    eq_ma = np.cumprod(1 + ma_ret)
    eq_bh = np.cumprod(1 + bh_ret)

    return {
        "ok": True,
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "days": n,
        "cost_bps": cost_bps,
        "strategy": _metrics(strat_ret, exp_prev, turnover),
        "overlay": _metrics(overlay_ret, exp_overlay_prev, overlay_turn),
        "ma200": _metrics(ma_ret, exp_ma_prev, ma_turn),
        "buy_hold": _metrics(bh_ret, exp_bh_prev, bh_turn),
        "rolling": _rolling_compare(overlay_ret, ma_ret, dates),
        "yearly": yearly,
        "equity": {
            "dates": dates,
            "strategy": [round(float(v), 4) for v in eq_strat],
            "overlay": [round(float(v), 4) for v in eq_overlay],
            "ma200": [round(float(v), 4) for v in eq_ma],
            "buy_hold": [round(float(v), 4) for v in eq_bh],
        },
    }


def _fmt(x, digits=2) -> str:
    if x is None:
        return "--"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "--"


def render_backtest(result: dict) -> str:
    """回测结果 → Markdown。"""
    if not result.get("ok"):
        return f"# 状态分机械规则回测\n\n{result.get('reason', '样本不足')}"

    lines = [
        "# 状态分机械规则回测",
        "",
        f"- 区间: {result.get('start')} ~ {result.get('end')} ({result.get('days')} 根日线)",
        f"- 成本: 单边 {_fmt(result.get('cost_bps'), 1)} bp",
        "- 规则: 状态分 ≥70 仓 80% / ≥50 60% / ≥30 40% / ≥20 20% / 其余空仓",
        "- Overlay 规则: MA200 下方空仓；上方 ≥50 满仓 / ≥30 80% / ≥20 50% / <20 20%",
        "- 注意: 机械映射，非最优参数；过去不代表未来。",
        "",
        "## 汇总",
        "",
        "| 策略 | 总收益 | 年化 | 最大回撤 | Sharpe | 胜率 | 平均仓位 | 累计换手 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, name in (
        ("strategy", "状态分"),
        ("overlay", "MA200+状态分"),
        ("ma200", "MA200"),
        ("buy_hold", "买入持有"),
    ):
        m = result.get(key) or {}
        lines.append(
            f"| {name} | {_fmt(m.get('total_return'))}% | "
            f"{_fmt(m.get('cagr'))}% | {_fmt(m.get('max_drawdown'))}% | "
            f"{_fmt(m.get('sharpe'))} | {_fmt(m.get('win_rate'), 1)}% | "
            f"{_fmt(m.get('avg_exposure'), 1)}% | {_fmt(m.get('turnover'))} |"
        )

    yearly = result.get("yearly") or []
    if yearly:
        lines += [
            "",
            "## 分年度",
            "",
            "| 年份 | 状态分 | MA200+状态分 | MA200 | 买入持有 |",
            "|---:|---:|---:|---:|---:|",
        ]
        for row in yearly:
            lines.append(
                f"| {row.get('year')} | {_fmt(row.get('strategy'))}% | "
                f"{_fmt(row.get('overlay'))}% | {_fmt(row.get('ma200'))}% | "
                f"{_fmt(row.get('buy_hold'))}% |"
            )

    rolling = result.get("rolling") or {}
    rows = rolling.get("rows") or []
    if rows:
        lines += [
            "",
            "## 滚动窗口对比",
            "",
            f"- 窗口: {rolling.get('window')} 交易日，步长 {rolling.get('step')} 日，"
            f"共 {rolling.get('n')} 个窗口",
            f"- MA200+状态分 跑赢 MA200 的窗口: {_fmt(rolling.get('win_rate'), 1)}%",
            f"- 平均超额: {_fmt(rolling.get('avg_excess'))}%",
            "",
            "| 起 | 止 | MA200+状态分 | MA200 | 超额 |",
            "|---|---|---:|---:|---:|",
        ]
        for row in rows[-12:]:
            lines.append(
                f"| {row.get('start')} | {row.get('end')} | "
                f"{_fmt(row.get('overlay'))}% | {_fmt(row.get('ma200'))}% | "
                f"{_fmt(row.get('excess'))}% |"
            )
        if len(rows) > 12:
            lines.append("")
            lines.append(f"> 只显示最近 12 个窗口，共 {len(rows)} 个。")
    return "\n".join(lines)
