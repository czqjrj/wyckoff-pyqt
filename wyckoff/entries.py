"""今日可靠入场点: 基于实测可交易胜率的确认制多头入场信号。

入场依据 = 只做强梯队 + 已确认 + 高置信 + 三重共振 (docs/accuracy_report.md §十):

  1. 强梯队: 仅接受 STRONG_TIER_TYPES ∩ 标称多头 (Spring/Shakeout/ST/LPS),
     弱信号 (SOS/JOC/BC/AR/PSY 及全部 VSA) 只作确认证据, 不触发入场;
  2. 已确认: 价格刺破前低 (事件, ATR 自适应深度) 后首根收回事件低点上方的
     bar 即确认bar (events.avail_idx), 确认后 ENTRY_WINDOW 根内且未跌破
     止损 (事件低点 ± 0.5×ATR) → 入场仍有效;
  3. 高置信: 事件 conf 低于 MIN_ENTRY_CONF 一律不列; 在线模型 v6 就绪时
     low 可靠度分位 (REL_TIER_LOW) 默认剔除 (实测该档方向命中仅 ~12%);
  4. 三重共振 (2026-08-28 新增): 仅当 同时满足 三条件才列入结果
     - 大盘趋势向上: MA20 斜率 > 0
     - 板块强度 > 60 分位: 所属行业强度百分位 > 0.6
     - 资金流净流入 > 50 分位: 20日主力净额分位 > 0.5
  5. 布局否决: 确认后延伸超 MAX_EXTEND_PCT 或止损距离过宽 → 放弃。

数据依据 (wx_signal_accuracy.json, 确认可交易口径 20根实测):
  Spring ~61% / Shakeout ~57% / SC 无 (中性不参与); 信号bar口径的 85%/83%
  含前视水分 (收盘时点不可知), 确认制才是可成交的。
"""
import numpy as np

from .chain import sector_strength_pct
from .config import STRONG_TIER_TYPES, event_dir
from .entry_journal import record_entries

# 入场梯队 = 强梯队 ∩ 标称多头 (空头/中性事件不参与确认制多头入场)
ENTRY_TIERS = frozenset(t for t in STRONG_TIER_TYPES if event_dir(t) > 0)

# 确认后的有效入场窗口: 确认bar距今超过该根数视为追高, 不再列出
ENTRY_WINDOW = 3
# 事件 conf 下限 (高置信门槛): 低于该值信号太弱, 不构成入场
MIN_ENTRY_CONF = 70
# 现价相对确认bar收盘的最大延伸 (超过则剩余空间不足)
MAX_EXTEND_PCT = 0.08
# 止损距离上限 (占入场价比例): 过宽说明弹簧结构松散, 盈亏比差
MAX_STOP_DIST_PCT = 0.10

# ── 2026-08-28 新增: 三重共振入口硬规则 (与 paper / run_analysis 保持一致) ──
# 阈值统一来自 discipline.py (唯一数据源), 避免与 paper 双份硬编码漂移。
from .discipline import (  # noqa: E402
    FUND_NET_PCT_GATE as ENTRY_MIN_FUND_NET_PCT,
)
from .discipline import (
    MARKET_TREND_20 as ENTRY_MIN_MKT_TREND_20,
)
from .discipline import (
    SECTOR_PCT_GATE as ENTRY_MIN_SECTOR_PCT,
)

# 可靠度否决门: 在线模型就绪时, low 档 (模型可靠度≤REL_TIER_LOW) 入场点
# 默认不进扫描结果 —— 实测该档方向命中仅 ~12%, 列出只会诱导低质量交易。
ENTRY_VETO_LOW_REL = True
# 自动记录开关: 每次扫描命中的入场点自动入账 (wx_entry_journal.json),
# 事后按入场价/止损逐笔结算胜负 (见 entry_journal.journal_stats)。
AUTO_RECORD_ENTRIES = True


