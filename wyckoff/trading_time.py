"""A 股交易日 / 交易时段判定 (模拟盘撮合门禁用)。

判定规则:
  - 交易时段: 周一至周五 09:30-11:30 / 13:00-15:00 (含边界)。
  - 交易日: 周末直接排除, 工作日叠加 akshare 节假日日历 (本地缓存,
    不可用或列表未覆盖当年时按工作日放行, 避免离线误拦)。

供 paper.py (run_cycle/run_scan 撮合门禁) 与 paper_cron.py (定时任务门禁)
统一复用, 避免两处实现漂移。
"""
from __future__ import annotations

import datetime
import json
import os
import time

from .paths import DATA_DIR

# A 股交易时段 ((起时,起分), (止时,止分)) 含边界: 上午 / 下午
TRADING_SESSIONS = (((9, 30), (11, 30)), ((13, 0), (15, 0)))
# 交易日历缓存 (akshare tool_trade_date_hist_sina), 每日刷新一次
TRADE_DATES_CACHE = os.path.join(DATA_DIR, "wx_trade_dates.json")


def in_trading_hours(now=None):
    """当前是否处于 A 股交易时段 (含边界)。"""
    n = now or datetime.datetime.now()
    hm = n.hour, n.minute
    return any(start <= hm <= end for start, end in TRADING_SESSIONS)


def _load_trade_dates():
    """返回当年交易日集合 {'YYYY-MM-DD', ...}; 失败/无缓存返回 None。

    每次成功抓取缓存到本地文件, 当天不重复抓取; 离线时用缓存兜底。
    """
    try:
        with open(TRADE_DATES_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if (isinstance(data, dict) and data.get("fetched") == time.strftime("%Y-%m-%d")
                and isinstance(data.get("dates"), list)):
            return set(data["dates"])
    except Exception:
        pass
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = {str(d).split(" ")[0] for d in df["trade_date"]}
        with open(TRADE_DATES_CACHE, "w", encoding="utf-8") as f:
            json.dump({"fetched": time.strftime("%Y-%m-%d"),
                       "dates": sorted(dates)}, f, ensure_ascii=False)
        return dates
    except Exception:
        return None


def is_trading_day(now=None):
    """是否交易日: 周末直接排除; 工作日叠加节假日日历 (失败/未覆盖当年放行)。"""
    n = now or datetime.datetime.now()
    if n.weekday() >= 5:
        return False
    dates = _load_trade_dates()
    if not dates:
        return True  # 日历不可用 → 按工作日放行
    today = n.strftime("%Y-%m-%d")
    if today in dates:
        return True
    try:
        newest = max(dates)
    except ValueError:
        return True
    return newest < today  # 列表未覆盖今年 → 放行; 已覆盖且缺今天 → 确为节假日


def gate_reason(now=None, anytime=False):
    """返回跳过原因 (None=允许执行)。

    anytime=True 强制放行 (供手动补跑/测试绕开门禁)。
    """
    if anytime:
        return None
    n = now or datetime.datetime.now()
    if not in_trading_hours(n):
        return f"非交易时段 {n:%H:%M} (A股 09:30-11:30 / 13:00-15:00)"
    if not is_trading_day(n):
        return f"非交易日 {n:%Y-%m-%d}"
    return None