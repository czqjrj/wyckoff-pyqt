"""P2 Git 传输 + CLI 端到端测试: tmp_path 内 git init --bare 当远端。

覆盖: 双机互推互拉 / push 被拒后重拉合并重推 / WYCKOFF_NO_NET 静默跳过。
conftest 全局置 WYCKOFF_NO_NET=1, git 用例内显式解除。
"""
import json
import os
import subprocess

import pytest

import sync.service as service
import sync.transport as transport
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


def allow_net(monkeypatch):
    monkeypatch.delenv("WYCKOFF_NO_NET", raising=False)


def sig(symbol="sh600000", date="2026-01-01", last_eval_ts=0):
    return {"symbol": symbol, "scale": 240, "kind": "event", "type": "Spring",
            "date": date, "created_ts": 1.0, "last_eval_ts": last_eval_ts,
            "status": "pending"}


@pytest.fixture()
def origin(tmp_path):
    """空 bare 仓库当共享远端; HEAD 钉到 main 与传输层约定一致。"""
    p = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(p)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True, capture_output=True)
    return str(p)


def _repo_signals(rdir):
    with open(os.path.join(rdir, "signals.json"), encoding="utf-8") as f:
        return json.load(f)


# ── 双机流程 ──

def test_two_machine_push_pull_sync(tmp_path, monkeypatch, origin):
    allow_net(monkeypatch)
    a = isolate(monkeypatch, tmp_path / "machine_a")
    sac.save_signals([sig("sh600000")])
    st = service.setup(origin)
    assert st["cloned"] and os.path.isdir(os.path.join(a, "calib_repo", ".git"))
    assert service.push()["pushed"] is True

    # B 首次同步: 拿到 A 的数据并把自己的推上去
    b = isolate(monkeypatch, tmp_path / "machine_b")
    sac.save_signals([sig("sh600001")])
    service.setup(origin)
    r = service.sync(retrain=False)
    assert r["ok"] and r["signals_new"] == 1
    syms = {s["symbol"] for s in sac.load_signals()}
    assert syms == {"sh600000", "sh600001"}
    assert {s["symbol"] for s in _repo_signals(os.path.join(b, "calib_repo"))} == syms

    # A 再 pull → 收到 B 的记录
    isolate(monkeypatch, tmp_path / "machine_a")
    counts = service.pull()
    assert counts["signals_new"] == 1
    syms = {s["symbol"] for s in sac.load_signals()}
    assert syms == {"sh600000", "sh600001"}


def test_setup_persists_url_and_status(tmp_path, monkeypatch, origin):
    allow_net(monkeypatch)
    isolate(monkeypatch, tmp_path / "m")
    service.setup(origin)
    assert storage.load_settings()["calib_repo_url"] == origin
    st = service.status()
    assert st["url"] == origin and st["repo_cloned"]
    assert st["remote_counts"]["signals"] == 0  # 尚无 canonical 文件时按空处理


# ── push 被拒 → 重拉合并重推 ──

def test_push_rejected_then_retry_merges(tmp_path, monkeypatch, origin):
    allow_net(monkeypatch)
    # A 首推建立基线 (sig_a)
    isolate(monkeypatch, tmp_path / "a")
    sac.save_signals([sig("sh600000")])
    service.setup(origin)
    service.push()

    # C: 第三方克隆, 稍后制造竞争提交
    c_clone = str(tmp_path / "competitor")
    subprocess.run(["git", "clone", "--quiet", origin, c_clone], check=True)

    # B 同步一次拿到基线
    b_root = tmp_path / "b"
    isolate(monkeypatch, b_root)
    service.setup(origin)
    service.sync(retrain=False)
    base_n = len(sac.load_signals())

    # C 推一条新记录 (本地文件层面模拟), 但先不推 —— 等 B 提交后再推
    subprocess.run(["git", "-C", c_clone, "pull", "-q", "--ff-only",
                    "origin", "main"], check=True)  # 对齐到含 B 数据的最新
    with open(os.path.join(c_clone, "signals.json"), encoding="utf-8") as f:
        c_sigs = json.load(f)
    c_sigs.append(sig("sh999999"))
    with open(os.path.join(c_clone, "signals.json"), "w", encoding="utf-8") as f:
        json.dump(c_sigs, f, ensure_ascii=False)
    subprocess.run(["git", "-C", c_clone, "add", "-A"], check=True)
    subprocess.run(["git", "-C", c_clone, "commit", "-qm", "c: add sig"],
                   check=True)

    # 在 B 的第一次 commit 之后、push 之前注入 C 的竞争推送
    orig_commit = transport.commit_all
    state = {"injected": False}

    def racing_commit(repo, message):
        changed = orig_commit(repo, message)
        if not state["injected"]:
            state["injected"] = True
            subprocess.run(["git", "-C", c_clone, "push", "-q", "origin", "HEAD"],
                           check=True)
        return changed

    monkeypatch.setattr(transport, "commit_all", racing_commit)
    r = service.sync(retrain=False)
    assert r["ok"] is True
    assert r.get("push_retries") == 1  # 第一次被拒后重试成功
    assert r["signals_new"] == 1  # 重试轮把 sh999999 合并进本地
    syms = {s["symbol"] for s in sac.load_signals()}
    assert "sh999999" in syms and len(syms) == base_n + 1
    final = _repo_signals(transport.repo_dir())  # 此刻 DATA_DIR 仍指向 B
    assert {s["symbol"] for s in final} >= {"sh600000", "sh999999"}


# ── 离线门控 ──

def test_no_net_skips_git(tmp_path, monkeypatch, origin):
    monkeypatch.setenv("WYCKOFF_NO_NET", "1")
    d = isolate(monkeypatch, tmp_path / "m")
    storage.save_settings({"calib_repo_url": origin})
    r = service.sync()
    assert r == {"skipped": "WYCKOFF_NO_NET=1"}
    assert not os.path.isdir(os.path.join(d, "calib_repo"))


# ── CLI ──

def test_cli_status_without_url(capsys):
    from sync.__main__ import main

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert '"url": ""' in out


def test_cli_sync_without_url_fails():
    from sync.__main__ import main

    assert main(["sync"]) == 2  # SyncError → 退出码 2
