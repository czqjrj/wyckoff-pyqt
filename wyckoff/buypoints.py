"""威科夫完整做多买点 (由低风险左侧到右侧加仓)。

把 events.py 已检测的威科夫事件 (SC/ST/Spring/Shakeout/SOS/JOC/LPS/BU)
与新增的结构识别组合为一套可执行、可回测的完整多买点体系:

  一、底部吸筹区间买点 (筑底阶段)  ← 可执行主力
    - st_bottom        SC 后的二次测试 (阶段 A)      → 低风险左侧试探
    - spring           弹簧/震仓 (阶段 C)            → 高盈亏比左侧
    - spring_retest    弹簧之后的二次测试             → 高盈亏比左侧
    - lps              末期最后支撑点 (阶段 C/D 边界) → 低风险左侧试探
  二、突破确认买点 (阶段 D)  ← 仅作加仓确认
    - bu_backup        突破后缩量回踩 (BU/LPS)       → 稳健主升启动 (加仓)
  三类实证负期望买点 (sos_break/markup_break/markup_bu) 已从可执行集合剔除:
  30 只回测 PF=0.52~0.86, 仅保留识别能力, 不再进入扫描/回测/最新买点。

买卖纪律 (对应教学文档原则):
  - 不在派发、下跌阶段买入 (context 为 distribution/markdown 直接丢弃);
  - 每个买点都要求量价验证, 并强制给出止损位;
  - 左侧买点小仓位试探 (紧止损), 右侧确认买点可加仓;
  - 优先级: 高盈亏比左侧 (Spring+二次测试) > 稳健主升启动 (SOS+LPS回踩) >
    趋势中继加仓 (整理后突破回踩)。

全部识别均为因果 (只利用 bar_idx 之前的数据), 可直接用于事件级回测与实盘扫描。
"""
from __future__ import annotations

import numpy as np

from .events import detect_all
from .indicators import add_indicators, find_pivots
from .phases import judge_phase

# ──────────────────────────── 元数据 ────────────────────────────

# kind → (中文名, 归类, 默认盈亏比, 建议仓位档位 0-3)
KIND_META = {
    "st_bottom": ("SC后二次测试(ST)", "left_probe", 2.0, 1),
    "lps": ("末期回踩(LPS)", "left_probe", 2.0, 1),
    "spring": ("弹簧(Spring/震仓)", "left_high_rr", 3.0, 2),
    "spring_retest": ("弹簧二次测试", "left_high_rr", 3.0, 2),
    "sos_break": ("放量突破(SOS/JOC)", "right_main", 2.0, 3),
    "bu_backup": ("突破回踩(BU/LPS)", "right_main", 2.0, 3),
    "markup_break": ("中继放量突破", "right_continuation", 2.0, 2),
    "markup_bu": ("中继回踩加仓", "right_continuation", 2.0, 2),
}

# 归类 → 中文说明 + 扫描基础分
CLASS_META = {
    "left_probe": ("低风险左侧试探", 14),
    "left_high_rr": ("高盈亏比左侧", 19),
    "right_main": ("稳健主升启动", 23),
    "right_continuation": ("趋势中继加仓", 20),
}

# 结构性参数 (根 K 线)
SPRING_RECOVER_WIN = 12      # 弹簧低点后收回确认窗口
SPRING_RETEST_WIN = 30       # 弹簧二次测试搜索窗口
RETEST_VOL = 0.92            # 二次测试缩量阈值 (< vol_ma20×92%)
RETEST_HOLD = 0.995          # 二次测试低点须守住弹簧低点 (>弹簧低点×99.5%)
STOP_BUF = 0.985             # 止损在锚点下方 1.5%
DIP_WIN = 30                 # 中继: 突破前整理回踩窗口
MID_RISE = 0.08              # 中继: 40 根内累计涨幅 ≥8% 视为上升途中
ACTIONABLE_LOOK = 10         # 扫描/实盘: 买点 bar 距当前 ≤10 根仍可执行
CONFIRM_WIN = 4              # 确认: 锚点后 windows 内收盘收复验证

