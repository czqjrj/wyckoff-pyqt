"""多维度信号融合: K线结构 / 威科夫事件 / VSA量价 / P&F点数图 → 统一多空评分。

设计动机: 各模块 (phase/events/vsa/pnf) 独立输出各自的多空判断, 结论区并列
罗列, 但缺少交叉验证——某个维度强多 + 另一维度强空时, 用户得不到明确结论。
本模块把四类信号量化成统一评分轴 (-100~+100), 加权合成综合多空倾向与
置信度, 并显式报告"哪几维共振 / 哪几维矛盾", 供结论区与信号汇总使用。

评分约定: >0 偏多, <0 偏空, |分| 越大信号越强; 置信度由"同向维度数 +
信号强度一致性"决定: 全部同向且强 → 高, 部分同向 → 中, 方向分裂 → 低。
"""

import json
import os

from .config import VSA_BEAR, VSA_BULL, W_RECENT, event_dir
from .paths import DATA_DIR

# 各维度权重 (K线结构最重, 威科夫事件次之, VSA与P&F辅助确认)
W_KLINE = 0.35
W_EVENT = 0.30
W_VSA = 0.20
W_PNF = 0.15

# 新闻情绪维度权重 (小权重探索: 无 A/B 实证样本前不喧宾夺主), 乘以 _news_cal_factor()
# 自校准因子: accuracy.update_news_calibration 依据已评估样本中 news_score 与实际
# 收益方向一致性落盘 (命中显著低于基线→缩权, 高于→放权), 由数据决定预测力。
W_NEWS = 0.05

# 新闻情绪参与评分的绝对强度门槛: |score|<0.3 视为弱情绪 (关键词噪声为主),
# 不接入评分 (仅展示); 只有强情绪才进入融合维度。
NEWS_MIN_ABS = 0.30

# 前瞻风险窗口: 未来 N 日内有解禁/定期报告披露等偏空节点时, 多头综合信号降一档
# 置信并提示 (事前排雷优于事后解释)。
FORWARD_RISK_DAYS = 7

# 新闻维度自校准因子文件: 当前 paths.py 未提供 NEWS_CALIBRATION_FILE 常量,
# 按参考语义内联到用户数据目录 (accuracy.update_news_calibration 落盘此处)。
NEWS_CALIBRATION_FILE = os.path.join(DATA_DIR, "wx_news_calibration.json")

# 新闻维度自校准因子缓存: 按校准文件 mtime 热加载 (修改即生效, 无需重启)
_news_cal_cache = {"mtime": None, "factor": 1.0}


def _news_cal_factor():
    """读取新闻维度自动校准因子 [0.4~1.5], 文件缺失/损坏时返回 1.0。"""
    try:
        mt = os.path.getmtime(NEWS_CALIBRATION_FILE)
    except OSError:
        return 1.0
    if _news_cal_cache["mtime"] != mt:
        try:
            with open(NEWS_CALIBRATION_FILE, encoding="utf-8") as f:
                fac = float(json.load(f).get("factor", 1.0))
            _news_cal_cache["factor"] = max(0.4, min(1.5, fac))
        except Exception:
            _news_cal_cache["factor"] = 1.0
        _news_cal_cache["mtime"] = mt
    return _news_cal_cache["factor"]

# VSA 标签方向采用 config 统一映射 (config.vsa_dir / VSA_BULL/VSA_BEAR/VSA_NEUTRAL,
# 语义核对 vsa._DESC / VSA_CN) —— 与 backtest_vsa / 状态栏头条 / 胜率方向化同源。
# 历史备注: 早期按旧实测把 UPT 归过多头, 新方向化口径 (20根 UPT 方向命中≈49%,
# 贴近随机) 后按经典语义归空头 (上冲量=诱多); TEST/SC/SV/NS 归多头,
# ND/BC/UT/SUP/UPT/ETF/TRU 归空头, ABS/CHOC/ER/EF/EVR/N 中性不计入。
# 威科夫事件方向由 config.event_dir 提供 (多头/空头/中性)
BULL_PHASES = ("底部整固", "上升趋势")
BEAR_PHASES = ("顶部构筑", "下跌趋势")

# 历史胜率校准: 是否把 signal_accuracy 实测胜率作为置信权重接入各维度评分
# (win 相对基准 0.5 的偏移量级 → 0.5~1.5 倍权重; 样本不足的类型不校准)。
USE_WINRATE_CALIBRATION = True


