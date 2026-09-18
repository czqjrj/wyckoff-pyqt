#!/usr/bin/env python3
"""三道硬门禁·历史消融回测 (mkt / flow / sect 开→关 分项对照).

复用 paper_replay_bt 的加载/回放骨架 (含 P4.2 板块强度历史快照 strength_at,
以及大盘 MA20 历史前缀因果重建), 对同一股票池把 8 种门禁组合各跑一趟完整账户
回放, 输出累计收益/胜率/盈亏比/最大回撤对照 —— 回答 docs/strategy4 ③号盲区:
"板块>60分位 + 资金净流入>0" 是否有正贡献, 三门齐开是否过度过滤。

门禁口径与 paper_replay_bt 严格一致:
  - mkt : 当日上证收盘>MA20 才开新仓 (因果重建, 无前视; 指数数据缺失=不满足)
  - flow: 信号日近5根量价净流入占比, 按『当日候选池 ≥ 截面中位』过滤 (fail-close)
  - sect: 资金… 板块强度历史快照分位≥0.6 (无快照/无映射放行 fail-open)
除门禁外全部参数各配置一致, 保证差异纯由门禁引起。

用法:
  python scripts/paper_gate_ablation.py --max-codes 100 --stocks-cache /tmp/gates.pkl
  python scripts/paper_gate_ablation.py --quick      # 只跑 none/mkt/flow/sect/all 五项
  python scripts/paper_gate_ablation.py --report docs/paper_gate_ablation.md
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault(
    "WYCKOFF_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", "data", "paper_replay_data"),
)


from scripts import paper_replay_bt as pbt  # noqa: E402
from wyckoff import paper  # noqa: E402

# ── 门禁组合 (8 组合全覆盖) ────────────────────────────────
GATE_NONE = ()
GATES_ALL = ("mkt", "flow", "sect")
ALL_COMBS = [
    (GATE_NONE, "none 三门全关(基线)"),
    (("mkt",), "mkt 仅大盘MA20"),
    (("flow",), "flow 仅资金流"),
    (("sect",), "sect 仅板块强度"),
    (("mkt", "flow"), "mkt+flow"),
    (("mkt", "sect"), "mkt+sect"),
    (("flow", "sect"), "flow+sect"),
    (GATES_ALL, "all 三门齐开(现行生产)"),
]
QUICK_COMBS = [
    (GATE_NONE, "none 三门全关(基线)"),
    (("mkt",), "mkt 仅大盘MA20"),
    (("flow",), "flow 仅资金流"),
    (("sect",), "sect 仅板块强度"),
    (GATES_ALL, "all 三门齐开(现行生产)"),
]
CN = {"mkt": "大盘", "flow": "资金", "sect": "板块"}


def _comb_label(gates):
    return "+".join(CN[g] for g in gates)


def _perf(st):
    """账户账目指标: 平仓/胜率/累计/盈亏比/最大回撤/期望(平均每笔)。"""
    s = paper.stats(st)
    closed = st.get("closed") or []
    wins = [c["ret"] for c in closed if c["ret"] > 0]
    loss = [c["ret"] for c in closed if c["ret"] <= 0]
    plr = (sum(wins) / len(wins)) / abs(sum(loss) / len(loss)) if wins and loss else None
    avg = sum(c["ret"] for c in closed) / len(closed) if closed else None
    return {
        "n": s["n_closed"],
        "wr": s["win_rate"],
        "total": s["total_return"],
        "plr": plr,
        "dd": s["max_drawdown"],
        "avg": avg,
        "n_pos": s["n_positions"],
    }


def run_one(stocks, params, market_gate, gates):
    p = dict(params)
    p["mkt_gate"] = "mkt" in gates
    p["flow_gate"] = "flow" in gates
    p["sect_gate"] = "sect" in gates
    return pbt.replay(stocks, p, market_gate=market_gate if p["mkt_gate"] else None)


def load_universe(args, repo_root):
    uni = []
    if args.universe_file:
        from wyckoff.utils import normalize_symbol

        with open(args.universe_file, encoding="utf-8") as f:
            uni = [normalize_symbol(c.strip()) for c in f if c.strip()]
    elif args.pool_size and args.pool_size > 0:
        import json
        import random

        from wyckoff.utils import normalize_symbol

        with open(os.path.join(repo_root, "wyckoff_all_stocks.json"), encoding="utf-8") as f:
            all_codes = list(json.load(f).keys())
        random.seed(20260918)
        uni = random.sample(all_codes, min(args.pool_size, len(all_codes)))
        uni = [normalize_symbol(c) for c in uni]
    else:
        import json

        from wyckoff.utils import normalize_symbol

        with open(os.path.join(repo_root, "wyckoff_all_stocks.json"), encoding="utf-8") as f:
            uni = [normalize_symbol(c) for c in json.load(f).keys()]
    uni = [
        c
        for c in uni
        if not (
            c.startswith("sh688")
            or c.startswith("sh689")
            or c.startswith("sz300")
            or c.startswith("sz301")
            or c.startswith("bj")
        )
    ]
    return (uni or [])[: args.max_codes]


def build_params(args, defaults):
    return {
        "min_conf": args.conf if args.conf is not None else defaults["min_conf"],
        "max_pos": args.maxpos if args.maxpos is not None else defaults["max_pos"],
        "hold_bars": args.hold if args.hold is not None else 999,
        "stop_loss": args.stop if args.stop is not None else defaults["stop_loss"],
        "take_profit": args.tp if args.tp is not None else defaults["take_profit"],
        "trailing_stop": args.trailing_stop,
        "trail_back_pct": args.trail_back if args.trail_back is not None
        else defaults["trail_back_pct"],
        "trail_activate_pct": args.trail_activate if args.trail_activate is not None
        else defaults["trail_activate_pct"],
        "trail_atr_mult": defaults["trail_atr_mult"],
        "stop_cooldown": args.stop_cooldown,
        "cost": args.cost if args.cost is not None else defaults["cost"],
        "init_cash": args.cash if args.cash is not None else defaults["init_cash"],
        "window": args.window,
        "chain_cap": args.chain_cap,
        "chain_min_pct": args.chain_min_pct,
        "start": args.start,
        "mkt_gate": False,
        "flow_gate": False,
        "sect_gate": False,
        "bear_exit": args.bear_exit,
        "qlib_veto": False,
        "qlib_veto_hi": 0.60,
        "strategy_track": args.track,
        "va_confirm": False,
        "event_types": None,  # 回放候选用已入库事件集 (Spring-only 生产口径)
        "sec_event_types": frozenset(),
        "sec_slots": 0,
        "disc_confirm": None,
        "va": False,
        "va_slots": 0,
        "va_bear_grace": 0,
    }


def load_stocks(args, uni, params, repo_root):
    stocks = None
    if args.stocks_cache and os.path.exists(args.stocks_cache):
        import pickle

        try:
            with open(args.stocks_cache, "rb") as f:
                stocks = pickle.load(f)
            print(f"加载股票缓存: {len(stocks)} 只 ({args.stocks_cache})")
        except Exception as e:
            print(f"股票缓存读取失败, 重新加载: {e}")
            stocks = None
    if stocks is not None:
        return stocks
    stocks = []
    for i, code in enumerate(uni):
        try:
            rec = pbt.load_stock_events(code, params["min_conf"], args.datalen, None)
        except Exception as e:
            print(f"  [{i + 1}/{len(uni)}] {code} 失败: {e}")
            rec = None
        if rec is None:
            continue
        stocks.append(rec)
        print(f"  [{i + 1}/{len(uni)}] {code} 事件{len(rec['events'])}个", flush=True)
    if args.stocks_cache:
        import pickle

        os.makedirs(os.path.dirname(os.path.abspath(args.stocks_cache)), exist_ok=True)
        with open(args.stocks_cache, "wb") as f:
            pickle.dump(stocks, f, protocol=4)
        print(f"已写股票缓存: {len(stocks)} 只 → {args.stocks_cache}")
    return stocks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max-codes", type=int, default=100, help="扫描股票数上限")
    ap.add_argument("--universe-file", default="", help="自定义股票池文件 (每行一个代码)")
    ap.add_argument("--pool-size", type=int, default=0,
                    help="全A随机抽样 N 只 (固定seed可复现; 与默认取全A前N只二选一)")
    ap.add_argument("--conf", type=int, default=None)
    ap.add_argument("--maxpos", type=int, default=None)
    ap.add_argument("--hold", type=int, default=None)
    ap.add_argument("--stop", type=float, default=None)
    ap.add_argument("--tp", type=float, default=None)
    ap.add_argument("--trail-back", type=float, default=None)
    ap.add_argument("--trail-activate", type=float, default=None)
    ap.add_argument("--no-trail", action="store_false", dest="trailing_stop")
    ap.add_argument("--stop-cooldown", type=int, default=0)
    ap.add_argument("--chain-cap", type=int, default=0)
    ap.add_argument("--chain-min-pct", type=float, default=0)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--datalen", type=int, default=700)
    ap.add_argument("--start", default="")
    ap.add_argument("--no-bear-exit", action="store_false", dest="bear_exit",
                    help="关闭空头信号卖出 (默认开)")
    ap.add_argument("--track", action="store_true", help="开启策略信号追踪(默认关, 消融不需要)")
    ap.add_argument("--quick", action="store_true", help="只跑 none/mkt/flow/sect/all 五项")
    ap.add_argument("--stocks-cache", default="")
    ap.add_argument("--report", default="", help="写出对比报告 md 路径")
    args = ap.parse_args()

    defaults = paper.apply_paper_params(None)
    params = build_params(args, defaults)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    uni = load_universe(args, repo_root)
    print(
        f"消融股票池 {len(uni)} 只: conf≥{params['min_conf']} 持仓≤{params['max_pos']} "
        f"止损-{params['stop_loss'] * 100:.0f}% 止盈+{params['take_profit'] * 100:.0f}% "
        f"回撤周期起始={args.start or '全段'}"
    )
    stocks = load_stocks(args, uni, params, repo_root)
    market_gate = pbt.load_market_gate()
    if market_gate is None:
        print("大盘指数数据缺失 → mkt 组合将视为全不满足 (见 load_market_gate)", file=sys.stderr)

    combs = QUICK_COMBS if args.quick else ALL_COMBS
    results = []
    for gates, name in combs:
        st = run_one(stocks, params, market_gate, gates)
        p = _perf(st)
        results.append((name, gates, p))
        wr = f"{p['wr'] * 100:.1f}%" if p["wr"] is not None else "-"
        plr = f"{p['plr'] * 100:.0f}%" if p["plr"] is not None else "-"
        dd = f"{p['dd'] * 100:.2f}%" if p["dd"] is not None else "-"
        avg = f"{p['avg'] * 100:+.3f}%" if p["avg"] is not None else "-"
        print(
            f"\n===== {name} =====\n"
            f"平仓 {p['n']:3d} | 胜率 {wr} | 累计 {p['total'] * 100:+.2f}% | "
            f"盈亏比 {plr} | 最大回撤 {dd} | 平均/笔 {avg}"
        )

    head = ["| 门禁组合 | 每次开仓条件 | 平仓 | 胜率 | 累计收益 | 盈亏比 | 最大回撤 | 平均/笔 |",
            "|---|---|---|---|---|---|---|---|"]
    rows = []
    for name, gates, p in results:
        cond = _comb_label(gates) if gates else "无门禁(事件即买)"
        wr = f"{p['wr'] * 100:.1f}%" if p["wr"] is not None else "-"
        plr = f"{p['plr']:.2f}" if p["plr"] is not None else "-"
        dd = f"{p['dd'] * 100:.2f}%" if p["dd"] is not None else "-"
        avg = f"{p['avg'] * 100:+.3f}%" if p["avg"] is not None else "-"
        rows.append(
            f"| {name} | {cond} | {p['n']} | {wr} | {p['total'] * 100:+.2f}% | "
            f"{plr} | {dd} | {avg} |"
        )
    md = "# 三道硬门禁·历史消融回测 (mkt/flow/sect 分项开→关)\n\n"
    md += f"- 口径: {len(stocks)} 只 · datalen={args.datalen} · conf≥{params['min_conf']} · " \
          f"持仓≤{params['max_pos']} · 止损-{params['stop_loss'] * 100:.0f}% · " \
          f"止盈+{params['take_profit'] * 100:.0f}% · 事件集 Spring-only\n"
    md += "- 门禁: mkt=大盘收盘>MA20(因果重建) · flow=当日候选池截面中位(fail-close) · " \
          "sect=板块历史快照分位≥0.6(fail-open)\n"
    md += "\n".join(head + rows) + "\n"
    md += "\n*历史回放 (240分钟K线), 不构成投资建议。*\n"
    print("=" * 60)
    print(md)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\n已写出报告: {args.report}")


if __name__ == "__main__":
    main()
