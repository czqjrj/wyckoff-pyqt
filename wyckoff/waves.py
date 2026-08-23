"""波浪理论分析 + 威科夫目标价计算。"""
import pandas as pd

from .config import W_PIVOT_LONG
from .wavecount import WaveCount, count_waves


def enhanced_wave_analysis(df: pd.DataFrame, pivots, phase="", events=None):
    """基于 wavecount 的增强波浪分析, 返回 (lines, wave_data)。

    wave_data: 供图表标注的结构化信息 {
        "points": [(idx, price, wave_label), ...],   # 与 chart.py 兼容的折线点
        "position": 当前浪位描述,
        "done": 当前结构是否完成,
        "fib": 斐波那契汇聚带列表,
        "next_target": 下一目标,
        "invalidation": 失效位,
    }
    """
    events = events or []
    wc: WaveCount = count_waves(pivots)
    last_close = float(df["close"].iloc[-1])
    lines = []

    if wc.kind == "none":
        lines.append(f"波浪: {wc.position}")
        wave_data = {"points": [], "position": wc.position, "done": False,
                     "fib": [], "next_target": None, "invalidation": None}
        return lines, wave_data

    # ── 基础结构行 ──
    kind_txt = {"impulse": "推动浪", "corrective": "修正浪"}.get(wc.kind, wc.kind)
    dir_txt = {"up": "上升", "down": "下跌"}.get(wc.direction, "")
    lines.append(f"波浪: {kind_txt} {dir_txt}结构 | 当前{dir_txt}浪位 {wc.position}")
    for w in wc.waves:
        lines.append(f"  {w['label']}: {w['start']:.2f} → {w['end']:.2f}")

    # ── 斐波那契汇聚 ──
    if wc.fib_confluence:
        fib_lines = "  ".join(
            f"{c['level']}={c['price']:.2f}" for c in wc.fib_confluence)
        lines.append(f"斐波那契汇聚: {fib_lines}")
        if wc.next_target is not None:
            if wc.kind == "corrective":
                lines.append(f"C浪目标位: {wc.next_target:.2f}")
            else:
                lines.append(f"下一扩展目标: {wc.next_target:.2f}")

    # ── 交叉验证: 浪位 × 威科夫阶段/事件 ──
    phase_txt = phase.split(" ")[0] if phase else ""
    cross = _wave_phase_cross(wc, phase_txt, events, last_close)
    for c in cross:
        lines.append(c)

    # ── 机会与风险 ──
    lines.append("")
    opp, risk = _wave_opp_risk(wc, phase_txt, events, last_close)
    lines.append("机会:")
    for o in opp:
        lines.append(f"  • {o}")
    lines.append("风险:")
    for r in risk:
        lines.append(f"  • {r}")

    wave_data = {
        "points": [(p.idx, p.price, p.wave) for p in wc.points],
        "position": wc.position,
        "done": wc.done,
        "kind": wc.kind,
        "direction": wc.direction,
        "fib": wc.fib_confluence,
        "next_target": wc.next_target,
        "invalidation": wc.invalidation,
    }
    return lines, wave_data


def _wave_phase_cross(wc: WaveCount, phase_txt: str, events, last_close: float):
    """波浪位置 × 威科夫阶段/事件交叉验证, 返回文本行。"""
    out = []
    ev_types = [e["type"] for e in events]
    if wc.kind == "impulse" and wc.direction == "up":
        if wc.done:
            out.append("  ⚡ 5浪上升完成 + 阶段在派发/顶部 → 顶部风险信号")
        elif any(t in ev_types for t in ("UTAD", "UT", "BC")) and phase_txt in (
                "派发", "派发阶段", "顶部"):
            out.append("  ⚡ 浪5推进 + UTAD/BC 事件 → 顶部构筑概率上升")
    elif wc.kind == "impulse" and wc.direction == "down":
        if wc.done and any(t in ev_types for t in ("Spring", "ST", "SC")):
            out.append("  ⚡ 5浪下跌完成 + Spring/SC 事件 → 筑底概率上升")
    elif wc.kind == "corrective" and wc.direction == "down":
        # ABC 回调出现在吸筹阶段常见 → 回调末段是吸筹区低吸机会
        if any(t in ev_types for t in ("SC", "Spring", "ST")):
            out.append("  ⚡ ABC 回调 + 吸筹事件 → 回调末端或为低吸机会")
    return out


