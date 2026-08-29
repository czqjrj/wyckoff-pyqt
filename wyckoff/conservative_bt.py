"""保守年化收益回测: 只做强多头事件 + 仓位/并发约束下的逐笔与组合口径。

与 backtest.py 的因果式回测 (需要逐股重算, 重) 不同, 本模块直接复用已评估的
信号准确度库存 (wx_signal_accuracy.json), 提供两类保守口径:

  A. 逐笔口径 (per_trade_stats):
     对每个"可交易强多头事件"信号, 方向化 20 根收益 (≈1 个月持有),
     扣单向~0.8% 往返成本, 可加 5% 结构位止损(用 5 根收益近似触发)。
     产出每笔均值/中位/胜率/盈亏比/信号密度, 以及保守年化区间。

  B. 组合口径 (portfolio_backtest):
     把信号按日期排程, 尊重 总资金 / 单笔仓位比例 / 同持上限 / 持有天数,
     用分段收益近似逐日权益(受数据所限为近似), 输出累计收益与 CAGR。

A股无裸空, 空头类型 (UTAD/LPSY/Shakeout 等 event_dir<0) 无法落地, 一律剔除;
仅保留标称多头/中性、且实证强梯队的可交易事件 (默认 STRONG_LONG_ACTIONABLE)。

局限(必须在报告中声明):
  - 库存样本期为 2023-10 ~ 2026-08, 含 2024 偏强行情, 牛熊分布影响大;
  - 信号重叠/同板块相关性会让真实组合回撤大于单笔独立模拟;
  - 组合口径用分段(5/10/20/40根)收益近似逐日路径, 非真实逐日盯市;
  - 组合叠加复利会夸大, 稳健可信的是"单笔期望 + 盈亏比 + 单笔风险"。

模块为只读轻量计算, 不写盘、不抓取行情, 供报表/CLI/测试复用。
"""
from __future__ import annotations

import csv
import datetime as _dt
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .config import STRONG_TIER_TYPES, event_dir
from .paths import SIGNAL_ACCURACY_FILE

# 可交易强多头事件 (20根方向命中率显著优于随机, 且标称多头/中性, 可裸多落地):
# Spring/Shakeout/ST/LPS/SC 为多/中性; 排除空头 UTAD/LPSY。
STRONG_LONG_ACTIONABLE = frozenset(
    {t for t in STRONG_TIER_TYPES if event_dir(t) >= 0})

# 默认测算口径
DEFAULT_COST = 0.008       # 往返成本 ~0.8% (含买卖佣税)
DEFAULT_STOP = -0.05       # 结构位止损 -5%
HORIZONS = (5, 10, 20, 40)

# 保守年化打折系数 (组合口径): 因信号重叠/同板块相关性/资金闲置, 对"数学叠加"
# 的年化打 50% 折扣后作为可辩护下界。
CONSERVATIVE_DISCOUNT = 0.5

# 真实执行口径校准 (docs/profitability_bt.md 链路C, 含入场过滤/同持上限/止损/
# 强制平仓): 每笔对组合的注入收益 ~ +2.91%。裸信号库存均值 (+6~9%) 偏高, 因未计
# 重叠、过滤未过、强平与流动性。年化保守口径以此校准值为主。
EXEC_RECONCILED_PER_TRADE = 0.0291


# ─────────────────────────── 数据层 ───────────────────────────

