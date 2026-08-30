"""account 私有数据同步核心逻辑测试。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["WYCKOFF_NO_NET"] = "1"

import wyckoff.profile_sync as ps


def _reload_modules(tmp_path):
    """顺序重载受 DATA_DIR 影响的模块, 让路径常量跟随测试临时目录。"""
    os.environ["WYCKOFF_DATA_DIR"] = str(tmp_path)
    import importlib
    for m in ("wyckoff.paths", "wyckoff.storage", "wyckoff.profile_sync"):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    return importlib.import_module("wyckoff.profile_sync")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_DATA_DIR", str(tmp_path))
    return _reload_modules(tmp_path)


def _write(tmp, rel, data):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def test_settings_whitelist_extracts_domains(tmp_path):
    m = _reload_modules(tmp_path)
    s = {k: f"v-{k}" for k in m.SETTINGS_WHITELIST}
    s["ai_api_key"] = "sk-secret123"
    s["calib_repo_url"] = "git@x"
    _write(tmp_path, "wyckoff_settings.json", s)
    state = m._read_settings_state()
    assert "ai_api_key" not in state
    assert "calib_repo_url" not in state
    for k in m.SETTINGS_WHITELIST:
        assert state.get(k) == f"v-{k}"


def test_watchlist_union_and_delete(tmp_path):
    m = _reload_modules(tmp_path)
    _write(tmp_path, "wyckoff_watchlist.json", ["600104", "000001"])
    m._collect_type("watchlist")
    assert "watchlist" in m._load_shadow()
    _write(tmp_path, "wyckoff_watchlist.json", ["000001"])
    st = m._collect_type("watchlist")
    assert st.get("600104", {}).get("v") is None
    assert st["600104"]["ts"] >= 0
    assert st["000001"]["v"] == "000001"


def test_merge_newer_wins_and_tombstone():
    local = {
        "A": {"v": "a1", "ts": 100},
        "B": {"v": "b1", "ts": 200},
        "C": {"v": None, "ts": 300},
    }
    remote = {
        "A": {"v": "a2", "ts": 150},
        "B": {"v": "b2", "ts": 100},
        "C": {"v": "c1", "ts": 200},
    }
    merged = ps._merge_items(local, remote)
    assert merged["A"]["v"] == "a2"
    assert merged["B"]["v"] == "b1"
    assert merged["C"]["v"] is None


def test_merge_tiebreak_deterministic():
    local = {"X": {"v": "l", "ts": 100}}
    remote = {"X": {"v": "r", "ts": 100}}
    a = ps._merge_items(local, remote)
    b = ps._merge_items(dict(local), dict(remote))
    assert a == b
    assert a["X"]["v"] == "r"


def test_merge_empty_vs_value():
    local = {"Y": {"v": None, "ts": 50}}
    remote = {"Y": {"v": "val", "ts": 50}}
    merged = ps._merge_items(local, remote)
    assert merged["Y"]["v"] == "val"


def test_apply_profile_writes_watchlist_and_settings(tmp_path):
    m = _reload_modules(tmp_path)
    _write(tmp_path, "wyckoff_watchlist.json", ["600104"])
    bundle = {
        "schema": 1,
        "types": {
            "watchlist": {"items": {
                "600104": {"v": "600104", "ts": 1},
                "000001": {"v": "000001", "ts": 2},
                "300750": {"v": None, "ts": 3},
            }},
            "settings": {"items": {
                "theme": {"v": "dark", "ts": 1},
            }},
        },
    }
    r = m.apply_profile(bundle)
    assert r["changed"] is True
    wl = m._read_watchlist()
    assert "600104" in wl and "000001" in wl and "300750" not in wl
    assert m._read_settings_state().get("theme") == "dark"


def test_collect_detects_add_delete_and_persists_shadow(tmp_path):
    m = _reload_modules(tmp_path)
    _write(tmp_path, "wyckoff_watchlist.json", ["600104"])
    m._collect_type("watchlist")
    _write(tmp_path, "wyckoff_watchlist.json", ["600104", "000001"])
    st = m._collect_type("watchlist")
    shad = m._load_shadow()["watchlist"]
    assert st["000001"]["ts"] > 0
    assert st["600104"]["ts"] == shad["600104"]["ts"]


def test_no_net_guards_git_ops():
    assert ps._no_net() is True
    assert ps._git(["status"]) == ("", 0)


def test_pull_merge_preserves_local_delete(tmp_path):
    """「从云下载」不能盲目用远端覆盖本地删除。

    回归背景: 本地删除某自选股后, 从云下载曾用含该股的远端 bundle 直接覆盖,
    导致被删除的股票复活。修复后 pull 需先按影子收集本地变更(含删除 tombstone)
    再做 LWW 合并, 使本地已删除条目不被云端旧数据拉回。
    """
    m = _reload_modules(tmp_path)
    # 1) 初始本地含 600104 + 300750, 建立影子
    _write(tmp_path, "wyckoff_watchlist.json", ["600104", "300750"])
    m._collect_type("watchlist")

    # 2) 用户删除 300750: 本地仅剩 600104
    _write(tmp_path, "wyckoff_watchlist.json", ["600104"])
    local = m._collect_type("watchlist")
    assert local["300750"].get("v") is None, "删除应产出 tombstone"

    # 3) 但远端 bundle 里 300750 仍存在(旧数据)
    remote = {
        "schema": m.SCHEMA,
        "types": {"watchlist": {"items": {
            "600104": {"v": "600104", "ts": 1},
            "300750": {"v": "300750", "ts": 2},
        }}},
    }

    # 4) 按新 pull 逻辑合并: 本地删除(ts 为 now 较新)必须胜出
    rt = remote["types"]["watchlist"]["items"]
    merged = m._merge_items(local, rt)
    assert merged["300750"]["v"] is None, "删除的股票不能被远程旧数据复活"

    # 5) 应用后磁盘无 300750
    m.apply_profile({"schema": m.SCHEMA, "types": {"watchlist": {"items": merged}}})
    wl = m._read_watchlist()
    assert "300750" not in wl
    assert "600104" in wl