# 结构性突破/回踩 (不依赖事件检测, 覆盖 SOS/JOC/LPS/BU 稀疏的短板)
RANGE_LOOK = 30              # 突破参考: 前 R 根箱体
BREAK_VOL = 1.5              # 突破放量阈值 (vol ≥ vol_ma20×1.5)
BREAK_MARGIN = 0.005         # 突破收盘须超过箱顶 ≥0.5%
RANGE_BAND_MAX = 0.35        # 箱体带宽上限 (防把趋势当箱体)
PULLBACK_WIN = 25            # 突破后回踩搜索窗口
PULLBACK_VOL = 0.95          # 回踩缩量阈值 (vol < vol_ma20×95%)
PULLBACK_HOLD = 0.98         # 回踩低点守住突破位 (±2%)
PULLBACK_RECOVER = True      # 回踩收盘须收复箱顶
BREAK_DEDUP_GAP = 10         # 同区间连续突破去重最小间隔
CONFIRM_WIN_BRK = 6          # 突破确认窗口 (站稳上限根数)
CONFIRM_BARS = 2             # 站稳箱顶的连续根数 (突破确认)
# 右侧加仓纪律: 右侧买点 (sos_break/bu_backup/markup_break/markup_bu) 仅当
# 其前 GATE_LEFT_LOOK 根内存在价位更低(≤×RIGHT_LOWER)的左侧买点时才成立 —
# 即必须在吸筹底部已先左侧起仓后, 右侧确认才可加仓 (文档"左低右高"加仓原则)。
GATE_LEFT_LOOK = 45          # 右侧加仓前提: 前向左侧买点窗口
RIGHT_LOWER = 0.995          # 左侧买点入场须低于右侧的乘数系数

# 买点分组 (供门控/剔除复用)
KIND_LEFT = ("st_bottom", "lps", "spring", "spring_retest")
KIND_RIGHT = ("sos_break", "bu_backup", "markup_break", "markup_bu")
# 实证负期望, 已从可执行买点中剔除 (识别能力保留, 但不进入最新买点/扫描/回测):
# 放量突破(SOS/JOC)、中继放量突破、中继回踩加仓 30 只回测 PF=0.52~0.86。
DISABLED_KINDS = frozenset({"sos_break", "markup_break", "markup_bu"})


def _finite(x) -> bool:
    return bool(np.isfinite(x))


def _markup_struct(df, i, rise_win=40, min_rise=MID_RISE):
    """结构化的上升趋势判定: MA20>MA50 且收盘站上 MA20, 且近 rise_win 根涨幅达标。

    作为 judge_phase 的补充, 更稳健地刻画"上涨途中" (中继买点的前提),
    且不依赖枢轴质量, 便于合成数据测试。"""
    if i < rise_win:
        return False
    c = df["close"].values
    ma20 = df["price_ma20"].values
    ma50 = df["price_ma50"].values
    if not (_finite(ma20[i]) and _finite(ma50[i])):
        return False
    if not (ma20[i] > ma50[i] and c[i] > ma20[i]):
        return False
    ref = c[i - rise_win]
    if ref <= 0:
        return False
    return (c[i] / ref - 1) >= min_rise


def _phase_at(df, pivots, events, i, cache=None):
    """在 bar i 处 (仅用 ≤i 的数据) 计算威科夫阶段归类。

    返回 "accumulation"/"markup"/"distribution"/"markdown"/"flat"。
    markup 优先用结构化判定 (更早、更稳), 派发/下跌必须尊重
    judge_phase (对应文档"不在派发、下跌区间买入"的纪律)。"""
    if cache is not None and i in cache:
        return cache[i]
    ctx = None
    if _markup_struct(df, i):
        ctx = "markup"
    else:
        try:
            wdf = df.iloc[:i + 1].copy()
            wp = [p for p in pivots if p["idx"] <= i]
            we = [e for e in events if e["idx"] <= i]
            if len(wdf) >= 30:
                phase, _ = judge_phase(wdf, wp, we)
                base = (phase or "").split(" ")[0]
                if base == "底部整固":
                    ctx = "accumulation"
                elif base == "上升趋势":
                    ctx = "markup"
                elif base == "顶部构筑":
                    ctx = "distribution"
                elif base == "下跌趋势":
                    ctx = "markdown"
                else:
                    ctx = "flat"
        except Exception:
            ctx = "flat"
    if cache is not None:
        cache[i] = ctx
    return ctx


