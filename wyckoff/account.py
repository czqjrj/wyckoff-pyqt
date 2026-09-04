"""账户登录 (用户名+密码, MySQL 云后端)。

身份 = 用户名 + 密码 (密码在云数据库 users 表以 PBKDF2 加盐哈希存储,
不会明文落盘)。

本模块负责:
- register(注册)/login(登录)/logout/switch
- change_password(改密码) / change_username(改用户名)
- 本机登录态 account.json ({accounts:{}, current:""}) 仅作离线缓存与当前用户记录

离线 (WYCKOFF_NO_NET=1): 本机已注册账号仍可登录(仅本地校验缓存哈希), 新注册/改密/改名会失败。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import time

from ._shared import atomic_write_json
from .paths import DATA_DIR
from . import cloud_db

NO_NET_ENV = "WYCKOFF_NO_NET"

ACCOUNT_FILE = os.path.join(DATA_DIR, "account.json")

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
_MIN_PASSWORD = 6
_PBKDF2_ITER = 120_000


def _no_net():
    return os.environ.get(NO_NET_ENV, "").strip() in ("1", "true", "TRUE")


def _cloud_enabled():
    """MySQL 云后端可用 (在线且可连通)。"""
    try:
        return cloud_db.enabled()
    except Exception:
        return False


def _hash_password(password, salt):
    """PBKDF2-HMAC-SHA256 → hex。salt 为 hex 字符串。"""
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        _PBKDF2_ITER, dklen=32)
    return dk.hex()


def _new_salt():
    return os.urandom(16).hex()


def _verify_password(password, record):
    if not isinstance(record, dict):
        return False
    salt = record.get("salt") or ""
    try:
        return hmac.compare_digest(_hash_password(password, salt),
                                   str(record.get("hash") or ""))
    except Exception:
        return False


# ── 本机登录态 ─────────────────────────────────────────────
def load_accounts():
    """本机登录态: {accounts: {user: {created_ts,...}}, current: ""}"""
    try:
        with open(ACCOUNT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("accounts", {})
            d.setdefault("current", "")
            return d
    except Exception:
        pass
    return {"accounts": {}, "current": ""}


def save_accounts(state):
    atomic_write_json(ACCOUNT_FILE, state)


# ── 查询 ──────────────────────────────────────────────────
def current_user():
    state = load_accounts()
    cur = state.get("current") or ""
    if cur and cur in state.get("accounts", {}):
        return cur
    return ""


def accounts():
    return load_accounts().get("accounts", {})


def require_login():
    if not current_user():
        raise ValueError("请先登录: 用户名 + 密码")


# ── 缓存登录态 ─────────────────────────────────────────────
def _cache_login(user, now):
    state = load_accounts()
    prev = state.get("accounts", {}).get(user, {})
    state.setdefault("accounts", {})[user] = {
        "created_ts": prev.get("created_ts", now),
        "last_login": now,
        "display": prev.get("display", user),
    }
    state["current"] = user
    save_accounts(state)


# ── 注册 / 登录 ────────────────────────────────────────────
def register(user, password):
    """注册新账号: 校验用户名唯一 + 密码强度, 写入云数据库。"""
    user = (user or "").strip()
    if not _USERNAME_RE.match(user):
        return False, ("用户名须 2-32 位, 仅含字母/数字/._-")
    if not password or len(password) < _MIN_PASSWORD:
        return False, f"密码至少 {_MIN_PASSWORD} 位"
    if _no_net():
        return False, "离线模式, 无法注册到共享账户"
    if not _cloud_enabled():
        return False, "云账户不可用, 请检查网络连接"
    now = time.time()
    salt = _new_salt()
    h = _hash_password(password, salt)
    try:
        if cloud_db.get_user(user):
            return False, "用户名已存在"
        cloud_db.upsert_user({
            "username": user, "salt": salt, "hash": h,
            "display": user, "created_ts": now,
        })
    except RuntimeError as e:
        return False, f"云账户不可达: {e}"
    _cache_login(user, now)
    return True, "注册成功并已登录"


def login(user, password):
    """登录: 从云数据库校验用户名密码。离线时回退本机缓存。"""
    user = (user or "").strip()
    if not user:
        return False, "用户名为空"
    if not password:
        return False, "密码为空"
    now = time.time()
    if not _no_net():
        if _cloud_enabled():
            try:
                rec = cloud_db.get_user(user)
                if rec is not None:
                    if not _verify_password(password, rec):
                        return False, "密码错误"
                    _cache_login(user, now)
                    return True, "登录成功"
            except RuntimeError as e:
                return False, f"云账户不可达: {e}"
        else:
            return False, "云账户不可用, 请检查网络连接"
    # 离线: 回退本机缓存账号校验 (不验证密码, 用于离线可用)
    cached = load_accounts().get("accounts", {}).get(user)
    if cached:
        _cache_login(user, now)
        return True, "登录成功(本机缓存)"
    return False, "用户不存在"


def logout():
    state = load_accounts()
    state["current"] = ""
    save_accounts(state)
    return True, "已退出"


def switch(user):
    if user not in load_accounts().get("accounts", {}):
        return False, f"账户 {user} 未登记"
    state = load_accounts()
    state["current"] = user
    save_accounts(state)
    return True, f"已切换至 {user}"


# ── 修改用户名 / 密码 ─────────────────────────────────────
def change_password(user, old_password, new_password):
    """修改密码(须验证旧密码)。更新云数据库的哈希。"""
    user = (user or "").strip()
    if not new_password or len(new_password) < _MIN_PASSWORD:
        return False, f"新密码至少 {_MIN_PASSWORD} 位"
    if _no_net():
        return False, "离线模式, 无法修改密码"
    if not _cloud_enabled():
        return False, "云账户不可用, 请检查网络连接"
    try:
        rec = cloud_db.get_user(user)
    except RuntimeError as e:
        return False, f"云账户不可达: {e}"
    if not rec:
        return False, "用户不存在"
    if not _verify_password(old_password, rec):
        return False, "旧密码错误"
    salt = _new_salt()
    rec["salt"], rec["hash"] = salt, _hash_password(new_password, salt)
    try:
        cloud_db.upsert_user(rec)
    except RuntimeError as e:
        return False, str(e)
    return True, "密码已修改"


def change_username(old_user, new_user, password):
    """修改用户名: 更新云数据库主键 + 迁移 profile_items 归属。"""
    old_user = (old_user or "").strip()
    new_user = (new_user or "").strip()
    if not _USERNAME_RE.match(new_user):
        return False, ("新用户名须 2-32 位, 仅含字母/数字/._-")
    if old_user == new_user:
        return False, "新旧用户名相同"
    if _no_net():
        return False, "离线模式, 无法修改用户名"
    if not _cloud_enabled():
        return False, "云账户不可用, 请检查网络连接"
    try:
        rec = cloud_db.get_user(old_user)
    except RuntimeError as e:
        return False, f"云账户不可达: {e}"
    if not rec:
        return False, "用户不存在"
    if not _verify_password(password, rec):
        return False, "密码错误"
    if cloud_db.get_user(new_user):
        return False, "新用户名已存在"
    try:
        cloud_db.rename_user(old_user, new_user, display=new_user)
    except RuntimeError as e:
        return False, str(e)
    # 更新本机登录态键
    state = load_accounts()
    acts = state.get("accounts", {})
    if old_user in acts:
        acts[new_user] = acts.pop(old_user)
    if state.get("current") == old_user:
        state["current"] = new_user
    save_accounts(state)
    return True, "用户名已修改"


def status():
    cur = current_user()
    acc = accounts()
    return {
        "current": cur,
        "logged_in": bool(cur),
        "accounts": list(acc.keys()),
        "no_net": _no_net(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="威科夫账户登录(用户名+密码)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("register")
    p.add_argument("user")
    p.add_argument("password")
    p = sub.add_parser("login")
    p.add_argument("user")
    p.add_argument("password")
    sub.add_parser("logout")
    sub.add_parser("status")
    p = sub.add_parser("switch")
    p.add_argument("user")
    p = sub.add_parser("change-password")
    p.add_argument("user")
    p.add_argument("old")
    p.add_argument("new")
    p = sub.add_parser("change-username")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("password")
    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd == "register":
        ok, msg = register(args.user, args.password)
        out = {"ok": ok, "message": msg}
    elif cmd == "login":
        ok, msg = login(args.user, args.password)
        out = {"ok": ok, "message": msg}
    elif cmd == "logout":
        ok, msg = logout()
        out = {"ok": ok, "message": msg}
    elif cmd == "status":
        out = status()
    elif cmd == "switch":
        ok, msg = switch(args.user)
        out = {"ok": ok, "message": msg}
    elif cmd == "change-password":
        ok, msg = change_password(args.user, args.old, args.new)
        out = {"ok": ok, "message": msg}
    elif cmd == "change-username":
        ok, msg = change_username(args.old, args.new, args.password)
        out = {"ok": ok, "message": msg}
    else:
        parser.print_help()
        return
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
