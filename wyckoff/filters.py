"""补充分析方法过滤层 (独立于威科夫结构, 作为交叉验证)。

集成五种独立方法, 全部 fail-soft (数据不足/异常 → None, 不阻塞分析):
  chip   筹码分布/成本分析: 三角分布法把成交量摊到价格区间, 得平均成本/
         获利盘/套牢盘/90%成本带/集中度/筹码峰, 判断支撑与套牢压力。
  ma     均线系统 (葛兰碧八大法则): 排列/金叉死叉/乖离/买卖信号。
  vol    布林带+ATR 波动率过滤: 带宽压缩=蓄势 (突破更可靠), 高波动=追高风险。
  fund   财报基本面/估值排雷: PE/PB/净利增速/亏损 → 价值陷阱识别。
  flow   资金流过滤增强: 主力净流入占成交额比例 (可比阈值) + 与阶段共振/背离。

各函数返回 dict 或 None; build_filter_sections 把结果拼为报告小节;
filter_summary_cards 生成顶部信号汇总卡片。纯规则, 无网络依赖 (flow/fund
由调用方抓取后传入)。
"""

import numpy as np

BULL_PHASES = ("底部整固", "上升趋势")
BEAR_PHASES = ("顶部构筑", "下跌趋势")


# ───────────────────────── 筹码分布 / 成本分析 ─────────────────────────

def chip_analysis(df, lookback: int = 120):
    """筹码分布 (三角分布近似), 返回:
      avg_cost      加权平均成本 (筹码重心)
      profit_ratio  获利盘比例 (成本低于现价的筹码占比)
      trapped_ratio 套牢盘比例
      cost_low/cost_high  90% 成本带上下沿
      concentration 成本集中度 = (cost_high-cost_low)/avg_cost, 越小越集中
      peak_price    筹码峰 (密度最大价格)
      peak_ratio    筹码峰占比
      state         '集中吸筹'/'高位获利'/'深度套牢'/'均衡'
      verdict       简评文本
    """
    d = df.tail(lookback)
    if len(d) < 30:
        return None
    try:
        lo = float(d["low"].min())
        hi = float(d["high"].max())
        if not (hi > lo):
            return None
        last = float(d["close"].iloc[-1])
        nbin = 200
        edges = np.linspace(lo, hi, nbin + 1)
        centers = (edges[:-1] + edges[1:]) / 2.0
        density = np.zeros(nbin)
        for _, r in d.iterrows():
            l, h = float(r["low"]), float(r["high"])
            v = float(r["volume"])
            if h <= l or v <= 0 or h < lo or l > hi:
                continue
            typ = (h + l + float(r["close"])) / 3.0
            mask = (centers >= l) & (centers <= h)
            if not mask.any():
                continue
            w = np.where(mask, np.where(
                centers <= typ,
                (centers - l) / max(typ - l, 1e-9),
                (h - centers) / max(h - typ, 1e-9)), 0.0)
            w = np.clip(w, 0.0, 1.0)
            s = w.sum()
            if s <= 0:
                continue
            density += v * w / s
        total = density.sum()
        if total <= 0:
            return None
        avg_cost = float((centers * density).sum() / total)
        cum = np.cumsum(density) / total
        c5 = float(centers[np.searchsorted(cum, 0.05)])
        c95 = float(centers[np.searchsorted(cum, 0.95)])
        profit = float(density[centers <= last].sum() / total)
        pk = int(np.argmax(density))
        concentration = (c95 - c5) / max(avg_cost, 1e-9)
        if profit > 0.75:
            state = "高位获利"
        elif profit < 0.25:
            state = "深度套牢"
        elif concentration < 0.25:
            state = "集中吸筹"
        else:
            state = "均衡"
        return {
            "avg_cost": avg_cost, "profit_ratio": profit,
            "trapped_ratio": 1.0 - profit,
            "cost_low": c5, "cost_high": c95,
            "concentration": concentration,
            "peak_price": float(centers[pk]),
            "peak_ratio": float(density[pk] / total),
            "state": state, "last": last,
        }
    except Exception:
        return None


