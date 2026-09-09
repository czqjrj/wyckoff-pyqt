"""sync 包核心纯逻辑测试 (merge/cloud/bundle)。

此前 sync 包无直接测试 (旧 test_sync_* 已删), 这里覆盖:
- merge_signals / merge_feedback / merge_model 的确定性合并语义
- cloud.make_meta 计数/schema
- bundle.machine_id 幂等持久化
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["WYCKOFF_NO_NET"] = "1"

from sync.bundle import machine_id
from sync.cloud import make_meta
from sync.merge import (
    SCHEMA_VERSION,
    feedback_key,
    merge_feedback,
    merge_model,
    merge_signals,
    signal_key,
)


def _sig(symbol="sh600036", scale="240", kind="bull", type_="Spring",
         date="2026-08-20", conf=90, last_eval_ts=1000.0):
    return {"symbol": symbol, "scale": scale, "kind": kind, "type": type_,
            "date": date, "conf": conf, "last_eval_ts": last_eval_ts}


def _fb(symbol="sh600036", scale="240", start="2026-08-01", end="2026-08-10",
        verdict="hit", date=1000.0):
    return {"symbol": symbol, "scale": scale, "start_dt": start, "end_dt": end,
            "verdict": verdict, "date": date}


# ── key 构造 ──

def test_signal_key_format():
    r = _sig()
    assert signal_key(r) == "sh600036|240|bull|Spring|2026-08-20"


def test_feedback_key_format():
    r = _fb()
    assert feedback_key(r) == "sh600036|240|2026-08-01|2026-08-10"


# ── merge_signals ──

def test_merge_signals_all_new_from_remote():
    local = [_sig()]
    remote = [_sig(date="2026-08-21", conf=88), _sig(symbol="sz000001")]
    merged, n_new, n_upd = merge_signals(local, remote)
    assert n_new == 2 and n_upd == 0
    assert len(merged) == 3


def test_merge_signals_newer_wins_and_local_kept_on_tie():
    local = [_sig(conf=90, last_eval_ts=2000.0)]
    remote = [_sig(conf=80, last_eval_ts=3000.0)]  # 远端更新 → 覆盖
    merged, _, n_upd = merge_signals(local, remote)
    assert n_upd == 1 and merged[0]["conf"] == 80

    merged_tie, _, n_upd_tie = merge_signals(
        [_sig(conf=90, last_eval_ts=3000.0)], [_sig(conf=80, last_eval_ts=3000.0)])
    assert n_upd_tie == 0 and merged_tie[0]["conf"] == 90  # tie 保留本地


def test_merge_signals_prefers_local_duplicate_key():
    # 本地同键重复只保留首条
    merged, _, _ = merge_signals(
        [_sig(conf=95), _sig(conf=50)], [])
    assert len(merged) == 1 and merged[0]["conf"] == 95


# ── merge_feedback ──

def test_merge_feedback_vernon_empty_verdict_wins():
    local = [_fb(verdict="miss", date=2000.0)]
    remote = [_fb(verdict="", date=9999.0)]  # 远端判空, 不应覆盖本地非空
    merged, n_new, n_upd = merge_feedback(local, remote)
    assert n_upd == 0 and merged[0]["verdict"] == "miss"


def test_merge_feedback_newer_verdict_wins():
    local = [_fb(verdict="hit", date=1000.0)]
    remote = [_fb(verdict="miss", date=2000.0)]
    merged, _, n_upd = merge_feedback(local, remote)
    assert n_upd == 1 and merged[0]["verdict"] == "miss"


def test_merge_feedback_local_has_verdict_remote_empty_new():
    remote = [_fb(verdict="hit")]
    merged, n_new, _ = merge_feedback([], remote)
    assert n_new == 1 and len(merged) == 1


# ── merge_model ──

def test_merge_model_remote_empty_rejected():
    merged, reason = merge_model(None, None)
    assert merged is None and reason == "remote_empty"


def test_merge_model_feat_version_mismatch_rejected():
    state = {"feat_version": 999, "trained_ts": 9999}
    m, reason = merge_model({}, state, feat_version=1)
    assert m is None and reason == "feat_version_mismatch"


def test_merge_model_local_newer_kept():
    local = {"feat_version": 1, "trained_ts": 5000}
    remote = {"feat_version": 1, "trained_ts": 4000}
    m, reason = merge_model(local, remote, feat_version=1)
    assert m is None and reason == "local_newer"


def test_merge_model_adopted_when_remote_newer():
    local = {"feat_version": 1, "trained_at": 3000}
    remote = {"feat_version": 1, "trained_at": 8000}
    m, reason = merge_model(local, remote, feat_version=1)
    assert m == remote and reason == "adopted"


# ── cloud.make_meta ──

def test_make_meta_counts_and_schema():
    meta = make_meta(["alice", "bob"], 12, 5)
    assert meta["schema"] == SCHEMA_VERSION
    assert meta["contributors"] == ["alice", "bob"]
    assert meta["counts"] == {"signals": 12, "feedback": 5}
    assert isinstance(meta["updated_ts"], float) and meta["updated_ts"] > 0


# ── bundle.machine_id ──

def test_machine_id_persistent(tmp_path):
    first = machine_id(str(tmp_path))
    second = machine_id(str(tmp_path))
    assert first == second and len(first) == 32
    assert os.path.exists(os.path.join(str(tmp_path), "wx_machine_id"))
