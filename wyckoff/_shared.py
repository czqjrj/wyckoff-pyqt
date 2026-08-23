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