def chip_lines(chip):
    """筹码分布 dict → 报告文本行。"""
    if not chip:
        return ["  (数据不足)"]
    pr = chip["profit_ratio"] * 100
    tr = chip["trapped_ratio"] * 100
    lines = [
        f"  平均成本: {chip['avg_cost']:.2f}  (现价 {chip['last']:.2f}, "
        f"{'现价在上方(浮盈)' if chip['last'] >= chip['avg_cost'] else '现价在下方(浮亏)'})",
        f"  获利盘 {pr:.1f}% · 套牢盘 {tr:.1f}%",
        f"  90%成本带: {chip['cost_low']:.2f} ~ {chip['cost_high']:.2f} · "
        f"集中度 {chip['concentration']:.2f} ({'集中' if chip['concentration'] < 0.25 else '分散'})",
        f"  筹码峰: {chip['peak_price']:.2f} (占 {chip['peak_ratio'] * 100:.1f}%)",
    ]
    if chip["state"] == "集中吸筹":
        lines.append("  筹码向平均成本集中且获利盘适中 → 支撑稳固, 与吸筹阶段相互印证")
    elif chip["state"] == "高位获利":
        lines.append("  获利盘偏高且现价贴近高位 → 上档抛压/回吐风险")
    elif chip["state"] == "深度套牢":
        lines.append("  深度套牢 → 上方套牢压力, 反弹易遇解套抛压")
    else:
        lines.append("  筹码分布均衡, 无明显单边支撑/套牢结构")
    return lines


# ───────────────────────── 均线系统 (葛兰碧八大法则) ─────────────────────────

def granville_signal(df):
    """均线系统: 排列 / 金叉死叉 / 乖离 / 葛兰碧信号。返回 dict 或 None。"""
    if not {"price_ma5", "price_ma20", "price_ma50"}.issubset(df.columns):
        return None
    try:
        c = df["close"].values
        m5 = df["price_ma5"].values
        m20 = df["price_ma20"].values
        m50 = df["price_ma50"].values
        i = -1
        last = float(c[i])
        if not np.isfinite(m50[i]):
            return None
        ma20 = float(m20[i]); ma5 = float(m5[i]); ma50 = float(m50[i])
        if last > ma5 > ma20 > ma50:
            arrangement, arr_tone = "多头排列", "bullish"
        elif last < ma5 < ma20 < ma50:
            arrangement, arr_tone = "空头排列", "bearish"
        else:
            arrangement, arr_tone = "均线纠缠", "neutral"
        cross = None
        for j in range(max(1, -10), 0):
            if m20[j - 1] <= m20[j] and m20[j] > m20[j - 1] and \
               c[j - 1] <= m20[j - 1] and c[j] > m20[j]:
                cross = ("MA20金叉", j)
                break
            if m20[j - 1] >= m20[j] and m20[j] < m20[j - 1] and \
               c[j - 1] >= m20[j - 1] and c[j] < m20[j]:
                cross = ("MA20死叉", j)
                break
        # MA20 走平/上行/下行 (近5根斜率)
        slope = m20[i] - m20[max(i - 5, 0)]
        slope_txt = "上行" if slope > 0.001 else "下行" if slope < -0.001 else "走平"
        bias20 = (last - ma20) / max(ma20, 1e-9) * 100
        # 葛兰碧法则 (基于MA20简化)
        signal, sig_tone = None, "neutral"
        crossed_up = c[i] > m20[i] and c[i - 1] <= m20[i - 1]
        crossed_dn = c[i] < m20[i] and c[i - 1] >= m20[i - 1]
        if crossed_up and slope >= -0.001:
            signal, sig_tone = "买1: 价上穿走平/上行的MA20", "bullish"
        elif crossed_dn and slope <= 0.001:
            signal, sig_tone = "卖1: 价下穿走平/下行的MA20", "bearish"
        elif last > ma20 and float(np.min(c[max(i - 3, 0):])) <= ma20 * 1.005:
            signal, sig_tone = "买2: 回踩MA20获支撑回升", "bullish"
        elif last < ma20 and float(np.max(c[max(i - 3, 0):])) >= ma20 * 0.995:
            signal, sig_tone = "卖2: 反抽MA20受阻回落", "bearish"
        elif bias20 > 8:
            signal, sig_tone = "卖3: 乖离过大(+" + f"{bias20:.0f}" + "%), 回归/回撤压力", "bearish"
        elif bias20 < -8:
            signal, sig_tone = "买3: 超卖乖离(" + f"{bias20:.0f}" + "%), 反抽动能积聚", "bullish"
        else:
            signal, sig_tone = "无明确葛兰碧信号", "neutral"
        return {
            "arrangement": arrangement, "arr_tone": arr_tone,
            "cross": cross, "slope": slope_txt, "bias20": bias20,
            "signal": signal, "tone": sig_tone, "last": last,
            "ma20": ma20, "ma50": ma50, "ma5": ma5,
        }
    except Exception:
        return None


