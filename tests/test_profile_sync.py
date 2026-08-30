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
