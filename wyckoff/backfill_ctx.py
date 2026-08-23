"""历史样本 L5 语境特征回填 (一次性 CLI)。

把信号准确度库 (wx_signal_accuracy.json 的 event 记录) 与阶段带反馈库
(wx_feedback.json) 中缺语境特征的旧记录, 按"信号日当天为截止"的前缀
数据重算 L5 特征 (wyckoff/context.enrich, 严格无前视), 就地补齐落库,
救回 online_model v2 的可训练样本。

用法:
    python -m wyckoff.backfill_ctx [--dry-run] [--sleep 0.4] [--force]

说明:
    - 仅处理 scale==240 (日线) 记录: RS/指数语境按日历对齐设计。
    - 指数序列整轮只取一次; 个股 K 线按 symbol 去重后逐只抓取并限速。
    - sec_pct 无历史快照, 恒为缺省 (安全填充 0.5)。
    - --force 时对已有语境特征的记录也重算。
"""
import argparse
import sys
import time

import pandas as pd

from .context import CONTEXT_FEAT_KEYS, enrich


def _has_ctx(features):
    return any(k in (features or {}) for k in CONTEXT_FEAT_KEYS)


def _locate_idx(days_or_df, date_str, max_gap=7):
    """在日线里定位 date_str 对应索引 (缺失时取之前最近交易日)。

    days_or_df 可传 df (取其 day 列) 或直接传 day Series。"""
    try:
        ts = pd.Timestamp(date_str)
    except Exception:
        return None
    days = days_or_df["day"] if isinstance(days_or_df, pd.DataFrame) \
        else days_or_df
    j = int(days.searchsorted(ts, side="right")) - 1
    if j < 0:
        return None
    gap = (ts - pd.Timestamp(days.iloc[j])).days
    if abs(gap) > max_gap:
        return None
    return int(j)


def _climax_events(sub_df, sub_pivots_unused=None):
    """前缀上的 SC/BC 高潮事件 (供 base_len_n 因果长度用), 相对前缀索引。"""
    try:
        from .events import _EventContext, detect_climaxes
        return detect_climaxes(_EventContext(sub_df)) or []
    except Exception:
        return []


def backfill_one_signal(rec, kline_df, index_df=None):
    """回填单条 event 记录的语境特征。返回 True 表示已写回。

    kline_df 为该标的日线全量 (含指标列); 内部按信号日前缀切片重算,
    保证与实盘捕获同一因果口径。"""
    from .indicators import find_pivots
    i_abs = _locate_idx(kline_df, rec.get("date", ""))
    if i_abs is None or i_abs < 70:
        return False
    m = min(len(kline_df), i_abs + 1 + 12)
    sub = kline_df.iloc[:m].reset_index(drop=True)
    feats_in = dict(rec.get("features") or {})
    ev = {"type": rec.get("type", ""),
          "idx": i_abs,
          "date": rec.get("date"),
          "price": None, "desc": "", "color": "",
          "conf": rec.get("conf", 50),
          "feat": feats_in}
    evs = [ev] + [c for c in _climax_events(sub) if c.get("idx") != i_abs]
    pivots = find_pivots(sub)
    enrich(sub, pivots, evs, index_df=index_df)
    out = ev.get("feat") or {}
    got = {k: out.get(k) for k in CONTEXT_FEAT_KEYS}
    if not any(v is not None for v in got.values()):
        return False
    feats_in.update(got)
    rec["features"] = feats_in
    return True


def backfill_signals(records, kline_fn, index_df=None, force=False,
                     sleep=0.0, log=None):
    """批量回填信号库。kline_fn(symbol) -> df|None。返回统计 dict。"""
    st = {"scan": 0, "todo": 0, "ok": 0, "skip": 0}
    by_sym = {}
    for r in records or []:
        if r.get("kind") != "event":
            continue
        st["scan"] += 1
        if not force and _has_ctx(r.get("features")):
            continue
        if int(r.get("scale", 240) or 240) != 240:
            st["skip"] += 1
            continue
        by_sym.setdefault(r.get("symbol") or "", []).append(r)
    for sym, group in by_sym.items():
        if not sym:
            st["skip"] += len(group)
            continue
        df = kline_fn(sym)
        if df is None or len(df) < 100:
            st["skip"] += len(group)
            if log:
                log(f"  跳过 {sym}: 无 K 线数据")
            continue
        for r in group:
            st["todo"] += 1
            try:
                if backfill_one_signal(r, df, index_df=index_df):
                    st["ok"] += 1
                else:
                    st["skip"] += 1
            except Exception as e:
                st["skip"] += 1
                if log:
                    log(f"  失败 {sym} {r.get('date')}: {e}")
        if sleep and log is not None:
            time.sleep(sleep)
    return st