def ma_lines(ma):
    if not ma:
        return ["  (数据不足)"]
    cross_txt = ""
    if ma.get("cross"):
        name, off = ma["cross"]
        cross_txt = f" · 近{abs(off)}根内{name}"
    lines = [
        f"  均线排列: {ma['arrangement']}  (MA5 {ma['ma5']:.2f} / MA20 {ma['ma20']:.2f} "
        f"/ MA50 {ma['ma50']:.2f})",
        f"  乖离率: MA20乖离 {ma['bias20']:+.1f}% · MA20 {ma['slope']}{cross_txt}",
        f"  葛兰碧信号: {ma['signal']}",
    ]
    return lines


# ───────────────────────── 布林带 / ATR 波动率过滤 ─────────────────────────

def volatility_signal(df):
    """波动率状态: 布林带宽百分位 + ATR%。低波动=蓄势 (突破更可靠)。"""
    if not {"atr", "boll_up", "boll_dn", "boll_mid"}.issubset(df.columns):
        return None
    try:
        d = df.tail(120)
        last = float(df["close"].iloc[-1])
        up = float(df["boll_up"].iloc[-1])
        dn = float(df["boll_dn"].iloc[-1])
        mid = float(df["boll_mid"].iloc[-1])
        atr = float(df["atr"].iloc[-1]) if np.isfinite(df["atr"].iloc[-1]) else None
        if not (up > dn and mid > 0):
            return None
        bw = (up - dn) / mid * 100
        bws = (d["boll_up"] - d["boll_dn"]) / d["boll_mid"].replace(0, np.nan) * 100
        bws = bws.dropna()
        pct = float((bws < bw).mean()) * 100 if len(bws) else 50.0
        pos = (last - dn) / (up - dn) if up > dn else 0.5
        if pct < 30:
            state, tone = "低波动蓄势", "bullish"
            note = "带宽压缩 → 突破可靠性提高, 若放量突破上轨可信度高"
        elif pct > 70:
            state, tone = "高波动", "bearish"
            note = "带宽扩张 → 追高风险, 等待回踩确认"
        else:
            state, tone = "正常波动", "neutral"
            note = "波动中性, 按既有结构操作"
        atr_txt = f"ATR {atr / last * 100:.1f}%" if atr and last else "ATR 无"
        return {
            "bw": bw, "bw_pct": pct, "atr_pct": (atr / last * 100) if atr and last else None,
            "position": pos, "state": state, "tone": tone, "note": note,
            "atr_txt": atr_txt, "boll_up": up, "boll_dn": dn,
        }
    except Exception:
        return None


def vol_lines(vol):
    if not vol:
        return ["  (数据不足)"]
    pos_txt = ("贴下轨" if vol["position"] < 0.2 else "贴上轨" if vol["position"] > 0.8
               else "中轨附近")
    lines = [
        f"  布林带宽: {vol['bw']:.1f}% (近120日分位 {vol['bw_pct']:.0f}%) · "
        f"{vol['atr_txt']}",
        f"  现价 {pos_txt}: 布林上轨 {vol['boll_up']:.2f} / 下轨 {vol['boll_dn']:.2f}",
        f"  波动状态: {vol['state']} → {vol['note']}",
    ]
    return lines


# ───────────────────────── 财报基本面 / 估值排雷 ─────────────────────────