def _reject_stage(context, kinds):
    """文档纪律: 派发/下跌阶段不买。context 为 distribution/markdown 时拒绝。"""
    return context in ("distribution", "markdown")


def _find_recovery(close, low, start, anchor_low, win=None):
    """自 start 之后首根"收盘收复 anchor_low"的 bar; 无则返回 None。
    与事件检测器"Spring 后收回"口径一致 (无须未来, 以该 bar 为决策点)。"""
    n = len(close)
    end = min(n, start + (win if win is not None else SPRING_RECOVER_WIN))
    for j in range(start + 1, end + 1):
        if close[j] > anchor_low:
            return int(j)
    return None


def _mk_bp(df, kind, e=None, bar_idx=None, bar_price=None, entry_price=None,
           stop_price=None, anchor_low=None, conf=None, extra=None):
    """构造统一的买点记录。"""
    day = df["day"].values
    meta = KIND_META[kind]
    bp = {
        "kind": kind,
        "label": meta[0],
        "cls": meta[1],
        "cls_label": CLASS_META[meta[1]][0],
        "rr": meta[2],
        "position": meta[3],
        "stage": None,
        "bar_idx": int(bar_idx),
        "bar_date": str(day[bar_idx]) if bar_idx is not None else None,
        "anchor_idx": int(e["idx"]) if e is not None else int(bar_idx),
        "anchor_price": float(e["price"]) if e is not None else float(bar_price),
        "conf": int(conf or 0) or 50,
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "target_price": None,
        "validation": [],
        "msg": "",
    }
    risk = bp["entry_price"] - bp["stop_price"]
    if risk > 0:
        bp["target_price"] = float(bp["entry_price"] + risk * bp["rr"])
    if extra:
        bp.update(extra)
    return bp


# ──────────────────────────── 各买点识别 ────────────────────────────

def _spring_bp(df, context, e):
    """弹簧/震仓: 刺破前低后收回的确认。决策点 = 收回 bar, 止损=弹簧低点下方。"""
    n = len(df)
    sp = int(e["idx"])
    sp_low = float(e["price"])
    close = df["close"].values
    if sp_low <= 0 or sp + 1 >= n:
        return None
    rec = _find_recovery(close, df["low"].values, sp, sp_low)
    if rec is None:
        return None
    entry = float(close[rec])
    stop = sp_low * STOP_BUF
    if entry <= stop:
        return None
    bp = _mk_bp(df, "spring", e=e, bar_idx=rec, entry_price=entry,
                stop_price=stop, anchor_low=sp_low, conf=e.get("conf"))
    bp["stage"] = context
    bp["validation"] = [f"弹簧@{sp_low:.2f}后第{rec - sp}根收回",
                        "未跌破弹簧低点",
                        "反转K线确认后买入"]
    bp["msg"] = f"{bp['label']}: 弹簧@{sp_low:.2f}收回确认"
    return bp


def _spring_retest_bp(df, context, e):
    """弹簧之后的二次测试: 弹簧后拉升、随后缩量回落守住弹簧低点, 再收复。

    仅利用弹簧 bar 到"收复 bar"之间的数据, 因果成立。"""
    n = len(df)
    sp = int(e["idx"])
    sp_low = float(e["price"])
    close = df["close"].values
    low = df["low"].values
    vol = df["volume"].values
    vma = df["vol_ma20"].values
    if sp_low <= 0 or sp + 1 >= n:
        return None
    end = min(n - 1, sp + SPRING_RETEST_WIN)
    # 弹簧后须先有一波拉升 (否则"二次测试"无从谈起)
    rally = close[sp + 1:min(sp + 8, end) + 1]
    if not len(rally) or float(np.max(rally)) <= sp_low * 1.02:
        return None
    for j in range(sp + 2, end + 1):
        if low[j] < sp_low * RETEST_HOLD:
            continue  # 跌破弹簧低点 → 非有效的二次测试
        if low[j] <= 0:
            continue
        if not _finite(vma[j]) or vma[j] <= 0:
            continue
        if vol[j] >= vma[j] * (1 / RETEST_VOL):
            continue  # 量未萎缩 → 不是"卖压耗尽"
        # 相对回落: 低于弹簧后累计高点 3% 以上才算"回落"
        peak = float(np.max(close[sp:j + 1]))
        if peak <= 0 or close[j] >= peak * 0.997:
            continue
        # 收回确认: 次窗内收盘收复测试低点
        rec = _find_recovery(close, low, j, low[j], win=CONFIRM_WIN)
        if rec is None:
            continue
        entry = float(close[rec])
        stop = sp_low * STOP_BUF
        if entry <= stop:
            continue
        bp = _mk_bp(df, "spring_retest", e=e, bar_idx=rec, entry_price=entry,
                    stop_price=stop, anchor_low=sp_low, conf=e.get("conf"))
        bp["stage"] = context
        bp["validation"] = [f"测试低点@{low[j]:.2f}守住弹簧低点{sp_low:.2f}",
                            f"回落缩量 (vol/ma20={vol[j] / vma[j]:.2f})",
                            f"次窗收复({rec - j}根)"]
        bp["msg"] = f"{bp['label']}: 缩量守住弹簧低点{sp_low:.2f}"
        return bp
    return None


