"""今日入场点自动记录 & 胜率统计 (wx_entry_journal.json)。

每次"今日可靠入场点"扫描出的命中行都会自动入账 (key = code|type|entry_date|
scale, 幂等去重), 事后按 确认可交易口径 逐笔结算方向胜负:

  入场价   = 确认bar收盘 (当时可成交, 与 signal_accuracy.ret_c 同口径);
  止损     = 事件低点;
  每个观察期 h (ENTRY_JOURNAL_HORIZONS, 默认 10/20 根):
    期内任一根 low ≤ 止损 → 判定触发止损 (记亏, ret = 止损/入场 - 1);
    数据满 h 根未触止损 → ret = close[h]/入场 - 1, 上涨记胜 (多头事件);
    数据不足 h 根   → 保持 open, 不算胜负, 待后续行情补充结算。

统计 output (journal_stats) 提供 全局 + 按类型 的 笔数/胜数/胜率/均收益,
供 UI 与报告复用。存量信号层历史胜率见 signal_accuracy (事件池),
本文件只跟踪"今日入场点实际选出"的标的。
"""
import json
import threading
import time

from ._shared import atomic_write_json
from .paths import ENTRY_JOURNAL_FILE

# 结算观察期 (根): 与 signal_accuracy.HORIZONS 的 10/20 挡对齐
ENTRY_JOURNAL_HORIZONS = (10, 20)
# 结算用 K 线长度: 保证金字 (确认bar一定在其中)
ENTRY_JOURNAL_DATALEN = 500
# 每次 evaluate_pending 最多抓取的代码数 (UI 调用防阻塞/防刷接口)
EVALUATE_CODE_LIMIT = 40

_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "data": None}
_CACHE_TTL = 30.0


