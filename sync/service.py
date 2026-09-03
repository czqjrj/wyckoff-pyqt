"""同步编排: setup / pull / push / sync / status。

协议 (docs/plan_multiuser_sync.md §2):
    pull 远端 → 按键合并到本地 → 合并新增>0 时用全量重训
    → 本地+远端 model.json 更新 → push (被拒则重拉合并重推, 最多 2 次重试)
"""
import os
import time

from . import transport
from .bundle import export_bundle, import_bundle, machine_id

MAX_PUSH_RETRY = 2


def _settings():
    from wyckoff.storage import load_settings
    return load_settings()


def _save_settings(s):
    from wyckoff.storage import save_settings
    save_settings(s)


# 固定共享校准仓: 与 UI (calibration_center.CALIB_SYNC_REPO) 同源, 供未初始化
# UI 时 (CLI / 自动同步 / 直接调用) 也能自动回填, 避免 "未配置校准仓库 URL"。
DEFAULT_CALIB_URL = "git@github.com:czqjrj/wyckoff-calib.git"


def configured_url():
    """返回当前校准仓 URL; 未配置(空)时自动回填到固定共享仓并落盘。

    UI 校准中心打开时既会 refresh_sync_url() 回填该值; 此处兜底保证任何入口
    (python -m sync / 自动同步) 在校准仓尚未登记时也能定位到固定共享仓。
    """
    url = str(_settings().get("calib_repo_url") or "").strip()
    if url:
        return url
    if DEFAULT_CALIB_URL:
        try:
            s = _settings()
            s["calib_repo_url"] = DEFAULT_CALIB_URL
            _save_settings(s)
        except Exception:
            pass
    return DEFAULT_CALIB_URL


def save_creds(url, username="", password=""):
    """仅写入 https 凭据 (无网络操作); 供 UI 在同步前静默持久化。"""
    if not username:
        return False
    host = transport.url_host(url)
    if not host:
        raise transport.SyncError("无法从 URL 解析 host (https 凭据需 https 地址)")
    transport.save_https_creds(username, password, host)
    return True


def setup(url, username="", password=""):
    """保存仓库 URL 并完成首次 clone。可附带 https 凭据实现自动鉴权。返回 status dict。"""
    url = str(url or "").strip()
    if not url:
        raise transport.SyncError("URL 为空")
    save_creds(url, username, password)
    rdir, branch = transport.ensure_repo(url)
    s = _settings()
    s["calib_repo_url"] = url
    s.setdefault("calib_last_sync", {})
    _save_settings(s)
    return {"url": url, "repo": rdir, "branch": branch, "cloned": True}


def pull(retrain=False):
    """拉取远端 canonical 数据合并进本地库。返回结果 dict。"""
    url = configured_url()
    repo, branch = transport.ensure_repo(url)
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    transport.reset_to_remote(repo, branch)
    remote = transport.read_canonical(repo)
    counts = {}
    if remote.get("signals.json") or remote.get("feedback.json"):
        counts = import_bundle({
            "signals": remote.get("signals.json") or [],
            "feedback": remote.get("feedback.json") or [],
            "model": remote.get("model.json"),
        })
    _mark_sync(counts)
    return counts


def push():
    """本地数据导出覆盖 canonical 文件并推送。"""
    url = configured_url()
    repo, branch = transport.ensure_repo(url)
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    transport.reset_to_remote(repo, branch)
    bundle = export_bundle(include_model=True)
    meta = transport.make_meta(
        {machine_id(): time.time()},
        len(bundle["signals"]),
        len(bundle["feedback"]),
    )
    transport.write_canonical(repo, {
        "signals.json": bundle["signals"],
        "feedback.json": bundle["feedback"],
        "model.json": bundle["model"],
        "meta.json": meta,
    })
    changed = transport.commit_all(repo, "sync: push from " + machine_id()[:8])
    transport.push(repo, branch)
    _mark_sync({"pushed": changed})
    return {"pushed": changed}