def _winrate_weight(kind, type_, direction=0, baseline=0.5, before_ts=None):
    """按历史实测方向一致性给信号置信加权。

    direction>0 (多头信号): 历史方向命中 (上涨) 占比越高越可信 → 权重增大。
    direction<0 (空头信号): 历史方向命中 (下跌) 占比越高越可信 → 权重增大。
    (win_rate_of / win_rate_of_oos 已返回方向化命中占比, 空头信号以跌记中。)
    before_ts: 样本外校准 —— 只统计该信号出现之前的样本 (消除"用未来数据
    校准当前信号权重"的前瞻偏差); None 时用全历史 (含未来, 有轻微前瞻)。
    返回 [0.5, 1.5] 区间系数。样本不足/校准关闭 → 1.0。
    """
    if not USE_WINRATE_CALIBRATION:
        return 1.0
    try:
        if before_ts is not None:
            from .signal_accuracy import load_signals
            from .validation import win_rate_of_oos
            win = win_rate_of_oos(load_signals(), kind, type_, before_ts,
                                  horizon=20, baseline=baseline)
        else:
            from .signal_accuracy import win_rate_of
            win = win_rate_of(kind, type_, horizon=20, baseline=baseline)
        if direction == 0:
            return 1.0
        alignment = win - baseline
        return max(0.5, min(1.5, 1.0 + alignment * 2.5))
    except Exception:
        return 1.0


def _event_score(events, recent_window=None, max_idx=None, oos=False):
    """威科夫事件维度: 最近 recent_window 根内 (以 max_idx 为当前时点,
    默认取事件中最大 idx), 按方向×置信度加权, 越近权重越高。
    返回 (-100~+100)。"""
    if not events:
        return 0.0
    if recent_window is None:
        recent_window = W_RECENT
    if max_idx is None:
        max_idx = max(e.get("idx", 0) for e in events)
    score = 0.0
    n_used = 0
    for e in events:
        idx = e.get("idx", 0)
        if idx < 0:
            continue
        d = event_dir(e.get("type", ""))
        if d == 0:
            continue
        conf = e.get("conf", 50) / 100.0
        dist = max_idx - idx
        if dist < 0 or dist > recent_window:
            continue
        # 越近权重越高; 保留绝对强度 (单事件也随新旧衰减), 避免被归一化抹平
        decay = (1.0 - dist / recent_window) ** 1.5
        # 弱信号折扣: SOS/JOC 突破日信号实测命中率贴近基准甚至反向 (37股回测
        # SOS 48.8% / JOC 44.2% vs 基准47.6%, Spring 83% / ST 78%),
        # 给半权重, 避免弱信号淹没强信号。
        strength = 0.5 if e.get("type") in ("SOS", "JOC") else 1.0
        # 跟进确认: 仅当事件携带 confirmed 字段时应用 (detect_all 输出),
        # 手工构造/旧数据没有该字段 → 权重不变。已确认 ×1.2 / 未确认 ×0.5 /
        # 待确认 ×0.9。研究结论: "只在确认后进场, 不在信号当根进场"——
        # 未获后续价格确认的孤立信号不可靠, 降权避免污染融合。
        if "confirmed" in e:
            if e["confirmed"] is True:
                strength *= 1.2
            elif e["confirmed"] is False:
                strength *= 0.5
            else:
                strength *= 0.9
        # 历史胜率校准: 方向一致的高胜率信号加权, 反向/弱命中信号降权;
        # oos=True 时只用该事件日期之前的历史样本 (消除前瞻偏差)。
        before_ts = e.get("date") if oos else None
        strength *= _winrate_weight("event", e.get("type", ""),
                                    direction=d, before_ts=before_ts)
        score += d * conf * decay * strength
        n_used += 1
    if n_used <= 0:
        return 0.0
    return max(-100.0, min(100.0, score / n_used * 100.0))


def _vsa_score(vsa_signals, recent_window=None, max_idx=None, oos=False):
    """VSA 维度: 最近 recent_window 根内信号 (以 max_idx 为当前时点),
    多空标签各计数加权。返回 (-100~+100)。"""
    if not vsa_signals:
        return 0.0
    if recent_window is None:
        recent_window = W_RECENT
    if max_idx is None:
        max_idx = max(s.get("idx", 0) for s in vsa_signals)
    bull = bear = 0.0
    for s in vsa_signals:
        idx = s.get("idx", 0)
        dist = max_idx - idx
        if dist < 0 or dist > recent_window:
            continue
        lb = s.get("label", "")
        w = 1.0 + (recent_window - dist) / recent_window  # 近期更重
        before_ts = s.get("date") if oos else None
        if lb in VSA_BULL:
            w *= _winrate_weight("vsa", lb, direction=1, before_ts=before_ts)
            bull += w
        elif lb in VSA_BEAR:
            w *= _winrate_weight("vsa", lb, direction=-1, before_ts=before_ts)
            bear += w
    total = bull + bear
    if total <= 0:
        return 0.0
    return (bull - bear) / total * 100.0


