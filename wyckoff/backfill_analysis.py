"""分析层历史快照回填 (整股结论准确度通道)。

背景: `wx_accuracy.json` (整股分析结论的准确度库) 只有 GUI 打开图表时经
`accuracy.record_analysis` 写入, 于是覆盖仅约 99 只自选股、且只记"打开那一刻"
一个时点 → 分析层评估通道 (`accuracy_stats` / `calibrate` 的 `phase_mislabel`
自检) 近乎空转。信号层早已有批量补齐脚本 (`scripts/backfill_live_signals.py`),
分析层缺对应通道, 两者样本量差三个数量级 (信号层 1.3 万条 vs 分析层个位数)。

做法: 对每只标的在历史日线上按固定步长取切点, 用"截至该切点的前缀 df"重跑
`accuracy.capture_snapshot` (阶段/P&F 方向/融合分/目标位/交易计划), 得到**当时**
真实的分析结论, 再用同一份全量 df 就地补齐未来收益 (离线, 不额外联网),
最后一次性批量落盘 (避免 `record_analysis` 逐条全量读写)。

严格无前视: 指标列在全量 df 上算好后切片 (所有 rolling/EWMA 均为后视窗口,
第 k 根只依赖 <= k 的数据), 其余管线 (枢轴/事件/判段/P&F/VSA/融合/目标位/
交易计划) 一律只跑前缀, 与 GUI 实时分析同口径。

已知限制 (有意为之):
  - 不含新闻维度: 历史新闻无法回溯, 故 `news_score` 恒为 None,
    `update_news_calibration` 的新闻自校准仍只能靠 GUI 路径积累样本。
  - 不调用 `record_signals` / `auto_evaluate_feedback`: 二者各有独立回填通道,
    在此逐条调用会 O(N^2) 且污染信号库的日期分布。
  - 切点须距序列末端 >= max(HORIZONS), 否则没有可评估的未来行情。

用法:
    python -m wyckoff.backfill_analysis --max-symbols 5 --step 40      # 试跑
    python -m wyckoff.backfill_analysis --universe --max-symbols 60   # 全市场
    python -m wyckoff.backfill_analysis --csv 自选.csv --dry-run
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

from .accuracy import (
    HORIZONS,
    _evaluate_against_df,
    _key,
    capture_snapshot,
    load_accuracy,
    save_accuracy,
)
from .indicators import add_indicators

# 切点默认参数
DEFAULT_STEP = 20        # 相邻切点间隔根数 (太密会簇拥在同一段行情, 相关性高)
DEFAULT_WARMUP = 250     # 预热根数 (price_ma200 等长窗口指标需 >= 200)
DEFAULT_MAX_POINTS = 24  # 单标的最多取多少切点 (超出按等距抽样, 控成本)
BENCH_SYMBOL = "sh000001"


# ── 标的来源 ──
def _skip_symbol(sym):
    """过滤指数/ETF/北交所等非主板标的 (与 scripts/backfill_live_signals 同口径)。"""
    if not sym or sym[:2] not in ("sh", "sz", "bj"):
        return True
    if sym.startswith("bj"):
        return True
    c = sym[-6:]
    if sym.startswith("sh") and c.startswith("000"):            # 上证指数系列
        return True
    if sym.startswith("sz") and c.startswith("399"):            # 深证指数系列
        return True
    return c.startswith(("51", "15", "58", "56", "588"))        # ETF


def _symbol_rows(data):
    """从已解析的 JSON 结构里收集代码字符串。"""
    def _one(x):
        if isinstance(x, dict):
            return str(x.get("symbol") or x.get("code") or "")
        return str(x or "")
    if isinstance(data, list):
        return [_one(x) for x in data]
    if isinstance(data, dict):
        for k in ("symbols", "codes", "watchlist", "records"):
            if isinstance(data.get(k), list):
                return [_one(x) for x in data[k]]
    return []


def _read_symbols(path):
    """从 JSON 记录列表或纯代码文本收集代码。解析失败返回 []。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return []
    try:
        return _symbol_rows(json.loads(text))
    except Exception:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]


