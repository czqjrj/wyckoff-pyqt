"""多用户校准数据合并核心 (纯函数, 无 IO 副作用)。

合并语义见 docs/plan_multiuser_sync.md §3 —— 确定性: 任意一端以相同的
本地/远端输入执行, 结果一致。

- 信号: 键 symbol|scale|kind|type|date; 冲突取 last_eval_ts 较新者
  (评估结果随行情推进更新)。
- 反馈: 键 symbol|scale|start_dt|end_dt; 非空 verdict 优先于空;
  都非空取判定日期新者。
- 模型: 整文件采纳制 —— 仅当 feat_version 与当前代码一致且训练时间
  更新才采纳, 否则忽略 (由调用方在 status 中提示)。
"""
from datetime import datetime

SCHEMA_VERSION = 1


def signal_key(rec):
    """信号唯一键, 与 wyckoff.signal_accuracy._key 一致。"""
    return f"{rec.get('symbol')}|{rec.get('scale')}|{rec.get('kind')}|{rec.get('type')}|{rec.get('date')}"


def feedback_key(rec):
    """反馈唯一键, 与 wyckoff.storage.feedback_key 一致。"""
    return f"{rec.get('symbol')}|{rec.get('scale')}|{rec.get('start_dt')}|{rec.get('end_dt')}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fb_time(rec):
    """反馈判定时间: 兼容 unix 时间戳与 ISO 日期串 ("2026-08-20")。"""
    d = rec.get("date")
    if isinstance(d, (int, float)):
        return float(d)
    s = str(d or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s[:19]).timestamp()
    except ValueError:
        return 0.0


def merge_signals(local, remote):
    """按信号键并集合并。返回 (merged, n_new, n_upd)。

    n_new=远端新增记录数, n_upd=远端覆盖本地记录数。
    last_eval_ts 相等时保留本地 (确定性, 两端执行结果一致)。
    """
    merged = {}
    order = []
    for r in (local or []):
        k = signal_key(r)
        if k in merged:
            continue
        merged[k] = dict(r)
        order.append(k)
    n_new = 0
    n_upd = 0
    for r in (remote or []):
        k = signal_key(r)
        cur = merged.get(k)
        if cur is None:
            merged[k] = dict(r)
            order.append(k)
            n_new += 1
        elif _num(r.get("last_eval_ts")) > _num(cur.get("last_eval_ts")):
            merged[k] = dict(r)
            n_upd += 1
    return [merged[k] for k in order], n_new, n_upd


def merge_feedback(local, remote):
    """按反馈键并集合并。返回 (merged, n_new, n_upd)。

    远端胜出条件: verdict 非空且本地为空; 或双方都非空且判定日期更新。
    其余情况保留本地 (确定性)。
    """
    def rank(rec):
        return 1 if str(rec.get("verdict") or "").strip() else 0

    merged = {}
    order = []
    for r in (local or []):
        k = feedback_key(r)
        if k in merged:
            continue
        merged[k] = dict(r)
        order.append(k)
    n_new = 0
    n_upd = 0
    for r in (remote or []):
        k = feedback_key(r)
        cur = merged.get(k)
        if cur is None:
            merged[k] = dict(r)
            order.append(k)
            n_new += 1
        else:
            rr, cr = rank(r), rank(cur)
            if rr > cr or (rr == cr == 1 and _fb_time(r) > _fb_time(cur)):
                merged[k] = dict(r)
                n_upd += 1
    return [merged[k] for k in order], n_new, n_upd


def model_trained_ts(state):
    """模型状态训练时间; 兼容 trained_at / trained_ts 两种字段名。"""
    if state.get("trained_at"):
        return _num(state.get("trained_at"))
    return _num(state.get("trained_ts"))


def merge_model(local_state, remote_state, feat_version=None):
    """模型整文件采纳判定。返回 (adopted_state|None, reason)。

    reason ∈ {adopted, remote_empty, feat_version_mismatch, local_newer}。
    feat_version 不匹配 → 拒绝采纳但数据照常合并, 调用方据此显示警告。
    """
    if not isinstance(remote_state, dict) or not remote_state:
        return None, "remote_empty"
    fv = feat_version
    if fv is None:
        from wyckoff.online_model import FEATURE_VERSION
        fv = FEATURE_VERSION
    try:
        remote_fv = int(remote_state.get("feat_version") or 0)
    except (TypeError, ValueError):
        remote_fv = -1
    if remote_fv != fv:
        return None, "feat_version_mismatch"
    if isinstance(local_state, dict) and local_state \
            and model_trained_ts(remote_state) <= model_trained_ts(local_state):
        return None, "local_newer"
    return dict(remote_state), "adopted"
