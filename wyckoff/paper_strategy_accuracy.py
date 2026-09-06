"""模拟盘策略级追踪: 「策略4·纪律」与「价值吸筹」的准确度与盈利能力。

与 signal_accuracy.py (逐事件/VSA 信号口径) 互补, 这里以「策略 + 标的 + 事件类型」
为粒度记录每一次入场信号, 覆盖三个口径:

  1. 预测准确度: 信号之后 5/10/20 根真实收盘收益, 方向化命中 (都是多头策略,
     ret>0 记命中, ret<=0 记落空), 直接回答"这个策略选股准不准";
  2. 执行准确率: 条件单触点 (buy_price 入场 / take_profit 止盈 / stop_loss 止损)
     触发时 correct 字段的命中占比, 直接回答"止盈止损触点是不是反映了真实走势";
  3. 盈利能力: 从 wx_paper.json 的 closed[] 按策略聚合
     (胜率 / 平均收益 / 累计 / 盈亏比 / 期望值 / 平均持有), 回答"实际赚不赚钱"。

写入时机: 模拟盘每次扫描/周期生成候选信号时 (paper._apply_auto_conditions → record_signal,
廉价只落盘, 不拉行情)。
评估时机: record 时若调用方带了 df 则立即评估; 否则由 evaluate_pending
(UI 后台线程 / CLI --eval / 调度) 补抓行情评估, 参考 signal_accuracy 的延迟评估模式。
存储: ~/.wyckoff/wx_paper_strategy_accuracy.json
"""
from __future__ import annotations

import json
import statistics
import threading
import time

import numpy as np

from ._shared import atomic_write_json, run_pending_eval
from .datasource import fetch_kline
from .indicators import add_indicators
from .paths import PAPER_STRATEGY_ACCURACY_FILE

# 评估周期 (根, 日线): ≈1周/2周/1月
HORIZONS = (5, 10, 20)

# 多策略注册信息唯一来源: 策略管理器 (STRATEGY_ORDER/STRATEGY_CN 随选股逻辑
# 一并迁移入 wyckoff.strategies.manager, 此处不再单独维护)。
from .strategies.manager import (  # noqa: E402
    STRATEGY_CN,
    STRATEGY_ORDER,
)

# 冷却窗 (根): 同策略同标的同事件在 N 根内只保留一条信号, 防止逐周期扫描刷屏
COOLDOWN_BARS = 20

# 评估节流: 同一信号两次补评估间隔 (秒)
MIN_EVAL_INTERVAL = 3600

_LOCK = threading.Lock()

# 汇总缓存 (供 GUI 频繁读取时避免每次重算)
_STATS_CACHE = None
_STATS_CACHE_KEY = None


def _key(rec):
    return (f"{rec.get('strategy')}|{rec.get('symbol')}|"
            f"{rec.get('event_type')}|{rec.get('date')}")


def _cn(strategy):
    return STRATEGY_CN.get(strategy or "", strategy or "-")


