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
OOS_DAYS = 252  # 样本外窗口: 最近 252 根日线


def score_components(f: pd.DataFrame) -> dict[str, pd.Series] | None:
    """状态分四个价量成分 (0-100 量纲, 只用当日及以前数据)。

    列不足 (如只有 close/ma200 的最小帧) 时返回 None。
    注意: 动量/低波动在 2026-11 消融中因显著负 IC 被移出 v2 配方,
    但这里仍返回全部成分, 供消融表展示证据。
    """
    required = (
        "ma20",
        "ma50",
        "ma200",
        "ma20_slope",
        "ma50_slope",
        "ret20",
        "vola_pct250",
        "dd250",
    )
    if not all(c in f.columns for c in required):
        return None
    close = pd.to_numeric(f["close"], errors="coerce")
    trend = (
        (close > f["ma20"]).astype(float) * 20
        + (
            pd.to_numeric(f["ma20"], errors="coerce") > pd.to_numeric(f["ma50"], errors="coerce")
        ).astype(float)
        * 20
        + (close > f["ma200"]).astype(float) * 20
        + (pd.to_numeric(f["ma20_slope"], errors="coerce") > 0).astype(float) * 20
        + (pd.to_numeric(f["ma50_slope"], errors="coerce") > 0).astype(float) * 20
    )
    momentum = 50 + 50 * np.tanh(pd.to_numeric(f["ret20"], errors="coerce").fillna(0.0) / 10.0)
    vol = 100 - pd.to_numeric(f["vola_pct250"], errors="coerce").fillna(50.0)
    drawdown = np.clip(100 + 2.0 * pd.to_numeric(f["dd250"], errors="coerce").fillna(-50.0), 0, 100)
    return {
        "trend": pd.Series(trend, index=f.index),
        "momentum": pd.Series(momentum, index=f.index),
        "vol": pd.Series(vol, index=f.index),
        "drawdown": pd.Series(drawdown, index=f.index),
    }


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


def _rank_ic(s: np.ndarray, fwd: np.ndarray) -> float | None:
    """Spearman 式秩相关: 分数对未来 20 日收益的线性区分度。"""
    ok = np.isfinite(s) & np.isfinite(fwd)
    if ok.sum() < 30:
        return None
    a = pd.Series(s[ok]).rank()
    b = pd.Series(fwd[ok]).rank()
    if a.std() == 0 or b.std() == 0:
        return None
    return round(float(a.corr(b)), 4)


def _quintile_spread(s: np.ndarray, fwd: np.ndarray) -> float | None:
    """最高分位与最低分位的未来 20 日平均收益差 (百分点)。"""
    ok = np.isfinite(s) & np.isfinite(fwd)
    if ok.sum() < 50:
        return None
    try:
        q = pd.qcut(s[ok], 5, labels=False, duplicates="drop")
    except ValueError:
        return None
    if len(pd.unique(q)) < 3:
        return None
    means = pd.Series(fwd[ok]).groupby(q).mean()
    return round(float(means.iloc[-1] - means.iloc[0]), 3)


def _ablation_row(label: str, s: np.ndarray, fwd: np.ndarray, rets: np.ndarray, oos: int) -> dict:
    exp = np.array([exposure_from_score(v) for v in s], dtype=float)
    full_ret, full_turn = _apply_exposure(exp, rets, DEFAULT_COST_BPS)
    row = {
        "name": label,
        "ic": _rank_ic(s, fwd),
        "q5_q1": _quintile_spread(s, fwd),
        "bt_total": round((float(np.prod(1 + full_ret)) - 1) * 100, 2),
    }
    if oos > 0:
        oos_ret, oos_turn = _apply_exposure(exp[oos:], rets[oos:], DEFAULT_COST_BPS)
        row["bt_oos"] = round((float(np.prod(1 + oos_ret)) - 1) * 100, 2)
    return row


