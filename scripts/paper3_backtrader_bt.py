#!/usr/bin/env python3
"""模拟盘三策略 backtrader 组合回测 (准确度 + 盈利能力).

复用 simulated trading 三策略信号生成 (scripts.three_strategy_analysis):
  - 纪律      (paper_discipline_bull):     强多头事件 {Spring,ST,LPS} conf>=下限
  - 左侧买点  (long_buy_left):             结构买点自带 entry/stop/target
  - 价值吸筹  (screener_value_accumulation): 底部整固 + 近20根吸筹事件
信号接入 bt.Cerebro 做组合持仓模拟 (≤max_pos 只, 等权, 单边成本):
  - 开仓: 信号日提交市价单, 下一 bar 开盘成交 (与模拟盘 T+1 口径一致);
  - 出场: 逐 bar 盘中判定 止损/固定止盈/移动止盈(回落%)/持有≤hold根到期, 次日开盘成交;
  - 弱市过滤 (--idx-code 指数日线): 指数收盘在 MA20 之下 → 仓位上限降为1 且停止价值开仓;
  - 价值降权 (--va-weight/--va-slots): 价值吸筹单仓资金×权重, 同时最多持有 va-slots 只.
输出:
  1. 信号方向准确度 (信号 bar 收盘 → 5/10/20 根后方向命中, 与 accuracy 面板同口径);
  2. 组合盈利能力 (backtrader 净值): 总收益/CAGR/夏普/最大回撤/胜率/盈亏比;
  3. 按策略分组的平仓盈利明细.

用法:
  python scripts/paper3_backtrader_bt.py                       # 默认 24 只蓝筹, 近4年(datalen=1000)
  python scripts/paper3_backtrader_bt.py --codes sh600519,sz000001
  python scripts/paper3_backtrader_bt.py --datalen 1000 --min-conf 90 --max-pos 3
  python scripts/paper3_backtrader_bt.py --report docs/paper3_backtrader_bt.md
"""
import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault(
    "WYCKOFF_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "paper_replay_data"),
)

import backtrader as bt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from scripts import three_strategy_analysis as tsa  # noqa: E402


# ── 数据与信号 ─────────────────────────────────────────────
def _with_day(rec, sig):
    s = dict(sig)
    s["day"] = str(rec["day"][sig["idx"]])[:10]
    s["code"] = rec["code"]
    return s


def build_signals(rec, min_conf, window, horizon, include_order):
    by = tsa.all_signals(rec, min_conf, window, horizon, include_order)
    out = []
    for s in include_order:
        for sig in by.get(s, []):
            out.append(_with_day(rec, sig))
    return out


def align_frames(recs, lo=None, hi=None):
    """把所有股票 K 线统一 reindex 到全局交易日轴 (缺失前/后填充), 保证多 data 对齐。"""
    dfs = []
    for rec in recs:
        d = rec["df"][["day", "open", "high", "low", "close"]].copy()
        d["day"] = pd.to_datetime(d["day"])
        if lo is not None:
            d = d[d["day"] >= lo]
        if hi is not None:
            d = d[d["day"] < hi]
        dfs.append(d)
    alldays = sorted({pd.Timestamp(x) for d in dfs for x in d["day"]})
    out = {}
    for rec, d in zip(recs, dfs):
        d = d.set_index("day").reindex(alldays).ffill().bfill()
        out[rec["code"]] = d
    return out


