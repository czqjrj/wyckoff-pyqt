#!/usr/bin/env python3
"""Spring-only 回放: 真实净值口径的完整指标 + 最大回撤窗口定位。

复用 paper_replay_bt 引擎与股票事件缓存, 直接输出账户级统计 (总收益/胜率/
盈亏比/最大回撤/夏普/CAGR)、最大回撤峰值→谷底日, 以及逐年收益。

用法:
  python scripts/spring_only_dd_analysis.py
  python scripts/spring_only_dd_analysis.py --maxpos 4 --stop 0.04 --trail-back 0.08
  python scripts/spring_only_dd_analysis.py --cache data/paper_replay_data/spring_only_conf100.pkl

缓存生成 (首次, 需联网拉取 200 只):
  python scripts/paper_replay_bt.py --max-codes 200 --conf 100 --start 2023-06-01 \
      --stocks-cache data/paper_replay_data/spring_only_conf100.pkl --no-bear-exit
"""
import argparse
import collections
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault(
    "WYCKOFF_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "paper_replay_data"),
)

import paper_replay_bt as pbt  # noqa: E402

from wyckoff import paper  # noqa: E402

DEFAULT_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "paper_replay_data", "spring_only_conf100.pkl"
)


def main():
    ap = argparse.ArgumentParser(description="Spring-only 回撤分析")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="股票事件缓存 pickle")
    ap.add_argument("--conf", type=int, default=100)
    ap.add_argument("--maxpos", type=int, default=4)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--stop", type=float, default=0.04)
    ap.add_argument("--tp", type=float, default=0.15)
    ap.add_argument("--cost", type=float, default=0.004)
    ap.add_argument("--trail-back", type=float, default=0.08)
    ap.add_argument("--no-trail", action="store_false", dest="trailing_stop")
    ap.add_argument("--bear-exit", action="store_true", help="事件型空头信号卖出")
    ap.add_argument("--mkt-gate", action="store_true", help="大盘20日线门禁")
    ap.add_argument("--start", default="2023-06-01")
    args = ap.parse_args()

    recs = pickle.load(open(args.cache, "rb"))
    params = {
        "init_cash": 1_000_000.0,
        "max_pos": args.maxpos,
        "hold_bars": args.hold,
        "stop_loss": args.stop,
        "take_profit": args.tp,
        "cost": args.cost,
        "min_conf": args.conf,
        "window": 10,
        "start": args.start,
        "mkt_gate": args.mkt_gate,
        "flow_gate": False,
        "sect_gate": False,
        "trailing_stop": args.trailing_stop,
        "trail_back_pct": args.trail_back,
        "trail_activate_pct": 0.0,
        "trail_atr_mult": 0.0,
        "bear_exit": args.bear_exit,
        "qlib_veto": False,
        "qlib_veto_hi": 0.9,
        "strategy_track": True,
        "va_confirm": False,
        "disc_confirm": None,
        "va": False,
        "va_slots": 0,
        "va_bear_grace": 0,
    }
    market_gate = pbt.load_market_gate() if args.mkt_gate else None
    st = pbt.replay(recs, params, market_gate=market_gate)
    s = paper.stats(st)
    hist = st.get("equity_hist") or []

    print(f"股票池 {len(recs)} 只 · 交易日 {len(hist)} · "
          f"conf>={args.conf} maxpos={args.maxpos} stop=-{args.stop:.0%} "
          f"trail={args.trail_back:.0%}")
    for k in ("total_return", "annual_return", "win_rate", "pl_ratio",
              "max_drawdown", "sharpe_ratio", "recovery_factor", "n_closed", "avg_ret"):
        print(f"  {k:18s} = {s.get(k)}")

    peak = peak_date = None
    max_dd = 0.0
    lo = hi = None
    year_end = collections.OrderedDict()
    for h in hist:
        e = h["equity"]
        year_end[str(h["ts"])[:4]] = e
        if peak is None or e > peak:
            peak, peak_date = e, h["ts"]
        dd = e / peak - 1.0
        if dd < max_dd:
            max_dd, lo, hi = dd, h["ts"], peak_date
    print(f"最大回撤: {max_dd*100:.2f}%  峰值={hi} 谷底={lo}")

    prev = params["init_cash"]
    print("年度:")
    for y, e in year_end.items():
        print(f"  {y}: {(e/prev-1)*100:+.1f}%  (末值 {e:,.0f})")
        prev = e


if __name__ == "__main__":
    main()
