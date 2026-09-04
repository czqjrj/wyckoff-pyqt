"""账户私有数据同步：同一账户多设备保持一致。

只同步"账户私有"数据 (UI 布局 / 主题 / 自选 / 候选 / 笔记 / 组合 / 模拟盘账户)，
与多用户"校准数据"合并 (`plan_multiuser_sync.md`) 是两套独立通道。

存储位置: 私有数据在本机 DATA_DIR 下的各自 JSON；远端镜像一个私有 Git 仓
(`profile_repo` 工作副本) 内的 `profile.json`。

合并语义: 逐条目 LWW (last-write-wins) + 删除(tombstone)支持。
- 每个同步条目带 `{v, ts}`；删除同样用 `{v: None, ts}` 表达(tombstone)。
- 两边取 `ts` 较新者；同 ts 确定性 tiebreak。
- 本机 `profile_shadow.json` 记录各条目"上次同步时的值+时间戳"，从而在 collect 时
  检测本地 新增/删除/修改 并打新时间戳 (无需在应用每条写路径埋点)。
- 缺陷说明: 依赖各设备时钟大致同步；并发同一条目双改时后写者胜，可能需再同步。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import uuid

from . import account, storage
from . import paths as P
from . import settings_keys as SK
from . import cloud_db
from ._shared import atomic_write_json

DATA_DIR = P.DATA_DIR
SETTINGS_FILE = P.SETTINGS_FILE
WATCHLIST_FILE = P.WATCHLIST_FILE
NOTES_FILE = P.NOTES_FILE
PORTFOLIO_FILE = P.PORTFOLIO_FILE
CANDIDATES_FILE = P.CANDIDATES_FILE
PAPER_FILE = P.PAPER_FILE

PROFILE_REPO_DIR = os.path.join(DATA_DIR, "profile_repo")
PROFILE_SHADOW_FILE = os.path.join(DATA_DIR, "profile_shadow.json")
REPO_BUNDLE_FILE = "profile.json"
SCHEMA = 1
NO_NET_ENV = "WYCKOFF_NO_NET"

# 同步的对象类型 (顺序即优先级)
TYPES = ("settings", "watchlist", "notes", "portfolio", "candidates", "paper")

# ── 设置键白名单: 只同步私有偏好域, AI 凭据/运行时记忆等不上库 ──
SETTINGS_WHITELIST = set()
for _cls in (SK.General, SK.UI, SK.Chart, SK.Watch, SK.Auto):
    SETTINGS_WHITELIST.update(_c.value for _c in _cls)

_SENSITIVE_HINT = ("key", "token", "secret", "password", "passwd", "credential")


def _sensitive(k):
    return any(h in k.lower() for h in _SENSITIVE_HINT)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if d is not None else default
    except Exception:
        return default


# ── 本地读/写 ───────────────────────────────────────────────
def _read_settings_state():
    s = storage.load_settings()
    return {k: v for k, v in s.items()
            if k in SETTINGS_WHITELIST and not _sensitive(k)}


def _is_default(k, v):
    """settings 键当前的本地值是否等于出厂默认 (用户未主动改过)。"""
    try:
        from .config import DEFAULT_SETTINGS
        return v is None or v == DEFAULT_SETTINGS.get(k)
    except Exception:
        return False


def _write_settings_state(state):
    s = storage.load_settings()
    changed = False
    for k in SETTINGS_WHITELIST:
        if _sensitive(k):
            continue
        v = state.get(k, s.get(k))
        if s.get(k) != v:
            s[k] = v
            changed = True
    if changed:
        storage.save_settings(s)
    return changed


def _read_watchlist():
    return list(_load_json(WATCHLIST_FILE, []) or [])


def _write_watchlist(state):
    # state: {code: {"v": code_or_None}}
    current = _read_watchlist()
    ids = {str(x) for x in current}
    for code, rec in state.items():
        if rec.get("v") is None:
            ids.discard(str(code))
        else:
            ids.add(str(code))
    result = [x for x in current if str(x) in ids]
    existing = {str(x) for x in result}
    for code in ids - existing:
        result.append(code)
    if sorted(map(str, result)) != sorted(map(str, current)):
        storage.save_watchlist(result)
        return True
    return False


def _read_notes():
    return dict(_load_json(NOTES_FILE, {}) or {})


def _write_notes(state):
    data = _read_notes()
    changed = False
    for k, rec in state.items():
        if rec.get("v") is None:
            if k in data:
                del data[k]
                changed = True
        elif data.get(k) != rec["v"]:
            data[k] = rec["v"]
            changed = True
    if changed:
        storage.save_notes(data)
    return changed


def _key_portfolio(r):
    return str(r.get("code"))


def _key_candidate(r):
    return f'{r.get("code")}|{r.get("date")}'


def _read_records(path, key_fn):
    data = _load_json(path, [])
    return {key_fn(r): r for r in data} if isinstance(data, list) else {}


def _write_records(path, state, key_fn):
    current = _read_records(path, key_fn)
    changed = False
    for iid, rec in state.items():
        if rec.get("v") is None:
            if iid in current:
                del current[iid]
                changed = True
        else:
            if current.get(iid) != rec["v"]:
                current[iid] = rec["v"]
                changed = True
    if changed:
        atomic_write_json(path, [current[i] for i in current])
    return changed


def _read_paper():
    return _load_json(PAPER_FILE, None)


def _write_paper(v):
    if v is None:
        if os.path.exists(PAPER_FILE):
            os.remove(PAPER_FILE)
            return True
        return False
    atomic_write_json(PAPER_FILE, v)
    return True


# type -> (read_raw, write_state, key_fn)
_READERS = {
    "settings": (_read_settings_state, _write_settings_state, None),
    "watchlist": (_read_watchlist, _write_watchlist, lambda r: str(r)),
    "notes": (_read_notes, _write_notes, lambda k: str(k)),
    "portfolio": (lambda: _read_records(PORTFOLIO_FILE, _key_portfolio),
                  lambda st: _write_records(PORTFOLIO_FILE, st, _key_portfolio),
                  _key_portfolio),
    "candidates": (lambda: _read_records(CANDIDATES_FILE, _key_candidate),
                   lambda st: _write_records(CANDIDATES_FILE, st, _key_candidate),
                   _key_candidate),
    "paper": (_read_paper, _write_paper, None),
}


# ── 影子 (记录各条目上次同步的值+时间戳) ────────────────────
def _load_shadow():
    return dict(_load_json(PROFILE_SHADOW_FILE, {}) or {})


def _save_shadow(shadow):
    atomic_write_json(PROFILE_SHADOW_FILE, shadow)


def _collect_type(tname):
    """把本地某类型数据转成 {id: {"v": value, "ts": ts}} 状态字典,
    并用影子对比打上"本地自上次同步以来是否变更"的时间戳。

    - 本地在影子中出现且值相同 → 保留原 ts (未变)
    - 本地在影子中值不同 / 影子没有 → 打 now (新增/修改)
    - 本地缺失但影子有 → tombstone {v: None, ts: now} (删除)
    合并后新影子写回。
    """
    reader, writer, key_fn = _READERS[tname]
    shadow = _load_shadow()
    per_type = shadow.setdefault(tname, {})
    now = time.time()
    state = {}
    new_per_type = {}

    if tname in ("settings", "paper"):
        # 单值集合: settings 为 whitelist 键集合; paper 为单条
        if tname == "paper":
            cur = reader()
            entry = per_type.get("paper")
            if cur is not None:
                if entry is None or entry.get("v") != cur:
                    ts = now
                else:
                    ts = entry.get("ts", 0.0)
                state["paper"] = {"v": cur, "ts": ts}
                new_per_type["paper"] = {"v": cur, "ts": ts}
            else:
                if entry is not None:
                    state["paper"] = {"v": None, "ts": now}
                    new_per_type["paper"] = {"v": None, "ts": now}
                # 无历史则保持空
            shadow[tname] = new_per_type
            _save_shadow(shadow)
            return state

        cur = reader()
        for k in SETTINGS_WHITELIST:
            if _sensitive(k):
                continue
            v = cur.get(k)
            entry = per_type.get(k)
            if v is not None:
                if entry is None or entry.get("v") != v:
                    # 首次(影子无记录)时, 若本地值仍是默认值(用户未改过),
                    # 用保守 ts=0, 让云端真实配置在 LWW 中胜出, 避免新设备
                    # 首同步时默认 UI 覆盖云端配置。
                    if entry is None and _is_default(k, v):
                        ts = 0.0
                    else:
                        ts = now
                else:
                    ts = entry.get("ts", 0.0)
                state[k] = {"v": v, "ts": ts}
                new_per_type[k] = {"v": v, "ts": ts}
            else:
                if entry is not None:
                    state[k] = {"v": None, "ts": now}
                    new_per_type[k] = {"v": None, "ts": now}
        shadow[tname] = new_per_type
        _save_shadow(shadow)
        return state

    # list/dict 型
    data = reader()
    if isinstance(data, dict):
        cur_items = {str(k): v for k, v in data.items()}
    else:
        cur_items = {key_fn(x): x for x in data}
    for iid, v in cur_items.items():
        entry = per_type.get(iid)
        if entry is None or entry.get("v") != v:
            ts = now
        else:
            ts = entry.get("ts", 0.0)
        state[iid] = {"v": v, "ts": ts}
        new_per_type[iid] = {"v": v, "ts": ts}
    # 删除检测
    for iid, entry in per_type.items():
        if iid not in cur_items:
            state[iid] = {"v": None, "ts": now}
            new_per_type[iid] = {"v": None, "ts": now}
    shadow[tname] = new_per_type
    _save_shadow(shadow)
    return state


def collect_profile():
    """收集本机私有数据为 bundle 字典 (含时间戳)。会更新影子。"""
    bundle = {"schema": SCHEMA, "machine": _machine_id(),
              "exported_ts": time.time(), "types": {}}
    for tname in TYPES:
        bundle["types"][tname] = {"items": _collect_type(tname)}
    return bundle


def _machine_id():
    path = os.path.join(DATA_DIR, "wx_machine_id")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        mid = uuid.uuid4().hex
        with open(path, "w", encoding="utf-8") as f:
            f.write(mid)
        return mid
    except Exception:
        return "unknown"


# ── 合并 (逐条目 LWW + 确定性 tiebreak) ─────────────────────
def _merge_items(local, remote):
    merged = {}
    for iid in set(local) | set(remote):
        left = local.get(iid) or {}
        right = remote.get(iid) or {}
        lt = left.get("ts", 0.0)
        rt = right.get("ts", 0.0)
        if rt > lt:
            merged[iid] = right
        elif lt > rt:
            merged[iid] = left
        else:
            # 同 ts: 空者输, 都非空取 remote (确定性)
            left_empty = left.get("v") is None
            right_empty = right.get("v") is None
            if left_empty == right_empty:
                merged[iid] = {"v": None, "ts": rt} if right_empty else dict(right)
            else:
                merged[iid] = right if right_empty is False else left
    return merged


def apply_profile(bundle):
    """把远端(已合并)状态写回本机同名文件。返回是否有实际变更。"""
    if not isinstance(bundle, dict) or "types" not in bundle:
        return {"error": "无效的同步包"}
    changed = False
    for tname in TYPES:
        items = (bundle.get("types", {}).get(tname, {}) or {}).get("items", {})
        _, writer, key_fn = _READERS[tname]
        if tname == "settings":
            st = {k: (v.get("v") if isinstance(v, dict) else v)
                  for k, v in items.items()}
            changed |= writer(st)
        elif tname == "paper":
            rec = items.get("paper")
            if rec is not None:
                changed |= writer(rec.get("v"))
        else:
            st = {k: (v if isinstance(v, dict) else {"v": v, "ts": 0})
                  for k, v in items.items()}
            changed |= writer(st)
    return {"changed": changed}


# ── git 传输 ────────────────────────────────────────────────
def _no_net():
    return os.environ.get(NO_NET_ENV, "").strip() in ("1", "true", "TRUE")


def _cloud_enabled():
    """MySQL 云后端可用: 在线 + 已登录账户 (按用户隔离) + 可连通。"""
    try:
        if not account.current_user():
            return False
        return cloud_db.enabled()
    except Exception:
        return False


def _cloud_sync_once():
    """用 MySQL 作为远端存储执行一次 LWW 合并同步 (读取远端→合并→写本机→回写)。

    与 git 版 `sync_once` 同一合并语义, 仅传输层不同: 远端为
    profile_items 表中该用户名下的各类型条目。
    """
    user = account.current_user()
    local = collect_profile()
    try:
        cloud_db.ensure_schema()
    except Exception:
        return {"ok": False, "error": "云库不可达"}
    types = {}
    for tname in TYPES:
        lt = (local.get("types", {}).get(tname, {}) or {}).get("items", {})
        try:
            rt = cloud_db.read_profile_items(user, tname)
        except Exception:
            rt = {}
        types[tname] = {"items": _merge_items(lt, rt)}
    merged = {"schema": SCHEMA, "machine": _machine_id(),
              "exported_ts": time.time(), "types": types}
    apply_profile(merged)
    # 回写该用户各类型条目 (整体覆盖, 与合并结果一致)
    for tname in TYPES:
        try:
            items = merged.get("types", {}).get(tname, {}).get("items", {})
            cloud_db.write_profile_items(user, tname, items)
        except Exception:
            return {"ok": False, "error": f"云写入失败: {tname}"}
    return {"ok": True}


def _cloud_pull():
    """拉取云端合并结果应用到本机 (不强制回写)。"""
    user = account.current_user()
    local = collect_profile()
    types = {}
    for tname in TYPES:
        lt = (local.get("types", {}).get(tname, {}) or {}).get("items", {})
        try:
            rt = cloud_db.read_profile_items(user, tname)
        except Exception:
            rt = {}
        types[tname] = {"items": _merge_items(lt, rt)}
    apply_profile({"schema": SCHEMA, "machine": _machine_id(),
                   "exported_ts": time.time(), "types": types})
    return {"ok": True}


def _cloud_push():
    """把本机当前数据整体写入云端 (含删除 tombstone)。"""
    user = account.current_user()
    merged = collect_profile()
    for tname in TYPES:
        try:
            items = merged.get("types", {}).get(tname, {}).get("items", {})
            cloud_db.write_profile_items(user, tname, items)
        except Exception:
            return {"ok": False, "error": f"云写入失败: {tname}"}
    return {"ok": True}


def _active_repo_url(explicit=""):
    """解析当前生效的私有仓 URL:
    优先当前登录账户绑定的仓库, 其次显式入参, 最后 settings 里的 profile_repo_url。"""
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        acc_url = account.current_repo_url()
        if acc_url:
            return acc_url
    except Exception:
        pass
    try:
        return str(storage.load_settings().get(SK.Runtime.PROFILE_REPO_URL) or "").strip()
    except Exception:
        return ""


def _git(args, cwd=None, check=True):
    if _no_net():
        return ("", 0)
    cmd = ["git"] + args
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} 失败: {(p.stderr or '').strip()}")
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except FileNotFoundError:
        raise RuntimeError("未找到 git 可执行文件")


def _ensure_repo_push():
    """在已初始化的本地 profile_repo 上: 提交本机当前数据并推送到 origin main。
    空仓库 clone 后本地落在 master, 统一改名 main 再推送。
    """
    _git(["branch", "-M", "main"], cwd=PROFILE_REPO_DIR, check=False)
    _commit_bundle(collect_profile(), "init")
    _git(["push", "-u", "origin", "main"], cwd=PROFILE_REPO_DIR)


def setup(url):
    """首次设置: clone 私有仓; 远端为空则本地初始化并首推。

    git clone 一个空仓库也会成功(exit 0), 因此 clone 后需检查远端是否有数据:
      - 有数据 → 应用远端私有数据到本机 (第二/第 N 台设备)
      - 无数据 → 说明是首次/空仓, 提交本机数据并推送 (第一台设备)
    """
    if _no_net():
        return {"ok": False, "error": "离线模式(no-net), 跳过"}
    if _cloud_enabled():
        # 云后端无需 clone 私有仓: 直接按当前用户做一次 LWW 合并同步。
        return _cloud_sync_once()
    url = _active_repo_url(url)
    if not url:
        return {"ok": False, "error": "缺少仓库 URL (请先登录账户或在设置中填写)"}
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(PROFILE_REPO_DIR):
        shutil.rmtree(PROFILE_REPO_DIR, ignore_errors=True)
    cloned = False
    try:
        _git(["clone", url, PROFILE_REPO_DIR])
        cloned = True
    except RuntimeError:
        pass
    try:
        if cloned:
            # 空仓库默认 HEAD 是 master, 数据在 main → clone 后统一切到 origin/main
            _git(["checkout", "-B", "main", "origin/main"],
                 cwd=PROFILE_REPO_DIR, check=False)
            _git(["branch", "-M", "main"], cwd=PROFILE_REPO_DIR, check=False)
            remote = _read_repo_bundle() or {}
            has_data = any((remote.get("types", {}).get(t, {}) or {}).get("items")
                           for t in TYPES)
            if has_data:
                apply_profile(remote)
                return {"ok": True, "action": "cloned-applied"}
            # 空远端: 当作首次设备
            os.makedirs(PROFILE_REPO_DIR, exist_ok=True)
            _ensure_repo_push()
            return {"ok": True, "action": "init-and-push"}
        # clone 失败(网络/错误) → 本地初始化再推送
        os.makedirs(PROFILE_REPO_DIR, exist_ok=True)
        _git(["init"], cwd=PROFILE_REPO_DIR)
        _git(["remote", "add", "origin", url], cwd=PROFILE_REPO_DIR)
        _git(["branch", "-M", "main"], cwd=PROFILE_REPO_DIR)
        _ensure_repo_push()
        return {"ok": True, "action": "init-and-push"}
    except RuntimeError as e2:
        return {"ok": False, "error": str(e2)}



def _commit_bundle(bundle, msg="profile sync"):
    p = os.path.join(PROFILE_REPO_DIR, REPO_BUNDLE_FILE)
    atomic_write_json(p, bundle)
    _git(["add", "-A"], cwd=PROFILE_REPO_DIR)
    _git(["commit", "-m", msg], cwd=PROFILE_REPO_DIR, check=False)


def _read_repo_bundle():
    p = os.path.join(PROFILE_REPO_DIR, REPO_BUNDLE_FILE)
    return _load_json(p, {}) or {} if os.path.exists(p) else {}


def _ensure_repo():
    if not os.path.isdir(os.path.join(PROFILE_REPO_DIR, ".git")):
        raise RuntimeError("未初始化, 先 profile-setup <url>")


def _ensure_remote_url():
    """把工作副本 origin 对齐到账户绑定的仓库地址。

    历史 bug: profile_repo 曾用拼错的仓库地址 clone/pull, 导致「从云下载」
    一直在拉错误的仓库而拿不到云端数据。这里在每轮 pull/push 前按
    账户当前绑定仓库动态重设 origin, 避免再次漂移。
    """
    url = _active_repo_url()
    if not url:
        return
    _git(["remote", "set-url", "origin", url], cwd=PROFILE_REPO_DIR, check=False)


def sync_once():
    """拉取远端 → 与本机合并 → 写本机 → 提交推送。

    云后端可用时走 MySQL (按当前登录用户隔离), 否则走 Git。
    """
    if _no_net():
        return {"ok": False, "error": "离线模式(no-net), 跳过"}
    if _cloud_enabled():
        return _cloud_sync_once()
    try:
        _ensure_repo()
        _ensure_remote_url()
        _git(["pull", "--no-rebase"], cwd=PROFILE_REPO_DIR, check=False)
        remote = _read_repo_bundle()
        local = collect_profile()
        types = {}
        for tname in TYPES:
            lt = (local.get("types", {}).get(tname, {}) or {}).get("items", {})
            rt = (remote.get("types", {}).get(tname, {}) or {}).get("items", {})
            types[tname] = {"items": _merge_items(lt, rt)}
        merged = {"schema": SCHEMA, "machine": _machine_id(),
                  "exported_ts": time.time(), "types": types}
        apply_profile(merged)
        _commit_bundle(merged, "profile sync")
        _git(["push"], cwd=PROFILE_REPO_DIR, check=False)
        return {"ok": True}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


def pull_or_push(mode):
    if _no_net():
        return {"ok": False, "error": "离线模式(no-net), 跳过"}
    if _cloud_enabled():
        if mode == "pull":
            return _cloud_pull()
        return _cloud_push()
    try:
        _ensure_repo()
        _ensure_remote_url()
        if mode == "pull":
            _git(["pull", "--no-rebase"], cwd=PROFILE_REPO_DIR, check=False)
            remote = _read_repo_bundle()
            # 不能盲目用远端覆盖本地: 先按影子收集本地变更(含删除 tombstone),
            # 与远端做 LWW 合并, 使本地已删除的条目不被云端旧数据复活。
            local = collect_profile()
            types = {}
            for tname in TYPES:
                lt = (local.get("types", {}).get(tname, {}) or {}).get("items", {})
                rt = (remote.get("types", {}).get(tname, {}) or {}).get("items", {})
                types[tname] = {"items": _merge_items(lt, rt)}
            apply_profile({"schema": SCHEMA, "machine": _machine_id(),
                           "exported_ts": time.time(), "types": types})
        else:
            _commit_bundle(collect_profile(), "profile push")
            _git(["push"], cwd=PROFILE_REPO_DIR, check=False)
        return {"ok": True}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}


def status():
    acc_status = {}
    try:
        acc_status = account.status()
    except Exception:
        pass
    return {
        "dir": PROFILE_REPO_DIR,
        "configured": os.path.isdir(os.path.join(PROFILE_REPO_DIR, ".git")),
        "url": _active_repo_url(),
        "no_net": _no_net(),
        "account": acc_status,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="威科夫账户私有数据同步")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("setup")
    p.add_argument("url", nargs="?", default="",
                   help="仓库 URL; 省略则用当前登录账户绑定的仓库")
    sub.add_parser("pull")
    sub.add_parser("push")
    sub.add_parser("sync")
    sub.add_parser("status")
    sub.add_parser("logout")
    sub.add_parser("account")
    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd == "setup":
        out = setup(args.url or _active_repo_url())
    elif cmd in ("pull", "push"):
        out = pull_or_push(cmd)
    elif cmd == "sync":
        out = sync_once()
    elif cmd == "logout":
        _ok, _msg = account.logout()
        out = {"ok": _ok, "message": _msg}
    elif cmd == "account":
        out = account.status()
    elif cmd == "status":
        out = status()
    else:
        parser.print_help()
        return
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