# ── backtrader 组合策略 ────────────────────────────────────
class Paper3BTStrategy(bt.Strategy):
    params = (
        ("codes", ()),
        ("sigmap", {}),
        ("dfmap", {}),
        ("max_pos", 3),
        ("stop", 0.03),
        ("tp", 0.15),
        ("cost", 0.004),
        ("hold", 20),
        ("trail", 0.08),
        ("weak_on", True),
        ("idx_code", ""),
        ("va_weight", 0.6),
        ("va_slots", 1),
    )

    def __init__(self):
        self.code2data = {d._name: d for d in self.datas
                          if d._name in self.p.codes}
        self.data2code = {id(d): c for c, d in self.code2data.items()}
        self.idx = next((d for d in self.datas
                         if getattr(d, "_name", "") == self.p.idx_code), None)
        self.idx_ma20 = (bt.ind.SMA(self.idx.close, period=20)
                         if self.idx is not None else None)
        self.held = {}          # code -> {size, entry_px, entry_day, strategy, bars, max_high, tp_hit}
        self.pending_sells = {}  # code -> reason
        self._order_info = {}   # order.ref -> {"kind", "code", "strategy"}
        self.closed = []        # 平仓记录
        self.equity = []        # 每 bar 总资产

    def _day(self):
        return f"{self.datas[0].datetime.date(0).isoformat()}"

    def _weak(self):
        if not (self.idx is not None and self.idx_ma20 is not None):
            return False
        try:
            close = self.idx.close[0]
            ma = self.idx_ma20[0]
        except Exception:
            return False
        return bool(close < ma)

    def next(self):
        day = self._day()
        # 1) 现有持仓出场判定 (逐 bar 盘中触发 → 次日收盘卖出)
        for code in list(self.held):
            pos = self.held[code]
            df = self.p.dfmap[code]
            try:
                row = df.loc[pd.Timestamp(day)]
            except KeyError:
                continue
            high = float(row["high"])
            low = float(row["low"])
            entry = pos["entry_px"]
            i_entry = df.index.get_loc(pd.Timestamp(pos["entry_day"]))
            i_now = df.index.get_loc(pd.Timestamp(day))
            pos["bars"] = i_now - i_entry + 1
            max_high = max(pos.get("max_high", entry), high)
            pos["max_high"] = max_high
            if high >= entry * (1 + self.p.tp) and self.p.trail > 0:
                pos["tp_hit"] = True
            if low <= entry * (1 - self.p.stop):
                reason = "止损"
            elif pos.get("tp_hit") and self.p.trail > 0 \
                    and low <= max_high * (1 - self.p.trail):
                reason = "移动止盈"
            elif high >= entry * (1 + self.p.tp) and self.p.trail == 0:
                reason = "止盈"
            elif pos["bars"] >= self.p.hold:
                reason = "到期"
            else:
                reason = None
            if reason:
                self.pending_sells[code] = reason
                o = self.close(data=self.code2data[code], size=pos["size"])
                self._order_info[o.ref] = {"kind": "sell", "code": code}
        # 2) 新开仓 (当日信号)
        weak = self.p.weak_on and self._weak()
        allowed = 1 if weak else self.p.max_pos
        va_now = sum(1 for p in self.held.values()
                     if p["strategy"] == "screener_value_accumulation")
        for sig in self.p.sigmap.get(day, []):
            code = sig["code"]
            strategy = sig["strategy"]
            if code in self.held or code in self.pending_sells:
                continue
            if self.p.weak_on and weak and \
                    strategy == "screener_value_accumulation":
                continue
            if strategy == "screener_value_accumulation":
                if va_now >= self.p.va_slots:
                    continue
            if len(self.held) >= allowed:
                continue
            df = self.p.dfmap[code]
            try:
                est_close = float(df.loc[pd.Timestamp(day), "close"])
            except KeyError:
                continue
            if est_close <= 0:
                continue
            w = self.p.va_weight if strategy == "screener_value_accumulation" else 1.0
            target_val = self.broker.getvalue() / self.p.max_pos * 0.95 * w
            size = int(target_val / est_close / 100) * 100
            if size < 100:
                continue
            o = self.buy(data=self.code2data[code], size=size)
            self._order_info[o.ref] = {
                "kind": "buy", "code": code,
                "strategy": strategy, "row": sig,
            }
        # 3) 记录总资产
        self.equity.append(self.broker.getvalue())

    def notify_order(self, order):
        if order.status not in (bt.Order.Completed, bt.Order.Canceled,
                                bt.Order.Margin, bt.Order.Rejected):
            return
        info = self._order_info.pop(order.ref, None)
        if info is None:
            return
        code = info["code"]
        if order.status == bt.Order.Completed:
            if info["kind"] == "buy":
                strategy = info["strategy"]
                entry_day = str(self.datas[0].datetime.date(0))
                # 左侧买点: 成交价刷新为该股 next-open, 止损/止盈沿用买点自带目标
                self.held[code] = {
                    "size": float(order.executed.size),
                    "entry_px": float(order.executed.price),
                    "entry_day": entry_day,
                    "strategy": strategy,
                    "bars": 1,
                    "max_high": float(order.executed.price),
                    "tp_hit": False,
                }
            else:
                pos = self.held.pop(code, None)
                reason = self.pending_sells.pop(code, "卖出")
                if pos is None:
                    return
                exit_px = float(order.executed.price)
                self.closed.append({
                    "code": code,
                    "strategy": pos["strategy"],
                    "entry": pos["entry_px"],
                    "exit": exit_px,
                    "ret": exit_px / pos["entry_px"] - 1 - self.p.cost,
                    "bars": pos.get("bars", 1),
                    "reason": reason,
                    "exit_day": str(self.datas[0].datetime.date(0)),
                    "qty": float(order.executed.size),
                })
        else:
            # 未成交买入: 丢弃等待的持仓; 若持仓卖出未成交则保留
            if info["kind"] == "buy" and self._order_info.get(order.ref) is not None:
                self._order_info.pop(order.ref, None)


