#!/usr/bin/env python3
"""三策略·优先级账户回放回测 (纪律 > 价值吸筹 > 左侧买点).

复用 paper_replay_bt 的加载/缓存/回放骨架, 但把"选股/填仓"改为按
STRATEGY_ORDER 优先级语义:

  每只股票每日最多产出一个候选 (依优先级扫描, 第一个命中的策略胜出,
  与 scan_individual 同口径); 候选池再按 (优先级, conf) 排序填仓。

  优先级排序由 wyckoff.strategies.candidates.STRATEGY_ORDER 驱动 (可 --left-first
  切到旧序 纪律>左侧>价值 对照)。左侧买点在回测中:
    - 不受大盘门禁 (弱市兜底);
    - 可执行: 买点 bar 距当日 ≤ ACTIONABLE_LOOK 根;
    - 持仓以 买点自带 stop/target (换算为 stop_pct/take_pct) 出场, 由 paper.step
      逐日判定 (与实盘 fill_buy 同字段)。

用法:
  python scripts/paper_priority_bt.py                  # 默认: 双策略基线 + 三策略新序
  python scripts/paper_priority_bt.py --left-first      # 加对照旧序
  python scripts/paper_priority_bt.py --report docs/paper_priority_bt.md
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
from wyckoff.buypoints import ACTIONABLE_LOOK, KIND_LEFT  # noqa: E402
from wyckoff.strategies import candidates  # noqa: E402

# 优先级权重应叠加是否适用 (回测候选优先级映射建立的名称; 顺序 = STRATEGY_ORDER)
STRAT_KEY = {
    candidates.STRATEGY_DISCIPLINE: "discipline",
    candidates.STRATEGY_VALUE_ACC: "value",
    candidates.STRATEGY_LONG_LEFT: "left",
}
CN = {"discipline": "纪律", "value": "价值吸筹", "left": "威科夫左侧"}

BEAR_TYPES = {"UTAD", "LPSY"}


def precompute_left(rec):
    """预计算该股票全部左侧买点 (因果), 缓存到 rec["left_bps"] (按 bar_idx 升序)。"""
    if rec.get("left_bps") is not None:
        return
    try:
        from wyckoff.buypoints import struct_buy_points

        bps = struct_buy_points(rec["df"], rec["all_evs"], rec["pivots"]) or []
    except Exception:
        bps = []
    rec["left_bps"] = [b for b in bps if b.get("kind") in KIND_LEFT]
    rec["left_bps"].sort(key=lambda b: int(b.get("bar_idx", 0) or 0))


def left_candidate(rec, j, look=ACTIONABLE_LOOK, last_bar=-1):
    """返回 bar j 可执行的左侧买点 (bar_idx∈(last_bar, j], 差≤look) 或 None。

    last_bar: 已用过的买点 bar 下限 (防同一买点窗口内重复建仓)。
    """
    best = None
    for b in rec["left_bps"]:
        bi = int(b.get("bar_idx") or 0)
        if bi > j:
            break
        if bi <= last_bar or (j - bi) > look:
            continue
        if best is None or bi > int(best.get("bar_idx") or 0):
            best = b
    return best


def left_pct(b, open_px):
    """由买点自带 stop/target 换算持仓 stop_pct/take_pct (基于实际成交价)。

    成交价已越界 (open 高于目标或已跌破止损) → 返回 None (错过/跳过)。
    """
    entry = open_px
    if entry <= 0:
        return None
    stop_abs = float(b["stop_price"])
    target_abs = float(b.get("target_price") or entry * 2)
    if entry >= target_abs or entry <= stop_abs:
        return None
    return {
        "stop_pct": 1.0 - stop_abs / entry,
        "take_pct": target_abs / entry - 1.0,
    }


def per_day_candidate(rec, j, params, left_last, mkt_ok, left_on, va_last):
    """依优先级返回该股当日唯一候选 (与 scan_individual 同口径)。

    返回 {conf,type,open,strategy,flow,stop_pct,take_pct,kind,src} 或 None。
    mkt_ok: 当日大盘状态 (仅纪律/价值受用; 左侧免门禁)。
    """
    # 纪律
    ev = pbt.newest_buyable(rec, j, window=params["window"])
    if ev is not None:
        if params.get("mkt_gate") and not mkt_ok:
            return None
        conf = int(ev["conf"] or 0)
        if conf < params["min_conf"]:
            return None
        return {
            "conf": conf,
            "type": ev["type"],
            "open": float(rec["open"][j]),
            "strategy": STRAT_KEY[candidates.STRATEGY_DISCIPLINE],
            "flow": pbt._flow_score(rec, j),
            "kind": "",
            "src": ev,
        }
    # 价值吸筹 (回退)
    va = pbt.va_candidate(rec, j, params["va_m"])
    if va is not None:
        if params.get("mkt_gate") and not mkt_ok:
            return None
        ev = va
        etype = ev["type"]
        econf = int(ev.get("conf") or 0)
        # 入场质量过滤 (回测层参数, 不改数据中心判据):
        #   conf 门槛 / 事件白名单 / 同股同事件冷却
        if econf < params.get("va_min_conf") or 0:
            return None
        if params.get("va_events") and etype not in params["va_events"]:
            return None
        key = (rec["code"], etype)
        last = va_last.get(key, -(10**9))
        if j - last < params.get("va_cooldown") or 0:
            return None
        va_last[key] = j
        if params.get("va_confirm"):
            ci = pbt.va_confirm_idx(rec, int(ev.get("idx") or 0))
            if ci is None or j != ci:
                return None
        return {
            "conf": econf,
            "type": etype,
            "open": float(rec["open"][j]),
            "strategy": STRAT_KEY[candidates.STRATEGY_VALUE_ACC],
            "flow": pbt._flow_score(rec, j),
            "kind": "",
            "src": ev,
            "stop_pct": params.get("va_stop") or None,
            "take_pct": params.get("va_take") or None,
        }
    # 左侧买点 (独立赛道, 不受大盘门禁)
    if not left_on:
        return None
    b = left_candidate(rec, j, last_bar=left_last)
    if b is None:
        return None
    pp = left_pct(b, float(rec["open"][j]))
    if pp is None:
        return None
    return {
        "conf": int(b.get("conf") or 50),
        "type": b.get("type_label")
        if isinstance(b.get("type_label"), str)
        else b.get("label", b["kind"]),
        "open": float(rec["open"][j]),
        "strategy": STRAT_KEY[candidates.STRATEGY_LONG_LEFT],
        "flow": pbt._flow_score(rec, j),
        "kind": b["kind"],
        "src": {"idx": int(b.get("bar_idx") or 0)},
        "stop_pct": pp["stop_pct"],
        "take_pct": pp["take_pct"],
    }


def replay_once(stocks, params, market_gate, left_on, left_first, day_to_j, code_to_idx, all_days):
    """一趟完整回放, 返回 st 状态。参数 left_on=是否启用左侧; left_first=旧序。"""
    from wyckoff.settings_keys import S

    paper.apply_paper_params(
        {
            S.Paper.INIT_CASH: params["init_cash"],
            S.Paper.MAX_POS: params["max_pos"],
            S.Paper.HOLD_BARS: int(params.get("hold_bars") or 10**6),
            S.Paper.STOP_LOSS: params["stop_loss"],
            S.Paper.TAKE_PROFIT: params["take_profit"],
            S.Paper.COST: params["cost"],
            S.Paper.MIN_CONF: params["min_conf"],
            S.Paper.REBALANCE: False,
        }
    )
    cfg = paper._CUR
    st = paper._new_state()
    left_last = {}
    va_last = {}
    # 优先级排序: 新序 纪律>价值>左侧; 旧序 纪律>左侧>价值
    order = ["discipline"]
    if left_on:
        if left_first:
            order += ["left", "value"]
        else:
            order += ["value", "left"]
    else:
        order += ["value"]
    prio = {k: i for i, k in enumerate(order)}

    _real_save = paper.save_state
    paper.save_state = lambda s: None
    from wyckoff import paper_log

    _log_off = paper_log.set_enabled(False)
    try:
        for D in all_days:
            df_by_code = {}
            for pos in st["positions"]:
                idx = code_to_idx.get(pos["symbol"])
                if idx is None:
                    continue
                df_by_code[pos["symbol"]] = pbt._window_df(stocks[idx], D, day_to_j[idx])
            paper.step(st, df_by_code)

            if params.get("bear_exit"):
                for pos in list(st["positions"]):
                    idx = code_to_idx.get(pos["symbol"])
                    if idx is None:
                        continue
                    j = day_to_j[idx].get(D)
                    if j is None:
                        continue
                    bear = pbt.bear_signal_on(stocks[idx], j, window=params["window"])
                    if bear is None:
                        continue
                    grace = int(params.get("va_bear_grace") or 0)
                    if (
                        grace > 0
                        and pos.get("strategy") in ("value", "left")
                        and int(pos.get("entry_bars", 0) or 0) < grace
                    ):
                        continue
                    dfw = df_by_code.get(pos["symbol"])
                    last = (
                        float(dfw["close"].iloc[-1])
                        if dfw is not None and len(dfw)
                        else pos.get("last", pos["buy_px"])
                    )
                    paper.close_position(
                        st, pos, last * (1 - paper.SLIP_SELL), "空头信号", event_type=bear["type"]
                    )

            mkt_ok = True
            if params.get("mkt_gate") and market_gate is not None:
                mk = market_gate.get(D)
                mkt_ok = bool(mk is not None and mk[0] > mk[1])

            cands = []
            for k, rec in enumerate(stocks):
                if paper.has_position(st, rec["code"]):
                    continue
                j = day_to_j[k].get(D)
                if j is None:
                    continue
                last_left = left_last.get(rec["code"], -1)
                cand = per_day_candidate(rec, j, params, last_left, mkt_ok, left_on, va_last)
                if cand is None:
                    continue
                cand["code"] = rec["code"]
                cand["sector"] = rec.get("sector", "")
                cand["chain"] = rec["chain"]
                cands.append(cand)

            cands.sort(key=lambda c: (prio[c["strategy"]], -c["conf"]))

            def _try_fill(cand):
                if len(st["positions"]) >= cfg["max_pos"]:
                    return
                if paper.has_position(st, cand["code"]):
                    return
                cc = params.get("chain_cap") or 0
                if cc and cand["chain"]:
                    n_chain = sum(
                        1
                        for p in st["positions"]
                        if stocks[code_to_idx[p["symbol"]]].get("chain") == cand["chain"]
                    )
                    if n_chain >= cc:
                        return
                if paper._risk_blocks_entry(st, cand, cand["open"]):
                    return
                order_rec = paper._make_order(
                    cand["code"],
                    "",
                    cand["type"],
                    cand["conf"],
                    cand["open"],
                    0,
                    st["cash"],
                    sector=cand["sector"],
                    strategy=cand["strategy"],
                    st=st,
                    stop_pct=cand.get("stop_pct"),
                    take_pct=cand.get("take_pct"),
                )
                if order_rec is None:
                    return
                paper.fill_buy(st, order_rec)
                if cand["strategy"] == "left":
                    left_last[cand["code"]] = int(cand["src"]["idx"])

            # 价值吸筹专属槽位: 非价值策略先填前 max_pos-va_slots 槽, 价值再填剩余
            va_slots = int(params.get("va_slots") or 0)
            if va_slots > 0:
                for cand in cands:
                    if cand["strategy"] == "value":
                        continue
                    if len(st["positions"]) >= cfg["max_pos"] - va_slots:
                        break
                    _try_fill(cand)
                for cand in cands:
                    if cand["strategy"] != "value":
                        continue
                    _try_fill(cand)
            else:
                for cand in cands:
                    _try_fill(cand)

            paper._rebalance_portfolio(st, df_by_code)
            st["equity_hist"].append(
                {
                    "ts": str(D),
                    "cash": round(st["cash"], 2),
                    "equity": round(paper.equity(st, {}), 2),
                }
            )
    finally:
        paper_log.set_enabled(_log_off)
        paper.save_state = _real_save
    return st


def summarize(st):
    s = paper.stats(st)
    by_strat = {}
    for c in st["closed"]:
        key = c.get("strategy") or "discipline"
        by_strat.setdefault(key, []).append(c["ret"])
    rows = {}
    for key, rets in by_strat.items():
        n = len(rets)
        wr = sum(1 for r in rets if r > 0) / n * 100 if n else 0
        mean = sum(rets) / n * 100 if n else 0
        rows[key] = {"n": n, "wr": round(wr, 1), "avg": round(mean, 2)}
    return {
        "total_return": s["total_return"],
        "win_rate": s["win_rate"],
        "max_drawdown": s["max_drawdown"],
        "n_closed": s["n_closed"],
        "n_positions": s["n_positions"],
        "by_strat": rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--max-codes", type=int, default=24)
    ap.add_argument("--codes", default="", help="逗号分隔股票代码 (默认 24 白马池)")
    ap.add_argument(
        "--pool-size",
        type=int,
        default=0,
        help="从全A名单随机抽样 N 只 (固定seed可复现, 过滤受限板块/北交); 与 --codes 二选一",
    )
    ap.add_argument("--conf", type=int, default=100, help="纪律 conf 下限 (实盘权威 100)")
    ap.add_argument("--maxpos", type=int, default=3)
    ap.add_argument("--stop", type=float, default=0.03)
    ap.add_argument("--tp", type=float, default=0.15)
    ap.add_argument("--cost", type=float, default=0.004)
    ap.add_argument("--cash", type=float, default=1_000_000)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--datalen", type=int, default=700)
    ap.add_argument("--start", default="")
    ap.add_argument("--mkt-gate", action="store_true")
    ap.add_argument("--no-bear-exit", action="store_false", dest="bear_exit")
    ap.add_argument("--left-first", action="store_true", help="加跑旧序对照 (纪律>左侧>价值)")
    ap.add_argument("--no-base", action="store_true", help="不跑双策略基线 (无左侧)")
    ap.add_argument("--va-confirm", action="store_true", help="价值: 事件后首根收盘站上MA10才建仓")
    ap.add_argument("--va-min-conf", type=int, default=0, help="价值: 事件 conf 门槛 (0=不限)")
    ap.add_argument(
        "--va-events", default="all", help="价值: 事件白名单逗号分隔 (默认 all=LONG_EVENT_TYPES)"
    )
    ap.add_argument("--va-cooldown", type=int, default=20, help="价值: 同股同事件冷却窗口 (根)")
    ap.add_argument(
        "--va-stop",
        type=float,
        default=0,
        help="价值: 独立止损比例 (0=用全局 stop)",
    )
    ap.add_argument(
        "--va-take",
        type=float,
        default=0,
        help="价值: 独立止盈比例 (0=用全局 tp)",
    )
    ap.add_argument(
        "--va-slots",
        type=int,
        default=0,
        help="价值: 专属槽位数 (默认0=共享; >0 时非价值策略最多 max_pos-va_slots)",
    )
    ap.add_argument("--stocks-cache", default="")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    defaults = paper.apply_paper_params(None)
    if args.conf is not None:
        defaults = {**defaults, "min_conf": args.conf}
    params = {
        "min_conf": args.conf,
        "max_pos": args.maxpos,
        "hold_bars": 999,
        "stop_loss": args.stop,
        "take_profit": args.tp,
        "cost": args.cost,
        "init_cash": args.cash,
        "window": args.window,
        "start": args.start,
        "mkt_gate": args.mkt_gate,
        "bear_exit": args.bear_exit,
        "va_confirm": args.va_confirm,
        "va_min_conf": args.va_min_conf,
        "va_events": frozenset(x.strip() for x in args.va_events.split(",") if x.strip())
        if args.va_events.lower() != "all"
        else None,
        "va_cooldown": args.va_cooldown,
        "va_slots": args.va_slots,
        "va_stop": args.va_stop if args.va_stop > 0 else None,
        "va_take": args.va_take if args.va_take > 0 else None,
        "chain_cap": 0,
        "va_bear_grace": 0,
    }
    # 股票池: 默认真实盘权威 24 白马 (与 three_strategy_analysis 同一池)
    from scripts.three_strategy_analysis import DEFAULT_CODES

    if args.pool_size > 0:
        import json
        import random

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        list_path = os.path.join(repo_root, "wyckoff_all_stocks.json")
        keys = []
        with open(list_path, encoding="utf-8") as f:
            keys = list(json.load(f).keys())
        uni = [c.strip() for c in keys]
        # 裸代码 → 补交易所前缀: 6xx/9xx→sh, 0xx/3xx→sz
        uni = [
            ("sh" if c[0] in "69" else "sz") + c if c[:2] not in ("sh", "sz", "bj") else c
            for c in uni
            if c
        ]
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
        random.seed(20260906)
        codes = random.sample(uni, min(args.pool_size, len(uni)))
        print(f"全A随机抽样 {len(codes)} 只 (seed=20260906)")
    elif args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = [c.strip() for c in DEFAULT_CODES.split(",") if c.strip()]

    stocks = []
    if args.stocks_cache and os.path.exists(args.stocks_cache):
        import pickle

        try:
            with open(args.stocks_cache, "rb") as f:
                stocks = pickle.load(f)
            print(f"加载股票缓存: {len(stocks)} 只")
        except Exception:
            stocks = []
    if not stocks:
        for i, c in enumerate(codes):
            try:
                rec = pbt.load_stock_events(c, args.conf, datalen=args.datalen)
            except Exception as exc:  # 单票失败 (数据源缺失等) 跳过, 不中断全池
                print(f"  [{i + 1}/{len(codes)}] {c} 加载失败, 跳过: {exc}", flush=True)
                rec = None
            if rec is not None:
                precompute_left(rec)
                stocks.append(rec)
            print(
                f"  [{i + 1}/{len(codes)}] {c} 事件{len(rec['events']) if rec else 0}个", flush=True
            )
        if args.stocks_cache:
            import pickle

            os.makedirs(os.path.dirname(os.path.abspath(args.stocks_cache)), exist_ok=True)
            with open(args.stocks_cache, "wb") as f:
                pickle.dump(stocks, f, protocol=4)
            print(f"已写股票缓存: {len(stocks)} 只 → {args.stocks_cache}")
    print(f"\n有效股票 {len(stocks)} 只, 开始回放 ...")

    params["va_m"] = paper._strategy_manager()

    market_gate = pbt.load_market_gate() if args.mkt_gate else None
    day_to_j, code_to_idx = [], {}
    for k, rec in enumerate(stocks):
        m = {d: j for j, d in enumerate(rec["day"])}
        day_to_j.append(m)
        code_to_idx[rec["code"]] = k
    all_days = sorted({d for rec in stocks for d in rec["day"]})
    if args.start:
        all_days = [d for d in all_days if d.date() >= pbt._sdate(args.start)]

    configs = []
    if not args.no_base:
        configs.append(("base_双策略", False, False))
    configs.append(("new_纪律>价值>左侧", True, False))
    if args.left_first:
        configs.append(("old_纪律>左侧>价值", True, True))

    results = []
    for name, left_on, left_first in configs:
        print(
            f"\n===== 配置: {name} ({'含左侧' if left_on else '无左侧'}) "
            f"优先级={'旧' if left_first else '新'} ====="
        )
        st = replay_once(
            stocks, params, market_gate, left_on, left_first, day_to_j, code_to_idx, all_days
        )
        r = summarize(st)
        results.append((name, r))
        print(
            f"已平仓 {r['n_closed']} | 累计 {r['total_return'] * 100:+.2f}% | "
            f"胜率 {r['win_rate'] * 100 if r['win_rate'] is not None else 0:.1f}% | "
            f"最大回撤 {r['max_drawdown'] * 100 if r['max_drawdown'] is not None else 0:.2f}%"
        )
        for s_key in ("discipline", "value", "left"):
            rs = r["by_strat"].get(s_key)
            if rs:
                print(
                    f"  {CN[s_key]:10s} n={rs['n']:3d} 胜率={rs['wr']:5.1f}% 平均={rs['avg']:+.2f}%"
                )

    lines = [
        "# 三策略·优先级账户回放对比",
        "",
        f"- 股票: {len(stocks)} 只 (24白马同一池) · datalen={args.datalen} · "
        f"conf≥{args.conf} · max_pos={args.maxpos} · "
        f"止损-{args.stop * 100:.0f}% · 止盈+{args.tp * 100:.0f}% · 成本{args.cost}",
        "- 纪律/价值: 全局止盈止损+持有上限; 左侧: 买点自带 stop/target "
        "(换算 stop_pct/take_pct, 由引擎逐日判定)",
        "- 事件集: 纪律 {Spring,ST,LPS} (实盘收紧口径) · 价值吸筹 底部整固+20根事件"
        " · 左侧 KIND_LEFT (st_bottom/lps/spring/spring_retest)",
        "",
        "| 配置 | 平仓 | 累计收益 | 胜率 | 最大回撤 | 各策略(笔/胜率/平均) |",
        "|---|---|---|---|---|---|",
    ]
    for name, r in results:
        parts = []
        for s_key in ("discipline", "value", "left"):
            rs = r["by_strat"].get(s_key)
            if rs:
                parts.append(f"{CN[s_key]}{rs['n']}笔/{rs['wr']}%/{rs['avg']:+.1f}%")
        wr = f"{r['win_rate'] * 100:.1f}%" if r["win_rate"] is not None else "-"
        dd = f"{r['max_drawdown'] * 100:.2f}%" if r["max_drawdown"] is not None else "-"
        lines.append(
            f"| {name} | {r['n_closed']} | {r['total_return'] * 100:+.2f}% "
            f"| {wr} | {dd} | {'; '.join(parts)} |"
        )
    lines += ["", "*历史回放 (240分钟K线), 不构成投资建议。*"]
    md = "\n".join(lines)
    print("=" * 60)
    print(md)
    if args.report:
        os.makedirs(os.path.dirname(args.report), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"\n已写出报告: {args.report}")


if __name__ == "__main__":
    main()
