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


def test_apply_profile_persists_shadow_for_later_pull(tmp_path):
    """从云下载应用合并结果后, 影子必须同步到磁盘状态。

    回归背景: apply 后影子若停留在拉取前的旧值, 下一次 collect 会把刚拉下来的
    云端版本误判为本地新变更(打 now 时间戳), LWW 合并中文档反而被本机覆盖,
    「从云下载」的模拟盘数据无法稳定生效。
    """
    m = _reload_modules(tmp_path)
    # 1) 本地先有一份旧模拟盘状态并建立影子
    old = {"cash": 1_000_000, "positions": [], "closed": [],
           "orders": [], "candidates": [], "pending": [],
           "conditions": [], "equity_hist": [], "meta": {}}
    _write(tmp_path, "wx_paper.json", old)
    st = m._collect_type("paper")
    assert st["paper"]["v"] == old
    old_ts = st["paper"]["ts"]

    # 2) 云端已有更新 (远端版本号更晚)
    new_state = dict(old)
    new_state["closed"] = [{"symbol": "600000", "buy_px": 10.0,
                            "sell_px": 11.0, "ret": 0.1, "reason": "stop_loss",
                            "type": "Spring", "strategy": "paper_discipline_bull",
                            "bars": 5, "close_ts": "2026-09-07",
                            "name": "浦发银行", "qty": 1000}]
    bundle = {"schema": m.SCHEMA, "types": {"paper": {"items": {
        "paper": {"v": new_state, "ts": old_ts + 10.0}}}}}

    # 3) 应用合并且影子同步为新值 → 磁盘与影子一致
    r = m.apply_profile(bundle)
    assert r["changed"] is True
    with open(os.path.join(tmp_path, "wx_paper.json"), encoding="utf-8") as f:
        assert json.load(f)["closed"]  == new_state["closed"]
    shadow = m._load_shadow()["paper"]
    assert shadow["paper"]["v"] == new_state
    assert shadow["paper"]["ts"] == old_ts + 10.0

    # 4) 磁盘未再变 → 再 collect 不再打新 ts, 远端版号得以保留
    st2 = m._collect_type("paper")
    assert st2["paper"]["v"] == new_state
    assert st2["paper"]["ts"] == old_ts + 10.0


def test_paper_write_skipped_keeps_shadow_and_no_clobber(tmp_path):
    """同步遇模拟盘周期锁让位 (paper 写回被跳过) 时, 不得污染影子。

    回归背景: 写回被跳过 (跨进程锁被进行中的周期占用) 却仍把合并结果写进影子,
    下次 collect 会把仍未落盘的旧盘误判为本地新变更 (打 now 时间戳), 在 LWW 中
    反超并覆盖云端更新, 造成「模拟盘同步抖动」— 从云端/另一设备拉取的新版本
    被本机旧数据回吐顶掉。修复后: 被跳过的类型保留旧影子, 云端更新留待锁空闲
    时正常生效。
    """
    m = _reload_modules(tmp_path)
    old = {"cash": 1_000_000, "positions": [], "closed": [],
           "orders": [], "candidates": [], "pending": [],
           "conditions": [], "equity_hist": [], "meta": {}}
    _write(tmp_path, "wx_paper.json", old)
    st = m._collect_type("paper")
    old_ts = st["paper"]["ts"]

    # 云端已有更新 (另一设备较新版本, ts 更晚)
    newer = dict(old)
    newer["closed"] = [{"symbol": "600000", "ret": 0.1, "reason": "stop_loss"}]
    remote = {"schema": m.SCHEMA, "types": {"paper": {"items": {
        "paper": {"v": newer, "ts": old_ts + 50}}}}}

    # 1) 模拟盘跨进程锁被进行中的周期占用 → 同步让位, 写回被跳过
    orig_lock = m._paper_lock
    m._paper_lock = lambda timeout=3.0: None
    r = m.apply_profile(remote)
    assert r["paper_skipped"] is True
    # 影子不得被未落盘的合并值污染
    shad = m._load_shadow()["paper"]
    assert shad["paper"]["v"] == old
    assert shad["paper"]["ts"] == old_ts
    # 磁盘保持本地旧盘
    with open(os.path.join(tmp_path, "wx_paper.json"), encoding="utf-8") as f:
        assert json.load(f) == old

    # 2) 锁空闲后同步 → 云端新版本正常生效, 不被旧盘回吐覆盖
    m._paper_lock = orig_lock
    st2 = m._collect_type("paper")
    assert st2["paper"]["v"] == old
    assert st2["paper"]["ts"] == old_ts, "未落盘的旧盘不得被误打新时间戳"
    merged = m._merge_items(st2, remote["types"]["paper"]["items"])
    assert merged["paper"]["v"] == newer
    r2 = m.apply_profile(
        {"schema": m.SCHEMA, "types": {"paper": {"items": merged}}})
    assert r2["changed"] is True and r2["paper_skipped"] is False
    with open(os.path.join(tmp_path, "wx_paper.json"), encoding="utf-8") as f:
        assert json.load(f)["closed"] == newer["closed"]


def test_no_net_guards_ops():
    assert ps._no_net() is True
    r = ps.sync_once()
    assert r.get("ok") is False
    assert "离线" in r.get("error", "")