def _wave_opp_risk(wc: WaveCount, phase_txt: str, events, last_close: float):
    """基于浪位生成机会/风险建议。"""
    opp, risk = [], []
    ev_types = [e["type"] for e in events]
    if wc.kind == "impulse":
        if wc.direction == "up":
            if wc.done:
                opp.append("5浪上升完成, 等ABC回调企稳后再介入, 勿追顶")
                risk.append("回调可能较深 (ABC), 追高套牢风险大")
            else:
                opp.append("推动浪上行中, 回踩前浪支撑可顺势加仓")
                risk.append("若跌破浪1起点 (失效位), 结构破坏需止损")
        else:
            if wc.done:
                opp.append("5浪下跌完成, 等待底部信号 (Spring/SC) 确认后布局")
                risk.append("延伸浪可能继续创新低, 左侧抄底仓位控制")
            else:
                opp.append("下跌推动中, 反弹至阻力位可减仓")
                risk.append("下跌结构未完成, 不宜逆势抄底")
    elif wc.kind == "corrective":
        if wc.direction == "up":
            # 向上修正 (下跌后反弹): C末端是反弹高点, 宜减仓不宜追
            opp.append("ABC 反弹中, C 浪末端 (反弹高点) 是减仓/高抛区")
            risk.append("反弹可能只是空头回补, 若破 A 浪起点修正失败, 需离场")
        else:
            # 向下修正 (上涨后回调): C末端是回调低点, 企稳后是低吸区
            opp.append("ABC 回调中, 等 C 浪末端企稳是低吸机会")
            risk.append("若跌破 A 浪起点, 修正演化为新下跌, 需离场")
    if wc.invalidation is not None:
        risk.append(f"结构失效位: {wc.invalidation:.2f}")
    return opp, risk


def _fibo_price(start, end, ratio):
    """start→end 波段的 ratio 比例价。"""
    return start + (end - start) * ratio



