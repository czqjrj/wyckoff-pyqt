# -*- coding: utf-8 -*-
"""结论生成: 结构化信号汇总 + 分节结论 + 文本拼接。"""
import pandas as pd

from .config import EVENT_CN, W_RECENT, ER_BULL, ER_BEAR, VSA_CN, vsa_dir
from .vsa_explain import VSA_EXPLAIN, meaning_pure, LONG_ONLY_NOTE
from .phases import judge_phase
from .counterevidence import ce_lines
from .ninetests import nt_lines
from .filters import build_filter_sections, filter_summary_cards
from .falsify import fal_lines


def _recent_win_rate(kind, sig_list, min_n=10):
    """取近期信号列表里各类型的历史 20 根方向命中占比 (样本>=min_n 才标注)。
    方向化: 空头信号下跌记命中 (config.vsa_dir/event_dir)。
    返回 [(类型, 胜率, 样本数)] 按出现次数降序。
    """
    from .signal_accuracy import load_win_rates
    rates = load_win_rates(horizon=20)
    out = []
    for s in sig_list:
        t = s.get("type") if kind == "event" else s.get("label")
        if not t:
            continue
        rec = rates.get((kind, str(t)))
        if not rec or rec["n"] < min_n:
            continue
        if any(x[0] == t for x in out):
            continue
        out.append((t, rec["win"], rec["n"]))
    return sorted(out, key=lambda x: -x[2])


def _recent_event_win_rate(recent_events):
    return _recent_win_rate("event", recent_events)


def _recent_vsa_win_rate(vsa_signals):
    return _recent_win_rate("vsa", vsa_signals)


def _er_ratio(df, window=20):
    seg = df.iloc[-window:]
    up = seg.loc[seg["direction"] == 1, "volume"]
    dn = seg.loc[seg["direction"] == -1, "volume"]
    up_v = up.mean() if len(up) else 0
    dn_v = dn.mean() if len(dn) else 0
    if up_v and dn_v:
        return up_v / dn_v
    if not up_v and not dn_v:
        return 1.0
    return float("inf") if up_v else 0.0