def test_first_sync_settings_default_not_override_remote(tmp_path):
    """新设备首次同步: 本地默认 UI 值不得覆盖云端真实配置。

    回归背景: 影子为空(首次)时, 本地每个 settings 键都被打上当前时间戳,
    导致 LWW 合并时本地默认值(时间新)胜过云端真实配置, 新设备拉不到已配置的
    UI 设置 (如深色主题)。修复后: 影子无记录的键若本地值仍是出厂默认,
    用保守 ts=0, 让云端值在合并中胜出。
    """
    m = _reload_modules(tmp_path)
    # 云端 bundle: 用户已配置 theme=dark
    remote = {
        "schema": m.SCHEMA,
        "types": {"settings": {"items": {
            "theme": {"v": "dark", "ts": 100},
        }}},
    }
    # 本地新设备: 默认值 light (未改过), 首 collect 无影子
    st = m._collect_type("settings")
    assert st["theme"]["v"] == "light", "本地默认应为 light"
    assert st["theme"]["ts"] == 0.0, "默认值首同步应打保守时间戳"

    # 合并后云端 dark 胜出
    rt = remote["types"]["settings"]["items"]
    merged = m._merge_items(st, rt)
    assert merged["theme"]["v"] == "dark"

    # 用户已改过非默认值: 首同步应打 now 时间戳(能上云)
    m2 = _reload_modules(tmp_path)
    import importlib
    storage = importlib.import_module("wyckoff.storage")
    s = storage.load_settings()
    s["theme"] = "custom-solarized"
    storage.save_settings(s)
    st2 = m2._collect_type("settings")
    assert st2["theme"]["v"] == "custom-solarized"
    assert st2["theme"]["ts"] > 0, "用户改过的值首同步应打当前时间戳"


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


def _paper_state(cash):
    return {"cash": cash, "positions": [], "closed": [], "orders": [],
            "candidates": [], "pending": [], "conditions": [],
            "equity_hist": [], "meta": {}}


def test_pull_paper_remote_overrides_local(tmp_path):
    """「从云下载」对模拟盘采用远端优先: 云端有状态则整包覆盖本地。

    回归背景: 模拟盘高频自更新 (周期每步写盘), 本地盘与影子几乎总不一致,
    collect 打新时间戳使 LWW 里云端永远输掉 → 下载后 UI 不更新。
    """
    m = _reload_modules(tmp_path)
    local = {"paper": {"v": _paper_state(1_000_000), "ts": 999.0}}
    remote = {"paper": {"v": _paper_state(2_000_000), "ts": 1000.0}}
    out = m._pull_merge_type("paper", local, remote)
    assert out == remote, "云端有模拟盘时应整包覆盖本地"

    # 其余类型仍走 LWW 合并 (自选股本地删除不被云端复活)
    wl_local = {"600104": {"v": "600104", "ts": 5000.0},
                "300750": {"v": None, "ts": 6000.0}}
    wl_remote = {"600104": {"v": "600104", "ts": 1.0},
                 "300750": {"v": "300750", "ts": 2.0}}
    out2 = m._pull_merge_type("watchlist", wl_local, wl_remote)
    assert out2["300750"]["v"] is None, "paper 改为远端优先不得影响其它类型 LWW"


def test_pull_paper_remote_empty_keeps_local(tmp_path):
    """云端无模拟盘数据时, 从云下载不得清空本地模拟盘。"""
    m = _reload_modules(tmp_path)
    local = {"paper": {"v": _paper_state(1_000_000), "ts": 999.0}}
    out = m._pull_merge_type("paper", local, {})
    assert out == local


def test_pull_merge_type_equal_remote_wins(tmp_path):
    """远端优先下, ts 相同 (含本地被污染打同秒) 也以远端覆盖为准。"""
    m = _reload_modules(tmp_path)
    local = {"paper": {"v": _paper_state(1_000_000), "ts": 500.0}}
    remote = {"paper": {"v": _paper_state(3_000_000), "ts": 500.0}}
    out = m._pull_merge_type("paper", local, remote)
    assert out == remote


def test_cloud_pull_paper_overwrites_polluted_local(tmp_path, monkeypatch):
    """端到端: 本地盘被周期写入污染(与影子不一致)后, 从云下载仍用云端整包覆盖。"""
    import wyckoff.cloud_db as cdb

    m = _reload_modules(tmp_path)
    old = _paper_state(1_000_000)
    _write(tmp_path, "wx_paper.json", old)
    m._collect_type("paper")  # 建立本地影子 (首次: ts=now)

    # 周期随后又写盘 → 本地盘与影子不一致 → collect 会打新时间戳 (复现污染)
    _write(tmp_path, "wx_paper.json", _paper_state(1_500_000))

    # 云端有另一设备更新的模拟盘
    remote_v = _paper_state(2_000_000)
    cdb_original = cdb.read_profile_items
    monkeypatch.setattr(
        cdb, "read_profile_items",
        lambda user, t: {"paper": {"v": remote_v, "ts": 12345.0}}
        if t == "paper" else {})
    monkeypatch.setattr("wyckoff.account.current_user", lambda: "testuser")

    res = m._cloud_pull()
    assert res.get("ok") is True
    with open(os.path.join(tmp_path, "wx_paper.json"), encoding="utf-8") as f:
        assert json.load(f)["cash"] == 2_000_000, "云端模拟盘应整包覆盖本地"
    shad = m._load_shadow()["paper"]
    assert shad["paper"]["v"] == remote_v
    assert shad["paper"]["ts"] == 12345.0