def elliott_wave(df: pd.DataFrame, pivots):
    """基于枢轴点检测最近一波5浪/3浪结构, 返回分析文本。
    包含: 波浪形态 / 回撤位 / 扩展目标 / 机会与风险 / 操作建议"""
    lines = []
    last_close = float(df["close"].iloc[-1])

    # 取最近若干枢轴
    seq = []
    all_p = sorted(pivots, key=lambda p: p["idx"])[-12:]
    for p in all_p:
        seq.append((p["type"], p["price"], p["idx"]))

    # 识别最近一段趋势 (高点/低点序列)
    def trend_segment(seq):
        """返回 (方向, 起点, 终点, 起点idx, 终点idx)"""
        for k in range(len(seq) - 1, 0, -1):
            a, b = seq[k - 1], seq[k]
            if a[0] == "low" and b[0] == "high":
                return "up", a[1], b[1], a[2], b[2]
            if a[0] == "high" and b[0] == "low":
                return "down", a[1], b[1], a[2], b[2]
        return None

    seg = trend_segment(seq)
    if not seg:
        lines.append("波浪: 枢轴结构不足以判断")
        return lines
    direction, start, end, s_idx, e_idx = seg
    swing = end - start
    if abs(swing) < 1e-6:
        lines.append("波浪: 波段过小无法计算")
        return lines

    fibo = {
        0.236: start + swing * 0.236,
        0.382: start + swing * 0.382,
        0.5: start + swing * 0.5,
        0.618: start + swing * 0.618,
        0.786: start + swing * 0.786,
        1.0: start + swing * 1.0,
        1.272: start + swing * 1.272,
        1.618: start + swing * 1.618,
    }

    if direction == "up":
        lines.append(f"上升波段: {start:.2f} → {end:.2f}  (+{swing:.2f}, {swing/start*100:.1f}%)")
        retrace = {k: v for k, v in fibo.items() if k <= 0.786}
        lines.append(f"支撑位: 0.382={retrace[0.382]:.2f}  |  0.5={retrace[0.5]:.2f}  |  0.618={retrace[0.618]:.2f}")
        lines.append(f"扩展目标: 1.272={fibo[1.272]:.2f}  |  1.618={fibo[1.618]:.2f}")
        # 当前价位置判断
        near_level = None
        for k in (0.382, 0.5, 0.618, 0.786):
            if abs(last_close - fibo[k]) / last_close < 0.03:
                near_level = (k, fibo[k])
                break
        if near_level:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 靠近 {near_level[0]:.3f} 支撑({near_level[1]:.2f})")
        elif last_close > end:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 已突破前高 {end:.2f}, 强势延续")
        else:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 距支撑较远, 趋势运行中")
    else:
        lines.append(f"下跌波段: {start:.2f} → {end:.2f}  ({swing:.2f}, {swing/start*100:.1f}%)")
        retrace = {k: v for k, v in fibo.items() if k <= 0.786}
        lines.append(f"阻力位: 0.382={retrace[0.382]:.2f}  |  0.5={retrace[0.5]:.2f}  |  0.618={retrace[0.618]:.2f}")
        lines.append(f"扩展目标: 1.272={fibo[1.272]:.2f}  |  1.618={fibo[1.618]:.2f}")
        near_level = None
        for k in (0.382, 0.5, 0.618, 0.786):
            if abs(last_close - fibo[k]) / last_close < 0.03:
                near_level = (k, fibo[k])
                break
        if near_level:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 靠近 {near_level[0]:.3f} 阻力({near_level[1]:.2f})")
        elif last_close < end:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 已跌破前低 {end:.2f}, 弱势延续")
        else:
            lines.append(f"  ▶ 当前价 {last_close:.2f} 距阻力较远, 趋势运行中")

    # ── 5浪计数 ──
    has_5wave = None
    if len(all_p) >= 5:
        last5 = all_p[-5:]
        types5 = [p["type"] for p in last5]
        prices5 = [p["price"] for p in last5]
        if types5 == ["low", "high", "low", "high", "low"]:
            lows, highs = prices5[::2], prices5[1::2]
            if all(lows[k] > lows[k + 1] for k in range(2)) \
                    and all(highs[k] > highs[k + 1] for k in range(1)):
                has_5wave = "down"
                lines.append("  ◆ 5浪下跌结构已现 (1-2-3-4-5) → 关注筑底")
        elif types5 == ["high", "low", "high", "low", "high"]:
            lows, highs = prices5[1::2], prices5[::2]
            if all(lows[k] < lows[k + 1] for k in range(1)) \
                    and all(highs[k] < highs[k + 1] for k in range(2)):
                has_5wave = "up"
                lines.append("  ◆ 5浪上升结构已现 (1-2-3-4-5) → 关注顶部风险")

    # ═══ 机会与风险 ═══
    lines.append("")
    lines.append("机会:")
    opportunities = []
    if direction == "up":
        if has_5wave == "up":
            opportunities.append("5浪上升已完成, 关注ABC回调后更安全的入场点")
        elif near_level and near_level[0] >= 0.5:
            opportunities.append(f"回踩{near_level[0]:.3f}支撑({near_level[1]:.2f})若企稳, 是加仓/入场机会")
        elif last_close > end:
            opportunities.append("突破前高, 回踩确认不破可追多, 目标看扩展位")
        else:
            opportunities.append("上升趋势中, 回调至0.382-0.5支撑位是理想入场区")
    else:
        if has_5wave == "down":
            opportunities.append("5浪下跌结构完整, 筑底信号出现后是左侧布局良机")
        elif near_level and near_level[0] >= 0.5:
            opportunities.append(f"反弹至{near_level[0]:.3f}阻力({near_level[1]:.2f})若受阻, 是逢高减仓/离场机会")
        else:
            opportunities.append("反弹至0.382-0.5阻力区可考虑减仓, 等待底部结构确认")
    for o in opportunities:
        lines.append(f"  • {o}")

    lines.append("")
    lines.append("风险:")
    risks = []
    if direction == "up":
        risks.append("若跌破0.618回撤位, 上升趋势可能逆转, 应止损离场")
        risks.append("高位追多风险: 距支撑过远, 回调幅度可能超预期")
        if has_5wave == "up":
            risks.append("5浪完成后回调幅度通常较深(A-B-C), 不宜追高")
    else:
        risks.append("下跌趋势中反弹可能只是空头回补, 不可逆势重仓")
        risks.append("抄底风险: 底部未确认前入场, 可能被继续下跌套牢")
        if has_5wave == "down":
            risks.append("5浪下跌虽完整, 但延伸浪可能继续创新低")
    for r in risks:
        lines.append(f"  • {r}")

    # ═══ 操作参考 ═══
    lines.append("")
    lines.append("操作参考:")
    if direction == "up":
        # 自洽校验: 上升结构止损(0.618回撤)必须仍在现价下方, 目标必须在现价上方,
        # 否则结构已失效, 不再输出"止损高于现价"的自相矛盾计划。
        if last_close > retrace[0.618]:
            lines.append(f"  止损: 跌破 {retrace[0.618]:.2f} (0.618回撤) 严格止损")
        else:
            lines.append(f"  止损: 现价{last_close:.2f}已跌破0.618回撤位, 上升结构失效, 不宜追多")
        if last_close < fibo[1.618]:
            lines.append(f"  目标: 前高 {end:.2f} 突破后看 {fibo[1.272]:.2f} → {fibo[1.618]:.2f}")
        else:
            lines.append("  目标: 现价已超1.618扩展位, 上方无结构性目标")
    else:
        # A股只能做多、不能做空: 下跌结构只给出 减仓/离场 指引与 上方确认位/下方
        # 回踩支撑参考, 不输出"空头止损/目标"式的可执行做空交易规格。
        if last_close < retrace[0.618]:
            lines.append(f"  上方确认位: 突破 {retrace[0.618]:.2f} (0.618反弹) 则下跌预期落空")
        else:
            lines.append(f"  上方确认位: 现价{last_close:.2f}已突破0.618反弹位, 下跌结构失效")
        if last_close > fibo[1.618]:
            lines.append(f"  下方回踩支撑参考: 前低 {end:.2f} 跌破后看 {fibo[1.272]:.2f} → {fibo[1.618]:.2f}")
        else:
            lines.append("  下方回踩支撑参考: 现价已跌破1.618扩展位, 下方无结构性支撑")
        lines.append("  提示: A股不能做空; 下跌结构仅供持仓者减仓/离场参考, 不构成做空指令")

    return lines


