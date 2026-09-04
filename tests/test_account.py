"""账户登录态(用户名+密码)测试。

离线 (WYCKOFF_NO_NET=1) 语义:
- register / change_password / change_username 需写仓 → 直接拒绝。
- login 不联网, 回退本机缓存账号校验(不验证密码), 用于离线可用。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["WYCKOFF_NO_NET"] = "1"

import wyckoff.account as acc


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WYCKOFF_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(acc, "ACCOUNT_FILE", os.path.join(str(tmp_path), "account.json"))
    return acc


def _seed(accounts, current=""):
    """直接写入本机登录态缓存, 模拟此前登录过的账户。"""
    state = {"accounts": accounts, "current": current}
    acc.save_accounts(state)


def _entry(user):
    return {"created_ts": 1, "last_login": 1, "display": user}


def test_register_offline_rejected(_iso):
    ok, msg = acc.register("alice", "secret1")
    assert not ok
    assert "离线" in msg


def test_login_cached_sets_current_and_status(_iso):
    _seed({"alice": _entry("alice")})
    ok, msg = acc.login("alice", "anything")  # 离线缓存路径不校验密码
    assert ok
    assert "本机缓存" in msg
    assert acc.current_user() == "alice"
    st = acc.status()
    assert st["logged_in"] is True
    assert st["current"] == "alice"
    assert st["no_net"] is True


def test_login_unknown_user_fails(_iso):
    ok, msg = acc.login("nobody", "x")
    assert not ok
    assert "用户不存在" in msg


def test_login_blank_rejects(_iso):
    ok, msg = acc.login("", "x")
    assert not ok
    assert "用户名为空" in msg
    ok2, msg2 = acc.login("alice", "")
    assert not ok2
    assert "密码为空" in msg2


def test_login_binds_current_user(_iso):
    _seed({"alice": _entry("alice")})
    ok, _ = acc.login("alice", "anything")
    assert ok
    assert acc.current_user() == "alice"


def test_switch_and_logout(_iso):
    _seed({"a": _entry("a"), "b": _entry("b")}, current="b")
    assert acc.current_user() == "b"
    ok, _ = acc.switch("a")
    assert ok
    assert acc.current_user() == "a"
    ok, msg = acc.logout()
    assert ok
    assert acc.current_user() == ""
    assert acc.status()["logged_in"] is False


def test_switch_unknown_fails(_iso):
    _seed({"a": _entry("a")}, current="a")
    ok, _ = acc.switch("nobody")
    assert not ok
    assert acc.current_user() == "a"


def test_require_login(_iso):
    with pytest.raises(ValueError):
        acc.require_login()
    _seed({"a": _entry("a")}, current="a")
    acc.require_login()  # 不抛


def test_multiple_accounts_persisted(_iso):
    _seed({"x": _entry("x"), "y": _entry("y")}, current="y")
    st = acc.status()
    assert set(st["accounts"]) == {"x", "y"}
    assert acc.current_user() == "y"


def test_change_password_offline_rejected(_iso):
    ok, msg = acc.change_password("a", "old", "newpassword")
    assert not ok
    assert "离线" in msg


def test_change_username_offline_rejected(_iso):
    ok, msg = acc.change_username("a", "bob", "pw")
    assert not ok
    assert "离线" in msg
