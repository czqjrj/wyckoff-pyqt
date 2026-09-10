"""微信推送 (WxPusher / Server酱 / 企业微信) 发送函数测试。"""
import json

import wyckoff.wechat_push as wp


def _fake_post(url, json=None, timeout=None):
    return _FakeResp(json)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_send_wxpusher_ok(monkeypatch):
    captured = {}

    def fake(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResp({"code": 1000, "msg": "处理成功"})

    monkeypatch.setattr(wp.requests, "post", fake)
    assert wp.send_wxpusher("AT_x", "测试正文", title="标题",
                            topic_ids=[123, 456]) is True
    assert captured["url"] == wp.WXPUSHER_SEND_URL
    assert captured["payload"]["appToken"] == "AT_x"
    assert captured["payload"]["content"] == "测试正文"
    assert captured["payload"]["summary"] == "标题"
    assert captured["payload"]["topicIds"] == [123, 456]
    assert captured["payload"]["contentType"] == 3


def test_send_wxpusher_no_token_no_receiver():
    assert wp.send_wxpusher("", "正文") is False
    assert wp.send_wxpusher("AT_x", "正文") is False  # 无主题/UID


def test_send_wxpusher_failure(monkeypatch):
    monkeypatch.setattr(wp.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionError("no net")))
    assert wp.send_wxpusher("AT_x", "正文", uids=["UID_1"]) is False


def test_push_to_wechat_no_such_method():
    assert wp.push_to_wechat("unknown", title="t", content="c") is False
