"""校准中心「数据同步」区离屏冒烟测试 (P4)。

覆盖: 控件构建 / 状态行渲染 / setup+sync 按钮路径 (bare 仓库当远端)。
"""
import json
import os
import subprocess

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

import sync.transport as transport  # noqa: E402
import wyckoff.online_model as online_model  # noqa: E402
import wyckoff.paths as paths  # noqa: E402
import wyckoff.signal_accuracy as sac  # noqa: E402
import wyckoff.storage as storage  # noqa: E402
from desktop.calibration_center import CalibrationCenter  # noqa: E402


@pytest.fixture()
def app():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def isolate(monkeypatch, root):
    d = str(root)
    monkeypatch.setattr(paths, "DATA_DIR", d)
    monkeypatch.setattr(storage, "FEEDBACK_FILE", os.path.join(d, "wx_feedback.json"))
    monkeypatch.setattr(storage, "SETTINGS_FILE", os.path.join(d, "wyckoff_settings.json"))
    monkeypatch.setattr(sac, "SIGNAL_ACCURACY_FILE",
                        os.path.join(d, "wx_signal_accuracy.json"))
    monkeypatch.setattr(online_model, "ONLINE_MODEL_FILE",
                        os.path.join(d, "wx_online_model.json"))
    return d


def _sig(sym):
    return {"symbol": sym, "scale": 240, "kind": "event", "type": "Spring",
            "date": "2026-01-01", "created_ts": 1.0, "last_eval_ts": 0,
            "status": "pending"}


def test_sync_section_build_and_status_line(tmp_path, monkeypatch, app):
    isolate(monkeypatch, tmp_path)
    dlg = CalibrationCenter()
    # 数据同步区控件存在且状态行为引导文案
    assert dlg._sync_url_edit is not None
    assert dlg._sync_btn is not None
    assert "尚未同步" in dlg._sync_status.text()
    # settings 有同步摘要后状态行刷新
    s = storage.load_settings()
    s["calib_last_sync"] = {"ts": 1787000000, "signals_new": 3, "feedback_new": 1}
    storage.save_settings(s)
    dlg._render_sync_status()
    assert "本次新增 4 条" in dlg._sync_status.text()


def test_sync_now_button_end_to_end(tmp_path, monkeypatch, app, qtbot=None):
    """点「立即同步」走完整线程流程并刷新状态行。"""
    monkeypatch.delenv("WYCKOFF_NO_NET", raising=False)
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", origin], check=True, capture_output=True)
    subprocess.run(["git", "-C", origin, "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True, capture_output=True)
    isolate(monkeypatch, tmp_path)
    sac.save_signals([_sig("sh600000")])

    dlg = CalibrationCenter()
    dlg._sync_url_edit.setText(origin)
    # 屏蔽空 URL 弹窗分支 (此处 URL 非空, 不会触发)
    dlg._on_sync_now()
    th = dlg._sync_th
    assert th is not None
    th.wait(60000)  # 后台线程完成 (git clone/push 本地 bare 仓很快)
    for _ in range(10):  # 队列信号投递到主线程槽
        app.processEvents()
        if "尚未同步" not in dlg._sync_status.text():
            break
        import time

        time.sleep(0.05)
    rec = storage.load_settings().get("calib_last_sync") or {}
    assert rec.get("ok") is True
    assert "同步完成" in dlg._sync_status.text()
    # 远端 canonical 已写入
    meta = json.load(open(os.path.join(transport.repo_dir(), "meta.json"),
                          encoding="utf-8"))
    assert meta["counts"]["signals"] == 1


def test_sync_setup_empty_url_warns(tmp_path, monkeypatch, app):
    isolate(monkeypatch, tmp_path)
    dlg = CalibrationCenter()
    dlg._sync_url_edit.setText("")
    called = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: called.setdefault("warned", True))
    dlg._on_sync_setup()
    assert called.get("warned")


def test_sync_url_from_settings(tmp_path, monkeypatch, app):
    """settings 已保存仓库 URL → 打开校准中心自动填入。"""
    isolate(monkeypatch, tmp_path)
    s = storage.load_settings()
    s["calib_repo_url"] = "git@github.com:user/wyckoff-calib.git"
    storage.save_settings(s)
    dlg = CalibrationCenter()
    assert dlg._sync_url_edit.text() == "git@github.com:user/wyckoff-calib.git"


def test_sync_url_autofill_from_repo_remote(tmp_path, monkeypatch, app):
    """settings 无 URL 但数据目录已有 clone 仓库 → 从 git remote 自动填充。"""
    isolate(monkeypatch, tmp_path)
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", origin], check=True, capture_output=True)
    rdir = os.path.join(str(tmp_path), "calib_repo")
    subprocess.run(["git", "clone", origin, rdir], check=True, capture_output=True)
    dlg = CalibrationCenter()
    assert dlg._sync_url_edit.text() == origin


def test_sync_url_refresh_picks_up_later_setup(tmp_path, monkeypatch, app):
    """窗口已打开后再执行 setup → refresh_sync_url 能把地址补上。"""
    isolate(monkeypatch, tmp_path)
    dlg = CalibrationCenter()
    assert dlg._sync_url_edit.text() == ""
    s = storage.load_settings()
    s["calib_repo_url"] = "git@github.com:user/wyckoff-calib.git"
    storage.save_settings(s)
    dlg.refresh_sync_url()
    assert dlg._sync_url_edit.text() == "git@github.com:user/wyckoff-calib.git"