def measured_win_rates(scale=240):
    """取入场类型的实测可交易胜率 (贝叶斯收缩值)。{type: {"win","n"}}"""
    try:
        from .signal_accuracy import load_win_rates
        rates = load_win_rates(20, scale=scale)
    except Exception:
        return {}
    out = {}
    for t in ENTRY_TIERS:
        rec = rates.get(("event", t))
        if rec:
            out[t] = {"win": float(rec["shrunk"]), "n": int(rec["n"])}
    return out


def find_entry_signals(df, events=None, mkt_trend_20=None, sector_strength_pct_val=None, fund_net_pct_val=None) -> list:
    """检测 df 中当前仍有效的确认制多头入场点。

    入场依据: 只做强梯队 (Spring/Shakeout/ST/LPS) + 已确认 (avail_idx) +
    高置信 (conf≥MIN_ENTRY_CONF, 模型就绪时低可靠档剔除) +
    三重共振 (大盘↑+板块>60%+资金>50%) —— 仅当三参数均显式提供时生效。

    返回 list[dict]: {type, confirm_idx, fresh_bars, entry_date, entry_price,
    last, stop, risk_pct, conf, model_rel, rel_tier} — 按 fresh_bars 升序
    (今日确认=0 最优)。
    """
    n = len(df)
    if n < 30 or df["close"].iloc[-1] <= 0:
        return []
    if events is None:
        from .events import detect_all
        from .indicators import find_pivots
        events = detect_all(df, find_pivots(df, order=6))

    # ── 三重共振入口硬规则 (2026-08-28): 仅当三参数均显式提供且通过时才放行 ──
    if (mkt_trend_20 is not None and sector_strength_pct_val is not None and fund_net_pct_val is not None):
        if not (mkt_trend_20 > ENTRY_MIN_MKT_TREND_20
                and sector_strength_pct_val > ENTRY_MIN_SECTOR_PCT
                and fund_net_pct_val > ENTRY_MIN_FUND_NET_PCT):
            return []

    close = float(df["close"].iloc[-1])
    days = df["day"]
    closes = df["close"].values
    atr_val = float(df["atr"].iloc[-1]) if "atr" in df.columns and np.isfinite(df["atr"].iloc[-1]) else None
    try:
        from .online_model import reliability, reliability_tier
        _rel_fn = reliability
        _tier_fn = reliability_tier
    except Exception:
        _rel_fn = _tier_fn = None
    out = []
    for e in events or []:
        # 1) 强梯队多头: 弱/空头/中性事件不触发入场
        if e.get("type") not in ENTRY_TIERS:
            continue
        # 3) 高置信: 事件 conf 门槛 (模型层残余过滤在下方 rel 判断)
        if int(e.get("conf", 50)) < MIN_ENTRY_CONF:
            continue
        ai = e.get("avail_idx")
        if not isinstance(ai, (int,)) or isinstance(ai, bool) \
                or ai is None or not (0 <= ai < n):
            continue
        # 结构止损 = 事件低点 (Spring/SC/LPS/BU 价位 × 0.99)
        struct_stop = float(e.get("price") or 0)
        if struct_stop <= 0:
            continue
        # ATR 缓冲: 0.5×ATR(14) 向现价方向收紧止损
        atr_buffer = 0.5 * atr_val if atr_val else 0.0
        stop = min(struct_stop, close - atr_buffer) if atr_buffer else (struct_stop * 0.99 if struct_stop else None)
        if stop is None:
            continue
        # 已失效: 确认后任一收盘跌回止损下方 (假收复/二次破位)
        if (closes[ai:] < stop).any():
            continue
        fresh = (n - 1) - int(ai)
        if fresh > ENTRY_WINDOW:
            continue
        entry_ref = float(closes[ai])       # 计划入场价 = 确认bar收盘
        if close > entry_ref * (1 + MAX_EXTEND_PCT):
            continue                         # 已大幅离开入场区
        dist = (entry_ref - stop) / entry_ref if entry_ref > 0 else 1.0
        if dist <= 0 or dist >= MAX_STOP_DIST_PCT:
            continue
        rel = _rel_fn(e) if _rel_fn else None
        tier = _tier_fn(rel) if _tier_fn else ""
        if ENTRY_VETO_LOW_REL and tier == "low":
            continue                         # 低可靠档: 只观察不进结果
        out.append({
            "type": e["type"],
            "confirm_idx": int(ai),
            "fresh_bars": int(fresh),
            "entry_date": str(days.iloc[int(ai)].date()),
            "entry_price": round(entry_ref, 2),
            "last": round(close, 2),
            "stop": round(stop, 2),
            "risk_pct": round(dist * 100, 1),
            "conf": int(e.get("conf", 50)),
            "model_rel": round(float(rel), 3) if rel is not None else None,
            "rel_tier": tier,
        })
    out.sort(key=lambda r: r["fresh_bars"])
    return out