# ── 存取 ────────────────────────────────────────────────
def load_signals():
    try:
        with open(PAPER_STRATEGY_ACCURACY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_signals(records):
    try:
        atomic_write_json(PAPER_STRATEGY_ACCURACY_FILE, records, indent=None)
        _invalidate_stats_cache()
    except Exception as e:
        from ._log import log_exc
        log_exc("paper_strategy_accuracy 落盘失败", e)


def _invalidate_stats_cache():
    global _STATS_CACHE, _STATS_CACHE_KEY
    _STATS_CACHE = None
    _STATS_CACHE_KEY = None


# ── 定位与评估 ──────────────────────────────────────────
def _locate(df, date_str):
    """在 df 中定位 date_str 对应的 bar 索引; 找不到返回 None。"""
    if df is None or df.empty:
        return None
    try:
        s = df["day"].astype(str)
        idx = np.where(s.values == str(date_str))[0]
        if len(idx):
            return int(idx[-1])
        idx = np.where(s.str.startswith(str(date_str)[:10]).values)[0]
        if len(idx):
            return int(idx[-1])
    except Exception:
        pass
    return None


def _locate_by_price(df, price, window=60, tol=0.03):
    """在 df 尾部 window 根内, 按收盘价最接近 ref_px (容差 tol) 定位信号 bar。

    用于延迟评估时信号日期已偏离最新行情的场景 (扫描后几天才补行情):
    信号价≈扫描当日收盘价, 在尾部窗口内最接近处即真实信号位置。
    """
    if df is None or df.empty or not price or price <= 0:
        return None
    closes = df["close"].values
    n = len(df)
    best, best_d = None, None
    for i in range(max(0, n - window), n):
        c = float(closes[i])
        if c <= 0:
            continue
        d = abs(c - price) / price
        if best_d is None or d < best_d:
            best_d, best = d, i
    if best is not None and best_d <= tol:
        return best
    return None


def _eval_against(df, idx, rec):
    """用 df 中 idx 之后的真实行情评估缺失周期。返回是否有新增评估。

    双口径与 signal_accuracy 一致, 但此处只保留原始收益 ret (策略都是多头)。
    """
    if idx is None:
        return False
    c = df["close"].values
    if idx < 0 or idx >= len(df) or c[idx] <= 0:
        return False
    results = dict(rec.get("results") or {})
    changed = False
    for h in HORIZONS:
        k = str(h)
        if k in results:
            continue
        if idx + h < len(df) and c[idx] > 0:
            results[k] = {"ret": round(float(c[idx + h] / c[idx] - 1), 6)}
            changed = True
    rec["results"] = results
    done = len(results) >= len(HORIZONS)
    rec["status"] = "done" if done else "pending"
    if not done and idx + min(HORIZONS) >= len(df):
        rec["waiting"] = True
    else:
        rec["waiting"] = False
    return changed


def _resolve_idx(df, rec):
    idx = _locate(df, rec.get("date", ""))
    if idx is None:
        idx = _locate_by_price(df, rec.get("ref_px"))
    return idx


# ── 记录 ────────────────────────────────────────────────
def _cooldown_dup(existing, rec, df, cooldown_bars):
    """冷却窗内找同策略+同标的+同事件类型的未评估旧记录 (合并用), 无则 None。"""
    if df is None or df.empty:
        return None
    rec_idx = _resolve_idx(df, rec)
    if rec_idx is None:
        return None
    for key, old in existing.items():
        if old.get("strategy") != rec.get("strategy"):
            continue
        if old.get("symbol") != rec.get("symbol"):
            continue
        if old.get("event_type") != rec.get("event_type"):
            continue
        o_idx = _resolve_idx(df, old)
        if o_idx is None:
            continue
        if abs(int(o_idx) - int(rec_idx)) <= cooldown_bars:
            return key
    return None


def _mk_rec(strategy, symbol, code, name, event_type, conf, date, price,
            fired=False):
    """构造一条信号记录。"""
    return {
        "strategy": strategy or "",
        "symbol": symbol or "",
        "code": str(code or "")[-6:],
        "name": name or "",
        "event_type": event_type or "",
        "conf": int(conf or 0),
        "date": str(date),
        "ref_px": float(price or 0),
        "created_ts": time.time(),
        "last_eval_ts": 0,
        "status": "pending",
        "eval_fails": 0,
        "waiting": False,
        "fired": bool(fired),
        "results": {},
    }


def _upsert(existing, rec, df, cooldown_bars=COOLDOWN_BARS):
    """把 rec 并入 existing (key=_key), 返回动作: skip/new/dup。

    - skip: 同 key 且已评估, 保留原结果;
    - dup: 冷却窗内合并进旧记录 (刷新 conf/ref_px/日期, 带 df 时重评估);
    - new: 新增 (带 df 时立即评估)。
    """
    key = _key(rec)
    old = existing.get(key)
    if old is not None and old.get("results"):
        return "skip"
    dup = None
    if not old and cooldown_bars > 0:
        dup = _cooldown_dup(existing, rec, df, cooldown_bars)
    if dup is not None:
        prior = existing[dup]
        prior["ref_px"] = rec["ref_px"]
        prior["date"] = rec["date"]
        prior["conf"] = max(int(prior.get("conf", 0) or 0), rec["conf"])
        if not prior.get("name"):
            prior["name"] = rec["name"]
        if rec.get("fired"):
            prior["fired"] = True
        if df is not None:
            _eval_against(df, _resolve_idx(df, prior), prior)
        return "dup"
    existing[key] = rec
    if df is not None:
        _eval_against(df, _resolve_idx(df, rec), rec)
    return "new"


def record_signal(strategy, symbol, code, name, event_type, conf, date, price,
                  cooldown_bars=COOLDOWN_BARS, df=None, fired=False):
    """记录一条策略信号 (扫描/周期生成候选时调用, 廉价, 不拉行情)。

    同策略+同标的+同事件在冷却窗内重复 → 合并 (刷新 conf / 名称 / 最新信号价,
    不覆盖已评估结果)。df 提供时立即评估 (供有数据在手的调用方可选优化)。

    返回: 1=新增, -1=合并已有, 0=忽略。
    """
    rec = _mk_rec(strategy, symbol, code, name, event_type, conf, date, price,
                  fired=fired)
    with _LOCK:
        records = load_signals()
        existing = {_key(r): r for r in records}
        action = _upsert(existing, rec, df, cooldown_bars)
        if action != "skip":
            save_signals(list(existing.values()))
    return {"new": 1, "dup": -1}.get(action, 0)


def run_signal_pipeline(items):
    """批量处理一批评测/历史回放生成的信号, 单次载入 + 冷却合并 + 立即评估, 单次落盘。

    items: [{strategy, symbol, code, name, event_type, conf, date, ref_px,
             fired, df}, ...]。df 提供时立即评估该信号的 5/10/20 根收益。

    比逐条 record_signal 高效 (避免 N 次全量读写盘), 供大样本回放回测使用。
    返回 {"added", "merged", "skipped", "evaluated"}。
    """
    out = {"added": 0, "merged": 0, "skipped": 0, "evaluated": 0}
    with _LOCK:
        records = load_signals()
        existing = {_key(r): r for r in records}
        for it in items:
            rec = _mk_rec(
                it.get("strategy", ""), it.get("symbol", ""),
                it.get("code", ""), it.get("name", ""),
                it.get("event_type", ""), it.get("conf", 0),
                it.get("date", ""), it.get("ref_px", 0),
                fired=bool(it.get("fired")))
            action = _upsert(existing, rec, it.get("df"))
            if action == "skip":
                out["skipped"] += 1
            elif action == "new":
                out["added"] += 1
            else:
                out["merged"] += 1
            if rec.get("results"):
                out["evaluated"] += 1
        save_signals(list(existing.values()))
    return out


def mark_fired(strategy, symbol):
    """把最近一条同策略+同标的的未成交信号标记为"已执行买入" (fill_buy 调用)。

    用于区分「信号产生」与「实际成交」, 二者数量差即为漏买/未触发。
    返回是否命中更新。
    """
    if not strategy or not symbol:
        return False
    with _LOCK:
        records = load_signals()
        changed = False
        for r in records:
            if (r.get("strategy") == strategy and r.get("symbol") == symbol
                    and not r.get("fired")):
                r["fired"] = True
                changed = True
        if changed:
            save_signals(records)
        return changed


# ── 评估 ────────────────────────────────────────────────
def _evaluate_one(rec):
    """拉取最新行情评估单条记录缺失周期 (失败递增 eval_fails 返回 False)。"""
    scale = 240
    try:
        df = add_indicators(
            fetch_kline(rec["symbol"], datalen=max(300, 420 + 80), scale=scale))
    except Exception:
        raise
    if df is None or len(df) == 0:
        raise ValueError("empty kline")
    idx = _resolve_idx(df, rec)
    if idx is None:
        raise ValueError(f"locate fail: {rec.get('date')}")
    return _eval_against(df, idx, rec)


def evaluate_pending(force=False, min_interval=MIN_EVAL_INTERVAL, max_records=40):
    """对缺评估周期的信号补评估, 返回新增评估条数。"""
    records = load_signals()
    if not records:
        return 0
    return run_pending_eval(records, _evaluate_one, HORIZONS,
                            load_signals, save_signals, _key, _LOCK,
                            force=force, min_interval=min_interval,
                            max_records=max_records)


def run_auto_eval(force=False):
    try:
        return evaluate_pending(force=force)
    except Exception:
        return 0


# ── 汇总 ────────────────────────────────────────────────
def signal_stats(records=None, force=False):
    """按策略汇总预测准确度 (方向化命中: 多头策略 ret>0 记命中)。

    返回 {strategy: {"n","evaluated","pending","horizons": {h: {"n","hit","avg"}},
                     "hit20": ..., "avg20": ...}}。
    """
    global _STATS_CACHE, _STATS_CACHE_KEY
    if records is None:
        recs = load_signals()
    else:
        recs = records
    cache_key = id(recs) if records is not None else None
    if not force and _STATS_CACHE is not None and _STATS_CACHE_KEY == cache_key:
        return _STATS_CACHE
    base = {s: {"n": 0, "evaluated": 0, "horizons": {str(h): [] for h in HORIZONS}}
            for s in STRATEGY_ORDER}
    for r in recs:
        strat = r.get("strategy", "")
        b = base.setdefault(strat, {
            "n": 0, "evaluated": 0, "horizons": {str(h): [] for h in HORIZONS}})
        b["n"] += 1
        results = r.get("results") or {}
        if not results:
            continue
        b["evaluated"] += 1
        for h in HORIZONS:
            rr = results.get(str(h))
            if rr and rr.get("ret") is not None:
                b["horizons"][str(h)].append(rr["ret"])
    out = {}
    for s, b in base.items():
        cum = {}
        for h in HORIZONS:
            rs = b["horizons"][str(h)]
            if not rs:
                cum[str(h)] = {"n": 0, "hit": None, "avg": None}
                continue
            hit = sum(1 for v in rs if v > 0)
            cum[str(h)] = {
                "n": len(rs),
                "hit": round(hit / len(rs), 4),
                "avg": round(statistics.mean(rs), 6),
            }
        out[s] = {
            "n": b["n"],
            "evaluated": b["evaluated"],
            "pending": b["n"] - b["evaluated"],
            "horizons": cum,
            "hit20": (cum["20"].get("hit") if cum["20"] else None),
            "avg20": (cum["20"].get("avg") if cum["20"] else None),
        }
    out["_summary"] = {
        "total": sum(base[s]["n"] for s in STRATEGY_ORDER),
        "evaluated": sum(base[s]["evaluated"] for s in STRATEGY_ORDER),
    }
    _STATS_CACHE = out
    _STATS_CACHE_KEY = cache_key
    return out


def _reason_strategy(reason, st):
    """从条件单 reason / 标的关系中解析策略。优先 reason 前缀, 回退按代码关联持仓。"""
    if isinstance(reason, str):
        for p in ("自动:",):
            if reason.startswith(p):
                rest = reason[len(p):]
                for s in STRATEGY_ORDER:
                    if rest.startswith(s):
                        return s
    return None


def cond_accuracy(st, strategy=None):
    """从 wx_paper 状态 (conditions + positions/closed) 聚合执行触点准确率。

    按条件单 reason 中的策略前缀或该代码持仓/平仓的策略归属归类;
    已触发 (status=done) 且 correct!=None 的计入分母, correct=True 计入命中。
    返回 {strategy: {"done","correct","wrong","accuracy"}}。
    """
    st = st or {}
    by_code = {}
    for p in st.get("positions", []):
        by_code[p.get("symbol")] = p.get("strategy", "")
    for c in st.get("closed", []):
        by_code.setdefault(c.get("symbol"), c.get("strategy", ""))
    agg = {s: {"done": 0, "correct": 0, "wrong": 0, "unknown": 0}
           for s in STRATEGY_ORDER}
    for c in st.get("conditions", []):
        if c.get("status") != "done":
            continue
        s = _reason_strategy(c.get("reason", ""), st) \
            or by_code.get(c.get("symbol")) or ""
        if s not in agg:
            continue
        if strategy is not None and s != strategy:
            continue
        correct = c.get("correct")
        agg[s]["done"] += 1
        if correct is True:
            agg[s]["correct"] += 1
        elif correct is False:
            agg[s]["wrong"] += 1
        else:
            agg[s]["unknown"] += 1
    out = {}
    for s, a in agg.items():
        judged = a["correct"] + a["wrong"]
        out[s] = {
            "done": a["done"],
            "correct": a["correct"],
            "wrong": a["wrong"],
            "unknown": a["unknown"],
            "accuracy": round(a["correct"] / judged, 4) if judged else None,
        }
    return out


def profit_summary(st):
    """按策略聚合盈利能力 (已平仓 closed[] + 在持 + 候选信号数)。

    cum 采用真实累计: (1+r) 连乘 − 1, 避免简单累加在样本少时被一笔大单主导。
    """
    st = st or {}
    agg = {s: {"n": 0, "rets": [], "positions": 0, "signals": 0, "bars": []}
           for s in STRATEGY_ORDER}
    for c in st.get("closed", []):
        s = c.get("strategy", "")
        if s not in agg:
            continue
        agg[s]["n"] += 1
        agg[s]["rets"].append(float(c.get("ret", 0) or 0))
        agg[s]["bars"].append(int(c.get("bars", 0) or 0))
    for p in st.get("positions", []):
        s = p.get("strategy", "")
        if s in agg:
            agg[s]["positions"] += 1
    for c in st.get("candidates", []):
        s = c.get("strategy", "")
        if s in agg:
            agg[s]["signals"] += 1
    out = {}
    for s, a in agg.items():
        rets = a["rets"]
        if rets:
            wins = [r for r in rets if r > 0]
            losses = [r for r in rets if r <= 0]
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = abs(statistics.mean(losses)) if losses else 0.0
            cum = 1.0
            for r in rets:
                cum *= (1.0 + r)
            out[s] = {
                "n": a["n"],
                "positions": a["positions"],
                "signals": a["signals"],
                "win_rate": round(len(wins) / len(rets), 4),
                "avg_ret": round(statistics.mean(rets), 4),
                "cum_ret": round(cum - 1.0, 4),
                "pl_ratio": round(avg_win / avg_loss, 3) if avg_loss > 0 else None,
                "expectancy": round(
                    (len(wins) / len(rets)) * avg_win
                    - (1 - len(wins) / len(rets)) * avg_loss, 4),
                "avg_hold_bars": round(statistics.mean(a["bars"]), 1) if a["bars"] else None,
            }
        else:
            out[s] = {"n": 0, "positions": a["positions"], "signals": a["signals"],
                      "win_rate": None, "avg_ret": None, "cum_ret": None,
                      "pl_ratio": None, "expectancy": None, "avg_hold_bars": None}
    return out


def strategy_report(st=None):
    """合并三个口径为一份报告 (供 UI / 导出)。"""
    sig = signal_stats()
    cond = cond_accuracy(st)
    prof = profit_summary(st)
    report = {}
    for s in STRATEGY_ORDER:
        report[s] = {
            "name": _cn(s),
            "accuracy": sig.get(s, {}),
            "execution": cond.get(s, {}),
            "profit": prof.get(s, {}),
        }
    report["_generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["_summary"] = sig.get("_summary", {})
    return report


def export_report(st=None, path=None):
    """导出策略追踪周报 (Markdown), 与报告导出按钮联动。"""
    path = path or PAPER_STRATEGY_ACCURACY_FILE.replace(".json", "_review.md")
    lines = _render_report(strategy_report(st))
    with open(path, "w", encoding="utf-8") as f:
        f.write(lines)
    return path


def _render_report(rep):
    L = ["# 模拟盘策略追踪报告", "",
         f"- 生成时间: {rep['_generated_at']}",
         f"- 信号样本: 累计 {rep['_summary'].get('total', 0)} 条 · "
         f"已评估 {rep['_summary'].get('evaluated', 0)} 条",
         "",
         "| 策略 | 信号 | 已评估 | 5根命中 | 10根命中 | 20根命中 | 20根均值 "
         "| 触点正确 | 平仓 | 胜率 | 平均 | 累计 | 盈亏比 | 期望 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in STRATEGY_ORDER:
        d = rep.get(s, {})
        acc = d.get("accuracy", {})
        exe = d.get("execution", {})
        pf = d.get("profit", {})
        h = acc.get("horizons", {})
        ca = exe.get("accuracy")

        def _pct(v):
            return f"{v * 100:.0f}%" if v is not None else "-"

        pos = (f"{exe.get('correct', 0)}/{exe.get('done', 0)}"
               + (f"({ca * 100:.0f}%)" if ca is not None else ""))
        pf_txt = (f"{pf.get('n', 0)}" if pf.get('n') else "0")
        wr = f"{pf['win_rate'] * 100:.0f}%" if pf.get("win_rate") is not None else "-"
        avg = f"{pf['avg_ret'] * 100:+.2f}%" if pf.get("avg_ret") is not None else "-"
        cum = f"{pf['cum_ret'] * 100:+.2f}%" if pf.get("cum_ret") is not None else "-"
        plr = f"{pf['pl_ratio']:.2f}" if pf.get("pl_ratio") is not None else "-"
        exp = f"{pf['expectancy']:+.4f}" if pf.get("expectancy") is not None else "-"
        L.append(f"| {d.get('name', s)} | {acc.get('n', 0)} | {acc.get('evaluated', 0)} "
                 f"| {_pct(h.get('5', {}).get('hit'))} | {_pct(h.get('10', {}).get('hit'))} "
                 f"| {_pct(h.get('20', {}).get('hit'))} | {_pct(h.get('20', {}).get('avg'))}"
                 f" | {pos} | {pf_txt} | {wr} | {avg} | {cum} | {plr} | {exp} |")
    L.append("")
    L.append("> 命中=信号后 N 根方向化命中 (多头 ret>0); 触点=条件单触发时 correct 判断; "
             "盈利=已平仓净收益 (扣成本)。")
    return "\n".join(L)


# ── CLI ─────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if "--eval" in args:
        n = run_auto_eval(force=True)
        print(f"\n本次新增评估 {n} 条")
        print(export_report())
    elif "--report" in args:
        print(export_report())
    else:
        st0 = None
        try:
            from .paper import load_state
            st0 = load_state()
        except Exception:
            pass
        print(_render_report(strategy_report(st0)))
        print("命令: --eval 补评估 / --report 导出周报")
