"""bundle 打包/落库往返测试。"""
import os

import sync.bundle as bundle
import wyckoff.online_model as online_model
import wyckoff.paths as paths
import wyckoff.signal_accuracy as sac
import wyckoff.storage as storage


def isolate(monkeypatch, root):
    """把全部用户数据文件常量重定向到 root (模拟独立机器)。"""
    d = str(root)
    monkeypatch.setattr(paths, "DATA_DIR", d)
    monkeypatch.setattr(storage, "FEEDBACK_FILE", os.path.join(d, "wx_feedback.json"))
    monkeypatch.setattr(storage, "SETTINGS_FILE", os.path.join(d, "wyckoff_settings.json"))
    monkeypatch.setattr(sac, "SIGNAL_ACCURACY_FILE",
                        os.path.join(d, "wx_signal_accuracy.json"))
    monkeypatch.setattr(online_model, "ONLINE_MODEL_FILE",
                        os.path.join(d, "wx_online_model.json"))
    return d


def test_machine_id_persist(tmp_path, monkeypatch):
    d = isolate(monkeypatch, tmp_path)
    m1 = bundle.machine_id()
    assert len(m1) == 32
    assert bundle.machine_id() == m1  # 二次调用稳定
    with open(os.path.join(d, "wx_machine_id"), encoding="utf-8") as f:
        assert f.read().strip() == m1
    # 显式 data_dir 参数不受全局状态影响
    other = bundle.machine_id(data_dir=str(tmp_path / "other"))
    assert other != m1


def test_export_import_roundtrip(tmp_path, monkeypatch):
    from sync import merge_signals

    d = isolate(monkeypatch, tmp_path)
    sigs = [{"symbol": "sh600000", "scale": 240, "kind": "event", "type": "Spring",
             "date": "2026-01-01", "last_eval_ts": 7, "status": "done"},
            {"symbol": "sh600001", "scale": 240, "kind": "event", "type": "SOS",
             "date": "2026-01-02", "last_eval_ts": 0, "status": "pending"}]
    fbs = [{"symbol": "sh600000", "scale": 240, "start_dt": "s", "end_dt": "e",
            "label": "accumulation", "verdict": "correct", "date": "2026-08-01"}]
    sac.save_signals(sigs)
    storage.save_feedback(fbs)
    b = bundle.export_bundle()
    assert b["schema"] == bundle.SCHEMA_VERSION
    assert len(b["signals"]) == 2 and len(b["feedback"]) == 1
    assert b["machine"] == bundle.machine_id()

    # 清空本地后导入 → 全量恢复且计数守恒
    sac.save_signals([])
    storage.save_feedback([])
    counts = bundle.import_bundle(b)
    assert counts["signals_new"] == 2 and counts["feedback_new"] == 1
    assert counts["n_total_signals"] == 2
    assert sac.load_signals() == merge_signals(sigs, sigs)[0]
    assert storage.load_feedback()[0]["verdict"] == "correct"

    # 幂等: 再次导入零变化
    again = bundle.import_bundle(b)
    assert again["signals_new"] == 0 and again["signals_upd"] == 0
    assert os.path.isdir(d)  # 数据目录正常存在
