"""导出报告构建: 面向大模型 (DeepSeek 等) 的完整结构化报告。

旧版导出只写当前选中的单个结论标签 (如"概览"), 信息量太小, 喂给大模型
解读时缺乏精确数字与量价上下文。build_export_report 把全部分析内容拼成
一份机器可读的完整报告:

1. 标的/周期/时间 元信息头
2. 关键量化速览 (现价/涨跌/均线/ATR/布林/MACD/KDJ/量比等最新精确值)
3. 信号汇总 (多空倾向卡片)
4. 完整分节结论 (结论面板全部标签: 阶段/事件/支撑阻力/结构/VSA/目标/
   波浪/大盘/交易计划/供需/多周期/历史胜率/买点卖点 等)
5. 近期K线明细 (最近 rows 根 OHLCV/涨跌/量比 原始数据)
6. 指标明细 (最近 rows 根 均线/ATR/MACD/KDJ/量能 完整序列)
7. VSA 信号全量 (所有非中性量价标签 + 含义)
8. 威科夫事件全量 (近期所有事件 + 确认状态)

只读 df/sections/cards, 不触发网络; 纯文本返回, 供界面导出 .txt 或
直喂 AI 解读 (interpret_report)。
"""
import numpy as np

from .conclusion import sections_to_text
from .config import EVENT_CN, VSA_CN


def _fmt_price(v):
    """价格格式化: NaN/None → 'N/A'。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if not np.isfinite(v):
        return "N/A"
    return f"{v:.2f}"


def _fmt_signed(v, digits=2):
    """带符号数字: 涨跌幅/盈亏/RS 用。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    if not np.isfinite(v):
        return "N/A"
    return f"{v:+.{digits}f}"


def _day_str(v):
    """day 列格式化 (datetime / Timestamp / str 兼容)。"""
    if v is None:
        return "N/A"
    return str(v)[:10]


def _safe_get(df, col, idx=-1):
    """取 df 某列第 idx 个值; 列不存在/NaN → None。"""
    if col not in df.columns:
        return None
    try:
        v = df.iloc[idx][col]
    except (IndexError, KeyError):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


# ── 关键量化速览 ──

def build_quick_snapshot(df):
    """关键量化速览: 最新一根 bar 的精确数值成块。"""
    if df is None or not len(df):
        return ["(无K线数据)"]
    lines = []
    last = df.iloc[-1]
    close = _safe_get(df, "close")
    if close is not None:
        prev = _safe_get(df, "close", -2)
        chg = (close / prev - 1) * 100 if prev else None
        vol = _safe_get(df, "volume")
        lines.append(f"最新价: {close:.2f} ({_day_str(last.get('day'))}"
                     f"{f', 当日 {_fmt_signed(chg)}%' if chg is not None else ''})")
        lines.append(f"当日成交: {vol / 1e4:.1f} 万手" if vol is not None
                     else "当日成交: N/A")
    for col, label in (("price_ma5", "MA5"), ("price_ma10", "MA10"),
                       ("price_ma20", "MA20"), ("price_ma50", "MA50"),
                       ("price_ma120", "MA120"), ("price_ma200", "MA200")):
        v = _safe_get(df, col)
        if v is not None:
            lines.append(f"{label}: {v:.2f}")
    atr = _safe_get(df, "atr")
    if atr is not None:
        lines.append(f"ATR(14): {atr:.2f}")
    boll = (_safe_get(df, "boll_up"), _safe_get(df, "boll_mid"),
            _safe_get(df, "boll_dn"))
    if any(v is not None for v in boll):
        lines.append("BOLL(20,2): "
                     f"上 {_fmt_price(boll[0])} / 中 {_fmt_price(boll[1])} / 下 {_fmt_price(boll[2])}")
    macd = (_safe_get(df, "macd_dif"), _safe_get(df, "macd_dea"),
            _safe_get(df, "macd_hist"))
    if any(v is not None for v in macd):
        lines.append(f"MACD: DIF {_fmt_price(macd[0])} / DEA {_fmt_price(macd[1])} / "
                     f"柱 {_fmt_signed(macd[2])}")
    kdj = (_safe_get(df, "kdj_k"), _safe_get(df, "kdj_d"), _safe_get(df, "kdj_j"))
    if any(v is not None for v in kdj):
        lines.append(f"KDJ: K {_fmt_price(kdj[0])} / D {_fmt_price(kdj[1])} / J {_fmt_price(kdj[2])}")
    vr = _safe_get(df, "vol_ratio_20")
    vz = _safe_get(df, "vol_z_20")
    if vr is not None or vz is not None:
        lines.append("量能: "
                     f"量比20 {_fmt_price(vr) if vr is not None else 'N/A'} / "
                     f"量Z20 {_fmt_signed(vz) if vz is not None else 'N/A'}")
    return lines