def _load():
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]
    try:
        with open(ENTRY_JOURNAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        data = data.get("records") if isinstance(data, dict) else data
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data


def _save(data):
    _CACHE["ts"] = time.time()
    _CACHE["data"] = data
    atomic_write_json(ENTRY_JOURNAL_FILE, {"version": 1, "records": data})


def load_records():
    """返回 {key: rec} 记录字典 (瞬时快照, 不含锁)。"""
    with _LOCK:
        return dict(_load())


def _make_rec(r, default_scale):
    code = str(r.get("code") or "").strip()
    entry = float(r.get("entry_price"))
    stop = float(r.get("stop"))
    if entry <= 0 or stop <= 0 or stop >= entry:
        return None
    scale = int(r.get("scale") or default_scale or 240)
    return {
        "key": "|".join((code, str(r.get("type") or ""),
                         str(r.get("entry_date") or ""), str(scale))),
        "code": code,
        "name": str(r.get("name") or ""),
        "type": str(r.get("type") or ""),
        "entry_date": str(r.get("entry_date") or ""),
        "entry_price": round(entry, 4),
        "stop": round(stop, 4),
        "conf": int(r.get("conf") or 0),
        "risk_pct": float(r.get("risk_pct") or 0),
        "scale": scale,
        "recorded_ts": time.time(),
        "ev": {},
    }


def record_entries(rows, scale=240):
    """把扫描命中的入场点行自动入账 (幂等去重)。

    rows: _scan_one 产出的行字典; 返回 {"added","dup","skipped"}。
    """
    if not rows:
        return {"added": 0, "dup": 0, "skipped": 0}
    with _LOCK:
        data = _load()
        added = dup = skipped = 0
        for r in rows:
            if not ((r.get("code") or "").strip() and r.get("type")
                    and (r.get("entry_date") or "").strip()):
                skipped += 1
                continue
            try:
                rec = _make_rec(r, scale)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if rec is None:
                skipped += 1
                continue
            key = rec["key"]
            if key in data:
                dup += 1
                continue
            data[key] = rec
            added += 1
        if added:
            _save(data)
        return {"added": added, "dup": dup, "skipped": skipped}


def _eval_record(rec, df):
    """用一个代码的 K 线结算该记录尚开放的各观察期。返回是否产生新判定。"""
    from .utils import locate_bar
    if df is None or len(df) == 0:
        return False
    i0 = locate_bar(df, str(rec["entry_date"]))
    if i0 is None or i0 >= len(df) - 1:
        return False                     # 数据还不含入场日 / 入场即末根 → 保持 open
    try:
        entry = float(rec["entry_price"])
        stop = float(rec["stop"])
    except (TypeError, ValueError):
        return False
    if entry <= 0 or stop <= 0 or stop >= entry:
        return False
    c = df["close"].values
    low = df["low"].values
    ev = dict(rec.get("ev") or {})
    changed = False
    for h in ENTRY_JOURNAL_HORIZONS:
        k = str(h)
        prev = ev.get(k)
        if prev and prev.get("state") != "open":
            continue                     # 已结算不重复判
        i_end = i0 + h
        if i_end >= len(df):
            continue                     # 数据未到 h 根 → 保持 open
        if low[i0 + 1: i_end + 1].min() <= stop:
            state, ret = "stop", stop / entry - 1.0
        else:
            ret = float(c[i_end]) / entry - 1.0
            state = "win" if ret > 0 else "loss"
        ev[k] = {"state": state, "ret": round(float(ret), 6)}
        changed = True
    if changed:
        rec["ev"] = ev
    return changed


def evaluate_pending(limit=EVALUATE_CODE_LIMIT):
    """结算未完成记录: 按代码抓一次 K 线, 更新所有 open 观察期。

    limit 为单次最多结算的代码数 (0/None=不限制); 抓网前释放锁 (不阻塞
    扫描线程的 record_entries); 返回 {"codes","records"} (涉及数与抓取数)。
    """
    from .datasource import fetch_kline
    with _LOCK:
        data = _load()
        by_code = {}
        for rec in data.values():
            ev = rec.get("ev") or {}
            open_h = [h for h in ENTRY_JOURNAL_HORIZONS
                      if not (ev.get(str(h)) and ev[str(h)].get("state") in ("win", "loss", "stop"))]
            if not open_h:
                continue                 # 全部观察期已结算
            by_code.setdefault(str(rec.get("code")), []).append(rec)
        codes = list(by_code)
        if limit:
            codes = codes[:int(limit)]
        snapshot = [(code, int(by_code[code][0].get("scale") or 240),
                     list(by_code[code])) for code in codes]
    changed = False
    for code, scale, recs in snapshot:
        try:
            df = fetch_kline(code, datalen=ENTRY_JOURNAL_DATALEN, scale=scale)
        except Exception:
            continue
        for rec in recs:
            if _eval_record(rec, df):
                changed = True
    if changed:
        with _LOCK:
            cur = _load()
            for _code, _scale, recs in snapshot:
                for rec in recs:
                    if rec["key"] in cur:
                        cur[rec["key"]]["ev"] = rec["ev"]
            _save(cur)
    return {"codes": len(snapshot), "records": sum(len(r) for r in
                                                   [r for _c, _s, r in snapshot])}


def _agg(bucket, h, st):
    a = bucket.setdefault(h, {"n": 0, "win": 0, "loss": 0, "rets": []})
    a["n"] += 1
    if st["state"] == "win":
        a["win"] += 1
    else:
        a["loss"] += 1
    a["rets"].append(float(st.get("ret") or 0))


def _finalize(bucket):
    for h, a in bucket.items():
        wins = a.get("win", 0)
        n = a.get("n", 0)
        rets = a.get("rets", [])
        a["win_pct"] = wins / n if n else None
        a["mean_ret"] = (sum(rets) / len(rets)) if rets else None
        a.pop("rets", None)


def journal_stats(refresh=False, horizons=ENTRY_JOURNAL_HORIZONS):
    """统计自动记录入场点的胜率。

    refresh=True 时先按最新行情结算一笔 (open → win/loss/stop) 再统计。
    返回 {n_recorded, n_open, h: {h: {n,win,loss,win_pct,mean_ret}},
    by_type: {type: {h: {...}}}} — open(未结算)不计入胜率分母。
    """
    if refresh:
        evaluate_pending()
    with _LOCK:
        recs = list(_load().values())
    out = {"n_recorded": len(recs), "n_open": 0, "h": {}, "by_type": {}}
    for rec in recs:
        ev = rec.get("ev") or {}
        opened = False
        for h in horizons:
            st = ev.get(str(h))
            if st and st.get("state") in ("win", "loss", "stop"):
                _agg(out["h"], str(h), st)
                bt = out["by_type"].setdefault(rec.get("type") or "?", {})
                _agg(bt, str(h), st)
            else:
                opened = True
        if opened:
            out["n_open"] += 1
    _finalize(out["h"])
    for bt in out["by_type"].values():
        _finalize(bt)
    return out