def build_signal_summary(df, pivots, events, structure=None, pnf_t=None,
                         market_env=None, sd=None, phase_label=None, fund=None,
                         flow=None, conf_q=None, sector=None, fusion=None, ce=None,
                         filters=None, wave_data=None, rs=None):
    """结构化信号汇总, 返回 [{label, value, tone}], tone: bullish/bearish/neutral"""
    last_close = float(df["close"].iloc[-1])
    recent = [e for e in events if e["idx"] >= len(df) - W_RECENT]
    types = [e["type"] for e in recent]
    out = []

    # 多维度融合综合评分 (置顶)
    if fusion:
        fscore = fusion["score"]
        fconf = fusion["confidence"]
        if fscore > 15:
            fval = (f"综合偏多 {fscore:+.0f} ({fconf}置信)")
            ftone = "bullish"
        elif fscore < -15:
            fval = (f"综合偏空 {fscore:+.0f} ({fconf}置信)")
            ftone = "bearish"
        else:
            fval = (f"综合中性 {fscore:+.0f} ({fconf}置信)")
            ftone = "neutral"
        if fusion["conflicts"]:
            fval += " ⚠矛盾"
            ftone = "caution" if ftone in ("bullish", "bearish") else ftone
        out.append({"label": "综合", "value": fval, "tone": ftone})

    # 大盘背景
    if market_env:
        out.append({"label": "大盘", "value": market_env["env"],
                    "tone": market_env["tone"]})

    # 供需强度
    if sd:
        ratio = sd["ratio"]
        sd_tone = "bullish" if ratio > 1.2 else "bearish" if ratio < 0.8 else "neutral"
        sd_txt = f"供需比 {ratio:.2f}" if ratio != float("inf") else "需求≫供给"
        out.append({"label": "供需", "value": sd_txt, "tone": sd_tone})

    # 相对强度 (vs 大盘): 全部窗口为正=强于大盘, 全负=弱于大盘, 混合=分歧
    if rs:
        items = sorted(rs.items())
        if items:
            vals = [v for _, v in items]
            rl = " / ".join(f"{w}日{v:+.1f}%" for w, v in items)
            if all(v > 0 for v in vals):
                rs_tone, rs_tag = "bullish", "强于大盘"
            elif all(v < 0 for v in vals):
                rs_tone, rs_tag = "bearish", "弱于大盘"
            elif vals[0] > 0:
                rs_tone, rs_tag = "neutral", "短线强/长线弱(分歧)"
            else:
                rs_tone, rs_tag = "neutral", "短线弱/长线强(分歧)"
            out.append({"label": "相对", "value": f"{rs_tag} ({rl})", "tone": rs_tone})

    # 吸筹/派发结构进度
    if structure:
        letter = structure[0]
        tone = {"A": "neutral", "B": "neutral", "C": "bullish", "D": "bullish",
                "E": "bullish"}.get(letter, "neutral")
        if "Distribution" in (structure[2] or ""):
            tone = {"A": "neutral", "B": "bearish", "C": "bearish",
                    "D": "bearish", "E": "bearish"}.get(letter, "neutral")
        out.append({"label": "结构", "value": f"{structure[2].splitlines()[0]} Phase {letter}",
                    "tone": tone})

    # 反面证据积分: 当前假设被质疑程度 (红灯=紧急反转提示)
    if ce and ce.get("hypothesis"):
        lvl = ce["alert_level"]
        if lvl == "RED":
            out.append({"label": "反面", "value": f"红灯 {ce['score']:.0f} 假设存疑",
                        "tone": "bearish"})
        elif lvl == "ORANGE":
            out.append({"label": "反面", "value": f"橙灯 {ce['score']:.0f} 谨慎",
                        "tone": "caution"})
        elif lvl == "YELLOW":
            out.append({"label": "反面", "value": f"黄灯 {ce['score']:.0f} 关注",
                        "tone": "neutral"})
        else:
            out.append({"label": "反面", "value": f"正常 {ce['score']:.0f}",
                        "tone": "bullish"})

    # 当前方向 + 是否到位 + 空间 (基于 P&F 计数目标)
    if pnf_t and "direction" in pnf_t:
        d = pnf_t["direction"]
        ups = sorted(v for k, v in pnf_t.items()
                     if k.endswith("上方目标") and isinstance(v, (int, float)))
        dns = sorted((v for k, v in pnf_t.items()
                      if k.endswith("下方目标") and isinstance(v, (int, float))),
                     reverse=True)
        if d == "up" and ups:
            tgt = ups[0]
            space = (tgt - last_close) / last_close * 100
            if last_close >= tgt:
                txt, tone = f"上涨 · 已到位 (现价 {last_close:.2f} 已达目标)", "bullish"
            else:
                txt, tone = f"上涨 · 未到位 · 空间 {space:.1f}%", "bullish"
        elif d == "down" and dns:
            tgt = dns[0]
            space = (last_close - tgt) / last_close * 100
            if last_close <= tgt:
                txt, tone = f"下跌 · 已到位 (现价 {last_close:.2f} 已达目标)", "bearish"
            else:
                txt, tone = f"下跌 · 未到位 · 空间 {space:.1f}%", "bearish"
        elif d == "up":
            txt, tone = "上涨 (突破区间上沿)", "bullish"
        elif d == "down":
            txt, tone = "下跌 (跌破区间下沿)", "bearish"
        else:
            txt, tone = "区间震荡 · 无目标空间", "neutral"
        out.append({"label": "方向", "value": txt, "tone": tone})
        if ups:
            out.append({"label": "目标↑", "value": " / ".join(f"{v:.2f}" for v in ups),
                        "tone": "bullish"})
        if dns:
            out.append({"label": "目标↓", "value": " / ".join(f"{v:.2f}" for v in dns),
                        "tone": "bearish"})
        if "tr_top" in pnf_t and "tr_bottom" in pnf_t:
            out.append({"label": "区间", "value": f"{pnf_t['tr_bottom']:.2f} ~ {pnf_t['tr_top']:.2f}",
                        "tone": "neutral"})

    # 阶段
    phase, _ = judge_phase(df, pivots, events)
    tone = {"底部整固": "bullish", "上升趋势": "bullish", "区间整理": "neutral",
            "顶部构筑": "bearish", "下跌趋势": "bearish"}.get(
        phase.split(" ")[0], "neutral")
    # 置信修饰覆盖色调: 需谨慎 → 琥珀 (即使基础阶段偏多/偏空, 也要降级提醒)
    if phase_label and "需谨慎" in phase_label:
        tone = "caution"
    out.append({"label": "阶段", "value": phase_label or phase, "tone": tone})

    # 波浪位置 (增强波浪计数: 当前浪位 + 方向)
    if wave_data and wave_data.get("position"):
        w_pos = wave_data["position"]
        w_done = wave_data.get("done")
        w_tone = "bullish" if (wave_data.get("kind") == "impulse"
                               and wave_data.get("direction") == "up") else \
                 "bearish" if (wave_data.get("kind") == "impulse"
                               and wave_data.get("direction") == "down") else "neutral"
        w_val = w_pos
        if w_done:
            w_val += " (结构完成)"
        else:
            w_val += " (进行中)"
        out.append({"label": "波浪", "value": w_val, "tone": w_tone})

    # 基本面 / 资金流卡片
    if fund:
        pe, pb = fund.get("pe_ttm"), fund.get("pb")
        if pe and pe > 0:
            v_tone = "bullish" if pe < 25 else "bearish" if pe > 45 else "neutral"
            out.append({"label": "估值", "value": f"PE {pe:.1f} / PB {pb or 0:.2f}",
                        "tone": v_tone})
        elif pb and pb > 0:
            out.append({"label": "估值", "value": f"亏损 / PB {pb:.2f}",
                        "tone": "bearish" if pe and pe < 0 else "neutral"})
        if fund.get("net_growth") is not None:
            g = fund["net_growth"] * 100
            out.append({"label": "成长", "value": f"净利同比 {g:+.0f}%",
                        "tone": "bullish" if g >= 0 else "bearish"})
    if flow is not None and len(flow):
        m20 = float(flow.tail(20)["main"].sum()) / 1e8
        out.append({"label": "主力", "value": f"近20日 {m20:+.2f}亿",
                    "tone": "bullish" if m20 > 0 else "bearish"})
    if sector and sector.get("name") and sector.get("main20") is not None:
        s20 = sector["main20"] / 1e8
        out.append({"label": "板块", "value": f"{sector['name']} 近20日 {s20:+.2f}亿",
                    "tone": "bullish" if s20 > 0 else "bearish"})
    if fund and fund.get("buy_vol") and fund.get("sell_vol"):
        b, s = float(fund["buy_vol"]), float(fund["sell_vol"])
        if b + s > 0:
            out.append({"label": "订单", "value": f"外盘{b/1e4:.0f}万/内盘{s/1e4:.0f}万",
                        "tone": "bullish" if b > s else "bearish"})

    # 突破/吸筹信号 (派发/下跌阶段: Spring/JOC/SOS 为假信号或失败突破, 不能看多)
    bear_phase = phase.startswith(("顶部构筑", "下跌趋势"))
    if "Spring" in types:
        if bear_phase:
            out.append({"label": "吸筹", "value": "Spring(派发中可能是假信号)",
                        "tone": "bearish"})
        else:
            out.append({"label": "吸筹", "value": "Spring弹簧已现", "tone": "bullish"})
    if "JOC" in types:
        if bear_phase:
            out.append({"label": "突破", "value": "JOC(派发中, 谨防假突破)", "tone": "bearish"})
        else:
            out.append({"label": "突破", "value": "JOC放量新高", "tone": "bullish"})
    elif "SOS" in types:
        if bear_phase:
            out.append({"label": "突破", "value": "SOS(派发中, 谨防冲高回落)", "tone": "bearish"})
        else:
            out.append({"label": "突破", "value": "SOS强势信号", "tone": "bullish"})
    if "UTAD" in types:
        _tone, _val = "bearish", "UTAD上冲派发"
        if conf_q == "high" and phase.startswith(("顶部构筑", "下跌趋势")):
            _val = "UTAD上冲派发(资金确认)"
        elif conf_q == "caution" and phase.startswith(("顶部构筑", "下跌趋势")):
            _val = "UTAD上冲派发(资金矛盾)"
        out.append({"label": "警示", "value": _val, "tone": _tone})
    if "BC" in types:
        _tone, _val = "bearish", "BC买入高潮"
        if conf_q == "high" and phase.startswith(("顶部构筑", "下跌趋势")):
            _val = "BC买入高潮(资金确认)"
        elif conf_q == "caution" and phase.startswith(("顶部构筑", "下跌趋势")):
            _val = "BC买入高潮(资金矛盾)"
        elif not phase.startswith(("顶部构筑", "下跌趋势")):
            _tone, _val = "neutral", "BC买入高潮(非派发阶段,待观察)"
        out.append({"label": "警示", "value": _val, "tone": _tone})
    if "ST" in types:
        if bear_phase:
            out.append({"label": "筑底", "value": "ST二次测试(派发中视为反抽)",
                        "tone": "neutral"})
        else:
            out.append({"label": "筑底", "value": "ST二次测试", "tone": "bullish"})
    if "SC" in types:
        out.append({"label": "筑底", "value": "SC卖出高潮", "tone": "neutral"})

    # 均线
    ma20, ma50 = df["price_ma20"].iloc[-1], df["price_ma50"].iloc[-1]
    ma200 = df["price_ma200"].iloc[-1]
    if last_close > ma20 > ma50:
        out.append({"label": "均线", "value": "多头排列(价>MA20>MA50)", "tone": "bullish"})
    elif last_close < ma20 < ma50:
        out.append({"label": "均线", "value": "空头排列(价<MA20<MA50)", "tone": "bearish"})
    else:
        out.append({"label": "均线", "value": "均线纠缠", "tone": "neutral"})
    if pd.notna(ma200):
        t = "牛市" if last_close > ma200 else "熊市"
        out.append({"label": "年线", "value": f"MA200 {'上方' if last_close > ma200 else '下方'}({t})",
                    "tone": "bullish" if last_close > ma200 else "bearish"})

    # 量价
    er = _er_ratio(df, 20)
    if er > ER_BULL:
        out.append({"label": "量价", "value": f"涨带量/跌缩量 ({er:.2f})", "tone": "bullish"})
    elif er < ER_BEAR:
        out.append({"label": "量价", "value": f"跌带量/涨缩量 ({er:.2f})", "tone": "bearish"})
    else:
        out.append({"label": "量价", "value": f"多空均衡 ({er:.2f})", "tone": "neutral"})

    # 补充分析方法过滤卡片 (筹码/均线/波动率/资金门/排雷)
    if filters:
        out.extend(filter_summary_cards(filters))

    if not out:
        out.append({"label": "状态", "value": "信号不足, 观望", "tone": "neutral"})
    return out


