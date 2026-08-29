"""完整分析流水线 (run_analysis) 与交易计划生成。"""
import threading

import numpy as np

from .backtest import backtest_events, backtest_vsa, robustness_check
from .chart import plot_chart, plot_indicators
from .conclusion import build_conclusion, build_signal_summary, sections_to_text
from .config import DEFAULT_BACKTEST, PERIOD_OPTIONS, W_RECENT
from .counterevidence import counter_evidence
from .datasource import data_source_of, fetch_kline, fetch_name, merge_realtime_bar
from .events import detect_all
from .falsify import falsify_structure
from .news import (apply_price_validation, fetch_forward_calendar,
                   fetch_market_news_sentiment, fetch_news_sentiment)
from .filters import (
    chip_analysis,
    flow_gate,
    fundamental_filter,
    granville_signal,
    volatility_signal,
)
from .fundamental import (
    build_confirm_section,
    fetch_fundamental,
    fetch_main_flow,
    fetch_sector_flow,
)
from .fusion import fuse_signals
from .indicators import add_indicators, find_pivots, pivot_order
from .interpret import interpret_report
from .market import (
    build_market_labels,
    build_sd_series,
    fetch_holder_history,
    fetch_market_env,
    fetch_market_series,
    find_trading_range,
    relative_strength,
    relative_strength_series,
    supply_demand,
    volume_profile,
)
from .multitime import multi_tf_analysis
from .ninetests import nine_tests
from .phases import judge_phase, phase_segments
from .pnf import build_pnf, plot_pnf, pnf_history_targets, pnf_targets, pnf_volume
from .risk import position_lines
from .structure import structure_progress
from .utils import normalize_symbol
from .vsa import vsa_classify
from .waves import calc_targets, enhanced_wave_analysis

_ANALYSIS_CACHE = {}
# run_analysis 会从多个后台线程 (分析/扫描/面板扫描) 并发调用, 缓存必须加锁
_ANALYSIS_LOCK = threading.Lock()


def cached_kline(symbol: str, datalen: int, scale: int):
    """最近一次 run_analysis 缓存的分析用 K 线 DataFrame (供界面取用)。"""
    with _ANALYSIS_LOCK:
        return _ANALYSIS_CACHE.get((symbol, datalen, scale), (None, None))[0]


def clamp_window(df, datalen):
    """时间段边界兜底: 分析窗口最多 = datalen 根已完成K线 + 1根实时bar。

    东财源 beg=0 时忽略 lmt 返回全历史 (600104 实测 6846 根), fetch_kline 已做
    tail(datalen) 截断; 此处再在分析边界兜一层, 保证任何数据源/未来回归都
    不会让图表超出所选时间段 (曾实测"设近1年仍显示3年")。
    """
    if df is None or not datalen:
        return df
    limit = int(datalen) + 1
    if len(df) > limit:
        return df.tail(limit).reset_index(drop=True)
    return df


