#!/usr/bin/env python3
"""模拟盘止盈/止损/持有时长参数网格扫描.

策略: 一次加载股票事件检测 (fetch+detect_all) 后, 先并行预热价值吸筹判据缓存
(evaluate_strategy_value_accumulation 是唯一昂贵判据 ~0.65s/bar, 且与止损/止盈参数
无关), 再对 stop_loss × take_profit (× hold_bars) 网格逐格调用
scripts.paper_replay_bt.replay 做完整回放模拟 (冷缓存后每格秒级)。
统计每格 n / 胜率 / 均收 / 盈亏比 / 盈亏因子 / 最大回撤 / Sharpe / 累计收益,
输出按累计收益排序的对比表。

用法:
  python scripts/paper_replay_grid.py                              # 默认网格, 打印+写 docs/paper_replay_grid.md
  python scripts/paper_replay_grid.py --stops 0.03,0.05,0.08 --tps 0.10,0.15,0.20
  python scripts/paper_replay_grid.py --hold 10,20,30 --start 2023-06-01 --mkt-gate
  python scripts/paper_replay_grid.py --workers 8                   # 并行预热进程数(默认 cpu-1)

网格排序: 主指标累计收益(账面); 同时展示风险指标供人工权衡。
"""
import argparse
import os
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from paper_replay_bt import (  # noqa: E402
    _va_cache,
    load_market_gate,
    load_qlib_prob_map,
    load_stock_events,
    replay,
    va_candidate,
)

from wyckoff import paper  # noqa: E402

LONG = paper.LONG_EVENT_TYPES


def _worker_va(args):
    """子进程: 对单只股票的需要判定的 bars 批量求价值吸筹候选。

    返回 (stock_index, {j: candidate_or_None})。
    """
    i, rec, jlist = args
    try:
        va_m = paper._strategy_manager()
    except Exception as e:
        print(f"[worker {os.getpid()}] 策略管理器不可用: {e}; 价值吸筹跳过", flush=True)
        va_m = None
    out = {}
    for j in jlist:
        try:
            out[j] = va_candidate(rec, j, va_m)
        except Exception:
            out[j] = None
    return i, out


def _cache_path():
    return os.path.join(os.path.dirname(__file__), "..", "data", "va_cache.pkl")


def _bar_sig(rec):
    df = rec["df"]
    last = df.iloc[-1] if len(df) else None
    if last is None:
        return None
    return [str(last["day"]), round(float(last["close"]), 4)]


def load_va_cache(stocks, datalen):
    """把磁盘上的价值吸筹缓存填入内存 _va_cache (按 code 键, 带上次bar签名校验)。

    返回命中的股票数; 未命中/数据过期则跳过 (重新并行预热)。
    """
    path = _cache_path()
    if not os.path.exists(path):
        return 0
    try:
        import pickle
        with open(path, "rb") as f:
            data = pickle.load(f)
    except Exception:
        return 0
    if data.get("datalen") != datalen:
        return 0
    codes = data.get("codes") or {}
    hit = 0
    for rec in stocks:
        code = rec["code"]
        entry = codes.get(code)
        if not entry:
            continue
        if entry.get("meta") != _bar_sig(rec):
            continue
        for j, cand in (entry.get("bars") or {}).items():
            if cand:
                _va_cache[(id(rec), int(j))] = cand
        hit += 1
    return hit


def save_va_cache(stocks, datalen):
    """把内存 _va_cache 按 code 聚合写盘, 供下次网格复用 (跳过昂贵预热)。"""
    codes = {}
    for rec in stocks:
        code = rec["code"]
        bars = {}
        for k, v in _va_cache.items():
            if k[0] != id(rec):
                continue
            if v:
                bars[k[1]] = v
        if bars:
            codes[code] = {"meta": _bar_sig(rec), "bars": bars}
    if not codes:
        return
    try:
        import pickle
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"datalen": datalen, "codes": codes}, f, protocol=4)
    except Exception as e:
        print(f"[warn] 缓存写盘失败: {e}", flush=True)


