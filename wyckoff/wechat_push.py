"""微信推送功能: 支持 Server酱 和 企业微信/微信工作平台。

依赖: requests (已在 requirements.txt 中)
"""

import requests

# ── Server酱 (ServerChan) ──────────────────────────────────────────
SERVER_CHEN_URL = "https://sctapi.ftqq.com/{sckey}.send"


def send_server_chan(sckey: str, title: str, content: str) -> bool:
    """通过 Server酱 发送微信消息。

    参数:
        sckey: Server酱 的 SCKEY (在 sct.ftqq.com 获取)
        title: 消息标题
        content: 消息正文

    返回:
        True 表示发送成功 (Server酱 接口返回即视为成功)
    """
    if not sckey:
        return False
    try:
        payload = {
            "title": title,
            "content": content,
        }
        resp = requests.post(
            SERVER_CHEN_URL.format(sckey=sckey),
            json=payload,
            timeout=10,
        )
        try:
            data = resp.json()
            if data.get("code") != 0:
                return False
        except Exception:
            pass
        return resp.status_code == 200
    except Exception:
        return False


# ── 企业微信 / 微信工作平台 ────────────────────────────────────────
WECHAT_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


def _get_access_token(corp_id: str, corp_secret: str) -> str | None:
    """获取企业微信 access_token (内部使用)。"""
    try:
        url = f"{WECHAT_API_BASE}/gettoken"
        params = {"corpid": corp_id, "corpsecret": corp_secret}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            return data.get("access_token")
    except Exception:
        pass
    return None


def send_wechat_work(
    corp_id: str,
    corp_secret: str,
    agent_id: int,
    open_ids: list | None = None,
    to_user: str | None = None,
    title: str = "",
    content: str = "",
) -> bool:
    """通过 企业微信/微信工作平台 发送消息。

    参数:
        corp_id: 企业ID
        corp_secret: 应用Secret
        agent_id: 应用ID (应用/集成的 agent_id)
        open_ids: 授权后的 Open ID 列表 (个人微信需用 open_id)
        to_user: 成员 userid 列表, 逗号分隔 (企业微信用)
        title: 消息标题
        content: 消息正文 (支持 Markdown)

    返回:
        True 表示发送成功
    """
    access_token = _get_access_token(corp_id, corp_secret)
    if not access_token:
        return False

    url = f"{WECHAT_API_BASE}/message/send?access_token={access_token}"

    msg = {
        "touser": (open_ids or [to_user] or [""]),
        "msgtype": "markdown",
        "agentid": agent_id,
        "markdown": {
            "title": title,
            "content": content,
        },
    }

    try:
        resp = requests.post(url, json=msg, timeout=10)
        data = resp.json()
        return data.get("errcode", -1) == 0
    except Exception:
        return False


# ── WxPusher (Server酱免费替代) ─────────────────────────────
WXPUSHER_SEND_URL = "http://wxpusher.zjiecode.com/api/send/message"


def send_wxpusher(app_token: str, content: str, title: str = "",
                  topic_ids: list | None = None, uids: list | None = None,
                  summary: str | None = None) -> bool:
    """通过 WxPusher 应用推送微信消息 (公众号模板消息中转)。

    参数:
        app_token: 应用 APP_TOKEN (wxpusher 后台创建应用后获取, 仅展示一次)
        content: 消息正文 (纯文本, 支持 \n 换行)
        title: 消息摘要 (会显示在消息摘要栏)
        topic_ids: 主题 ID 列表 (主题二维码被扫码后订阅, 见后台"主题管理")
        uids: 用户 UID 列表 (扫描应用二维码关注后, 见后台"用户管理")
                topic_ids 与 uids 至少提供一个, 否则消息无人接收。

    返回:
        True 表示发送成功 (接口 code == 1000)
    """
    if not app_token:
        return False
    payload = {
        "appToken": app_token,
        "content": content,
        "contentType": 1,
        "summary": (summary or title or "行情提醒")[:100],
    }
    if topic_ids:
        payload["topicIds"] = [
            int(t) if str(t).isdigit() else t for t in topic_ids]
    if uids:
        payload["uids"] = list(uids)
    if not payload.get("topicIds") and not payload.get("uids"):
        return False
    try:
        resp = requests.post(WXPUSHER_SEND_URL, json=payload, timeout=10)
        data = resp.json()
        return data.get("code") == 1000
    except Exception:
        return False


# ── 通用推送入口 ─────────────────────────────────────────────────────
def push_to_wechat(method: str, **kwargs) -> bool:
    """统一的微信推送入口。

    method:
        "server_chan"  -> 调用 send_server_chan
        "wechat_work"  -> 调用 send_wechat_work
        "wxpusher"     -> 调用 send_wxpusher

    返回:
        True 表示发送成功
    """
    method = method.lower()
    if method == "server_chan":
        return send_server_chan(kwargs["sckey"], kwargs["title"], kwargs["content"])
    if method == "wechat_work":
        return send_wechat_work(
            kwargs["corp_id"],
            kwargs["corp_secret"],
            kwargs["agent_id"],
            kwargs.get("open_ids"),
            kwargs.get("to_user"),
            kwargs.get("title", ""),
            kwargs.get("content", ""),
        )
    if method == "wxpusher":
        return send_wxpusher(
            kwargs.get("app_token", ""), kwargs.get("content", ""),
            kwargs.get("title", ""), kwargs.get("topic_ids"),
            kwargs.get("uids"), kwargs.get("summary"),
        )
    return False