def build_trade_plan(df, pivots, events, phase, structure, targets, pnf_t, tr, last_close):
    """生成交易计划文本行: 方向/入场/止损/目标/盈亏比。
    以阶段为纲: 买点仅在吸筹/上升阶段有效, 卖点仅在派发/下跌阶段有效。"""
    lines = []
    recent = [e for e in events if e["idx"] >= len(df) - W_RECENT]
    rtypes = [e["type"] for e in recent]
    spring = "Spring" in rtypes
    utad = "UTAD" in rtypes
    lpsy = "LPSY" in rtypes
    dist = "Distribution" in (structure[2] if structure else "") \
        or "Markdown" in (structure[2] if structure else "")

    # 提取基础阶段 (去除"高置信"/"需谨慎"修饰)
    base_phase = phase.replace("高置信 ", "").replace(" (需谨慎)", "").split(" ")[0]
    bullish_phase = base_phase in ("底部整固", "上升趋势")
    bearish_phase = base_phase in ("顶部构筑", "下跌趋势")
    neutral_phase = base_phase == "区间整理"

    top = tr["top"] if tr else (pnf_t.get("tr_top") if pnf_t else None)
    bottom = tr["bottom"] if tr else (pnf_t.get("tr_bottom") if pnf_t else None)
    range_w = (top - bottom) if (top and bottom) else None

    # 按方向标签收集目标: "上方/上涨"类目标且>现价 → 多头目标; "下方/下跌"类且<现价 → 空头目标。
    # 不能只看数值大小 (如失败SC算出的"点数下跌目标"可能反超现价), 否则方向全错。
    def _dir_targets(src, up_keywords, dn_keywords):
        up, dn = set(), set()
        for k, v in src.items():
            if not isinstance(v, (int, float)) or not np.isfinite(v):
                continue
            if any(w in k for w in up_keywords) and v > last_close:
                up.add(float(v))
            if any(w in k for w in dn_keywords) and v < last_close:
                dn.add(float(v))
        return sorted(up), sorted(dn, reverse=True)

    ups, dns = _dir_targets(targets, ("上方", "上涨"), ("下方", "下跌"))
    if pnf_t:
        pu, pd = _dir_targets(pnf_t, ("上方", "上涨"), ("下方", "下跌"))
        ups = sorted(set(ups) | set(pu))
        dns = sorted(set(dns) | set(pd), reverse=True)
    # 目标合理性过滤: 过远目标 (距现价 >100%) 通常是全历史投影失真 (如 P&F 纵向
    # 计数取到数年前主升段), 不当作短中期交易目标, 避免目标价离谱。
    ups = [v for v in ups if v <= last_close * 2.0]
    dns = [v for v in dns if v >= last_close * 0.1]

    entry = last_close
    # ═══ 阶段驱动方向判定 ═══
    direction = None
    if bearish_phase and (utad or "BC" in rtypes or lpsy):
        direction = "空头/减仓"
        stop = top or max((e["price"] for e in recent if e["type"] in ("BC", "UTAD", "LPSY")),
                          default=None)
    elif bearish_phase:
        # 无派发事件确认的空头阶段: 仅提示趋势偏弱, 不构成做空信号
        # (校准: 空头/观望类信号实盘命中率仅15%, 强上涨环境下多空翻转)。
        direction = "观望"
        stop = t1 = t2 = None
    elif bullish_phase and (spring or "ST" in rtypes or "LPS" in rtypes or "BU" in rtypes):
        # 买点需 Spring/ST 确认 (JOC/SOS 突破日信号实测无边际, 追高不占优)。
        # LPS/BU 是威科夫 Phase D 标准买点 (SOS/JOC 后的缩量回踩不破)——
        # 修复 detect_joc_lps_bu 后已能生成, 作为"低吸"确认同样有效。
        direction = "多头/低吸"
        stop = min((e["price"] for e in recent
                    if e["type"] in ("Spring", "SC", "LPS", "BU")),
                   default=None)
        stop = stop * 0.99 if stop else ((bottom * 0.99) if bottom else None)
    elif bullish_phase:
        direction = "多头/持有"
        stop = bottom
    elif neutral_phase and (spring or "ST" in rtypes or "LPS" in rtypes or "BU" in rtypes):
        direction = "多头/突破"
        stop = bottom
    elif neutral_phase and (utad or "BC" in rtypes or lpsy):
        direction = "空头/减仓"
        stop = top
    else:
        direction = "观望"
        stop = t1 = t2 = None

    # ═══ 方向-价位自洽校验 (A股单边做多) ═══
    # 多头: 止损 < 现价 < 目标; 空头: 止损 > 现价 > 目标。
    # 若结构止损落在现价错误一侧 (如现价已跌破 Spring/SC 低点, 或已突破派发高点),
    # 结构买/卖点已失效 → 降级为观望, 杜绝"多头止损高于现价"的自相矛盾计划
    # (实测复现: 方向=多头/低吸, 现价10.27, 止损却给10.34)。
    invalid_note = None
    if direction and "多头" in direction and stop is not None and stop >= entry:
        invalid_note = "结构止损高于现价, 买点(Spring/SC低点)已被跌破, 转观望"
        direction = "观望"
        stop = t1 = t2 = None
    elif direction and "空头" in direction and stop is not None and stop <= entry:
        invalid_note = "结构止损低于现价, 卖点(BC/UTAD高点)已被突破, 转观望"
        direction = "观望"
        stop = t1 = t2 = None

    # 处理 target
    if direction and "多头" in direction:
        t1 = ups[0] if ups else top
        t2 = (ups[1] if len(ups) > 1
              else ((top + range_w) if (top and range_w) else None))
    elif direction and "空头" in direction:
        t1 = dns[0] if dns else bottom
        t2 = (dns[1] if len(dns) > 1
              else ((bottom - range_w) if (bottom and range_w) else None))
    else:
        t1 = t2 = None

    # 目标自洽: 多头目标必须 > 现价, 空头目标必须 < 现价; 错误一侧的目标作废
    if direction and "多头" in direction:
        if t1 is not None and t1 <= entry:
            t1 = None
        if t2 is not None and t2 <= entry:
            t2 = None
    elif direction and "空头" in direction:
        if t1 is not None and t1 >= entry:
            t1 = None
        if t2 is not None and t2 >= entry:
            t2 = None
    lines.append(f"  方向: {direction}")
    lines.append(f"  现价: {entry:.2f}")
    if invalid_note:
        lines.append(f"  提示: {invalid_note}")
    if "空头" in direction:
        # A股只能做多、不能做空: 空头/减仓 仅是持仓者的减仓/离场指引,
        # 不构成可执行的做空交易计划, 因此不输出 止损/目标/盈亏比 的买卖语义,
        # 避免被 AI 解读成"现价距止损较近而盈亏比不足"的矛盾交易。
        if stop:
            lines.append(f"  上空确认位: {stop:.2f} (若放量收复, 空头预期落空, 可暂缓减仓)")
        downs = [v for v in (t1, t2) if v is not None]
        if downs:
            lines.append("  下方回踩支撑参考: " + "、".join(f"{v:.2f}" for v in downs))
        lines.append("  提示: A股只能做多、不能做空; 该方向仅供持仓者减仓/逢高了结参考, "
                     "不构成做空指令, 也不按此开仓")
        lines.append("  退出规则: 反弹至上方阻力滞涨即逢高了结; 或有效跌破下方支撑后再减")
    else:
        if stop:
            if direction in ("多头/低吸", "多头/持有", "多头/突破") and t1 and entry != stop:
                rr = (t1 - entry) / (entry - stop)
            else:
                rr = None
            lines.append(f"  止损: {stop:.2f}")
            if t1:
                lines.append(f"  目标1: {t1:.2f}")
            if t2:
                lines.append(f"  目标2: {t2:.2f}")
            if rr and rr > 0:
                lines.append(f"  盈亏比: {rr:.2f} : 1")
        # ATR 动态止损兜底 + 仓位 (仅做多方向: 空头/减仓是离场指引, 不计算交易仓位)
        atr = float(df["atr"].iloc[-1]) if ("atr" in df.columns
                                             and np.isfinite(df["atr"].iloc[-1])) else None
        is_long = direction in ("多头/低吸", "多头/持有", "多头/突破")
        if atr and is_long:
            atr_stop = entry - 3 * atr
            if stop and atr_stop > stop:
                lines.append(f"  ATR止损: {atr_stop:.2f} (现价-3×ATR, 防假跌破)")
            # 有效风险止损: 取结构止损与ATR止损中更宽者, 避免噪声级止损(低于日波动)放大仓位
            risk_stop = stop if stop else atr_stop
            if stop and atr_stop < stop:
                risk_stop = atr_stop
                stop_dist_pct = abs(entry - stop) / max(entry, 1e-9) * 100
                lines.append(f"  提示: 结构止损距离仅{stop_dist_pct:.1f}%, "
                             f"仓位改按更宽的ATR有效止损{abs(entry - risk_stop) / max(entry, 1e-9) * 100:.1f}%计算")
            dist_pct = abs(entry - risk_stop) / max(entry, 1e-9) * 100
            if dist_pct > 0.5:
                pos = 2.0 / dist_pct * 100
                if pos > 100:
                    lines.append(f"  仓位参考: 单笔风险2% → 约{pos:.0f}%仓位, 超过100%需杠杆; "
                                 "建议放宽止损或降低单笔风险")
                else:
                    lines.append(f"  仓位参考: 单笔风险2% → 约{pos:.0f}%仓位 (止损距离{dist_pct:.1f}%)")
        lines.append("  退出规则: 目标1到→止损上移至入场; 或移动止损=现价-3×ATR; 20日未盈利→时间止损")
    lines.append("  (仅供参考, 不构成投资建议)")
    return lines


