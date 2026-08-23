"""L5 威科夫语境特征: 为每个事件补写发生时的结构性上下文。

动机: L4 在线校准模型的特征全是通用 K 线技术量 (量比/波幅/布林位...),
而实证表明同类信号在不同威科夫语境下胜率天差地别 (Spring 84.9% 但
SOS/JOC ≈ 随机)。这里把结构语境注入 feat, 让校准模型从"类型平均胜率"
进化为"条件化可靠度"。

特征清单 (全部只用信号 bar 当日及之前的数据, 回填与实盘同一代码路径,
天然无前视):
    ph_acc / ph_dis / ph_mup / ph_mkd  事件所处阶段 one-hot (区间/未知全 0)
    tr_pos        收盘在交易区间内的位置 (0=带底, 1=带顶; 无区间 0.5)
    tr_age_n      区间年龄 (bar 数/150, 封顶 1)
    tr_wid_n      区间宽度分数 ((top/bottom-1) 封顶 [0,1])
    base_len_n    因果长度: 距同向高潮 (多看 SC/空看 BC) 的 bar 数/120
    vol_shrink    量能萎缩比: 近5根均量 ÷ 此前55根内最大5根均量 (封顶[0,1])
    rs_pct        RS20 超额收益在近 120 日的百分位 ([0,1]; 缺数据 0.5)
    idx_align     指数趋势状态 × 事件方向 (-1/0/+1; 缺数据 0)
    sec_pct       板块强度百分位 (预留, 当前恒缺省 0.5 安全填充)

设计约束:
    - 严格无前视: 阶段/区间结构按"信号日后 _MARGIN 根"的前缀切片重算
      (phase_segments 会用阶段带之后的行情做验证, 直接全量计算会把未来
      泄漏进历史事件特征); vol_shrink/base_len/rs_pct/idx_align 的窗口
      本就只看 ≤信号日 的数据。
    - enrich 只就地修改 e["feat"], 不改变检测/置信度逻辑; 内部全程
      try/except, 任何失败仅导致对应特征缺省, 绝不影响原流程。
    - 网络获取 (指数序列) 受 WYCKOFF_NO_NET=1 门控, 测试环境离线降级。
"""
import os

import numpy as np

# 语境特征键全集 (online_model / backfill_ctx 共用)
CONTEXT_FEAT_KEYS = (
    "ph_acc", "ph_dis", "ph_mup", "ph_mkd",
    "tr_pos", "tr_age_n", "tr_wid_n",
    "base_len_n", "vol_shrink", "rs_pct", "idx_align", "sec_pct",
)

# feature_vector 缺失安全填充值 (与上文注释一一对应)
SAFE_FILL = {
    "tr_pos": 0.5,
    "rs_pct": 0.5,
    "sec_pct": 0.5,
    "vol_shrink": 0.25,   # 无信息 ≈ 无萎缩
    "idx_align": 0.0,
}

_SEG_KEY_DUMMIES = {
    "accumulation": "ph_acc",
    "distribution": "ph_dis",
    "markup": "ph_mup",
    "markdown": "ph_mkd",
}


def _seg_at(segs, i):
    """返回覆盖索引 i 的阶段段 key, 无则 None。"""
    for a, e, key, _label in segs:
        if a <= i <= e:
            return key
    return None


# 前缀切片余量: 覆盖枢轴确认所需的右侧 bar 数 (find_pivots order 默认 6)
_MARGIN = 12


def _prefix_structs(df, pivots, m, cache):
    """df[:m] 前缀上的 (阶段段, 区间) 结构; cache 为单次 enrich 调用内的 {m: 结果}。

    枢轴只保留 idx+order < m 的 (更晚的枢轴在信号时点尚未确认, 真实时点不可见)。"""
    from .phases import _detect_ranges, phase_segments
    hit = cache.get(m)
    if hit is not None:
        return hit
    sub = df.iloc[:m]
    pv = [p for p in pivots if int(p.get("idx", 0)) + 6 < m] \
        if pivots else []
    segs = phase_segments(sub, pv) or []
    ranges = _detect_ranges(sub, pv) or []
    out = (segs, ranges, sub)
    cache[m] = out
    return out


def _range_at(ranges, i):
    """返回覆盖索引 i 的区间 (a, e, top, bottom), 无则 None。"""
    for r in ranges:
        if r[0] <= i <= r[1]:
            return r
    return None


def _phase_dummies(segs, i):
    out = {"ph_acc": 0.0, "ph_dis": 0.0, "ph_mup": 0.0, "ph_mkd": 0.0}
    key = _seg_at(segs, i)
    d = _SEG_KEY_DUMMIES.get(key)
    if d:
        out[d] = 1.0
    return out


def _tr_feats(ranges, close_i, i):
    out = {"tr_pos": None, "tr_age_n": None, "tr_wid_n": None}
    r = _range_at(ranges, i)
    if not r:
        return out
    a, e, top, bottom = r
    if bottom and top > bottom:
        pos = (close_i - bottom) / (top - bottom)
        out["tr_pos"] = float(min(1.0, max(0.0, pos)))
        wid = float(top / bottom - 1)
        out["tr_wid_n"] = min(1.0, max(0.0, wid))
        age = (i - a) / 150.0
        out["tr_age_n"] = round(min(1.0, max(0.0, age)), 4)
    return out