def fundamental_filter(fund):
    """基本面/估值排雷层。返回 {items, verdict, tone, hard_fail} 或 None。"""
    if not fund:
        return None
    items = []
    neg = 0
    pos = 0
    pe = fund.get("pe_ttm")
    pb = fund.get("pb")
    g = fund.get("net_growth")
    if pe and pe > 0:
        if pe < 25:
            items.append(("估值: PE(TTM) " + f"{pe:.1f} · 相对合理/偏低", "bullish"))
            pos += 1
        elif pe < 45:
            items.append(("估值: PE(TTM) " + f"{pe:.1f} · 中性", "neutral"))
        else:
            items.append(("估值: PE(TTM) " + f"{pe:.1f} · 偏高", "bearish"))
            neg += 1
    elif pe is not None and pe <= 0:
        items.append(("亏损: PE<0, 盈利为负", "bearish"))
        neg += 2
    if pb and pb > 0:
        if pb < 1.5:
            items.append(("市净率: PB " + f"{pb:.2f} · 低/破净", "bullish"))
            pos += 1
        elif pb > 4:
            items.append(("市净率: PB " + f"{pb:.2f} · 偏高", "bearish"))
            neg += 1
    if g is not None:
        gp = g * 100
        if gp < 0:
            items.append(("成长: 净利同比 " + f"{gp:+.0f}% · 负增长", "bearish"))
            neg += 1
        else:
            items.append(("成长: 净利同比 " + f"{gp:+.0f}%", "bullish"))
            pos += 1
    # 价值陷阱: 高估值 + 负增长 / 亏损
    hard_fail = False
    if g is not None and g < 0 and (pe is None or pe > 0 and pe > 30 or pe <= 0):
        items.append(("排雷: 高估值/亏损 且 净利负增长 → 价值陷阱嫌疑", "bearish"))
        neg += 2
        hard_fail = True
    if not items:
        return None
    if hard_fail:
        verdict, tone = "排雷: 基本面警示", "bearish"
    elif neg == 0 and pos >= 1:
        verdict, tone = "基本面健康", "bullish"
    elif neg > pos:
        verdict, tone = "估值/基本面偏弱", "bearish"
    else:
        verdict, tone = "基本面中性", "neutral"
    return {"items": items, "verdict": verdict, "tone": tone, "hard_fail": hard_fail}


def fund_lines(ff):
    if not ff:
        return None
    return [f"  {t}" for t, _t in ff["items"]]


# ───────────────────────── 资金流过滤增强 ─────────────────────────

def flow_gate(phase, df, flow):
    """资金流门槛: 主力净流入占近20日成交额比例 (可比阈值) + 阶段共振。
    返回 {main20, pct, trend, verdict, tone, lines} 或 None。"""
    if flow is None or not len(flow):
        return None
    try:
        base = phase.split(" ")[0]
        bull = base in BULL_PHASES
        bear = base in BEAR_PHASES
        f20 = flow.tail(20)
        main20 = float(f20["main"].sum())
        df20 = df.tail(20)
        amt = float((df20["volume"] * 100 * df20["close"]).sum())
        pct = main20 / amt * 100 if amt > 0 else 0.0
        main5 = float(f20.tail(5)["main"].sum())
        trend = ("加速流入" if main5 > 0 and main20 > 0 else
                 "流出趋缓" if main5 > 0 and main20 < 0 else
                 "加速流出" if main5 < 0 and main20 < 0 else
                 "流入减缓" if main5 < 0 and main20 > 0 else "平稳")
        # 阈值分级 (可比: 占成交额比例)
        if pct > 0.5:
            level, ltone = "强净流入", "bullish"
        elif pct > 0:
            level, ltone = "弱净流入", "neutral"
        elif pct > -0.5:
            level, ltone = "弱净流出", "neutral"
        else:
            level, ltone = "强净流出", "bearish"
        # 阶段共振
        if bull and ltone == "bullish":
            verdict, vtone = "资金与吸筹阶段共振确认", "bullish"
        elif bull and ltone == "bearish":
            verdict, vtone = "吸筹阶段但资金强流出 → 背离, 谨慎", "bearish"
        elif bear and ltone == "bearish":
            verdict, vtone = "资金与派发阶段共振, 派发可信", "bearish"
        elif bear and ltone == "bullish":
            verdict, vtone = "派发阶段但资金回流 → 背离, 或为护盘/反抽", "neutral"
        else:
            verdict, vtone = "资金流向中性", "neutral"
        return {
            "main20": main20, "pct": pct, "trend": trend,
            "level": level, "level_tone": ltone,
            "verdict": verdict, "tone": vtone,
        }
    except Exception:
        return None


def flow_lines(fg):
    if not fg:
        return None
    return [
        f"  近20日主力净流入 {fg['main20'] / 1e8:+.2f}亿 · 占成交额 {fg['pct']:+.2f}% "
        f"({fg['level']})",
        f"  资金趋势: {fg['trend']}",
        f"  共振判断: {fg['verdict']}",
    ]


# ───────────────────────── 小节 / 汇总卡片 ─────────────────────────

