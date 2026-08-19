# -*- coding: utf-8 -*-
"""K线图 / 资金透视 (资金/筹码/股东) 绘制。"""
from collections import defaultdict

import numpy as np
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Arc, Circle, Rectangle
from matplotlib.text import Text

from .config import (_fs, _PHASE_STYLE, EVENT_CN, event_dir,
                     THEME, C_UP, C_UP_DARK, C_DOWN, C_DOWN_DARK, C_GRID, W_RECENT,
                     SD_BULL, SD_BEAR)
from .fundamental import holder_ratio_ok
from .phases import phase_segments
from .waves import extract_wave_points

# A股配色: 红涨绿跌
_UP = C_UP
_UP_DK = C_UP_DARK
_DN = C_DOWN
_DN_DK = C_DOWN_DARK


def _index_overlay(df, index_series, min_len=20):
    """大盘归一叠加: 按日期对齐个股与上证指数, 返回 (x_pos, norm_close) 或 (None, None)。

    个股与指数交易日往往不完全一致 (停牌/实时bar差一天), 若按"尾部位置"对齐
    会把叠加线整体错位一天以上且无任何提示 (与 RS 面板同样的对齐问题)。这里按
    day 交集对齐, 并从第一个交集日对齐到个股同位置收盘价, 之后跟随指数相对涨跌。
    """
    if index_series is None or len(index_series) < min_len:
        return None, None
    try:
        ix_days = index_series["day"].to_numpy()
        ix_close = index_series["close"].to_numpy()
        day_map = {}
        for i in range(len(ix_days)):
            d = ix_days[i]
            v = ix_close[i]
            if v is not None and np.isfinite(v):
                day_map[d] = float(v)
        days = df["day"].to_numpy()
        pos = []
        vals = []
        for i in range(len(days)):
            v = day_map.get(days[i])
            if v is not None and v > 0:
                pos.append(i)
                vals.append(v)
        if len(pos) < min_len:
            return None, None
        base = float(df["close"].to_numpy()[pos[0]])
        if base <= 0:
            return None, None
        v0 = vals[0]
        if v0 <= 0:
            return None, None
        vals = np.asarray(vals, dtype=float)
        return pos, (vals / v0 * base).tolist()
    except Exception:
        return None, None


def _fast_bars(ax, x, heights, bottom=None, colors=None, width=0.6,
               edgecolor=None, linewidth=0.5, alpha=1.0, zorder=1):
    """用 ax.add_artist 快速绘制大量垂直柱 (K线实体/成交量/MACD柱)。

    相比逐个 ax.bar (内部为每根柱 add_patch + _update_patch_limits, 每根都要
    走一遍 datalim 更新), add_artist 跳过 datalim 计算, 700 根柱时耗时从
    ~330ms 降到 ~60ms。外观与 ax.bar 逐像素一致 (含亚像素柱的整数像素化)。
    heights 允许为负 (向下绘制, 对应 MACD 绿柱)。"""
    from matplotlib.patches import Rectangle
    x = np.asarray(x, dtype=float)
    heights = np.asarray(heights, dtype=float)
    rooted_at_zero = bottom is None
    bottom = np.zeros_like(x) if rooted_at_zero else np.asarray(bottom, dtype=float)
    if colors is None:
        colors = [None] * len(x)
    if edgecolor is None:
        edgecolor = ["none"] * len(x)
    half = width / 2.0
    for i in range(len(x)):
        h = heights[i]
        if not np.isfinite(h):
            continue
        rect = Rectangle((x[i] - half, bottom[i]), width, h,
                         facecolor=colors[i], edgecolor=edgecolor[i],
                         linewidth=linewidth, alpha=alpha, zorder=zorder)
        if rooted_at_zero:
            rect.sticky_edges.y[:] = [0]
        ax.add_artist(rect)
    finite = np.isfinite(heights)
    if finite.any():
        xs = x[finite]
        ys0 = bottom[finite]
        ys1 = bottom[finite] + heights[finite]
        corners = np.column_stack([np.concatenate([xs - half, xs + half]),
                                   np.concatenate([ys0, ys1])])
        ax.update_datalim(corners)
    return None


def draw_lock(ax, x, y, label="", size=18, offset_pt=6, color=_DN, dark="#15803d"):
    """在数据点 (x, y) 右侧画一把小锁 (像素单位, 不依赖字体字形)。
    label 非空时在锁身上显示编号 (如 1/2/3), 便于区分逐把上锁的顺序。
    color/dark 控制锁身与锁环颜色: 买点用红, 卖点用绿。"""
    w, h = size, int(size * 1.15)
    da = DrawingArea(w, h, 0, 0)
    body = Rectangle((w * 0.16, h * 0.28), w * 0.68, h * 0.52,
                     facecolor=color, edgecolor=dark, linewidth=1.3,
                     joinstyle="round")
    da.add_artist(body)
    shackle = Arc((w * 0.5, h * 0.60), w * 0.60, w * 0.60,
                  theta1=0, theta2=180, edgecolor=dark,
                  linewidth=1.8, facecolor="none")
    da.add_artist(shackle)
    if label:
        t = Text(w * 0.5, h * 0.50, label, ha="center", va="center",
                 fontsize=size * 0.58, fontweight="bold", color="#ffffff")
        da.add_artist(t)
    else:
        hole = Circle((w * 0.5, h * 0.50), w * 0.08, facecolor="#ffffff")
        da.add_artist(hole)
    ab = AnnotationBbox(da, (x, y), xybox=(offset_pt, 0),
                        xycoords="data", boxcoords="offset points",
                        frameon=False, annotation_clip=False, zorder=6)
    ax.add_artist(ab)
    return ab


# 均线配置: (列名, 颜色, 标签) — 主图叠加 MA5/10/20/50/200
_MA_LINES = [
    ("price_ma5", "#f783ac", "MA5"),
    ("price_ma10", "#12b886", "MA10"),
    ("price_ma20", "#1971c2", "MA20"),
    ("price_ma50", "#f08c00", "MA50"),
    ("price_ma200", "#adb5bd", "MA200"),
]


import warnings as _warnings


def _deprecated_matplotlib():
    _warnings.warn(
        "matplotlib 绘图路径 (plot_indicators/plot_chart/plot_market) 已弃用, "
        "PyQt6 桌面端改用 pyqtgraph 引擎 (build_ind_data/build_kline_data/"
        "build_market_data)。此路径仅保留供旧 wx 版与脚本迁移期间使用, "
        "后续版本将移除。",
        DeprecationWarning, stacklevel=3)


def plot_indicators(df, fig=None, index_series=None):
    """技术指标图: 布林带+大盘对比 / 量能+量比 / MACD / KDJ / RSI / OBV / 量价分布。
    index_series: 上证指数K线 (含 close 列), 用于与个股归一化叠加对比。"""
    _deprecated_matplotlib()
    if fig is None:
        fig = Figure(figsize=(8.5, 13.5), dpi=100)
    else:
        fig.clear()
    x = np.arange(len(df))
    n = len(df)
    close = df["close"].values
    colors = [_UP if c >= o else _DN
              for c, o in zip(df["close"].values, df["open"].values)]

    def _ticks(ax):
        step = max(1, n // 10)
        is_minute = df["day"].dt.hour.nunique() > 1
        idx = x[::step]
        if is_minute:
            labs = [df["day"].iloc[i].strftime("%m-%d %H:%M") for i in idx]
        else:
            labs = [df["day"].iloc[i].strftime("%y-%m-%d") for i in idx]
        ax.set_xticks(idx)
        ax.set_xticklabels(labs, fontsize=_fs(0), rotation=0)
        ax.grid(alpha=0.35, lw=0.5, color=C_GRID)

    gs = fig.add_gridspec(4, 2, height_ratios=[2.0, 1.3, 1.3, 1.5],
                          hspace=0.45, wspace=0.14)
    axp = fig.add_subplot(gs[0, :])
    # ── 价格 + 布林带 + 大盘归一叠加 ──
    axp.plot(x, close, color="#1f2937", lw=1.1, label="收盘")
    for col, color, lab in (("boll_up", _UP, "BOLL上轨"),
                            ("boll_dn", _DN, "BOLL下轨")):
        if col in df.columns:
            axp.plot(x, df[col].values, color=color, lw=0.9, ls="--", alpha=0.85, label=lab)
    if "boll_dn" in df.columns and "boll_up" in df.columns:
        axp.fill_between(x, df["boll_dn"].values, df["boll_up"].values,
                         color="#2563eb", alpha=0.06)
    x_ix, norm_ix = _index_overlay(df, index_series)
    if x_ix is not None:
        axp.plot(x_ix, norm_ix, color="#f08c00", lw=1.1,
                 label="上证(归一)")
    axp.set_title("价格 · 布林带 (20,2) · 大盘对比", fontsize=_fs(1))
    axp.legend(fontsize=_fs(-2), loc="upper left", ncol=3)
    _ticks(axp)

    # ── 成交量 + 量均线 + 量比 ──
    axv = fig.add_subplot(gs[1, 0])
    _fast_bars(axv, x, df["volume"].values / 1e4, colors=colors, width=0.6)
    for col, color, lab in (("vol_ma5", "#f783ac", "量MA5"),
                            ("vol_ma10", "#12b886", "量MA10")):
        if col in df.columns:
            axv.plot(x, df[col].values / 1e4, color=color, lw=0.9, label=lab)
    vr = float(df["vol_ratio_20"].iloc[-1]) if "vol_ratio_20" in df.columns else None
    axv.set_title(("量能 (万手)" + (f" · 量比 {vr:.2f}" if vr is not None else "")),
                  fontsize=_fs(1))
    axv.legend(fontsize=_fs(-2), loc="upper left")
    _ticks(axv)

    # ── MACD ──
    axm = fig.add_subplot(gs[1, 1])
    hist = df["macd_hist"].values
    _fast_bars(axm, x, hist, colors=[_UP if h >= 0 else _DN for h in hist], width=0.6)
    axm.plot(x, df["macd_dif"].values, color="#1971c2", lw=0.9, label="DIF")
    axm.plot(x, df["macd_dea"].values, color="#f08c00", lw=0.9, label="DEA")
    axm.axhline(0, color="#adb5bd", lw=0.7)
    axm.set_title("MACD (12,26,9)", fontsize=_fs(1))
    axm.legend(fontsize=_fs(-2), loc="upper left")
    _ticks(axm)

    # ── KDJ ──
    axk = fig.add_subplot(gs[2, 0])
    axk.plot(x, df["kdj_k"].values, color="#1971c2", lw=0.9, label="K")
    axk.plot(x, df["kdj_d"].values, color="#f08c00", lw=0.9, label="D")
    axk.plot(x, df["kdj_j"].values, color="#9c36b5", lw=0.7, label="J")
    axk.axhline(80, color="#adb5bd", ls="--", lw=0.7, alpha=0.6)
    axk.axhline(20, color="#adb5bd", ls="--", lw=0.7, alpha=0.6)
    axk.set_title("KDJ (9,3,3)", fontsize=_fs(1))
    axk.legend(fontsize=_fs(-2), loc="upper left")
    _ticks(axk)

    # ── RSI ──
    axr = fig.add_subplot(gs[2, 1])
    for col, color, lab in (("rsi_6", "#f783ac", "RSI6"),
                            ("rsi_12", "#1971c2", "RSI12"),
                            ("rsi_24", "#f08c00", "RSI24")):
        if col in df.columns:
            axr.plot(x, df[col].values, color=color, lw=0.9, label=lab)
    axr.axhline(70, color=_UP, ls="--", lw=0.8, alpha=0.5)
    axr.axhline(30, color=_DN, ls="--", lw=0.8, alpha=0.5)
    axr.set_ylim(-5, 105)
    axr.set_title("RSI (6,12,24)", fontsize=_fs(1))
    axr.legend(fontsize=_fs(-2), loc="upper left")
    _ticks(axr)

    # ── OBV 能量潮 ──
    axo = fig.add_subplot(gs[3, 0])
    axo.plot(x, df["obv"].values, color="#495057", lw=1.0)
    if "obv" in df.columns and len(df) >= 40:
        _obv = df["obv"].values
        axo.plot(x, _pd_ma(_obv, 20), color="#1971c2", lw=0.9, ls="--", label="OBV均线")
    axo.set_title("OBV 能量潮", fontsize=_fs(1))
    axo.legend(fontsize=_fs(-2), loc="upper left")
    _ticks(axo)

    # ── 量价分布 (Volume Profile) ──
    axd = fig.add_subplot(gs[3, 1])
    vp = _volume_profile(df)
    if vp is not None:
        edges, vols, mid, poc = vp
        axd.barh(mid, vols / 1e4, height=(edges[1] - edges[0]) * 0.9,
                 color=[_UP if m >= close[-1] else _DN for m in mid], alpha=0.85)
        axd.axhline(close[-1], color="#1f2937", lw=1.0, ls=":")
        axd.text(0.98, 0.02, f"现价 {close[-1]:.2f}", transform=axd.transAxes,
                 ha="right", va="bottom", fontsize=_fs(-2), color="#1f2937")
        axd.text(0.02, 0.98, f"POC {poc:.2f}", transform=axd.transAxes,
                 ha="left", va="top", fontsize=_fs(-2), color="#d97706")
        axd.set_title("量价分布 (Volume Profile)", fontsize=_fs(1))
        axd.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="x")
    else:
        axd.text(0.5, 0.5, "数据不足", ha="center", va="center", fontsize=_fs(3))
        axd.axis("off")

    fig.subplots_adjust(left=0.03, right=0.97, top=0.96, bottom=0.08)

    # ── 每个指标下方的"当前信号 → 预示"解读 ──
    # 用 set_xlabel 让 matplotlib 自动在坐标轴下方预留空间, 避免文字与图表重叠。
    _ax_map = {"price": axp, "volume": axv, "macd": axm, "kdj": axk,
               "rsi": axr, "obv": axo, "vp": axd}
    for panel, (msg, color) in ind_caps(df, index_series).items():
        ax = _ax_map[panel]
        ax.set_xlabel(msg, fontsize=_fs(1), color=color, fontweight="bold")
    return fig