def _scan_one(codes_str, datalen=500, scale=240, stats=None, macro_ctx=None, **kwargs) -> list:
    """单只股票的入场点检测 (供串行/并行扫描复用)。返回行列表 (可能为空)。
    (统一管线见 _shared.analyze_light; 注入本模块引用保留打桩点。)
    macro_ctx: 可选 dict，含 mkt_trend_20, sector_strength_pct_val, fund_net_pct_val 等宏观因子
    """
    from . import datasource as _ds
    from ._shared import analyze_light
    from .events import detect_all
    from .indicators import add_indicators, find_pivots
    from .phases import judge_phase
    if stats is None:
        stats = measured_win_rates(scale)
    r = analyze_light(codes_str, datalen=datalen, scale=scale,
                      fetch_kline=_ds.fetch_kline,
                      add_indicators=add_indicators,
                      find_pivots=find_pivots,
                      detect_all=detect_all,
                      judge_phase=judge_phase)
    df, phase = r["df"], r["phase"]

    # 宏观因子: 复用传入的 ctx，或就地计算 (缓存到 macro_ctx 供下一只复用)
    if macro_ctx is None:
        macro_ctx = {}
    mkt_trend_20 = macro_ctx.get("mkt_trend_20")
    sector_strength_pct_val = macro_ctx.get("sector_strength_pct_val")
    fund_net_pct_val = macro_ctx.get("fund_net_pct_val")

    # 大盘趋势 (MA20 斜率)
    if mkt_trend_20 is None:
        try:
            market_series = r.get("market_series")
            if market_series is not None and len(market_series) >= 20:
                ma20 = market_series["price_ma20"].iloc[-1]
                ma20_prev = market_series["price_ma20"].iloc[-2]
                mkt_trend_20 = float(ma20 - ma20_prev)
                macro_ctx["mkt_trend_20"] = mkt_trend_20
        except Exception:
            mkt_trend_20 = 0.0

    # 板块强度
    if sector_strength_pct_val is None:
        try:
            sector = r.get("sector")
            if sector and sector.get("name"):
                sp = sector_strength_pct(sector["name"])
                if sp is not None:
                    sector_strength_pct_val = sp
                    macro_ctx["sector_strength_pct_val"] = sector_strength_pct_val
        except Exception:
            sector_strength_pct_val = 0.5

    # 资金流净额分位
    if fund_net_pct_val is None:
        try:
            from .discipline import fund_net_pct
            flow = r.get("flow")
            fund_net_pct_val = fund_net_pct(flow)
            if fund_net_pct_val is not None:
                macro_ctx["fund_net_pct_val"] = fund_net_pct_val
        except Exception:
            fund_net_pct_val = 0.5

    rows = []
    for s in find_entry_signals(df, events=r["events"],
                                mkt_trend_20=mkt_trend_20,
                                sector_strength_pct_val=sector_strength_pct_val,
                                fund_net_pct_val=fund_net_pct_val):
        m = stats.get(s["type"], {})
        d = event_dir(s["type"])
        rows.append({
            "code": str(codes_str)[-6:],
            "name": r["name"],
            "type": s["type"],
            "dir": "空" if d < 0 else "多",
            "phase": (phase or "").split(" ")[0],
            "fresh_bars": s["fresh_bars"],
            "entry_date": s["entry_date"],
            "entry_price": s["entry_price"],
            "last": s["last"],
            "stop": s["stop"],
            "risk_pct": s["risk_pct"],
            "conf": s["conf"],
            "rel_tier": s["rel_tier"],
            "win_rate": m.get("win"),
            "win_n": m.get("n", 0),
        })
    return rows


