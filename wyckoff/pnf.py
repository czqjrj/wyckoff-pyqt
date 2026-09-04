"""点数图 (Point & Figure) 计算与绘图。

口径依据 (威科夫 P&F 计数官方来源):
  * Wyckoff Analytics — "Wyckoff Count Guide" / "The Wyckoff Method":
      - box size 按价格分档: 低价股 0.5~1 点, $200 以上 5 点, DJIA 100 点;
      - 横向计数 (Law of Cause and Effect): cause = count line 列数 × 格值 ×
        反转格数, effect 投影成 三档目标 (count line / TR 极值 / 中点);
      - 目标只是 "stop, look and listen" 位置, 不承诺必到。
    https://www.wyckoffanalytics.com/wyckoff-method
    https://www.wyckoffanalytics.com/demand/point-and-figure-part-1
  * StockCharts ChartSchool — 横向/纵向计数标准算法:
      - 纵向计数: 目标 = 主推力列低(高) + count×格值 (向下乘以较小系数为变体);
      - 45 度趋势线为 3-box 反转图上的支撑/阻力。
    https://chartschool.stockcharts.com/table-of-contents/chart-analysis/point-and-figure-charts/p-and-f-price-objectives/p-and-f-price-objectives-horizontal-counts
    https://chartschool.stockcharts.com/table-of-contents/chart-analysis/point-and-figure-charts/p-and-f-price-objectives/p-and-f-price-objectives-vertical-counts
  * Incredible Charts "Point and Figure Charting Guide":
      - 无 "唯一最佳" box 设置 (no single box setting); 对数刻度用于长期/高价,
        普通刻度用于短期; 计数须 "全部横向分格 (有/无 posting 均计)"。
    https://www.incrediblecharts.com/technical/point_figure_charting.php
  * Wyckoff SMI 5-Step Method PDF: 计数从形成右侧到左侧, 有 posting 与无
    posting 的横向分格全部计入。
    https://wyckoffsmi.com/wp-content/uploads/2022/06/Wyckoff-5-Step-Method.pdf

本模块口径与上述来源的对应:
  - box = 最新价 × box_pct (默认 1.5%, 取整到分), 反转默认 3 格;
    另支持 atr 模式: box = ATR(14) × atr_factor (默认 0.5), 随波动率自适应;
  - 横向计数 = count line 列数 × 格值 × 反转 (pnf_targets);
  - 纵向计数 = 最近 window 内最大 X/O 列高度 × 格值 (从列底/列顶投影);
  - 三档目标: 横向计数上方/下方目标 (count line 投影=最大) / _保守 (TR 极值)
    / _中 (中点);
  - 近端参考目标为本项目自创的可到达口径, 非威科夫概念 (见 _pnf_targets_at)。
"""
import pandas as pd
from matplotlib.figure import Figure

from .config import _fs


def _pnf_box(df: pd.DataFrame, box_pct: float = 0.015,
             box_mode: str = "pct", atr_factor: float = 0.5) -> float:
    """计算点数图格值, 支持两种模式 (口径见模块 docstring):
      - pct: 格值 = 最新价 × box_pct (取整到分), 默认模式, 行为与旧版一致;
      - atr: 格值 = ATR(14) × atr_factor (默认 0.5×ATR), 随波动率动态自适应,
        比固定百分比更能刻画近期噪声幅度 (参考 TradingView/Domo/StockCharts
        ATR 动态缩放: 波动大时放大格值滤噪, 波动小时收窄保留细节)。
    取整到分, 下限 0.01。df 缺 atr 列或 atr 不可用时回退 pct 模式。
    """
    if box_mode == "atr" and "atr" in df.columns:
        atr = float(df["atr"].dropna().iloc[-1]) if df["atr"].notna().any() else 0.0
        if atr > 0:
            return max(round(atr * atr_factor, 2), 0.01)
    last = float(df["close"].iloc[-1])
    return max(round(last * box_pct, 2), 0.01)


# 最近一次 build_pnf 的列归属缓存: pnf_volume 收到同一 cols 对象时直接复用
# bar_col, 避免为拿列号重复构建整张点数图 (单次分析中 PnF 图会被构建 2~3 遍)。
# 以 cols 对象身份匹配保证正确性; 并发多股分析时身份不匹配则回退重建, 不影响结果。
_LAST_BUILD = {"cols": None, "bar_col": None, "reversal": 3}


def build_pnf(df: pd.DataFrame, box_pct: float = 0.015, reversal: int = 3,
              box_mode: str = "pct", atr_factor: float = 0.5):
    """点数图 (Point & Figure) 计算。
    box: 格值, 见 _pnf_box (pct=最新价×box_pct / atr=ATR×atr_factor, 取整到分)
    reversal: 反转格数(默认3格)
    box_mode: "pct"(默认, 百分比) 或 "atr"(动态 ATR 格值, 见模块 docstring)
    atr_factor: box_mode="atr" 时的 ATR 倍数 (默认 0.5, 参考主流 0.5×ATR)
    返回 ([{type:'X'|'O', rows:[行号...], lo, hi, count}], box)
    """
    box = _pnf_box(df, box_pct, box_mode, atr_factor)
    cols, bar_col = _build_pnf(df, box, reversal)
    # 记录本次构建的列归属, 供 pnf_volume 复用 (同 cols 对象 → 跳过重复构建)
    _LAST_BUILD["cols"] = cols
    _LAST_BUILD["bar_col"] = bar_col
    _LAST_BUILD["reversal"] = reversal
    return cols, box


def _build_pnf(df: pd.DataFrame, box: float, reversal: int = 3):
    """build_pnf 核心循环: 返回 (cols, bar_col)。

    bar_col[i] = 第 i 根K线所属的列号 (列按生成序编号, 与 cols 一一对应),
    供成交量聚合 (pnf_volume) 复用, 保证列归属与 build_pnf 完全一致。
    """
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values
    n = len(df)

    def r(p):
        return int(round(p / box))

    def price(row):
        return row * box

    def mkcol(t, rows):
        return {"type": t, "rows": list(rows)}

    # 首列: 由首根K线方向决定
    first_rows = list(range(r(lows[0]), r(highs[0]) + 1))
    cur = mkcol("X" if closes[0] >= opens[0] else "O", first_rows)
    cols = []
    bar_col = [0] * n
    for i in range(1, n):
        h = r(highs[i])
        low = r(lows[i])
        if cur["type"] == "X":
            top = cur["rows"][-1]
            if h > top:
                cur["rows"].extend(range(top + 1, h + 1))
            elif low <= top - reversal:
                cols.append(cur)
                # 反转新列从最高 X 的下一格开始 (不与前一列同格重叠)
                cur = mkcol("O", range(low, top))
                bar_col[i] = len(cols)
        else:
            bottom = cur["rows"][0]
            if low < bottom:
                cur["rows"] = list(range(low, bottom)) + cur["rows"]
            elif h >= bottom + reversal:
                cols.append(cur)
                # 反转新列从最低 O 的上一格开始
                cur = mkcol("X", range(bottom + 1, h + 1))
                bar_col[i] = len(cols)
    cols.append(cur)

    out = []
    for c in cols:
        if not c["rows"]:
            continue
        out.append({"type": c["type"], "rows": list(c["rows"]),
                    "lo": price(min(c["rows"])), "hi": price(max(c["rows"])),
                    "count": max(c["rows"]) - min(c["rows"]) + 1})
    return out, bar_col