def _volume_profile(df):
    """量价分布 (Volume Profile) 桶统计: (edges, vols, mid, poc) 或 None。

    供 matplotlib plot_indicators 与 pyqtgraph build_ind_data 共用, 保证
    两引擎的分布直方图与 POC 完全一致。
    """
    lo, hi = float(df["low"].min()), float(df["high"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, 24)
    idx = np.clip(np.digitize(df["close"].values, edges) - 1, 0, len(edges) - 2)
    vols = np.zeros(len(edges) - 1)
    for i, v in zip(idx, df["volume"].values):
        vols[i] += v
    mid = (edges[:-1] + edges[1:]) / 2
    poc = mid[int(np.argmax(vols))]
    return edges, vols, mid, poc


def ind_caps(df, index_series=None, rs_series=None):
    """技术指标"当前信号 → 预示"解读文案: {panel: (msg, color)}。

    panel ∈ price/volume/macd/kdj/rsi/obv/vp/rs。matplotlib plot_indicators
    与 pyqtgraph IndWidget 共用, 保证文案与配色完全一致。
    """
    n = len(df)
    close = df["close"].values
    last = float(close[-1])
    caps = {}

    if "boll_up" in df.columns and "boll_dn" in df.columns and len(df) >= 6:
        bu = float(df["boll_up"].iloc[-1])
        bm = float(df["boll_mid"].iloc[-1])
        bd = float(df["boll_dn"].iloc[-1])
        if last >= bu:
            pos = "上轨外(超强)"
        elif last >= bm:
            pos = "中轨上方"
        elif last >= bd:
            pos = "中轨下方"
        else:
            pos = "下轨外(超弱)"
        w0 = (bu - bd) / bm if bm else 0
        wm1 = df["boll_mid"].iloc[-6]
        w1 = (float(df["boll_up"].iloc[-6]) - float(df["boll_dn"].iloc[-6])) / wm1 if wm1 else w0
        band = "开口" if w0 >= w1 else "收口"
        idx_msg = ""
        x_ix, _norm = _index_overlay(df, index_series)
        if x_ix is not None:
            s0, s1 = x_ix[0], x_ix[-1]
            if close[s0] > 0:
                stock_ret = close[s1] / close[s0]
                idx_ret = _norm[-1] / _norm[0] if _norm and _norm[0] else 0.0
                idx_msg = " · 强于大盘" if stock_ret > idx_ret else " · 弱于大盘"
        trend = "上行趋势, 顺势看多" if last > bm else "偏空整理, 谨慎"
        ccolor = _UP if last > bm else _DN
        caps["price"] = (f"现价{last:.2f}在{pos} · 布林{band}{idx_msg} → {trend}", ccolor)
    if "vol_ratio_20" in df.columns and len(df):
        vr = float(df["vol_ratio_20"].iloc[-1])
        if vr >= 1.5:
            vmsg, vtip = "明显放量", "放量上攻则突破可信; 放量滞涨警惕出货"
        elif vr >= 1.2:
            vmsg, vtip = "温和放量", "量能配合, 关注能否持续"
        elif vr <= 0.8:
            vmsg, vtip = "明显缩量", "缩量整理, 方向待选择"
        else:
            vmsg, vtip = "量能平稳", "观望为主, 等待放量确认"
        vcolor = "#d97706" if vr >= 1.2 else "#64748b"
        caps["volume"] = (f"量比{vr:.2f} {vmsg} → {vtip}", vcolor)
    if "macd_dif" in df.columns and len(df) >= 3:
        dif = float(df["macd_dif"].iloc[-1])
        dea = float(df["macd_dea"].iloc[-1])
        hist = float(df["macd_hist"].iloc[-1])
        h0 = float(df["macd_hist"].iloc[-2])
        cross = "金叉" if dif > dea else "死叉"
        zero = "零轴上" if dif > 0 else "零轴下"
        if hist >= 0 and hist >= h0:
            hmsg = "红柱放大"
        elif hist >= 0:
            hmsg = "红柱缩短"
        elif hist < 0 and hist <= h0:
            hmsg = "绿柱放大"
        else:
            hmsg = "绿柱缩短"
        if cross == "金叉" and "放大" in hmsg:
            mtip = "多头动能增强, 有望延续上行"
            mcolor = _UP
        elif cross == "死叉" and "放大" in hmsg:
            mtip = "空头动能增强, 防下跌延续"
            mcolor = _DN
        else:
            mtip = "动能正在衰减, 警惕方向反转"
            mcolor = "#d97706"
        caps["macd"] = (f"{cross} · {zero} · {hmsg} → {mtip}", mcolor)
    if "kdj_k" in df.columns:
        k = float(df["kdj_k"].iloc[-1])
        d = float(df["kdj_d"].iloc[-1])
        j = float(df["kdj_j"].iloc[-1])
        cross = "金叉" if k > d else "死叉"
        if j > 100 or k > 80:
            state = "超买"
        elif j < 0 or k < 20:
            state = "超卖"
        else:
            state = "中性"
        if state == "超买":
            ktip, kcolor = "高位超买, 防回调", _UP
        elif state == "超卖":
            ktip, kcolor = "超卖, 反弹概率增大", _DN
        else:
            ktip, kcolor = ("短线偏多" if k > d else "短线偏空"), \
                (_UP if k > d else _DN)
        caps["kdj"] = (f"K{k:.0f} D{d:.0f} J{j:.0f} · {cross} · {state} → {ktip}", kcolor)
    if "rsi_6" in df.columns:
        r6 = float(df["rsi_6"].iloc[-1])
        if r6 >= 70:
            rmsg, rtip, rcolor = "超买", "短线过热, 防回调", _UP
        elif r6 <= 30:
            rmsg, rtip, rcolor = "超卖", "超卖, 反弹机会", _DN
        else:
            rmsg, rtip, rcolor = "中性", "多空均衡", "#64748b"
        caps["rsi"] = (f"RSI6 {r6:.0f} · {rmsg} → {rtip}", rcolor)
    if "obv" in df.columns and len(df) >= 30:
        obv = df["obv"].values
        o_now = float(obv[-1])
        o_20 = float(obv[-20])
        o_max = float(obv[-40:].max())
        if o_now >= o_max * 0.999:
            omsg, otip, ocolor = "创近期新高", "资金持续流入, 多头格局", _UP
        elif o_now < o_20:
            omsg, otip, ocolor = "走低", "资金流出, 弱势; 关注是否止跌", _DN
        else:
            omsg, otip, ocolor = "企稳/上行", "资金净流入为主, 偏多", _UP
        caps["obv"] = (f"OBV {omsg} → {otip}", ocolor)
    vp = _volume_profile(df)
    if vp is not None and "boll_dn" in df.columns and len(df) >= 6:
        _e, _v, _m, poc = vp
        if last >= poc:
            dmsg = f"现价{last:.2f}在密集区(POC {poc:.2f})上方 → 上方套牢少, 偏强"
            dcolor = _UP
        else:
            dmsg = f"现价{last:.2f}在密集区(POC {poc:.2f})下方 → 上方抛压重, 谨慎"
            dcolor = _DN
        caps["vp"] = (dmsg, dcolor)
    if rs_series is not None and len(rs_series) >= 2:
        rs = np.asarray(rs_series, dtype=float)
        valid = np.isfinite(rs)
        if valid.sum() >= 2:
            rs = rs[valid]
            cur = float(rs[-1])
            win = max(int(valid.sum()) - 1, 1)
            rmsg = f"RS {cur:+.1f}%"
            if cur > 2:
                rtip, rcolor = "显著强于大盘, 资金偏好", _UP
            elif cur > 0:
                rtip, rcolor = "小幅强于大盘", _UP
            elif cur > -2:
                rtip, rcolor = "小幅弱于大盘", _DN
            else:
                rtip, rcolor = "明显弱于大盘, 资金回避", _DN
            caps["rs"] = (f"RS({win}日) {rmsg} → {rtip}", rcolor)
    return caps


def build_ind_data(df, index_series=None, rs_series=None):
    """收集桌面端 pyqtgraph 技术指标所需绘制数据 (与 plot_indicators 同口径)。

    在 worker 线程内调用, 返回值可跨线程交给 IndWidget.set_data。
    rs_series: 相对强度时序 (relative_strength_series 输出, float[n]), 供
    技术指标页新增的 RS 面板绘制; None 时不输出该面板数据。
    """
    n = len(df)
    close = df["close"].values
    is_minute = False
    try:
        is_minute = df["day"].dt.hour.nunique() > 1
    except Exception:
        pass
    index_ov = None
    x_ix, norm_ix = _index_overlay(df, index_series)
    if x_ix is not None:
        index_ov = {"x": x_ix, "close": norm_ix}
    data = {
        "n": n,
        "day": [str(d) for d in df["day"].tolist()],
        "is_minute": is_minute,
        "x": np.arange(n).tolist(),
        "close": close.tolist(),
        "open": df["open"].values.tolist() if "open" in df.columns
        else [None] * n,
        "volume": (df["volume"].values / 1e4).tolist(),
        "index_ov": index_ov,
    }
    for col in ("boll_up", "boll_mid", "boll_dn", "macd_hist", "macd_dif",
                "macd_dea", "kdj_k", "kdj_d", "kdj_j", "rsi_6", "rsi_12",
                "rsi_24", "obv"):
        if col in df.columns:
            data[col] = df[col].values.tolist()
    for col in ("vol_ma5", "vol_ma10"):
        if col in df.columns:
            data[col] = (df[col].values / 1e4).tolist()
    if "vol_ratio_20" in df.columns and len(df):
        data["vol_ratio"] = float(df["vol_ratio_20"].iloc[-1])
    if "obv" in df.columns and len(df) >= 40:
        data["obv_ma"] = _pd_ma(df["obv"].values, 20).tolist()
    vp = _volume_profile(df)
    if vp is not None:
        _e, _v, _m, _p = vp
        data["vp"] = {
            "edges": _e.tolist(), "vols": (_v / 1e4).tolist(),
            "mid": _m.tolist(), "poc": float(_p),
            "last": float(close[-1]),
        }
    if rs_series is not None:
        rs = np.asarray(rs_series, dtype=float)
        if len(rs) == n:
            data["rs_series"] = rs.tolist()
    data["caps"] = {p: [m, c] for p, (m, c) in
                    ind_caps(df, index_series, rs_series).items()}
    return data


def _pd_ma(arr, n):
    """简单滑动平均 (纯numpy, 避免引入pandas依赖)。"""
    arr = np.asarray(arr, dtype=float)
    if len(arr) < n:
        return np.full(len(arr), np.nan)
    out = np.full(len(arr), np.nan)
    if n <= 1:
        out[:] = arr
        return out
    # 前缀和滑动平均: O(n) 而非 O(n×window)
    cs = np.concatenate(([0.0], np.cumsum(arr)))
    win = cs[n:] - cs[:-n]
    out[n - 1:] = win / n
    return out


def _wave_segments(n, waves):
    """从波浪点序列切分波段分段, 返回 [(a, b, direction)] 或 None。
    a/b 为闭区间 index; direction 1=上升(需求), -1=下跌(供给), 0=中性。
    首个波浪点之前的柱并入前导段, 末点之后并入尾随段 (方向与相邻段相反,
    因为波浪点是极值点)。波浪点来自 wavecount/extract_wave_points, 时间序递增。
    无波浪点时返回 None (调用方回退为全局 OBV 式累计)。"""
    pts = [w for w in (waves or []) if len(w) >= 2 and w[0] is not None]
    pts = sorted(pts, key=lambda w: w[0])
    if not pts:
        return None
    if len(pts) == 1:
        return [(0, n - 1, 0)]
    segs = []
    for k in range(len(pts) - 1):
        a, b = int(pts[k][0]), int(pts[k + 1][0])
        if b <= a:
            continue
        direction = 1 if pts[k + 1][1] >= pts[k][1] else -1
        segs.append((a, b, direction))
    if not segs:
        return None
    a0, b0, d0 = segs[0]
    if a0 > 0:
        segs.insert(0, (0, a0, -d0))
    a1, b1, d1 = segs[-1]
    if b1 < n - 1:
        segs.append((b1, n - 1, -d1))
    return segs


def _wave_cum_volume(n, waves, up_mask, volume):
    """每波段累积成交量: 在每个波浪边界处重置为 0 后累加带符号量
    (涨柱为正, 跌柱为负)。返回 (cum, segs):
    cum 为 float[n], 无波浪点时回退为全局 OBV 式累计 (segs=None)。"""
    segs = _wave_segments(n, waves)
    signed = np.where(up_mask, volume, -volume).astype(float)
    if not segs:
        return np.cumsum(signed), None
    cum = np.full(n, np.nan)
    for a, b, _direction in segs:
        if b > a:
            cum[a:b + 1] = np.cumsum(signed[a:b + 1])
        else:
            cum[a] = signed[a]
    return cum, segs


def _build_locks(df, events, pivots, recent_n=W_RECENT):
    """买点锁序列: ①底信号(Spring/ST/SC) ②放量突破(SOS/JOC) ③回踩确认(LPS/BU)。

    返回 [(idx, price, level)]。与 plot_chart 内联逻辑完全一致, 供桌面端
    pyqtgraph K 线图复用, 避免两处漂移。"""
    locks = []
    lock_map = {1: {"Spring", "ST", "SC"}, 2: {"SOS", "JOC"}, 3: {"LPS", "BU"}}
    recent_ev = [e for e in events if e["idx"] >= len(df) - recent_n]
    recent_ev.sort(key=lambda e: e["idx"])
    for e in recent_ev:
        if e["type"] in lock_map[1]:
            locks = [(e["idx"], e["price"], 1)]
        elif locks and locks[-1][2] == 1 and e["type"] in lock_map[2]:
            locks.append((e["idx"], e["price"], 2))
        elif locks and locks[-1][2] == 2 and e["type"] in lock_map[3]:
            locks.append((e["idx"], e["price"], 3))
    # ③回踩确认兜底: 突破后 30 根内出现"回踩不破"的低枢轴 (LPS/BU 极少触发)
    if len(locks) == 2:
        b = locks[-1][0]
        before = [p for p in pivots if p["type"] == "low" and p["idx"] < b and p["idx"] >= b - 40]
        floor = min(p["price"] for p in before) if before else None
        if floor is not None:
            for p in pivots:
                if p["type"] == "low" and b < p["idx"] <= b + 30 and p["price"] > floor:
                    locks.append((p["idx"], p["price"], 3))
                    break
    return locks


def _dedup_events(events):
    """事件去重: 同一天同类事件只留一个, 相邻 2 根内同类跳过 (SC/AR, Spring/ST 扎堆)。"""
    out = []
    seen = set()
    last_type_by_idx = {}
    for e in events:
        key = (e["idx"], e["type"])
        if key in seen:
            continue
        seen.add(key)
        skip = False
        for other_idx, other_type in list(last_type_by_idx.items()):
            if other_type == e["type"] and abs(other_idx - e["idx"]) <= 2:
                skip = True
                break
        if skip:
            continue
        last_type_by_idx[e["idx"]] = e["type"]
        out.append(e)
    return out


def _event_layout(df, events):
    """事件标注布局: 去重 + 按所在K线上/下错开防重叠。

    返回 [(e, sign, dy)]: sign 为 1(标在上方)/-1(下方), dy 为标签相对事件
    价位的纵向偏移 (数据单位)。"""
    ymin, ymax = float(df["low"].min()), float(df["high"].max())
    mid = (ymin + ymax) / 2
    base_dy = (ymax - ymin) * 0.06
    used_x = defaultdict(list)
    out = []
    for e in _dedup_events(events):
        above = e["price"] >= mid
        sign = 1 if above else -1
        level = 0
        offsets = used_x[e["idx"]]
        while any(abs(o - level) < 0.5 for o in offsets):
            level += 1
        offsets.append(level)
        dy = base_dy * (1 + level * 0.9)
        out.append((e, sign, dy))
    return out


def kline_caption(df, events, sector=None):
    """K线图底部解读: 现价 · 均线排列 · 量能 · 近期事件 → 当前倾向。返回 (text, color)。"""
    _last = float(df["close"].iloc[-1])
    _ma20 = float(df["price_ma20"].iloc[-1]) if ("price_ma20" in df.columns
                                                 and np.isfinite(df["price_ma20"].iloc[-1])) else None
    _ma50 = float(df["price_ma50"].iloc[-1]) if ("price_ma50" in df.columns
                                                 and np.isfinite(df["price_ma50"].iloc[-1])) else None
    if _ma20 is not None and _ma50 is not None:
        if _last > _ma20 > _ma50:
            _ma_msg, _ma_col = "多头排列", _UP
        elif _last < _ma20 < _ma50:
            _ma_msg, _ma_col = "空头排列", _DN
        else:
            _ma_msg, _ma_col = "均线交织", "#d97706"
    else:
        _ma_msg, _ma_col = "均线数据不足", "#64748b"
    _vr = float(df["vol_ratio_20"].iloc[-1]) if ("vol_ratio_20" in df.columns and len(df)) else None
    if _vr is None:
        _vol_msg, _vol_col = "量能数据不足", "#64748b"
    elif _vr >= 1.5:
        _vol_msg, _vol_col = f"量比 {_vr:.2f} 明显放量", "#d97706"
    elif _vr >= 1.2:
        _vol_msg, _vol_col = f"量比 {_vr:.2f} 温和放量", "#d97706"
    elif _vr <= 0.8:
        _vol_msg, _vol_col = f"量比 {_vr:.2f} 明显缩量", "#64748b"
    else:
        _vol_msg, _vol_col = f"量比 {_vr:.2f} 量能平稳", "#64748b"
    _recent = [e for e in events if e["idx"] >= len(df) - 60]
    _bull = sorted({e["type"] for e in _recent if event_dir(e["type"]) > 0})
    _bear = sorted({e["type"] for e in _recent if event_dir(e["type"]) < 0})
    _ev_parts = []
    if _bull:
        _ev_parts.append("多头:" + "/".join(EVENT_CN.get(t, t) for t in _bull[-3:]))
    if _bear:
        _ev_parts.append("空头:" + "/".join(EVENT_CN.get(t, t) for t in _bear[-3:]))
    if _bull and not _bear:
        _ev_msg, _ev_col = " · ".join(_ev_parts) + " → 偏多", _UP
    elif _bear and not _bull:
        _ev_msg, _ev_col = " · ".join(_ev_parts) + " → 偏空", _DN
    elif _bull and _bear:
        _ev_msg, _ev_col = " · ".join(_ev_parts) + " → 多空拉锯", "#d97706"
    else:
        _ev_msg, _ev_col = "近期无高置信事件", "#64748b"
    _cap_col = _ev_col if _ev_col != "#64748b" else _ma_col
    _kline_cap = (f"现价{_last:.2f} · {_ma_msg} · {_vol_msg}\n"
                  f"{_ev_msg}")
    if sector and sector.get("name") and sector.get("main20") is not None:
        _kline_cap += (f" · 板块 {sector['name']} 20日主力 "
                       f"{sector['main20'] / 1e8:+.2f}亿")
    return _kline_cap, _cap_col


def build_kline_data(df, pivots, events, title, waves=None, draw_waves=True,
                     draw_locks=True, tr=None, profile=None, phase=None,
                     segs=None, sector=None, vsa_signals=None,
                     symbol=None, scale=240):
    """收集桌面端 pyqtgraph K 线图所需的全部绘制数据 (与 plot_chart 同口径)。

    在 worker 线程内调用, 返回值可跨线程交给 KlineWidget.set_data。
    symbol/scale 用于阶段带反馈标注的定位 (图表上显示 正确/错误 徽标)。"""
    if segs is None:
        segs = phase_segments(df, pivots, events)
    locks = _build_locks(df, events, pivots) if draw_locks else []
    ev_layout = _event_layout(df, events)
    up = df["close"].values >= df["open"].values
    cum, wave_segs = _wave_cum_volume(len(df), waves, up, df["volume"].values)
    return {
        "df": df,
        "title": title,
        "pivots": list(pivots or []),
        "events": ev_layout,
        "waves": [list(w) for w in (waves or [])],
        "draw_waves": bool(draw_waves),
        "locks": locks,
        "tr": tr,
        "profile": profile,
        "phase": phase,
        "segs": segs,
        "sector": sector,
        "vsa_signals": vsa_signals,
        "wave_cum": cum,
        "wave_segs": wave_segs,
        "up_mask": up,
        "caption": kline_caption(df, events, sector),
        "symbol": symbol,
        "scale": int(scale),
    }


def plot_chart(df, pivots, events, title, fig=None, waves=None, draw_locks=True,
               tr=None, profile=None, phase=None, segs=None, sector=None,
               vsa_signals=None, draw_waves=True):
    """绘制K线图。传入 fig 时复用(清空重绘), 否则新建, 避免 GUI 反复换 Figure 造成内存泄漏。
    sector: {"name", "main20"} 时在右上角绘制板块确认卡 (威科夫三击法·板块层)。
    vsa_signals: VSA 分类结果 (见 vsa_classify), 非空时在 K 线下方标注有操作
    意义的标签 (CHOC/UPT/TRU/TRD/DEM/SUP/ABS/TEST/ETR/ETF/BC/SV)。"""
    _deprecated_matplotlib()
    if fig is None:
        fig = Figure(figsize=(11, 7.5), dpi=100)
    else:
        fig.clear()
    gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 1, 0.7], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)
    axc = fig.add_subplot(gs[2], sharex=ax)
    # 三图共享 x 轴: 仅最下方子图显示日期刻度, 上方两图隐藏避免标签重叠
    ax.tick_params(labelbottom=False)
    axv.tick_params(labelbottom=False)

    # 威科夫阶段底色 (Markdown/吸筹/拉升/派发)
    if phase is not None:
        if segs is None:
            segs = phase_segments(df, pivots, events)
        ytop = df["high"].max()
        ybot = df["low"].min()
        for s0, s1, key, label in segs:
            color, alpha = _PHASE_STYLE[key][1], _PHASE_STYLE[key][2]
            ax.axvspan(s0, s1 + 1, color=color, alpha=alpha, zorder=0)
            ax.text(min(s0 + 3, s1), ytop - (ytop - ybot) * 0.015,
                    f"  {label}  ", fontsize=_fs(-2), fontweight="bold",
                    color=color, ha="left", va="top", zorder=3,
                    bbox=dict(facecolor="white", alpha=0.5, edgecolor="none",
                              boxstyle="round,pad=0.15"))

    x = np.arange(len(df))
    up = df["close"].values >= df["open"].values
    colors = np.where(up, _UP, _DN)
    w = 0.6
    ax.vlines(x, df["low"].values, df["high"].values, colors=colors, linewidth=0.8)
    body_low = np.minimum(df["open"].values, df["close"].values)
    body_h = np.abs(df["close"].values - df["open"].values)
    _fast_bars(ax, x, body_h, bottom=body_low, colors=colors, width=w,
               edgecolor=colors, linewidth=0.5)
    for col, color, label in _MA_LINES:
        if col in df.columns:
            ax.plot(x, df[col].values, color=color, lw=1, label=label)
    # 布林带 (20,2) 叠加
    if "boll_up" in df.columns and "boll_dn" in df.columns:
        ax.plot(x, df["boll_up"].values, color=_UP, lw=0.8, ls="--",
                alpha=0.55, label="BOLL上轨")
        ax.plot(x, df["boll_dn"].values, color=_DN, lw=0.8, ls="--",
                alpha=0.55, label="BOLL下轨")

    # 支撑阻力线
    for p in pivots[-4:]:
        if p["type"] == "low":
            ax.axhline(p["price"], color=_DN, ls="--", lw=0.7, alpha=0.5)
        else:
            ax.axhline(p["price"], color=_UP, ls="--", lw=0.7, alpha=0.5)

    # 交易区间 (TR) 上下轨 + Volume Profile POC
    x_end = len(df) - 1
    if tr:
        ax.axhline(tr["top"], color=_UP, ls="-.", lw=1.0, alpha=0.6)
        ax.axhline(tr["bottom"], color=_DN, ls="-.", lw=1.0, alpha=0.6)
        ax.text(x_end, tr["top"], f" TR上轨 {tr['top']:.2f}", color=_UP,
                fontsize=_fs(-2), ha="right", va="center")
        ax.text(x_end, tr["bottom"], f" TR下轨 {tr['bottom']:.2f}", color=_DN,
                fontsize=_fs(-2), ha="right", va="center")
    if profile:
        poc = profile["poc"]
        ax.axhline(poc, color="#d97706", ls=":", lw=1.2, alpha=0.8)
        ax.text(x_end, poc, f" POC {poc:.2f}", color="#d97706",
                fontsize=_fs(-2), ha="right", va="center")

    # 波浪理论: 在K线图上标注最近一段波浪结构 (上升浪/下跌浪 起止位置)
    wave_handles = []
    if not waves:
        waves = extract_wave_points(pivots)
    if draw_waves and len(waves) >= 2:
        ymin_w, ymax_w = df["low"].min(), df["high"].max()
        off = (ymax_w - ymin_w) * 0.04
        seen_cols = set()
        for i in range(len(waves) - 1):
            _w_i = waves[i]
            _w_j = waves[i + 1]
            # 兼容旧二元组 (idx,price) 与新增三元组 (idx,price,label)
            x0, y0 = (_w_i[0], _w_i[1]) if len(_w_i) >= 2 else (0, 0)
            x1, y1 = (_w_j[0], _w_j[1]) if len(_w_j) >= 2 else (0, 0)
            up = y1 >= y0
            col = _UP if up else _DN
            tag = "上升浪" if up else "下跌浪"
            ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                        arrowprops=dict(arrowstyle="-|>", color=col, lw=1.4,
                                        shrinkA=3, shrinkB=3), zorder=5)
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            yt = my + (off if up else -off)
            ax.text(mx, yt, f"{tag} {y0:.2f}→{y1:.2f}",
                    fontsize=_fs(-2), fontweight="bold", color=col,
                    ha="center", va="center", zorder=6)
            if col not in seen_cols:
                seen_cols.add(col)
                wave_handles.append(Line2D([0], [0], color=col, lw=1.4,
                                           marker="o", ms=4, label=tag))
        for i, wpt in enumerate(waves):
            wx, wy = (wpt[0], wpt[1]) if len(wpt) >= 2 else (0, 0)
            wlabel = wpt[2] if len(wpt) >= 3 else str(i + 1)
            ax.plot(wx, wy, "o", ms=5, color="#9c36b5", zorder=6)
            ax.annotate(str(wlabel), xy=(wx, wy), xytext=(5, 7),
                        textcoords="offset points", fontsize=_fs(-1),
                        fontweight="bold", color="#9c36b5", zorder=6)

    # 买点: 三重确认逐把上锁 (①底信号 ②放量突破 ③回踩确认)
    locks = _build_locks(df, events, pivots) if draw_locks else []
    # 事件标注 (去重 + 上下错开防重叠)
    ymin, ymax = float(df["low"].min()), float(df["high"].max())
    for e, sign, dy in _event_layout(df, events):
        col = e["color"]
        label = f"{e['type']}"
        t = ax.annotate(label, xy=(e["idx"], e["price"]),
                        xytext=(e["idx"], e["price"] + sign * dy),
                        ha="center", fontsize=_fs(-2), fontweight="bold", color=col,
                        arrowprops=dict(arrowstyle="-", color=col, lw=0.8))
        t.set_picker(True)  # 允许桌面端点击弹出事件解释
        t._ev_label = e["type"]  # 供 pick_event 回查标签

    # VSA K线标签 (整合 FibAlgo / VSA Advanced): 只标注有操作意义的信号,
    # 画在 K 线下方小字号, 与上方事件/锁标签错开。ND/NS/SV/EVR 等中性或
    # 高频信号不标注 (结论区文本仍有), 避免图上标签刷屏。
    if vsa_signals:
        vsa_draw = {"CHOC", "UPT", "TRU", "TRD", "DEM", "SUP", "ABS",
                    "TEST", "ETR", "ETF", "BC", "SV"}
        span = len(df)
        for s in vsa_signals:
            if s["label"] not in vsa_draw:
                continue
            ix = s["idx"]
            if ix >= span:
                continue
            col = s["color"]
            lab = s["label"]
            ly = float(df["low"].iloc[ix])
            y_off = (ymax - ymin) * 0.025
            t = ax.annotate(lab, xy=(ix, ly),
                            xytext=(ix, ly - y_off),
                            ha="center", fontsize=_fs(-4), fontweight="bold",
                            color=col, alpha=0.9,
                            arrowprops=dict(arrowstyle="-", color=col, lw=0.4,
                                            alpha=0.6))
            t.set_picker(True)  # 允许桌面端点击弹出信号解释
            t._ev_label = lab  # 供 pick_event 回查标签

    # 买点/卖点锁: 红带编号=买点, 绿=卖点; 放在事件之后, 用渲染器量出全部
    # 已有标签(事件/波浪方向/浪号)的包围盒, 逐个右移避让, 不与任何文字重叠
    if draw_locks:
        sell_types = {"UTAD", "BC"}
        lock_defs = [(lx, ly, str(lno), _UP, _UP_DK) for lx, ly, lno in locks]
        lock_defs += [(e["idx"], e["price"], "", _DN, _DN_DK)
                      for e in events if e["type"] in sell_types and e["idx"] >= len(df) - W_RECENT]
        lock_defs.sort(key=lambda d: d[0])
        from matplotlib.backends.backend_agg import RendererAgg
        _renderer = RendererAgg(
            int(fig.get_figwidth() * fig.dpi), int(fig.get_figheight() * fig.dpi), fig.dpi)
        fig.subplots_adjust(left=0.03, right=0.97, top=0.94, bottom=0.06)
        fig.draw(_renderer)  # 离屏定稿布局, 此后 transData 与最终渲染一致
        label_boxes = []
        for t in ax.texts:
            if not t.get_text().strip():
                continue
            b = t.get_window_extent(_renderer)
            if b.width >= 2 and b.height >= 2:
                label_boxes.append((b.x0, b.y0, b.x1, b.y1))

        def _icon_box(dx, dy, off):
            ix = dx + off * pt_px
            return (ix - 4, dy - 14, ix + 22, dy + 14)

        def _hit(box, others):
            return any(not (box[2] < o[0] or o[2] < box[0] or box[3] < o[1] or o[3] < box[1])
                       for o in others)

        pt_px = fig.dpi / 72.0
        right_edge = (ax.get_position().x0 + ax.get_position().width) \
            * fig.get_figwidth() * fig.dpi
        ymin_l, ymax_l = df["low"].min(), df["high"].max()
        mid_l = (ymin_l + ymax_l) / 2
        base_dy_l = (ymax_l - ymin_l) * 0.06
        placed = []
        lock3 = None
        for lx, ly, label, color, dark in lock_defs:
            sign = 1 if ly >= mid_l else -1
            ly2 = ly - sign * base_dy_l * 0.7
            dx, dy = ax.transData.transform((lx, ly2))
            off = 6
            lock_clear = None
            for cand in (6, 32, 58, -6, -32, -58):
                box = _icon_box(dx, dy, cand)
                if cand > 0 and box[2] > right_edge:
                    continue
                if not _hit(box, placed):
                    if not _hit(box, label_boxes):
                        off = cand
                        break
                    if lock_clear is None:
                        lock_clear = cand
            else:
                if lock_clear is not None:
                    off = lock_clear
            placed.append(_icon_box(dx, dy, off))
            draw_lock(ax, lx, ly2, label=label, size=18, offset_pt=off, color=color, dark=dark)
            if label == "3":
                lock3 = (lx, ly2, off)
        if lock3:
            lx, ly, off = lock3
            ax.annotate("✓ 买点", xy=(lx, ly), xytext=(off + 26, 2),
                        textcoords="offset points", fontsize=_fs(-1),
                        fontweight="bold", color=_UP, zorder=6)

    # 成交量 (含 MA5/10 量均线)
    _fast_bars(axv, x, df["volume"].values / 1e4, colors=colors, width=w)
    for col, color, label in (("vol_ma5", "#f783ac", "量MA5"),
                              ("vol_ma10", "#12b886", "量MA10"),
                              ("vol_ma20", "#1971c2", "量MA20")):
        if col in df.columns:
            axv.plot(x, df[col].values / 1e4, color=color, lw=0.8, label=label)
    # 最新量比标注 (放量程度: >1 放量, <1 缩量)
    if "vol_ratio_20" in df.columns and len(df):
        _vr = float(df["vol_ratio_20"].iloc[-1])
        _vc = _UP if _vr >= 1.2 else _DN if _vr <= 0.8 else "#adb5bd"
        axv.text(0.995, 0.95, f"量比 {_vr:.2f}", transform=axv.transAxes,
                 ha="right", va="top", fontsize=_fs(-1), fontweight="bold", color=_vc,
                 bbox=dict(facecolor="white", alpha=0.85, edgecolor="none",
                           boxstyle="round,pad=0.3"))

    # 波段累积成交量 (维斯波·每波段重置累计, 与波浪边界对齐, 可跨波段比较量能)
    cum, wave_segs = _wave_cum_volume(len(df), waves, up, df["volume"].values)
    if wave_segs is not None:
        _cmax = float(np.nanmax(np.abs(cum)))
        scale = _cmax if _cmax > 0 else 1.0
        cum_norm = cum / scale * 100
        first_seg = wave_segs[0][0]
        for a, b, direction in wave_segs:
            xs = x[a:b + 1]
            ys = cum_norm[a:b + 1]
            wcol = _UP if direction > 0 else _DN if direction < 0 else "#8a94a6"
            axc.plot(xs, ys, color=wcol, lw=1.0,
                     label="波段累计量" if a == first_seg else None)
            axc.fill_between(xs, 0, ys, color=wcol, alpha=0.22)
            tot = float(df["volume"].iloc[a:b + 1].sum()) / 1e4
            arrow = "↑" if direction > 0 else "↓" if direction < 0 else ""
            peak = float(np.nanmax(np.abs(ys)))
            y_txt = peak + 6 if direction >= 0 else -(peak + 6)
            axc.text((a + b) / 2, y_txt, f"{arrow}{tot:.0f}万手",
                     ha="center", va="center", fontsize=_fs(-3),
                     fontweight="bold", color=wcol)
        for (a, _b, _d) in wave_segs[1:]:
            axv.axvline(a, color="#adb5bd", ls=":", lw=0.7, alpha=0.5)
            axc.axvline(a, color="#adb5bd", ls=":", lw=0.7, alpha=0.5)
        axc.set_ylim(-110, 110)
        axc.set_ylabel("波段累计量", fontsize=_fs(-2))
    else:
        # 无波浪点回退: 全局 OBV 式累计
        cum_norm = cum / cum.max() * 100 if cum.max() != 0 else cum
        axc.fill_between(x, 0, cum_norm, color="#8a94a6", alpha=0.25)
        axc.plot(x, cum_norm, color="#495057", lw=0.9, label="累积成交量")
        axc.set_ylabel("累计量", fontsize=_fs(-2))

    # X轴日期 (分钟级显示 月-日 时:分)
    step = max(1, len(df) // 10)
    tick_idx = x[::step]
    is_minute = df["day"].dt.hour.nunique() > 1
    if is_minute:
        tick_lab = [df["day"].iloc[i].strftime("%m-%d %H:%M") for i in tick_idx]
    else:
        tick_lab = [df["day"].iloc[i].strftime("%y-%m-%d") for i in tick_idx]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(tick_lab, fontsize=_fs(-2))

    ax.set_title(title, fontsize=_fs(3))
    # 板块确认卡 (右上角; 威科夫三击法·板块层)
    if sector and sector.get("name") and sector.get("main20") is not None:
        s20 = sector["main20"] / 1e8
        sc = _UP if s20 >= 0 else _DN
        ax.text(0.99, 0.985, f"板块 {sector['name']} · 近20日主力 {s20:+.2f}亿",
                transform=ax.transAxes, fontsize=_fs(-1), fontweight="bold", color=sc,
                ha="right", va="top", zorder=5,
                bbox=dict(facecolor="white", alpha=0.9, edgecolor=sc, lw=0.9,
                          boxstyle="round,pad=0.35"))
    _h, _l = ax.get_legend_handles_labels()
    ax.legend(handles=_h + wave_handles, loc="upper left", fontsize=_fs(-2), ncol=3)
    ax.grid(alpha=0.35, lw=0.5, color=C_GRID)
    axv.grid(alpha=0.35, lw=0.5, color=C_GRID)
    axv.set_ylabel("量(万手)", fontsize=_fs(-2))
    axc.grid(alpha=0.35, lw=0.5, color=C_GRID)
    axc.legend(loc="upper left", fontsize=_fs(-2))

    # ── K线解读: 均线/量能/近期事件 → 当前倾向 (与技术指标一致的"信号→预示"风格) ──
    _kline_cap, _cap_col = kline_caption(df, events, sector)
    axc.set_xlabel(_kline_cap, fontsize=_fs(1), color=_cap_col, fontweight="bold")
    fig.subplots_adjust(left=0.03, right=0.97, top=0.94, bottom=0.13)
    return fig


def _confirm_banner(mkt):
    """确认机制横幅文本: 阶段置信 + 估值 + 20日主力/5日超大单。无数据返回 None。"""
    q = mkt.get("conf_q")
    if not q and not mkt.get("fund") and not (mkt.get("flow") is not None):
        return None
    parts = []
    if q:
        label = {"high": "高置信", "caution": "需谨慎"}.get(q, "")
        parts.append(f"阶段确认: {label}")
    fund = mkt.get("fund") or {}
    if fund.get("pe_ttm") and fund["pe_ttm"] > 0:
        parts.append(f"PE {fund['pe_ttm']:.1f} / PB {fund.get('pb') or 0:.2f}")
    flow = mkt.get("flow")
    if flow is not None and len(flow):
        m20 = float(flow.tail(20)["main"].sum()) / 1e8
        s5 = float(flow.tail(5)["super"].sum()) / 1e8
        parts.append(f"20日主力 {m20:+.1f}亿 · 5日超大单 {s5:+.1f}亿")
    sector = mkt.get("sector")
    if sector and sector.get("name") and sector.get("main20") is not None:
        parts.append(f"板块 {sector['name']} {sector['main20'] / 1e8:+.1f}亿")
    return "  |  ".join(parts) or None


def plot_market(market, fig):
    """资金透视 2×2 面板: 主力资金流向 / 资金分项 / 当前筹码堆积形态 / 股东户数。
    供需强度与估值卡片合并进底部总结。"""
    _deprecated_matplotlib()
    import pandas as _pd
    fig.clear()
    fig.set_layout_engine(None)
    mkt = market or {}
    main_flow = mkt.get("main_flow_series") or []
    flow_series = mkt.get("flow_series") or []
    chips_series = mkt.get("chips_series") or []
    holder_series = mkt.get("holder_series") or []
    # 防御性过滤: 股东户数失真记录 (IPO 假性暴增等, 见 holder_ratio_ok) 若进入
    # 图表会造成荒谬环比/误导信号, 统一在此剔除, 与 fundamental/market 口径一致。
    holder_series = [s for s in holder_series if holder_ratio_ok(s)]
    fund = mkt.get("fund") or {}
    flow_source = main_flow or flow_series
    chip_dist = mkt.get("chip_dist")
    panels = sum(bool(s) for s in (flow_source, holder_series, chip_dist))

    banner = _confirm_banner(mkt)

    if panels == 0:
        ax = fig.add_subplot(111)
        if banner:
            ax.set_title(banner, fontsize=_fs(4), color="#374151")
        ax.text(0.5, 0.5, "数据不足\n请确保已开启确认机制并分析日线股票",
                ha="center", va="center", fontsize=_fs(3), color="#9ca3af")
        ax.axis("off")
        return fig

    # ── 整体布局: 顶部(标题+指标卡,紧凑) + 下方面板(2×2+供需+总结,行距宽) ──
    gs_top = fig.add_gridspec(2, 1, height_ratios=[0.32, 0.68], hspace=0.25,
                              left=0.03, right=0.97, top=0.99, bottom=0.945)
    gs = fig.add_gridspec(4, 4, height_ratios=[0.34, 0.34, 0.20, 0.12],
                          hspace=0.72, wspace=0.18,
                          left=0.03, right=0.97, top=0.925, bottom=0.03)

    # ═══ 顶部: 页面标题 ═══
    ax_title = fig.add_subplot(gs_top[0, 0])
    ax_title.axis("off")
    title_parts = []
    fund_name = fund.get("name", "") if fund else ""
    if fund_name:
        title_parts.append(fund_name)
    title_parts.append("资金透视")
    title_text = "  ——  ".join(title_parts)
    ax_title.text(0.5, 0.4, title_text, ha="center", va="center",
                  fontsize=_fs(5), color="#1f2937", fontweight="bold",
                  transform=ax_title.transAxes)

    # ═══ 顶部: 估值指标卡 ═══
    ax_top = fig.add_subplot(gs_top[1, 0])
    ax_top.axis("off")
    header_parts = []
    if banner:
        header_parts.append(banner)
    if fund:
        pe = fund.get("pe_ttm", 0)
        pb = fund.get("pb", 0)
        mcap = fund.get("mcap_yi", 0)
        turnover = fund.get("turnover", 0)
        eps = fund.get("eps", 0)
        growth = fund.get("net_growth", 0)
        header_parts.append(f"PE {pe:.1f}")
        header_parts.append(f"PB {pb:.2f}" if pb else "")
        if mcap:
            if mcap >= 1e4:
                header_parts.append(f"市值 {mcap/1e4:.0f}万亿")
            else:
                header_parts.append(f"市值 {mcap:.0f}亿")
        if turnover:
            header_parts.append(f"换手率 {turnover:.2f}%")
        if eps:
            header_parts.append(f"EPS {eps:.2f}")
        if growth:
            g_sign = "+" if growth > 0 else ""
            header_parts.append(f"净利 {g_sign}{growth:.1f}%")
    header_text = "  |  ".join(p for p in header_parts if p)
    if header_text:
        ax_top.text(0.5, 0.5, header_text, ha="center", va="center",
                     fontsize=_fs(0), color="#1f2937", fontweight="bold",
                     transform=ax_top.transAxes)
        ax_top.set_facecolor("#f8fafc")

    # ═══ 2×2 面板 ═══
    # 左上: 主力资金流向 (含 5/20 日均线 + 累计净流入 + 流入占比)
    ax1 = fig.add_subplot(gs[0, :2])
    if flow_source:
        days = [s["day"] for s in flow_source]
        if main_flow:
            vals = [s["main"] / 1e8 for s in flow_source]
            src_tag = "东财真实数据"
        else:
            vals = [s["flow"] / 1e8 for s in flow_source]
            src_tag = "日K估算"
        colors = [_UP if v >= 0 else _DN for v in vals]
        ax1.bar(days, vals, color=colors, width=0.72, alpha=0.85, zorder=2)
        v_arr = np.array(vals, dtype=float)
        for n, color, lab in ((5, "#f08c00", "5日均线"), (20, "#1971c2", "20日均线")):
            if len(v_arr) >= n:
                ma = np.full(len(v_arr), np.nan)
                for i in range(n - 1, len(v_arr)):
                    ma[i] = v_arr[i - n + 1:i + 1].mean()
                ax1.plot(days, ma, color=color, lw=1.6, label=lab, zorder=3)
        cum = np.cumsum(vals)
        ax1.plot(days, cum, color="#374151", lw=1.8, ls="-.", label="累计净流入", zorder=4)
        ax1.fill_between(days, 0, cum, color="#8a94a6", alpha=0.08)
        ax1.axhline(0, color="#9ca3af", lw=0.8, zorder=1)
        m20 = float(sum(vals[-20:])) if len(vals) >= 20 else float(sum(vals))
        m5 = float(sum(vals[-5:])) if len(vals) >= 5 else 0
        ratio_txt = ""
        if main_flow:
            last = flow_source[-1]
            super_v = float(last.get("super") or 0)
            large_v = float(last.get("large") or 0)
            mid_v = float(last.get("mid") or 0)
            small_v = float(last.get("small") or 0)
            tot = abs(super_v) + abs(large_v) + abs(mid_v) + abs(small_v)
            if tot > 0:
                main_ratio = (super_v + large_v) / tot * 100
                ratio_txt = f" · 主力占比 {main_ratio:+.0f}%"
        ax1.set_title(f"主力资金流向 ({src_tag})  20日 {m20:+.1f}亿 · 5日 {m5:+.1f}亿"
                      f"{ratio_txt}", fontsize=_fs(1), color="#374151", fontweight="bold")
        ax1.legend(fontsize=_fs(-2), loc="upper left", ncol=3,
                    framealpha=0.5, edgecolor="#e5e7eb")
        ax1.grid(alpha=0.35, lw=0.5, color=C_GRID, zorder=0)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        for lbl in ax1.get_xticklabels():
            lbl.set_fontsize(_fs(-2))
        ax1.tick_params(axis="y", labelsize=_fs(-2))
        ax1.set_xmargin(0.01)
    else:
        ax1.axis("off")

    # 右上: 资金分项 (超大单/大单/中单/小单)
    ax1b = fig.add_subplot(gs[0, 2:])
    if main_flow:
        days = [s["day"] for s in main_flow]
        n = len(days)
        super_v = np.array([s["super"] / 1e8 for s in main_flow])
        large_v = np.array([s["large"] / 1e8 for s in main_flow])
        mid_v = np.array([s["mid"] / 1e8 for s in main_flow])
        small_v = np.array([s["small"] / 1e8 for s in main_flow])
        x = np.arange(n)
        wd2 = 0.62
        ax1b.bar(x - 1.5 * wd2 / 4, super_v, width=wd2 / 4, color="#e03131",
                 alpha=0.9, label="超大单", zorder=2)
        ax1b.bar(x - 0.5 * wd2 / 4, large_v, width=wd2 / 4, color="#f08c00",
                 alpha=0.9, label="大单", zorder=2)
        ax1b.bar(x + 0.5 * wd2 / 4, mid_v, width=wd2 / 4, color="#1971c2",
                 alpha=0.9, label="中单", zorder=2)
        ax1b.bar(x + 1.5 * wd2 / 4, small_v, width=wd2 / 4, color="#94a3b8",
                 alpha=0.9, label="小单", zorder=2)
        ax1b.axhline(0, color="#9ca3af", lw=0.8, zorder=1)
        s20 = float(sum(super_v[-20:]))
        l20 = float(sum(large_v[-20:]))
        m20b = float(sum(mid_v[-20:]))
        sm20 = float(sum(small_v[-20:]))
        ax1b.set_title(f"资金分项 (亿)  20日 超大 {s20:+.1f} · 大单 {l20:+.1f} · "
                       f"中单 {m20b:+.1f} · 小单 {sm20:+.1f}",
                       fontsize=_fs(1), color="#374151", fontweight="bold")
        ax1b.legend(fontsize=_fs(-2), loc="upper left", ncol=2,
                    framealpha=0.5, edgecolor="#e5e7eb")
        ax1b.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="y", zorder=0)
        step = max(1, n // 8)
        ax1b.set_xticks(x[::step])
        ax1b.set_xticklabels([d.strftime("%m-%d") for d in days[::step]],
                             fontsize=_fs(-2))
        ax1b.tick_params(axis="y", labelsize=_fs(-2))
    elif flow_series:
        # 东财分项不可用: 用日K估算的资金流 + 量能 替代 (保证 2×2 四格不空)
        days = [s["day"] for s in flow_series]
        vals = [s["flow"] / 1e8 for s in flow_series]
        colors = [_UP if v >= 0 else _DN for v in vals]
        ax1b.bar(days, vals, color=colors, width=0.72, alpha=0.85, zorder=2)
        ax1b.axhline(0, color="#9ca3af", lw=0.8, zorder=1)
        ax1b_twin = ax1b.twinx()
        vol = [v / 1e8 for v in (mkt.get("vol_series") or [])]
        if len(vol) == len(days):
            ax1b_twin.fill_between(days, 0, vol, color="#94a3b8", alpha=0.25)
        ax1b_twin.set_ylabel("量能(亿手)", fontsize=_fs(-1), color="#94a3b8")
        ax1b_twin.tick_params(axis="y", labelsize=_fs(-2), colors="#94a3b8")
        m20 = float(sum(vals[-20:])) if len(vals) >= 20 else float(sum(vals))
        ax1b.set_title(f"资金流估算 (亿)  近20日 {m20:+.1f}亿  ·  东财分项暂不可用",
                       fontsize=_fs(1), color="#374151", fontweight="bold")
        ax1b.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="y", zorder=0)
        ax1b.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        for lbl in ax1b.get_xticklabels():
            lbl.set_fontsize(_fs(-2))
        ax1b.tick_params(axis="y", labelsize=_fs(-2))
        ax1b.set_xmargin(0.01)
    else:
        ax1b.axis("off")

    # 左下: 当前筹码堆积形态 (横向柱状)
    ax3 = fig.add_subplot(gs[1, :2])
    if chip_dist:
        prices = chip_dist["prices"]
        weights = chip_dist["weights"]
        cur = chip_dist["cur"]
        poc = chip_dist["poc"]
        below = chip_dist["below"]
        colors = [_UP if p >= cur else _DN for p in prices]
        ax3.barh(prices, weights, height=(prices[1] - prices[0]) * 0.85,
                 color=colors, alpha=0.85, zorder=2)
        ax3.axhline(cur, color="#1f2937", lw=1.4, ls=":", zorder=3)
        ax3.text(0.99, 0.02, f"现价 {cur:.2f}", transform=ax3.transAxes,
                 ha="right", va="bottom", fontsize=_fs(-1), color="#1f2937",
                 fontweight="bold", zorder=4)
        ax3.text(0.01, 0.98, f"POC {poc:.2f}", transform=ax3.transAxes,
                 ha="left", va="top", fontsize=_fs(-1), color="#d97706",
                 fontweight="bold", zorder=4)
        if below >= 0.6:
            shape_txt = f"现价上方堆积 {below*100:.0f}% → 上方套牢重, 抛压大"
            shape_col = C_DOWN
        elif below >= 0.35:
            shape_txt = f"现价下方筹码 {below*100:.0f}% → 下方支撑较扎实"
            shape_col = C_UP
        else:
            shape_txt = f"现价下方筹码仅 {below*100:.0f}% → 获利盘薄, 追高风险"
            shape_col = THEME["amber"]
        ax3.set_title(f"当前筹码堆积形态 · 现价下方筹码 {below*100:.0f}%",
                      fontsize=_fs(1), color="#374151", fontweight="bold")
        ax3.set_xlabel(shape_txt, fontsize=_fs(0), color=shape_col, fontweight="bold")
        ax3.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="x", zorder=0)
        ax3.set_yticks([])
        ax3.tick_params(axis="x", labelsize=_fs(-2))
    else:
        ax3.axis("off")

    # 右下: 股东户数变化
    ax4 = fig.add_subplot(gs[1, 2:])
    if holder_series:
        days = [_pd.Timestamp(s["end_date"]) for s in holder_series]
        nums = [float(s["holder_num"]) / 1e4 for s in holder_series]
        ratios = [(s["ratio"] or 0) for s in holder_series]
        cols = [_UP if r > 0 else _DN for r in ratios]
        ax4.bar(days, nums, color=cols, width=55, alpha=0.82, zorder=2)
        ax4.plot(days, nums, color="#374151", lw=1.5, marker="D", ms=4,
                  zorder=3)
        for d, n, r in zip(days, nums, ratios):
            tag = "↑" if r > 0 else "↓" if r < 0 else ""
            ax4.annotate(f"{n:.0f}万{tag}", (d, n),
                          textcoords="offset points", xytext=(0, 6),
                          ha="center", fontsize=_fs(-3), color="#374151",
                          fontweight="bold")
        last_ratio = ratios[-1] if ratios else 0
        trend = "筹码分散" if last_ratio > 0 else "筹码集中" if last_ratio < 0 else "平稳"
        tc4 = _DN if last_ratio > 0 else _UP if last_ratio < 0 else "#64748b"
        ax4.set_title(f"股东户数变化 (万户)  ·  最新 {last_ratio:+.1f}% ({trend})",
                      fontsize=_fs(1), color="#374151", fontweight="bold")
        ax4.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="y", zorder=0)
        ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        for lbl in ax4.get_xticklabels():
            lbl.set_fontsize(_fs(-2))
        ax4.tick_params(axis="y", labelsize=_fs(-2))
    else:
        ax4.axis("off")

    # ═══ 第4行: 供需强度 (全宽, 柱状图) ═══
    sd_series = mkt.get("sd_series") or []
    ax_sd = fig.add_subplot(gs[2, :])
    if sd_series:
        days = [s["day"] for s in sd_series]
        dem = [s["demand"] / 1e6 for s in sd_series]
        sup = [s["supply"] / 1e6 for s in sd_series]
        n = len(days)
        xp = np.arange(n)
        wd2 = 0.36
        ax_sd.bar(xp - wd2 / 2, dem, width=wd2, color=_UP, alpha=0.85,
                  label="需求", zorder=2)
        ax_sd.bar(xp + wd2 / 2, sup, width=wd2, color=_DN, alpha=0.85,
                  label="供给", zorder=2)
        total_dem = sum(dem)
        total_sup = sum(sup)
        ratio = total_dem / total_sup if total_sup > 0 else 1
        if ratio >= SD_BULL:
            tone, tc = "需求占优 · 买方积极", _UP
        elif ratio <= SD_BEAR:
            tone, tc = "供给占优 · 卖方主导", _DN
        else:
            tone, tc = "多空均衡", "#d97706"
        ax_sd.set_title(f"供需强度 (万手)  ·  供需比 {ratio:.2f}  {tone}",
                        fontsize=_fs(1), color="#374151", fontweight="bold")
        ax_sd.legend(fontsize=_fs(-2), loc="upper left", framealpha=0.5,
                     edgecolor="#e5e7eb")
        ax_sd.grid(alpha=0.35, lw=0.5, color=C_GRID, axis="y", zorder=0)
        step = max(1, n // 8)
        ax_sd.set_xticks(xp[::step])
        ax_sd.set_xticklabels([d.strftime("%m-%d") for d in days[::step]],
                              fontsize=_fs(-2))
        ax_sd.tick_params(axis="y", labelsize=_fs(-2))
        ax_sd.set_xmargin(0.01)
    else:
        ax_sd.axis("off")

    # ═══ 底部综合分析 ═══
    caps = []
    flow_src = main_flow or flow_series
    if flow_src:
        m20 = float(sum(s.get("main", s.get("flow", 0)) for s in flow_src[-20:])) / 1e8
        fc = _UP if m20 > 0.5 else _DN if m20 < -0.5 else "#64748b"
        caps.append((f"近20日主力 {m20:+.2f}亿", fc))
    if chips_series:
        last_chip = chips_series[-1]
        conc_val = last_chip.get("conc")
        profit_val = last_chip.get("profit")
        if conc_val is not None:
            ct = "集中" if conc_val <= 20 else "分散" if conc_val >= 40 else "中性"
            cc = _UP if conc_val <= 20 else _DN if conc_val >= 40 else "#d97706"
            caps.append((f"90%成本集中度 {conc_val:.1f}%({ct})", cc))
        if profit_val is not None:
            pc = _UP if profit_val >= 70 else _DN if profit_val <= 30 else "#64748b"
            caps.append((f"获利盘 {profit_val:.0f}%", pc))
    if chip_dist:
        below_pct = chip_dist["below"] * 100
        if below_pct >= 60:
            cc2, ct2 = _DN, "上方套牢重"
        elif below_pct >= 35:
            cc2, ct2 = _UP, "下方支撑扎实"
        else:
            cc2, ct2 = THEME["amber"], "获利盘薄"
        caps.append((f"现价下方筹码 {below_pct:.0f}%({ct2})", cc2))
    if holder_series:
        last_h = holder_series[-1]
        hr = last_h.get("ratio") or 0
        hc = _DN if hr > 0 else _UP if hr < 0 else "#64748b"
        ht = "分散" if hr > 0 else "集中" if hr < 0 else "平稳"
        caps.append((f"股东户数 {hr:+.1f}%({ht})", hc))
    if caps:
        cap_text = "  ·  ".join(t for t, _c in caps)
        greens = sum(1 for _t, c in caps if c == _DN)
        reds = sum(1 for _t, c in caps if c == _UP)
        cap_color = _DN if greens and not reds else _UP if reds and not greens else "#d97706"
        ax_cap = fig.add_subplot(gs[3, :])
        ax_cap.axis("off")
        ax_cap.text(0.5, 0.80, cap_text, ha="center", va="center",
                     fontsize=_fs(1), color=cap_color, fontweight="bold",
                     transform=ax_cap.transAxes)
        # 第二行: 解读提示
        insights = []
        if flow_src:
            m20_v = float(sum(s.get("main", s.get("flow", 0)) for s in flow_src[-20:])) / 1e8
            if m20_v > 1:
                insights.append(("主力持续流入", _UP))
            elif m20_v < -1:
                insights.append(("主力持续流出", _DN))
        if main_flow and len(main_flow) >= 5:
            sup5 = float(sum(s["super"] for s in main_flow[-5:]))
            sm5 = float(sum(s["small"] for s in main_flow[-5:]))
            if sup5 > 0 and sm5 < 0:
                insights.append(("超大单吸筹 · 散户离场", _UP))
            elif sup5 < 0 and sm5 > 0:
                insights.append(("超大单派发 · 散户接盘", _DN))
        if chips_series and chips_series[-1].get("conc", 999) <= 20:
            insights.append(("筹码高度集中 · 主力控盘", _UP))
        elif chips_series and chips_series[-1].get("conc", 0) >= 40:
            insights.append(("筹码分散 · 散户主导", _DN))
        if chip_dist:
            if chip_dist["below"] >= 0.6:
                insights.append(("上方套牢盘沉重 · 反弹抛压大", _DN))
            elif chip_dist["below"] >= 0.35:
                insights.append(("下方筹码扎实 · 支撑可靠", _UP))
            else:
                insights.append(("获利盘薄 · 追高需谨慎", THEME["amber"]))
        if holder_series:
            hr2 = (holder_series[-1].get("ratio") or 0)
            if hr2 < -10:
                insights.append(("股东数骤降 · 吸筹迹象", _UP))
            elif hr2 > 10:
                insights.append(("股东数激增 · 派发迹象", _DN))
        if sd_series:
            dt = sum(s["demand"] for s in sd_series)
            st = sum(s["supply"] for s in sd_series)
            if st > 0 and dt / st > 1.2:
                insights.append(("买方主导 · 需求强劲", _UP))
            elif st > 0 and dt / st < 0.8:
                insights.append(("卖方主导 · 供给压力", _DN))
        if insights:
            in_text = "  |  ".join(t for t, _c in insights)
            ax_cap.text(0.5, 0.40, in_text, ha="center", va="center",
                         fontsize=_fs(0), color="#64748b",
                         transform=ax_cap.transAxes)
    else:
        ax_cap = fig.add_subplot(gs[3, :])
        ax_cap.axis("off")

    return fig


def build_market_data(market):
    """收集桌面端 pyqtgraph 资金透视所需绘制数据 (与 plot_market 同口径)。

    在 worker 线程内调用, 返回纯 JSON 化 dict, 可跨线程交给 MktWidget.set_data。
    文本/标题/解读均在 worker 侧算好, 渲染端只画图。market 里含 DataFrame
    (market['flow']), 不能直接跨线程, 故在此转为纯数据。
    """
    mkt = market or {}
    out = {}
    main_flow = mkt.get("main_flow_series") or []
    flow_series = mkt.get("flow_series") or []
    holder_series = [s for s in (mkt.get("holder_series") or [])
                     if holder_ratio_ok(s)]
    fund = mkt.get("fund") or {}
    chip_dist = mkt.get("chip_dist")
    sd_series = mkt.get("sd_series") or []
    chips_series = mkt.get("chips_series") or []
    flow_source = main_flow or flow_series

    def _day(v):
        try:
            return str(v.date())
        except Exception:
            return str(v)

    # ── 顶部: 标题 + 估值卡 ──
    title_parts = []
    if fund.get("name"):
        title_parts.append(str(fund["name"]))
    title_parts.append("资金透视")
    out["title"] = "  ——  ".join(title_parts)
    banner = _confirm_banner(mkt)
    header_parts = []
    if banner:
        header_parts.append(banner)
    if fund:
        pe = fund.get("pe_ttm") or 0
        pb = fund.get("pb") or 0
        mcap = fund.get("mcap_yi") or 0
        turnover = fund.get("turnover") or 0
        eps = fund.get("eps") or 0
        growth = fund.get("net_growth") or 0
        if pe:
            header_parts.append(f"PE {pe:.1f}")
        if pb:
            header_parts.append(f"PB {pb:.2f}")
        if mcap:
            header_parts.append(f"市值 {mcap/1e4:.0f}万亿" if mcap >= 1e4
                                else f"市值 {mcap:.0f}亿")
        if turnover:
            header_parts.append(f"换手率 {turnover:.2f}%")
        if eps:
            header_parts.append(f"EPS {eps:.2f}")
        if growth:
            header_parts.append(f"净利 {growth:+.1f}%")
    out["header"] = "  |  ".join(p for p in header_parts if p) or None

    # ── 左上: 主力资金流向 ──
    if flow_source:
        days = [_day(s["day"]) for s in flow_source]
        if main_flow:
            vals = [float(s["main"]) / 1e8 for s in flow_source]
            src = "东财真实数据"
        else:
            vals = [float(s["flow"]) / 1e8 for s in flow_source]
            src = "日K估算"
        v_arr = np.array(vals, dtype=float)
        ma5 = None
        ma20 = None
        if len(v_arr) >= 5:
            ma5 = [float(np.mean(v_arr[max(0, i - 4):i + 1]))
                   for i in range(len(v_arr))]
        if len(v_arr) >= 20:
            ma20 = [float(np.mean(v_arr[max(0, i - 19):i + 1]))
                    for i in range(len(v_arr))]
        cum = np.cumsum(vals).tolist()
        m20 = float(sum(vals[-20:])) if len(vals) >= 20 else float(sum(vals))
        m5 = float(sum(vals[-5:])) if len(vals) >= 5 else 0
        ratio_txt = ""
        if main_flow:
            last = flow_source[-1]
            super_v = float(last.get("super") or 0)
            large_v = float(last.get("large") or 0)
            mid_v = float(last.get("mid") or 0)
            small_v = float(last.get("small") or 0)
            tot = abs(super_v) + abs(large_v) + abs(mid_v) + abs(small_v)
            if tot > 0:
                ratio_txt = f" · 主力占比 {(super_v + large_v) / tot * 100:+.0f}%"
        out["main_flow"] = {
            "days": days, "vals": vals, "cum": cum,
            "ma5": ma5, "ma20": ma20, "src": src,
            "title": f"主力资金流向 ({src})  20日 {m20:+.1f}亿 · 5日 {m5:+.1f}亿"
                     f"{ratio_txt}",
        }
    else:
        out["main_flow"] = None

    # ── 右上: 资金分项 (或日K估算+量能) ──
    if main_flow:
        days = [_day(s["day"]) for s in main_flow]
        n = len(days)
        sub = {"days": days,
               "super": [float(s["super"]) / 1e8 for s in main_flow],
               "large": [float(s["large"]) / 1e8 for s in main_flow],
               "mid": [float(s["mid"]) / 1e8 for s in main_flow],
               "small": [float(s["small"]) / 1e8 for s in main_flow]}
        s20 = float(sum(sub["super"][-20:]))
        l20 = float(sum(sub["large"][-20:]))
        m20b = float(sum(sub["mid"][-20:]))
        sm20 = float(sum(sub["small"][-20:]))
        sub["title"] = (f"资金分项 (亿)  20日 超大 {s20:+.1f} · 大单 {l20:+.1f} · "
                        f"中单 {m20b:+.1f} · 小单 {sm20:+.1f}")
        out["sub_flow"] = sub
    elif flow_series:
        days = [_day(s["day"]) for s in flow_series]
        vals = [float(s["flow"]) / 1e8 for s in flow_series]
        vol = [float(v) / 1e8 for v in (mkt.get("vol_series") or [])]
        m20 = float(sum(vals[-20:])) if len(vals) >= 20 else float(sum(vals))
        out["sub_flow"] = {
            "days": days, "vals": vals, "vol": vol,
            "title": f"资金流估算 (亿)  近20日 {m20:+.1f}亿  ·  东财分项暂不可用",
        }
    else:
        out["sub_flow"] = None

    # ── 左下: 当前筹码堆积形态 ──
    if chip_dist:
        prices = [float(p) for p in chip_dist["prices"]]
        weights = [float(w) for w in chip_dist["weights"]]
        cur = float(chip_dist["cur"])
        poc = float(chip_dist["poc"])
        below = float(chip_dist["below"])
        if below >= 0.6:
            shape_txt, shape_col = (f"现价上方堆积 {below*100:.0f}% → "
                                    f"上方套牢重, 抛压大"), _DN
        elif below >= 0.35:
            shape_txt, shape_col = (f"现价下方筹码 {below*100:.0f}% → "
                                    f"下方支撑较扎实"), _UP
        else:
            shape_txt, shape_col = (f"现价下方筹码仅 {below*100:.0f}% → "
                                    f"获利盘薄, 追高风险"), THEME["amber"]
        out["chips"] = {
            "prices": prices, "weights": weights, "cur": cur, "poc": poc,
            "below": below,
            "title": f"当前筹码堆积形态 · 现价下方筹码 {below*100:.0f}%",
            "shape_txt": shape_txt, "shape_color": shape_col,
        }
    else:
        out["chips"] = None

    # ── 右下: 股东户数变化 ──
    if holder_series:
        days = [_day(s["end_date"]) for s in holder_series]
        nums = [float(s["holder_num"]) / 1e4 for s in holder_series]
        ratios = [(s["ratio"] or 0) for s in holder_series]
        labels = []
        for n, r in zip(nums, ratios):
            tag = "↑" if r > 0 else "↓" if r < 0 else ""
            labels.append(f"{n:.0f}万{tag}")
        last_ratio = ratios[-1] if ratios else 0
        trend = ("筹码分散" if last_ratio > 0
                 else "筹码集中" if last_ratio < 0 else "平稳")
        out["holders"] = {
            "days": days, "nums": nums, "ratios": ratios, "labels": labels,
            "title": f"股东户数变化 (万户)  ·  最新 {last_ratio:+.1f}% ({trend})",
        }
    else:
        out["holders"] = None

    # ── 第3行: 供需强度 ──
    if sd_series:
        days = [_day(s["day"]) for s in sd_series]
        dem = [float(s["demand"]) / 1e6 for s in sd_series]
        sup = [float(s["supply"]) / 1e6 for s in sd_series]
        total_dem = sum(dem)
        total_sup = sum(sup)
        ratio = total_dem / total_sup if total_sup > 0 else 1
        if ratio >= SD_BULL:
            tone, tc = "需求占优 · 买方积极", _UP
        elif ratio <= SD_BEAR:
            tone, tc = "供给占优 · 卖方主导", _DN
        else:
            tone, tc = "多空均衡", "#d97706"
        out["sd"] = {
            "days": days, "demand": dem, "supply": sup,
            "title": f"供需强度 (万手)  ·  供需比 {ratio:.2f}  {tone}",
            "tone_color": tc,
        }
    else:
        out["sd"] = None

    # ── 底部综合分析 ──
    caps = []
    if flow_source:
        m20 = float(sum(s.get("main", s.get("flow", 0))
                        for s in flow_source[-20:])) / 1e8
        fc = _UP if m20 > 0.5 else _DN if m20 < -0.5 else "#64748b"
        caps.append((f"近20日主力 {m20:+.2f}亿", fc))
    if chips_series:
        last_chip = chips_series[-1]
        conc_val = last_chip.get("conc")
        profit_val = last_chip.get("profit")
        if conc_val is not None:
            ct = "集中" if conc_val <= 20 else "分散" if conc_val >= 40 else "中性"
            cc = _UP if conc_val <= 20 else _DN if conc_val >= 40 else "#d97706"
            caps.append((f"90%成本集中度 {conc_val:.1f}%({ct})", cc))
        if profit_val is not None:
            pc = _UP if profit_val >= 70 else _DN if profit_val <= 30 else "#64748b"
            caps.append((f"获利盘 {profit_val:.0f}%", pc))
    if chip_dist:
        below_pct = chip_dist["below"] * 100
        if below_pct >= 60:
            cc2, ct2 = _DN, "上方套牢重"
        elif below_pct >= 35:
            cc2, ct2 = _UP, "下方支撑扎实"
        else:
            cc2, ct2 = THEME["amber"], "获利盘薄"
        caps.append((f"现价下方筹码 {below_pct:.0f}%({ct2})", cc2))
    if holder_series:
        hr = holder_series[-1].get("ratio") or 0
        hc = _DN if hr > 0 else _UP if hr < 0 else "#64748b"
        ht = "分散" if hr > 0 else "集中" if hr < 0 else "平稳"
        caps.append((f"股东户数 {hr:+.1f}%({ht})", hc))
    out["caps"] = None
    out["caps_color"] = None
    out["insights"] = None
    if caps:
        cap_text = "  ·  ".join(t for t, _c in caps)
        greens = sum(1 for _t, c in caps if c == _DN)
        reds = sum(1 for _t, c in caps if c == _UP)
        cap_color = (_DN if greens and not reds
                     else _UP if reds and not greens else "#d97706")
        out["caps"] = cap_text
        out["caps_color"] = cap_color
        insights = []
        if flow_source:
            m20_v = float(sum(s.get("main", s.get("flow", 0))
                              for s in flow_source[-20:])) / 1e8
            if m20_v > 1:
                insights.append(("主力持续流入", _UP))
            elif m20_v < -1:
                insights.append(("主力持续流出", _DN))
        if main_flow and len(main_flow) >= 5:
            sup5 = float(sum(s["super"] for s in main_flow[-5:]))
            sm5 = float(sum(s["small"] for s in main_flow[-5:]))
            if sup5 > 0 and sm5 < 0:
                insights.append(("超大单吸筹 · 散户离场", _UP))
            elif sup5 < 0 and sm5 > 0:
                insights.append(("超大单派发 · 散户接盘", _DN))
        if chips_series:
            last_conc = chips_series[-1].get("conc", 999)
            if last_conc <= 20:
                insights.append(("筹码高度集中 · 主力控盘", _UP))
            elif last_conc >= 40:
                insights.append(("筹码分散 · 散户主导", _DN))
        if chip_dist:
            if chip_dist["below"] >= 0.6:
                insights.append(("上方套牢盘沉重 · 反弹抛压大", _DN))
            elif chip_dist["below"] >= 0.35:
                insights.append(("下方筹码扎实 · 支撑可靠", _UP))
            else:
                insights.append(("获利盘薄 · 追高需谨慎", THEME["amber"]))
        if holder_series:
            hr2 = holder_series[-1].get("ratio") or 0
            if hr2 < -10:
                insights.append(("股东数骤降 · 吸筹迹象", _UP))
            elif hr2 > 10:
                insights.append(("股东数激增 · 派发迹象", _DN))
        if sd_series:
            dt = sum(s["demand"] for s in sd_series)
            st = sum(s["supply"] for s in sd_series)
            if st > 0 and dt / st > 1.2:
                insights.append(("买方主导 · 需求强劲", _UP))
            elif st > 0 and dt / st < 0.8:
                insights.append(("卖方主导 · 供给压力", _DN))
        if insights:
            out["insights"] = "  |  ".join(t for t, _c in insights)
    return out

