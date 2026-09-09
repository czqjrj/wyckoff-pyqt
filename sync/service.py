"""同步编排: pull / push / sync / status (云端 MySQL 后端)。

协议 (docs/plan_multiuser_sync.md §2 云端版):
    pull 远端 → 按键合并到本地 → 合并新增>0 时用全量重训 → 写回远端。
云端传输为单行原子 upsert (last-writer-wins at storage), 但调用方总是
"先合并远端+本地再写回", 因此不丢数据; 不存在 Git 的 push 冲突重试。
"""
import time

from . import cloud, transport
from .bundle import export_bundle, import_bundle, machine_id


def _require_cloud():
    if transport.no_net():
        raise transport.SyncError("WYCKOFF_NO_NET=1, 已跳过同步")
    if not cloud.enabled():
        raise transport.SyncError("云端 (MySQL) 不可用, 无法同步")


def pull(retrain=False):
    """拉取远端 canonical 数据合并进本地库。返回结果 dict。"""
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    if not cloud.enabled():
        return {"error": "云端 (MySQL) 不可用, 无法同步"}
    remote = cloud.read_canonical()
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
    """本地数据导出覆盖 canonical 文件并推送 (云单行原子 upsert)。"""
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    if not cloud.enabled():
        return {"error": "云端 (MySQL) 不可用, 无法同步"}
    bundle = export_bundle(include_model=True)
    meta = cloud.make_meta(
        {machine_id(): time.time()},
        len(bundle["signals"]),
        len(bundle["feedback"]),
    )
    cloud.write_canonical({
        "signals.json": bundle["signals"],
        "feedback.json": bundle["feedback"],
        "model.json": bundle["model"],
        "meta.json": meta,
    })
    _mark_sync({"pushed": True})
    return {"pushed": True}


def sync(retrain=True):
    """完整同步。返回汇总 dict (含各步计数/警告)。

    读远端 → 合并进本地 → (有新增则重训) → 写回合并后的全量。
    """
    if transport.no_net():
        return {"skipped": "WYCKOFF_NO_NET=1"}
    if not cloud.enabled():
        return {"error": "云端 (MySQL) 不可用, 无法同步", "ok": False}
    result = {"cloud": True, "retrained": False}
    remote = cloud.read_canonical()
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
        import os
        if os.path.exists(ONLINE_MODEL_FILE):
            final_model = _load_state()
        else:
            final_model = None
    bundle = export_bundle(include_model=False)
    meta = cloud.make_meta(
        {machine_id(): time.time()},
        len(bundle["signals"]),
        len(bundle["feedback"]),
    )
    cloud.write_canonical({
        "signals.json": bundle["signals"],
        "feedback.json": bundle["feedback"],
        "model.json": final_model,
        "meta.json": meta,
    })
    result["ok"] = True
    _mark_sync(result)
    return result


def _mark_sync(extra):
    """把上次同步时间/摘要记入 settings (供 UI 状态行展示)。"""
    try:
        from wyckoff.storage import load_settings, save_settings
        s = load_settings()
        rec = s.setdefault("calib_last_sync", {})
        rec["ts"] = time.time()
        for k in ("signals_new", "signals_upd", "feedback_new",
                  "feedback_upd", "pushed", "ok", "error", "retrained"):
            if k in extra:
                rec[k] = extra[k]
        save_settings(s)
    except Exception:
        pass


def status():
    """汇总当前同步状态 (读共享 meta, 不触发写操作)。"""
    from wyckoff.storage import load_settings
    s = load_settings()
    st = {
        "cloud": True,
        "machine": machine_id(),
        "last_sync": dict(s.get("calib_last_sync") or {}),
        "remote_meta": None,
        "feat_version_warn": None,
    }
    if transport.no_net():
        return st
    try:
        meta = cloud.remote_meta()
        st["remote_meta"] = meta
        if isinstance(meta, dict) and isinstance(meta.get("counts"), dict):
            st["remote_counts"] = meta["counts"]
            contributors = meta.get("contributors") or {}
            st["n_contributors"] = len(contributors)
        if st["remote_meta"]:
            from .merge import SCHEMA_VERSION
            if int(st["remote_meta"].get("schema") or 0) > SCHEMA_VERSION:
                st["feat_version_warn"] = (
                    f"远端 schema v{st['remote_meta']['schema']} "
                    f"高于本地 v{SCHEMA_VERSION}, 请先升级程序再同步")
    except Exception as e:
        st["fetch_error"] = str(e)[:200]
    return st