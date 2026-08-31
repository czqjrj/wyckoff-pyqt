"""跨模块共享的小工具 (避免在 ninetests/counterevidence 等处重复定义)。

集中在同一文件, 保证相同语义的窗口/量波计算只有一份实现, 防止分叉漂移。
"""
import json
import os
import tempfile
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from .config import W_RECENT

# ── 共享 HTTP Session ──
# 全项目所有行情/基本面请求复用一条连接池: 每请求省去完整 TCP+TLS 握手
# (实测 0.13s → 0.05s), 全市场扫描数千请求累计节省数分钟。
# urllib3 连接池线程安全, 可多线程共享; connect/read 失败自动重试 1 次。
_SESSION = None
_SESSION_LOCK = threading.Lock()


def http_session():
    """返回全局共享 requests.Session (懒初始化, 线程安全)。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                s = requests.Session()
                try:
                    from urllib3.util.retry import Retry
                    retry = Retry(total=1, connect=1, read=1,
                                  backoff_factor=0.3,
                                  allowed_methods=frozenset(["GET"]))
                    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16,
                                          max_retries=retry)
                except Exception:
                    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


def atomic_write_json(path, data, indent=2):
    """原子写 JSON: 先写临时文件再 os.replace, 崩溃/断电不会截断目标文件。

    所有用户数据落盘 (设置/自选/笔记/持仓/准确度/拼音索引等) 都应走这里。
    indent=None 用紧凑序列化 (大文件体积 -40% 左右, 供高频写入的大记录集用)。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wyckoff_tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def recent_events(events, df, span=None):
    """取最近 span 根内的事件 (默认用全局近期窗口)。"""
    if span is None:
        span = W_RECENT
    cutoff = len(df) - span
    return [e for e in events if e["idx"] >= cutoff]


def vol_wave(df, window=40):
    """近 window 根上涨/下跌波量均值。"""
    seg = df.tail(window)
    up = seg[seg["close"] >= seg["open"]]["volume"].mean()
    dn = seg[seg["close"] < seg["open"]]["volume"].mean()
    return (up if up == up else 0.0), (dn if dn == dn else 0.0)


def parallel_map(codes, fn, workers=8, stop_fn=None, progress=None, on_result=None):
    """线程池并行映射: 对 codes 逐项调用 fn(code), 收集结果。

    供全市场扫描 (`entries.scan_entries_parallel` 等) 复用; 为并行框架的
    唯一实现 (避免各扫描引擎各写一套 ThreadPoolExecutor)。

    参数:
      codes        可迭代的条目 (str 代码)
      fn(code)     单条处理函数; 正常返回任意结果 (会在结果里收集),
                   返回 None 或抛异常则跳过该条
      workers      并发数 (自动 clamp 到 [1, 12]; 0/负/None 视为串行)
      stop_fn()    可选; 调用返回 True 时尽早停止派发/收集
      progress(done, total, code)  可选进度回调
      on_result(r, code) 可选; 每条 fn 返回非 None 结果时的增量回调
                    (r=结果, code=条目; UI 流式刷新用)
    返回 [fn(code) for code in codes 中结果非 None 的项] (不保证顺序)。
    """
    items = [c for c in (codes or []) if c is not None]
    total = len(items)
    if total == 0:
        return []
    nw = max(1, min(int(workers or 1), 12)) if workers else 1
    out = []
    lock = threading.Lock()
    done = [0]

    if nw == 1:
        for i, c in enumerate(items):
            if stop_fn is not None and stop_fn():
                break
            try:
                if progress is not None:
                    progress(i + 1, total, c)
                r = fn(c)
            except Exception:
                r = None
            if r is not None:
                if on_result is not None:
                    try:
                        on_result(r, c)
                    except Exception:
                        pass
                out.append(r)
        return out

    def _work(c):
        try:
            return fn(c)
        except Exception:
            return None

    def _done(fut, c):
        with lock:
            done[0] += 1
            if progress is not None:
                try:
                    progress(done[0], total, c)
                except Exception:
                    pass
        try:
            r = fut.result()
        except Exception:
            r = None
        if r is not None:
            if on_result is not None:
                try:
                    on_result(r, c)
                except Exception:
                    pass
            with lock:
                out.append(r)

    fut_map = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=nw) as ex:
            for i, c in enumerate(items):
                if stop_fn is not None and stop_fn():
                    break
                fut = ex.submit(_work, c)
                fut_map[fut] = c
                fut.add_done_callback(lambda f, _c=c: _done(f, _c))
            for fut in list(fut_map.keys()):
                if stop_fn is not None and stop_fn():
                    break
                try:
                    fut.result()
                except Exception:
                    pass
    except Exception:
        for i, c in enumerate(items):
            if stop_fn is not None and stop_fn():
                break
            try:
                if progress is not None:
                    progress(i + 1, total, c)
                r = fn(c)
            except Exception:
                r = None
            if r is not None:
                out.append(r)
    return out


