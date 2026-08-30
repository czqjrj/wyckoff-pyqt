"""端到端: 两台设备经私有 Git 仓同步账户私有数据 (含删除传播)。"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(ROOT, "wyckoff", "profile_sync.py")

# 测试用 git 身份 (避免依赖全局配置)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "WYCKOFF_NO_NET": "",   # 允许真实 git 操作
}


def _run(cli_args, data_dir):
    env = dict(os.environ)
    env.update(_GIT_ENV)
    env["WYCKOFF_DATA_DIR"] = data_dir
    env["WYCKOFF_NO_NET"] = ""
    p = subprocess.run([sys.executable, "-m", "wyckoff.profile_sync"] + cli_args,
                       capture_output=True, text=True, env=env, cwd=ROOT,
                       timeout=180)
    return p


def _setup_bare_repo(tmp_path):
    bare = os.path.join(tmp_path, "bare.git")
    subprocess.run(["git", "init", "--bare", "-q", bare], check=True)
    return bare


def _write(data_dir, rel, data):
    p = os.path.join(data_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return p


def _read(data_dir, rel, default=None):
    p = os.path.join(data_dir, rel)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def test_e2e_two_devices_sync_and_delete(tmp_path):
    bare = _setup_bare_repo(tmp_path)
    dev_a = os.path.join(tmp_path, "a")
    os.makedirs(dev_a, exist_ok=True)
    dev_b = os.path.join(tmp_path, "b")
    os.makedirs(dev_b, exist_ok=True)

    # 设备 A: 自选 [600104, 000001]，设置主题 dark
    _write(dev_a, "wyckoff_watchlist.json", ["600104", "000001"])
    st_a = _read(dev_a, "wyckoff_settings.json", {})
    st_a["theme"] = "dark"
    _write(dev_a, "wyckoff_settings.json", st_a)

    # A 初始化并推送
    r = _run(["setup", bare], dev_a)
    assert r.returncode == 0, r.stderr
    assert '"ok": true' in r.stdout

    # 设备 B: 相同仓 setup（clone），清空默认自选看合并
    _write(dev_b, "wyckoff_watchlist.json", [])
    r = _run(["setup", bare], dev_b)
    assert r.returncode == 0, r.stderr
    # B clone 后其 bundle 已包含 A 的数据(经 clone→apply? setup 只 clone+commit collect)。
    # 主动拉取一次以应用远端数据
    r = _run(["pull"], dev_b)
    assert r.returncode == 0, r.stderr
    b_watch = _read(dev_b, "wyckoff_watchlist.json", [])
    assert "600104" in b_watch and "000001" in b_watch
    b_st = _read(dev_b, "wyckoff_settings.json", {})
    assert b_st.get("theme") == "dark"

    # 设备 A 删除 000001, 推送; 设备 B 拉取后应删除
    _write(dev_a, "wyckoff_watchlist.json", ["600104"])
    r = _run(["push"], dev_a)
    assert r.returncode == 0, r.stderr
    r = _run(["pull"], dev_b)
    assert r.returncode == 0, r.stderr
    b_watch2 = _read(dev_b, "wyckoff_watchlist.json", [])
    assert "000001" not in b_watch2
    assert "600104" in b_watch2