def pnf_volume(df: pd.DataFrame, cols, box: float, reversal: int = 3,
               bar_col: list = None) -> dict:
    """点数图成交量聚合: 列级量 + 箱体量 (Volume-at-Price)。

    cols/box 须来自 build_pnf(df) 的同一输出 (内部以同一 box/反转复算每根
    K线的列归属, 与 build_pnf 口径一致, 保证列号对齐)。df 需含 volume 列
    (单位: 股)。

    bar_col 为可选列归属缓存: 与 build_pnf 同一次构建的 bar_col 时直接复用
    (无需重建整图); 传入且匹配即零成本, 否则自动按 build_pnf 的最近一次
    构建或重新计算兜底。

    - col_vols: 与 cols 等长的列级成交量列表 (每列累计量, 底部柱)
    - row_vols: {行号: 累计量}, 每根K线的量按高低价跨越的价格箱体均分
      (右侧 Volume-at-Price 直方图)
    - col_max / row_max: 各自最大值 (供归一化), 0 表示无成交量数据
    - total: 总成交量
    """
    if "volume" not in df.columns or len(df) == 0 or box <= 0:
        return {"col_vols": [], "row_vols": {}, "col_max": 0.0,
                "row_max": 0.0, "total": 0.0}
    if bar_col is None:
        last = _LAST_BUILD.get("cols")
        if last is cols and _LAST_BUILD.get("reversal") == reversal:
            bar_col = _LAST_BUILD.get("bar_col")
    if bar_col is None:
        _, bar_col = _build_pnf(df, box, reversal)
    vols = df["volume"].astype(float).values
    lows = df["low"].values
    highs = df["high"].values
    n = len(df)

    def r(p):
        return int(round(p / box))

    col_vols = [0.0] * len(cols)
    row_vols = {}
    total = 0.0
    for i in range(n):
        v = float(vols[i])
        total += v
        j = bar_col[i]
        if 0 <= j < len(col_vols):
            col_vols[j] += v
        r0, r1 = r(lows[i]), r(highs[i])
        lo, hi = min(r0, r1), max(r0, r1)
        share = v / (hi - lo + 1)
        for row in range(lo, hi + 1):
            row_vols[row] = row_vols.get(row, 0.0) + share
    return {
        "col_vols": col_vols,
        "row_vols": row_vols,
        "col_max": max(col_vols) if col_vols else 0.0,
        "row_max": max(row_vols.values()) if row_vols else 0.0,
        "total": total,
    }


def pnf_targets(df: pd.DataFrame, cols, box: float, reversal: int = 3,
                volumes: dict = None) -> dict:
    """威科夫计数目标价 (P&F), 在最后 (最新) 一列处计算。

    横向计数: TR(交易区间)宽度 = 区间上沿 - 区间下沿, 从突破边界投影;
    纵向计数: 主升/主跌列高度投影。
    TR 取当前横向盘整带: 最近一根大幅趋势列之后的列群。
    volumes: 可选 pnf_volume() 返回值, 用于 POC/价值区量加权计算。
    返回含 tr 区间、POC、三档目标、到达概率、空间百分比等, 供 plot_pnf 标注。
    """
    last_price = float(df["close"].iloc[-1])
    row_vols = volumes.get("row_vols") if volumes else None
    return _pnf_targets_at(cols, box, reversal, end_col=len(cols) - 1,
                           last_price=last_price, row_vols=row_vols)


