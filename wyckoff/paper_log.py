"""模拟盘日志记录模块。

按日期记录模拟盘的所有重要操作:
- 扫描: 每次扫描的时间、扫描数量、命中数量、候选列表
- 买入: 成交的股票、数量、价格、策略、conf
- 卖出: 平仓的股票、价格、原因、收益
- 条件单: 触发的条件单类型、价格
- 风控: 被拦截的操作及原因
- 账户: 每日净值、持仓数量

日志文件存储在 DATA_DIR/paper_logs/ 目录下, 按日期命名。
"""
import json
import os
import threading
import time
from datetime import datetime

from .paths import DATA_DIR

_LOG_DIR = os.path.join(DATA_DIR, "paper_logs")
_LOCK = threading.Lock()
_MAX_LOG_DAYS = 90  # 保留最近90天日志


def _ensure_dir():
    os.makedirs(_LOG_DIR, exist_ok=True)


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_file(date_str=None):
    if date_str is None:
        date_str = _today_str()
    return os.path.join(_LOG_DIR, f"paper_log_{date_str}.json")


def _load_day(date_str=None):
    """加载某天的日志, 不存在则返回空结构。"""
    fp = _log_file(date_str)
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": date_str or _today_str(), "events": [], "summary": {}}


def _save_day(data, date_str=None):
    """保存某天的日志。"""
    _ensure_dir()
    fp = _log_file(date_str)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _add_event(event_type, detail):
    """添加一条事件到当天日志。"""
    try:
        with _LOCK:
            today = _today_str()
            data = _load_day(today)
            event = {
                "ts": _now_str(),
                "type": event_type,
                "detail": detail,
            }
            data["events"].append(event)
            _update_summary(data)
            _save_day(data, today)
    except Exception:
        pass


def _update_summary(data):
    """更新当天汇总统计。"""
    events = data.get("events", [])
    summary = {
        "scan_count": sum(1 for e in events if e["type"] == "scan"),
        "buy_count": sum(1 for e in events if e["type"] == "buy"),
        "sell_count": sum(1 for e in events if e["type"] == "sell"),
        "condition_count": sum(1 for e in events if e["type"] == "condition"),
        "risk_block_count": sum(1 for e in events if e["type"] == "risk_block"),
        "rebalance_count": sum(1 for e in events if e["type"] == "rebalance"),
    }
    data["summary"] = summary


# ── 公开日志接口 ─────────────────────────────────────────

def log_scan(scan_count, codes_scanned, candidates_found, candidates=None,
             gate_results=None):
    """记录扫描事件。

    Args:
        scan_count: 当天第几次扫描
        codes_scanned: 扫描的代码数量
        candidates_found: 命中的候选数量
        candidates: 候选列表 (可选, 记录详细信息)
        gate_results: 门禁结果 (可选)
    """
    detail = {
        "scan_count": scan_count,
        "codes_scanned": codes_scanned,
        "candidates_found": candidates_found,
    }
    if candidates:
        # 只记录关键字段, 避免日志过大
        detail["candidates"] = [
            {
                "code": c.get("code", ""),
                "name": c.get("name", ""),
                "type": c.get("type", ""),
                "conf": c.get("conf", 0),
                "last": c.get("last", 0),
                "strategy": c.get("strategy", ""),
                "sector": c.get("sector", ""),
            }
            for c in candidates[:20]  # 最多记录20个
        ]
    if gate_results:
        detail["gate_results"] = gate_results
    _add_event("scan", detail)


def log_buy(symbol, name, qty, price, conf, strategy="", event_type="",
            sector="", reason=""):
    """记录买入事件。

    Args:
        symbol: 股票代码
        name: 股票名称
        qty: 数量 (股)
        price: 成交价
        conf: 信号置信度
        strategy: 策略来源
        event_type: 事件类型
        sector: 行业
        reason: 买入原因 (如 "候选买入" / "条件单触发")
    """
    detail = {
        "symbol": symbol,
        "name": name,
        "qty": qty,
        "price": round(price, 3),
        "amount": round(qty * price, 2),
        "conf": conf,
        "strategy": strategy,
        "event_type": event_type,
        "sector": sector,
        "reason": reason,
    }
    _add_event("buy", detail)