# ── 组合统计 ───────────────────────────────────────────────
def equity_stats(eq, days):
    arr = np.asarray([float(x) for x in eq])
    n = len(arr)
    if n < 2:
        return {"n_days": n, "total": 0.0, "cagr": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    total = arr[-1] / arr[0] - 1
    years = max(n / 252.0, 1e-9)
    cagr = (arr[-1] / arr[0]) ** (1 / years) - 1
    rets = np.diff(arr) / arr[:-1]
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0.0
    peak = np.maximum.accumulate(arr)
    dd = (peak - arr) / peak
    return {"n_days": n, "total": total, "cagr": cagr,
            "sharpe": sharpe, "max_dd": float(dd.max())}


def closed_stats(closed):
    rets = [c["ret"] for c in closed]
    if not rets:
        return {}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    cum = 1.0
    for r in rets:
        cum *= 1.0 + r
    return {
        "n": len(rets),
        "win_rate": len(wins) / len(rets),
        "avg_ret": statistics.mean(rets),
        "cum_ret": cum - 1,
        "pl_ratio": (statistics.mean(wins) / abs(statistics.mean(losses))
                     if wins and losses else None),
        "avg_bars": statistics.mean([c["bars"] for c in closed]),
    }


def hit_stats(sig_hits):
    """sig_hits: list[ {h: ret} | None ] → 方向命中率/均值。"""
    out = {}
    for h in ("5", "10", "20"):
        vals = [t[h] for t in sig_hits if t and h in t]
        if vals:
            out[f"hit{h}"] = sum(1 for v in vals if v > 0) / len(vals)
            out[f"avg{h}"] = statistics.mean(vals)
        else:
            out[f"hit{h}"] = None
            out[f"avg{h}"] = None
    return out


# ── 主流程 ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--codes", default=tsa.DEFAULT_CODES)
    ap.add_argument("--datalen", type=int, default=1000, help="近4年=1000根日线")
    ap.add_argument("--min-conf", type=int, default=90)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--stop", type=float, default=0.05, help="止损 (基准=0.03)")
    ap.add_argument("--tp", type=float, default=0.15)
    ap.add_argument("--trail", type=float, default=0.08,
                    help="移动止盈回落% (0=固定止盈模式)")
    ap.add_argument("--cost", type=float, default=0.004)
    ap.add_argument("--max-pos", type=int, default=3)
    ap.add_argument("--init-cash", type=float, default=1_000_000)
    ap.add_argument("--idx-code", default="sh000001",
                    help="弱市断言指数 (空则关闭弱市过滤)")
    ap.add_argument("--weak-off", action="store_true",
                    help="关闭弱市(指数MA20下)降仓与停价值")
    ap.add_argument("--va-weight", type=float, default=0.6,
                    help="价值吸筹单仓资金权重")
    ap.add_argument("--va-slots", type=int, default=1,
                    help="价值吸筹同时持有上限")
    ap.add_argument("--start", default=None, help="回测区间起点 YYYY-MM-DD (含)")
    ap.add_argument("--end", default=None, help="回测区间终点 YYYY-MM-DD (不含)")
    ap.add_argument("--report", default="docs/paper3_backtrader_bt.md")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    include_order = ["paper_discipline_bull", "long_buy_left",
                     "screener_value_accumulation"]
    lo = pd.to_datetime(args.start) if args.start else None
    hi = pd.to_datetime(args.end) if args.end else None
    if lo is not None and hi is not None and lo >= hi:
        ap.error("--start 必须早于 --end")

    recs, failed = [], []
    for c in codes:
        r = tsa.load_stock(c, args.datalen)
        if r is None:
            failed.append(c)
            continue
        sgs = build_signals(r, args.min_conf, args.window, args.horizon,
                            include_order)
        if lo is not None or hi is not None:
            sgs = [s for s in sgs
                   if (lo is None or pd.Timestamp(s["day"]) >= lo)
                   and (hi is None or pd.Timestamp(s["day"]) < hi)]
        r["signals"] = sgs
        recs.append(r)
    print(f"股票池: {len(recs)} 只 (失败 {len(failed)} {failed})")
    if not recs:
        print("无可用股票, 退出")
        return
    print(f"回测区间: {lo or '最早'} ~ {hi or '最新'}")

    # ── 信号准确度 (方向化命中, 与 accuracy 面板同口径) ──
    print("\n拉取近4年行情并逐信号评估 (信号方向准确度)...")
    per_strategy_hits = {s: [] for s in include_order}
    for rec in recs:
        for sig in rec["signals"]:
            t = tsa.eval_signal(rec, sig, args.stop, args.tp, args.cost,
                                args.horizon, args.horizon)
            per_strategy_hits[sig["strategy"]].append(
                t["hits"] if t else None)
    acc = {}
    for s in include_order:
        hs = hit_stats(per_strategy_hits[s])
        acc[s] = hs
    for s in include_order:
        a = acc[s]
        def _f(k): return f"{a[k] * 100:.0f}%" if a[k] is not None else "-"
        def _g(k): return f"{a[k] * 100:+.1f}%" if a[k] is not None else "-"
        print(f"  准确度 {s:28s} 命中5/10/20={_f('hit5')} {_f('hit10')} {_f('hit20')} "
              f"均值20={_g('avg20')}")

    # ── backtrader 组合回测 ──
    print("\n构建 backtrader Cerbero 组合回测...")
    frames = align_frames(recs, lo, hi)
    sigmap = {}
    for rec in recs:
        for sig in rec["signals"]:
            sigmap.setdefault(sig["day"], []).append(sig)

    # 弱市断言指数 (指数收盘 < MA20 → 弱势) -> 追加为回测 data
    weak_on = not args.weak_off
    idx_code = (args.idx_code or "").strip()
    if idx_code in ("500000", "sh000001", "000001.SH"):
        idx_code = "sh000001"
    idx_df = None
    if weak_on and idx_code:
        try:
            from wyckoff.datasource import fetch_kline
            idf = fetch_kline(idx_code, datalen=args.datalen, scale=240)
            if idf is not None and len(idf):
                idx_df = idf[["day", "open", "high", "low", "close"]].copy()
                idx_df["day"] = pd.to_datetime(idx_df["day"])
                if lo is not None:
                    idx_df = idx_df[idx_df["day"] >= lo]
                if hi is not None:
                    idx_df = idx_df[idx_df["day"] < hi]
                if idx_df.empty:
                    idx_df = None
        except Exception as e:
            print(f"警告: 指数 {idx_code} 加载失败: {e}, 弱市过滤关闭")
            idx_df = None

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(args.init_cash)
    cerebro.broker.setcommission(commission=args.cost, percabs=True)
    bt_codes = [c for c in codes if c in frames]
    for c in bt_codes:
        df = frames[c][["open", "high", "low", "close"]].copy()
        cerebro.adddata(bt.feeds.PandasData(dataname=df, name=c))
    if idx_df is not None:
        alldays_i = sorted({pd.Timestamp(d) for d in idx_df["day"]})
        idx_df = (idx_df.set_index("day").reindex(alldays_i)
                  .ffill().bfill())
        cerebro.adddata(
            bt.feeds.PandasData(dataname=idx_df[["open", "high", "low", "close"]].copy(),
                                name=idx_code))
    weak_active = weak_on and idx_df is not None
    print("\n=== 交易规则 ===")
    print(f"  止损 {args.stop*100:.0f}% · 固定止盈 {args.tp*100:.0f}% · "
          f"移动止盈回落 {args.trail*100:.0f}% · 持有≤{args.horizon}根")
    print(f"  弱市过滤(指数{idx_code} MA20下→1仓+停价值): "
          f"{'开启' if weak_active else '关闭'}")
    print(f"  价值吸筹: 资金×{args.va_weight} · 同时≤{args.va_slots}只")
    cerebro.addstrategy(
        Paper3BTStrategy,
        codes=bt_codes,
        sigmap=sigmap,
        dfmap=frames,
        max_pos=args.max_pos,
        stop=args.stop,
        tp=args.tp,
        trail=args.trail,
        cost=args.cost,
        hold=args.horizon,
        weak_on=(not args.weak_off) and (idx_df is not None),
        idx_code=idx_code,
        va_weight=args.va_weight,
        va_slots=args.va_slots,
    )
    results = cerebro.run(runonce=False)
    strat = results[0]
    eq_stats = equity_stats(strat.equity, len(strat.equity))
    closed = strat.closed
    port_stats = closed_stats(closed)

    print("\n=== backtrader 组合盈利 ===")
    print(f"  总收益 {eq_stats['total']*100:+.2f}%  CAGR {eq_stats['cagr']*100:+.2f}%  "
          f"夏普 {eq_stats['sharpe']:.2f}  最大回撤 {eq_stats['max_dd']*100:.1f}%  "
          f"bars={eq_stats['n_days']}")
    if port_stats:
        print(f"  平仓 {port_stats['n']} 笔 胜率 {port_stats['win_rate']*100:.0f}%  "
              f"平均 {port_stats['avg_ret']*100:+.2f}% 累计 {port_stats['cum_ret']*100:+.2f}%  "
              f"盈亏比 {port_stats['pl_ratio']} 均持 {port_stats['avg_bars']:.1f} 根")

    # 按策略的平仓盈利
    by_strat = {}
    for c in closed:
        by_strat.setdefault(c["strategy"], []).append(c)
    print("\n=== 按策略平仓明细 ===")
    for s in include_order:
        st = closed_stats(by_strat.get(s, []))
        if not st:
            print(f"  {s:28s} 无平仓")
            continue
        print(f"  {s:28s} n={st['n']:3d} 胜率 {st['win_rate']*100:.0f}% "
              f"平均 {st['avg_ret']*100:+.2f}% 累计 {st['cum_ret']*100:+.2f}% "
              f"盈亏比 {st['pl_ratio']} 均持 {st['avg_bars']:.1f}")

    # 按策略的信号数
    counts = {s: sum(1 for rec in recs for sg in rec["signals"]
                     if sg["strategy"] == s) for s in include_order}
    print("\n信号数: " + ", ".join(f"{s}={counts[s]}" for s in include_order))
    if eq_stats["total"] > 0:
        end_v = args.init_cash * (1 + eq_stats["total"])
        print(f"期末资产: {end_v:,.0f}")

    _write_report(args.report, args, codes, recs, acc, eq_stats,
                  port_stats, by_strat, counts, failed)
    print(f"\n报告已写入: {args.report}")


def _pct(v, sig=1, pos=True):
    if v is None:
        return "-"
    s = "+" if (pos and v > 0) else ""
    return f"{s}{v * 100:.{sig}f}%"


def _write_report(path, args, codes, recs, acc, eq, port, by_strat,
                  counts, failed):
    span = f"{args.start or '最早'} ~ {args.end or '最新'}"
    weak_txt = "开启" if not args.weak_off else "关闭"
    L = ["# 模拟盘三策略 backtrader 组合回测报告", "",
         "- 生成时间: 2026-09-07 自动回测",
         f"- 回测区间: {span}",
         f"- 股票池: {len(recs)} 只蓝筹 ({len(failed)} 失败 {failed}) · datalen={args.datalen} 根日线",
         f"- 纪律条件: min_conf={args.min_conf} · 窗口={args.window}根",
         f"- 组合规则: 同持≤{args.max_pos} · 单边成本{args.cost} · "
         f"止损{args.stop*100:.0f}% / 止盈{args.tp*100:.0f}% / "
         f"移动止盈回落{args.trail*100:.0f}% / 持有≤{args.horizon}根",
         f"- 弱市过滤(指数{args.idx_code} MA20下→1仓+停价值): {weak_txt} · "
         f"价值吸筹: 资金×{args.va_weight} · 同时≤{args.va_slots}只",
         "",
         "## 1. 信号准确度 (方向化命中, 信号 bar 收盘 → N 根后)",
         "",
         "| 策略 | 信号数 | 命中5 | 命中10 | 命中20 | 均值20 |",
         "|---|---|---|---|---|---|"]
    for s in tsa.TYPES_ORDER if hasattr(tsa, "TYPES_ORDER") else [
            "paper_discipline_bull", "long_buy_left",
            "screener_value_accumulation"]:
        a = acc[s]
        L.append(f"| {s} | {counts[s]} | {_pct(a['hit5'], 0, pos=False)} | {_pct(a['hit10'], 0, pos=False)} | "
                 f"{_pct(a['hit20'], 0, pos=False)} | {_pct(a['avg20'], 2)} |")
    L += ["", "> 命中=信号后 N 根收益>0; 与模拟盘 accuracy 面板口径一致。", "",
          "## 2. 组合盈利能力 (backtrader 净值)",
          "",
          f"- 总收益: **{_pct(eq['total'], 2)}** · CAGR {_pct(eq['cagr'], 2)} · "
          f"夏普 {eq['sharpe']:.2f} · 最大回撤 {_pct(-eq['max_dd'], 2)} · "
          f"回测天数 {eq['n_days']}",
          f"- 平仓 {port.get('n', 0)} 笔 · 胜率 {_pct(port.get('win_rate'), 0, pos=False)} · "
          f"平均 {_pct(port.get('avg_ret'), 2)} · 累计 {_pct(port.get('cum_ret'), 2)} · "
          f"盈亏比 {port.get('pl_ratio')} · 均持 {port.get('avg_bars')} 根",
          "",
          "| 策略 | 平仓 | 胜率 | 平均 | 累计 | 盈亏比 | 均持(根) |",
          "|---|---|---|---|---|---|---|"]
    for s in ["paper_discipline_bull", "long_buy_left",
              "screener_value_accumulation"]:
        st = closed_stats(by_strat.get(s, []))
        if not st:
            L.append(f"| {s} | 0 | - | - | - | - | - |")
        else:
            L.append(f"| {s} | {st['n']} | {_pct(st['win_rate'], 0, pos=False)} | "
                     f"{_pct(st['avg_ret'], 2)} | {_pct(st['cum_ret'], 2)} | "
                     f"{st['pl_ratio']} | {st['avg_bars']} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