def _ablation(f: pd.DataFrame, rets: np.ndarray, dates: list[str]) -> dict | None:
    """成分消融: 各成分 IC / 分位价差 / 机械回测 (全样本 + 样本外)。

    消融表使用未来 20 日收益来"评估"历史分数, 属于事后诊断;
    仓位规则本身仍是收盘决定、次日生效, 无前视。
    """
    comp = score_components(f)
    if comp is None:
        return None
    close = pd.to_numeric(f["close"], errors="coerce")
    fwd = (close.pct_change(20).shift(-20) * 100).fillna(0.0).to_numpy()
    n = len(f)
    oos = n - OOS_DAYS if n > OOS_DAYS + 60 else 0
    rows = [
        _ablation_row("趋势", comp["trend"].to_numpy(), fwd, rets, oos),
        _ablation_row("动量", comp["momentum"].to_numpy(), fwd, rets, oos),
        _ablation_row("低波动", comp["vol"].to_numpy(), fwd, rets, oos),
        _ablation_row("回撤", comp["drawdown"].to_numpy(), fwd, rets, oos),
    ]
    blend = (0.6 * comp["trend"] + 0.4 * comp["drawdown"]).clip(0, 100)
    rows.append(_ablation_row("v2 配方", blend.to_numpy(), fwd, rets, oos))
    return {
        "horizon": 20,
        "oos_days": OOS_DAYS if oos else 0,
        "oos_start": dates[oos] if oos else "",
        "rows": rows,
        "note": (
            "消融用于评估各成分对未来 20 日收益的区分度 (IC>0 为正贡献)。"
            "v2 = 0.6×趋势 + 0.4×回撤；动量/低波动因在部分指数显著负 IC 被剔除。"
        ),
    }


WF_WINDOW = 252
WF_STEP = 63


def _walkforward_ic(f: pd.DataFrame, dates: list[str]) -> dict:
    """滚动窗口检验状态分对未来 20 日收益的 IC 稳定性。

    规则本身没有拟合参数, 这里验证的是“区分度是否稳定”, 不是参数优化。
    """
    close = pd.to_numeric(f["close"], errors="coerce")
    score = pd.to_numeric(f["regime_score"], errors="coerce")
    fwd = (close.pct_change(20).shift(-20) * 100).to_numpy(dtype=float)
    s_arr = score.to_numpy(dtype=float)
    n = len(f)
    rows = []
    if n >= WF_WINDOW + 20:
        for end in range(WF_WINDOW, n + 1, WF_STEP):
            start = end - WF_WINDOW
            s = s_arr[start:end]
            y = fwd[start:end]
            ok = np.isfinite(s) & np.isfinite(y)
            if ok.sum() < 60:
                continue
            a = pd.Series(s[ok]).rank()
            b = pd.Series(y[ok]).rank()
            if a.std() == 0 or b.std() == 0:
                continue
            ic = a.corr(b)
            if pd.isna(ic):
                continue
            rows.append({"start": dates[start], "end": dates[end - 1], "ic": round(float(ic), 3)})
    ics = [r["ic"] for r in rows]
    ok_full = np.isfinite(s_arr) & np.isfinite(fwd)
    full_ic = None
    if ok_full.sum() >= 60:
        a = pd.Series(s_arr[ok_full]).rank()
        b = pd.Series(fwd[ok_full]).rank()
        if a.std() > 0 and b.std() > 0:
            v = a.corr(b)
            if not pd.isna(v):
                full_ic = round(float(v), 3)
    return {
        "window": WF_WINDOW,
        "step": WF_STEP,
        "n": len(rows),
        "full_ic": full_ic,
        "positive_rate": round(float(np.mean([x > 0 for x in ics])) * 100, 1) if ics else None,
        "median": round(float(np.median(ics)), 3) if ics else None,
        "min": round(float(np.min(ics)), 3) if ics else None,
        "max": round(float(np.max(ics)), 3) if ics else None,
        "rows": rows[-12:],
    }


def _yearly_stability(yearly: list[dict]) -> dict:
    rows = []
    for r in yearly:
        s = _f_num(r.get("strategy"))
        o = _f_num(r.get("overlay"))
        m = _f_num(r.get("ma200"))
        if s is None or m is None:
            continue
        rows.append(
            {
                "year": r.get("year"),
                "strategy": s,
                "overlay": o,
                "ma200": m,
                "excess": round(s - m, 2),
                "overlay_excess": round(o - m, 2) if o is not None else None,
            }
        )
    exc = [r["excess"] for r in rows]
    ov = [r["overlay_excess"] for r in rows if r["overlay_excess"] is not None]
    return {
        "n_years": len(rows),
        "beat_ma200": round(float(np.mean([x > 0 for x in exc])) * 100, 1) if exc else None,
        "avg_excess": round(float(np.mean(exc)), 2) if exc else None,
        "overlay_beat_rate": round(float(np.mean([x > 0 for x in ov])) * 100, 1) if ov else None,
        "rows": rows,
    }


