# -*- coding: utf-8 -*-
"""SQLite 持久化缓存 (K线 / 复权因子) - msgpack 二进制序列化优化版。

内存缓存 (datasource._KLINE_CACHE / _FACTOR_CACHE) 进程退出即失效; 本模块把
行情数据落盘到 SQLite, 构成"内存 → SQLite → 网络"两级缓存:

    fetch_kline:    内存 5min → SQLite 4h → 网络
    qfq 复权因子:   内存 1d  → SQLite 7d → 网络

好处: 跨会话复用 (重启不重抓历史), 批量扫描/回测重复拉取时大幅省流量与延时。

优化:
- msgpack 二进制序列化替代 JSON (速度 5-10x, 体积 -50%)。
- schema 只在首次连接时初始化一次 (旧版每次读写都执行 DDL+写 meta)。
- 每线程复用一条长连接 (sqlite3 连接不可跨线程, 但线程内复用完全合法),
  省去每股 2 次的建连/PRAGMA 开销。
- WAL 模式下读路径无锁 (多读一写), 仅写操作串行 (_DB_WRITE_LOCK)。

db 读写失败一律静默降级为"未命中" (回退网络/内存), 并写 wx_debug.log,
缓存故障绝不阻塞行情获取主流程。
"""
import msgpack
import os
import sqlite3
import threading
import time

import numpy as np
import pandas as pd

from ._log import log_exc
from .paths import CACHE_DB