def _st_bp(df, context, e):
    """SC 后二次测试: 缩量回踩 SC 区未创新低。决策点 = ST bar, 止损=ST低点下方。"""
    n = len(df)
    i = int(e["idx"])
    anchor = float(e["price"])
    close = df["close"].values
    low = df["low"].values
    if not (0 <= i < n) or anchor <= 0 or close[i] <= 0:
        return None
    entry = float(close[i])
    stop = anchor * 0.97
    if entry <= stop:
        return None
    bp = _mk_bp(df, "st_bottom", e=e, bar_idx=i, entry_price=entry,
                stop_price=stop, anchor_low=anchor, conf=e.get("conf"))
    bp["stage"] = context
    low_win = min(i, 3)
    guard = float(np.min(low[i - low_win:i + 1]))
    bp["validation"] = [f"回踩{anchor:.2f}未创新低",
                        "缩量回踩SC区 (事件自带)",
                        f"近{low_win + 1}根低点{guard:.2f}",
                        "止损=ST低点下方3%"]
    bp["msg"] = f"{bp['label']}: 缩量回踩{anchor:.2f}"
    return bp


def _lps_bp(df, context, e, kind=None):
    """突破后缩量回踩: LPS/BU 事件。决策点 = 事件 bar, 止损=回踩低点下方。"""
    n = len(df)
    i = int(e["idx"])
    anchor = float(e["price"])
    close = df["close"].values
    if not (0 <= i < n) or anchor <= 0 or close[i] <= 0:
        return None
    kind = kind or ("bu_backup" if e["type"] == "BU" else "lps")
    entry = float(close[i])
    stop = anchor * 0.97
    if entry <= stop:
        return None
    bp = _mk_bp(df, kind, e=e, bar_idx=i, entry_price=entry,
                stop_price=stop, anchor_low=anchor, conf=e.get("conf"))
    bp["stage"] = context
    bp["validation"] = ["缩量回踩 (事件自带)", f"守住锚点{anchor:.2f}",
                        "止损=回踩低点下方3%"]
    bp["msg"] = f"{bp['label']}: 缩量回踩{anchor:.2f}不破"
    return bp


def _sos_bp(df, context, e, kind="sos_break"):
    """放量突破: SOS/JOC。决策点 = 突破 bar, 止损=突破bar低点下方。"""
    n = len(df)
    i = int(e["idx"])
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    if not (0 <= i < n) or close[i] <= 0 or low[i] <= 0:
        return None
    entry = float(close[i])
    stop = float(low[i] * 0.985)
    if entry <= stop:
        return None
    ref = float(np.max(high[max(0, i - 20):i])) if i >= 5 else None
    bp = _mk_bp(df, kind, e=e, bar_idx=i, entry_price=entry,
                stop_price=stop, anchor_low=low[i], conf=e.get("conf"))
    bp["stage"] = context
    bp["validation"] = ["放量突破 (事件自带)", "突破后未深回",
                        f"突破bar低点{low[i]:.2f}为止损参考"]
    if ref:
        bp["validation"].append(f"突破近20根高点{ref:.2f}")
    bp["msg"] = f"{bp['label']}: 放量突破收{entry:.2f}"
    return bp


