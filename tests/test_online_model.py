"""L4 在线校准模型测试: 特征向量 / 训练 / 样本外门控 / conf 接管。

覆盖:
  - feature_vector: 定长维度、缺失特征安全填充、类型 one-hot。
  - train_model: 带标签+特征记录总能训练; 无标签/无特征不崩。
  - 门控: 样本未达门槛时 apply_model_conf 不改写 conf。
  - 达标: 构造强特征可识别数据, 模型 AUC 达标后接管 conf 且方向正确。
"""
import os
import tempfile

import pytest

os.environ.setdefault("WYCKOFF_DATA_DIR", tempfile.mkdtemp())


from wyckoff import online_model as om


def _rec(ret, typ="Spring", conf=70, date="2024-06-01", extra=None):
    feats = {"vr": 1.5, "rw": 1.2, "cpos": 0.7, "trend": 1, "pos60": 0.8,
             "boll_pct": 0.6, "bw_pct": 40.0, "reson": 2, "dir": 1}
    if extra:
        feats.update(extra)
    return {"kind": "event", "type": typ, "date": date, "conf": conf,
            "features": feats, "results": {"20": {"ret": ret}}}


def _vsa_rec(ret):
    return {"kind": "vsa", "type": "BU", "date": "2024-06-02",
            "features": None, "results": {"20": {"ret": ret}}}


def test_feature_vector_dims_and_fill():
    ev = {"type": "SOS", "conf": 82, "feat": {"vr": 2.0, "dir": 1}}
    x = om.feature_vector(ev)
    assert x.shape == (len(om.FEATURES),)
    # 缺失特征安全填充
    assert x[om._FEAT_INDEX["cpos"]] == 0.0
    assert x[om._FEAT_INDEX["boll_pct"]] == 0.5
    # type one-hot
    ti = om._FEAT_INDEX["type_SOS"]
    assert x[ti] == 1.0
    assert sum(x[om._FEAT_INDEX["type_" + t]] for t in om._EVENT_TYPES) == 1.0


def test_feature_vector_dir_from_type():
    """feat 缺 dir 时从事件类型方向推断 (Spring 多头)."""
    ev = {"type": "Spring", "conf": 60}
    x = om.feature_vector(ev)
    assert x[om._FEAT_INDEX["dir"]] == 1.0


def test_train_model_small_and_gating(tmp_path, monkeypatch):
    """少量样本训练不崩; 未达门槛时不接管 conf。"""
    pytest.importorskip("sklearn")  # sklearn 为可选依赖, 缺失时跳过
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m.json"))
    recs = [_rec(0.1 if k % 2 == 0 else -0.1, date=f"2024-06-{k % 28 + 1:02d}")
            for k in range(10)]
    st = om.train_model(recs)
    assert st["n_labels"] == 10
    assert om.model_status()["ready"] is False
    ev = {"type": "SOS", "conf": 90, "feat": {"dir": 1}}
    assert om.apply_model_conf([ev]) == 0  # 未达门槛 → 不改写
    assert ev["conf"] == 90


def test_train_model_ignores_no_feature_and_vsa():
    """无特征记录 (老数据/VSA) 不参与训练。"""
    recs = [_rec(0.1), _vsa_rec(0.1), {"kind": "event", "type": "SOS",
                                        "date": "2024-01-01", "results": {}}]
    rows = om.labeled_rows(recs)
    assert len(rows) == 1


def test_auc_correct_on_perfect_model():
    """AUC 计算: 概率完全区分正负样本 → 1.0。"""
    y = [1, 1, 1, 0, 0, 0]
    p = [0.9, 0.8, 0.7, 0.3, 0.2, 0.1]
    assert abs(om._auc(y, p) - 1.0) < 1e-9
    # 随机 → 0.5
    y2 = [1, 1, 1, 0, 0, 0]
    p2 = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    assert abs(om._auc(y2, p2) - 0.5) < 1e-9


