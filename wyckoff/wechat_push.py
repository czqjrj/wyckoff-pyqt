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


# ── 通用推送入口 ─────────────────────────────────────────────────────
def push_to_wechat(method: str, **kwargs) -> bool:
    """统一的微信推送入口。

    method:
        "server_chan"  -> 调用 send_server_chan
        "wechat_work"  -> 调用 send_wechat_work

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
    return False