# v2: 读取端只认 msgpack; 初始化时清除 v1 遗留的 JSON 文本行 (解析必败的死数据)
_SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kline_cache (
    symbol     TEXT NOT NULL,
    scale      INTEGER NOT NULL,
    data       BLOB NOT NULL,
    source     TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (symbol, scale)
);
CREATE TABLE IF NOT EXISTS qfq_factor (
    symbol     TEXT PRIMARY KEY,
    data       BLOB NOT NULL,
    fetched_at REAL NOT NULL
);
"""

_DB_WRITE_LOCK = threading.Lock()   # 写操作串行 (WAL 单写者)
_INIT_LOCK = threading.Lock()
_INIT_DONE = False                  # schema 是否已在当前 db 上初始化
_DB_PATH = CACHE_DB

_local = threading.local()          # 每线程连接缓存 {conn, path}

_MSGPACK_OPTS = dict(use_bin_type=True, strict_types=False)


def set_db_path(path):
    """覆盖 db 文件路径 (测试用 / 可配置)。

    只影响后续打开的新连接; 各线程的旧连接在下次使用时检测到路径变化后关闭重建。
    """
    global _DB_PATH, _INIT_DONE
    _DB_PATH = path
    _INIT_DONE = False


def _new_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _conn():
    """取当前线程的长连接 (路径变化时自动重建)。"""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) != _DB_PATH:
        try:
            conn.close()
        except Exception:
            pass
        conn = None
    if conn is None:
        conn = _new_conn()
        _local.conn = conn
        _local.path = _DB_PATH
    return conn


def _ensure_init(conn):
    """schema + 版本迁移只在每个 db 文件上执行一次。"""
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        try:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != str(_SCHEMA_VERSION):
                # 迁移: 清除旧版本遗留的 JSON 文本行 (msgpack 解析必败的死数据)
                n = 0
                for table in ("kline_cache", "qfq_factor"):
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE typeof(data)='text'")
                    n += max(0, cur.rowcount)
                conn.commit()
                if n:
                    try:
                        conn.execute("VACUUM")
                    except sqlite3.OperationalError:
                        pass
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) "
                    "VALUES('schema_version', ?)", (str(_SCHEMA_VERSION),))
                conn.commit()
            _INIT_DONE = True
        except Exception as e:
            log_exc("SQLite schema 初始化失败", e)


def _df_to_msgpack(df: pd.DataFrame) -> bytes:
    """DataFrame → msgpack bytes。列按固定顺序存储, 避免键名重复。"""
    # 只存储数值列, day 单独处理为 int64 时间戳(纳秒); numpy 数组需先转 list
    # (msgpack 不支持 ndarray 直接序列化)。
    days = pd.to_datetime(df["day"]).astype("datetime64[ns]").to_numpy().view("int64")
    data = {
        "day": days.tolist(),
        "open": df["open"].to_numpy().tolist(),
        "high": df["high"].to_numpy().tolist(),
        "low": df["low"].to_numpy().tolist(),
        "close": df["close"].to_numpy().tolist(),
        "volume": df["volume"].to_numpy().tolist(),
    }
    return msgpack.packb(data, **_MSGPACK_OPTS)


def _msgpack_to_df(data: bytes) -> pd.DataFrame:
    """msgpack bytes → DataFrame。"""
    d = msgpack.unpackb(data, raw=False, strict_map_key=False)
    df = pd.DataFrame({
        "day": pd.to_datetime(np.asarray(d["day"], dtype="int64"), unit="ns"),
        "open": np.asarray(d["open"]),
        "high": np.asarray(d["high"]),
        "low": np.asarray(d["low"]),
        "close": np.asarray(d["close"]),
        "volume": np.asarray(d["volume"]),
    })
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    return df.sort_values("day").reset_index(drop=True)


def _fac_to_msgpack(fac: pd.DataFrame) -> bytes:
    days = pd.to_datetime(fac["d"]).astype("datetime64[ns]").to_numpy().view("int64")
    data = {"d": days.tolist(), "f": fac["f"].to_numpy().tolist()}
    return msgpack.packb(data, **_MSGPACK_OPTS)


def _msgpack_to_fac(data: bytes) -> pd.DataFrame:
    d = msgpack.unpackb(data, raw=False, strict_map_key=False)
    fac = pd.DataFrame({"d": pd.to_datetime(np.asarray(d["d"], dtype="int64"), unit="ns"),
                        "f": np.asarray(d["f"])})
    fac["f"] = pd.to_numeric(fac["f"])
    return fac.sort_values("d").reset_index(drop=True)


def kline_load(symbol, scale, max_age):
    """读 SQLite K线缓存。命中且未过期返回 (df, source), 否则 None。失败静默降级。"""
    try:
        conn = _conn()
        _ensure_init(conn)
        row = conn.execute(
            "SELECT data, source, fetched_at FROM kline_cache "
            "WHERE symbol = ? AND scale = ?",
            (symbol, int(scale))).fetchone()
    except Exception as e:
        log_exc("SQLite kline_load 失败", e)
        return None
    if not row:
        return None
    data, source, fetched_at = row
    if time.time() - fetched_at > max_age:
        return None
    try:
        return _msgpack_to_df(data), source
    except Exception as e:
        log_exc("SQLite kline_load 解析失败", e)
        return None


def kline_save(symbol, scale, df, source):
    """写 SQLite K线缓存 (INSERT OR REPLACE)。失败静默降级。"""
    if df is None or df.empty:
        return
    try:
        blob = _df_to_msgpack(df)
    except Exception as e:
        log_exc("SQLite kline_save 序列化失败", e)
        return
    with _DB_WRITE_LOCK:
        try:
            conn = _conn()
            _ensure_init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO kline_cache "
                "(symbol, scale, data, source, fetched_at) VALUES (?, ?, ?, ?, ?)",
                (symbol, int(scale), blob, source, time.time()))
            conn.commit()
        except Exception as e:
            log_exc("SQLite kline_save 失败", e)


def qfq_load(symbol, max_age):
    """读 SQLite 复权因子缓存。命中且未过期返回 DataFrame, 否则 None。失败静默降级。"""
    try:
        conn = _conn()
        _ensure_init(conn)
        row = conn.execute(
            "SELECT data, fetched_at FROM qfq_factor WHERE symbol = ?",
            (symbol,)).fetchone()
    except Exception as e:
        log_exc("SQLite qfq_load 失败", e)
        return None
    if not row:
        return None
    data, fetched_at = row
    if time.time() - fetched_at > max_age:
        return None
    try:
        return _msgpack_to_fac(data)
    except Exception as e:
        log_exc("SQLite qfq_load 解析失败", e)
        return None


def qfq_save(symbol, fac):
    """写 SQLite 复权因子缓存 (INSERT OR REPLACE)。失败静默降级。"""
    if fac is None or fac.empty:
        return
    try:
        blob = _fac_to_msgpack(fac)
    except Exception as e:
        log_exc("SQLite qfq_save 序列化失败", e)
        return
    with _DB_WRITE_LOCK:
        try:
            conn = _conn()
            _ensure_init(conn)
            conn.execute(
                "INSERT OR REPLACE INTO qfq_factor (symbol, data, fetched_at) "
                "VALUES (?, ?, ?)",
                (symbol, blob, time.time()))
            conn.commit()
        except Exception as e:
            log_exc("SQLite qfq_save 失败", e)


def clear_cache():
    """清空全部 SQLite 缓存 (K线 + 复权因子)。失败静默。"""
    with _DB_WRITE_LOCK:
        try:
            conn = _conn()
            _ensure_init(conn)
            conn.execute("DELETE FROM kline_cache")
            conn.execute("DELETE FROM qfq_factor")
            conn.commit()
            conn.execute("VACUUM")
        except Exception as e:
            log_exc("SQLite clear_cache 失败", e)


def cache_stats():
    """返回 {kline_rows, qfq_rows, db_bytes} 供界面显示。失败返回全 0。"""
    try:
        conn = _conn()
        _ensure_init(conn)
        k = conn.execute("SELECT COUNT(*) FROM kline_cache").fetchone()[0]
        q = conn.execute("SELECT COUNT(*) FROM qfq_factor").fetchone()[0]
        size = os.path.getsize(_DB_PATH) if os.path.exists(_DB_PATH) else 0
        return {"kline_rows": k, "qfq_rows": q, "db_bytes": size}
    except Exception as e:
        log_exc("SQLite cache_stats 失败", e)
        return {"kline_rows": 0, "qfq_rows": 0, "db_bytes": 0}