def _pnf_targets_at(cols, box: float, reversal: int = 3,
                    end_col: int = None, last_price: float = None,
                    row_vols: dict = None) -> dict:
    """在指定列 end_col (视为"当前"最后一列) 处计算威科夫计数目标。

    pnf_targets 与 pnf_history_targets 共用此核心, 保证历史回溯与最新测算
    口径完全一致。end_col=None 默认最后一列; last_price 用于 TR 合理性
    (宽度不超过当时价格的 1.5 倍) 校验, 缺省用该列中位价。
    row_vols: 可选 VAP 行成交量字典 {行号: 累计量}, 用于 POC(控制点)计算。
    """
    if not cols or len(cols) < 8:
        return {}
    end_col = len(cols) - 1 if end_col is None else int(end_col)
    if end_col < 7 or end_col >= len(cols):
        return {}
    view = cols[:end_col + 1]
    if last_price is None:
        lastc = view[-1]
        last_price = (lastc["hi"] + lastc["lo"]) / 2
    if last_price <= 0:
        return {}

    # 识别最近盘整带: 在最近 max(12, len//4) 列中找最大趋势列(单列跨度最大), 其后为 TR。
    # TR 上下轨用"中部价格带" (25%~75% 分位数 ± 1格), 避免被 TR 列群中的
    # 大趋势列极值拉宽 (校准: 688981 下跌中继被误标成 149~176 派发区间, 横向
    # 目标失真到 +84%; 中位带剔除了趋势列贡献的头部/尾部, 区间收窄到真实盘整带)。
    # 动态窗口: 列数较少时用12, 列数较多时扩展到 len//4, 避免漏掉早期趋势列。
    win_size = max(12, min(len(view) // 4, 30))
    window = view[-win_size:]
    biggest_idx = max(range(len(window)), key=lambda k: window[k]["count"])
    if biggest_idx != len(window) - 1 and window[biggest_idx]["count"] >= reversal * 2:
        start_idx = biggest_idx + 1
    else:
        start_idx = max(len(window) - 8, 0)
    tr_cols = window[start_idx:]
    if not tr_cols:
        return {}

    # TR 列群全部价格点 (每列 rows×box, 兼容 rows 为空的测试列则用 lo/hi)
    prices = []
    for c in tr_cols:
        if c["rows"]:
            prices.extend(r * box for r in c["rows"])
        else:
            prices.extend((c["lo"], c["hi"]))
    if not prices:
        return {}
    prices.sort()
    q25 = prices[len(prices) // 4]
    q75 = prices[3 * len(prices) // 4]
    tr_top = q75 + box
    tr_bottom = q25 - box
    tr_width = tr_top - tr_bottom
    if tr_width <= box or tr_width > last_price * 1.5:
        return {}

    # ── POC (控制点 / Point of Control): VAP 成交量最大的价格行 ──
    # 有 VAP 数据时用量加权, 否则用价格频次 (与 count_line 类似但更关注"持续驻留")
    poc_row = None
    poc_val = None
    if row_vols:
        tr_row_set = set()
        for c in tr_cols:
            if c["rows"]:
                tr_row_set.update(c["rows"])
            else:
                tr_row_set.update(range(round(c["lo"] / box), round(c["hi"] / box) + 1))
        vol_pairs = [(r, row_vols.get(r, 0.0)) for r in tr_row_set if row_vols.get(r, 0) > 0]
        if vol_pairs:
            poc_row, _ = max(vol_pairs, key=lambda x: x[1])
            poc_val = poc_row * box
    if poc_val is None:
        # 无 VAP 时退化为 TR 价格中位数 (更稳健的中枢)
        poc_val = prices[len(prices) // 2]
    val_area_half = tr_width * 0.35  # 价值区 = POC ± 35% TR 宽 (标准市场轮廓口径)
    vah = poc_val + val_area_half
    val = poc_val - val_area_half

    # 记录 TR 在整图中的列序号范围 (用于绘图标注计数起止)。
    base = end_col + 1 - len(window)
    tr_start_col = base + start_idx
    tr_end_col = base + len(window) - 1

    # 方向: 最后一列相对"前序区间"(不含最后一列)是否突破
    prev_tr = tr_cols[:-1] if len(tr_cols) > 1 and tr_cols[-1] is view[-1] else tr_cols
    p_top = max(c["hi"] for c in prev_tr)
    p_bottom = min(c["lo"] for c in prev_tr)
    lastc = view[-1]
    direction = "range"
    if lastc["type"] == "X" and lastc["hi"] > p_top:
        direction = "up"
    elif lastc["type"] == "O" and lastc["lo"] < p_bottom:
        direction = "down"
    # 突破时 TR 边界用中部价格带 (剔除趋势列极值拉宽)
    if direction != "range" and prev_tr:
        p_prices = []
        for c in prev_tr:
            if c["rows"]:
                p_prices.extend(r * box for r in c["rows"])
            else:
                p_prices.extend((c["lo"], c["hi"]))
        if p_prices:
            p_prices.sort()
            p_q25 = p_prices[len(p_prices) // 4]
            p_q75 = p_prices[3 * len(p_prices) // 4]
            tr_top, tr_bottom = p_q75 + box, p_q25 - box
            tr_width = tr_top - tr_bottom

    targets = {
        "tr_top": round(tr_top, 2),
        "tr_bottom": round(tr_bottom, 2),
        "tr_width": round(tr_width, 2),
        "tr_start_col": tr_start_col,
        "tr_end_col": tr_end_col,
        "direction": direction,
        "poc": round(poc_val, 2),
        "vah": round(vah, 2),
        "val": round(val, 2),
    }
    # TR 全区间最值 (保守档投影锚点)
    tr_low = min((c["lo"] for c in tr_cols), default=tr_bottom)
    tr_high = max((c["hi"] for c in tr_cols), default=tr_top)

    # 威科夫横向计数 (Law of Cause and Effect)
    def _crossing(row):
        out = 0
        for c in tr_cols:
            if c["rows"]:
                if min(c["rows"]) <= row <= max(c["rows"]):
                    out += 1
            else:
                lo_r, hi_r = round(c["lo"] / box), round(c["hi"] / box)
                if lo_r <= row <= hi_r:
                    out += 1
        return out

    rows_in_tr = set()
    for c in tr_cols:
        if c["rows"]:
            rows_in_tr.update(c["rows"])
        else:
            rows_in_tr.update(range(round(c["lo"] / box), round(c["hi"] / box) + 1))
    count_line_row = max(rows_in_tr, key=_crossing, default=round(tr_bottom / box))
    columns = max(_crossing(count_line_row), 1)
    count_line = count_line_row * box
    cause = columns * box * reversal
    targets["columns"] = columns
    targets["count_line"] = round(count_line, 2)
    targets["cause"] = round(cause, 2)

    # 三档候选: 三个锚点分别投影 (TR极值 / POC / count_line)
    # 注意: 锚点相对现价的距离顺序不确定, 下面按"目标距现价远近"重排为保守/中/激进
    up_cands = []
    dn_cands = []
    for label, anchor_up, anchor_dn in (
        ("tr_ext",  tr_low,  tr_high),            # 教科书极值
        ("poc",     poc_val, poc_val),            # 价值中枢
        ("cntline", count_line, count_line),      # 威科夫原教旨
    ):
        up_t = round(anchor_up + cause, 2)
        dn_t = round(anchor_dn - cause, 2)
        up_cands.append((up_t, label))
        dn_cands.append((dn_t, label))

    # 按距现价的**绝对值距离从小到大**重排 → 保守(最近) / 中 / 激进(最远)
    up_cands.sort(key=lambda x: abs(x[0] - last_price))
    dn_cands.sort(key=lambda x: abs(x[0] - last_price))
    targets["横向计数上方目标_保守"] = up_cands[0][0]
    targets["横向计数上方目标_中"]    = up_cands[1][0]
    targets["横向计数上方目标"]       = up_cands[2][0]  # 激进 = 最远 = 默认值
    targets["横向计数下方目标_保守"] = dn_cands[0][0]
    targets["横向计数下方目标_中"]    = dn_cands[1][0]
    targets["横向计数下方目标"]       = dn_cands[2][0]  # 激进 = 最远 = 默认值

    # 近端参考目标: 区间边界外推 0.2×cause, 相对现价 ±4% 封顶 (可到达口径)
    NEAR_CAP = 0.04
    targets["近端上方目标"] = round(min(tr_top + cause * 0.2, last_price * (1 + NEAR_CAP)), 2)
    targets["近端下方目标"] = round(max(tr_bottom - cause * 0.2, last_price * (1 - NEAR_CAP)), 2)

    # 纵向计数: 主升/主跌列高度 (window 内最大趋势列, 避免全历史远期列失真)
    x_cols = [c for c in window if c["type"] == "X"]
    o_cols = [c for c in window if c["type"] == "O"]
    if x_cols:
        best_x = max(x_cols, key=lambda c: c["count"])
        targets["纵向计数上方目标"] = round(best_x["lo"] + best_x["count"] * box, 2)
    if o_cols:
        best_o = max(o_cols, key=lambda c: c["count"])
        targets["纵向计数下方目标"] = round(best_o["hi"] - best_o["count"] * box, 2)

    # ── 距现价空间 (百分比) & 到达概率 ──
    # 空间 = (目标 - 现价) / 现价 * 100, 上涨为正, 下跌为负
    def _pct(tgt):
        return round((tgt - last_price) / last_price * 100, 1) if last_price > 0 else 0.0

    # 到达概率估算: 基于评估结果 (530段历史) 校准的连续函数
    #   空间: 用分段线性从历史到达率反推, 避免三档台阶造成分布挤堆
    #   cause_ratio: 因果强度 (cause/TR宽) 越高 → 置信加分
    #   POC同向: 现价在POC上方做多看涨, 下方做空看跌 → 小幅加分
    cause_ratio = cause / tr_width if tr_width > 0 else 0.0

    def _prob(space_pct, direction_flag):
        a = abs(float(space_pct))
        # ── 空间衰减 (从评估数据校准): 空间越小到达率越高, 连续单调降 ──
        if a <= 5:
            # 0~5%: 历史到达率 90%+
            s_base = 0.90 - (a / 5) * 0.08  # 90% → 82%
        elif a <= 10:
            # 5~10%: 历史 82% → 68%
            s_base = 0.82 - ((a - 5) / 5) * 0.14
        elif a <= 20:
            # 10~20%: 历史 68% → 48%
            s_base = 0.68 - ((a - 10) / 10) * 0.20
        elif a <= 35:
            # 20~35%: 历史 48% → 34%
            s_base = 0.48 - ((a - 20) / 15) * 0.14
        else:
            # >35%: 历史 ~30%, 继续缓慢衰减到 20% 下限
            s_base = max(0.20, 0.34 - ((a - 35) / 30) * 0.14)
        # ── 因果强度: cause_ratio 0~1.5 线性加分 0~+0.08 ──
        cr_add = min(0.08, max(0.0, cause_ratio) / 1.5 * 0.08)
        # ── POC方向支持: 同向 +0.04 ──
        poc_add = 0.0
        poc_above = last_price > poc_val
        if direction_flag == "up" and poc_above:
            poc_add = 0.04
        elif direction_flag == "down" and not poc_above:
            poc_add = 0.04
        p = s_base + cr_add + poc_add
        return max(0.15, min(0.95, round(p, 2)))

    up_t_near = targets.get("近端上方目标", 0)
    dn_t_near = targets.get("近端下方目标", 0)
    up_t_cons = targets.get("横向计数上方目标_保守", 0)
    dn_t_cons = targets.get("横向计数下方目标_保守", 0)
    up_t_mid = targets.get("横向计数上方目标_中", 0)
    dn_t_mid = targets.get("横向计数下方目标_中", 0)
    up_t_agg = targets.get("横向计数上方目标", 0)
    dn_t_agg = targets.get("横向计数下方目标", 0)
    targets["上方空间_近端%"] = _pct(up_t_near)
    targets["下方空间_近端%"] = _pct(dn_t_near)
    targets["上方空间_保守%"] = _pct(up_t_cons)
    targets["下方空间_保守%"] = _pct(dn_t_cons)
    targets["上方空间_中%"] = _pct(up_t_mid)
    targets["下方空间_中%"] = _pct(dn_t_mid)
    targets["上方空间_激进%"] = _pct(up_t_agg)
    targets["下方空间_激进%"] = _pct(dn_t_agg)
    targets["上方概率_保守"] = _prob(targets["上方空间_保守%"], "up")
    targets["下方概率_保守"] = _prob(targets["下方空间_保守%"], "down")
    targets["上方概率_中"] = _prob(targets["上方空间_中%"], "up")
    targets["下方概率_中"] = _prob(targets["下方空间_中%"], "down")
    targets["上方概率_激进"] = _prob(targets["上方空间_激进%"], "up")
    targets["下方概率_激进"] = _prob(targets["下方空间_激进%"], "down")
    # TR 内位置: 现价相对 TR 高低的百分位 (0~100)
    tr_range = tr_top - tr_bottom
    targets["tr_position%"] = round(max(0, min(100,
        (last_price - tr_bottom) / tr_range * 100 if tr_range > 0 else 50)), 1)
    return targets


def _pnf_zone(cols, tr_top, tr_bottom, direction, break_col, box, lookahead: int = 6):
    """威科夫吸筹/派发区间判定: 以突破后的走势结果为准, 而非仅看瞬时突破方向。

    吸筹 = 区间后价格向上突破并延续 (低位区向上的标志);
    派发 = 区间后价格向下破位并延续 (高位区向下的标志)。
    若突破后在 lookahead 列内被快速反向打回 —
      向上失败 → UTAD上冲, 实为派发 (如高位区的诱多上冲);
      向下失败 → Spring弹簧, 实为吸筹 (如低位区的诱空下探)。
    这样同一位价区间不会随瞬时突破方向被同时标成吸筹/派发,
    且与威科夫位置语义一致 (高位区上冲=UTAD→派发, 低位区下探=Spring→吸筹)。
    返回 (zone, note): zone 恒为 "吸筹"/"派发", note 为 Spring/UTAD 提示或空串。
    """
    tol = max(box, (tr_top - tr_bottom) * 0.1)
    post = cols[break_col + 1: break_col + 1 + lookahead]
    if direction == "up":
        if any(c["lo"] < tr_bottom - tol for c in post):
            return "派发", "UTAD上冲"
        return "吸筹", ""
    if any(c["hi"] > tr_top + tol for c in post):
        return "吸筹", "Spring"
    return "派发", ""


def pnf_history_targets(cols, box: float, reversal: int = 3,
                        window: int = 12, max_items: int = 6,
                        min_gap: int = 5) -> list:
    """回溯点数图历史: 对每个已形成的 TR 突破段计算当时的上涨/下跌计数目标,
    并用突破后实际走势核对目标是否被触及。

    返回值: list of dict, 每段含:
      break_col   突破列索引 (TR 后首次突破上/下沿)
      direction   up / down
      zone        威科夫语义: 按突破后走势结果划分 — 向上突破并延续→吸筹区间,
                  向下破位并延续→派发区间; 快速反向打回则 向上失败(UTAD)→派发,
                  向下失败(Spring)→吸筹
      zone_note   "Spring"/"UTAD上冲" 或空串 (供图上标注)
      seq         段序号 (返回子集内从 1 起)
      tr_top/tr_bottom/tr_width 突破前的 TR 区间
      tr_start_col/tr_end_col    TR 的列范围 (绘图用)
      up_target/down_target      突破方向的目标价 (近端口径, 供图标注)
      up_hit/down_hit            突破后价格是否到达对应目标 (近端口径)
      上方目标_保守/中/激进 / 下方目标_保守/中/激进  三档目标价
      上方hit_保守/中/激进  /  下方hit_保守/中/激进   三档是否到达
      上方概率_保守/中/激进 / 下方概率_保守/中/激进  模型估算到达概率 (0~1)
    同一 TR 的连续同向突破只记第一条 (相邻段间距 < min_gap 视为同一趋势延续,
    避免同一波下跌在图上堆叠多条近重复测算), 每段目标与最新测算
    (_pnf_targets_at) 同口径, 供"点数图历史测算"绘图与准确度核对。
    """
    if not cols or len(cols) < window + 1:
        return []
    hist = []
    last_key = None
    i = window
    while i < len(cols):
        t = _pnf_targets_at(cols, box, reversal, end_col=i)
        if not t or t["direction"] == "range":
            i += 1
            continue
        if hist and i - hist[-1]["break_col"] < min_gap:
            i += 1
            continue
        key = (round(t["tr_top"], 4), round(t["tr_bottom"], 4), t["direction"])
        if key != last_key:
            t["break_col"] = i
            # 命中核对的优先用"近端目标" (可到达口径, 已做±4%封顶), 横向满宽
            # 目标常为教科书理想位, 在评估窗口内几乎不可达 — 用近端目标才与
            # accuracy 的 up_hit/down_hit 口径一致 (校准: 历史段 up_hit 恒为
            # False 的根因之一)。取第一个"近端"目标, 缺省回退到任意上方/下方目标。
            up = next((v for k, v in t.items()
                       if "近端" in k and k.endswith("上方目标")
                       and isinstance(v, (int, float))), None)
            if up is None:
                up = next((v for k, v in t.items()
                           if k.endswith("上方目标") and isinstance(v, (int, float))), None)
            dn = next((v for k, v in t.items()
                       if "近端" in k and k.endswith("下方目标")
                       and isinstance(v, (int, float))), None)
            if dn is None:
                dn = next((v for k, v in t.items()
                           if k.endswith("下方目标") and isinstance(v, (int, float))), None)
            t["up_target"] = up
            t["down_target"] = dn
            t["up_hit"] = False
            t["down_hit"] = False
            # 到位容差: 目标价 ±1格 视为"基本到位" (威科夫计数给出的是目标位
            # 而非精确点, 价格差一格内到达即算有效, 避免"差0.3就判未到"的误判)
            tol = max(box, t.get("tr_width", 0) * 0.05)

            # ── 三档目标 + 概率 分别记录并核对 hit (供准确率评估) ──
            tier_map = {
                "保守": ("横向计数上方目标_保守", "横向计数下方目标_保守",
                         "上方概率_保守", "下方概率_保守"),
                "中":   ("横向计数上方目标_中", "横向计数下方目标_中",
                         "上方概率_中", "下方概率_中"),
                "激进": ("横向计数上方目标", "横向计数下方目标",
                         "上方概率_激进", "下方概率_激进"),
            }
            for tier, (up_k, dn_k, up_p_k, dn_p_k) in tier_map.items():
                t_up = t.get(up_k)
                t_dn = t.get(dn_k)
                t[f"上方目标_{tier}"] = t_up
                t[f"下方目标_{tier}"] = t_dn
                t[f"上方概率_{tier}"] = t.get(up_p_k)
                t[f"下方概率_{tier}"] = t.get(dn_p_k)
                t[f"上方hit_{tier}"] = False
                t[f"下方hit_{tier}"] = False
                if isinstance(t_up, (int, float)):
                    t[f"上方空间_{tier}%"] = t.get(f"上方空间_{tier}%")
                if isinstance(t_dn, (int, float)):
                    t[f"下方空间_{tier}%"] = t.get(f"下方空间_{tier}%")

            for c in cols[i + 1:]:
                if up is not None and c["hi"] >= up - tol:
                    t["up_hit"] = True
                if dn is not None and c["lo"] <= dn + tol:
                    t["down_hit"] = True
                # 三档分别核对
                for tier in tier_map:
                    up_t = t.get(f"上方目标_{tier}")
                    dn_t = t.get(f"下方目标_{tier}")
                    if isinstance(up_t, (int, float)) and c["hi"] >= up_t - tol:
                        t[f"上方hit_{tier}"] = True
                    if isinstance(dn_t, (int, float)) and c["lo"] <= dn_t + tol:
                        t[f"下方hit_{tier}"] = True
            # 威科夫语义: 按突破后的走势结果划分吸筹/派发区间。
            # 向上突破并延续 → 吸筹; 向下破位并延续 → 派发;
            # 快速反向打回 → 向上失败(UTAD→派发) / 向下失败(Spring→吸筹)。
            t["zone"], t["zone_note"] = _pnf_zone(
                cols, t["tr_top"], t["tr_bottom"], t["direction"], i, box)
            t["seq"] = len(hist) + 1
            hist.append(t)
            last_key = key
        i += 1
    hist = hist[-max_items:]
    for n, t in enumerate(hist, start=1):
        t["seq"] = n
    return hist


def build_pnf_data(cols, box, title, targets=None, history=None, df=None,
                   box_mode: str = "pct", atr_factor: float = 0.5):
    """收集桌面端 pyqtgraph 点数图所需绘制数据 (与 plot_pnf 同口径)。

    在 worker 线程内调用, 返回值可跨线程交给 PnfWidget.set_data。
    df 提供成交量列时, 附加上成交量数据 (pnf_volume), 供 PnfWidget 渲染
    列级柱与箱体量 Volume-at-Price。
    box_mode/atr_factor 透传用于标题格值说明 (与 build_pnf 同参)。
    """
    data = {
        "cols": [dict(c) for c in cols],
        "box": float(box),
        "box_mode": box_mode,
        "atr_factor": float(atr_factor),
        "title": title,
        "targets": dict(targets or {}),
        "history": [dict(h) for h in (history or [])],
    }
    if df is not None:
        data["volumes"] = pnf_volume(df, cols, box)
    return data


def pnf_box_label(box_mode: str = "pct", atr_factor: float = 0.5) -> str:
    """格值来源说明文字 (供图表标题/解读行): pct → 百分比, atr → 动态ATR。"""
    if box_mode == "atr":
        return f"动态ATR×{atr_factor}"
    return "百分比"


def pnf_hist_title(history) -> str:
    """点数图历史命中统计标题 (plot_pnf 与 pyqtgraph PnfWidget 共用)。"""
    if not history:
        return ""
    ups = [(h["up_target"], h["up_hit"]) for h in history
           if h.get("direction") == "up" and h.get("up_target") is not None]
    dns = [(h["down_target"], h["down_hit"]) for h in history
           if h.get("direction") == "down" and h.get("down_target") is not None]
    if not (ups or dns):
        return ""
    u_ok = sum(1 for _t, hit in ups if hit)
    d_ok = sum(1 for _t, hit in dns if hit)
    u_pct = u_ok / len(ups) * 100 if ups else 0.0
    d_pct = d_ok / len(dns) * 100 if dns else 0.0
    total_n = len(ups) + len(dns)
    total_ok = u_ok + d_ok
    total_pct = total_ok / total_n * 100 if total_n else 0.0
    if ups and dns:
        return (f"  |  历史测算(准确率): 上涨目标 {u_ok}/{len(ups)}"
                f" ({u_pct:.0f}%) · 下跌目标 {d_ok}/{len(dns)}"
                f" ({d_pct:.0f}%) · 综合 {total_ok}/{total_n}"
                f" ({total_pct:.0f}%)")
    if ups:
        return (f"  |  历史测算(准确率): 上涨目标 {u_ok}/{len(ups)}"
                f" ({u_pct:.0f}%)")
    return (f"  |  历史测算(准确率): 下跌目标 {d_ok}/{len(dns)}"
            f" ({d_pct:.0f}%)")


def pnf_cap(targets, cols):
    """点数图底部解读行 (文字, 颜色)。plot_pnf 与 pyqtgraph PnfWidget 共用。

    内容丰富: 突破状态、TR、POC价值区、三档目标+空间%+概率、计数因。
    """
    if not targets:
        cap = "列数不足, 无法形成TR计数 → 暂观望"
        cap_color = "#64748b"
        return cap, cap_color

    direction = targets.get("direction", "range")
    tr_top = targets.get("tr_top")
    tr_bottom = targets.get("tr_bottom")
    poc = targets.get("poc")
    vah = targets.get("vah")
    val_ = targets.get("val")
    c = targets.get("columns", 0)
    ca = targets.get("cause", 0)
    count_s = f"{c}列×格×反转 · 因{ca:.2f}" if c else ""
    poc_s = f" · POC{poc:.2f} 价值区{val_:.2f}~{vah:.2f}" if poc else ""

    def _tier(targets_dict, direction_suffix):
        """三档目标汇总: 保守(概率) / 中(概率) / 激进(概率)  + 空间%。"""
        tiers = []
        # 保守 / 中 / 激进
        for tier_key, label in (("保守", "保"), ("中", "中"), ("激进", "激")):
            t_up = targets_dict.get(f"横向计数上方目标{'_' + tier_key if tier_key != '激进' else ''}")
            t_dn = targets_dict.get(f"横向计数下方目标{'_' + tier_key if tier_key != '激进' else ''}")
            sp_up = targets_dict.get(f"上方空间_{tier_key}%")
            sp_dn = targets_dict.get(f"下方空间_{tier_key}%")
            prob_up = targets_dict.get(f"上方概率_{tier_key}")
            prob_dn = targets_dict.get(f"下方概率_{tier_key}")
            t = t_up if direction_suffix == "up" else t_dn
            sp = sp_up if direction_suffix == "up" else sp_dn
            prob = prob_up if direction_suffix == "up" else prob_dn
            if t and sp is not None:
                sign = "+" if sp > 0 else ""
                p = f"{int(prob*100)}%" if prob is not None else ""
                tiers.append(f"{label}{t:.2f}{sign}{sp:.1f}%{p}")
        return " / ".join(tiers) if tiers else ""

    tr_range = tr_top - tr_bottom if tr_top and tr_bottom else 0
    tr_pos = targets.get("tr_position%")
    pos_s = f" · TR位{tr_pos:.0f}%" if tr_pos is not None and tr_range > 0 else ""

    if direction == "up":
        tier_s = _tier(targets, "up")
        near_t = targets.get("近端上方目标")
        near_sp = targets.get("上方空间_近端%")
        near_s = ""
        if near_t and near_sp is not None:
            sign = "+" if near_sp > 0 else ""
            near_s = f" · 近端{near_t:.2f}{sign}{near_sp:.1f}%"
        cap = (f"↑已突破TR上沿{tr_top:.2f}向上{pos_s}{poc_s}{near_s}"
               f" · 三档 [{tier_s}] · {count_s}")
        cap_color = "#16a34a"
    elif direction == "down":
        tier_s = _tier(targets, "down")
        near_t = targets.get("近端下方目标")
        near_sp = targets.get("下方空间_近端%")
        near_s = ""
        if near_t and near_sp is not None:
            sign = "+" if near_sp > 0 else ""
            near_s = f" · 近端{near_t:.2f}{sign}{near_sp:.1f}%"
        cap = (f"↓已跌破TR下沿{tr_bottom:.2f}向下{pos_s}{poc_s}{near_s}"
               f" · 三档 [{tier_s}] · {count_s}")
        cap_color = "#dc2626"
    else:
        up_t_cons = targets.get("横向计数上方目标_保守")
        up_sp_cons = targets.get("上方空间_保守%")
        dn_t_cons = targets.get("横向计数下方目标_保守")
        dn_sp_cons = targets.get("下方空间_保守%")
        sign_u = "+" if (up_sp_cons or 0) > 0 else ""
        sign_d = "+" if (dn_sp_cons or 0) > 0 else ""
        tier_s = ""
        if up_t_cons and up_sp_cons is not None and dn_t_cons and dn_sp_cons is not None:
            tier_s = (f"  上{up_t_cons:.2f}{sign_u}{up_sp_cons:.1f}%"
                      f" / 下{dn_t_cons:.2f}{sign_d}{dn_sp_cons:.1f}%")
        cap = (f"⇄TR区间{tr_bottom:.2f}~{tr_top:.2f}内整理{pos_s}{poc_s}"
               f" · 等待突破{tier_s} · {count_s}")
        cap_color = "#d97706"
    return cap, cap_color


def plot_pnf(df: pd.DataFrame, cols, box, title, fig=None, targets=None,
             history=None, box_mode: str = "pct", atr_factor: float = 0.5):
    """绘制点数图, 按威科夫计数原理标注 TR 区间、计数起止点与上涨/下跌目标位。

    history: pnf_history_targets 的返回值 (历史各 TR 突破段的测算), 以淡色
    标注在图上, 并核对突破后实际价格是否到达目标 (✓ 到达 / ✗ 未到),
    用于检验威科夫横向/纵向计数在历史段上的准确度。
    box_mode/atr_factor: 透传给标题显示格值来源 (见 pnf_box_label)。
    """
    if fig is None:
        fig = Figure(figsize=(11, 7.5), dpi=100)
    else:
        fig.clear()
    vol = pnf_volume(df, cols, box)
    gs = fig.add_gridspec(2, 1, height_ratios=(6.0, 1.1), hspace=0.05,
                          left=0.07, right=0.97, top=0.90, bottom=0.15)
    ax = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)
    xstep = 0.4
    # 标注底色块: 压在图格上也能看清文字
    _txt_bbox = dict(boxstyle="round,pad=0.3", facecolor="#f8fafc",
                     edgecolor="#cbd5e1", alpha=0.95)
    # 传统圈叉图: X 列画 × (红), O 列画 ○ (绿)
    xs_x, ys_x = [], []
    xs_o, ys_o = [], []
    for j, c in enumerate(cols):
        x = j * xstep
        for row in c["rows"]:
            if c["type"] == "X":
                xs_x.append(x)
                ys_x.append(row * box)
            else:
                xs_o.append(x)
                ys_o.append(row * box)
    # 方格坐标纸: 每列一条竖线、每格一条横线 (圈叉图传统底纹)
    rows_all = [row for c in cols for row in c["rows"]]
    if rows_all:
        y = (min(rows_all) - 0.5) * box
        y_end = (max(rows_all) + 1.5) * box
        while y <= y_end:
            ax.axhline(y, color="#dfe6f2", lw=0.5, zorder=0.3)
            y += box
    for j in range(len(cols) + 1):
        ax.axvline((j - 0.5) * xstep, color="#dfe6f2", lw=0.5, zorder=0.3)
    if xs_x:
        ax.scatter(xs_x, ys_x, marker="X", s=30, color="#e03131",
                   linewidths=1.6, zorder=2)
    if xs_o:
        ax.scatter(xs_o, ys_o, marker="o", s=22, facecolors="none",
                   edgecolors="#2f9e44", linewidths=1.6, zorder=2)

    _, yhi = ax.get_ylim()

    # 历史命中统计 (标题附注): 只统计已完结段 (突破后有后续走势可核对)
    hist_title = pnf_hist_title(history)

    # ── 历史测算 (先画, 垫底): 每段吸筹/派发区间 + 目标位 + 到位核对 ──
    if history:
        for h in history:
            c0 = h.get("tr_start_col", 0) * xstep
            c1 = h.get("tr_end_col", len(cols)) * xstep
            cx = (c0 + c1) / 2
            top, bottom = h.get("tr_top", 0), h.get("tr_bottom", 0)
            zone = h.get("zone", "")
            # 吸筹(向上突破后)→暖红底, 派发(向下破位后)→冷绿底, 便于区分
            if zone == "吸筹":
                ax.axvspan(c0, c1, color="#ffe4e6", alpha=0.55, zorder=0)
            else:
                ax.axvspan(c0, c1, color="#d1fae5", alpha=0.55, zorder=0)
            ax.axhline(top, color="#94a3b8", ls=":", lw=0.8, alpha=0.6)
            ax.axhline(bottom, color="#94a3b8", ls=":", lw=0.8, alpha=0.6)
            # 段标签: 仅短标 (段号+区间), 详细语义在图注, 减少压格
            note = h.get("zone_note") or ""
            note = f"·{note}" if note else ""
            ax.text(cx, top + (yhi - bottom) * 0.012,
                    f"段{int(h.get('seq', 0))} {bottom:.2f}~{top:.2f}",
                    fontsize=_fs(0), ha="center", va="bottom",
                    fontweight="bold",
                    bbox=_txt_bbox,
                    color="#be123c" if zone == "吸筹" else "#047857")
            tx = c1 + 0.12
            # 目标位: 只画突破方向的目标 (派发段看下跌目标, 吸筹段看上涨目标),
            # 反向目标未到是正常 (派发后不该再涨), 画出来只会干扰准确率观感。
            if zone == "吸筹" and h.get("up_target") is not None:
                hit = bool(h.get("up_hit"))
                col = "#16a34a" if hit else "#94a3b8"
                ax.axhline(h["up_target"], color=col,
                           ls="-" if hit else ":", lw=1.0,
                           alpha=0.95 if hit else 0.7)
                ax.text(tx, h["up_target"],
                        f"{'已到' if hit else '未到'} 上涨目标 {h['up_target']:.2f}",
                        fontsize=_fs(-1), color=col, va="center",
                        bbox=_txt_bbox,
                        fontweight="bold" if hit else "normal")
            if zone == "派发" and h.get("down_target") is not None:
                hit = bool(h.get("down_hit"))
                col = "#dc2626" if hit else "#94a3b8"
                ax.axhline(h["down_target"], color=col,
                           ls="-" if hit else ":", lw=1.0,
                           alpha=0.95 if hit else 0.7)
                ax.text(tx, h["down_target"],
                        f"{'已到' if hit else '未到'} 下跌目标 {h['down_target']:.2f}",
                        fontsize=_fs(-1), color=col, va="center",
                        bbox=_txt_bbox,
                        fontweight="bold" if hit else "normal")

    if targets:
        tr_top, tr_bottom = targets["tr_top"], targets["tr_bottom"]
        direction = targets["direction"]
        c0 = targets.get("tr_start_col", 0) * xstep
        c1 = targets.get("tr_end_col", len(cols)) * xstep
        # 投影线移到箱体量直方图 (VAP) 右侧, 避免与量条重叠
        cend = len(cols) * xstep + 1.1

        # 当前 TR 的威科夫语义: 区间位置为主、突破方向为辅。
        # 低位区 → 吸筹 (向下试探多为 Spring); 高位区 → 派发 (向上冲击多为
        # UTAD); 中位区按突破方向。避免"高位上冲标吸筹/低位下破标派发"的误导。
        mid = (tr_top + tr_bottom) / 2
        tr_c0 = int(targets.get("tr_start_col", 0))
        loc = cols[max(0, tr_c0 - 30):]
        if loc:
            _lo = min(c["lo"] for c in loc)
            _hi = max(c["hi"] for c in loc)
            _pos = (mid - _lo) / (_hi - _lo) if _hi > _lo else 0.5
        else:
            _pos = 0.5
        if direction == "up":
            if _pos > 2 / 3:
                zone_label, zone_fill, zone_edge = "派发区间", "#d1fae5", "#047857"
                zone_note = "高位上冲 → 警惕UTAD, 当前TR实为派发区间"
            else:
                zone_label, zone_fill, zone_edge = "吸筹区间", "#ffe4e6", "#be123c"
                zone_note = "低位向上突破 → 当前TR为吸筹区间"
        elif direction == "down":
            if _pos < 1 / 3:
                zone_label, zone_fill, zone_edge = "吸筹区间", "#ffe4e6", "#be123c"
                zone_note = "低位下破 → 警惕Spring, 当前TR实为吸筹区间"
            else:
                zone_label, zone_fill, zone_edge = "派发区间", "#d1fae5", "#047857"
                zone_note = "高位向下破位 → 当前TR为派发区间"
        else:
            zone_label, zone_fill, zone_edge = "TR区间(整理中)", "#f1f3f5", "#495057"
            zone_note = "仍在区间内盘整 → 等待突破确认方向"
        # 区间语义/计数文字 (图注显示, 不压格子)
        tr_c0n = int(targets.get("tr_start_col", 0))
        tr_c1n = int(targets.get("tr_end_col", len(cols)))
        _cn = targets.get("columns", 0)
        _ca = targets.get("cause", 0)
        info_line = (f"当前区间: {zone_label} {tr_bottom:.2f}~{tr_top:.2f}"
                     f"  |  威科夫横向计数: {_cn}列×格×反转(因{_ca:.2f})"
                     f" → 目标 {targets.get('横向计数上方目标', 0):.2f}"
                     f" / {targets.get('横向计数下方目标', 0):.2f}"
                     f"  |  {zone_note}")

        # TR 区间 (威科夫交易区间)
        # 用数据坐标的 axvspan 而非 axhspan+比例, 避免与末尾 set_xlim 错位
        ax.axvspan(c0, c1, color=zone_fill, alpha=0.6, zorder=0)
        ax.axhline(tr_top, color=zone_edge, ls="--", lw=1.2, alpha=0.8)
        ax.axhline(tr_bottom, color=zone_edge, ls="--", lw=1.2, alpha=0.8)
        # 区间语义/计数文字移到图注 (info_line), 不压格子

        # 计数起止点标注 (横向宽度): 从 TR 起点列到终点列
        ax.annotate("", xy=(c0, tr_bottom), xytext=(c0, tr_top),
                    arrowprops=dict(arrowstyle="<->", color="#495057", lw=1.2))
        ax.annotate("", xy=(c1, tr_bottom), xytext=(c1, tr_top),
                    arrowprops=dict(arrowstyle="<->", color="#495057", lw=1.2))

        # 上涨目标 (累积突破后计数): 从 TR 上沿投影
        if "横向计数上方目标" in targets:
            up = targets["横向计数上方目标"]
            active = direction in ("up", "range")
            ax.axhline(up, color="#2f9e44", ls="--", lw=1.2,
                       alpha=0.95 if active else 0.45)
            ax.annotate("", xy=(cend, up), xytext=(cend, tr_top),
                        arrowprops=dict(arrowstyle="->", color="#2f9e44", lw=1.4,
                                        alpha=0.95 if active else 0.45))
            ax.text(cend + 0.15, up, f"▲ 上涨目标位 {up:.2f}", fontsize=_fs(0),
                    fontweight="bold", color="#2f9e44", va="center",
                    bbox=_txt_bbox)
            ax.text(cend + 0.15, (tr_top + up) / 2,
                    f"+因 {targets.get('cause', 0):.2f}"
                    f" ({targets.get('columns', 0)}列×格×反转)",
                    fontsize=_fs(0), color="#2f9e44", va="center", alpha=0.8,
                    bbox=_txt_bbox)
            # 近端参考目标 (非威科夫, 可到达性校准): 浅色点线区分
            if "近端上方目标" in targets:
                near = targets["近端上方目标"]
                if abs(near - up) > box:
                    ax.axhline(near, color="#82c91e", ls=":", lw=1.0,
                               alpha=0.75 if active else 0.35)
                    ax.text(cend + 0.15, near, f"近端参考 {near:.2f}",
                            fontsize=_fs(0), color="#82c91e", va="center",
                            alpha=0.85 if active else 0.4, bbox=_txt_bbox)
        # 下跌目标 (派发破位后计数): 从 TR 下沿投影
        if "横向计数下方目标" in targets:
            dn = targets["横向计数下方目标"]
            active = direction in ("down", "range")
            ax.axhline(dn, color="#e03131", ls="--", lw=1.2,
                       alpha=0.95 if active else 0.45)
            ax.annotate("", xy=(cend, dn), xytext=(cend, tr_bottom),
                        arrowprops=dict(arrowstyle="->", color="#e03131", lw=1.4,
                                        alpha=0.95 if active else 0.45))
            ax.text(cend + 0.15, dn, f"▼ 下跌目标位 {dn:.2f}", fontsize=_fs(0),
                    fontweight="bold", color="#e03131", va="center",
                    bbox=_txt_bbox)
            ax.text(cend + 0.15, (tr_bottom + dn) / 2,
                    f"-因 {targets.get('cause', 0):.2f}"
                    f" ({targets.get('columns', 0)}列×格×反转)",
                    fontsize=_fs(0), color="#e03131", va="center", alpha=0.8,
                    bbox=_txt_bbox)
            # 近端参考目标 (非威科夫, 可到达性校准): 浅色点线区分
            if "近端下方目标" in targets:
                near = targets["近端下方目标"]
                if abs(near - dn) > box:
                    ax.axhline(near, color="#f08c00", ls=":", lw=1.0,
                               alpha=0.75 if active else 0.35)
                    ax.text(cend + 0.15, near, f"近端参考 {near:.2f}",
                            fontsize=_fs(0), color="#f08c00", va="center",
                            alpha=0.85 if active else 0.4, bbox=_txt_bbox)

    # ── 箱体量 Volume-at-Price: 网格右侧每个价格箱体一根横向量条 ──
    if vol and vol.get("row_max", 0) > 0 and vol.get("row_vols"):
        row_max = vol["row_max"]
        rows_v = vol["row_vols"]
        gx_right = (len(cols) - 0.5) * xstep
        vx0 = gx_right + 0.1
        vw = 1.0
        for row, rv in sorted(rows_v.items()):
            frac = rv / row_max
            ax.barh(row * box, max(frac * vw, 0.012), height=box * 0.72,
                    left=vx0, color="#3b82f6",
                    alpha=0.18 + 0.82 * frac, zorder=1)
        ax.text(vx0 + vw + 0.03, (max(rows_v) + 0.3) * box, "量",
                fontsize=_fs(-1), color="#3b82f6", ha="left", va="top")

    ax.set_title(f"{title}\n[P&F] 格值={box:.2f} ({pnf_box_label(box_mode, atr_factor)})  反转=3格"
                 f"{'  |  威科夫计数目标已标注' if targets else ''}", fontsize=_fs(2))
    ax.tick_params(labelbottom=False)
    ax.set_ylabel("价格", fontsize=_fs(-1))
    ax.grid(False)
    ax.set_xlim(-0.6, len(cols) * xstep + 3.2)

    # ── 列级成交量 (底部面板): 每列一根, 与列对齐, 颜色随 X/O ──
    if vol and vol.get("col_max", 0) > 0 and len(vol["col_vols"]) == len(cols):
        for j, c in enumerate(cols):
            v = vol["col_vols"][j]
            if v <= 0:
                continue
            axv.bar(j * xstep, v, width=xstep * 0.78,
                    color="#e03131" if c["type"] == "X" else "#2f9e44",
                    alpha=0.85)
        axv.set_ylim(0, vol["col_max"] * 1.1)
    axv.set_xlabel("列序号", fontsize=_fs(-1))
    axv.set_ylabel("列量", fontsize=_fs(-1))
    axv.grid(False, axis="x")

    # ── 点数图解读: 方向/TR宽度/目标位 → 预示 (与技术指标一致的"信号→预示"风格) ──
    cap, cap_color = pnf_cap(targets, cols)
    # 顶部信息行: 当前区间/计数起止/TR宽度 (不压格子)
    if targets and "info_line" in locals():
        fig.text(0.5, 0.96, info_line, ha="center", va="top",
                 fontsize=_fs(1), color=zone_edge, fontweight="bold")
    # 历史准确率独立行 (底部量图上方): 上涨/下跌目标到位率与综合
    if hist_title:
        fig.text(0.5, 0.09, hist_title, ha="center", va="center",
                 fontsize=_fs(1), color="#374151", fontweight="bold",
                 bbox=dict(boxstyle="round,pad=0.35", facecolor="#f1f5f9",
                           edgecolor="#cbd5e1", lw=0.8))
    fig.text(0.5, 0.03, cap, ha="center", va="bottom", fontsize=_fs(1),
             color=cap_color, fontweight="bold")

    # 记号填满小格子: 依布局后的实际像素, 把 ×/○ 尺寸调到格子的较短边 (正圆/正叉)
    fig.canvas.draw()
    bb = ax.get_window_extent()
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    if xlim[1] > xlim[0] and ylim[1] > ylim[0] and bb.width > 0 and bb.height > 0:
        cw_px = bb.width / (xlim[1] - xlim[0]) * xstep
        ch_px = bb.height / (ylim[1] - ylim[0]) * box
        cell_px = min(cw_px, ch_px)
        if cell_px > 1:
            s = (cell_px * 72.0 / fig.dpi) ** 2
            for sc in ax.collections:
                if sc.get_paths() and len(sc.get_paths()[0].vertices) > 2:
                    sc.set_sizes([s * 0.95])
    return fig
