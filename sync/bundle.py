"""校准数据打包/落库: 用户数据 ↔ 可同步 bundle。

bundle 结构:
    {
        "schema": 1,
        "machine": "<machine_id>",
        "exported_ts": <unix ts>,
        "signals": [...],
        "feedback": [...],
        "model": {...}|None,
    }
"""
import os
import time
import uuid

from .merge import SCHEMA_VERSION, merge_feedback, merge_model, merge_signals


def machine_id(data_dir=None):
    """本端身份 (uuid4, 首次生成后持久化), 仅用于 meta.contributors 统计。"""
    if data_dir is None:
        from wyckoff.paths import DATA_DIR
        data_dir = DATA_DIR
    path = os.path.join(data_dir, "wx_machine_id")
    try:
        with open(path, encoding="utf-8") as f:
            mid = f.read().strip()
        if mid:
            return mid
    except Exception:
        pass
    mid = uuid.uuid4().hex
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(mid)
        os.replace(tmp, path)
    except Exception:
        pass
    return mid


def export_bundle(include_model=True):
    """从本地用户数据导出全量 bundle。"""
    from wyckoff.signal_accuracy import load_signals
    from wyckoff.storage import load_feedback

    model = None
    if include_model:
        from wyckoff.online_model import _load_state
        from wyckoff.paths import ONLINE_MODEL_FILE
        if os.path.exists(ONLINE_MODEL_FILE):
            model = _load_state() or None
    return {
        "schema": SCHEMA_VERSION,
        "machine": machine_id(),
        "exported_ts": time.time(),
        "signals": load_signals(),
        "feedback": load_feedback(),
        "model": model,
    }


def import_bundle(bundle):
    """把远端 bundle 合并写入本地库。返回计数 dict。

    写入沿用现有原子替换路径 (save_signals/save_feedback 会连带失效
    命中率缓存); 模型按采纳制写入 ONLINE_MODEL_FILE。
    """
    from wyckoff.signal_accuracy import load_signals, save_signals
    from wyckoff.storage import load_feedback, save_feedback

    signals = bundle.get("signals") or []
    feedback = bundle.get("feedback") or []
    merged_s, s_new, s_upd = merge_signals(load_signals(), signals)
    merged_f, f_new, f_upd = merge_feedback(load_feedback(), feedback)
    if s_new + s_upd:
        save_signals(merged_s)
    if f_new + f_upd:
        save_feedback(merged_f)
    model_state, model_reason = None, "remote_empty"
    if bundle.get("model"):
        from wyckoff.online_model import _load_state
        from wyckoff.paths import ONLINE_MODEL_FILE
        model_state, model_reason = merge_model(_load_state(), bundle["model"])
        if model_state is not None:
            from wyckoff._shared import atomic_write_json
            atomic_write_json(ONLINE_MODEL_FILE, model_state)
    return {
        "signals_new": s_new,
        "signals_upd": s_upd,
        "feedback_new": f_new,
        "feedback_upd": f_upd,
        "n_total_signals": len(merged_s),
        "n_total_feedback": len(merged_f),
        "model_adopted": model_state is not None,
        "model_reason": model_reason,
    }