def _pnf_score(pnf_t, last_close):
    """P&F 维度: 方向 + 是否到位 + TR 内位置。
    向上且未到位→看多; 向下且未到位→看空; 已到位→信号衰减(空间耗尽);
    区间内→按价格在 TR 中的位置微调。返回 (-100~+100)。"""
    if not pnf_t:
        return 0.0
    direction = pnf_t.get("direction", "range")
    tr_top = pnf_t.get("tr_top")
    tr_bottom = pnf_t.get("tr_bottom")
    ups = sorted(v for k, v in pnf_t.items()
                 if k.endswith("上方目标") and isinstance(v, (int, float)))
    dns = sorted((v for k, v in pnf_t.items()
                  if k.endswith("下方目标") and isinstance(v, (int, float))),
                 reverse=True)
    if direction == "up" and ups:
        tgt = ups[0]
        return 60.0 if last_close < tgt else 15.0  # 已到位→空间耗尽
    if direction == "down" and dns:
        tgt = dns[0]
        # P&F 向下破位实测方向命中偏弱甚至反向 (20根54%涨/40根64%涨), 仅作弱空提示,
        # 不当作强空头依据 (回调/破位失败占比高)。
        return -15.0 if last_close > tgt else 0.0
    if direction == "range" and tr_top and tr_bottom and tr_top > tr_bottom:
        pos = (last_close - tr_bottom) / (tr_top - tr_bottom)
        return (pos - 0.5) * 40.0  # 区间内: 偏上沿→弱多, 偏下沿→弱空
    return 0.0


def _kline_score(phase, df, events):
    """K线结构维度: 阶段方向为主, 均线/突破事件修正。
    返回 (-100~+100)。"""
    base = phase.split(" ")[0]
    if base in BULL_PHASES:
        score = 50.0
    elif base in BEAR_PHASES:
        score = -50.0
    else:
        score = 0.0
    # 均线修正: 多头排列+15 / 空头排列-10 (实测两者方向命中率都贴近基准甚至
    # 反向——多头排列20根44%、空头40根46.5%, 均线排列不是可靠的方向信号,
    # 大幅降权避免误导, 方向主要交给阶段与事件)
    try:
        ma20 = df["price_ma20"].iloc[-1]
        ma50 = df["price_ma50"].iloc[-1]
        last = float(df["close"].iloc[-1])
        if last > ma20 > ma50:
            score += 15.0
        elif last < ma20 < ma50:
            score -= 10.0
    except Exception:
        pass
    # 突破/破位事件修正 (K线层面确认)
    recent = [e for e in events if e["idx"] >= len(df) - 60]
    for e in recent:
        d = event_dir(e.get("type", ""))
        if d > 0:
            score += 15.0 * (e.get("conf", 50) / 100.0)
        elif d < 0:
            score -= 15.0 * (e.get("conf", 50) / 100.0)
    return max(-100.0, min(100.0, score))


def _htf_direction(mf):
    """从多周期结果提取高周期方向: +1 周/月线偏多, -1 偏空, 0 无/中性。"""
    if not mf:
        return 0
    sig = 0
    for key in ("weekly_phase", "monthly_phase"):
        ph = (mf.get(key) or "").split(" ")[0]
        if ph in BULL_PHASES:
            sig += 1
        elif ph in BEAR_PHASES:
            sig -= 1
    return 1 if sig > 0 else -1 if sig < 0 else 0


def _align(score, htf):
    """高周期对齐修正: 与周/月线方向一致 ×1.2, 冲突 ×0.6, 无参照不变。
    研究结论: "优先选择与高周期方向一致的信号, 冲突则降仓/放弃"——
    顺大势的信号胜率更高, 逆势突破多为假信号。"""
    if htf == 0 or score == 0:
        return score
    align = (score > 0) == (htf > 0)
    return score * (1.2 if align else 0.6)