def _has_recent_dip(df, i):
    """突破前是否存在整理回踩 (跌破过 MA20): 用于区分"中继突破"与单纯新高。

    仅看 [i-DIP_WIN, i) 数据, 因果成立。"""
    if i < DIP_WIN + 10:
        return False
    c = df["close"].values
    ma20 = df["price_ma20"].values
    a = i - DIP_WIN
    seg = c[a:i]
    mseg = ma20[a:i]
    ok = np.isfinite(mseg)
    if not ok.any():
        return False
    return bool((seg[ok] < mseg[ok]).any())


# ──────────────────── 结构性突破 / 突破后回踩 (事件缺口的补充) ────────────────────

def _structural_breakouts(df):
    """放量上破近期箱体的巴识别 (SOS/JOC 事件之外的结构化补充)。

    条件 (全部因果):
      - vol ≥ vol_ma20×BREAK_VOL (放量);
      - 收盘 ≥ 前 RANGE_LOOK 根高点 ×(1+BREAK_MARGIN) (突破);
      - 前 RANGE_LOOK 根带宽 ≤ RANGE_BAND_MAX (是箱体/整理, 非趋势);
      - 突破前一日收盘仍 ≤ 箱顶×1.03 (箱体内酝酿, 非趋势追高)。
    相邻 10 根内只留 conf 最高的一个。"""
    n = len(df)
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    vma = df["vol_ma20"].values
    R = RANGE_LOOK
    out = []
    for i in range(R + 20, n):
        if not _finite(vma[i]) or vma[i] <= 0:
            continue
        if vol[i] < vma[i] * BREAK_VOL:
            continue
        H = float(np.max(high[i - R:i]))
        if H <= 0 or close[i] <= H * (1 + BREAK_MARGIN):
            continue
        band_hi = float(np.max(high[i - R:i]))
        band_lo = float(np.min(low[i - R:i]))
        if band_lo <= 0 or band_hi / band_lo - 1 > RANGE_BAND_MAX:
            continue
        if close[i - 1] > H * 1.03:
            continue
        vr = vol[i] / vma[i]
        conf = int(min(100, round(60 + (vr - BREAK_VOL) * 12 +
                                  (12 if close[i] >= H * 1.02 else 0))))
        out.append({"idx": i, "level": H, "close": float(close[i]),
                    "vr": float(vr), "conf": conf})
    res = []
    for s in out:
        if res and s["idx"] - res[-1]["idx"] < BREAK_DEDUP_GAP:
            if s["conf"] > res[-1]["conf"]:
                res[-1] = s
            continue
        res.append(s)
    return res


def _structural_confirmations(df, breakouts):
    """对放量突破的候选做"站稳箱顶"确认: 突破后连续 CONFIRM_BARS 根收盘在箱顶上方。

    返回确认后的买点候选: 在首个满足条件的 bar 收盘入场。不像裸突破
    直接用一根K线入场 — 需站稳后才右侧确认, 降低假突破成本。"""
    n = len(df)
    close = df["close"].values
    out = []
    for s in breakouts:
        b = s["idx"]
        level = s["level"]
        j = None
        for k in range(1, CONFIRM_WIN_BRK + 1):
            end = b + k
            if end >= n:
                break
            if close[end] <= level:
                continue
            if end - CONFIRM_BARS + 1 >= b + 1 and all(
                close[end - CONFIRM_BARS + 1:end + 1] > level
            ):
                j = end
                break
        if j is None:
            continue
        item = dict(s)
        item["j"] = j
        item["conf"] = int(min(100, s["conf"] + 12))
        out.append(item)
    return out


def _structural_pullbacks(df, breakouts):
    """突破后缩量回踩守住突破位并收复箱顶 (BU 式回踩)。

    条件 (全部因果):
      - 回踩 bar 在突破后 PULLBACK_WIN 根内, vol < vol_ma20×PULLBACK_VOL (缩量);
      - 回踩低点 ≥ 突破位×PULLBACK_HOLD (守住支持);
      - 回踩收盘 ≥ 突破位 (盘中测试后收复箱顶, 守稳才有效);
      - 回踩收盘低于突破日收盘 (是回抽而非直接续攻)。
    首根满足者即入场。"""
    n = len(df)
    close = df["close"].values
    low = df["low"].values
    vol = df["volume"].values
    vma = df["vol_ma20"].values
    out = []
    for s in breakouts:
        b = s["idx"]
        level = s["level"]
        clo = close[b]
        end = min(b + PULLBACK_WIN + 1, n - 1)
        for j in range(b + 1, end + 1):
            if not _finite(vma[j]) or vma[j] <= 0:
                continue
            if vol[j] >= vma[j] * PULLBACK_VOL:
                continue
            if low[j] < level * PULLBACK_HOLD:
                continue
            if PULLBACK_RECOVER and close[j] < level:
                continue
            if close[j] >= clo:
                continue  # 未回踩 (收盘未低于突破日), 非回踩买点
            shrink = vol[j] / vma[j]
            conf = int(min(100, round(60 + (1.0 / max(shrink, 1e-3)) * 6 +
                                      (8 if low[j] >= level else 0))))
            out.append({"idx": b, "level": level, "j": j,
                        "shrink": float(shrink), "conf": conf})
            break  # 每个突破只取首个有效回踩
    return out


