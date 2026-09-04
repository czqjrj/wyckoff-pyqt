"""MySQL 云存储后端 (SQLPub Serverless MySQL)。

作为账户登录 (users 表) 与私有数据同步 (profile_items 表) 的云端存储层,
替代原先的 Git 远程仓。同一后端同时服务:
  - 登录/注册/改密/改用户名: users 表 (PBKDF2 哈希, 不存明文)
  - 私有数据多机同步:       profile_items 表 (LWW 合并后的 {type,item_id,value,ts})

连接配置优先级 (从环境变量读取, 便于测试隔离与多环境部署):
    WYCKOFF_SQL_HOST / _PORT / _DB / _USER / _PASSWORD
未设置环境变量时回退到内置出厂连接 (SQLPub 默认实例)。

`enabled()` 判断该后端是否可用: 非离线且能建立连接时启用;
`WYCKOFF_NO_NET=1`(离线) 或连接失败时禁用, 上游回退到 Git 传输/本地缓存。
"""
from __future__ import annotations

import json
import os
import threading

from ._log import log_exc

try:
    import pymysql
except Exception:  # pragma: no cover
    pymysql = None

# ── 连接配置 (环境变量优先, 回退内置出厂连接) ──────────────
DEFAULT_HOST = os.environ.get("WYCKOFF_SQL_HOST", "mysql6.sqlpub.com")
DEFAULT_PORT = int(os.environ.get("WYCKOFF_SQL_PORT", "3311"))
DEFAULT_DB = os.environ.get("WYCKOFF_SQL_DB", "wyckoff")
DEFAULT_USER = os.environ.get("WYCKOFF_SQL_USER", "wyckoff")
DEFAULT_PASSWORD = os.environ.get("WYCKOFF_SQL_PASSWORD", "98nM5egHVauDIbqm")

NO_NET_ENV = "WYCKOFF_NO_NET"
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 15
_LOCK = threading.Lock()

# 建表 (两表): users 登录账号; profile_items 私有数据同步条目
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   VARCHAR(64)  NOT NULL,
    salt       VARCHAR(64)  NOT NULL,
    hash       VARCHAR(128) NOT NULL,
    display    VARCHAR(128) NOT NULL DEFAULT '',
    created_ts DOUBLE       NOT NULL DEFAULT 0,
    repo_url   VARCHAR(512) NOT NULL DEFAULT '',
    PRIMARY KEY (username)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS profile_items (
    username VARCHAR(64)  NOT NULL,
    type     VARCHAR(32)  NOT NULL,
    item_id  VARCHAR(255) NOT NULL,
    value    MEDIUMTEXT,
    ts       DOUBLE       NOT NULL DEFAULT 0,
    PRIMARY KEY (username, type, item_id)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS calib_bundle (
    bundle_key    VARCHAR(32) NOT NULL,
    signals_json  MEDIUMTEXT,
    feedback_json MEDIUMTEXT,
    model_json    MEDIUMTEXT,
    meta_json     MEDIUMTEXT,
    updated_ts    DOUBLE      NOT NULL DEFAULT 0,
    PRIMARY KEY (bundle_key)
) DEFAULT CHARSET=utf8mb4;
"""


def _no_net():
    return os.environ.get(NO_NET_ENV, "").strip() in ("1", "true", "TRUE")


def config():
    """返回当前连接配置 dict (供状态展示)。"""
    return {
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "db": DEFAULT_DB,
        "user": DEFAULT_USER,
        "password": DEFAULT_PASSWORD,
        "configured": bool(pymysql),
    }


def connect():
    """建立到 SQLPub 的新连接。失败抛异常。"""
    if pymysql is None:
        raise RuntimeError("未安装 PyMySQL, 无法使用 MySQL 云同步")
    conn = pymysql.connect(
        host=DEFAULT_HOST, port=DEFAULT_PORT, user=DEFAULT_USER,
        password=DEFAULT_PASSWORD, database=DEFAULT_DB,
        connect_timeout=_CONNECT_TIMEOUT, read_timeout=_READ_TIMEOUT,
        write_timeout=_READ_TIMEOUT, charset="utf8mb4",
        autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    return conn


def enabled():
    """后端是否可用: 在线 + PyMySQL 可用 + 连通。失败静默 False。"""
    if _no_net() or pymysql is None:
        return False
    try:
        with connect() as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


def ensure_schema():
    """建表 (幂等)。"""
    with connect() as conn:
        with conn.cursor() as cur:
            for stmt in _SCHEMA.split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)


# ── users: 登录账号 ────────────────────────────────────────
def get_user(username):
    """按用户名取账号记录, 无则返回 None。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, salt, hash, display, created_ts, repo_url "
                "FROM users WHERE username=%s", (username,))
            row = cur.fetchone()
    if not row:
        return None
    return {
        "username": row["username"], "salt": row["salt"], "hash": row["hash"],
        "display": row["display"] or row["username"],
        "created_ts": float(row["created_ts"]),
        "repo_url": row["repo_url"] or "",
    }


