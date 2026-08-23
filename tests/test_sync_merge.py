"""P1 合并核心单元测试: 并集/冲突规则/计数守恒。"""
import sync.merge as merge


def sig(symbol="sh600000", date="2026-01-01", last_eval_ts=0, **kw):
    r = {"symbol": symbol, "scale": 240, "kind": "event", "type": "Spring",
         "date": date, "created_ts": 1.0, "last_eval_ts": last_eval_ts,
         "status": "pending"}
    r.update(kw)
    return r


def fb(symbol="sh600000", start="2026-01-01 00:00", end="2026-02-01 00:00",
       verdict="", date="", **kw):
    r = {"symbol": symbol, "scale": 240, "start_dt": start, "end_dt": end,
         "label": "accumulation", "verdict": verdict, "date": date}
    r.update(kw)
    return r


# ── 信号合并 ──

def test_signals_union_counts():
    a = [sig(date="2026-01-01"), sig(date="2026-01-02")]
    b = [sig(date="2026-01-02"), sig(symbol="sh600001", date="2026-01-03")]
    m, n_new, n_upd = merge.merge_signals(a, b)
    assert len(m) == 3 and n_new == 1 and n_upd == 0


def test_signals_newer_last_eval_wins():
    a = [sig(last_eval_ts=100, status="done")]
    m, _, n_upd = merge.merge_signals(a, [sig(last_eval_ts=200)])
    assert n_upd == 1 and m[0]["last_eval_ts"] == 200
    m, _, n_upd = merge.merge_signals([sig(last_eval_ts=300)], [sig(last_eval_ts=200)])
    assert n_upd == 0 and m[0]["last_eval_ts"] == 300
    # 时间戳相等 → 保留本地 (确定性)
    m, _, n_upd = merge.merge_signals([sig(last_eval_ts=100)], [sig(last_eval_ts=100)])
    assert n_upd == 0


def test_signals_missing_ts_treated_as_zero():
    a = [sig(date="d1")]  # 无 last_eval_ts 字段
    m, _, n_upd = merge.merge_signals(a, [sig(date="d1", last_eval_ts=5)])
    assert n_upd == 1 and m[0]["last_eval_ts"] == 5


# ── 反馈合并 ──

def test_feedback_nonempty_verdict_wins():
    k = dict(start="2026-01-01 00:00", end="2026-02-01 00:00")
    m, _, n_upd = merge.merge_feedback([fb(verdict="", **k)],
                                       [fb(verdict="correct", **k)])
    assert n_upd == 1 and m[0]["verdict"] == "correct"
    m, _, n_upd = merge.merge_feedback([fb(verdict="wrong", **k)], [fb(**k)])
    assert n_upd == 0 and m[0]["verdict"] == "wrong"


def test_feedback_both_nonempty_newer_date_wins():
    k = dict(start="s", end="e")
    a = [fb(verdict="wrong", date="2026-08-01", **k)]
    b = [fb(verdict="correct", date="2026-08-10", **k)]
    m, _, n_upd = merge.merge_feedback(a, b)
    assert n_upd == 1 and m[0]["verdict"] == "correct"
    m, _, n_upd = merge.merge_feedback(b, a)  # 反向一致
    assert n_upd == 0 and m[0]["verdict"] == "correct"


def test_feedback_empty_dates_keep_local():
    k = dict(start="s", end="e")
    a = [fb(verdict="correct", date="2026-08-01", **k)]
    b = [fb(verdict="correct", date="", **k)]
    m, _, n_upd = merge.merge_feedback(a, b)
    assert n_upd == 0 and m[0]["date"] == "2026-08-01"


# ── 计数守恒 / 幂等 ──

def test_merge_roundtrip_conservation():
    a = [sig(date=f"2026-01-{d:02d}") for d in range(1, 6)]
    b = [sig(date=f"2026-01-{d:02d}", last_eval_ts=d * 10) for d in range(3, 9)]
    m, n_new, n_upd = merge.merge_signals(a, b)
    assert len(m) == len(set(merge.signal_key(r) for r in m))
    assert n_new + n_upd + len(a) - n_upd == len(m)  # 新增+保留旧+覆盖 = 总数
    m2, n2, u2 = merge.merge_signals(m, b)  # 再合并一次应零变化
    assert n2 == 0 and u2 == 0 and m2 == m


def test_merge_disjoint_symmetry():
    a = [sig(symbol="sh600000"), sig(symbol="sh600001")]
    b = [sig(symbol="sh000001"), sig(symbol="sh000002")]
    ms, ns_new, ua = merge.merge_signals(a, b)
    ms2, nb_new, ub = merge.merge_signals(b, a)
    assert ua == 0 and ub == 0 and ns_new == nb_new == 2
    assert {merge.signal_key(r) for r in ms} == {merge.signal_key(r) for r in ms2}


# ── 模型采纳 ──

def test_merge_model_rules():
    cur_fv = 999  # 隔离测试: 显式传 feat_version, 不依赖 online_model 常量
    local = {"feat_version": cur_fv, "trained_at": 100.0}
    # feat_version 不匹配 → 拒绝
    st, reason = merge.merge_model(local, {"feat_version": cur_fv - 1,
                                           "trained_at": 999.0},
                                   feat_version=cur_fv)
    assert st is None and reason == "feat_version_mismatch"
    # 本地较新 → 忽略远端
    st, reason = merge.merge_model(local, {"feat_version": cur_fv,
                                           "trained_at": 50.0}, feat_version=cur_fv)
    assert st is None and reason == "local_newer"
    # 远端较新 → 采纳
    remote = {"feat_version": cur_fv, "trained_at": 200.0, "auc_oos": 0.74}
    st, reason = merge.merge_model(local, remote, feat_version=cur_fv)
    assert reason == "adopted" and st["auc_oos"] == 0.74
    # 空本地 → 直接采纳合法远端
    st, reason = merge.merge_model({}, dict(remote), feat_version=cur_fv)
    assert reason == "adopted"
    # 空远端 → 无动作
    st, reason = merge.merge_model(local, {}, feat_version=cur_fv)
    assert st is None and reason == "remote_empty"


def test_merge_model_trained_ts_alias():
    # 兼容文档中的 trained_ts 字段名
    st, reason = merge.merge_model({"trained_at": 1.0},
                                   {"feat_version": 9, "trained_ts": 5.0},
                                   feat_version=9)
    assert reason == "adopted" and merge.model_trained_ts(st) == 5.0