# ── 近期K线明细 ──

def build_recent_bars(df, rows=20):
    """最近 rows 根 bar 明细: 日期 开 高 低 收 涨跌% 量(万手) 量比20。"""
    if df is None or not len(df):
        return ["(无K线数据)"]
    lines = ["日期        开盘    最高    最低    收盘    涨跌%    量(万手)  量比20"]
    n = min(rows, len(df))
    for i in range(len(df) - n, len(df)):
        r = df.iloc[i]
        prev = _safe_get(df, "close", i - 1)
        close = _safe_get(df, "close", i)
        chg = (close / prev - 1) * 100 if (close is not None and prev) else None
        vol = _safe_get(df, "volume", i)
        vr = _safe_get(df, "vol_ratio_20", i)
        lines.append(
            f"{_day_str(r.get('day'))}  "
            f"{_fmt_price(_safe_get(df, 'open', i)):>8s}  "
            f"{_fmt_price(_safe_get(df, 'high', i)):>8s}  "
            f"{_fmt_price(_safe_get(df, 'low', i)):>8s}  "
            f"{_fmt_price(close):>8s}  "
            f"{_fmt_signed(chg):>7s}  "
            f"{(f'{vol / 1e4:.1f}') if vol is not None else 'N/A':>10s}  "
            f"{_fmt_price(vr) if vr is not None else 'N/A'}")
    return lines


# ── 指标明细 (完整序列) ──

def build_indicator_table(df, rows=30):
    """最近 rows 根 bar 的完整指标序列 (均线/ATR/MACD/KDJ/量能)。

    逐根给出指标数值, 让模型能独立校验均线排列、MACD 金叉死叉、KDJ 超买
    超卖等量价关系, 而不只是信任结论文本。
    """
    if df is None or not len(df):
        return ["(无K线数据)"]
    cols = [("close", "收盘"), ("price_ma5", "MA5"), ("price_ma10", "MA10"),
            ("price_ma20", "MA20"), ("price_ma50", "MA50"),
            ("price_ma120", "MA120"), ("price_ma200", "MA200"),
            ("atr", "ATR"), ("boll_up", "BOLL上"), ("boll_dn", "BOLL下"),
            ("macd_dif", "DIF"), ("macd_dea", "DEA"), ("macd_hist", "MACD柱"),
            ("kdj_k", "K"), ("kdj_d", "D"), ("kdj_j", "J"),
            ("vol_ratio_20", "量比20"), ("vol_z_20", "量Z20")]
    header = "日期        " + "  ".join(c for _, c in cols)
    lines = [header]
    n = min(rows, len(df))
    for i in range(len(df) - n, len(df)):
        r = df.iloc[i]
        cells = [_fmt_price(_safe_get(df, c, i)) for c, _ in cols]
        lines.append(_day_str(r.get("day")).ljust(11) + "  " + "  ".join(cells))
    return lines


# ── VSA 信号全量 ──

def _derive_vsa(df, scale):
    """df 缺失 vsa 结果时重算 (与 analysis 同源, 默认灵敏度)。"""
    from .vsa import vsa_classify
    return vsa_classify(df, scale=scale) if df is not None and len(df) else None


def build_vsa_table(df, vsa_signals=None, scale=240, max_rows=80):
    """全部 VSA 量价信号 (剔除中性 N): 日期 标签 中文 描述。

    默认展示最近 max_rows 条非中性信号 (正常逐根都标 N, 有效标签较稀疏,
    全量列出信息密度高)。
    """
    if vsa_signals is None:
        vsa_signals = _derive_vsa(df, scale)
    if not vsa_signals:
        return ["(无 VSA 信号)"]
    lines = ["日期        标签    中文        描述"]
    for s in vsa_signals:
        if s.get("label") in ("N", None):
            continue
        lines.append(f"{_day_str(s.get('date'))}  "
                     f"{str(s.get('label')):<5s}  "
                     f"{VSA_CN.get(s.get('label'), ''):<10s}  {s.get('desc', '')}")
    if len(lines) <= 1:
        return ["(近窗口无非中性 VSA 信号)"]
    if len(lines) - 1 > max_rows:
        return lines[:1] + lines[-(max_rows - 1):] + \
            [f"... (共 {len(lines) - 1} 条, 只显示最近 {max_rows} 条)"]
    return lines


