"""自动同步触发机制测试 (P: 校准数据变更后去抖同步)。

覆盖:
  - sync.auto 变更标记: notify → pending/last_change_ts; reset 清空
  - 落盘钩子: save_signals / save_feedback 写库后自动置脏
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

import wyckoff.online_model as online_model  # noqa: E402
import wyckoff.paths as paths  # noqa: E402
import wyckoff.signal_accuracy as sac  # noqa: E402
import wyckoff.storage as storage  # noqa: E402
from sync import auto as sync_auto  # noqa: E402


def _isolate(monkeypatch, root):
    d = str(root)
    monkeypatch.setattr(paths, "DATA_DIR", d)
    monkeypatch.setattr(storage, "FEEDBACK_FILE", os.path.join(d, "wx_feedback.json"))
    monkeypatch.setattr(storage, "SETTINGS_FILE", os.path.join(d, "wyckoff_settings.json"))
    monkeypatch.setattr(sac, "SIGNAL_ACCURACY_FILE",
                        os.path.join(d, "wx_signal_accuracy.json"))
    monkeypatch.setattr(online_model, "ONLINE_MODEL_FILE",
                        os.path.join(d, "wx_online_model.json"))
    return d


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    sync_auto.reset()
    yield
    sync_auto.reset()


def test_notify_pending_reset():
    assert sync_auto.pending() is False
    sync_auto.notify_change("signals")
    assert sync_auto.pending() is True
    assert sync_auto.last_change_ts() > 0
    sync_auto.reset()
    assert sync_auto.pending() is False


def test_save_signals_marks_dirty():
    sac.save_signals([{"symbol": "sh600000", "type": "Spring"}])
    assert sync_auto.pending() is True
    sync_auto.reset()
    assert sync_auto.pending() is False


def test_save_feedback_marks_dirty():
    storage.save_feedback([{"symbol": "600000", "verdict": "correct"}])
    assert sync_auto.pending() is True


def test_notify_thread_safe():
    import threading

    errors = []
    def worker():
        try:
            for _ in range(50):
                sync_auto.notify_change("signals")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sync_auto.pending() is True
