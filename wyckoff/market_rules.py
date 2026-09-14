"""A股市场规则集中层 (交易时段 / T+1 / 整手 / 涨跌停 / 交易成本)。

全面适配A股: 此前交易时段、整手、涨跌停阈值 (indicators._limit_pct)、单边成本
(paper.COST=0.004 扁平率) 散落在各模块硬编码, 数据口径漂移。此处统一收敛:
  - 交易时段: trading_time.TRADING_SESSIONS 引用这里, 全仓单一来源。
  - 涨跌停阈值: indicators.add_indicators 的 limit_up/limit_dn/locked 基于这里的
    limit_pct(symbol) 计算; bar_limit_flags / limit_blocked 供撮合层做成交约束。
  - 交易成本: A股 2023-08-28 起印花税单边卖出 0.05%, 佣金万2.5(最低5元),
    过户费双向万0.1。buy_fee/sell_fee 供模拟盘撮合按明细拆分, 替代扁平 cost。

本模块不 import 项目内部业务模块 (避免循环依赖), 仅依赖 numpy/pandas 与符号规约。
"""
from __future__ import annotations

# ── 交易时段 (含边界, 与旧 trading_time.TRADING_SESSIONS 同口径) ──
# (起时,起分) ~ (止时,止分): 上午 / 下午
TRADING_SESSIONS = (((9, 30), (11, 30)), ((13, 0), (15, 0)))

# ── 结算/委托约束 ──
T_PLUS_1 = True          # A股 T+1: 当日买入次一交易日方可卖出
MIN_LOT = 100.0          # 最低整手 (百股起, 递增 100 股)

# ── 涨跌停阈值 (相对前收盘, 含阈值内回旋余量) ──
LIMIT_MAIN = 0.099            # 沪深主板 10%
LIMIT_CHINEXT_STAR = 0.199    # 创业板 30x / 科创板 68x: 20%
LIMIT_BJ = 0.299              # 北交所 8x/4x: 30%
# 注: ST/*ST 股 5% 涨停、新股上市首日不设涨跌幅等特殊情形无法从代码判定,
# 保持与 indicators 旧口径一致 (按板块近似), 由个股筛选层另行过滤 ST。


def limit_pct(symbol) -> float:
    """按板块返回近似涨跌停阈值 (含阈值内回旋余量):
    创业板(30x)/科创板(68x) 20%, 北交所(8x/4x) 30%, 其余主板 10%。"""
    code6 = (symbol or "")[-6:]
    if code6.startswith(("30", "68")):
        return LIMIT_CHINEXT_STAR
    if code6.startswith(("8", "4")):
        return LIMIT_BJ
    return LIMIT_MAIN


# ── 交易成本 (A股) ──────────────────────────────────────────
# 佣金率 万2.5 (=0.00025) 双边收取, 单笔最低 5 元
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
# 印花税: 仅卖出单边收取, 0.05% (2023-08-28 起由 0.1% 降至 0.05%)
STAMP_TAX_RATE = 0.0005
# 过户费: 双边收取, 成交金额的 0.001% (2022-04-29 起由按股数改为按成交金额)
TRANSFER_FEE_RATE = 0.00001


def buy_fee(amount, commission_rate=COMMISSION_RATE, min_commission=MIN_COMMISSION,
            transfer_rate=TRANSFER_FEE_RATE) -> float:
    """买入费用 = 佣金(单笔保底5元) + 过户费。"""
    amount = float(amount)
    if amount <= 0:
        return 0.0
    return max(amount * commission_rate, min_commission) + amount * transfer_rate


def sell_fee(amount, commission_rate=COMMISSION_RATE, min_commission=MIN_COMMISSION,
             transfer_rate=TRANSFER_FEE_RATE, stamp_rate=STAMP_TAX_RATE) -> float:
    """卖出费用 = 佣金(单笔保底5元) + 过户费 + 印花税 (卖出单边)。"""
    amount = float(amount)
    if amount <= 0:
        return 0.0
    return (max(amount * commission_rate, min_commission)
            + amount * transfer_rate + amount * stamp_rate)


def round_trip_rate(representative_amount, commission_rate=COMMISSION_RATE,
                    min_commission=MIN_COMMISSION, transfer_rate=TRANSFER_FEE_RATE,
                    stamp_rate=STAMP_TAX_RATE) -> float:
    """往返费率 (买入+卖出费用 / 金额), 供 UI/报告展示摊平后的综合成本率。"""
    amt = float(representative_amount or 1.0)
    if amt <= 0:
        amt = 1.0
    return ((buy_fee(amt, commission_rate, min_commission, transfer_rate)
             + sell_fee(amt, commission_rate, min_commission,
                        transfer_rate, stamp_rate)) / amt)


# ── 涨跌停成交约束 ──────────────────────────────────────────
def bar_limit_flags(df, symbol=None):
    """逐 bar 涨跌停封板旗标 (limit_up / limit_dn)。

    基于 indicators.add_indicators 已计算的列 (与事件/锁定 bar 口径一致);
    调用方未附加指标列时返回全 False (fail-open, 不误拦合成/裸 K 线)。
    返回 (limit_up: bool[], limit_dn: bool[])。
    """
    import numpy as np
    if df is None or len(df) == 0:
        return np.zeros(0, bool), np.zeros(0, bool)
    if "limit_up" in df.columns and "limit_dn" in df.columns:
        return (np.asarray(df["limit_up"], dtype=bool),
                np.asarray(df["limit_dn"], dtype=bool))
    return np.zeros(len(df), bool), np.zeros(len(df), bool)


def limit_blocked(df, side, symbol=None) -> bool:
    """最新一根 K 线是否封死无法成交 (涨跌停成交约束)。

    buy: 最新 bar 涨停封板 → 按市价买不进 (返回 True);
    sell: 最新 bar 跌停封板 → 按市价卖不出 (返回 True)。
    数据缺失/无法判定时返回 False (fail-open), 不误拦正常撮合。
    """
    if df is None or len(df) == 0:
        return False
    try:
        up, dn = bar_limit_flags(df, symbol)
    except Exception:
        return False
    if len(up) == 0:
        return False
    if side == "sell":
        return bool(dn[-1])
    return bool(up[-1])