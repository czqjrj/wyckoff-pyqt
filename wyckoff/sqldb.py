# -*- coding: utf-8 -*-
"""SQLite 持久化缓存 (K线 / 复权因子)。

内存缓存 (datasource._KLINE_CACHE / _FACTOR_CACHE) 进程退出即失效; 本模块把
行情数据落盘到 SQLite, 构成"内存 → SQLite → 网络"两级缓存:

    fetch_kline:    内存 5min → SQLite 4h → 网络
    qfq 复权因子:   内存 1d  → SQLite 7d → 网络

好处: 跨会话复用 (重启不重抓历史), 批量扫描/回测重复拉取时大幅省流量与延时。

线程安全: 单锁 + 每次操作独立连接 (sqlite3 默认不允许跨线程共享连接),
WAL 模式分离读写锁, 定时刷新/扫描/回测多线程并发也不互相阻塞。
K线/因子以 JSON 文本存储 (量级小, 便于排查与跨版本迁移)。

db 读写失败一律静默降级为"未命中" (回退网络/内存), 并写 wx_debug.log,
缓存故障绝不阻塞行情获取主流程。
"""
import json
import os
import sqlite3
import threading
import time

import pandas as pd

from ._log import log_exc
from .paths import CACHE_DB

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kline_cache (
    symbol     TEXT NOT NULL,
    scale      INTEGER NOT NULL,
    data       TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (symbol, scale)
);
CREATE TABLE IF NOT EXISTS qfq_factor (
    symbol     TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""

_DB_LOCK = threading.Lock()
_DB_PATH = CACHE_DB


def set_db_path(path):
    """覆盖 db 文件路径 (测试用 / 可配置)。线程安全: 只影响后续打开的新连接。"""
    global _DB_PATH
    _DB_PATH = path


def _connect():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),))
        conn.commit()
    except sqlite3.Error:
        pass
    return conn


# ── 序列化: DataFrame ↔ JSON 行列表 ──
def _kline_to_rows(df):
    days = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
    return [
        {"day": days[i],
         "open": float(df["open"].iloc[i]),
         "high": float(df["high"].iloc[i]),
         "low": float(df["low"].iloc[i]),
         "close": float(df["close"].iloc[i]),
         "volume": float(df["volume"].iloc[i])}
        for i in range(len(df))
    ]


def _rows_to_kline(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["day"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    return df.sort_values("day").reset_index(drop=True)


def _qfq_to_rows(fac):
    days = pd.to_datetime(fac["d"]).dt.strftime("%Y-%m-%d").tolist()
    return [{"d": days[i], "f": float(fac["f"].iloc[i])} for i in range(len(fac))]


def _rows_to_qfq(rows):
    fac = pd.DataFrame(rows)
    if fac.empty:
        return fac
    fac["d"] = pd.to_datetime(fac["d"])
    fac["f"] = pd.to_numeric(fac["f"])
    return fac.sort_values("d").reset_index(drop=True)


# ── K线 ──
def kline_load(symbol, scale, max_age):
    """读 SQLite K线缓存。命中且未过期返回 (df, source), 否则 None。失败静默降级。"""
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT data, source, fetched_at FROM kline_cache "
                    "WHERE symbol = ? AND scale = ?",
                    (symbol, int(scale))).fetchone()
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite kline_load 失败", e)
            return None
    if not row:
        return None
    data, source, fetched_at = row
    if time.time() - fetched_at > max_age:
        return None
    try:
        return _rows_to_kline(json.loads(data)), source
    except Exception as e:
        log_exc("SQLite kline_load 解析失败", e)
        return None


def kline_save(symbol, scale, df, source):
    """写 SQLite K线缓存 (INSERT OR REPLACE)。失败静默降级。"""
    if df is None or df.empty:
        return
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO kline_cache "
                    "(symbol, scale, data, source, fetched_at) VALUES (?, ?, ?, ?, ?)",
                    (symbol, int(scale), json.dumps(_kline_to_rows(df), ensure_ascii=False),
                     source, time.time()))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite kline_save 失败", e)


# ── 复权因子 ──
def qfq_load(symbol, max_age):
    """读 SQLite 复权因子缓存。命中且未过期返回 DataFrame, 否则 None。失败静默降级。"""
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT data, fetched_at FROM qfq_factor WHERE symbol = ?",
                    (symbol,)).fetchone()
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite qfq_load 失败", e)
            return None
    if not row:
        return None
    data, fetched_at = row
    if time.time() - fetched_at > max_age:
        return None
    try:
        return _rows_to_qfq(json.loads(data))
    except Exception as e:
        log_exc("SQLite qfq_load 解析失败", e)
        return None


def qfq_save(symbol, fac):
    """写 SQLite 复权因子缓存 (INSERT OR REPLACE)。失败静默降级。"""
    if fac is None or fac.empty:
        return
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO qfq_factor (symbol, data, fetched_at) "
                    "VALUES (?, ?, ?)",
                    (symbol, json.dumps(_qfq_to_rows(fac), ensure_ascii=False), time.time()))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite qfq_save 失败", e)


# ── 维护 ──
def clear_cache():
    """清空全部 SQLite 缓存 (K线 + 复权因子)。失败静默。"""
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                conn.execute("DELETE FROM kline_cache")
                conn.execute("DELETE FROM qfq_factor")
                conn.commit()
                conn.execute("VACUUM")
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite clear_cache 失败", e)


def cache_stats():
    """返回 {kline_rows, qfq_rows, db_bytes} 供界面显示。失败返回全 0。"""
    with _DB_LOCK:
        try:
            conn = _connect()
            try:
                k = conn.execute("SELECT COUNT(*) FROM kline_cache").fetchone()[0]
                q = conn.execute("SELECT COUNT(*) FROM qfq_factor").fetchone()[0]
                size = os.path.getsize(_DB_PATH) if os.path.exists(_DB_PATH) else 0
                return {"kline_rows": k, "qfq_rows": q, "db_bytes": size}
            finally:
                conn.close()
        except Exception as e:
            log_exc("SQLite cache_stats 失败", e)
            return {"kline_rows": 0, "qfq_rows": 0, "db_bytes": 0}