def log_sell(symbol, name, qty, buy_price, sell_price, reason, ret,
             bars_held=0, strategy="", event_type=""):
    """记录卖出事件。

    Args:
        symbol: 股票代码
        name: 股票名称
        qty: 数量 (股)
        buy_price: 买入价
        sell_price: 卖出价
        reason: 卖出原因 (止盈/止损/破位/到期/手动)
        ret: 收益率
        bars_held: 持有周期数
        strategy: 策略来源
        event_type: 事件类型
    """
    detail = {
        "symbol": symbol,
        "name": name,
        "qty": qty,
        "buy_price": round(buy_price, 3),
        "sell_price": round(sell_price, 3),
        "pnl": round((sell_price - buy_price) * qty, 2),
        "ret": round(ret, 4),
        "ret_pct": f"{ret * 100:+.2f}%",
        "reason": reason,
        "bars_held": bars_held,
        "strategy": strategy,
        "event_type": event_type,
    }
    _add_event("sell", detail)


def log_condition_fired(symbol, name, kind, trigger_price, current_price,
                        action="", reason=""):
    """记录条件单触发事件。

    Args:
        symbol: 股票代码
        name: 股票名称
        kind: 条件单类型 (buy_price/sell_price/take_profit/stop_loss/trailing)
        trigger_price: 触发价
        current_price: 当前价
        action: 执行的动作
        reason: 触发原因
    """
    detail = {
        "symbol": symbol,
        "name": name,
        "kind": kind,
        "trigger_price": round(trigger_price, 3) if trigger_price else None,
        "current_price": round(current_price, 3) if current_price else None,
        "action": action,
        "reason": reason,
    }
    _add_event("condition", detail)


def log_risk_block(symbol, name, reason, risk_type=""):
    """记录风控拦截事件。

    Args:
        symbol: 股票代码
        name: 股票名称
        reason: 拦截原因
        risk_type: 风控类型 (drawdown/risk_budget/sector/single/capital)
    """
    detail = {
        "symbol": symbol,
        "name": name,
        "reason": reason,
        "risk_type": risk_type,
    }
    _add_event("risk_block", detail)


def log_rebalance(symbol, name, qty, price, old_qty, new_qty):
    """记录再平衡加仓事件。

    Args:
        symbol: 股票代码
        name: 股票名称
        qty: 加仓数量
        price: 加仓价格
        old_qty: 原持仓数量
        new_qty: 新持仓数量
    """
    detail = {
        "symbol": symbol,
        "name": name,
        "qty": qty,
        "price": round(price, 3),
        "old_qty": old_qty,
        "new_qty": new_qty,
    }
    _add_event("rebalance", detail)


def log_account_snapshot(equity_value, cash, positions_count, closed_count):
    """记录账户快照 (每天一次)。

    Args:
        equity_value: 总资产
        cash: 现金
        positions_count: 持仓数量
        closed_count: 已平仓数量
    """
    detail = {
        "equity": round(equity_value, 2),
        "cash": round(cash, 2),
        "positions_count": positions_count,
        "closed_count": closed_count,
    }
    _add_event("account", detail)


# ── 日志查询接口 ─────────────────────────────────────────

def get_log(date_str=None):
    """获取某天的日志。"""
    with _LOCK:
        return _load_day(date_str)


def get_log_range(start_date, end_date):
    """获取日期范围内的日志。"""
    from datetime import timedelta
    results = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        data = get_log(date_str)
        if data.get("events"):
            results.append(data)
        current += timedelta(days=1)
    return results


def get_recent_logs(days=7):
    """获取最近N天的日志。"""
    from datetime import timedelta
    today = datetime.now()
    start = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    return get_log_range(start, end)