def market_universe(n=500) -> list:
    """全市场宇宙: 东财按成交额取活跃 Top-N, 接口失败回退本地全A抽样。
    (统一入口见 fundamental.universe, 此处保留仅返回代码列表的薄封装。)"""
    from .fundamental import universe
    return universe(n)[0]


def scan_entries(codes, datalen=500, progress=None, stopped=None,
                 scale=240) -> list:
    """扫描一批代码 (串行), 返回有效入场点行。见 scan_entries_parallel。"""
    stats = measured_win_rates(scale)
    codes = [c for c in (codes or []) if str(c).strip()]
    total = len(codes)
    rows = []
    macro_ctx = {}
    for done, c in enumerate(codes, 1):
        if stopped is not None and stopped():
            break
        if progress is not None:
            try:
                progress(done, total, str(c))
            except Exception:
                pass
        try:
            rows.extend(_scan_one(c, datalen=datalen, scale=scale, stats=stats, macro_ctx=macro_ctx))
        except TypeError:
            # 兼容旧签名 mock (测试用): 不接受 macro_ctx 时回退
            rows.extend(_scan_one(c, datalen=datalen, scale=scale, stats=stats))
        except Exception:
            continue
    if AUTO_RECORD_ENTRIES and rows:
        record_entries(rows, scale=scale)
    return _sort_rows(rows)


def scan_entries_parallel(codes, datalen=500, workers=8, progress=None,
                          stopped=None, scale=240, on_rows=None) -> list:
    """并行扫描 (线程池): 适合全市场宇宙。

    与 scan_entries 同参, 另有:
      workers     并发数
      on_rows(rows) 每只有命中的股票完成时增量回调 (UI 流式刷新用)
    返回按 今日确认 → 实测胜率降序 → 风险低优先 排序的全部行。
    (并行框架唯一实现见 _shared.parallel_map。)
    """
    from ._shared import parallel_map
    stats = measured_win_rates(scale)
    codes = [c for c in (codes or []) if str(c).strip()]
    if not codes:
        return []
    rows = []
    macro_ctx = {}

    def _one(c):
        try:
            return _scan_one(c, datalen=datalen, scale=scale, stats=stats, macro_ctx=macro_ctx)
        except TypeError:
            try:
                return _scan_one(c, datalen=datalen, scale=scale, stats=stats)
            except Exception:
                return None
        except Exception:
            return None

    def _collect(r, _c):
        if r:
            rows.extend(r)
            if on_rows is not None:
                try:
                    on_rows(r)
                except Exception:
                    pass

    parallel_map(codes, _one, workers=max(1, min(int(workers), 12)),
                 stop_fn=stopped,
                 progress=lambda d, t, c: progress(d, t, c) if progress else None,
                 on_result=_collect)
    rows = _sort_rows(rows)
    if AUTO_RECORD_ENTRIES and rows:
        record_entries(rows, scale=scale)
    return rows


def _sort_rows(rows) -> list:
    """统一排序: 今日确认优先 → 实测胜率降序 → 风险低优先。"""
    return sorted(rows, key=lambda r: (r.get("fresh_bars", 9),
                                       -(r.get("win_rate") or 0.5),
                                       r.get("risk_pct") or 99))