def prewarm_va(stocks, workers=None, datalen=850, verbose=True):
    """并行预热水暖所有可能触发价值吸筹判据的 (股票, bar), 填充 paper_replay_bt._va_cache。

    先在父进程把所有 bar 标 False (预筛不过/无候选), 再用子进程并行求事件区
    (近20根内 LONG 事件) 的 bar。优先复用磁盘缓存; 未命中才并行计算并写盘。
    返回用时(秒)。
    """
    t0 = time.time()
    n_cache = load_va_cache(stocks, datalen)
    done = True
    for rec in stocks:
        n = len(rec["df"])
        for j in range(n):
            if (id(rec), j) not in _va_cache and done:
                done = False
            _va_cache.setdefault((id(rec), j), False)
    if not done:
        n_cpu = (os.cpu_count() or 4) if workers is None else workers
        if workers is None:
            n_cpu = max(1, n_cpu - 1)
        tasks = []
        for i, rec in enumerate(stocks):
            n = len(rec["df"])
            evs = rec["all_evs"]
            # 事件区 bar (近20根内 LONG 事件) 且缓存值为空(未求判/判为None) 才需并行求。
            # 注意必须按 "值" 判断: 上面 setdefault 已把全部 bar 标记 False,
            # 若按 "键是否已存在" 过滤会把 js 恒清空 → 价值吸筹永不求判 (历史 bug)。
            js = [j for j in range(n) if not _va_cache.get((id(rec), j)) and any(
                e.get("type") in LONG and 0 <= j - (e.get("idx") or -1) <= 20
                for e in evs)]
            if js:
                tasks.append((i, rec, js))
        if verbose:
            print(f"价值吸筹判据 bar 区: {sum(len(t[2]) for t in tasks)} 个 "
                  f"({len(tasks)} 只股票), 并行预热 ...", flush=True)
        if tasks:
            with Pool(n_cpu) as pool:
                for i, out in pool.imap_unordered(_worker_va, tasks):
                    rec = stocks[i]
                    for j, res in (out or {}).items():
                        if res:
                            _va_cache[(id(rec), j)] = res
        save_va_cache(stocks, datalen)
        if verbose:
            print(f"预热完成 (耗时 {time.time()-t0:.0f}s, 缓存已写盘)", flush=True)
    elif verbose:
        print(f"价值吸筹缓存命中 {n_cache} 只股票 (磁盘缓存, 无需预热)", flush=True)
    return time.time() - t0


def _floats(s):
    return [float(x) for x in str(s).split(",") if x.strip() != ""]


def _ints(s):
    return [int(x) for x in str(s).split(",") if x.strip() != ""]


def _pct(v, width=5, nd=1):
    if v is None:
        return "-" * width
    return f"{v*100:{width}.{nd}f}%"


def _avg(v, width=7):
    if v is None:
        return "-" * width
    return f"{v*100:+{width}.2f}%"


def _num(v, width=5, nd=2):
    if v is None:
        return "-" * width
    return f"{v:{width}.{nd}f}"


def _cagr(v, width=7):
    if v is None:
        return "-" * width
    return f"{v*100:+{width}.1f}%"