def _plan_from_lines(lines):
    """从 build_trade_plan 文本行解析结构化计划 (供仓位计算)。"""
    plan = {}
    keymap = {"现价:": "entry", "止损:": "stop", "目标1:": "t1"}
    for ln in lines or []:
        s = ln.strip()
        if s.startswith("方向:"):
            plan["direction"] = s.split("方向:", 1)[1].strip()
            continue
        for prefix, key in keymap.items():
            if s.startswith(prefix):
                try:
                    plan[key] = float(s.split(prefix, 1)[1].strip())
                except ValueError:
                    pass
                break
    return plan


def run_analysis(code: str, datalen: int = 700, scale: int = 240, fig=None, pnf_fig=None,
                 ind_fig=None, draw_waves: bool = True, draw_locks: bool = True,
                 bt_horizon: int = None, bt_min_n: int = None, bt_cost: float = None,
                 force_refresh: bool = False, confirm_enabled: bool = True,
                 settings: dict = None, precomputed_cb=None,
                 kline_engine: str = "matplotlib", kline_data: dict = None,
                 pnf_engine: str = "matplotlib", pnf_data: dict = None,
                 ind_engine: str = "matplotlib", ind_data: dict = None,
                 mkt_engine: str = "matplotlib", mkt_data: dict = None,
                 pivot_sensitivity: str = None, pnf_box_mode: str = None,
                 pnf_atr_factor: float = None, vsa_backtest: bool = True):
    """返回 (text_summary, fig, pnf_fig, ind_fig, signal_summary, sections, market, segs)。

    bt_horizon/bt_min_n/bt_cost 为空时取默认回测参数 (DEFAULT_BACKTEST)。
    force_refresh=True 时绕过分析缓存并强制重新拉取行情 (供定时刷新/手动刷新)。
    confirm_enabled=False 时跳过基本面/资金流/板块抓取 (离线快速模式, 无确认小节)。
    settings 传入界面设置 dict: 用于 AI 证伪 (ai_falsify_enabled/api_key) 与
    仓位建议 (portfolio_value/risk_pct)。不传则跳过这两个可选层。
    pivot_sensitivity: "fast"/"normal"/"safe" (None 时取 settings 或默认 normal)。
    pnf_box_mode: "pct"/"atr" (None 时取 settings 或默认 pct)。
    pnf_atr_factor: atr 模式下格值=ATR×factor (None 时取 settings 或默认 0.5)。
    vsa_backtest: True 时附带 VSA 标签滚动回测 (结论小节)。
    precomputed_cb 可选回调: precomputed_cb(df, symbol, code, scale, datalen, name,
    phase_label, conf_q, precomputed), 供 accuracy 记录复用管线结果, 避免重复计算。
    kline_engine: "matplotlib" 绘制 matplotlib K 线图; "pyqtgraph" 时不绘制 K 线
    (返回 fig=None), 改为把绘制数据写入 kline_data dict (见 build_kline_data)。
    pnf_engine 同理: "pyqtgraph" 时不绘制 matplotlib 点数图 (返回 pnf_fig=None),
    把绘制数据写入 pnf_data dict (见 build_pnf_data)。
    ind_engine 同理: "pyqtgraph" 时不绘制 matplotlib 技术指标 (返回 ind_fig=None),
    把绘制数据写入 ind_data dict (见 chart.build_ind_data)。
    mkt_engine 同理: "pyqtgraph" 时不绘制 matplotlib 资金透视图, 把绘制数据写入
    mkt_data dict (见 chart.build_market_data)。注意 market 本身 (含 DataFrame)
    仍照常返回, 供结论与准确性等使用。
    """
    horizon = bt_horizon if bt_horizon is not None else DEFAULT_BACKTEST["horizon"]
    min_n = bt_min_n if bt_min_n is not None else DEFAULT_BACKTEST["min_n"]
    cost = bt_cost if bt_cost is not None else DEFAULT_BACKTEST["cost"]
    s = settings or {}
    if pivot_sensitivity is None:
        pivot_sensitivity = s.get("pivot_sensitivity", "normal")
    if pnf_box_mode is None:
        pnf_box_mode = s.get("pnf_box_mode", "pct")
    if pnf_atr_factor is None:
        pnf_atr_factor = float(s.get("pnf_atr_factor", 0.5))

    symbol = normalize_symbol(code)
    period_txt = {240: "日线", 120: "120分钟", 60: "60分钟"}.get(scale, f"{scale}分钟")
    cache_key = (symbol, datalen, scale)
    # 历史K线走5分钟缓存 (已完成bar盘中不变, 复用省流量); 但实时价每次分析
    # 都重新合并 (merge_realtime_bar), 保证图内"现价"始终是当前实际价格,
    # 不因 _ANALYSIS_CACHE 命中而停留在上一次刷新的旧价。
    name = fetch_name(symbol)
    df = merge_realtime_bar(fetch_kline(symbol, datalen=datalen, scale=scale,
                                        use_cache=not force_refresh), symbol=symbol)
    df = add_indicators(df, symbol=symbol)
    df = clamp_window(df, datalen)
    with _ANALYSIS_LOCK:
        _ANALYSIS_CACHE[cache_key] = (df, name, None)  # 第三位 vsa_signals, 下方填充
        if len(_ANALYSIS_CACHE) > 32:
            _ANALYSIS_CACHE.pop(next(iter(_ANALYSIS_CACHE)), None)
    pivots = find_pivots(df, sensitivity=pivot_sensitivity)
    events = detect_all(df, pivots)
    phase, detail = judge_phase(df, pivots, events)
    wave_lines, wave_data = enhanced_wave_analysis(df, pivots, phase=phase, events=events)
    targets = calc_targets(df, pivots, events)
    vsa_signals = vsa_classify(df, scale=scale)
    with _ANALYSIS_LOCK:
        cached_t = _ANALYSIS_CACHE.get(cache_key)
        if cached_t:
            _ANALYSIS_CACHE[cache_key] = (cached_t[0], cached_t[1], vsa_signals)
    pnf_cols, box = build_pnf(df, box_mode=pnf_box_mode, atr_factor=pnf_atr_factor)
    pnf_vol = pnf_volume(df, pnf_cols, box)
    pnf_t = pnf_targets(df, pnf_cols, box, volumes=pnf_vol)
    structure = structure_progress(events, df, phase=phase)
    tr = find_trading_range(df, pivots)
    profile = volume_profile(df)
    sd = supply_demand(df)
    backtest = backtest_events(df, events, horizon=horizon, min_n=min_n, cost=cost)
    if vsa_backtest:
        try:
            vsa_bt = backtest_vsa(df, horizon=horizon, min_n=min_n, cost=cost, scale=scale)
        except Exception:
            vsa_bt = {"by_label": {}, "benchmark": 0.0, "cost": cost, "note": "VSA回测失败"}
    else:
        vsa_bt = None
    if scale == 240:
        mf = multi_tf_analysis(df)
    else:
        # 分钟级分析: 拉日线作长周期参照 (与日线分析同口径: 250根+实时合并),
        # 识别"日线下跌/分钟反弹"类背离。
        daily_phase = None
        try:
            ddf = add_indicators(merge_realtime_bar(
                fetch_kline(symbol, datalen=250, scale=240), symbol))
            dpivots = find_pivots(ddf, order=6)
            devents = detect_all(ddf, dpivots)
            daily_phase, _ = judge_phase(ddf, dpivots, devents)
        except Exception:
            daily_phase = None
        mf = multi_tf_analysis(df, daily_phase=daily_phase)
    market_env = market_series = fund = flow = holder = None
    news_sentiment = None
    s_name = s_flow = None
    if scale == 240:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            f_env = ex.submit(fetch_market_env)
            f_series = ex.submit(fetch_market_series)
            f_fund = ex.submit(fetch_fundamental, symbol) if confirm_enabled else None
            f_flow = ex.submit(fetch_main_flow, symbol, 120) if confirm_enabled else None
            f_holder = ex.submit(fetch_holder_history, code) if confirm_enabled else None
            f_sector = ex.submit(fetch_sector_flow, symbol) if confirm_enabled else None
            # 新闻情绪始终抓取 (仅公开标题, 轻量): 让 accuracy 快照持续记录 news_score,
            # 否则新闻 A/B 评估 (accuracy.py 的 news_with/news_without) 永远无样本可评。
            f_news = ex.submit(fetch_news_sentiment, symbol, 7)
            f_mnews = ex.submit(fetch_market_news_sentiment)
            f_cal = ex.submit(fetch_forward_calendar, symbol)
            market_env = f_env.result()
            market_series = f_series.result()
            if market_env and f_mnews:
                mnews = f_mnews.result()
                if mnews and mnews.get("count"):
                    market_env = {**market_env, "news": mnews}
            news_sentiment = f_news.result() if f_news else None
            # 价格反应验证 (effort-vs-result): 用发布后量价修正每条新闻权重,
            # 并挂上前瞻事件日历 (解禁/业绩预告/财报披露窗口)。
            try:
                if news_sentiment:
                    news_sentiment = apply_price_validation(news_sentiment, df)
                    cal = f_cal.result() if f_cal else None
                    if cal:
                        news_sentiment = {**news_sentiment, "forward_calendar": cal}
            except Exception:
                pass
            if confirm_enabled:
                fund = f_fund.result()
                flow = f_flow.result()
                holder = f_holder.result()
                s_name, s_flow = f_sector.result()
    else:
        market_env = fetch_market_env()
    rs = relative_strength(df, market_series) if scale == 240 else {}
    # 主链路 pivot_sensitivity 默认档 (normal) 对应 order=6, 与稳健性检查的
    # order=6 结果完全一致, 复用避免重复跑整条 find_pivots+detect_all+judge_phase。
    reuse6 = pivot_order(pivot_sensitivity) == 6
    robustness = robustness_check(df, order6=(pivots, events, phase) if reuse6 else None)

    # ── 基本面 + 主力资金流 (仅日线; confirm_enabled=False 时离线快速, 不抓取) ──
    # fund/flow/holder/s_name/s_flow 已在上面并发池中取得 (命中则复用缓存)。
    sector = None
    conf_q, conf_items = "", []
    if scale == 240 and confirm_enabled:
        if s_flow is not None and len(s_flow) >= 20:
            sector = {"name": s_name,
                      "main20": float(s_flow.tail(20)["main"].sum())}
        conf_q, conf_items = build_confirm_section(phase, df, fund, flow, holder, sector,
                                                   events=events)
        # P4: 把当前板块强度百分位写入近期事件的 sec_pct 特征 (context 预留钩子),
        # 补上模型缺失的市场环境维度; 板块名缺失 → chain 返回 None, 保持缺省。
        if s_name:
            try:
                from .chain import apply_sector_strength, sector_strength_pct
                _sp = sector_strength_pct(s_name)
                if _sp is not None:
                    apply_sector_strength(events, _sp, len(df))
            except Exception:
                pass
    if conf_q == "high":
        phase_label = f"高置信 {phase}"
    elif conf_q == "caution":
        phase_label = f"{phase} (需谨慎)"
    else:
        phase_label = phase

    # ── 补充分析方法过滤层 (筹码/均线/波动率从K线直接计算, 全周期可用;
    #    资金流/基本面排雷依赖网络抓取, 仅日线确认模式) ──
    filters = {
        "chip": chip_analysis(df),
        "ma": granville_signal(df),
        "vol": volatility_signal(df),
        "fund": fundamental_filter(fund) if scale == 240 else None,
        "flow": flow_gate(phase, df, flow) if scale == 240 else None,
    }

    # ── 多维度信号融合: K线结构 / 威科夫事件 / VSA / P&F → 统一多空评分
    #    (mf 高周期方向注入融合权重: 顺周/月线趋势的信号加权, 逆势信号降权)
    fusion = fuse_signals(df, phase, events, vsa_signals, pnf_t, mf=mf,
                          news_sentiment=news_sentiment,
                          forward_calendar=(news_sentiment or {}).get("forward_calendar")
                          if news_sentiment else None)
    if scale == 240 and confirm_enabled:
        # 基本面/资金流确认融入: 强多空时若资金反向, 降一档置信
        flow_net = float(flow.tail(20)["main"].sum()) if flow is not None \
            and len(flow) else 0.0
        if fusion["confidence"] in ("高", "中"):
            if fusion["score"] > 0 and flow_net < 0:
                fusion["confidence"] = "低"
                fusion["conflicts"].append({
                    "bull": ["K线结构", "威科夫事件", "VSA", "P&F"],
                    "bear": ["资金流"], "note": "综合偏多但主力资金净流出, 谨慎"})
            elif fusion["score"] < 0 and flow_net > 0:
                fusion["confidence"] = "低"
                fusion["conflicts"].append({
                    "bull": ["资金流"], "bear": ["K线结构", "威科夫事件", "VSA", "P&F"],
                    "note": "综合偏空但主力资金净流入, 谨慎"})
        # 新闻情绪融入: 强多空时若新闻反向, 降一档置信; 新闻强烈同向时提升置信。
        # (fail-soft: news 缺失/异常不影响主流程, news_score 回退 0 与中性一致)
        try:
            news_score = news_sentiment.get("score", 0.0) if news_sentiment else 0.0
            if fusion["confidence"] in ("高", "中"):
                if fusion["score"] > 0 and news_score < -0.3:
                    fusion["confidence"] = "低"
                    fusion["conflicts"].append({
                        "bull": ["K线结构", "威科夫事件", "VSA", "P&F"],
                        "bear": ["新闻情绪"], "note": f"综合偏多但新闻情绪偏空({news_score:.2f}), 谨慎"})
                elif fusion["score"] < 0 and news_score > 0.3:
                    fusion["confidence"] = "低"
                    fusion["conflicts"].append({
                        "bull": ["新闻情绪"], "bear": ["K线结构", "威科夫事件", "VSA", "P&F"],
                        "note": f"综合偏空但新闻情绪偏多({news_score:.2f}), 谨慎"})
            if fusion["confidence"] == "低":
                if fusion["score"] > 0 and news_score > 0.5:
                    fusion["confidence"] = "中"
                    fusion["resonances"].append(["新闻情绪", "技术面共振"])
                elif fusion["score"] < 0 and news_score < -0.5:
                    fusion["confidence"] = "中"
                    fusion["resonances"].append(["新闻情绪", "技术面共振"])
        except Exception:
            pass

    trade_plan = build_trade_plan(df, pivots, events, phase, structure, targets, pnf_t, tr,
                                  float(df["close"].iloc[-1]))
    # ── 威科夫Pro整合层 (可选增强) ──
    ce = counter_evidence(df, events, phase=phase, structure=structure)
    nt = nine_tests(df, events, pivots=pivots, phase=phase, structure=structure,
                    tr=tr, pnf_t=pnf_t, rs=rs)
    fal = None
    if settings and settings.get("ai_falsify_enabled"):
        try:
            fal = falsify_structure(df, events, phase_label, structure, pnf_t, settings)
        except Exception:
            fal = None
    risk_lines = None
    if settings:
        pv = float(settings.get("portfolio_value", 0) or 0)
        if pv > 0:
            plan = _plan_from_lines(trade_plan)
            risk_lines = position_lines(
                plan, float(df["close"].iloc[-1]), pv,
                max_risk_pct=float(settings.get("risk_pct", 0.02) or 0.02),
                min_rr=float(settings.get("risk_min_rr", 3.0) or 3.0))
    sections = build_conclusion(df, pivots, events, phase_label, detail, wave_lines,
                                targets, pnf_t, vsa_signals, structure,
                                market_env=market_env, trade_plan=trade_plan,
                                backtest=backtest, vsa_bt=vsa_bt, mf=mf, sd=sd, tr=tr,
                                rs=rs, robustness=robustness,
                                data_source=data_source_of(symbol, datalen, scale),
                                fund=fund, flow=flow, conf=conf_items, conf_q=conf_q,
                                fusion=fusion, ce=ce, nt=nt, fal=fal,
                                risk_plan=risk_lines, filters=filters)
    # ── 功能缺失提示: 让用户知道哪些确认层当前未生效 (离线/非日线) ──
    notices = []
    if scale != 240:
        notices.append("分钟级分析: 相对强度(RS)/资金流/基本面确认列不可用, 仅基于K线结构")
    elif not confirm_enabled:
        notices.append("离线模式: 未抓取资金流/基本面/板块, 结论无确认列 (高置信/需谨慎)")
    elif conf_q == "":
        notices.append("资金流/基本面抓取失败: 结论无确认列, 仅基于K线结构 (详见来源节)")
    if notices:
        sections.append(("数据提示", notices))
    text = sections_to_text(sections)
    # ── AI 报告解读 (可选): 把整合了量化速览/完整分节/近期K线的优化报告发给
    # 大模型 (report.build_export_report), 让解读有精确数字与量价上下文, 而不是
    # 只有结论罗列; 通俗化解读后追加为 'AI解读' 节 ──
    if settings and settings.get("ai_interpret_enabled"):
        try:
            from .report import build_export_report
            datalen_txt = next((k for k, v in PERIOD_OPTIONS.items()
                                if v == datalen), f"近{datalen}根")
            ai_report = build_export_report(
                symbol, name, period_txt, datalen_txt, df, sections,
                data_source=data_source_of(symbol, datalen, scale),
                vsa_signals=vsa_signals, events=events, scale=scale)
            interp = interpret_report(ai_report, settings)
            if interp:
                sections.append(("AI解读", interp.splitlines()))
                text = sections_to_text(sections)
        except Exception:
            pass
    summary = build_signal_summary(df, pivots, events, structure, pnf_t,
                                   market_env=market_env, sd=sd,
                                   phase_label=phase_label, fund=fund, flow=flow,
                                   conf_q=conf_q, sector=sector, fusion=fusion,
                                   ce=ce, filters=filters, wave_data=wave_data,
                                   rs=rs)
    market = build_market_labels(code, symbol, df, scale, confirm_enabled)
    if market is not None:
        market["sd_series"] = build_sd_series(df)
        market["conf_q"] = conf_q
        market["fund"] = fund
        market["flow"] = flow
        market["sector"] = sector
        market["news_sentiment"] = news_sentiment
    title = f"{name} ({symbol})  威科夫分析 [{period_txt}]  |  区间 {df['day'].iloc[0]} ~ {df['day'].iloc[-1]}  |  {len(df)}根"
    segs = phase_segments(df, pivots, events)
    if kline_engine == "pyqtgraph":
        from .chart import build_kline_data
        if kline_data is not None:
            kline_data.update(build_kline_data(
                df, pivots, events, title, waves=wave_data["points"],
                draw_waves=draw_waves, draw_locks=draw_locks,
                tr=tr, profile=profile,
                phase=phase.split(" ")[0], segs=segs, sector=sector,
                vsa_signals=vsa_signals,
                symbol=symbol, scale=int(scale)))
        fig = None
    else:
        fig = plot_chart(df, pivots, events, title, fig=fig,
                         waves=wave_data["points"],
                         draw_waves=draw_waves, draw_locks=draw_locks,
                         tr=tr, profile=profile,
                         phase=phase.split(" ")[0], segs=segs, sector=sector,
                         vsa_signals=vsa_signals)
    hist = pnf_history_targets(pnf_cols, box)
    if pnf_engine == "pyqtgraph":
        from .pnf import build_pnf_data
        if pnf_data is not None:
            pnf_data.update(build_pnf_data(
                pnf_cols, box, title, targets=pnf_t, history=hist, df=df,
                box_mode=pnf_box_mode, atr_factor=pnf_atr_factor))
        pnf_fig = None
    else:
        pnf_fig = plot_pnf(df, pnf_cols, box, title, fig=pnf_fig,
                           targets=pnf_t, history=hist,
                           box_mode=pnf_box_mode, atr_factor=pnf_atr_factor)
    ind_fig = None
    if ind_engine == "pyqtgraph":
        from .chart import build_ind_data
        if ind_data is not None:
            rs_series = relative_strength_series(df, market_series) if scale == 240 else None
            ind_data.update(build_ind_data(df, index_series=market_series,
                                           rs_series=rs_series))
    else:
        ind_fig = plot_indicators(df, fig=ind_fig, index_series=market_series)
    if mkt_engine == "pyqtgraph":
        from .chart import build_market_data
        if mkt_data is not None:
            mkt_data.update(build_market_data(market))
    if precomputed_cb is not None:
        try:
            precomputed = {
                "pivots": pivots, "events": events, "phase": phase,
                "pnf_t": pnf_t, "vsa_signals": vsa_signals, "fusion": fusion,
                "targets": targets, "trade_plan": trade_plan, "vsa_bt": vsa_bt,
                "news_sentiment": news_sentiment,
            }
            precomputed_cb(df, symbol, code, scale, datalen, name,
                           phase_label, conf_q, precomputed)
        except Exception:
            import traceback
            traceback.print_exc()
    return text, fig, pnf_fig, ind_fig, summary, sections, market, segs