def analyze_light(codes_str, datalen=500, scale=240,
                  fetch_kline=None, add_indicators=None,
                  find_pivots=None, detect_all=None, judge_phase=None):
    """单只股票的"轻量"统一分析管线, 供全市场扫描 (`entries._scan_one`) 复用。

    这是全项目技术管线 (K线指标 + 枢轴 + 事件 + 阶段) 的唯一共享入口:
      scan_adv._load_df / backtest.classify_phase / screener.score_stock /
      paper.pick_candidates 此前各自手写同一段代码, 参数 (datalen/pivot order/
      scale) 稍有偏差就会静默分叉。统一收敛到这里, 保证所有扫描同口径。

    参数: 顶层函数注入 (便于测试打桩), 与 entries._scan_one 的调用约定一致。
      fetch_kline(symbol, datalen=datalen, scale=scale) -> df (含 close/day 列)
      add_indicators(df, symbol=symbol)           -> 带指标列的 df
      find_pivots(df, order=6)                     -> pivots
      detect_all(df, pivots)                       -> events
      judge_phase(df, pivots, events)              -> (phase, detail)
    仅网络相关 (板块/资金流/大盘/名称) 由本函数内部抓取 (全部 fail-soft)。

    返回 dict:
      {df, phase, events, pivots, name, sector, market_series, flow}
      - sector: {"name": <东财行业板块名>} 或 None (板块缺失时缺省)
      - flow: 主力资金流 DataFrame (day/main/...) 或 None
      - market_series: 上证指数日线 (含 price_ma20) 或 None
      任一步失败不抛异常: 返回的 dict 对应字段为 None/空, 由调用方 fail-soft。
    """
    try:
        from .utils import normalize_symbol
    except Exception:
        raise
    out = {"df": None, "phase": "", "events": None, "pivots": None,
           "name": "", "sector": None, "market_series": None, "flow": None}
    if add_indicators is None or find_pivots is None \
            or detect_all is None or judge_phase is None:
        return out
    try:
        symbol = normalize_symbol(codes_str)
    except Exception:
        return out

    # ── 技术管线 (注入) ──
    df = None
    try:
        if fetch_kline is not None:
            df = fetch_kline(symbol, datalen=datalen, scale=scale)
        else:
            from .datasource import fetch_kline as _fk
            df = _fk(symbol, datalen=datalen, scale=scale)
        if df is not None and add_indicators is not None:
            df = add_indicators(df, symbol=symbol)
    except Exception:
        df = None
    out["df"] = df
    if df is None or len(df) == 0:
        return out

    pivots = events = None
    phase = ""
    try:
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _ = judge_phase(df, pivots, events)
    except Exception:
        pass
    out["pivots"] = pivots
    out["events"] = events
    out["phase"] = phase

    # ── 网络字段 (全部 fail-soft) ──
    try:
        from .datasource import fetch_name
        out["name"] = fetch_name(symbol) or ""
    except Exception:
        out["name"] = ""
    try:
        from .fundamental import fetch_sector
        _sec = fetch_sector(symbol)
        if _sec:
            out["sector"] = {"name": _sec}
    except Exception:
        out["sector"] = None
    try:
        from .fundamental import fetch_main_flow
        out["flow"] = fetch_main_flow(symbol, 120)
    except Exception:
        out["flow"] = None
    try:
        from .market import fetch_market_series
        out["market_series"] = fetch_market_series()
    except Exception:
        out["market_series"] = None
    return out


def run_pending_eval(records, evaluator, horizons, load, save, key,
                     lock, force=False, min_interval=3600, max_records=20):
    """对缺评估周期的记录补评估 (accuracy/signal_accuracy 共用核心)。

    参数:
      records     待评估记录列表 (会被就地更新 status/results/...)
      evaluator   单条评估函数: r -> bool (是否产生新评估); 失败应抛异常
      horizons    评估周期列表 (根数), 决定何时算 done
      load/save   加载/落盘函数 (无参/单参)
      key         记录去重键函数
      lock        落盘用线程锁
    返回新增评估条数。"""
    now = time.time()
    pending = []
    for r in records:
        results = r.get("results") or {}
        if len(results) >= len(horizons):
            r["status"] = "done"
            continue
        if r.get("status") == "stale":
            continue
        if not force and now - (r.get("last_eval_ts") or 0) < min_interval:
            continue
        pending.append(r)
    # 等未来数据的 waiting 记录排在最后, 优先评估真正可出结果的记录
    pending.sort(key=lambda r: bool(r.get("waiting")))
    pending = pending[:max_records]
    n_new = 0
    for r in pending:
        try:
            if evaluator(r):
                n_new += 1
        except Exception:
            fails = int(r.get("eval_fails", 0)) + 1
            r["eval_fails"] = fails
            if fails >= 3:
                r["status"] = "stale"
        r["last_eval_ts"] = time.time()
    if pending:
        with lock:
            cur = load()
            for r in pending:
                merged = False
                for c in cur:
                    if key(c) == key(r):
                        c["results"] = r["results"]
                        c["last_eval_ts"] = r["last_eval_ts"]
                        c["status"] = r["status"]
                        c["eval_fails"] = r.get("eval_fails", 0)
                        merged = True
                        break
                if not merged:
                    cur.append(r)
            save(cur)
    return n_new