def _struct_break_bp(df, context, s, kind):
    """结构性放量突破(确认站稳) → 买点。

    s: _structural_confirmations 的候选 {idx=突破bar, j=确认bar, level=箱顶,
    vr, conf}; 以确认 bar 收盘入场, 止损=箱顶下方。"""
    if "j" not in s:
        return None
    b = s["idx"]
    j = s["j"]
    level = s["level"]
    entry = float(df["close"].values[j])
    stop = level * 0.985
    if entry <= stop:
        return None
    bp = _mk_bp(df, kind, e=None, bar_idx=j, bar_price=entry, entry_price=entry,
                stop_price=stop, anchor_low=level, conf=s["conf"])
    bp["stage"] = context
    bp["validation"] = [f"放量(vol/ma20={s['vr']:.1f}x)破30根箱顶{level:.2f}",
                        f"突破后{b}→{j}站稳箱顶", "止损=箱顶下方"]
    bp["msg"] = f"{bp['label']}: 放量突破箱顶{level:.2f}并站稳"
    return bp


def _struct_pullback_bp(df, context, s, kind):
    """结构性突破后回踩 → 买点 (锚点=突破位/原阻力变新支撑)。"""
    j = s["j"]
    level = s["level"]
    entry = float(df["close"].values[j])
    stop = level * 0.985
    if entry <= stop:
        return None
    bp = _mk_bp(df, kind, e=None, bar_idx=j, bar_price=entry, entry_price=entry,
                stop_price=stop, anchor_low=level, conf=s["conf"])
    bp["stage"] = context
    bp["validation"] = [f"缩量回踩(vol/ma20={s['shrink']:.2f})",
                        f"守住突破位{level:.2f}并收复", "止损=突破位下方"]
    bp["msg"] = f"{bp['label']}: 缩量回踩突破位{level:.2f}并收复"
    return bp


# ──────────────────────────── 主入口 ────────────────────────────