def load_symbols(source=None, use_universe=False, max_symbols=None,
                 universe_n=100):
    """收集目标标的: 全市场 / 指定文件 / 自选股 (优先级依次回退), 去重去指数去 ETF。"""
    from .paths import WATCHLIST_FILE
    from .utils import normalize_symbol

    raw = []
    if use_universe:
        # 走 fundamental.universe 统一入口: 东财成交额 Top-N 优先, 接口不可用时
        # 自动降级 local_universe (本地全A名单等距抽样)。不能直接调
        # fetch_market_universe —— 它是单页请求且失败返回 [], 无任何兜底。
        try:
            from .fundamental import universe
            codes, src = universe(n=universe_n)
            raw = _symbol_rows(codes or [])
            print(f"全市场列表: 来源={src or '不可用'} 取得 {len(raw)} 只", flush=True)
        except Exception as e:
            print(f"!! 全市场列表不可用 ({type(e).__name__}: {e}), 回退自选股",
                  flush=True)
    if not raw and source:
        raw = _read_symbols(source)
    if not raw:
        raw = _read_symbols(WATCHLIST_FILE)

    seen, out = set(), []
    for s in raw:
        if not s:
            continue
        try:
            s = normalize_symbol(s.strip())
        except Exception:
            continue
        if s in seen or _skip_symbol(s):
            continue
        seen.add(s)
        out.append(s)
    return out[:max_symbols] if max_symbols else out


# ── 基准 (超额收益) ──
def align_bench(df, bench_df, max_gap=7):
    """把基准指数收盘价按**日期**对齐到 df 的行位置。

    `_horizon_result` 以同一 idx 索引 df 与 bench, 而个股/指数的抓取窗口长度
    未必一致, 直接按位置对齐会错位 → 按交易日 searchsorted 映射, 跨度超
    max_gap 自然日 (停牌/长假) 的位置留 NaN: 超额收益缺省, 不污染方向命中。
    返回 {"close": ndarray} 或 None (无任何可对齐点)。
    """
    if bench_df is None or len(bench_df) == 0 or df is None or len(df) == 0:
        return None
    b_days = pd.to_datetime(bench_df["day"]).to_numpy()
    b_close = bench_df["close"].to_numpy(dtype=float)
    if len(b_close) == 0:
        return None
    d_days = pd.to_datetime(df["day"]).to_numpy()
    pos = np.searchsorted(b_days, d_days, side="right") - 1
    out = np.full(len(df), np.nan)
    ok = pos >= 0
    if ok.any():
        idx = np.clip(pos, 0, len(b_close) - 1)
        gap_days = (d_days - b_days[idx]) / np.timedelta64(1, "D")
        good = ok & (gap_days <= max_gap)
        if good.any():
            out[good] = b_close[idx[good]]
    return {"close": out} if np.isfinite(out).any() else None


# ── 切点 ──
def cut_points(n, step=DEFAULT_STEP, warmup=DEFAULT_WARMUP, tail=None,
               max_points=DEFAULT_MAX_POINTS):
    """历史切点下标列表: [warmup, n-tail] 区间内按 step 取, 超出 max_points 等距抽样。"""
    tail = max(HORIZONS) if tail is None else int(tail)
    lo, hi = int(warmup), int(n) - int(tail)
    if hi <= lo:
        return []
    pts = list(range(lo, hi + 1, max(1, int(step))))
    if max_points and len(pts) > max_points:
        sel = np.linspace(0, len(pts) - 1, int(max_points)).round().astype(int)
        pts = [pts[i] for i in dict.fromkeys(sel.tolist())]
    return pts


def backfill_one_symbol(df, symbol, code="", name="", step=DEFAULT_STEP,
                        warmup=DEFAULT_WARMUP, max_points=DEFAULT_MAX_POINTS,
                        bench=None, scale=240):
    """单标的回填: 返回新记录列表 (调用方负责去重与落盘)。

    df 需已 add_indicators。切点处取前缀重跑 capture_snapshot, 并用同一份
    全量 df 就地补齐未来收益 —— 全程不联网。返回记录 status 多为 done。
    """
    if df is None or len(df) == 0:
        return []
    code = code or str(symbol)[-6:]
    out = []
    for k in cut_points(len(df), step=step, warmup=warmup,
                        max_points=max_points):
        sub = df.iloc[:k + 1]
        if len(sub) < 30:
            continue
        try:
            rec = capture_snapshot(sub, symbol, code, int(scale), len(df),
                                   name=name)
        except Exception:
            continue
        if not rec:
            continue
        rec["backfill"] = True
        try:
            _evaluate_against_df(rec, df, bench=bench)
        except Exception:
            pass
        out.append(rec)
    return out


# ── 批量编排 ──
def _real_kline_fn(datalen, scale=240):
    from .datasource import fetch_kline
    cache = {}

    def _get(sym):
        if sym not in cache:
            cache.clear()   # 只留最近一只, 控内存
            cache[sym] = add_indicators(
                fetch_kline(sym, datalen=datalen, scale=scale), symbol=sym)
        return cache[sym]
    return _get


