#!/usr/bin/env python3
"""把实盘扫描历史候选涉及的标的回填进信号准确度库 (wx_signal_accuracy.json)。

背景: 信号统计基座 wx_signal_accuracy 原只覆盖约 99 只自选股 (打开图表才
record_signals), 而模拟盘扫描的是全市场 ~3100 只主板 → 强事件命中率统计存在
严重选择偏差 ("评估集=选股池=同批老白马")。本脚本读取模拟盘扫描候选记录
(wx_paper_strategy_accuracy.json), 拉各标的历史日线, 用完整事件检测回填全部
强事件 (含校准 conf), record_signals 立即评估未来收益。

自 pick_candidates 扫描钩子 (record_events_batch) 落地后, 每次实盘扫描会自动
积累真实宇宙样本, 本脚本只用于一次性补历史/定期补录。

用法:
  python scripts/backfill_live_signals.py
  python scripts/backfill_live_signals.py --source wx_paper_strategy_accuracy.json --datalen 1200 --max-symbols 20
  python scripts/backfill_live_signals.py --csv 自选清单.csv   # 任意代码清单 (逗号/换行分隔)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.datasource import fetch_kline  # noqa: E402
from wyckoff.events import detect_all  # noqa: E402
from wyckoff.indicators import add_indicators, find_pivots  # noqa: E402
from wyckoff.signal_accuracy import record_signals  # noqa: E402
from wyckoff.utils import normalize_symbol  # noqa: E402


def _load_symbols(source, max_symbols=None):
    """从模拟盘候选记录或代码清单文件收集目标标的 (去重/去指数/去ETF)。"""
    syms = []
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        syms = [str(x.get("symbol")) for x in data if x.get("symbol")]
    elif isinstance(data, dict):
        syms = []  # 非列表 JSON: 调用方显式用 --csv 或 --symbols
    else:
        try:
            with open(source, encoding="utf-8") as f:
                raw = f.read()
            syms = [line.strip() for line in raw.splitlines() if line.strip()]
        except Exception:
            pass
    syms = [normalize_symbol(s) for s in syms if s]
    seen, out = set(), []
    for s in syms:
        if s in seen:
            continue
        seen.add(s)
        c = s[-6:]
        if s.startswith("sh") and c.startswith(("000", "399")):  # 上证指数
            continue
        if c.startswith(("51", "15", "58")):                     # ETF
            continue
        out.append(s)
    return out[:max_symbols] if max_symbols else out


def _name_of(symbol):
    try:
        from wyckoff.screener import _get_stock_name
        return _get_stock_name(str(symbol)[-6:])
    except Exception:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="回填实盘扫描标的进信号准确度库")
    ap.add_argument("--source", default="wx_paper_strategy_accuracy.json",
                    help="候选记录 JSON 或代码清单文件"
                         " (默认: wx_paper_strategy_accuracy.json)")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="最多处理的标的数 (试跑用)")
    ap.add_argument("--datalen", type=int, default=1200,
                    help="拉取日线根数 (足够覆盖候选信号日期; 默认 1200≈4.8年)")
    ap.add_argument("--throttle", type=float, default=0.3,
                    help="相邻请求最小间隔秒 (东财限流防护)")
    args = ap.parse_args(argv)

    symbols = _load_symbols(args.source, args.max_symbols)
    if not symbols:
        print("!! 未解析到任何标的 (检查 --source 文件)", file=sys.stderr)
        return 1
    print(f"待回填标的: {len(symbols)}")

    added = skipped = 0
    t0 = time.time()
    last = 0.0
    for i, sym in enumerate(symbols, 1):
        if time.time() - last < args.throttle:
            time.sleep(args.throttle - (time.time() - last))
        last = time.time()
        try:
            code = sym[-6:]
            df = add_indicators(fetch_kline(sym, datalen=args.datalen, scale=240),
                                symbol=sym)
            if df is None or len(df) < 220:
                print(f"  [{i}/{len(symbols)}] {sym} 数据不足, 跳过")
                skipped += 1
                continue
            evs = detect_all(df, find_pivots(df, order=6))
            if not evs:
                skipped += 1
                continue
            n = record_signals(df, sym, code, 240, int(len(df)),
                               events=evs, vsa_signals=[], name=_name_of(sym))
            if n:
                added += n
                print(f"  [{i}/{len(symbols)}] {sym} {_name_of(sym)}: "
                      f"新增 {n} 条强事件")
            else:
                skipped += 1  # 已存在/落入冷却窗
        except Exception as e:
            print(f"  [{i}/{len(symbols)}] {sym} 失败: {type(e).__name__}: {e}")
            skipped += 1
    dt = time.time() - t0
    print(f"\n完成: {len(symbols)} 标的 · 新增 {added} 条 · 跳过 {skipped} · "
          f"耗时 {dt:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