def fuse_signals(df, phase, events, vsa_signals, pnf_t, mf=None, oos=False,
                 news_sentiment=None, forward_calendar=None):
    """融合四类信号, 返回综合评分与维度明细。

    参数: 与各模块输出直接兼容 (analysis.py 中已有)。
      news_sentiment: 新闻情绪结果 (建议先经 news.apply_price_validation 验证,
        含 items 明细与 validation 摘要; 旧格式 dict 也兼容)。
      forward_calendar: news.fetch_forward_calendar 的前瞻日历
        ({items, risk_days, risk_label}), 有临近偏空节点时多头信号降置信。
    返回 dict:
      score     综合评分 (-100~+100, >0 偏多)
      bias      看多 / 看空 / 中性
      confidence 高 / 中 / 低
      dims      [{key,name,score,bias,detail}]
      resonances [{names...}] 同向共振维度组
      conflicts [{name_a,name_b,note}] 矛盾说明
      summary   一行文本 (供结论区/状态栏)
    """
    last_close = float(df["close"].iloc[-1])
    max_idx = len(df) - 1
    htf = _htf_direction(mf)
    dims = [
        {"key": "kline", "name": "K线结构",
         "score": _align(_kline_score(phase, df, events), htf),
         "detail": phase.split(" ")[0]},
        {"key": "event", "name": "威科夫事件",
         "score": _align(_event_score(events, max_idx=max_idx, oos=oos), htf),
         "detail": f"近期{sum(1 for e in events if max_idx - e['idx'] <= 120)}个事件"},
        {"key": "vsa", "name": "VSA量价",
         "score": _align(_vsa_score(vsa_signals, max_idx=max_idx, oos=oos), htf),
         "detail": f"近期{len([s for s in (vsa_signals or []) if max_idx - s.get('idx', 0) <= 120])}个信号"},
        {"key": "pnf", "name": "P&F点数图",
         "score": _pnf_score(pnf_t, last_close),
         "detail": pnf_t.get("direction", "range") if pnf_t else "无"},
    ]
    # 新闻情绪维度: 打分 → 价格验证(上游) → 事件共振 → 自校准因子
    news_score = 0.0
    news_detail = "无"
    if news_sentiment:
        ns = news_sentiment.get("score", 0.0)
        val = news_sentiment.get("validation") or {}
        # 弱情绪 (|score|<NEWS_MIN_ABS) 未经验证, 不参与评分; 仅强情绪接入。
        if abs(ns) >= NEWS_MIN_ABS:
            news_score = ns * 100.0  # -1~1 → -100~100
        cnt = news_sentiment.get("count", 0)
        ann = news_sentiment.get("ann_count")
        if ann is not None:
            cnt_txt = f"{ann}公告+{cnt - ann}资讯"
        else:
            cnt_txt = f"{cnt}条"
        news_detail = f"情绪{ns:+.2f}·{cnt_txt}"
        if news_sentiment.get("key_events"):
            news_detail += f"·利好{len(news_sentiment['key_events'])}"
        if news_sentiment.get("risk_flags"):
            news_detail += f"·利空{len(news_sentiment['risk_flags'])}"
        # 价格反应验证注记: 证伪条目已在上游 apply_price_validation 中降权,
        # 此处仅展示市场投票结果 (✓确认/✗证伪)。
        if val.get("confirmed") or val.get("rejected"):
            news_detail += (f"·价验{val.get('confirmed', 0)}✓"
                            f"/{val.get('rejected', 0)}✗")
        # 事件共振: 新闻方向与近期威科夫事件互证加分 / 背离减分;
        # Spring+利空(吓筹)/UTAD+利好(诱多) 按复合人行为学反向解读。
        try:
            from .news import event_resonance
            res_bonus, res_note = event_resonance(news_sentiment, events, max_idx)
        except Exception:
            res_bonus, res_note = 0.0, ""
        if res_bonus:
            news_score = max(-100.0, min(100.0, news_score + res_bonus))
            news_detail += f"·{res_note}"
        # 自校准因子: 历史样本实测的新闻预测力缩放 W_NEWS 有效贡献
        cal_f = _news_cal_factor()
        if cal_f != 1.0 and news_score:
            news_score *= cal_f
            news_detail += f"·校准×{cal_f:.2f}"
    dims.append({
        "key": "news", "name": "新闻情绪",
        "score": _align(news_score, htf),
        "detail": news_detail,
    })
    # 前瞻风险窗口: 解禁/财报披露临近时在新闻维度标注 (置信度调整见 bias 之后)
    risk_days = None
    risk_label = ""
    if isinstance(forward_calendar, dict):
        rd = forward_calendar.get("risk_days")
        if rd is not None:
            try:
                risk_days = int(rd)
                risk_label = str(forward_calendar.get("risk_label") or "风险窗口")
            except Exception:
                risk_days = None
        if forward_calendar.get("items"):
            nxt = forward_calendar["items"][0]
            dims[-1]["detail"] += (f"·前瞻{nxt['kind']}{nxt['date'][5:]}" if risk_days is None
                                   else f"·⚠{risk_label[:18]}({risk_days}天)")
    if htf != 0:
        for d in dims:
            if d["score"] != 0 and ((d["score"] > 0) == (htf > 0)):
                d["detail"] += "·顺高周期"
            elif d["score"] != 0:
                d["detail"] += "·逆高周期"
    for d in dims:
        d["bias"] = "看多" if d["score"] > 10 else "看空" if d["score"] < -10 else "中性"

    score = (dims[0]["score"] * W_KLINE + dims[1]["score"] * W_EVENT
             + dims[2]["score"] * W_VSA + dims[3]["score"] * W_PNF
             + dims[4]["score"] * W_NEWS)
    score = max(-100.0, min(100.0, score))

    # bias 阈值 ±8 (与 phase_tone 对齐, 减少 fusion_bias 过度中性化)
    bias = "看多" if score > 8 else "看空" if score < -8 else "中性"
    strong_bull = sum(1 for d in dims if d["bias"] == "看多")
    strong_bear = sum(1 for d in dims if d["bias"] == "看空")
    if score > 40 and strong_bear == 0:
        confidence = "高"
    elif score < -40 and strong_bull == 0:
        confidence = "高"
    elif (score > 8 or score < -8) and max(strong_bull, strong_bear) >= 2:
        confidence = "中"
    else:
        confidence = "低"

    resonances = []
    if strong_bull >= 2:
        names = [d["name"] for d in dims if d["bias"] == "看多"]
        resonances.append(names)
    if strong_bear >= 2:
        names = [d["name"] for d in dims if d["bias"] == "看空"]
        resonances.append(names)

    conflicts = []
    if strong_bull and strong_bear:
        bull_names = [d["name"] for d in dims if d["bias"] == "看多"]
        bear_names = [d["name"] for d in dims if d["bias"] == "看空"]
        conflicts.append({
            "bull": bull_names, "bear": bear_names,
            "note": f"{'、'.join(bull_names)} 与 {'、'.join(bear_names)} 方向矛盾, 信号分歧",
        })

    # 前瞻风险窗口惩罚: 解禁/定期报告披露等偏空节点临近 (FORWARD_RISK_DAYS 内)
    # 时, 多头综合信号降一档置信并显式提示 —— 财报雷/解禁抛压常让技术形态失效,
    # 事前排雷优于事后解释。空头信号不受影响 (风险窗口与看空方向同向)。
    if risk_days is not None and risk_days <= FORWARD_RISK_DAYS and score > 8:
        if confidence == "高":
            confidence = "中"
        elif confidence == "中":
            confidence = "低"
        bull_names = [d["name"] for d in dims if d["bias"] == "看多"] or ["综合"]
        conflicts.append({
            "bull": bull_names, "bear": ["前瞻日历"],
            "note": f"临近{risk_label} ({risk_days}天内), 多头信号降级",
        })

    verdict = {"score": round(score, 1), "bias": bias,
               "confidence": confidence, "dims": dims,
               "resonances": resonances, "conflicts": conflicts,
               "htf": htf,
               "summary": _summary_text(score, bias, confidence, dims, conflicts, htf)}
    return verdict


def _summary_text(score, bias, confidence, dims, conflicts, htf=0):
    """生成一行摘要: 综合方向 + 置信 + 共振/矛盾说明。"""
    parts = [f"综合{'偏多' if score > 0 else '偏空' if score < 0 else '中性'} "
             f"{score:+.0f} ({confidence}置信)"]
    for d in dims:
        arrow = "↑" if d["score"] > 10 else "↓" if d["score"] < -10 else "→"
        parts.append(f"{d['name']}{arrow}")
    if htf != 0:
        parts.append("顺高周期" if (score > 0) == (htf > 0) else "逆高周期")
    if conflicts:
        parts.append("⚠ 方向矛盾")
    return "  ".join(parts)