def test_spearman_guard_constant():
    """常数列不抛异常, 返回 0.0。"""
    assert om._spearman([1, 1, 1], [0.1, 0.2, 0.3]) == 0.0
    # 单调一致 → 高正相关
    s = om._spearman([1, 2, 3, 4], [0.1, 0.3, 0.4, 0.9])
    assert abs(s - 1.0) < 1e-9


def test_train_takeover_conf_direction(tmp_path, monkeypatch):
    """特征强可识别 + 样本达标 → 接管 conf; 多头高 P(up) 推高 conf。"""
    pytest.importorskip("sklearn")  # sklearn 为可选依赖, 缺失时跳过
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m2.json"))
    monkeypatch.setattr(om, "MODEL_MIN_TRAIN", 40)
    monkeypatch.setattr(om, "MODEL_MIN_OOS", 8)
    monkeypatch.setattr(om, "MODEL_MIN_AUC", 0.55)
    # 可识别: 高 vr → 上涨, 低 vr → 下跌
    recs = []
    for k in range(120):
        high = k % 2 == 0
        recs.append(_rec(0.15 if high else -0.15,
                         date=f"2024-{(k // 30) + 1:02d}-{(k % 28) + 1:02d}",
                         extra={"vr": 2.5 if high else 0.5}))
    st = om.train_model(recs)
    assert st["n_train"] >= 40 and st["n_oos"] >= 8
    assert st["auc_oos"] >= 0.55
    assert st["ready"] is True

    def _full_feat(vr):
        return {"vr": vr, "rw": 1.2, "cpos": 0.7, "trend": 1, "pos60": 0.8,
                "boll_pct": 0.6, "bw_pct": 40.0, "reson": 2, "dir": 1}
    # 高 vr 多头事件 → conf 应上升
    ev = {"type": "Spring", "conf": 50, "feat": _full_feat(2.5)}
    assert om.apply_model_conf([ev]) == 1
    assert ev["conf"] > 50
    # 低 vr 多头事件 → 该信号不可靠, conf 应下降
    ev2 = {"type": "Spring", "conf": 50, "feat": _full_feat(0.5)}
    assert om.apply_model_conf([ev2]) == 1
    assert ev2["conf"] < 50
    # 涨跌停等硬性低置信档保持不动
    ev3 = {"type": "Spring", "conf": 3, "feat": _full_feat(2.5)}
    assert om.apply_model_conf([ev3]) == 0
    assert ev3["conf"] == 3

def test_feature_vector_v2_context_fills():
    """v2 语境特征: 缺失按中性填充, 存在则原样映射。"""
    ev = {"type": "SOS", "conf": 70}
    x = om.feature_vector(ev)
    assert x.shape == (len(om.FEATURES),)
    assert x[om._FEAT_INDEX["tr_pos"]] == 0.5
    assert x[om._FEAT_INDEX["rs_pct"]] == 0.5
    assert x[om._FEAT_INDEX["sec_pct"]] == 0.5
    assert x[om._FEAT_INDEX["vol_shrink"]] == 0.25
    assert x[om._FEAT_INDEX["idx_align"]] == 0.0
    ev2 = {"type": "Spring", "conf": 70,
           "feat": {"dir": 1, "ph_acc": 1.0, "tr_pos": 0.2}}
    x2 = om.feature_vector(ev2)
    assert x2[om._FEAT_INDEX["ph_acc"]] == 1.0
    assert x2[om._FEAT_INDEX["tr_pos"]] == 0.2


def test_stale_feat_version_blocks_takeover(tmp_path, monkeypatch):
    """旧特征集版本的状态文件必须静默失效, 等待 v2 重训。"""
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m3.json"))
    st = {"version": 1, "feat_version": 1, "features": ["legacy"],
          "n_train": 500, "n_oos": 50, "auc_oos": 0.9,
          "intercept": 0.1, "coef": [0.0] * len(om.FEATURES),
          "trained_at": 0.0, "horizon": 20, "ready": True,
          "n_labels": 550, "acc_oos": 0.7}
    om._save_state(st)
    ev = {"type": "SOS", "conf": 80, "feat": {"dir": 1}}
    assert om.model_status()["ready"] is False
    assert om.apply_model_conf([ev]) == 0
    assert ev["conf"] == 80


