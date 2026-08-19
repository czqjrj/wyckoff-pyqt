# -*- coding: utf-8 -*-
"""L4 在线校准模型测试: 特征向量 / 训练 / 样本外门控 / conf 接管。

覆盖:
  - feature_vector: 定长维度、缺失特征安全填充、类型 one-hot。
  - train_model: 带标签+特征记录总能训练; 无标签/无特征不崩。
  - 门控: 样本未达门槛时 apply_model_conf 不改写 conf。
  - 达标: 构造强特征可识别数据, 模型 AUC 达标后接管 conf 且方向正确。
"""
import os
import tempfile

os.environ.setdefault("WYCKOFF_DATA_DIR", tempfile.mkdtemp())

import numpy as np
import pytest

from wyckoff import online_model as om


def _rec(ret, typ="SOS", conf=70, date="2024-06-01", extra=None):
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
    ev = {"type": "SOS", "conf": 50, "feat": _full_feat(2.5)}
    assert om.apply_model_conf([ev]) == 1
    assert ev["conf"] > 50
    # 低 vr 多头事件 → 该信号不可靠, conf 应下降
    ev2 = {"type": "SOS", "conf": 50, "feat": _full_feat(0.5)}
    assert om.apply_model_conf([ev2]) == 1
    assert ev2["conf"] < 50
    # 涨跌停等硬性低置信档保持不动
    ev3 = {"type": "SOS", "conf": 3, "feat": _full_feat(2.5)}
    assert om.apply_model_conf([ev3]) == 0
    assert ev3["conf"] == 3