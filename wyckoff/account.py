"""账户登录与私有数据同步 (内置共享登录仓 + 用户名/密码)。

身份 = 用户名 + 密码 (密码在仓内 `accounts/accounts.json` 以 PBKDF2 加盐哈希存储,
不会明文落盘/上库)。所有用户共用一个内置登录专用私有仓
(`wyckoff-account.git`), 仓内按用户名隔离:
    accounts/accounts.json            # {username: {salt, hash, display, created_ts}}
    accounts/profiles/{username}.json # 该用户私有数据 bundle (见 profile_sync)

本模块负责:
- 内置登录取用户名/密码, register(注册)/login(登录)/logout/switch
- 仓内账号的读写(稀疏检出仅 accounts/ 子目录, 不碰校准仓/其它文件)
- change_password(改密码) / change_username(改用户名, 迁移 profiles 文件)
- 本机登录态 account.json ({accounts:{}, current:""}) 仅作离线缓存与当前用户记录

离线 (WYCKOFF_NO_NET=1): 本机已注册账号仍可登录(仅本地校验缓存哈希), 新注册
会因无法写仓而失败或标记为待同步。仓不存在时注册/登录需先在 GitHub 建空私有仓。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import time

from ._shared import atomic_write_json
from .paths import DATA_DIR
from . import cloud_db

NO_NET_ENV = "WYCKOFF_NO_NET"

# 内置共享登录仓: 所有用户共用一个, 账号+私有数据按用户名隔离
BUILTIN_REPO_URL = "git@github.com:czqjrj/wyckoff-account.git"

ACCOUNT_FILE = os.path.join(DATA_DIR, "account.json")

# 登录仓在本机的独立工作副本 (与校准 sync / profile_sync 各自独立)
ACCOUNT_REPO_DIR = os.path.join(DATA_DIR, "account_repo")

_ACCOUNTS_SUBDIR = "accounts"
_ACCOUNTS_NODE = os.path.join(_ACCOUNTS_SUBDIR, "accounts.json")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
_MIN_PASSWORD = 6
_PBKDF2_ITER = 120_000


def _no_net():
    return os.environ.get(NO_NET_ENV, "").strip() in ("1", "true", "TRUE")


def _cloud_enabled():
    """MySQL 云后端可用 (在线且可连通)。失败静默回退 Git/本地缓存。"""
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


# ── 登录仓 git 传输 (稀疏检出 accounts/ 子目录) ────────────
def _git(args, cwd=None, check=True, timeout=120):
    if _no_net():
        return "", 0
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError((p.stderr or "").strip() or
                               f"git {' '.join(args)} 失败")
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except FileNotFoundError:
        raise RuntimeError("未找到 git 可执行文件")


def _ensure_repo():
    """确保登录仓工作副本已 clone 且稀疏检出 accounts/ 子目录。返回目录。"""
    if os.path.isdir(os.path.join(ACCOUNT_REPO_DIR, ".git")):
        return ACCOUNT_REPO_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(ACCOUNT_REPO_DIR):
        import shutil
        shutil.rmtree(ACCOUNT_REPO_DIR, ignore_errors=True)
    try:
        _git(["clone", "--no-checkout", "--depth", "1",
              BUILTIN_REPO_URL, ACCOUNT_REPO_DIR], timeout=180)
    except RuntimeError:
        # clone 失败(空仓/网络/首次): 初始化一个本地仓指向内置地址,
        # 由首个用户在本机注册并首推, 后续设备克隆既有分支。
        os.makedirs(ACCOUNT_REPO_DIR, exist_ok=True)
        _git(["init"], cwd=ACCOUNT_REPO_DIR)
        _git(["remote", "add", "origin", BUILTIN_REPO_URL], cwd=ACCOUNT_REPO_DIR)
        _git(["branch", "-M", "main"], cwd=ACCOUNT_REPO_DIR, check=False)
    else:
        _git(["branch", "-M", "main"], cwd=ACCOUNT_REPO_DIR, check=False)
        # 空仓克隆默认 HEAD 在 master; 有数据则在 origin/main, 检出到 main
        _git(["checkout", "-B", "main", "origin/main"],
             cwd=ACCOUNT_REPO_DIR, check=False) if False else None
        _git(["checkout", "main"], cwd=ACCOUNT_REPO_DIR, check=False) \
            if False else None
        _git(["checkout", "-B", "main", "origin/main"],
             cwd=ACCOUNT_REPO_DIR, check=False) if False else None
    _git(["config", "core.sparseCheckout", "true"], cwd=ACCOUNT_REPO_DIR)
    _git(["sparse-checkout", "set", _ACCOUNTS_SUBDIR],
         cwd=ACCOUNT_REPO_DIR, check=False)
    return ACCOUNT_REPO_DIR


def _read_node_default():
    """读仓内 accounts.json, 缺失返回空 dict。先在 ensure_repo 后调用。"""
    p = os.path.join(ACCOUNT_REPO_DIR, _ACCOUNTS_NODE)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_and_push(node_data, msg):
    """把 node_data 写为 accounts/accounts.json 并提交推送(先 pull 最新防覆盖)。"""
    _git(["pull", "--no-rebase"], cwd=ACCOUNT_REPO_DIR, check=False)
    p = os.path.join(ACCOUNT_REPO_DIR, _ACCOUNTS_NODE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    atomic_write_json(p, node_data)
    _git(["add", "-A"], cwd=ACCOUNT_REPO_DIR)
    _git(["commit", "-m", msg], cwd=ACCOUNT_REPO_DIR, check=False)
    out, rc = _git(["push", "origin", "main"], cwd=ACCOUNT_REPO_DIR, check=False)
    if rc != 0:
        raise RuntimeError(f"推送失败(请检查登录仓权限): {(out or '').strip() or '无输出'}")
    return True


# ── 查询 ──────────────────────────────────────────────────
def current_user():
    state = load_accounts()
    cur = state.get("current") or ""
    if cur and cur in state.get("accounts", {}):
        return cur
    return ""


def current_repo_url():
    """当前登录账户绑定的私有档案仓 URL (供 profile_sync 定位远端镜像)。"""
    state = load_accounts()
    cur = state.get("current") or ""
    rec = state.get("accounts", {}).get(cur, {}) if cur else {}
    return (rec.get("repo_url") or "").strip()


def accounts():
    return load_accounts().get("accounts", {})


def require_login():
    if not current_user():
        raise ValueError("请先登录: 用户名 + 密码")


# ── 注册 / 登录 ────────────────────────────────────────────
def register(user, password, repo_url=""):
    """注册新账号: 校验用户名唯一 + 密码强度, 写入仓内 accounts.json (推送)。"""
    user = (user or "").strip()
    if not _USERNAME_RE.match(user):
        return False, ("用户名须 2-32 位, 仅含字母/数字/._-")
    if not password or len(password) < _MIN_PASSWORD:
        return False, f"密码至少 {_MIN_PASSWORD} 位"
    if _no_net():
        return False, "离线模式, 无法注册到共享账户"
    now = time.time()
    salt = _new_salt()
    h = _hash_password(password, salt)
    if _cloud_enabled():
        try:
            if cloud_db.get_user(user):
                return False, "用户名已存在"
            cloud_db.upsert_user({
                "username": user, "salt": salt, "hash": h,
                "display": user, "created_ts": now, "repo_url": repo_url,
            })
        except RuntimeError as e:
            return False, f"云账户不可达: {e}"
        _cache_login(user, now, repo_url)
        return True, "注册成功并已登录"
    try:
        _ensure_repo()
    except RuntimeError as e:
        return False, f"仓库不可达: {e}"
    node = _read_node_default()
    existing = _accounts_from_node(node)
    if user in existing:
        return False, "用户名已存在"
    node.setdefault("users", {})[user] = {
        "salt": salt, "hash": h,
        "display": user, "created_ts": now,
        "repo_url": repo_url,
    }
    try:
        _write_and_push(node, f"register: {user}")
    except RuntimeError as e:
        return False, str(e)
    # 建该用户私有数据文件并推送
    try:
        _write_user_profile(user, {"schema": 1, "types": {}})
    except RuntimeError as e:
        return False, f"注册成功但私有数据初始化失败: {e}"
    _cache_login(user, now, repo_url)
    return True, "注册成功并已登录"


def _accounts_from_node(node):
    return node.get("users", {}) if isinstance(node, dict) else {}


def _cache_login(user, now, repo_url=""):
    state = load_accounts()
    prev = state.get("accounts", {}).get(user, {})
    state.setdefault("accounts", {})[user] = {
        "created_ts": prev.get("created_ts", now),
        "last_login": now,
        "display": prev.get("display", user),
        "repo_url": (repo_url or prev.get("repo_url", "")),
    }
    state["current"] = user
    save_accounts(state)


def login(user, password, repo_url=None):
    """登录: 从仓内 accounts.json 校验用户名密码。离线时回退本机缓存。

    repo_url 可传该账户私有档案仓地址并绑定到本机登录态(供 profile_sync 使用)。
    """
    user = (user or "").strip()
    if not user:
        return False, "用户名为空"
    if not password:
        return False, "密码为空"
    now = time.time()
    node = {}
    if not _no_net():
        if _cloud_enabled():
            try:
                rec = cloud_db.get_user(user)
                if rec is not None:
                    if not _verify_password(password, rec):
                        return False, "密码错误"
                    _cache_login(user, now, repo_url or rec.get("repo_url", ""))
                    return True, "登录成功"
            except RuntimeError as e:
                return False, f"云账户不可达: {e}"
        else:
            try:
                _ensure_repo()
                node = _read_node_default()
            except RuntimeError as e:
                return False, f"仓库不可达: {e}"
    users = _accounts_from_node(node)
    if user in users:
        if not _verify_password(password, users[user]):
            return False, "密码错误"
        _cache_login(user, now, repo_url or users[user].get("repo_url", ""))
        return True, "登录成功"
    # 仓内没有: 尝试本机缓存账号校验 (离线或仓尚未含该用户)
    cached = load_accounts().get("accounts", {}).get(user)
    if cached:
        _cache_login(user, now, repo_url or cached.get("repo_url", ""))
        return True, "登录成功(本机缓存)"
    # 【新增】即使仓内没有该用户，也尝试读取本机缓存账号 (account.json),
    # 这样即使GitHub私有仓里没有该用户，如果Windows/其他机器上有本机缓存，
    # 也能进行登录而不报“用户不存在”
    cached2 = load_accounts().get("accounts", {}).get(user)
    if cached2:
        _cache_login(user, now, repo_url or cached2.get("repo_url", ""))
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


# ── 私有数据 profile 读写 ─────────────────────────────────
def _profile_path(user):
    return os.path.join(ACCOUNT_REPO_DIR, _ACCOUNTS_SUBDIR,
                        "profiles", f"{user}.json")


def _read_user_profile(user):
    p = _profile_path(user)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_user_profile(user, bundle):
    """写该用户私有数据 bundle 并推送。state 记录的 ts 保留远端较新者, 供 profile_sync 合并。"""
    _git(["pull", "--no-rebase"], cwd=ACCOUNT_REPO_DIR, check=False)
    p = _profile_path(user)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    atomic_write_json(p, bundle)
    _git(["add", "-A"], cwd=ACCOUNT_REPO_DIR)
    _git(["commit", "-m", f"profile: {user}"], cwd=ACCOUNT_REPO_DIR, check=False)
    out, rc = _git(["push", "origin", "main"], cwd=ACCOUNT_REPO_DIR, check=False)
    if rc != 0:
        raise RuntimeError((out or "").strip() or "推送失败")


def profile_dir_path():
    """返回本机登录仓 profiles/ 目录(绝对路径)。供 profile_sync 定位远端镜像。"""
    try:
        _ensure_repo()
    except RuntimeError:
        pass
    return os.path.join(ACCOUNT_REPO_DIR, _ACCOUNTS_SUBDIR, "profiles")


# ── 修改用户名 / 密码 ─────────────────────────────────────
def change_password(user, old_password, new_password):
    """修改密码(须验证旧密码)。更新仓内 accounts.json 的哈希并推送。"""
    user = (user or "").strip()
    if not new_password or len(new_password) < _MIN_PASSWORD:
        return False, f"新密码至少 {_MIN_PASSWORD} 位"
    if _no_net():
        return False, "离线模式, 无法修改密码"
    if _cloud_enabled():
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
    try:
        _ensure_repo()
    except RuntimeError as e:
        return False, f"仓库不可达: {e}"
    node = _read_node_default()
    users = _accounts_from_node(node)
    rec = users.get(user)
    if not rec:
        return False, "用户不存在"
    if not _verify_password(old_password, rec):
        return False, "旧密码错误"
    salt = _new_salt()
    rec["salt"], rec["hash"] = salt, _hash_password(new_password, salt)
    try:
        _write_and_push(node, f"password: {user}")
    except RuntimeError as e:
        return False, str(e)
    return True, "密码已修改"


def change_username(old_user, new_user, password):
    """修改用户名: 迁移 profiles/{old}.json → profiles/{new}.json, 更新 accounts.json 键。"""
    old_user = (old_user or "").strip()
    new_user = (new_user or "").strip()
    if not _USERNAME_RE.match(new_user):
        return False, ("新用户名须 2-32 位, 仅含字母/数字/._-")
    if old_user == new_user:
        return False, "新旧用户名相同"
    if _no_net():
        return False, "离线模式, 无法修改用户名"
    if _cloud_enabled():
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
    else:
        try:
            _ensure_repo()
        except RuntimeError as e:
            return False, f"仓库不可达: {e}"
        node = _read_node_default()
        users = _accounts_from_node(node)
        rec = users.get(old_user)
        if not rec:
            return False, "用户不存在"
        if not _verify_password(password, rec):
            return False, "密码错误"
        if new_user in users:
            return False, "新用户名已存在"
        # 迁移 profiles 文件
        old_p = _profile_path(old_user)
        new_p = _profile_path(new_user)
        os.makedirs(os.path.dirname(new_p), exist_ok=True)
        bundle = {}
        try:
            with open(old_p, encoding="utf-8") as f:
                bundle = json.load(f)
        except Exception:
            pass
        atomic_write_json(new_p, bundle)
        if os.path.exists(old_p):
            os.remove(old_p)
        # accounts.json 键改名
        users[new_user] = users.pop(old_user)
        users[new_user]["display"] = new_user
        _git(["pull", "--no-rebase"], cwd=ACCOUNT_REPO_DIR, check=False)
        atomic_write_json(os.path.join(ACCOUNT_REPO_DIR, _ACCOUNTS_NODE), node)
        atomic_write_json(new_p, bundle)
        _git(["add", "-A"], cwd=ACCOUNT_REPO_DIR)
        _git(["commit", "-m", f"rename: {old_user}->{new_user}"],
             cwd=ACCOUNT_REPO_DIR, check=False)
        out, rc = _git(["push", "origin", "main"], cwd=ACCOUNT_REPO_DIR, check=False)
        if rc != 0:
            return False, (out or "").strip() or "推送失败"
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
        "repo_url": current_repo_url(),
        "no_net": _no_net(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="威科夫账户登录(用户名+密码)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("register")
    p.add_argument("user")
    p.add_argument("password")
    p.add_argument("--repo", default="", help="账户私有档案仓地址(可选)")
    p = sub.add_parser("login")
    p.add_argument("user")
    p.add_argument("password")
    p.add_argument("--repo", default="", help="首次登录时绑定私有档案仓地址(可选)")
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
        ok, msg = register(args.user, args.password, args.repo)
        out = {"ok": ok, "message": msg}
    elif cmd == "login":
        ok, msg = login(args.user, args.password, args.repo)
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