def backfill_feedback(records, kline_fn, force=False, sleep=0.0, log=None):
    """批量回填阶段带反馈库特征 (按 start_dt/end_dt 重定位后重造)。"""
    from .storage import build_feedback_record
    st = {"scan": 0, "todo": 0, "ok": 0, "skip": 0}
    by_sym = {}
    for r in records or []:
        st["scan"] += 1
        if not force and _has_ctx_fb(r.get("features")):
            continue
        by_sym.setdefault(r.get("symbol") or "", []).append(r)
    for sym, group in by_sym.items():
        if not sym:
            st["skip"] += len(group)
            continue
        df = kline_fn(sym)
        if df is None or len(df) < 60:
            st["skip"] += len(group)
            continue
        days = df["day"]
        for r in group:
            st["todo"] += 1
            try:
                a = _locate_idx(days, r.get("start_dt", ""))
                e = _locate_idx(days, r.get("end_dt", ""))
                if a is None or e is None or e < a:
                    st["skip"] += 1
                    continue
                fresh = build_feedback_record(
                    sym, len(df), int(r.get("scale", 240) or 240),
                    df, a, e, r.get("label", ""), r.get("label_cn", ""))
                feat = fresh.get("features") or {}
                if feat:
                    old = dict(r.get("features") or {})
                    old.update(feat)
                    r["features"] = old
                    st["ok"] += 1
                else:
                    st["skip"] += 1
            except Exception as ex:
                st["skip"] += 1
                if log:
                    log(f"  失败 {sym} {r.get('start_dt')}: {ex}")
        if sleep:
            time.sleep(sleep)
    return st


def _has_ctx_fb(features):
    """反馈库特征判定: 泛化后任何带 lo1 的记录视为已含特征。"""
    return bool(features) and "lo1" in features


def _real_kline_fn(cache={}):
    from .datasource import fetch_kline
    def _get(sym):
        if sym not in cache:
            cache[sym] = fetch_kline(sym, datalen=1500, scale=240,
                                     use_cache=True)
        return cache[sym]
    return _get


def main(argv=None):
    ap = argparse.ArgumentParser(description="L5 语境特征历史回填")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写回")
    ap.add_argument("--force", action="store_true", help="已含特征的也重算")
    ap.add_argument("--sleep", type=float, default=0.35,
                    help="逐标的抓取间隔秒数")
    args = ap.parse_args(argv)

    import os
    if os.environ.get("WYCKOFF_NO_NET") == "1":
        print("[backfill] WYCKOFF_NO_NET=1, 回填需要联网抓 K 线, 终止。")
        return 2

    def log(msg):
        print(msg, flush=True)

    from .market import fetch_market_series
    index_df = fetch_market_series()
    log(f"[backfill] 指数序列: {'OK' if index_df is not None else '不可用 (RS/指数语境将缺省)'}")

    kfn = _real_kline_fn()

    from .signal_accuracy import load_signals, save_signals
    recs = load_signals()
    sig_st = backfill_signals(recs, kfn, index_df=index_df,
                              force=args.force, sleep=args.sleep, log=log)
    log(f"[backfill] 信号库: 扫描 {sig_st['scan']} / 待回填 {sig_st['todo']} / "
        f"成功 {sig_st['ok']} / 跳过 {sig_st['skip']}")
    if not args.dry_run and sig_st["ok"]:
        save_signals(recs)

    from .storage import load_feedback, save_feedback
    fb = load_feedback()
    fb_st = backfill_feedback(fb, kfn, force=args.force,
                              sleep=args.sleep, log=log)
    log(f"[backfill] 反馈库: 扫描 {fb_st['scan']} / 待回填 {fb_st['todo']} / "
        f"成功 {fb_st['ok']} / 跳过 {fb_st['skip']}")
    if not args.dry_run and fb_st["ok"]:
        save_feedback(fb)

    log("[backfill] 完成。" + (" (dry-run 未写盘)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
