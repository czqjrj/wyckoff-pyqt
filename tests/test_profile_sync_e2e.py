"""端到端: 两台设备经 MySQL 云后端 (profile_items 表) 同步账户私有数据 (含删除传播)。

无 git / 无网络依赖: 用内存后端桩替换 wyckoff.cloud_db 的读写函数,
每台设备独立 DATA_DIR 与登录态缓存 (account.json)。
"""
import json
import os

import pytest

import wyckoff.account as account
import wyckoff.profile_sync as ps
import wyckoff.storage as storage

_USER = "tester"


class _Cloud:
    """内存版 profile_items: (user, tname) → items dict。"""

    def __init__(self):
        self.items = {}

    def enabled(self):
        return True

    def ensure_schema(self):
        return True

    def read(self, user, tname):
        return dict(self.items.get((user, tname), {}) or {})

    def write(self, user, tname, items):
        self.items[(user, tname)] = dict(items or {})


@pytest.fixture(autouse=True)
def _cloud(monkeypatch):
    backend = _Cloud()
    monkeypatch.setattr(ps.cloud_db, "enabled", backend.enabled)
    monkeypatch.setattr(ps.cloud_db, "ensure_schema", backend.ensure_schema)
    monkeypatch.setattr(ps.cloud_db, "read_profile_items", backend.read)
    monkeypatch.setattr(ps.cloud_db, "write_profile_items", backend.write)
    monkeypatch.setattr(account.cloud_db, "enabled", backend.enabled)
    return backend


def _use_device(tmp_path, name, monkeypatch):
    """切换到某台设备的数据目录, 并写入本机登录态缓存。"""
    dev = os.path.join(tmp_path, name)
    os.makedirs(dev, exist_ok=True)
    monkeypatch.setenv("WYCKOFF_NO_NET", "")
    monkeypatch.setattr(ps, "DATA_DIR", dev)
    monkeypatch.setattr(ps, "SETTINGS_FILE", os.path.join(dev, "wyckoff_settings.json"))
    monkeypatch.setattr(ps, "WATCHLIST_FILE", os.path.join(dev, "wyckoff_watchlist.json"))
    monkeypatch.setattr(ps, "NOTES_FILE", os.path.join(dev, "wx_notes.json"))
    monkeypatch.setattr(ps, "PORTFOLIO_FILE", os.path.join(dev, "wx_portfolio.json"))
    monkeypatch.setattr(ps, "CANDIDATES_FILE", os.path.join(dev, "wyckoff_candidates.json"))
    monkeypatch.setattr(ps, "PAPER_FILE", os.path.join(dev, "wx_paper.json"))
    monkeypatch.setattr(ps, "PROFILE_SHADOW_FILE", os.path.join(dev, "profile_shadow.json"))
    # storage 模块的文件常量在 import 时绑定, 同样按设备重定向,
    # 否则 save_watchlist/load_settings 会写到真实项目数据目录。
    for const, name in (("WATCHLIST_FILE", "wyckoff_watchlist.json"),
                        ("SETTINGS_FILE", "wyckoff_settings.json"),
                        ("NOTES_FILE", "wx_notes.json"),
                        ("PORTFOLIO_FILE", "wx_portfolio.json"),
                        ("CANDIDATES_FILE", "wyckoff_candidates.json")):
        monkeypatch.setattr(storage, const, os.path.join(dev, name))
    monkeypatch.setattr(account, "DATA_DIR", dev)
    monkeypatch.setattr(account, "ACCOUNT_FILE", os.path.join(dev, "account.json"))
    with open(account.ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": {_USER: {"created_ts": 1}}, "current": _USER}, f)
    return dev


def _write(dev, rel, data):
    p = os.path.join(dev, rel)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def _read(dev, rel, default=None):
    p = os.path.join(dev, rel)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def test_e2e_two_devices_sync_and_delete(tmp_path, monkeypatch, _cloud):
    dev_a = _use_device(tmp_path, "a", monkeypatch)
    _write(dev_a, "wyckoff_watchlist.json", ["600104", "000001"])
    st_a = _read(dev_a, "wyckoff_settings.json", {})
    st_a["theme"] = "dark"
    _write(dev_a, "wyckoff_settings.json", st_a)

    # 设备 A: setup → 云端无数据, 首推即初始全量
    r = ps.setup("")
    assert r.get("ok") is True, r
    assert any("600104" in it for it in _cloud.items.values())
    assert any("000001" in it for it in _cloud.items.values())

    # 设备 B: 清空默认自选, 从云端拉取合并 → 应看到 A 的数据
    dev_b = _use_device(tmp_path, "b", monkeypatch)
    _write(dev_b, "wyckoff_watchlist.json", [])
    r = ps.pull_or_push("pull")
    assert r.get("ok") is True, r
    b_watch = _read(dev_b, "wyckoff_watchlist.json", [])
    assert "600104" in b_watch and "000001" in b_watch
    assert _read(dev_b, "wyckoff_settings.json", {}).get("theme") == "dark"

    # 设备 A 删除 000001 并推送; 设备 B 拉取后应删除
    _use_device(tmp_path, "a", monkeypatch)
    _write(dev_a, "wyckoff_watchlist.json", ["600104"])
    r = ps.pull_or_push("push")
    assert r.get("ok") is True, r
    _use_device(tmp_path, "b", monkeypatch)
    r = ps.pull_or_push("pull")
    assert r.get("ok") is True, r
    b_watch2 = _read(dev_b, "wyckoff_watchlist.json", [])
    assert "000001" not in b_watch2
    assert "600104" in b_watch2