# ── 5.7 模型质量标尺 / 历史轨迹 / 连续劣化 ──

def test_auc_scale_ramp():
    """AUC 质量标尺: 0.50→0, 0.65→1, 线性单调。"""
    assert om._auc_scale(None) == 0.0
    assert om._auc_scale(0.50) == 0.0
    assert om._auc_scale(0.65) == 1.0
    assert om._auc_scale(0.80) == 1.0
    assert om._auc_scale(0.575) == pytest.approx(0.5, abs=1e-9)
    assert om._auc_scale(0.60) > om._auc_scale(0.55)


def test_blend_weight_quality_scaled():
    """接管权重同时受样本量与区分度约束, 不再只随样本数爬坡。"""
    assert om._blend_weight(50, 0.70) == 0.0          # 样本不足
    assert om._blend_weight(2000, None) == 0.0        # 无 AUC → 不取信
    assert om._blend_weight(2000, 0.50) == 0.0        # 随机 → 不取信
    # 0.575 → 标尺 0.5, 规模 0.70 → 0.35
    assert om._blend_weight(2000, 0.575) == 0.35
    # 满分 → 饱和到 MODEL_MAX_BLEND
    assert om._blend_weight(2000, 0.65) == om.MODEL_MAX_BLEND
    # 踩线 0.60 → 权重约 0.47 (而非满权重)
    w_at_floor = om._blend_weight(2000, om.MODEL_MIN_AUC)
    assert om.MODEL_MAX_BLEND * 0.4 < w_at_floor < om.MODEL_MAX_BLEND


def _mk_state(path, hist_aucs, auc, n_train=2000, n_oos=300):
    st = {"version": 3, "feat_version": 3, "features": list(om.FEATURES),
          "n_train": n_train, "n_oos": n_oos, "auc_oos": auc,
          "ic_oos": 0.2, "acc_oos": 0.65, "intercept": 5.0,
          "coef": [0.0] * len(om.FEATURES), "trained_at": 0.0,
          "n_labels": n_train + n_oos,
          "history": [{"auc_oos": a, "n_train": n_train, "ic_oos": 0.2}
                      for a in hist_aucs]}
    om._save_state(st)


def test_degrade_on_consecutive_drop(tmp_path, monkeypatch):
    """AUC 连续劣化 → 折减 0.5 + 告警, 接管幅度显著变小。"""
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m4.json"))
    _mk_state(tmp_path, [0.66, 0.65, 0.64, 0.63, 0.62, 0.60], auc=0.60)
    st = om.model_status()
    assert st["degraded"] is True
    assert any("劣化" in w for w in st["warnings"])
    assert st["blend_eff"] == pytest.approx(st["blend"] * 0.5, abs=1e-4)

    # 全多头高 P(up): 折减后 conf 抬升幅度应明显小于不劣化场景
    ev = {"type": "Spring", "conf": 50, "feat": {"vr": 2.0, "dir": 1}}
    assert om.apply_model_conf([ev]) == 1
    degraded_conf = ev["conf"]

    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m5.json"))
    _mk_state(tmp_path, [0.66] * 6, auc=0.68)  # 高且平稳 → 不劣化
    ev2 = {"type": "Spring", "conf": 50, "feat": {"vr": 2.0, "dir": 1}}
    assert om.apply_model_conf([ev2]) == 1
    assert degraded_conf < ev2["conf"]          # 劣化导致接管幅度减半

    st2 = om.model_status()
    assert st2["degraded"] is False
    assert st2["warnings"] == []


def test_degrade_insensitive_to_few_trainings():
    """历史不足 DEGRADE_LOOKBACK 次时不做劣化判定。"""
    st = {"history": [{"auc_oos": 0.50 + i * 0.01} for i in range(4)]}
    assert om._degrade_factor(st) == 1.0
    assert om._quality_overlay(st)["degraded"] is False


