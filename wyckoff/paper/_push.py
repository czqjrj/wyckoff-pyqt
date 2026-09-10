"""模拟盘微信推送层 (WxPusher/Server酱/企业微信)。

_push_dispatch 会被测试 monkeypatch (paper._push_dispatch), _notify_trade 触发
推送时必须经 paper._push_dispatch 运行时解析; paper._CUR/paper._STRATEGY_LABELS 同样
经 paper 命名空间读取 (可在 apply_paper_params 后重建/变化)。
"""

import threading

import wyckoff.paper as paper


def _split_list(v):
    """把"逗号/顿号分隔"的字符串拆成非空列表 (兼容中英文分隔符)。"""
    if not v:
        return []
    out = []
    for item in str(v).replace("，", ",").replace("、", ",").split(","):
        item = item.strip()
        if item:
            out.append(item)
    return out


def _push_dispatch(method, cfg, title, content):
    """后台线程发送微信推送; 失败静默 (不阻塞撮合引擎)。"""
    try:
        from ..wechat_push import push_to_wechat

        push_to_wechat(method, title=title, content=content, **cfg)
    except Exception:
        pass


def _notify_trade(kind, **info):
    """交易发生时推送微信消息 (buy/sell)。未启用或配置缺失时静默跳过。"""
    if not bool(paper._CUR.get("push_enabled")):
        return
    method = str(paper._CUR.get("push_method") or "").lower()
    if method not in ("server_chan", "wechat_work", "wxpusher"):
        return
    cfg = {
        "server_chan": {"sckey": paper._CUR.get("server_chan_key", "")},
        "wechat_work": {
            "corp_id": paper._CUR.get("wechat_corp_id", ""),
            "corp_secret": paper._CUR.get("wechat_corp_secret", ""),
            "agent_id": paper._CUR.get("wechat_agent_id", ""),
            "to_user": paper._CUR.get("wechat_to_user", "") or "",
        },
        "wxpusher": {
            "app_token": paper._CUR.get("wxpusher_app_token", ""),
            "topic_ids": paper._split_list(paper._CUR.get("wxpusher_topic_ids", "")),
            "uids": paper._split_list(paper._CUR.get("wxpusher_uids", "")),
        },
    }[method]
    if method == "server_chan" and not cfg["sckey"]:
        return
    if method == "wechat_work" and not (cfg["corp_id"] and cfg["corp_secret"]):
        return
    if method == "wxpusher" \
            and not (cfg["app_token"] and (cfg["topic_ids"] or cfg["uids"])):
        return
    code = info.get("symbol", "")
    name = info.get("name", "") or str(code)[-6:]
    strat = paper._STRATEGY_LABELS.get(info.get("strategy", ""), info.get("strategy", ""))
    if kind == "buy":
        title = f"[模拟盘] 买入 {name} {code}"
        lines = [
            f"> **买入 {name} ({code})**",
            f"- 策略: {strat}",
            f"- 理由: {info.get('reason', '买入')}",
            f"- 数量: {info.get('qty')} 股",
            f"- 价格: {info.get('price')}",
            f"- 金额: {info.get('amount', '')}",
            f"- 时间: {info.get('ts', '')}",
        ]
    else:
        ret = info.get("ret")
        ret_txt = f"{ret * 100:+.2f}%" if isinstance(ret, (int, float)) else ""
        title = f"[模拟盘] 卖出 {name} {code} {ret_txt}"
        lines = [
            f"> **卖出 {name} ({code})**",
            f"- 策略: {strat}",
            f"- 理由: {info.get('reason', '')}",
            f"- 数量: {info.get('qty')} 股",
            f"- 买价: {info.get('buy_price')} / 卖价: {info.get('sell_price')}",
            f"- 收益率: {ret_txt}",
            f"- 持仓: {info.get('bars', '')} 根K线",
        ]
    content = "\n".join(lines)
    try:
        threading.Thread(
            target=paper._push_dispatch, args=(method, cfg, title, content),
            daemon=True,
        ).start()
    except Exception:
        pass