# ── 威科夫事件全量 ──

def _derive_events(df):
    """df 缺失 events 时重算 (与 analysis 同源, 默认灵敏度)。"""
    from .events import detect_all
    from .indicators import find_pivots
    if df is None or not len(df):
        return []
    pivots = find_pivots(df)
    return detect_all(df, pivots)


def build_event_table(df, events=None, max_rows=40):
    """近期全部威科夫事件: 日期 类型 价位 中文 描述 (含确认状态)。"""
    if events is None:
        events = _derive_events(df)
    if not events:
        return ["(无威科夫事件)"]
    events = sorted(events, key=lambda e: e.get("idx", 0))
    recent = events[-max_rows:] if len(events) > max_rows else events
    lines = ["日期        类型      价位     状态  中文    描述"]
    for e in recent:
        cf = e.get("confirmed")
        mark = "✓确认" if cf is True else "✗未确认" if cf is False else "…待确认"
        lines.append(f"{_day_str(e.get('date'))}  "
                     f"{str(e.get('type')):<8s}  "
                     f"{_fmt_price(e.get('price')):>8s}  {mark:<6s}  "
                     f"{EVENT_CN.get(e.get('type'), e.get('type', ''))}  {e.get('desc', '')}")
    if len(events) > max_rows:
        lines.append(f"... (共 {len(events)} 条, 只显示最近 {max_rows} 条)")
    return lines


# ── 信号汇总 ──

def build_signal_summary_text(cards):
    """信号汇总卡片 → 文本块 (multi 倾向标注)。"""
    if not cards:
        return []
    route = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性",
             "caution": "谨慎"}
    return [f"- {c.get('label', '')}: {c.get('value', '')} "
            f"[{route.get(c.get('tone', 'neutral'), c.get('tone', ''))}]"
            for c in cards]


# ── 完整报告 ──

def build_export_report(symbol, name, scale_txt, period_txt, df, sections,
                        summary_cards=None, data_source=None, rows=20,
                        vsa_signals=None, events=None, scale=240,
                        indicator_rows=30):
    """拼装面向大模型解读的完整导出报告文本。

    symbol/name: 代码与名称; scale_txt/period_txt: 周期与时间段显示文本;
    df: 分析用 DataFrame (需包含 indicators.add_indicators 输出列);
    sections: build_conclusion 返回的 [(标题, [行,...])];
    summary_cards: build_signal_summary 返回的卡片列表 (可 None);
    data_source: 数据源名称 (可 None, 分节里通常已含);
    rows: 近期K线明细条数; indicator_rows: 指标明细条数;
    vsa_signals/events: 分析产物 (可 None, 缺省从 df 重算);
    scale: VSA 周期校准用 (缺省重算时)。
    """
    blocks = []
    head = ["威科夫分析报告 (DeepSeek 解读优化版)",
            f"标的: {name or ''} ({symbol})",
            f"周期: {scale_txt} / {period_txt}"]
    if df is not None and len(df):
        head.append(f"数据区间: {_day_str(df['day'].iloc[0])} ~ "
                    f"{_day_str(df['day'].iloc[-1])} ({len(df)}根)")
    if data_source:
        head.append(f"数据源: {data_source}")
    blocks.append("\n".join(head))

    blocks.append("【关键量化速览】\n" + "\n".join(build_quick_snapshot(df)))

    card_lines = build_signal_summary_text(summary_cards)
    if card_lines:
        blocks.append("【信号汇总】\n" + "\n".join(card_lines))

    full_text = sections_to_text(sections).strip()
    if full_text:
        blocks.append("【完整分析结论】\n" + full_text)

    blocks.append("【近期K线明细】\n" + "\n".join(build_recent_bars(df, rows=rows)))

    ind_lines = build_indicator_table(df, rows=indicator_rows)
    if ind_lines:
        blocks.append("【指标明细】\n" + "\n".join(ind_lines))

    vsa_lines = build_vsa_table(df, vsa_signals=vsa_signals, scale=scale)
    if vsa_lines:
        blocks.append("【VSA信号全量】\n" + "\n".join(vsa_lines))

    ev_lines = build_event_table(df, events=events)
    if ev_lines:
        blocks.append("【威科夫事件全量】\n" + "\n".join(ev_lines))

    blocks.append("(报告由威科夫分析工具生成, 仅供技术研究参考, 不构成投资建议)")
    return "\n\n".join(blocks)