def _base_len(events, e):
    """距最近同向高潮的 bar 数 (封顶 120), 归一化到 [0,1]; 找不到 → 0。"""
    from .config import event_dir
    i = int(e.get("idx", 0))
    d = event_dir(e.get("type", "")) or 0
    want = "BC" if d < 0 else "SC"
    best = None
    for o in events:
        if o is e or o.get("type") != want:
            continue
        dist = i - int(o.get("idx", i))
        if 0 < dist <= 120 and (best is None or dist < best):
            best = dist
    return round(min(1.0, (best or 0) / 120.0), 4)


def _vol_shrink(volume, i):
    """近5根均量 / 此前55根内最大滚动5根均量, clip[0,4]/4。历史不足 → None。"""
    if volume is None or i < 60:
        return None
    v = np.asarray(volume, dtype=float)
    recent = float(np.mean(v[i - 4:i + 1]))
    if not np.isfinite(recent) or recent <= 0:
        return None
    prior = v[max(0, i - 59):i - 4]
    if len(prior) < 20:
        return None
    # 此前窗口内最大滚动5根均量
    c = np.cumsum(np.insert(prior, 0, 0.0))
    roll5 = (c[5:] - c[:-5]) / 5.0
    peak = float(roll5.max()) if len(roll5) else 0.0
    if peak <= 0 or not np.isfinite(peak):
        return None
    ratio = recent / peak
    return round(min(4.0, max(0.0, ratio)) / 4.0, 6)


def _rs_pct(rs_series, i):
    """RS 值在近 120 日窗口内的百分位 ([0,1]); 数据不足 → None。"""
    if rs_series is None or i >= len(rs_series):
        return None
    cur = rs_series[i]
    if cur is None or not np.isfinite(cur):
        return None
    trail = rs_series[max(0, i - 119):i + 1]
    trail = trail[np.isfinite(trail)]
    if len(trail) < 40:
        return None
    return round(float((trail < cur).mean()), 4)


def _idx_regime(index_df, day_i):
    """指数在 day_i 当日的趋势状态: +1 多头 / -1 空头 / 0 震荡或缺数据。"""
    if index_df is None or "close" not in index_df or len(index_df) < 55:
        return 0
    days = index_df["day"]
    try:
        j = int(days.searchsorted(day_i, side="right")) - 1
    except Exception:
        return 0
    if j < 50:
        return 0
    cl = index_df["close"].values
    ma20 = index_df["price_ma20"].values if "price_ma20" in index_df \
        else index_df["close"].rolling(20).mean().values
    ma50 = index_df["price_ma50"].values if "price_ma50" in index_df \
        else index_df["close"].rolling(50).mean().values
    above = cl[j] > ma50[j] and ma20[j] >= ma50[j]
    below = cl[j] < ma50[j] and ma20[j] <= ma50[j]
    return 1 if above else (-1 if below else 0)


def _load_index_df():
    """取上证指数日线序列 (30 分钟进程内缓存); 失败返回 None。"""
    if os.environ.get("WYCKOFF_NO_NET") == "1":
        return None
    try:
        from .market import fetch_market_series
        return fetch_market_series()
    except Exception:
        return None


def enrich(df, pivots, events, index_df=None):
    """为事件列表就地补写 L5 语境特征 (写入 e["feat"])。

    df/pivots 与检测管线一致; events 为 detect_all 产物 (含 feat.dir)。
    index_df 可传入已取好的指数日线, 缺省时按需拉取 (受 WYCKOFF_NO_NET 门控)。
    """
    try:
        if not events or df is None or len(df) == 0:
            return
        close = df["close"].values
        vol = df["volume"].values if "volume" in df else None
        cache = {}

        rs_series = None
        if index_df is None:
            index_df = _load_index_df()
        if index_df is not None:
            try:
                from .market import relative_strength_series
                rs_series = relative_strength_series(df, index_df, window=20)
            except Exception:
                rs_series = None

        for e in events:
            try:
                feat = e.setdefault("feat", {})
                i = int(e.get("idx", 0))
                if i < 0 or i >= len(close):
                    continue
                # 严格因果: 阶段/区间结构按"信号日后 MARGIN 根"的前缀重算。
                # phase_segments/_detect_ranges 会用阶段带之后的行情做验证,
                # 直接用全量 df 会把未来信息泄漏进历史事件特征 (回填尤其致命)。
                m = min(len(df), i + 1 + _MARGIN)
                segs_i, ranges_i, _sub = _prefix_structs(df, pivots, m, cache)
                feat.update(_phase_dummies(segs_i, i))
                trf = _tr_feats(ranges_i, float(close[i]), i)
                for k, v in trf.items():
                    feat[k] = round(v, 4) if v is not None else None
                feat["base_len_n"] = _base_len(events, e)
                feat["vol_shrink"] = _vol_shrink(vol, i)
                feat["rs_pct"] = _rs_pct(rs_series, i)
                regime = _idx_regime(index_df, df["day"].iloc[i])
                d = feat.get("dir")
                if d is None:
                    from .config import event_dir
                    d = event_dir(e.get("type", ""))
                feat["idx_align"] = int(regime * (1 if d and d > 0 else (-1 if d else 0)))
                feat.setdefault("sec_pct", None)
            except Exception:
                continue
    except Exception:
        pass