def struct_buy_points(df, events, pivots=None, context_cache=None):
    """在完整历史上组装全部因果买点。

    Args:
        df: add_indicators 后的 DataFrame (含 price_ma20/price_ma50/vol_ma20 等)
        events: detect_all 输出的事件列表
        pivots: find_pivots 输出 (可选, 用于阶段判定)
        context_cache: 复用阶段缓存 (内部调用用)

    Returns:
        list[dict]: 每个买点记录 {kind, label, cls, cls_label, rr, position,
        bar_idx, bar_date, anchor_idx, anchor_price, entry_price, stop_price,
        target_price, conf, validation, msg, ...}
    """
    if df is None or len(df) < 120 or not events:
        return []
    pivots = pivots or []
    cache = context_cache if context_cache is not None else {}
    out = []

    for e in events:
        t = e["type"]
        i = int(e["idx"])
        if not (60 <= i < len(df)):
            continue
        ctx = _phase_at(df, pivots, events, i, cache)
        if t == "Spring":
            if _reject_stage(ctx, ("spring",)):
                continue
            bp = _spring_bp(df, ctx, e)
            if bp:
                out.append(bp)
            bp2 = _spring_retest_bp(df, ctx, e)
            if bp2:
                out.append(bp2)
        elif t == "Shakeout":
            if _reject_stage(ctx, ("spring",)):
                continue
            bp = _spring_bp(df, ctx, e)
            if bp:
                out.append(bp)
        elif t == "ST":
            if _reject_stage(ctx, ("st_bottom",)):
                continue
            bp = _st_bp(df, ctx, e)
            if bp:
                out.append(bp)
        elif t in ("SOS", "JOC"):
            if _reject_stage(ctx, ("sos_break",)):
                continue
            if ctx == "markup" and _has_recent_dip(df, i):
                bp = _sos_bp(df, ctx, e, kind="markup_break")
                if bp:
                    out.append(bp)
            else:
                bp = _sos_bp(df, ctx, e, kind="sos_break")
                if bp:
                    out.append(bp)
        elif t in ("LPS", "BU"):
            if _reject_stage(ctx, ("lps", "bu_backup")):
                continue
            kind = "markup_bu" if ctx == "markup" else (
                "bu_backup" if t == "BU" else "lps")
            bp = _lps_bp(df, ctx, e, kind=kind)
            if bp:
                out.append(bp)
        else:
            continue

    # 结构性补充: 事件中 SOS/JOC/LPS/BU 极其稀疏 (需吸筹背景), 直接按
    # 价格/量价结构检测放量突破(确认站稳)与突破后回踩, 覆盖阶段D与主升中继买点。
    st_breaks = _structural_breakouts(df)
    st_confirms = _structural_confirmations(df, st_breaks)
    st_pulls = _structural_pullbacks(df, st_breaks)
    ev_sos_bars = {int(e["idx"]) for e in events if e["type"] in ("SOS", "JOC")}
    ev_pull_bars = {int(e["idx"]) for e in events if e["type"] in ("LPS", "BU")}
    for s in st_confirms:
        if s["idx"] in ev_sos_bars:
            continue
        ctx = _phase_at(df, pivots, events, s["j"], cache)
        if _reject_stage(ctx, ("sos_break",)):
            continue
        kind = "markup_break" if ctx == "markup" else "sos_break"
        bp = _struct_break_bp(df, ctx, s, kind)
        if bp:
            out.append(bp)
    for s in st_pulls:
        j = s["j"]
        if j in ev_pull_bars:
            continue
        ctx = _phase_at(df, pivots, events, j, cache)
        if _reject_stage(ctx, ("bu_backup", "lps")):
            continue
        kind = "markup_bu" if ctx == "markup" else \
            ("bu_backup" if s["idx"] > 0 else "lps")
        bp = _struct_pullback_bp(df, ctx, s, kind)
        if bp:
            out.append(bp)

    # 去重: 同一 kind 同 bar 只留最高 conf
    seen = {}
    for bp in out:
        key = (bp["kind"], bp["bar_idx"])
        if key not in seen or bp["conf"] > seen[key]["conf"]:
            seen[key] = bp
    result = sorted(seen.values(), key=lambda b: (b["bar_idx"], b["bar_date"],
                                                  -b["conf"]))
    for bp in result:
        anchor = df["low"].values[bp["anchor_idx"]]
        bp["anchor_date"] = str(df["day"].values[bp["anchor_idx"]])
        bp["anchor_price"] = float(bp["anchor_price"])
        if bp["anchor_price"] <= 0:
            bp["anchor_price"] = float(anchor)

    # 左侧起仓 → 右侧加仓纪律: 右侧买点须以价位更低的左侧买点为前提。
    # (实证: 无此前提时右侧单独开仓盈亏比<1, 加此后整体胜率明显抬升。)
    right_gate = [b for b in result if b["kind"] in KIND_RIGHT]
    if right_gate:
        cache_gate = [(b["bar_idx"], b["entry_price"]) for b in result
                      if b["kind"] in KIND_LEFT]
        out_f = []
        for b in result:
            if b["kind"] in KIND_RIGHT:
                ok = any(j < b["bar_idx"] and b["bar_idx"] - j <= GATE_LEFT_LOOK
                         and left_price < b["entry_price"] * RIGHT_LOWER
                         for (j, left_price) in cache_gate)
                if not ok:
                    continue
            out_f.append(b)
        result = out_f
    # 剔除实证负期望的买点 (见 DISABLED_KINDS 注释)
    if DISABLED_KINDS:
        result = [b for b in result if b["kind"] not in DISABLED_KINDS]
    return result