def backfill_analysis(symbols, kline_fn, step=DEFAULT_STEP,
                      warmup=DEFAULT_WARMUP, max_points=DEFAULT_MAX_POINTS,
                      sleep=0.35, bench_df=None, log=print, dry_run=False):
    """批量回填分析层快照。返回统计 dict。

    kline_fn(symbol) → 已 add_indicators 的日线 DataFrame (便于测试注入)。
    已存在的 (symbol, scale, ref_dt) 跳过 (按 _key 去重, 幂等可重跑)。
    """
    st = {"scan": 0, "sym_ok": 0, "points": 0, "added": 0, "evaluated": 0,
          "skip": 0, "failed": 0}
    records = load_accuracy()
    have = {_key(r) for r in records}
    pending = []
    last = 0.0
    for i, sym in enumerate(symbols, 1):
        if sleep and time.time() - last < sleep:
            time.sleep(sleep - (time.time() - last))
        last = time.time()
        st["scan"] += 1
        try:
            df = kline_fn(sym)
        except Exception as e:
            st["failed"] += 1
            log(f"  [{i}/{len(symbols)}] {sym} 抓取失败: {type(e).__name__}: {e}")
            continue
        if df is None or len(df) < int(warmup) + max(HORIZONS):
            st["skip"] += 1
            continue
        try:
            bench = align_bench(df, bench_df)
            recs = backfill_one_symbol(df, sym, code=sym[-6:], step=step,
                                       warmup=warmup, max_points=max_points,
                                       bench=bench)
        except Exception as e:
            st["failed"] += 1
            log(f"  [{i}/{len(symbols)}] {sym} 回填异常: {type(e).__name__}: {e}")
            continue
        added = 0
        for rec in recs:
            st["points"] += 1
            k = _key(rec)
            if k in have:
                st["skip"] += 1
                continue
            have.add(k)
            pending.append(rec)
            added += 1
            if rec.get("results"):
                st["evaluated"] += 1
        if added:
            st["sym_ok"] += 1
    st["added"] = len(pending)
    if pending and not dry_run:
        records.extend(pending)
        save_accuracy(records)
        log(f"[backfill] 新增 {len(pending)} 条 (其中已带评估结果 "
            f"{st['evaluated']} 条) → 库内合计 {len(records)} 条")
    elif dry_run and pending:
        log(f"[backfill] dry-run: 将新增 {len(pending)} 条 (未写盘)")
    else:
        log("[backfill] 无新增记录")
    return st


# ── CLI ──
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="分析层历史快照回填 (整股结论准确度通道)")
    ap.add_argument("--source", default=None,
                    help="候选记录 JSON 或代码清单文本 (缺省用自选股)")
    ap.add_argument("--csv", dest="source", help="代码清单文件 (--source 别名)")
    ap.add_argument("--universe", action="store_true", help="用全市场主板列表")
    ap.add_argument("--universe-n", type=int, default=100,
                    help="全市场列表条数 (配合 --universe)")
    ap.add_argument("--max-symbols", type=int, default=None, help="最多处理标的数")
    ap.add_argument("--datalen", type=int, default=1200, help="日线根数 (默认 1200)")
    ap.add_argument("--step", type=int, default=DEFAULT_STEP, help="切点间隔根数")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="预热根数")
    ap.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS,
                    help="单标的最多切点数")
    ap.add_argument("--sleep", type=float, default=0.35, help="抓取间隔秒 (限流防护)")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args(argv)

    if os.environ.get("WYCKOFF_NO_NET") == "1":
        print("[backfill] WYCKOFF_NO_NET=1, 回填需要联网抓 K 线, 终止。")
        return 2

    symbols = load_symbols(args.source, use_universe=args.universe,
                           max_symbols=args.max_symbols,
                           universe_n=args.universe_n)
    if not symbols:
        print("!! 未解析到任何标的 (检查 --source / --csv)", file=sys.stderr)
        return 1
    print(f"待回填标的: {len(symbols)} · step={args.step} warmup={args.warmup} "
          f"max_points={args.max_points} datalen={args.datalen}")

    kfn = _real_kline_fn(args.datalen)
    bench_df = None
    try:
        bench_df = kfn(BENCH_SYMBOL)
    except Exception as e:
        print(f"[backfill] 基准 {BENCH_SYMBOL} 不可用 ({type(e).__name__}: {e})"
              f" → 超额收益缺省", flush=True)

    t0 = time.time()
    st = backfill_analysis(symbols, kfn, step=args.step, warmup=args.warmup,
                           max_points=args.max_points, sleep=args.sleep,
                           bench_df=bench_df, dry_run=args.dry_run)
    print(f"\n完成: 扫描 {st['scan']} 标的 · 有效 {st['sym_ok']} · 切点 "
          f"{st['points']} · 新增 {st['added']} (已评估 {st['evaluated']}) · "
          f"跳过 {st['skip']} · 失败 {st['failed']} · 耗时 {time.time() - t0:.1f}s")
    print("下一步: accuracy.accuracy_stats (方向命中率) / "
          "python -m wyckoff.calibrate (阶段自检)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