def build_conclusion(df, pivots, events, phase, detail, wave_lines=None, targets=None,
                     pnf_t=None, vsa_signals=None, structure=None,
                     market_env=None, trade_plan=None, backtest=None, vsa_bt=None,
                     mf=None, sd=None,
                     tr=None, rs=None, robustness=None, data_source=None,
                     phase_label=None, fund=None, flow=None, conf=None, conf_q=None,
                     fusion=None, ce=None, nt=None, fal=None, risk_plan=None,
                     filters=None):
    """生成结构化结论: 返回 [(标题, [行,...]), ...] 列表, 供 GUI 竖标签展示。
    `sections_to_text` 可将其拼回为纯文本。
    phase_label: 带置信度修饰的展示用阶段文本; fund/flow/conf: 基本面与资金流证据;
    conf_q: "high"/"caution"/"" 置信修饰, 用于分级卖信号 (实证见 backtest_confirm.py)。
    vsa_bt: VSA 标签滚动回测 (backtest_vsa 输出), 见 "VSA回测" 小节。
    ce/nt/fal/risk_plan/filters: 可选增强层 (反面证据/九大检验/AI证伪/仓位/补充过滤),
    缺省不渲染。filters 结构见 filters.build_filter_sections。"""
    last_close = float(df["close"].iloc[-1])
    last_date = df["day"].iloc[-1].date()
    recent = [e for e in events if e["idx"] >= len(df) - W_RECENT]
    sections = []

    # ── 概览 ──
    head = [f"最新价 {last_close:.2f}  ({last_date})   当前阶段: {phase_label or phase}",
            f"解读: {detail}"]
    if data_source:
        head.append(f"数据源: {data_source}")
    sections.append(("概览", head))

    # ── 多维度共振 (K线/事件/VSA/P&F 融合) ──
    if fusion:
        fx = ["综合判断: " + fusion["summary"]]
        for d in fusion["dims"]:
            fx.append(f"  {d['name']}: {d['score']:+.0f} ({d['bias']}, {d['detail']})")
        for names in fusion["resonances"]:
            fx.append(f"  ★ 共振: {'、'.join(names)} 同向")
        for c in fusion["conflicts"]:
            fx.append(f"  ⚠ 矛盾: {c['note']}")
        sections.insert(1, ("多维度共振", fx))

    # ── 近期事件 ──
    ev_lines = ["近期威科夫事件:"]
    for e in recent[-12:]:
        cf = e.get("confirmed")
        mark = "✓" if cf is True else "✗" if cf is False else "…"
        ev_lines.append(f"  {mark} {e['date'].date()}  {e['type']:<7s} @ {e['price']:.2f}  {EVENT_CN.get(e['type'], e['type'])} - {e['desc']}")
    n_ok = sum(1 for e in recent if e.get("confirmed") is True)
    n_fail = sum(1 for e in recent if e.get("confirmed") is False)
    if n_ok or n_fail:
        ev_lines.append(f"  跟进确认: 已确认 {n_ok} / 未确认 {n_fail} (✓确认 ✗未确认 …待确认; 只在✓后进场)")
    hit = _recent_event_win_rate(recent)
    if hit:
        ev_lines.append("  历史胜率: " + "  ".join(
            f"{k} {v*100:.0f}% ({n}例)" for k, v, n in hit))
    sections.append(("近期事件", ev_lines))

    # ── 支撑 / 阻力 (聚焦近90日枢轴; 支撑在现价下方, 阻力在现价上方) ──
    cutoff = df["day"].iloc[max(0, len(df) - 90)]
    lows = [p for p in pivots if p["type"] == "low" and p["date"] >= cutoff]
    highs = [p for p in pivots if p["type"] == "high" and p["date"] >= cutoff]
    sup = sorted({round(p["price"], 2) for p in lows if p["price"] < last_close}, reverse=True)
    res = sorted({round(p["price"], 2) for p in highs if p["price"] > last_close})
    sr_lines = [f"近期支撑: {', '.join(map(str, sup)) if sup else 'N/A'}",
                f"近期阻力: {', '.join(map(str, res)) if res else 'N/A'}"]
    sections.append(("支撑/阻力", sr_lines))

    # ── 吸筹/派发结构进度 ──
    if structure:
        st_lines = ["结构进度 (威科夫示意图):"]
        st_lines += [f"  {ln}" for ln in structure]
        sections.append(("结构进度", st_lines))

    # ── 反面证据积分 (当前假设被质疑程度) ──
    if ce:
        sections.append(("反面证据", ce_lines(ce)))

    # ── 九大买卖检验 (吸筹/派发成熟度) ──
    if nt:
        sections.append(("九大检验", nt_lines(nt, phase=phase)))

    # ── 补充分析方法过滤层 (筹码/均线/波动率/资金/基本面排雷) ──
    if filters:
        for title, flines in build_filter_sections(filters):
            if flines:
                sections.append((title, flines))

    # ── AI 反向证伪 (可选, 唱反调复核) ──
    if fal is not None:
        sections.append(("AI证伪", fal_lines(fal)))

    # ── VSA 量价信号 (取最近8个) ──
    if vsa_signals:
        vsa_lines = ["VSA 量价信号 (近期):"]
        for s in vsa_signals[-8:]:
            cn = VSA_CN.get(s["label"], s["label"])
            vsa_lines.append(f"  {s['date'].date()}  {s['label']:<4s} {cn}  {s['desc']}")
        vsa_hit = _recent_vsa_win_rate(vsa_signals[-8:])
        if vsa_hit:
            vsa_lines.append("  历史胜率: " + "  ".join(
                f"{k} {v*100:.0f}% ({n}例)" for k, v, n in vsa_hit))
        # 信号解释: 对本次实际出现的标签附完整解释 (含义/方向/关注/建议/流程/失效)
        seen = {}
        for s in vsa_signals[-8:]:
            seen.setdefault(s["label"], s["date"])
        if seen:
            vsa_lines.append("")
            vsa_lines.append("信号解释:")
            for lb in sorted(seen, key=lambda k: (-len(seen), k)):
                e = VSA_EXPLAIN.get(lb)
                if not e:
                    continue
                vsa_lines.append(f"  {lb} {VSA_CN.get(lb, lb)}: {meaning_pure(e['meaning'])}")
                vsa_lines.append(f"    方向: {e['direction']} · 关注: {e['watch']}")
                vsa_lines.append(f"    建议: {e['advice']}")
                vsa_lines.append(f"    流程: {e['role']} · 失效: {e['fail']}")
            vsa_lines.append(f"  注: {LONG_ONLY_NOTE}")
        sections.append(("VSA 信号", vsa_lines))

    # ── 目标价 (威科夫点数计算 + P&F) ──
    tgt_lines = []
    if targets:
        tgt_lines.append("目标价 (威科夫点数计算):")
        for k, v in targets.items():
            arrow = "▲ 上方" if v > last_close else "▼ 下方"
            tgt_lines.append(f"  {k}: {v:.2f}  {arrow}")
    if pnf_t:
        tgt_lines.append("P&F 点数图目标:")
        for k, v in pnf_t.items():
            if not isinstance(v, (int, float)) or k in (
                    "tr_top", "tr_bottom", "tr_width", "tr_start_col", "tr_end_col"):
                continue
            # 目标名含 上方/下方 时按其方向标注, 避免出现 "下方目标: xx ▲ 上方" 这类矛盾表述
            if "下方" in k:
                arrow = "▼ 下方"
            elif "上方" in k:
                arrow = "▲ 上方"
            else:
                arrow = "▲ 上方" if v > last_close else "▼ 下方"
            tgt_lines.append(f"  {k}: {v:.2f}  {arrow}")
    if tgt_lines:
        sections.append(("目标价", tgt_lines))

    # ── 波浪 ──
    if wave_lines:
        sections.append(("波浪结构", list(wave_lines)))

    # ── 大盘背景 (威科夫三击法: 大盘→板块→个股) ──
    if market_env:
        me = market_env
        m_color = {"bullish": "做多友好", "bearish": "逆风/空头", "neutral": "中性"}.get(me["tone"], "")
        m_lines = [f"上证指数 {me['close']:.2f}",
                   f"环境: {me['env']} ({m_color})",
                   f"MA20 {me['ma20']:.2f} / MA50 {me['ma50']:.2f} / MA200 {me['ma200'] if me.get('ma200') else 'N/A'}"]
        if me["tone"] == "bullish":
            m_lines.append("建议: 大盘走强, 个股买点可信度↑")
        elif me["tone"] == "bearish":
            m_lines.append("建议: 大盘走弱, 谨慎追多/控制仓位")
        else:
            m_lines.append("建议: 大盘震荡, 以个股结构为准")
        sections.append(("大盘背景", m_lines))

    # ── 基本面 / 资金流确认 (威科夫三击中的个股基本面+订单流) ──
    conf_lines = []
    if conf:
        conf_lines = [f"  {t}" for t, _tone in conf]
    if fund and fund.get("name"):
        conf_lines.insert(0, f"{fund['name']} · 市值 {fund.get('mcap_yi', 0):.0f}亿 · "
                             f"换手 {fund.get('turnover', 0):.1f}%")
    if conf_lines:
        if phase_label and phase_label.startswith("高置信"):
            conf_lines.append("  → 基本面与资金流相互印证, 阶段置信度提升")
        elif phase_label and "需谨慎" in phase_label:
            conf_lines.append("  → 基本面/资金流与阶段矛盾, 建议降低仓位")
        sections.append(("基本面/资金流确认", conf_lines))

    # ── 交易计划 (入场/止损/目标/盈亏比) ──
    if trade_plan:
        sections.append(("交易计划", list(trade_plan)))

    # ── 仓位建议 (配置账户资产后按单笔风险预算计算) ──
    if risk_plan:
        sections.append(("仓位建议", list(risk_plan)))

    # ── 供需强度对比 ──
    if sd:
        ratio = sd["ratio"]
        if ratio == float("inf"):
            sd_txt = "需求远大于供给 (近20日)"
        else:
            sd_txt = f"需求 {sd['demand'] / 1e6:.0f} 万手量 · 供给 {sd['supply'] / 1e6:.0f} 万手量"
            sd_txt += f" · 供需比 {ratio:.2f}"
        bal = "需求占优(偏多)" if ratio > 1.2 else "供给占优(偏空)" if ratio < 0.8 else "多空均衡"
        sd_lines = [f"近20日累计: {sd_txt} → {bal}"]
        # 今日单日供需 (量×收盘位置, 与累计同口径), 反映当日多空
        try:
            r = df.iloc[-1]
            rng = float(r["range"]) or 1e-9
            cpos = (float(r["close"]) - float(r["low"])) / rng
            d_today = float(r["volume"]) * cpos
            s_today = float(r["volume"]) * (1 - cpos)
            if s_today > 0:
                t_ratio = d_today / s_today
                t_bal = ("需求占优(偏多)" if t_ratio > 1.2 else
                         "供给占优(偏空)" if t_ratio < 0.8 else "多空均衡")
                t_txt = (f"今日单日: 需求 {d_today/1e6:.1f} 万手 · "
                         f"供给 {s_today/1e6:.1f} 万手 · 供需比 {t_ratio:.2f} → {t_bal}")
                if (ratio > 1.2 and t_ratio < 0.8) or (ratio < 0.8 and t_ratio > 1.2):
                    t_txt += " (与累计方向相反)"
                sd_lines.append(t_txt)
        except (TypeError, ValueError, IndexError):
            pass
        sections.append(("供需强度", sd_lines))

    # ── 多周期共振 (日线+周线+月线 + 相对强度) ──
    if mf:
        # 分钟级: 展示日线长周期参照与背离提示
        if mf.get("daily_phase") and mf.get("intraday_phase"):
            d_phase = mf["daily_phase"]
            i_phase = mf["intraday_phase"]
            div = mf.get("trend_divergence")
            mf_lines = [f"当前周期: {i_phase}",
                        f"日线参照: {d_phase}"]
            if div == "up":
                mf_lines.append("背离提示: 短周期(分钟级)偏多, 但日线长周期偏空 → "
                                "大概率是超跌反弹, 不宜逆日线重仓, 注意上方压力")
            elif div == "down":
                mf_lines.append("背离提示: 短周期(分钟级)偏空, 但日线长周期偏多 → "
                                "短线回调, 不改变日线方向, 可等企稳")
            else:
                mf_lines.append("共振结论: 当前周期与日线方向一致")
            if mf.get("weekly_phase"):
                mf_lines.append(f"周线阶段: {mf['weekly_phase']}")
            if mf.get("monthly_phase"):
                mf_lines.append(f"月线阶段: {mf['monthly_phase']}")
            sections.append(("多周期共振", mf_lines))
            if rs:
                items = sorted(rs.items())
                rl = "  ".join(f"{w}日RS {v:+.1f}%" for w, v in items)
                vals = [v for _, v in items]
                if all(v > 0 for v in vals):
                    tag = "强于大盘"
                elif all(v < 0 for v in vals):
                    tag = "弱于大盘"
                elif vals[0] > 0:
                    tag = "短线强/长线弱(分歧)"
                else:
                    tag = "短线弱/长线强(分歧)"
                mf_lines.append(f"相对强度: {rl} ({tag})")
        else:
            wp = mf["weekly_phase"].split(" ")[0]
            w_bull = wp in ("底部整固", "上升趋势")
            d_bull = phase.split(" ")[0] in ("底部整固", "上升趋势")
            if w_bull and d_bull:
                res = "日线+周线同向偏多 → 强共振"
            elif (not w_bull) and (not d_bull):
                res = "日线+周线同向偏空 → 弱共振"
            else:
                res = "日线/周线背离 → 信号打折, 谨慎"
            mf_lines = [f"周线阶段: {mf['weekly_phase']}",
                        f"周线近期事件: {', '.join(mf['weekly_events']) if mf['weekly_events'] else '无'}"]
            if mf.get("monthly_phase"):
                mf_lines.append(f"月线阶段: {mf['monthly_phase']}")
                mp = mf["monthly_phase"].split(" ")[0]
                if mp in ("底部整固", "上升趋势"):
                    res += "；月线偏多助涨"
                elif mp in ("下跌趋势", "顶部构筑"):
                    res += "；月线偏空压制"
            mf_lines.append(f"共振结论: {res}")
            if rs:
                items = sorted(rs.items())
                rl = "  ".join(f"{w}日RS {v:+.1f}%" for w, v in items)
                vals = [v for _, v in items]
                if all(v > 0 for v in vals):
                    tag = "强于大盘"
                elif all(v < 0 for v in vals):
                    tag = "弱于大盘"
                elif vals[0] > 0:
                    tag = "短线强/长线弱(分歧)"
                else:
                    tag = "短线弱/长线强(分歧)"
                mf_lines.append(f"相对强度: {rl} ({tag})")
            sections.append(("多周期共振", mf_lines))

    # ── 历史胜率回测 (因果式: 无前瞻偏差, 费后) ──
    if backtest and backtest.get("by_type"):
        bt_lines = [f"因果式回测·触发后{backtest.get('horizon', 20)}日 (费后均值):"]
        for t in sorted(backtest["by_type"], key=lambda x: -backtest["by_type"][x]["win"]):
            s = backtest["by_type"][t]
            exp = s["avg"]
            bt_lines.append(f"  {t:<7s} {s['n']}次 胜率{s['win']:.0f}% 均{exp:+.1f}% "
                            f"中位{s['med']:+.1f}% 盈亏比{s['pl_ratio']:.1f}")
            if "win_confirmed" in s:
                bt_lines.append(f"    其中✓已确认 {s['n_confirmed']}次 胜率{s['win_confirmed']:.0f}% "
                                f"均{s['avg_confirmed']:+.1f}%")
        bt_lines.append(f"  同期买入持有: {backtest.get('benchmark', 0):+.1f}% (费前)")
        top = max(backtest["by_type"].items(),
                  key=lambda kv: kv[1]["avg"], default=None)
        if top:
            bt_lines.append(f"  期望最优: {top[0]} 单笔期望{top[1]['avg']:+.1f}% "
                            f"(相对大盘{top[1]['vs_bh']:+.1f}%)")
        sections.append(("历史胜率", bt_lines))

    # ── VSA 标签滚动回测 (同因果口径, 无前瞻偏差, 费后) ──
    if vsa_bt and vsa_bt.get("by_label"):
        vsa_lines = [f"VSA回测·触发后{vsa_bt.get('horizon', 20)}根 (费后均值, 看涨做多/看跌反向):"]
        for lb in sorted(vsa_bt["by_label"], key=lambda x: -vsa_bt["by_label"][x]["win"]):
            s = vsa_bt["by_label"][lb]
            d = vsa_dir(lb)
            tag = "看涨" if d > 0 else ("看跌" if d < 0 else "中性")
            vsa_lines.append(f"  {lb:<5s}{tag} {s['n']}次 胜率{s['win']:.0f}% "
                             f"均{s['avg']:+.1f}% 中位{s['med']:+.1f}% 盈亏比{s['pl_ratio']:.1f}")
        vsa_lines.append(f"  同期买入持有: {vsa_bt.get('benchmark', 0):+.1f}% (费前)")
        top = max(vsa_bt["by_label"].items(),
                  key=lambda kv: kv[1]["avg"], default=None)
        if top:
            vsa_lines.append(f"  期望最优: {top[0]} 单笔期望{top[1]['avg']:+.1f}% "
                             f"(相对大盘{top[1]['vs_bh']:+.1f}%)")
        sections.append(("VSA回测", vsa_lines))

    # ── 稳健性: 参数微小扰动下结论是否稳定 (防过拟合) ──
    if robustness:
        r_lines = robustness["lines"]
        if robustness["verdict"] == "脆弱":
            r_lines.append("  → 结构对参数敏感, 建议降低仓位或等待更清晰信号")
        elif robustness["verdict"] == "一般":
            r_lines.append("  → 结论中等稳定, 信号需量价确认")
        else:
            r_lines.append("  → 结构稳健, 信号可信度较高")
        sections.append(("稳健性", r_lines))

    # ── 数据质量提示 ──
    if "locked" in df.columns:
        locked_n = int(df["locked"].tail(30).sum())
        if locked_n:
            sections.append(("数据提示",
                             [f"近30根中 {locked_n} 根为近似涨跌停/一字板 bar (无法正常成交)",
                              "回测已剔除此类样本; 若为最新一根, 实际无法按现价成交, 请留意次日开盘"]))
    if bool(df["is_new_stock"].iloc[-1] if "is_new_stock" in df.columns else False):
        sections.append(("数据提示",
                         ["次新股: 上市/历史样本不足, 威科夫结构与枢轴判定不可靠",
                          "建议缩短观察周期或降权, 勿按长期吸筹/派发结构交易"]))

    # ── 买点提示 ──
    buy_lines = ["买点提示 (威科夫):"]
    recent_types = [e["type"] for e in recent]
    spring_present = "Spring" in recent_types
    st_present = "ST" in recent_types
    joc_present = "JOC" in recent_types
    sos_present = "SOS" in recent_types
    sc_present = "SC" in recent_types
    utad_present = "UTAD" in recent_types
    # 实证: Spring(82.6%)>>ST(75.0%)>>SC(58.3%)>SOS(45.7%)>JOC(43.5%)
    if spring_present:
        buy_lines.append("  • Spring 弹簧已现(置信高) → 放量收回即买点, 止损设于 Spring 低点下方")
    elif st_present:
        buy_lines.append("  • SC 二次测试(缩量回踩)完成 → 等待放量阳线确认后买入")
    elif sc_present and (joc_present or sos_present):
        buy_lines.append("  • SC后现突破信号 → 回踩突破位不破即买点 (止损设SC低点下方)")
    elif joc_present or sos_present:
        if st_present or sc_present:
            buy_lines.append("  • JOC/SOS突破 + 前期吸筹信号 → 回踩不破可作买点")
        else:
            buy_lines.append("  • 已现 JOC/SOS 突破但缺少前期吸筹确认 → 建议等回踩再评估")
    elif utad_present:
        buy_lines.append("  • 出现 UTAD → 警惕派发, 暂不宜追多, 等缩量回踩再评估")
    else:
        buy_lines.append("  • 结构未明确, 建议等待更清晰信号 (ST/Spring/SC+突破)")
    buy_lines.append("风险提示: 技术分析不构成投资建议; 放量跌破关键支撑即止损离场。")
    sections.append(("买点建议", buy_lines))

    # ── 卖点提示 ──
    sell_lines = ["卖点提示 (威科夫):"]
    base_ph = phase.split(" ")[0]
    bearish_ph = base_ph in ("顶部构筑", "下跌趋势")
    sell_conf = None
    if bearish_ph:
        if conf_q == "high":
            sell_conf = "confirm"
        elif conf_q == "caution":
            sell_conf = "doubt"
    # 实证: UTAD(23.4%上涨,即76.6%下跌)>>BC(49%上涨,近乎随机)
    utad_present = "UTAD" in recent_types
    bc_present = "BC" in recent_types
    psy_present = "PSY" in recent_types
    if utad_present:
        if sell_conf == "confirm":
            sell_lines.append("  • UTAD + 资金确认派发 → 减仓信号可信度高, 逢高离场")
        elif sell_conf == "doubt":
            sell_lines.append("  • 已现 UTAD 但资金/基本面矛盾 → 派发存疑, 以跌破 UTAD 低点为准, 不主动清仓")
        else:
            sell_lines.append("  • 已现 UTAD → 上冲派发, 逢高减仓/离场 (跌破 UTAD 低点确认)")
    elif bc_present:
        if sell_conf == "confirm":
            sell_lines.append("  • BC + 资金确认派发 → 高位兑现, 分批了结")
        elif bearish_ph:
            sell_lines.append("  • 已现 BC 买入高潮 → 高位注意风险, 放量长阴跌破支撑再离场")
        else:
            sell_lines.append("  • 已现 BC(买入高潮) → 警示信号, 结合趋势方向判断, 不急于卖出")
    elif psy_present:
        sell_lines.append("  • 出现 PSY → 弱势反抽, 反弹乏力即卖出")
    else:
        sell_lines.append("  • 当前无明确派发信号; 跌破近期支撑或放量长阴时再离场")
    sell_lines.append("风控: 跌破关键支撑止损; 利润回吐超 5% 可上移止盈。")
    sections.append(("卖点建议", sell_lines))
    return sections


def sections_to_text(sections):
    """把结构化结论 [(标题, [行,...])] 拼回平铺文本 (导出报告用)。"""
    blocks = []
    for title, lines in sections:
        block = [f"── {title} ──"] + lines
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)