def build_filter_sections(filters):
    """把五种过滤结果拼为报告小节列表 [(title, [lines]), ...] (None 项跳过)。"""
    if not filters:
        return []
    out = []
    chip = filters.get("chip")
    if chip:
        out.append(("筹码分布", chip_lines(chip)))
    ma = filters.get("ma")
    vol = filters.get("vol")
    if ma or vol:
        lines = []
        if ma:
            lines += ma_lines(ma)
        if vol:
            if lines:
                lines.append("")
            lines += vol_lines(vol)
        out.append(("趋势过滤 (均线/波动)", lines))
    fund_f = filters.get("fund")
    flow_f = filters.get("flow")
    if fund_f or flow_f:
        lines = []
        if fund_f:
            lines.append("基本面排雷:")
            lines += ["  " + t for t, _t in fund_f["items"]]
        if flow_f:
            if lines:
                lines.append("")
            lines.append("资金流门槛:")
            lines += ["  " + ln.lstrip() for ln in flow_lines(flow_f)]
        out.append(("资金/基本面过滤", lines))
    summary = filter_overview(filters)
    if summary:
        out.append(("过滤总览", summary))
    return out


def filter_overview(filters):
    """过滤总览: 各层方向 + 综合结论。返回文本行列表。"""
    layers = [
        ("筹码分布", (filters.get("chip") or {}).get("state"),
         (filters.get("chip") or {}).get("state")),
        ("均线系统", (filters.get("ma") or {}).get("arrangement"),
         (filters.get("ma") or {}).get("arr_tone")),
        ("波动率过滤", (filters.get("vol") or {}).get("state"),
         (filters.get("vol") or {}).get("tone")),
        ("资金流门槛", (filters.get("flow") or {}).get("level"),
         (filters.get("flow") or {}).get("level_tone")),
        ("基本面排雷", (filters.get("fund") or {}).get("verdict"),
         (filters.get("fund") or {}).get("tone")),
    ]
    present = [(name, val, tone) for name, val, tone in layers if val]
    if not present:
        return None
    marks = {"bullish": "✓", "bearish": "✗", "neutral": "○"}
    lines = ["各过滤层结论 (与威科夫结构交叉验证, 不构成独立买卖指令):"]
    pos = sum(1 for _, _, t in present if t == "bullish")
    neg = sum(1 for _, _, t in present if t == "bearish")
    for name, val, tone in present:
        lines.append(f"  {marks.get(tone, '○')} {name}: {val}")
    hard = (filters.get("fund") or {}).get("hard_fail")
    if hard:
        lines.append("  ⚠ 基本面排雷红灯 → 即使技术结构吸筹, 也建议回避或降权")
    elif neg == 0:
        lines.append(f"  → {pos}/{len(present)} 层偏多, 无反向过滤 → "
                     f"{'、'.join(n for n, _, _ in present)} 各层交叉验证无矛盾")
    elif pos > neg:
        lines.append(f"  → {pos}/{len(present)} 层偏多, {neg} 层反向 → 信号打折, 降低仓位")
    else:
        lines.append(f"  → {neg}/{len(present)} 层偏空 → 当前信号以看空过滤为主, 谨慎")
    return lines


def filter_summary_cards(filters):
    """过滤层 → 顶部信号汇总卡片 [{label, value, tone}]。"""
    if not filters:
        return []
    out = []
    chip = filters.get("chip")
    if chip:
        tone = {"集中吸筹": "bullish", "高位获利": "bearish",
                "深度套牢": "bearish"}.get(chip["state"], "neutral")
        out.append({"label": "筹码", "value": f"获利{chip['profit_ratio'] * 100:.0f}% "
                    f"集中{chip['concentration']:.2f}", "tone": tone})
    # 均线卡已在 build_signal_summary 原生输出 (多头/空头排列), 此处不重复
    vol = filters.get("vol")
    if vol:
        out.append({"label": "波动", "value": vol["state"],
                    "tone": vol["tone"]})
    flow_f = filters.get("flow")
    if flow_f:
        out.append({"label": "资金门", "value": f"{flow_f['level']} "
                    f"{flow_f['pct']:+.2f}%", "tone": flow_f["tone"]})
    fund_f = filters.get("fund")
    if fund_f:
        out.append({"label": "排雷", "value": fund_f["verdict"], "tone": fund_f["tone"]})
    return out