def cell_metrics(st, hist, init):
    s = paper.stats(st)
    # CAGR (与 build_report 同口径, 基于 equity_hist)
    cagr = None
    if hist:
        first = hist[0].get("equity", init)
        last_e = hist[-1].get("equity", init)
        t0 = hist[0]["ts"][:10]
        t1 = hist[-1]["ts"][:10]
        try:
            from datetime import date
            y0 = date.fromisoformat(t0)
            y1 = date.fromisoformat(t1)
            years = (y1 - y0).days / 365.25
            if years > 0 and first > 0 and last_e > 0:
                cagr = (last_e / first) ** (1 / years) - 1
        except Exception:
            cagr = None
    return {
        "stop": None, "tp": None, "hold": None, "trail": None,
        "n": s["n_closed"],
        "win": s["win_rate"],
        "avg": s["avg_ret"],
        "pl": s["pl_ratio"],
        "pf": s["profit_factor"],
        "dd": s["max_drawdown"],
        "sharpe": s["sharpe_ratio"],
        "total": s["total_return"],
        "cagr": cagr,
        "veto": len(st.get("_qlib_veto_hits") or []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stops", default="0.03,0.04,0.05,0.06,0.08")
    ap.add_argument("--tps", default="0.10,0.15,0.20,0.30")
    ap.add_argument("--hold", default="20")
    ap.add_argument("--trail-backs", default="0.06,0.08,0.10,0.12",
                    help="移动止盈回落幅度(逗号分隔, 0.0=关闭移动止盈退化为固定止盈)")
    ap.add_argument("--qlib-veto", action="store_true",
                    help="QLib 卖出端否决: 空头信号卖出触发且prob≥阈值时否决一次")
    ap.add_argument("--qlib-veto-hi", type=float, default=0.60,
                    help="QLib 否决阈值 (prob_buy, 默认0.60)")
    ap.add_argument("--bear-exit", action="store_true",
                    help="事件型空头信号卖出 (UTAD/LPSY 主动平仓; 与 --qlib-veto 配合)" )
    ap.add_argument("--disc-confirm", action="store_true",
                    help="纪律·确认式入场: 事件确认后首根才建仓 (与bt --disc-confirm 同口径)")
    ap.add_argument("--va", action="store_true",
                    help="启用价值吸筹回退 (默认关闭: 网格只跑纪律策略)")
    ap.add_argument("--max-codes", type=int, default=60)
    ap.add_argument("--qlib-pool", action="store_true",
                    help="用 qlib 训练池 (train_pool.txt) 作为 universe")
    ap.add_argument("--start", default="2023-06-01")
    ap.add_argument("--datalen", type=int, default=850)
    ap.add_argument("--conf", type=int, default=90)
    ap.add_argument("--maxpos", type=int, default=3)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--cash", type=float, default=1_000_000)
    ap.add_argument("--mkt-gate", action="store_true")
    ap.add_argument("--workers", type=int, default=None,
                    help="并行预热进程数 (默认 cpu-1)")
    ap.add_argument("--stocks-cache", default=None,
                    help="股票事件缓存文件 (读写 pickle, 与 paper_replay_bt 同格式, 省去重复拉K线)")
    ap.add_argument("--report", default="docs/paper_replay_grid.md")
    ap.add_argument("--export", default="docs/paper_replay_grid.csv")
    args = ap.parse_args()

    stops = _floats(args.stops)
    tps = _floats(args.tps)
    holds = _ints(args.hold)
    trail_backs = _floats(args.trail_backs)
    print(f"网格: {len(stops)} 止损 × {len(tps)} 止盈 × {len(holds)} 持有 × "
          f"{len(trail_backs)} 移动止盈 = "
          f"{len(stops) * len(tps) * len(holds) * len(trail_backs)} 格")

    defaults = paper.apply_paper_params(None)
    base = {
        "min_conf": args.conf,
        "max_pos": args.maxpos,
        "hold_bars": 20,
        "stop_loss": defaults["stop_loss"],
        "take_profit": defaults["take_profit"],
        "cost": args.cost if args.cost is not None else defaults["cost"],
        "init_cash": args.cash,
        "window": 10,
        "chain_cap": 0,
        "chain_min_pct": 0.0,
        "start": args.start,
        "mkt_gate": args.mkt_gate,
        "flow_gate": False,
        "sect_gate": False,
        "trailing_stop": True,
        "trail_back_pct": 0.08,
        "trail_activate_pct": 0.0,
        "trail_atr_mult": 0.0,
        "bear_exit": args.bear_exit,
        "qlib_veto": args.qlib_veto,
        "qlib_veto_hi": args.qlib_veto_hi,
        "strategy_track": False,
        "va_confirm": False,
        "va": args.va,
        "disc_confirm": args.disc_confirm,
        "va_slots": 0,
        "va_bear_grace": 0,
    }

    # universe (与 paper_replay_bt 同口径: 优先主目录全A名单, 东财被拒时本地兜底;
    # --qlib-pool 用训练池保证模型覆盖)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    uni = []
    if getattr(args, "qlib_pool", False):
        try:
            from wyckoff.utils import normalize_symbol
            pool_path = os.path.join(repo_root, "data", "train_pool.txt")
            with open(pool_path, encoding="utf-8") as f:
                uni = [normalize_symbol(c.strip()) for c in f if c.strip()]
        except Exception:
            uni = []
    try:
        if uni:
            pass
        else:
            import json as _json

            from wyckoff.utils import normalize_symbol
            list_path = os.path.join(repo_root, "wyckoff_all_stocks.json")
            with open(list_path, encoding="utf-8") as f:
                codes = list(_json.load(f).keys())
            uni = [normalize_symbol(c) for c in codes]
    except Exception:
        uni = []
    if not uni:
        try:
            from wyckoff.fundamental import fetch_market_universe, local_universe
            from wyckoff.utils import normalize_symbol
            uni = [normalize_symbol(c) for c in fetch_market_universe(args.max_codes)]
            if not uni:
                uni = [normalize_symbol(c) for c in local_universe(args.max_codes)]
        except Exception:
            uni = []
    uni = uni[: max(2000, args.max_codes)]
    # 受限板块(创业板/科创板)与北交所排除 (与 paper_replay_bt 同口径)
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
    uni = uni[: args.max_codes]
    print("宇宙: " + (",".join(uni[:6]) + (" ..." if len(uni) > 6 else "")), flush=True)

    stocks = []
    if args.stocks_cache and os.path.exists(args.stocks_cache):
        import pickle as _pk
        try:
            with open(args.stocks_cache, "rb") as f:
                stocks = _pk.load(f)
            print(f"加载股票缓存: {len(stocks)} 只 ({args.stocks_cache})")
        except Exception as e:
            print(f"股票缓存读取失败, 重新加载: {e}")
            stocks = []
    if not stocks:
        for i, code in enumerate(uni):
            try:
                rec = load_stock_events(code, args.conf, datalen=args.datalen)
            except Exception as e:
                print(f"  [{i+1}/{len(uni)}] {code} 失败: {e}")
                rec = None
            if rec is None:
                continue
            stocks.append(rec)
        if args.stocks_cache:
            import pickle as _pk
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.stocks_cache)), exist_ok=True)
                with open(args.stocks_cache, "wb") as f:
                    _pk.dump(stocks, f, protocol=4)
                print(f"已写股票缓存: {len(stocks)} 只 → {args.stocks_cache}")
            except Exception as e:
                print(f"股票缓存写入失败: {e}")
    print(f"有效股票 {len(stocks)} 只, 加载完毕")

    if args.mkt_gate:
        market_gate = load_market_gate()
    else:
        market_gate = None
    if args.mkt_gate:
        print("大盘20日线门禁:", "已启用" if market_gate else "已启用(指数数据缺失, 视为不满足)")

    if args.va:
        prewarm_va(stocks, workers=args.workers, datalen=args.datalen)

    prob_map = {}
    if args.qlib_veto:
        prob_map = load_qlib_prob_map(stocks)
        print(f"QLib 概率映射: {len(prob_map)} 只", flush=True)

    cells = []
    t_start = time.time()
    for sl in stops:
        for tp in tps:
            for tb in trail_backs:
                for hb in holds:
                    params = dict(base, stop_loss=sl, take_profit=tp, hold_bars=hb,
                                  trail_back_pct=tb)
                    st = replay(stocks, params, market_gate=market_gate, prob_map=prob_map)
                    m = cell_metrics(st, st.get("equity_hist") or [], args.cash)
                    m.update(stop=sl, tp=tp, hold=hb, trail=tb)
                    cells.append(m)
                    el = time.time() - t_start
                    print(f"stop={sl:.0%} tp={tp:.0%} trail={tb:.0%} hold={hb}K  "
                          f"n={m['n']} 累计={m['total']*100:+.1f}% "
                          f"(累计已用 {el/60:.1f}min)", flush=True)

    for c in cells:
        c["_pct_win"] = _pct(c["win"])
        c["_pct_avg"] = _avg(c["avg"])
        c["_pl"] = _num(c["pl"])
        c["_pf"] = _num(c["pf"])
        c["_pct_dd"] = _pct(c["dd"])
        c["_sharpe"] = _num(c["sharpe"])
        c["_pct_cagr"] = _cagr(c["cagr"])

    cells.sort(key=lambda c: -(c["total"] or -1))

    # 控制台表
    print("\n" + "=" * 108)
    hdr = (f"{'止损':>5} {'止盈':>5} {'回撤止盈':>6} {'持有':>4} {'笔数':>4} "
           f"{'胜率':>5} {'均收':>7} {'盈亏比':>5} {'盈亏因子':>6} "
           f"{'最大回撤':>7} {'Sharpe':>6} {'累计':>8} {'CAGR':>7}")
    print(hdr)
    print("-" * 108)
    for c in cells:
        print(f"{c['stop']*100:5.0f}% {c['tp']*100:5.0f}% {c['trail']*100:6.0f}% "
              f"{c['hold']:>3}K "
              f"{c['n']:>4} {c['_pct_win']:>5} {c['_pct_avg']:>7} {c['_pl']:>5} "
              f"{c['_pf']:>6} {c['_pct_dd']:>7} {c['_sharpe']:>6} "
              f"{c['total']*100:+8.1f}% {c['_pct_cagr']:>7}")

    best = cells[0]
    _win = "-" if best["win"] is None else f"{best['win']*100:.1f}%"
    print("\n最优: " + f"止损-{best['stop']*100:.0f}% 止盈+{best['tp']*100:.0f}% "
          f"持{best['hold']}K 移动止盈回撤{best['trail']*100:.0f}% "
          f"累计{best['total']*100:+.1f}% "
          f"胜率{_win}")

    # 报告
    md = [
        "# 模拟盘止盈/止损参数网格扫描",
        "",
        f"- 生成: {paper.time.strftime('%Y-%m-%d')}",
        f"- 宇宙: {len(stocks)} 只 A 股 · 区间自 {args.start} · datalen {args.datalen} · "
        f"conf≥{args.conf} · 持仓≤{args.maxpos} · 单边成本{base['cost']*100:.2f}% · "
        f"大盘20日线门禁{'开' if args.mkt_gate else '闭'}",
        f"- 选股策略: 纪律优先(conf≥{args.conf}, 默认) {'+ 价值吸筹回退(--va)' if args.va else '· [价值吸筹回退未启用, 仅纪律]'}",
        f"- 移动止盈回撤网格: {args.trail_backs} (开启=追踪止损, 源ref用法: 直接修改 params)",
        "- 指标口径: 已平仓单笔收益含双边费用+滑点; 累计收益含未平仓浮盈; 盈亏因子=盈利合计/亏损合计",
        "",
        "### 扫描结果 (按累计收益排序)",
        "",
        "| 止损 | 止盈 | 移动止盈回撤 | 持有 | 笔数 | 胜率 | 均收 | 盈亏比 | 盈亏因子 | 最大回撤 | Sharpe | 累计 | CAGR | QLib否决 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cells:
        md.append(
            f"| {c['stop']*100:.0f}% | {c['tp']*100:.0f}% | {c['trail']*100:.0f}% "
            f"| {c['hold']}K | {c['n']} "
            f"| {c['_pct_win']} | {c['_pct_avg']} | {c['_pl']} | {c['_pf']} "
            f"| {c['_pct_dd']} | {c['_sharpe']} | {c['total']*100:+.1f}% "
            f"| {c['_pct_cagr']} | {c.get('veto', 0)} |")
    md.append("")
    dd_txt = "-" if best["dd"] is None else f"{best['dd']*100:.2f}%"
    pl_txt = "-" if best["pl"] is None else f"{best['pl']:.2f}"
    md.append(f"**最优参数**: 止损 **-{best['stop']*100:.0f}%** · 止盈 **+{best['tp']*100:.0f}%** · "
              f"移动止盈回撤 **{best['trail']*100:.0f}%** · 持有 **{best['hold']}K** → "
              f"累计 **{best['total']*100:+.1f}%**"
              f" · 胜率 {best['win']*100:.1f}% · 盈亏比 {pl_txt} · 最大回撤 {dd_txt}")
    md.append("")
    md.append("*历史回放，不构成投资建议。*")
    md_text = "\n".join(md)
    print("\n" + md_text)

    if args.report:
        os.makedirs(os.path.dirname(args.report), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md_text)
        print(f"\n已写出报告: {args.report}")

    if args.export:
        import csv
        os.makedirs(os.path.dirname(args.export), exist_ok=True)
        with open(args.export, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["stop_loss", "take_profit", "trail_back", "hold_bars", "n", "win_rate",
                        "avg_ret", "pl_ratio", "profit_factor", "max_drawdown",
                        "sharpe", "total_return", "cagr", "qlib_veto_hits"])
            for c in cells:
                w.writerow([c["stop"], c["tp"], c["trail"], c["hold"], c["n"], c["win"],
                            c["avg"], c["pl"], c["pf"], c["dd"], c["sharpe"],
                            c["total"], c["cagr"], c.get("veto", 0)])
        print(f"已导出网格数据: {args.export}")


if __name__ == "__main__":
    main()