def latest_buy_points(df, events, pivots=None, look=ACTIONABLE_LOOK):
    """当前可执行的买点 (近 look 根内触发、price 仍在入场~目标之间)。"""
    bps = struct_buy_points(df, events, pivots or [])
    if not bps:
        return []
    n = len(df)
    close = df["close"].values
    out = []
    for bp in bps:
        if bp["bar_idx"] < n - look:
            continue
        last = float(close[-1])
        if last < bp["entry_price"] * 0.995:
            continue  # 已跌破入场位, 买点失效
        if bp["target_price"] and last >= bp["target_price"]:
            continue  # 已到目标, 不再追
        bp = dict(bp)
        bp["now"] = last
        bp["gap"] = round((last / bp["entry_price"] - 1) * 100, 2) if bp["entry_price"] else 0.0
        out.append(bp)
    out.sort(key=lambda b: (CLASS_META[b["cls"]][1], b["bar_idx"]), reverse=True)
    return out[:8]


# ──────────────────────────── 扫描 (scan_adv 兼容) ────────────────────────────

def _stock_name(code):
    try:
        from .screener import _get_stock_name
        return _get_stock_name(str(code)[-6:] if len(str(code)) > 6 else str(code))
    except Exception:
        return ""


def scan_buypoints(codes, workers=6, cancel_event=None, datalen=500):
    """按"完整做多买点"框架扫描全市场当前可执行买点。

    返回行: {code, name, last, phase, score, cls, kind, entry, stop,
    target, rr, gap, conf, msg}。"""
    from .datasource import fetch_kline
    from .utils import normalize_symbol

    def one(code):
        try:
            symbol = normalize_symbol(code)
            df = fetch_kline(symbol, datalen=datalen, scale=240)
            df = add_indicators(df, symbol=symbol)
            pivots = find_pivots(df, order=6)
            events = detect_all(df, pivots)
            bps = latest_buy_points(df, events, pivots)
        except Exception:
            return None
        if not bps:
            return None
        last = float(df["close"].values[-1])
        try:
            phase, _ = judge_phase(df, pivots, events)
        except Exception:
            phase = ""
        best = bps[0]
        phase_base = (phase or "").split(" ")[0] if phase else ""
        cls_base = CLASS_META[best["cls"]][1]
        phase_bonus = {"底部整固": 4, "上升趋势": 3, "顶部构筑": -6,
                       "下跌趋势": -8}.get(phase_base, 0)
        conf_bonus = min(10, int(best["conf"] / 10))
        score = cls_base + conf_bonus + phase_bonus + (3 if best["bar_idx"] >= len(df) - 5 else 1)
        return {
            "code": str(code)[-6:] if len(str(code)) > 6 else str(code),
            "name": _stock_name(code),
            "last": round(last, 2),
            "phase": phase_base,
            "score": round(max(1, score), 1),
            "cls": best["cls_label"],
            "kind": best["kind"],
            "label": best["label"],
            "entry": round(best["entry_price"], 2),
            "stop": round(best["stop_price"], 2),
            "target": round(best["target_price"], 2) if best["target_price"] else None,
            "rr": best["rr"],
            "gap": best.get("gap", 0.0),
            "conf": best["conf"],
            "msg": f"{best['msg']} | 入{best['entry_price']:.2f} 损{best['stop_price']:.2f} "
                   f"标{best['target_price']:.2f} RR1:{best['rr']}",
        }

    if workers <= 1 or not codes:
        rows = [r for c in codes if c and (r := one(c))]
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        rows = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, c): c for c in codes}
            for fu in as_completed(futs):
                if cancel_event is not None and cancel_event.is_set():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                r = fu.result()
                if r:
                    rows.append(r)
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    return rows


# 便捷: 单股分析入口 (供脚本/交互)
def analyze_buypoints(code, datalen=500, scale=240):
    """抓取单只股票并返回 (df, buy_points, latest)。"""
    from .datasource import fetch_kline
    from .utils import normalize_symbol

    symbol = normalize_symbol(code)
    df = fetch_kline(symbol, datalen=datalen, scale=scale)
    df = add_indicators(df, symbol=symbol)
    pivots = find_pivots(df, order=6)
    events = detect_all(df, pivots)
    return df, struct_buy_points(df, events, pivots), latest_buy_points(df, events, pivots)


__all__ = [
    "KIND_META", "CLASS_META", "struct_buy_points", "latest_buy_points",
    "scan_buypoints", "analyze_buypoints",
]