def extract_wave_points(pivots):
    """从枢轴序列提取最近一段完整波浪 (交替 low/high) 的点序列。
    返回 [(idx, price), ...]，供 K 线图连线标注 (与 elliott_wave 同源)。
    以最近一段交替枢轴对 (趋势段) 的终点为锚, 向前后扩展完整交替序列。"""
    all_p = sorted(pivots, key=lambda p: p["idx"])[-12:]
    seq = [(p["type"], p["price"], p["idx"]) for p in all_p]
    if len(seq) < 2:
        return []
    end = None
    for k in range(len(seq) - 1, 0, -1):
        if seq[k - 1][0] != seq[k][0]:
            end = k
            break
    if end is None:
        return []
    pts = [seq[end]]
    i = end - 1
    while i >= 0 and seq[i][0] != pts[-1][0]:
        pts.append(seq[i])
        i -= 1
    pts.reverse()
    j = end + 1
    while j < len(seq) and seq[j][0] != pts[-1][0]:
        pts.append(seq[j])
        j += 1
    return [(idx, price) for (_t, price, idx) in pts]


def calc_targets(df, pivots, events):
    """威科夫目标价: 基于吸筹/派发区间的点数计算 + 关键事件价位"""
    targets = {}
    recent = [e for e in events if e["idx"] >= len(df) - W_PIVOT_LONG]

    scs = [e for e in recent if e["type"] == "SC"]
    springs = [e for e in recent if e["type"] == "Spring"]
    bcs = [e for e in recent if e["type"] == "BC"]
    last_close = float(df["close"].iloc[-1])

    # 吸筹区间 (最近SC后的高点/低点)
    if scs:
        sc = scs[-1]
        after = [p for p in pivots if p["idx"] > sc["idx"]]
        hi_after = max((p["price"] for p in after if p["type"] == "high"), default=None)
        lo_after = min((p["price"] for p in after if p["type"] == "low"), default=None)
        lo = sc["price"]
        # 失败SC: SC后价格创新低超3% → SC非有效吸筹起点 (吸筹区间未成立,
        # 高点/低点来自破位中继, 计点数目标会严重失真), 不再输出区间目标。
        failed_sc = bool(lo_after and lo_after < lo * 0.97)
        if not failed_sc and hi_after and hi_after > lo:
            mid = (lo + hi_after) / 2
            height = hi_after - lo
            targets["区间中轴"] = round(mid, 2)
            up_tgt = hi_after + height
            dn_tgt = lo - height
            # 点数目标必须相对现价方向合理才输出: 上涨目标>现价, 下跌目标<现价
            if up_tgt > last_close:
                targets["点数上涨目标(上方)"] = round(up_tgt, 2)
            if dn_tgt < last_close:
                targets["点数下跌目标(下方)"] = round(dn_tgt, 2)
            # 近端目标: 先到达的保守目标 (校准: 距离主要来自 目标-现价 而非系数,
            # 故与 pnf.py 同步改为 结构目标与现价+/-4% 封顶取近者, 保证可到达)
            NEAR_CAP = 0.04
            near_up = min(hi_after + height * 0.2, last_close * (1 + NEAR_CAP))
            near_dn = max(lo - height * 0.2, last_close * (1 - NEAR_CAP))
            if near_up > last_close:
                targets["近端上涨目标(上方)"] = round(near_up, 2)
            if near_dn < last_close:
                targets["近端下跌目标(下方)"] = round(near_dn, 2)

    # Spring 低点止损位 (仅当Spring仍在现价下方有效, 失效/破位则不输出)
    if springs:
        sp = springs[-1]
        sp_stop = sp["price"] * 0.98
        if sp_stop < last_close:
            targets["Spring止损位"] = round(sp_stop, 2)

    # 派发区间 (最近BC后的高点/低点)
    if bcs and not scs:
        bc = bcs[-1]
        hi = bc["price"]
        after = [p for p in pivots if p["idx"] > bc["idx"]]
        lo_after = min((p["price"] for p in after if p["type"] == "low"), default=None)
        hi_after = max((p["price"] for p in after if p["type"] == "high"), default=None)
        # 失败BC: BC后价格创新高超3% → 派发区间未成立, 不计点数目标
        failed_bc = bool(hi_after and hi_after > hi * 1.03)
        if lo_after and not failed_bc:
            mid = (hi + lo_after) / 2
            height = hi - lo_after
            targets["区间中轴"] = round(mid, 2)
            dn_tgt = lo_after - height
            up_tgt = hi + height
            if dn_tgt < last_close:
                targets["点数下跌目标(下方)"] = round(dn_tgt, 2)
            if up_tgt > last_close:
                targets["点数上涨目标(上方)"] = round(up_tgt, 2)
            # 近端目标: 先到达的保守目标 (与吸筹侧同步: 现价+/-4% 封顶)
            NEAR_CAP = 0.04
            near_dn = max(lo_after - height * 0.2, last_close * (1 - NEAR_CAP))
            near_up = min(hi + height * 0.2, last_close * (1 + NEAR_CAP))
            if near_dn < last_close:
                targets["近端下跌目标(下方)"] = round(near_dn, 2)
            if near_up > last_close:
                targets["近端上涨目标(上方)"] = round(near_up, 2)

    return targets