def sync(retrain=True):
    """完整同步。返回汇总 dict (含各步计数/警告)。"""
    url = configured_url()
    if not url:
        raise transport.SyncError("未配置校准仓库 URL (先执行 setup)")
    repo, branch = transport.ensure_repo(url)
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    result = {"url": url, "retrained": False}
    last_err = None
    for attempt in range(1, MAX_PUSH_RETRY + 1):
        try:
            transport.fetch_repo(repo)
            transport.reset_to_remote(repo, branch)
            remote = transport.read_canonical(repo)
            counts = import_bundle({
                "signals": remote.get("signals.json") or [],
                "feedback": remote.get("feedback.json") or [],
                "model": remote.get("model.json"),
            })
            result.update(counts)
            n_changed = (counts.get("signals_new", 0) + counts.get("signals_upd", 0)
                         + counts.get("feedback_new", 0) + counts.get("feedback_upd", 0))
            final_model = None
            if n_changed and retrain:
                from wyckoff.online_model import train_model
                state = train_model()
                result["retrained"] = bool(state)
                result["model_metrics"] = {
                    k: state.get(k)
                    for k in ("n_labels", "n_train", "auc_oos")
                    if k in state
                }
                final_model = state or None
            else:
                from wyckoff.online_model import _load_state
                from wyckoff.paths import ONLINE_MODEL_FILE
                if os.path.exists(ONLINE_MODEL_FILE):
                    final_model = _load_state()
                else:
                    final_model = None
            bundle = export_bundle(include_model=False)
            meta = transport.make_meta(
                {machine_id(): time.time()},
                len(bundle["signals"]),
                len(bundle["feedback"]),
            )
            transport.write_canonical(repo, {
                "signals.json": bundle["signals"],
                "feedback.json": bundle["feedback"],
                "model.json": final_model,
                "meta.json": meta,
            })
            transport.commit_all(repo, "sync: merge from " + machine_id()[:8])
            transport.push(repo, branch)
            result["ok"] = True
            break
        except transport.PushRejected as e:
            last_err = e
            result["push_retries"] = attempt + 1
    else:
        result["ok"] = False
        result["error"] = f"push 重试 {MAX_PUSH_RETRY} 次仍被拒: {last_err}"
    _mark_sync(result)
    return result


def _mark_sync(extra):
    """把上次同步时间/摘要记入 settings (供 UI 状态行展示)。"""
    try:
        s = _settings()
        rec = s.setdefault("calib_last_sync", {})
        rec["ts"] = time.time()
        for k in ("signals_new", "signals_upd", "feedback_new",
                  "feedback_upd", "pushed", "ok", "error", "retrained"):
            if k in extra:
                rec[k] = extra[k]
        _save_settings(s)
    except Exception:
        pass


def status():
    """汇总当前同步状态 (不触发网络写操作; fetch 失败降级为本地信息)。"""
    s = _settings()
    st = {
        "url": str(s.get("calib_repo_url") or ""),
        "machine": machine_id(),
        "last_sync": dict(s.get("calib_last_sync") or {}),
        "repo_cloned": False,
        "remote_meta": None,
        "feat_version_warn": None,
    }
    rdir = transport.repo_dir()
    st["repo_cloned"] = os.path.isdir(os.path.join(rdir, ".git"))
    st["remote_counts"] = {"signals": 0, "feedback": 0}
    if st["repo_cloned"]:
        branch = transport.default_branch(rdir)
        p = transport._run(["fetch", "origin", "--prune"], cwd=rdir)
        if transport._ok(p):
            st["remote_meta"] = transport.remote_head_meta(rdir, branch)
            if isinstance(st["remote_meta"], dict) \
                    and isinstance(st["remote_meta"].get("counts"), dict):
                st["remote_counts"] = st["remote_meta"]["counts"]
                contributors = st["remote_meta"].get("contributors") or {}
                st["n_contributors"] = len(contributors)
        else:
            st["fetch_error"] = (p.stderr or "")[:200]
    if st["remote_meta"]:
        from .merge import SCHEMA_VERSION
        if int(st["remote_meta"].get("schema") or 0) > SCHEMA_VERSION:
            st["feat_version_warn"] = (
                f"远端 schema v{st['remote_meta']['schema']} "
                f"高于本地 v{SCHEMA_VERSION}, 请先升级程序再同步")
    return st
