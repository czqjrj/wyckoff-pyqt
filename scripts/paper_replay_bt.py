#!/usr/bin/env python3
"""模拟盘引擎·真实K线历史回放回测 CLI.

复用 wyckoff.paper 的买/卖/止盈止损/持仓上限/成本/统计逻辑, 在真实日线K线上
逐日回放, 统计累计收益 / CAGR / 最大回撤 / 胜率 / 净值曲线。

用法:
  python scripts/paper_replay_bt.py                          # 默认参数, 打印摘要
  python scripts/paper_replay_bt.py --max-codes 80 --conf 80
  python scripts/paper_replay_bt.py --conf 90 --maxpos 2 --hold 20 \
         --stop 0.05 --tp 0.15 --cost 0.004
  python scripts/paper_replay_bt.py --start 2023-09-01 --report docs/paper_replay_bt.md
  python scripts/paper_replay_bt.py --export docs/paper_replay_trades.csv
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff import paper  # noqa: E402


def _sdate(s):
    from datetime import date
    if isinstance(s, str):
        return date.fromisoformat(s)
    return s


def load_stock_events(code, min_conf, datalen):
    """对单只股票跑完整检测, 返回 (day列表, open, low, high, close, events)。

    events: [{idx, type, conf}] 强多头事件 (已经 conf≥min_conf 过滤)。
    """
    from wyckoff.datasource import fetch_kline
    from wyckoff.events import detect_all
    from wyckoff.indicators import add_indicators, find_pivots

    df = add_indicators(fetch_kline(code, datalen=datalen, scale=240), symbol=code)
    if df is None or len(df) < 120:
        return None
    piv = find_pivots(df, order=6)
    evs = detect_all(df, piv)
    events = []
    for e in evs or []:
        if e.get("type") not in paper.LONG_EVENT_TYPES:
            continue
        conf = int(e.get("conf", 0) or 0)
        if conf < min_conf:
            continue
        idx = int(e.get("idx", 0) or 0)
        if idx < 0 or idx >= len(df):
            continue
        events.append({"idx": idx, "type": e["type"], "conf": conf})
    events.sort(key=lambda x: x["idx"])
    sector = ""
    try:
        from wyckoff.fundamental import fetch_sector
        sector = fetch_sector(code) or ""
    except Exception:
        pass
    chain_key = ""
    try:
        from wyckoff.chain import chain_cap_key
        chain_key = chain_cap_key(sector) or ""
    except Exception:
        pass
    return {
        "code": code,
        "sector": sector,
        "chain": chain_key,
        "day": list(df["day"]),
        "open": list(df["open"].astype(float)),
        "low": list(df["low"].astype(float)),
        "high": list(df["high"].astype(float)),
        "close": list(df["close"].astype(float)),
        "volume": list(df["volume"].astype(float)),
        "events": events,
    }


def load_market_gate(datalen=850):
    """加载上证指数日线并构建 date -> (close, ma20) 映射, 供大盘20日线门禁用。

    返回 dict {date: (close, ma20)}; 失败返回 None。
    注意: 大盘门禁的历史重建用指数历史前缀因果计算 (MA20仅用当日及之前数据, 无前视)。
    """
    try:
        from wyckoff.datasource import fetch_kline
        from wyckoff.indicators import add_indicators
        df = add_indicators(fetch_kline("sh000001", datalen=datalen, scale=240))
        if df is None or len(df) < 60:
            return None
        m = {}
        for d, cl, ma in zip(df["day"], df["close"].astype(float),
                             df["price_ma20"]):
            if ma is not None and pd.notna(ma):
                m[d] = (float(cl), float(ma))
        return m
    except Exception:
        return None


def newest_buyable(rec, j, window=10):
    """返回该股票在 bar j 处可买入的最新事件 (事件在 j 之前 ≤window 根内)。

    返回 (event, buy_bar) 或 None。buy_bar 用事件后下一根=事件idx+1 (开盘买入)。
    """
    best = None
    for e in rec["events"]:
        if e["idx"] <= j and (j - e["idx"]) <= window:
            if best is None or e["idx"] > best["idx"]:
                best = e
    if best is None:
        return None
    return best


def _flow_score(rec, j, back=5):
    """资金流分 (因果历史代理): 截至 bar j 的近 back 根量价资金净流入占比 (无量纲)。

    口径与 wyckoff.phases.flow_confirmed 一致: Σ(body×vol)/Σ(|body|×vol) ∈ [-1,1],
    仅用 <=j 的历史K线 (无前视)。数据不足 → None。
    """
    if j < back:
        return None
    import numpy as np
    b = np.asarray(rec["close"][j - back + 1: j + 1]) - np.asarray(rec["open"][j - back + 1: j + 1])
    v = np.asarray(rec["volume"][j - back + 1: j + 1])
    num = float(np.sum(b * v))
    den = float(np.sum(np.abs(b) * v)) or 1.0
    return num / den


def _sector_gate_ok(rec, ts, gate=0.60):
    """板块强度门禁(历史快照): 信号日板块强度百分位 >= gate。

    口径与模拟盘一致: 用 chain.strength_at 查信号日前最近快照 (无前视)。
    与实盘不同: 实盘 fetch_sector 若缺失板块会 fail-close; 但回测 K 线里板块映射
    缺失普遍存在 (load_stock_events 未填 sector), 若照搬 fail-close 会把整段历史
    清空。故此处按"贴近模拟盘"的回测口径采用 fail-open —— 无板块映射或快照断档
    时放行, 仅有真实分位 < gate 才拦截 (有数据才真正过滤)。返回 (ok, reason)。
    """
    sector = (rec.get("sector") or "").strip()
    if not sector:
        return True, "无板块映射(放行)"
    try:
        from wyckoff.chain import strength_at
        pct = strength_at(sector, ts=pd.Timestamp(ts))
    except Exception:
        return True, "板块快照查询异常(放行)"
    if pct is None:
        return True, f"板块「{sector}」无历史快照(放行)"
    return (pct >= gate), f"板块强度{pct*100:.0f}分位"


def replay(stocks, params, market_gate=None):
    from wyckoff.settings_keys import S
    paper.apply_paper_params({
        S.Paper.INIT_CASH: params["init_cash"],
        S.Paper.MAX_POS: params["max_pos"],
        S.Paper.HOLD_BARS: params["hold_bars"],
        S.Paper.STOP_LOSS: params["stop_loss"],
        S.Paper.TAKE_PROFIT: params["take_profit"],
        S.Paper.COST: params["cost"],
        S.Paper.MIN_CONF: params["min_conf"],
    })
    cfg = paper._CUR
    st = paper._new_state()

    # 构造 日->bar 索引 每只股票 与 code->rec 索引
    day_to_j = []
    code_to_idx = {}
    for k, rec in enumerate(stocks):
        m = {}
        for j, d in enumerate(rec["day"]):
            m[d] = j
        day_to_j.append(m)
        code_to_idx[rec["code"]] = k

    # 主时间轴 = 所有交易日并集
    all_days = sorted({d for rec in stocks for d in rec["day"]})
    start = params.get("start")
    if start:
        all_days = [d for d in all_days if d.date() >= _sdate(start)]

    last_close = {}  # 持仓股票最近可用的 bar 收盘 (用于停牌日 carry)
    last_low = {}
    for D in all_days:
        # 1) 卖出/估值: 用今日收盘更新持仓
        for pos in list(st["positions"]):
            idx = code_to_idx.get(pos["symbol"])
            if idx is None:
                continue
            j = day_to_j[idx].get(D) if idx is not None else None
            if j is not None:
                last_close[pos["symbol"]] = stocks[idx]["close"][j]
                last_low[pos["symbol"]] = stocks[idx]["low"][j]
            last = last_close.get(pos["symbol"])
            if last is None:
                continue
            entry = float(pos["buy_px"])
            ret = last / entry - 1
            pos["last"] = round(last, 3)
            pos["last_ret"] = round(ret, 4)
            # 结构支撑 = 最近 10 根低点
            support = None
            if j is not None:
                lo = stocks[idx]["low"]
                support = min(lo[max(0, j - 9): j + 1])
            stop_px = entry * (1 - cfg["stop_loss"])
            pos["entry_bars"] = int(pos.get("entry_bars", 0)) + 1
            held = int(pos["entry_bars"])
            reason = None
            if ret >= cfg["take_profit"]:
                reason = "止盈"
            elif last <= stop_px:
                reason = "止损"
            elif support is not None and support < stop_px and last <= support:
                reason = "破位"
            elif held >= cfg["hold_bars"]:
                reason = "到期"
            if reason:
                sell_price = max(stop_px, last) * (1 - paper.SLIP_SELL)
                paper.close_position(st, pos, sell_price, reason)

        # 2) 买入: 产生新信号 (前 N 根内事件) 的候选按 conf 填位
        cands = []
        for k, rec in enumerate(stocks):
            code = rec["code"]
            if paper.has_position(st, code):
                continue
            j = day_to_j[k].get(D)
            if j is None:
                continue
            ev = newest_buyable(rec, j, window=params["window"])
            if ev is None:
                continue
            # D: 强链过滤 — 只交易信号日链条强度达标的板块内个股。
            #    用历史快照 (strength_at) 无前视; 无历史快照时 fail-open 不筛选
            #    (避免把无数据区间误清空, 覆盖区间内才真正过滤)。
            if params.get("chain_min_pct"):
                from wyckoff.chain import chain_factor_for
                try:
                    cf = chain_factor_for(rec["sector"], ts=pd.Timestamp(D))
                    if cf is not None and cf["pct"] < params["chain_min_pct"]:
                        continue
                except Exception:
                    pass
            # E: 资金流门禁 (因果历史代理, 跨日截面中位) —
            #    先收集每只候选的资金流分 (近5根量价净流入占比); None=数据不足 fail-close。
            flow_score = None
            if params.get("flow_gate"):
                flow_score = _flow_score(rec, j)
                if flow_score is None:
                    continue
            # F: 板块强度门禁 (历史快照) — 有快照按分位过滤, 无快照放行
            if params.get("sect_gate"):
                s_ok, _sr = _sector_gate_ok(rec, ts=pd.Timestamp(D))
                if not s_ok:
                    continue
            cands.append((ev["conf"], code, ev["type"], rec["open"][j],
                          rec["chain"], flow_score))
        cands.sort(key=lambda x: -x[0])
        # 大盘20日线门禁: 仅当日上证收盘 > MA20 才开新仓 (因果历史重建, 无前视)
        if params.get("mkt_gate") and market_gate is not None:
            mk = market_gate.get(D)
            if mk is None or not (mk[0] > mk[1]):
                cands = []
        # E: 资金流门禁(因果代理) — 跨日截面: 当日候选池净流入分 ≥ 中位才保留。
        #    镜像 paper._flow_net5 + pick_candidates 的"净流入>50分位" (fail-close)。
        if params.get("flow_gate") and cands:
            scores = sorted([c[5] for c in cands if c[5] is not None])
            if scores:
                med = scores[len(scores) // 2]
                cands = [c for c in cands if c[5] is not None and c[5] >= med]
            else:
                cands = []  # 无任何资金流分 → fail-close
        for conf, code, typ, open_px, chain, flow_score in cands:
            if len(st["positions"]) >= cfg["max_pos"]:
                break
            if paper.has_position(st, code):
                continue
            # C: 同产业链限仓 — 同链已持 ≥ chain_cap 则不重复买
            cc = params.get("chain_cap") or 0
            if cc and chain:
                n_chain = sum(1 for p in st["positions"]
                              if stocks[code_to_idx[p["symbol"]]].get("chain") == chain)
                if n_chain >= cc:
                    continue
            order = paper._make_order(code, "", typ, conf, open_px, 0, st["cash"])
            if order is None:
                continue
            paper.fill_buy(st, order)

        # 3) 记录净值 (今日收盘市值)
        st["equity_hist"].append({
            "ts": str(D),
            "cash": round(st["cash"], 2),
            "equity": round(paper.equity(st, {}), 2),
        })

    paper.save_state(st)
    return st


def build_report(st, params):
    s = paper.stats(st)
    hist = st.get("equity_hist") or []
    L = []
    L.append("# 模拟盘引擎·真实K线历史回放回测")
    L.append("")
    L.append(f"- 生成: {paper.time.strftime('%Y-%m-%d')}")
    L.append(f"- 口径: 初始资金 {params['init_cash']:,.0f} · 持仓上限 {params['max_pos']} · "
             f"conf≥{params['min_conf']} · 持{params['hold_bars']}K · "
             f"止损-{params['stop_loss']*100:.0f}% · 止盈+{params['take_profit']*100:.0f}% · "
             f"单边成本{params['cost']*100:.2f}%")
    L.append("- 强多头事件过滤: Spring/Shakeout/ST/LPS/SC")
    gates_on = [
        ("大盘20日线" if params.get("mkt_gate") else None),
        ("资金流(因果代理)" if params.get("flow_gate") else None),
        ("板块强度(历史快照)" if params.get("sect_gate") else None),
    ]
    on = [g for g in gates_on if g]
    L.append(f"- 硬门禁: {('、'.join(on)) if on else '全部关闭'}")
    L.append("")
    if params.get("flow_gate") or params.get("sect_gate"):
        L.append("> 门禁口径备注: 大盘门禁用历史前缀因果重建(收盘>MA20); 资金流门禁用"
                 "近5根量价净流入占比, 按『当日候选池 ≥ 截面中位』过滤(fail-close, "
                 "与实盘『净流入>50分位』一致; 属可回测的因果量价代理而非真实主力资金流); "
                 "板块强度门禁用历史快照分位≥0.6(个股板块已通过 fetch_sector 注入, "
                 "但板块强度快照仅自 2026-08 起, 回测区间此前无快照故 fail-open 放行, "
                 "仅末尾有真实分位才过滤)。")
    else:
        L.append("> 局限: 板块强度>60分位 与 资金流净流入>50分位 两道门禁缺历史数据，本次回测未执行（仅执行可历史重建的大盘20日线门禁 + 强多头事件 + conf≥90 + 结构位止损 + 持仓上限3）。")
    L.append("")
    L.append("### 收益统计")
    L.append("")
    L.append(f"- 已平仓: **{s['n_closed']}** 笔 · 当前持仓 {s['n_positions']} 只")
    L.append(f"- 累计收益(账面): **{s['total_return']*100:+.2f}%**")
    if s["win_rate"] is not None:
        L.append(f"- 胜率: **{s['win_rate']*100:.1f}%** · 单笔均收 "
                 f"{s['avg_ret']*100:+.2f}% · 盈亏比 {s['pl_ratio']}")
    if s["max_drawdown"] is not None:
        L.append(f"- 最大回撤: **{s['max_drawdown']*100:.2f}%**")
    # CAGR
    if hist:
        first = hist[0].get("equity", params["init_cash"])
        last_e = hist[-1].get("equity", params["init_cash"])
        t0 = hist[0]["ts"][:10]
        t1 = hist[-1]["ts"][:10]
        try:
            from datetime import date
            y0 = date.fromisoformat(t0)
            y1 = date.fromisoformat(t1)
            years = (y1 - y0).days / 365.25
            if years > 0 and first > 0:
                cagr = (last_e / first) ** (1 / years) - 1
                L.append(f"- CAGR: **{cagr*100:+.2f}%** (区间 {t0} ~ {t1}, {years:.1f} 年)")
        except Exception:
            pass
    if s["by_reason"]:
        L.append("")
        L.append("| 平仓原因 | 笔数 | 平均收益 |")
        L.append("|---|---|---|")
        for r, b in sorted(s["by_reason"].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| {r} | {b['n']} | {b['avg']*100:+.2f}% |")
    if s["by_type"]:
        L.append("")
        L.append("| 事件 | 笔数 | 胜率 | 平均收益 |")
        L.append("|---|---|---|---|")
        for t, b in sorted(s["by_type"].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| {t} | {b['n']} | {b['win']*100:.0f}% | {b['avg']*100:+.2f}% |")
    if params.get("sect_gate"):
        L.append("")
        L.append("### 局限")
        L.append("")
        L.append("- 板块强度门禁虽已通过 fetch_sector 为个股注入真实板块，但板块强度历史快照"
                 "(wx_board_snap.json) 仅自 2026-08 起，回测区间(2023-06~2026-08)此前信号"
                 "均落在快照之前 → fail-open 放行，故本门禁在历史回测中近乎空转，"
                 "仅在末尾快照窗内才有真实过滤。")
        L.append("- 要让板块门禁真正参与历史回测，需回填板块强度快照的历史序列(按日/按周回填"
                 "东财行业板块分位)，使 strength_at 在回测区间内均能取到可信分位。")
        L.append("- 资金流门禁为『近5根量价净流入占比 ≥ 当日候选池截面中位』的因果量价代理，"
                 "非真实主力资金流(实盘用东财 main 净流入)；两者在量价承接方向一致但取值口径不同。")
    L.append("")
    L.append("*历史回放，不构成投资建议。*")
    return "\n".join(L), s


def main():
    ap = argparse.ArgumentParser(description="模拟盘引擎·真实K线历史回放回测")
    ap.add_argument("--max-codes", type=int, default=60, help="扫描股票数上限")
    ap.add_argument("--conf", type=int, default=None, help="最低置信度 (默认取模块值)")
    ap.add_argument("--maxpos", type=int, default=None, help="持仓上限")
    ap.add_argument("--hold", type=int, default=None, help="持有K数")
    ap.add_argument("--stop", type=float, default=None, help="止损(小数, 如0.05)")
    ap.add_argument("--tp", type=float, default=None, help="止盈(小数, 如0.15)")
    ap.add_argument("--cost", type=float, default=None, help="单边成本")
    ap.add_argument("--cash", type=float, default=None, help="初始资金")
    ap.add_argument("--window", type=int, default=10, help="信号可买入窗口(根)")
    ap.add_argument("--chain-cap", type=int, default=0,
                    help="同产业链最多同时持有N只 (0=不限, 需个股有板块映射)")
    ap.add_argument("--chain-min-pct", type=float, default=0,
                    help="强链过滤: 只交易信号日板块强度≥该分位(0~1)的链条内个股, "
                         "用历史快照无前视 (0=关闭)")
    ap.add_argument("--mkt-gate", action="store_true",
                    help="大盘20日线门禁: 仅当日上证收盘>MA20才开新仓 (因果历史重建)")
    ap.add_argument("--flow-gate", action="store_true",
                    help="资金流门禁(因果代理): 信号日近5根量价净流入占比>0 (fail-close)")
    ap.add_argument("--sect-gate", action="store_true",
                    help="板块强度门禁: 历史快照分位≥0.6 (无快照期放行, 有数据才过滤)")
    ap.add_argument("--start", default="", help="回放起始日期 YYYY-MM-DD")
    ap.add_argument("--datalen", type=int, default=700,
                    help="每只标的拉取的K线根数 (覆盖回放起始前的历史, 建议≥850覆盖3年)")
    ap.add_argument("--report", default="", help="写出报告 md 路径")
    ap.add_argument("--export", default="", help="导出逐笔 CSV 路径")
    args = ap.parse_args()

    defaults = paper.apply_paper_params(None)
    params = {
        "min_conf": args.conf if args.conf is not None else defaults["min_conf"],
        "max_pos": args.maxpos if args.maxpos is not None else defaults["max_pos"],
        "hold_bars": args.hold if args.hold is not None else defaults["hold_bars"],
        "stop_loss": args.stop if args.stop is not None else defaults["stop_loss"],
        "take_profit": args.tp if args.tp is not None else defaults["take_profit"],
        "cost": args.cost if args.cost is not None else defaults["cost"],
        "init_cash": args.cash if args.cash is not None else defaults["init_cash"],
        "window": args.window,
        "chain_cap": args.chain_cap,
        "chain_min_pct": args.chain_min_pct,
        "start": args.start,
        "mkt_gate": args.mkt_gate,
        "flow_gate": args.flow_gate,
        "sect_gate": args.sect_gate,
    }

    # universe
    try:
        from wyckoff.fundamental import fetch_market_universe, local_universe
        from wyckoff.utils import normalize_symbol
        uni = [normalize_symbol(c) for c in fetch_market_universe(args.max_codes)]
        if not uni:
            uni = [normalize_symbol(c) for c in local_universe(args.max_codes)]
    except Exception:
        uni = []
    uni = uni[:args.max_codes]

    print(f"扫描 {len(uni)} 只: conf≥{params['min_conf']} 持仓≤{params['max_pos']} "
          f"持{params['hold_bars']}K 止损-{params['stop_loss']*100:.0f}% "
          f"止盈+{params['take_profit']*100:.0f}% 成本{params['cost']*100:.2f}% "
          f"门禁: 大盘{'开' if args.mkt_gate else '闭'}/资金{'开' if args.flow_gate else '闭'}"
          f"/板块{'开' if args.sect_gate else '闭'}")
    stocks = []
    for i, code in enumerate(uni):
        try:
            rec = load_stock_events(code, params["min_conf"], datalen=args.datalen)
        except Exception as e:
            print(f"  [{i+1}/{len(uni)}] {code} 失败: {e}")
            rec = None
        if rec is None:
            continue
        stocks.append(rec)
        print(f"  [{i+1}/{len(uni)}] {code} 事件{len(rec['events'])}个", flush=True)

    print(f"\n有效股票 {len(stocks)} 只, 开始回放 ...")
    market_gate = load_market_gate() if args.mkt_gate else None
    if args.mkt_gate:
        print("大盘20日线门禁: 已启用" if market_gate else "大盘20日线门禁: 已启用(指数数据缺失, 视为不满足)")
    st = replay(stocks, params, market_gate=market_gate)

    if args.export:
        import csv
        with open(args.export, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "type", "conf", "buy_px", "sell_px", "ret",
                        "reason", "bars", "close"])
            for c in st["closed"]:
                w.writerow([c["symbol"], c["type"], c["conf"], c["buy_px"],
                            c["sell_px"], c["ret"], c["reason"], c["bars"],
                            c.get("close_ts", "")])
        print(f"已导出逐笔: {args.export}")

    md, s = build_report(st, params)
    print("=" * 60)
    print(md)

    if args.report:
        os.makedirs(os.path.dirname(args.report), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"已写出报告: {args.report}")


if __name__ == "__main__":
    main()