def get_log_dates():
    """获取所有有日志的日期列表。"""
    _ensure_dir()
    dates = []
    for f in os.listdir(_LOG_DIR):
        if f.startswith("paper_log_") and f.endswith(".json"):
            date_str = f[10:-5]  # paper_log_YYYY-MM-DD.json -> YYYY-MM-DD
            dates.append(date_str)
    return sorted(dates)


def format_daily_report(date_str=None):
    """生成某天的日志报告 (文本格式)。"""
    data = get_log(date_str)
    events = data.get("events", [])
    summary = data.get("summary", {})
    date = data.get("date", date_str or _today_str())

    lines = [f"=== 模拟盘日志 {date} ===", ""]

    # 汇总
    lines.append(f"扫描次数: {summary.get('scan_count', 0)}")
    lines.append(f"买入次数: {summary.get('buy_count', 0)}")
    lines.append(f"卖出次数: {summary.get('sell_count', 0)}")
    lines.append(f"条件单触发: {summary.get('condition_count', 0)}")
    lines.append(f"风控拦截: {summary.get('risk_block_count', 0)}")
    lines.append(f"再平衡: {summary.get('rebalance_count', 0)}")
    lines.append("")

    # 按时间顺序列出事件
    for event in events:
        ts = event.get("ts", "")[11:19]  # 只取时分秒
        etype = event.get("type", "")
        detail = event.get("detail", {})

        if etype == "scan":
            lines.append(f"[{ts}] 扫描 #{detail.get('scan_count', '?')}: "
                         f"扫描 {detail.get('codes_scanned', 0)} 码, "
                         f"命中 {detail.get('candidates_found', 0)} 个候选")
        elif etype == "buy":
            lines.append(f"[{ts}] 买入 {detail.get('symbol', '')} "
                         f"{detail.get('name', '')} "
                         f"{detail.get('qty', 0)}股 @ {detail.get('price', 0)} "
                         f"conf={detail.get('conf', 0)} "
                         f"策略={detail.get('strategy', '')} "
                         f"原因={detail.get('reason', '')}")
        elif etype == "sell":
            lines.append(f"[{ts}] 卖出 {detail.get('symbol', '')} "
                         f"{detail.get('name', '')} "
                         f"{detail.get('qty', 0)}股 @ {detail.get('sell_price', 0)} "
                         f"收益={detail.get('ret_pct', '')} "
                         f"原因={detail.get('reason', '')}")
        elif etype == "condition":
            lines.append(f"[{ts}] 条件单 {detail.get('kind', '')} "
                         f"{detail.get('symbol', '')} "
                         f"触发价={detail.get('trigger_price', '')} "
                         f"现价={detail.get('current_price', '')} "
                         f"动作={detail.get('action', '')}")
        elif etype == "risk_block":
            lines.append(f"[{ts}] 风控拦截 {detail.get('symbol', '')} "
                         f"{detail.get('name', '')} "
                         f"类型={detail.get('risk_type', '')} "
                         f"原因={detail.get('reason', '')}")
        elif etype == "rebalance":
            lines.append(f"[{ts}] 再平衡 {detail.get('symbol', '')} "
                         f"{detail.get('name', '')} "
                         f"加仓 {detail.get('qty', 0)}股 @ {detail.get('price', 0)}")
        elif etype == "account":
            lines.append(f"[{ts}] 账户快照: "
                         f"净值={detail.get('equity', 0):,.0f} "
                         f"现金={detail.get('cash', 0):,.0f} "
                         f"持仓={detail.get('positions_count', 0)}只")

    return "\n".join(lines)


def cleanup_old_logs(keep_days=_MAX_LOG_DAYS):
    """清理旧日志, 保留最近N天。"""
    from datetime import timedelta
    _ensure_dir()
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    for f in os.listdir(_LOG_DIR):
        if f.startswith("paper_log_") and f.endswith(".json"):
            date_str = f[10:-5]
            if date_str < cutoff:
                try:
                    os.remove(os.path.join(_LOG_DIR, f))
                    removed += 1
                except OSError:
                    pass
    return removed