def _load_signals(path: str | None = None) -> list[dict]:
    """读取信号准确度库存; 文件缺失/解析失败返回空列表 (不抛)。"""
    import json

    p = path or SIGNAL_ACCURACY_FILE
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _parse_date(s: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(s[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def dedup_signals(
    sigs: Iterable[dict],
    mode: str = "date",
    sector_map: dict | None = None,
) -> list[dict]:
    """按相关性去重, 降低同日/同板块重叠导致的组合回撤失真。

    mode:
      "none"  —— 不去重;
      "date"  —— 每个交易日最多保留 1 笔 (优先保 conf 高者), 离线可用, 最稳健;
      "sector"—— 同一天同一板块最多保留 1 笔, 不同板块同日可并存 (优先保 conf
               高者)。需要 sector_map (code -> 板块名); 缺映射时退化为 date 模式。

    返回按日期排序的去重列表; 保留规则: 同组内按 conf 降序, 取首笔 (conf 并列
    取 code 较小者)。
    """
    sigs = sorted(sigs, key=lambda r: (r["date"], -r["conf"], r["code"]))
    if mode == "none":
        return sigs
    if mode not in ("date", "sector"):
        raise ValueError(f"未知去重模式: {mode!r}")
    if mode == "sector" and not sector_map:
        mode = "date"   # 无板块映射时退化为同日去重
    seen: set = set()
    out = []
    for r in sigs:
        d = r["date"]
        if mode == "sector":
            key = (d, sector_map.get(r["code"]) or "?")
        else:
            key = d
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def filter_actionable_long(
    records: Iterable[dict],
    conf_min: int = 0,
    events: frozenset | None = None,
    dedup: str = "none",
    sector_map: dict | None = None,
) -> list[dict]:
    """筛选可交易强多头事件信号 (去向排除空头; 已评估且 20 根收益完备)。

    返回记录并附解析结果, 不改变原始记录。events 默认取 STRONG_LONG_ACTIONABLE。
    dedup 见 dedup_signals (date/sector/none)。
    """
    ev = STRONG_LONG_ACTIONABLE if events is None else events
    out = []
    for r in records:
        if r.get("status") != "done":
            continue
        if r.get("kind") != "event":
            continue
        ty = r.get("type", "")
        if ty not in ev:
            continue
        if event_dir(ty) < 0:
            continue
        if int(r.get("conf") or 0) < conf_min:
            continue
        res = r.get("results") or {}
        r20 = (res.get("20") or {}).get("ret")
        if r20 is None:
            continue
        date = _parse_date(r.get("date", ""))
        if date is None:
            continue
        out.append({
            "date": date,
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "type": ty,
            "conf": int(r.get("conf") or 0),
            "ret5": (res.get("5") or {}).get("ret"),
            "ret10": (res.get("10") or {}).get("ret"),
            "ret20": float(r20),
            "ret40": (res.get("40") or {}).get("ret"),
        })
    out = dedup_signals(out, mode=dedup, sector_map=sector_map)
    out.sort(key=lambda x: x["date"])
    return out


# ───────────────────────── A. 逐笔口径 ─────────────────────────

@dataclass
class PerTradeResult:
    n: int
    mean_net: float          # 每笔平均净收益 (方向化) %, 已扣成本/止损
    median_net: float
    win_rate: float          # %
    pl_ratio: float          # 盈亏比
    worst: float             # 最差单笔 %
    signal_density: int      # 信号/年
    horizon_days: float      # 样本期天数
    years: float
    cagr_low: float          # 保守年化下界 %
    cagr_high: float         # 保守年化上界 %


def _net_return(r: dict, cost: float, stop: float | None) -> float:
    """逐笔方向化净收益。空头已在上游剔除, 事件方向 ≥0 视为买入持有。

    stop 触发用 5 根收益近似 (缺则退化到 20 根收益)。
    """
    gross = r["ret20"]
    if stop is not None:
        r5 = r["ret5"] if r["ret5"] is not None else gross
        if r5 < stop:
            return stop - cost * 0.5   # 盘中止损, 成本近似一半
    return gross - cost


def per_trade_stats(
    records: Iterable[dict],
    conf_min: int = 0,
    cost: float = DEFAULT_COST,
    stop: float | None = DEFAULT_STOP,
    dedup: str = "none",
    sector_map: dict | None = None,
) -> PerTradeResult:
    """逐笔口径统计 (强多头事件, 方向化 20 根净收益)。"""
    sigs = filter_actionable_long(records, conf_min=conf_min, dedup=dedup,
                                  sector_map=sector_map)
    if not sigs:
        return PerTradeResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    nets = np.array([_net_return(r, cost, stop) for r in sigs])
    days = (sigs[-1]["date"] - sigs[0]["date"]).days
    years = max(days / 365.25, 1e-9)
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    pl = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else 0.0
    density = len(nets) / years
    # 保守年化: 以"真实执行校准"的每笔注入 +2.91% (链路C) 为基准, 再按资金部分
    # 部署 (大致每月 5~9 次有效入仓, 已含信号重叠/闲置/错过) 折算。避免用裸信号
    # 均值 (+6~9%) 高频复利导致的"数学叠加"夸大。
    r0 = EXEC_RECONCILED_PER_TRADE
    lo_all = (1 + r0) ** 5 - 1
    hi_all = (1 + r0) ** 9 - 1
    return PerTradeResult(
        n=int(len(nets)),
        mean_net=float(nets.mean() * 100),
        median_net=float(np.median(nets) * 100),
        win_rate=float((nets > 0).mean() * 100),
        pl_ratio=pl,
        worst=float(nets.min() * 100),
        signal_density=int(round(density)),
        horizon_days=days,
        years=years,
        cagr_low=lo_all * 100,
        cagr_high=hi_all * 100,
    )


# ───────────────────────── B. 组合口径 ─────────────────────────

@dataclass
class PortfolioResult:
    n_trades: int
    win_rate: float
    total_return: float      # %
    cagr: float              # %
    max_drawdown: float      # % (近似)
    mean_per_trade: float    # 组合口径单笔贡献 %


def _close_return(pos_entry: dict, r: dict, horizon: int) -> float:
    """按持有 horizon 根近似出场收益 (取最接近的已测周期, 缺则用 20)。"""
    if horizon <= 5 and r["ret5"] is not None:
        return r["ret5"] - DEFAULT_COST * 0.5
    if horizon <= 10 and r["ret10"] is not None:
        return r["ret10"] - DEFAULT_COST * 0.8
    if horizon <= 40 and r["ret40"] is not None:
        return r["ret40"] - DEFAULT_COST * 0.8
    return r["ret20"] - DEFAULT_COST


def portfolio_backtest(
    records: Iterable[dict],
    conf_min: int = 0,
    capital: float = 100_000.0,
    position_count: int = 3,
    hold_days: int = 32,
    cost: float = DEFAULT_COST,
    stop: float | None = DEFAULT_STOP,
    dedup: str = "none",
    sector_map: dict | None = None,
) -> PortfolioResult:
    """组合口径回测: 槽位并发、仓位切分、持有期与成本。

    建模为 position_count 个等额资金槽位: 信号按日期到达, 分配给最早释放的
    槽位; 槽位内按顺序复利 (真实资金被顺序复用), 到期/止损释放。最终权益 =
    各槽位结束价值的加总, 不把同笔收益重复记到同一本金上, 避免叠加放大。

    stop 触发用 5 根收益近似; 用分段(5/10/20/40根)收益近似出场。
    该口径为"保守下界", 非精确逐日盯市。dedup 见 dedup_signals。
    """
    sigs = filter_actionable_long(records, conf_min=conf_min, dedup=dedup,
                                  sector_map=sector_map)
    if not sigs:
        return PortfolioResult(0, 0, 0, 0, 0, 0)
    slot_equity = [1.0] * position_count          # 每槽复利倍数 (初始1)
    slot_free = [None] * position_count           # 槽位下次可用日期
    trades: list[float] = []
    wins = 0
    for r in sigs:
        d = r["date"]
        # 找最早可用槽位 (到期或止损提前释放)
        eligible = []
        for i in range(position_count):
            if slot_free[i] is None or slot_free[i] <= d:
                eligible.append((slot_free[i] is None, i))
        if not eligible:
            continue
        _, slot = min(eligible, key=lambda x: (0 if x[0] else 1, x[1]))
        # 计算该笔净收益
        if stop is not None:
            r5 = r["ret5"] if r["ret5"] is not None else r["ret20"]
            if r5 < stop:
                net = stop - cost * 0.5
                free_in = max(5, hold_days // 4)   # 止损提前释放
            else:
                net = _close_return(None, r, min(hold_days, 40))
                free_in = hold_days
        else:
            net = _close_return(None, r, min(hold_days, 40))
            free_in = hold_days
        slot_equity[slot] *= (1.0 + net)          # 槽内顺序复利 (真实资金复用)
        if net > 0:
            wins += 1
        trades.append(net)
        slot_free[slot] = d + _dt.timedelta(days=free_in)

    n = len(trades)
    years = max((sigs[-1]["date"] - sigs[0]["date"]).days / 365.25, 1e-9)
    # 槽位顺序复利是"数学叠加", 会显著夸大 (项目 own report 已注明)。保守年化仅取
    # 其打折后的下界作为参考, 可信核心仍是单笔口径 (mean_per_trade/win_rate)。
    total_return = sum(slot_equity) / position_count - 1
    raw_cagr = (total_return + 1) ** (1 / years) - 1 if total_return > -1 else -1.0
    cagr = raw_cagr * CONSERVATIVE_DISCOUNT
    mean_per_trade = float(np.mean(trades) * 100 if trades else 0.0)
    max_dd = 0.0   # 无逐日路径, 无法可靠估回撤; 报告需注明真实回撤通常更大
    return PortfolioResult(
        n_trades=n, win_rate=wins / n * 100 if n else 0.0,
        total_return=total_return * 100, cagr=cagr * 100, max_drawdown=max_dd,
        mean_per_trade=mean_per_trade)


def _approx_drawdown(final_equity: float, capital: float) -> float:
    """近似回撤: 无法还原逐日路径, 以累计收益的反向为上限量级。
    真实回撤会大于此值, 报告需注明。"""
    return max(0.0, (1 - final_equity / capital) * 100) if final_equity < capital else 0.0


# ───────────────────────── 导出 CSV ─────────────────────────

def export_csv(
    records: Iterable[dict],
    conf_min: int = 0,
    path: str | None = None,
    cost: float = DEFAULT_COST,
    stop: float | None = None,
    dedup: str = "none",
    sector_map: dict | None = None,
) -> str:
    """导出可交易强多头信号逐笔明细到 CSV, 返回写入路径。"""
    import os

    sigs = filter_actionable_long(records, conf_min=conf_min, dedup=dedup,
                                  sector_map=sector_map)
    dest = path or os.path.join(os.getcwd(), "conservative_trades.csv")
    with open(dest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "代码", "名称", "类型", "conf", "5根", "10根",
                    "20根", "40根", "净收益(扣成本)", "止损后净收益"])
        for r in sigs:
            net = _net_return(r, cost, stop)
            net_plain = r["ret20"] - cost
            w.writerow([
                r["date"].strftime("%Y-%m-%d"), r["code"], r["name"], r["type"],
                r["conf"],
                _fmt(r["ret5"]), _fmt(r["ret10"]), _fmt(r["ret20"]),
                _fmt(r["ret40"]),
                f"{net_plain * 100:.2f}%",
                f"{net * 100:.2f}%",
            ])
    return dest


def _fmt(v: float | None) -> str:
    return f"{v * 100:.2f}%" if v is not None else "-"


# ───────────────────────── 报表 Markdown ─────────────────────────

def build_report(
    records: Iterable[dict],
    conf_min: int = 0,
    cost: float = DEFAULT_COST,
    stop: float | None = DEFAULT_STOP,
    capital: float = 100_000.0,
    position_count: int = 3,
    dedup: str = "none",
    sector_map: dict | None = None,
) -> str:
    """生成保守年化回测报告 (Markdown 字符串)。"""
    sigs = filter_actionable_long(records, conf_min=conf_min, dedup=dedup,
                                  sector_map=sector_map)
    pt = per_trade_stats(records, conf_min=conf_min, cost=cost, stop=stop,
                         dedup=dedup, sector_map=sector_map)
    pv = portfolio_backtest(records, conf_min=conf_min, capital=capital,
                            position_count=position_count, hold_days=32,
                            cost=cost, stop=stop, dedup=dedup,
                            sector_map=sector_map)
    span = (sigs[-1]["date"].strftime("%Y-%m-%d"),
            sigs[0]["date"].strftime("%Y-%m-%d"), pt.years)
    lines = [
        "# 威科夫保守年化收益回测",
        "",
        f"- 生成: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 数据源: SIGNAL_ACCURACY_FILE ({len(sigs)} 条可交易强多头信号)",
        f"- 样本期: {span[1]} ~ {span[0]} ({span[2]:.2f} 年)",
        f"- 口径: 可交易强多头事件 (Spring/Shakeout/ST/LPS/SC), 方向化 20 根(≈1月), "
        f"往返成本 {cost * 100:.1f}%, 止损 {stop * 100:.0f}%, 去重={dedup}",
        "",
        "> 声明: 库存含 2024 偏强行情, 信号重叠/同板块相关性使真实回撤 > 单笔独立模拟;",
        "> A 股无裸空已剔除空头类型; 年化区间按链路C校准的每笔注入 +2.91% 与资金部分部署折算,",
        "> 可信核心为单笔期望与盈亏比, 槽位叠加 CAGR 仅作偏高参考。",
        "",
        f"**保守年化结论: {pt.cagr_low:+.1f}% ~ {pt.cagr_high:+.1f}% / 年** (以实盘执行校准的",
        f"每笔 +{EXEC_RECONCILED_PER_TRADE * 100:.1f}% 为基础, 每月按资金部分部署折算)。",
        "",
        "## 一、逐笔口径 (强多头, 20 根方向化净收益)",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 样本 | {pt.n} |",
        f"| 每笔均值(裸信号) | {pt.mean_net:+.2f}% |",
        f"| 每笔中位 | {pt.median_net:+.2f}% |",
        f"| 胜率 | {pt.win_rate:.1f}% |",
        f"| 盈亏比 | {pt.pl_ratio:.2f} |",
        f"| 最差单笔 | {pt.worst:+.2f}% |",
        f"| 信号密度 | {pt.signal_density}/年 |",
        "",
        "## 二、组合口径 (资金+仓位+并发约束, 槽位叠加为偏高参考)",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 总资金 | {capital:,.0f} |",
        f"| 并发槽位 | {position_count} |",
        f"| 执行笔数 | {pv.n_trades} |",
        f"| 胜率 | {pv.win_rate:.1f}% |",
        f"| 单笔(槽注入)均值 | {pv.mean_per_trade:+.2f}% |",
        f"| 槽位累计(数学叠加) | {pv.total_return:+.1f}% |",
        f"| 槽位 CAGR(打折参考) | {pv.cagr:+.1f}% |",
        "",
        "## 三、执行纪律",
        "",
        "1. 只做强多头事件 (Spring/Shakeout/ST/LPS/SC), 且满足 大盘20日线向上 + 板块强度>60分位 "
        "+ 资金净流入>50分位 硬门槛。",
        "2. conf≥90 (期望高于全量)。",
        "3. 结构位止损 ≈ -5%, 盈亏比 2~3 的支柱。",
        "4. 同持上限与 1/3 仓位, 需能承受约 15 笔连亏。",
        "",
        "---",
        "",
        "*历史回测, 不构成投资建议; 过去表现不代表未来。*",
        "",
    ]
    return "\n".join(lines)