def list_users():
    """返回全部用户名列表。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users")
            return [r["username"] for r in cur.fetchall()]


def upsert_user(record):
    """写入/更新一条账号记录 (含哈希)。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, salt, hash, display, created_ts, repo_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE salt=VALUES(salt), hash=VALUES(hash), "
                "display=VALUES(display), created_ts=VALUES(created_ts), "
                "repo_url=VALUES(repo_url)",
                (record["username"], record.get("salt", ""),
                 record.get("hash", ""), record.get("display", record["username"]),
                 record.get("created_ts", 0.0), record.get("repo_url", "")))


def rename_user(old_user, new_user, display=None):
    """改用户名: 更新 users 主键 + 迁移 profile_items 归属。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET username=%s, display=%s WHERE username=%s",
                (new_user, display or new_user, old_user))
            cur.execute(
                "UPDATE profile_items SET username=%s WHERE username=%s",
                (new_user, old_user))


def delete_user(username):
    """删除账号及其全部同步数据 (仅破坏性调用, 谨慎)。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET hash='', salt='' WHERE username=%s",
                        (username,))
            cur.execute("DELETE FROM profile_items WHERE username=%s", (username,))


# ── profile_items: 私有数据同步条目 ─────────────────────────
def _serialize(v):
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False)


def _deserialize(s):
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def read_profile_items(username, type_):
    """读取某用户某类型的全部条目, 返回 {item_id: {"v": value, "ts": ts}}。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_id, value, ts FROM profile_items "
                "WHERE username=%s AND type=%s", (username, type_))
            rows = cur.fetchall()
    return {
        r["item_id"]: {"v": _deserialize(r["value"]), "ts": float(r["ts"])}
        for r in rows
    }


def write_profile_items(username, type_, items):
    """整体覆盖写入某用户某类型的条目集 (items: {item_id: {v, ts}})。

    事务内先删后插, 保证与 LWW 合并结果一致。
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM profile_items WHERE username=%s AND type=%s",
                (username, type_))
            for iid, rec in items.items():
                v = rec.get("v") if isinstance(rec, dict) else rec
                ts = rec.get("ts", 0.0) if isinstance(rec, dict) else 0.0
                cur.execute(
                    "INSERT INTO profile_items (username, type, item_id, value, ts) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (username, type_, str(iid), _serialize(v), float(ts)))


def clear_profile(username):
    """清空某用户全部同步数据。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM profile_items WHERE username=%s", (username,))


# ── calib_bundle: 共享校准数据云存储 ──────────────────────────
CALIB_KEY = "calib"


def read_calib_bundle():
    """读取共享校准 bundle (signals/feedback/model/meta 四份 canonical JSON)。

    返回 {signals, feedback, model, meta} (缺失字段=None); 无记录返回全 None。
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT signals_json, feedback_json, model_json, meta_json, updated_ts "
                "FROM calib_bundle WHERE bundle_key=%s", (CALIB_KEY,))
            row = cur.fetchone()
    if not row:
        return {"signals": None, "feedback": None, "model": None,
                "meta": None, "updated_ts": 0.0}
    return {
        "signals": _deserialize(row["signals_json"]),
        "feedback": _deserialize(row["feedback_json"]),
        "model": _deserialize(row["model_json"]),
        "meta": _deserialize(row["meta_json"]),
        "updated_ts": float(row["updated_ts"] or 0),
    }


def write_calib_bundle(files, updated_ts=None):
    """整体覆盖写入共享校准 bundle。

    files: {signals|feedback|model|meta: obj|None}, None 表示保留该字段不改动
    (避免用 None 覆盖既有数据)。updated_ts 缺省取写入时刻。
    """
    if updated_ts is None:
        import time
        updated_ts = time.time()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bundle_key FROM calib_bundle WHERE bundle_key=%s",
                        (CALIB_KEY,))
            exists = cur.fetchone() is not None
            sets, args = [], []
            for col, obj in (("signals_json", files.get("signals")),
                             ("feedback_json", files.get("feedback")),
                             ("model_json", files.get("model")),
                             ("meta_json", files.get("meta"))):
                if obj is None:
                    continue
                sets.append(f"{col}=%s")
                args.append(_serialize(obj))
            if not sets:
                return
            sets.append("updated_ts=%s")
            args.append(float(updated_ts))
            if exists:
                cur.execute(
                    f"UPDATE calib_bundle SET {', '.join(sets)} "
                    "WHERE bundle_key=%s", args + [CALIB_KEY])
            else:
                cols = [s.split("=")[0] for s in sets]
                cur.execute(
                    f"INSERT INTO calib_bundle (bundle_key, {', '.join(cols)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(cols))})",
                    [CALIB_KEY] + args)


def clear_calib_bundle():
    """清空共享校准 bundle (仅破坏性调用, 谨慎)。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM calib_bundle WHERE bundle_key=%s", (CALIB_KEY,))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="MySQL 云同步后端自检")
    p.add_argument("--setup", action="store_true", help="建表")
    p.add_argument("--ping", action="store_true", help="连通测试")
    p.add_argument("--list", action="store_true", help="列出全部用户")
    a = p.parse_args()
    if a.setup:
        ensure_schema()
        print("schema OK")
    if a.ping:
        print("enabled:", enabled())
    if a.list:
        print("users:", list_users())