def test_overlay_low_auc_warning():
    """AUC 低于接管下限 → warning, 但不因劣化折减 (未起步接管)。"""
    st = {"auc_oos": 0.52, "history": [{"auc_oos": 0.52}]}
    ov = om._quality_overlay(st)
    assert any("低于接管下限" in w for w in ov["warnings"])
    assert ov["degraded"] is False


def test_history_bounded_and_appended(tmp_path, monkeypatch):
    """每次重训追加历史条目, 截断到 MODEL_HISTORY_MAX。"""
    pytest.importorskip("sklearn")
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m6.json"))
    monkeypatch.setattr(om, "MODEL_MIN_TRAIN", 40)
    monkeypatch.setattr(om, "MODEL_MIN_OOS", 8)
    monkeypatch.setattr(om, "MODEL_MIN_AUC", 0.55)
    recs = []
    for k in range(150):
        high = k % 2 == 0
        recs.append(_rec(0.15 if high else -0.15,
                         date=f"2024-{(k // 30) + 1:02d}-{(k % 28) + 1:02d}",
                         extra={"vr": 2.5 if high else 0.5}))
    st = om.train_model(recs)
    hist = st.get("history") or []
    assert len(hist) == 1
    assert hist[0]["auc_oos"] is not None
    st2 = om.train_model(recs)
    assert len(st2["history"]) == 2
    st3 = om.train_model(recs[:4])  # 少样本分支 (rows<5) 不追加历史
    assert len(st3["history"]) == 2
    assert st3["n_labels"] == 4


# ── 5.7 多 seed 重采样区间 ──

def test_train_multi_seed_interval(tmp_path, monkeypatch):
    """重训产出多 seed 重采样的 AUC 中位数 + 5/95 分位区间。"""
    pytest.importorskip("sklearn")
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m7.json"))
    monkeypatch.setattr(om, "MODEL_MIN_TRAIN", 40)
    monkeypatch.setattr(om, "MODEL_MIN_OOS", 8)
    monkeypatch.setattr(om, "MODEL_MIN_AUC", 0.55)
    recs = []
    for k in range(150):
        high = k % 2 == 0
        recs.append(_rec(0.15 if high else -0.15,
                         date=f"2024-{(k // 30) + 1:02d}-{(k % 28) + 1:02d}",
                         extra={"vr": 2.5 if high else 0.5}))
    st = om.train_model(recs)
    seeds = st["auc_seeds"]
    assert seeds["n"] == om.MODEL_N_SEEDS
    assert seeds["seed_deployed"] == om.MODEL_SEED
    # 中位数必须落在区间内, 且 lo ≤ 中位 ≤ hi
    assert seeds["lo"] is not None and seeds["hi"] is not None
    assert seeds["lo"] <= st["auc_oos"] <= seeds["hi"]
    assert seeds["mean"] is not None and seeds["std"] >= 0.0
    assert seeds["auc_deployed"] is not None
    # 历史轨迹也带上区间
    assert st["history"][0]["auc_lo"] == seeds["lo"]


def test_overlay_ci_straddles_floor(tmp_path, monkeypatch):
    """中位达标但下沿跌破门槛 → 提示性告警 (不关停)。"""
    monkeypatch.setattr(om, "ONLINE_MODEL_FILE", str(tmp_path / "m8.json"))
    _mk_state(tmp_path, [0.66] * 6, auc=om.MODEL_MIN_AUC + 0.02)
    st = om._load_state()
    st["auc_seeds"] = {"lo": om.MODEL_MIN_AUC - 0.05, "hi": om.MODEL_MIN_AUC + 0.08}
    st["auc_oos"] = om.MODEL_MIN_AUC + 0.02
    ov = om._quality_overlay(st)
    assert any("下沿" in w and "不稳固" in w for w in ov["warnings"])
    assert ov["degraded"] is False