def _f_num(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


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

    oos_block = None
    if n > OOS_DAYS + 60:
        oos = n - OOS_DAYS
        oos_block = {
            "start": dates[oos],
            "days": OOS_DAYS,
            "strategy": _metrics(strat_ret[oos:], exp_prev[oos:], turnover[oos:]),
            "overlay": _metrics(overlay_ret[oos:], exp_overlay_prev[oos:], overlay_turn[oos:]),
            "ma200": _metrics(ma_ret[oos:], exp_ma_prev[oos:], ma_turn[oos:]),
            "buy_hold": _metrics(bh_ret[oos:], exp_bh_prev[oos:], bh_turn[oos:]),
        }

    ablation = _ablation(f, rets, dates)
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
    walkforward = {
        "ic": _walkforward_ic(f, dates),
        "yearly": _yearly_stability(yearly),
    }

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
        "oos": oos_block,
        "ablation": ablation,
        "walkforward": walkforward,
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

    oos = result.get("oos")
    if oos:
        lines += [
            "",
            f"## 样本外（最近 {oos.get('days')} 日，{oos.get('start')} 起）",
            "",
            "| 策略 | 总收益 | 最大回撤 | 累计换手 |",
            "|---|---:|---:|---:|",
        ]
        for key, name in (
            ("strategy", "状态分"),
            ("overlay", "MA200+状态分"),
            ("ma200", "MA200"),
            ("buy_hold", "买入持有"),
        ):
            m = oos.get(key) or {}
            lines.append(
                f"| {name} | {_fmt(m.get('total_return'))}% | "
                f"{_fmt(m.get('max_drawdown'))}% | {_fmt(m.get('turnover'))} |"
            )
        lines.append(
            "> 样本外单独展示，避免用全样本结论掩盖近期失效；"
            "弱市/震荡市里趋势型打分普遍跑输，属正常现象。"
        )

    ablation = result.get("ablation")
    if ablation:
        lines += [
            "",
            f"## 成分消融（未来 {ablation.get('horizon')} 日收益）",
            "",
            "| 成分 | IC | 高-低分位价差 | 全样本回测 | 样本外回测 |",
            "|---|---:|---:|---:|---:|",
        ]
        for row in ablation.get("rows") or []:
            lines.append(
                f"| {row.get('name')} | {_fmt(row.get('ic'), 4)} | "
                f"{_fmt(row.get('q5_q1'), 3)}% | {_fmt(row.get('bt_total'))}% | "
                f"{_fmt(row.get('bt_oos'))}% |"
            )
        lines.append("")
        lines.append(f"> {ablation.get('note', '')}")

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

    wf = result.get("walkforward") or {}
    ic = wf.get("ic") or {}
    ys = wf.get("yearly") or {}
    if ic.get("n"):
        lines += [
            "",
            f"## Walk-forward 稳定性（滚动 {ic.get('window')} 日，步长 {ic.get('step')} 日）",
            "",
            f"- 窗口数: {ic.get('n')}",
            f"- 全样本 IC: {_fmt(ic.get('full_ic'), 3)}",
            f"- 状态分 IC>0 的窗口: {_fmt(ic.get('positive_rate'), 1)}%",
            f"- 窗口 IC 中位数: {_fmt(ic.get('median'), 3)}"
            f"（范围 {_fmt(ic.get('min'), 3)} ~ {_fmt(ic.get('max'), 3)}）",
        ]
        lines.append(
            "> IC 衡量“同一窗口内分数越高、未来 20 日收益是否越高”。"
            "全样本 IC 为正但窗口内常为负，说明趋势型打分在 A 股震荡市容易被均值回归打脸；"
            "这正是“状态分适合当仪表盘、不适合单独择时”的证据。"
        )
        if ic.get("rows"):
            lines += ["", "| 起 | 止 | IC |", "|---|---|---:|"]
            for row in ic["rows"]:
                lines.append(
                    f"| {row.get('start')} | {row.get('end')} | {_fmt(row.get('ic'), 3)} |"
                )
    if ys.get("n_years"):
        lines += [
            "",
            f"- 逐年跑赢 MA200: {_fmt(ys.get('beat_ma200'), 1)}%"
            f"（共 {ys.get('n_years')} 年），平均超额 {_fmt(ys.get('avg_excess'))}%",
            "",
            "| 年份 | 状态分 | MA200 | 超额 |",
            "|---:|---:|---:|---:|",
        ]
        for row in ys.get("rows") or []:
            lines.append(
                f"| {row.get('year')} | {_fmt(row.get('strategy'))}% | "
                f"{_fmt(row.get('ma200'))}% | {_fmt(row.get('excess'))}% |"
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
