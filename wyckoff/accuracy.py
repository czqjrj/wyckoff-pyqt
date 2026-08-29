"""分析准确度跟踪: 记录每次分析的预测, 到期后用真实行情评估并汇总。

流程:
  1. record_analysis: 每次分析完成后, 把该股票的预测快照 (阶段/P&F方向/交易计划/目标价)
     写入 `~/.wyckoff/wx_accuracy.json` (按 symbol+scale+分析时点 去重)。
  2. run_auto_accuracy_eval / evaluate_pending: 拉取最新行情, 对已到期的记录
     计算 10/20/40 根后的真实收益与目标价命中, 回写结果。
  3. accuracy_stats / export_accuracy: 汇总方向命中率 (偏多/偏空阶段, P&F方向, 交易计划,
     目标价命中), 导出 JSON 供人工核查与校准阈值。

评估口径:
  - 方向正确: 偏多预测 (阶段/方向/计划为 bullish) → 未来 N 根收益 > 0;
              偏空预测 → 收益 < 0; 中性不计方向命中。
  - 目标命中: 窗口内最高价 >= 上方目标 (或最低价 <= 下方目标)。
"""
import json
import os
import statistics
import sys
import threading
import time

import numpy as np

from ._log import log_exc
from ._shared import atomic_write_json, run_pending_eval
from .analysis import build_trade_plan
from .config import VERSION, W_RECENT
from .datasource import fetch_kline
from .events import detect_all
from .fusion import fuse_signals
from .indicators import add_indicators, find_pivots
from .paths import ACCURACY_FILE, DATA_DIR
from .phases import judge_phase
from .pnf import build_pnf, pnf_targets
from .vsa import vsa_classify
from .waves import calc_targets

# 评估周期 (根): 约 2周/1月/2月 (日线)
HORIZONS = (10, 20, 40)

_LOCK = threading.Lock()


# ── 存取 ──
def _key(rec):
    return f"{rec.get('symbol')}|{rec.get('scale')}|{rec.get('ref_dt')}"


def load_accuracy():
    try:
        with open(ACCURACY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_accuracy(records):
    try:
        atomic_write_json(ACCURACY_FILE, records)
    except Exception as e:
        from ._log import log_exc
        log_exc("save_accuracy 落盘失败", e)


# ── 记录 ──
def capture_snapshot(df, symbol, code, scale, datalen, name="",
                     phase_label=None, conf_q=None, precomputed=None):
    """从分析用 df 提取预测快照 (不访问网络, 复用分析结果口径)。

    precomputed: 可选 dict, 已算好的管线中间结果 (pivots/events/phase/pnf_t/
    vsa_signals/fusion/targets/trade_plan), 由 run_analysis/scan_stock_signals
    传入, 避免每次记录时整条管线重算。缺失的键会自动补齐。
    """
    pivots = (precomputed or {}).get("pivots")
    events = (precomputed or {}).get("events")
    phase = (precomputed or {}).get("phase")
    pnf_t = (precomputed or {}).get("pnf_t")
    vsa_signals = (precomputed or {}).get("vsa_signals")
    fusion = (precomputed or {}).get("fusion")
    targets = (precomputed or {}).get("targets")
    trade_plan = (precomputed or {}).get("trade_plan")
    news_sentiment = (precomputed or {}).get("news_sentiment")
    news_score = round(news_sentiment.get("score", 0.0), 3) if news_sentiment else None
    # 新闻验证层明细: 价格反应验证计数与融合层新闻维度分 (供新闻贡献自校准)。
    news_val = (news_sentiment or {}).get("validation") or {} \
        if isinstance(news_sentiment, dict) else {}
    news_dim_score = None
    try:
        for _d in ((precomputed or {}).get("fusion") or {}).get("dims", []):
            if _d.get("key") == "news":
                news_dim_score = round(float(_d.get("score", 0.0)), 1)
                break
    except Exception:
        news_dim_score = None
    # 对照口径: 同一样本的"纯技术面融合" (剔除新闻维度), 用于评估新闻情绪贡献。
    fusion_no_news = None
    if news_score is not None:
        try:
            fusion_no_news = fuse_signals(df, phase, events, vsa_signals, pnf_t)
        except Exception:
            fusion_no_news = None
    # 缺失的管线键补齐 (调用方通常已算好大部分, 只有个别可缺省键需要兜底)。
    if pivots is None or events is None or phase is None:
        pivots = find_pivots(df, order=6)
        events = detect_all(df, pivots)
        phase, _detail = judge_phase(df, pivots, events)
    if pnf_t is None:
        pnf_cols, box = build_pnf(df)
        pnf_t = pnf_targets(df, pnf_cols, box)
    if vsa_signals is None:
        vsa_signals = vsa_classify(df, scale=scale)
    if fusion is None:
        fusion = fuse_signals(df, phase, events, vsa_signals, pnf_t)
    if targets is None:
        targets = calc_targets(df, pivots, events)
    last_close = float(df["close"].iloc[-1])
    if trade_plan is None:
        trade_plan = build_trade_plan(df, pivots, events, phase, None, targets,
                                      pnf_t, None, last_close)

    trade_dir = ""
    for ln in trade_plan:
        if ln.strip().startswith("方向:"):
            trade_dir = ln.split("方向:")[1].strip()
            break
    trade_tone = ("bullish" if "多头" in trade_dir else
                  "bearish" if "空头" in trade_dir else "neutral")

    if phase_label:
        phase = phase_label
    base_phase = phase.replace("高置信 ", "").replace(" (需谨慎)", "").split(" ")[0]
    phase_tone = {"底部整固": "bullish", "上升趋势": "bullish",
                  "顶部构筑": "bearish", "下跌趋势": "bearish"}.get(
        base_phase, "neutral")
    phase_conf = (conf_q if conf_q else
                  ("high" if phase.startswith("高置信")
                   else "caution" if "需谨慎" in phase else ""))

    pnf_dir = pnf_t.get("direction", "") if pnf_t else ""
    fusion_score = round(fusion.get("score", 0.0), 1)
    fusion_bias = fusion.get("bias", "中性")
    fusion_conf = fusion.get("confidence", "低")
    fusion_tone = ("bullish" if fusion_bias == "看多"
                   else "bearish" if fusion_bias == "看空" else "neutral")
    # 对照: 纯技术面融合的方向 (无新闻维度)。
    fusion_no_news_tone = None
    fusion_no_news_score = None
    if fusion_no_news:
        _fnb = fusion_no_news.get("bias", "中性")
        fusion_no_news_tone = ("bullish" if _fnb == "看多"
                               else "bearish" if _fnb == "看空" else "neutral")
        fusion_no_news_score = round(fusion_no_news.get("score", 0.0), 1)
    ups = sorted(v for k, v in pnf_t.items()
                 if k.endswith("上方目标") and isinstance(v, (int, float))) if pnf_t else []
    dns = sorted((v for k, v in pnf_t.items()
                  if k.endswith("下方目标") and isinstance(v, (int, float))),
                 reverse=True) if pnf_t else []
    up_target = next((float(v) for v in ups if v > last_close), None)
    down_target = next((float(v) for v in dns if v < last_close), None)

    recent = [e["type"] for e in events if e["idx"] >= len(df) - W_RECENT]
    return {
        "symbol": symbol,
        "code": code,
        "name": name or "",
        "scale": scale,
        "datalen": datalen,
        "ref_dt": df["day"].iloc[-1].strftime("%Y-%m-%d %H:%M"),
        "ref_close": round(last_close, 4),
        "phase": base_phase,
        "phase_tone": phase_tone,
        "phase_conf": phase_conf,
        "pnf_dir": pnf_dir,
        "fusion_score": fusion_score,
        "fusion_bias": fusion_bias,
        "fusion_conf": fusion_conf,
        "fusion_tone": fusion_tone,
        "news_score": news_score,
        "news_count": news_sentiment.get("count") if news_sentiment else None,
        "news_dim_score": news_dim_score,
        "news_confirmed": news_val.get("confirmed"),
        "news_rejected": news_val.get("rejected"),
        "fusion_no_news_tone": fusion_no_news_tone,
        "fusion_no_news_score": fusion_no_news_score,
        "trade_dir": trade_dir,
        "trade_tone": trade_tone,
        "up_target": round(up_target, 4) if up_target else None,
        "down_target": round(down_target, 4) if down_target else None,
        "events": list(dict.fromkeys(recent)),
        "n_bars": int(len(df)),
        "created_ts": time.time(),
        "last_eval_ts": 0,
        "status": "pending",
        "results": {},
    }


def record_analysis(df, symbol, code, scale, datalen, name="",
                    phase_label=None, conf_q=None, precomputed=None):
    """记录一次分析预测 (去重: 同 symbol+scale+时点 覆盖; 近1小时同标的快照合并)。

    precomputed: 复用调用方已算好的管线中间结果, 避免整条管线重复计算。"""
    rec = capture_snapshot(df, symbol, code, scale, datalen, name=name,
                           phase_label=phase_label, conf_q=conf_q,
                           precomputed=precomputed)
    # 顺带记录逐信号准确度快照 (事件 + VSA), 供 signal_accuracy 追踪每类信号命中。
    try:
        from .signal_accuracy import record_signals
        record_signals(df, symbol, code, scale, datalen, name=name)
    except Exception as e:
        from ._log import log_exc
        log_exc(f"record_signals({symbol}) 失败", e)
    # 顺带自动标注已走完的阶段带 (吸筹/拉升后上涨=正确, 派发/下跌后下跌=正确)。
    try:
        from .phases import phase_segments
        _piv = (precomputed or {}).get("pivots") or find_pivots(df, order=6)
        _ev = (precomputed or {}).get("events")
        auto_evaluate_feedback(df, symbol, int(scale),
                               phase_segments(df, _piv, _ev))
    except Exception as e:
        from ._log import log_exc
        log_exc(f"auto_evaluate_feedback({symbol}) 失败", e)
    with _LOCK:
        records = load_accuracy()
        key = _key(rec)
        for i, r in enumerate(records):
            if _key(r) == key:
                records[i] = rec
                save_accuracy(records)
                return rec
        # 日内自动刷新等: 近1小时同标的未评估快照直接替换 (避免高频刷屏)
        now = time.time()
        for i, r in enumerate(records):
            if r.get("symbol") == symbol and r.get("scale") == scale \
                    and not (r.get("results")) \
                    and now - (r.get("created_ts") or 0) < 3600:
                records[i] = rec
                save_accuracy(records)
                return rec
        records.append(rec)
        save_accuracy(records)
        return rec


# ── 阶段带自动反馈标注 ──
_FB_LOCK = threading.Lock()

# 阶段带评估窗口组: 多窗口综合判定取代单一最长窗口。
# 旧逻辑 (h_star=max(rets)) 仅用最长窗口定生死,
# 兑现周期内的短期反转易误标, 旧口径自动标注正确率仅 26.2%。
# 新逻辑: 多窗口多数票决 + 幅度加权, 大幅降低单窗口噪声影响。
FB_HORIZONS = (20, 40, 60)

# 多窗口判定时各窗口的权重 (短窗口噪声少但意义弱, 长窗口意义强但噪声多)
_FB_WEIGHTS = {20: 0.35, 40: 0.30, 60: 0.35}

# 阶段带"结束后延续方向"实测先验 (替代硬编码 accumulation/markup→涨, 其余→跌)。
# 硬编码先验忽略了均值回归: markup 段以局部高点收尾、其后多回落, markdown 段
# 以局部低点收尾、其后多反弹 (实测 markup 续涨仅~19%, markdown 续涨~81%)。
# 改用历史累计样本实测各阶段带的续变方向, 样本不足或方向不明时回退硬编码。
FB_PRIOR_MIN_N = 25      # 信任某标签先验的最少样本
FB_PRIOR_MIN_GAP = 0.05  # 偏离 50% 的最少幅度, 太小视为方向不明
# 硬编码回退方向: 吸筹/拉升→预期上涨, 派发/下跌→预期下跌
_FB_DEFAULT_DIR = {"accumulation": "up", "markup": "up",
                   "distribution": "down", "markdown": "down"}
_FB_PRIOR_CACHE = {"mtime": -1, "data": None}
_FB_PRIOR_LOCK = threading.Lock()


def _fb_verdict(rets, bullish, min_move=0.005):
    """多窗口综合判定: 多数窗口一致 + 幅度加权决定 verdict 与置信度。

    返回 (verdict, confidence, wdetails) 其中:
      verdict    — "correct"/"wrong"/None
      confidence — 0.0~1.0 (同向窗口幅度占比)
      wdetails   — {h: ("correct"|"wrong"|"neutral", ret)}
    """
    if not rets:
        return None, 0.0, {}
    # 各窗口独立判定
    wdv = {}
    for h, ret in rets.items():
        if abs(ret) < min_move:
            wdv[h] = ("neutral", ret)
        else:
            wdv[h] = ("correct" if (ret > 0) == bullish else "wrong", ret)
    # 有效窗口 (非 neutral)
    eff = {h: v for h, v in wdv.items() if v[0] != "neutral"}
    if eff:
        # 多数票决
        correct_votes = sum(1 for v, _ in eff.values() if v == "correct")
        wrong_votes = sum(1 for v, _ in eff.values() if v == "wrong")
        # 幅度加权
        correct_w = sum(abs(ret) * _FB_WEIGHTS.get(h, 0.25)
                        for h, (v, ret) in eff.items() if v == "correct")
        wrong_w = sum(abs(ret) * _FB_WEIGHTS.get(h, 0.25)
                      for h, (v, ret) in eff.items() if v == "wrong")
        total_w = correct_w + wrong_w
        confidence = max(correct_w, wrong_w) / total_w if total_w > 0 else 0.5
        verdict = "correct" if correct_votes >= wrong_votes else "wrong"
        return verdict, confidence, wdv
    # 全部窗口均为 neutral (小幅波动): 若所有窗口方向一致仍给出弱判定
    signs = [ret for _, ret in wdv.values()]
    if signs and all(s > 0 for s in signs):
        return "correct", 0.30, wdv
    if signs and all(s < 0 for s in signs):
        return "wrong", 0.30, wdv
    return None, 0.0, wdv


# ── 阶段带续变方向实测先验 ──
def update_fb_prior(feedback=None):
    """从已累计阶段带样本实测各标签的"结束后延续方向"先验并落盘。

    用原始收益方向 (fwd_ret 符号) 而非 verdict 统计, 避免"判定即自证"的循环:
      up_ratio[label] = 该标签样本中 fwd_ret>0 的占比 (样本取主判据窗口收益)。
      dir[label]      = "up" if up_ratio>=0.5 else "down" (含时点偏差, 训练样本内统计)。
    阈值: 样本数 < FB_PRIOR_MIN_N 或背离 50% 不足 FB_PRIOR_MIN_GAP 时标记
      dir=null, 由 fb_expected_direction 回退硬编码。落盘 wx_fb_prior.json
      供 auto_evaluate_feedback mtime 热加载 (与新闻校准同机制)。
    """
    from .paths import FB_PRIOR_FILE
    if feedback is None:
        from .storage import load_feedback
        feedback = load_feedback()
    stats = {}
    for r in feedback:
        lb = r.get("label")
        ret = r.get("fwd_ret")
        if not lb or lb not in _FB_DEFAULT_DIR or ret is None:
            continue
        s = stats.setdefault(lb, {"n": 0, "up": 0})
        s["n"] += 1
        s["up"] += int(ret > 0)
    payload = {"updated_ts": time.time(), "min_n": FB_PRIOR_MIN_N,
               "min_gap": FB_PRIOR_MIN_GAP, "labels": {}}
    for lb, s in stats.items():
        ratio = s["up"] / s["n"] if s["n"] else 0.5
        d = None
        if s["n"] >= FB_PRIOR_MIN_N and abs(ratio - 0.5) >= FB_PRIOR_MIN_GAP:
            d = "up" if ratio >= 0.5 else "down"
        payload["labels"][lb] = {"n": s["n"], "up": s["up"],
                                 "up_ratio": round(ratio, 4),
                                 "dir": d, "fallback": _FB_DEFAULT_DIR.get(lb)}
    try:
        atomic_write_json(FB_PRIOR_FILE, payload)
    except Exception as e:
        log_exc("update_fb_prior 落盘失败", e)
    return payload


def load_fb_prior():
    """按 mtime 热加载先验 (文件变化才重读), 失败返回空 dict。"""
    from .paths import FB_PRIOR_FILE
    try:
        mt = os.path.getmtime(FB_PRIOR_FILE)
    except OSError:
        return {}
    with _FB_PRIOR_LOCK:
        if _FB_PRIOR_CACHE["mtime"] == mt and _FB_PRIOR_CACHE["data"] is not None:
            return _FB_PRIOR_CACHE["data"]
    try:
        with open(FB_PRIOR_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    with _FB_PRIOR_LOCK:
        _FB_PRIOR_CACHE["mtime"] = mt
        _FB_PRIOR_CACHE["data"] = data
    return data


def fb_expected_direction(label):
    """给定阶段带 label, 返回实测先验期望方向 "up"/"down"。

    先验信不过 (样本不足/方向不明/文件缺失) 时回退硬编码 _FB_DEFAULT_DIR。
    """
    rec = (load_fb_prior().get("labels") or {}).get(label)
    if rec and rec.get("dir") in ("up", "down"):
        return rec["dir"]
    return _FB_DEFAULT_DIR.get(label, "up")


def auto_evaluate_feedback(df, symbol, scale, segs, horizon=20, min_move=0.005):
    """自动给"已走完"的阶段带打 正确/错误 反馈标注 (无需人工点击)。

    判定: 阶段带结束后 FB_HORIZONS 中所有已成熟窗口做多窗口综合判定 ——
    多数窗口一致 + 幅度加权决定 verdict。期望方向取实测先验
    (吸筹/拉升通常续涨; 派发/下跌通常续跌, 但实测出现均值回归者自动反转,
    如 markup 结尾多为局部高点、其后回落)。同时落库各窗口收益 (fwd_ret_20/40/60)
    与每窗口 verdict (fb_vd_20/40/60)、置信度 fb_confidence,
    供 calibrate.diagnose 分窗复检。
    全部窗口未成熟 / 主判据窗口涨跌幅度小于 min_move (方向不明) 时跳过,
    下次分析数据更多后再评估。结果写入 wx_feedback.json (source=auto),
    已有人工标注 (source=manual) 的不覆盖。返回新增/更新条数。

    每批样本落库后重算先验 (update_fb_prior), 使阶段→方向映射随真实行情
    演进自适应, 而非固定硬编码。
    """
    if not segs:
        return 0
    from .config import _PHASE_STYLE
    from .storage import _day_fmt, build_feedback_record, feedback_key, load_feedback, save_feedback
    close = df["close"].values
    n = len(df)
    with _FB_LOCK:
        records = load_feedback()
        fmap = {}
        for r in records:
            if r.get("start_dt") and r.get("end_dt"):
                fmap[feedback_key(r["symbol"], r.get("scale", 240),
                                  r["start_dt"], r["end_dt"])] = r
        changed = 0
        for a, e, key, label in segs:
            if key not in _PHASE_STYLE or not (0 <= a < e < n) or close[e] <= 0:
                continue
            rets = {}
            for h in FB_HORIZONS:
                j = e + h
                if j < n and close[j] > 0:
                    rets[h] = float(close[j] / close[e] - 1)
            if not rets:
                continue                      # 所有窗口均未成熟
            # 期望方向改用实测先验 (吸筹/拉升→"up", 派发/下跌→"down",
            # 实测延续方向与预设相反者直接反转); sample不足回退硬编码
            expect_up = fb_expected_direction(key) == "up"
            verdict, fb_conf, wdv = _fb_verdict(rets, expect_up, min_move)
            if verdict is None:
                continue                      # 全部窗口方向不明
            # 主判据取幅度最大的有效窗口
            effective = {h: (v, r) for h, (v, r) in wdv.items() if v != "neutral"}
            h_star = max(effective, key=lambda h: abs(effective[h][1]))
            ret = effective[h_star][1]
            k = feedback_key(symbol, int(scale), _day_fmt(df["day"].iloc[a]),
                             _day_fmt(df["day"].iloc[e]))
            old = fmap.get(k)
            if old and old.get("source") == "manual" and old.get("verdict"):
                continue
            rec = build_feedback_record(symbol, len(df), int(scale), df,
                                        int(a), int(e), key, label)
            rec["verdict"] = verdict
            rec["fb_confidence"] = round(fb_conf, 2)
            rec["date"] = time.strftime("%Y-%m-%d")
            rec["source"] = "auto"
            rec["fwd_ret"] = round(ret * 100, 2)
            rec["fwd_h"] = int(h_star)
            for h, v in rets.items():
                rec[f"fwd_ret_{h}"] = round(v * 100, 2)
                rec[f"fb_vd_{h}"] = wdv.get(h, ("neutral", v))[0]
            fmap[k] = rec
            changed += 1
        if changed:
            save_feedback(list(fmap.values()))
            # 新样本落库后立即重算续变方向先验 (供下次判定热加载)
            try:
                update_fb_prior(list(fmap.values()))
            except Exception as e:
                log_exc("update_fb_prior 重算失败", e)
        return changed


# ── 评估 ──
def _locate_ref(df, ref_dt):
    """在最新 K 线中定位 ref_dt 对应的索引; 找不到返回 None。"""
    try:
        dt = df["day"]
        s = dt.dt.strftime("%Y-%m-%d %H:%M") if hasattr(dt, "dt") else dt.astype(str)
        idx = np.where(s.values == ref_dt)[0]
        if len(idx):
            return int(idx[-1])
        same = dt.dt.strftime("%Y-%m-%d") if hasattr(dt, "dt") else dt.astype(str)
        idx = np.where(same.values == ref_dt[:10])[0]
        if len(idx):
            return int(idx[-1])
    except Exception:
        return None
    return None


def _horizon_result(df, idx, h, rec, bench=None):
    base = float(df["close"].iloc[idx])
    if base <= 0:
        return None
    seg_close = float(df["close"].iloc[idx + h])
    seg_hi = float(df["high"].iloc[idx + 1: idx + h + 1].max())
    seg_lo = float(df["low"].iloc[idx + 1: idx + h + 1].min())
    out = {"ret": round(seg_close / base - 1, 6),
           "hi": round(seg_hi, 4), "lo": round(seg_lo, 4),
           "up_hit": False, "down_hit": False, "bench": None}
    if bench is not None and bench.get("close") is not None \
            and len(bench["close"]) > 0 and idx + h < len(bench["close"]):
        b0 = float(bench["close"][idx])
        b1 = float(bench["close"][idx + h])
        if b0 > 0:
            out["bench"] = round(b1 / b0 - 1, 6)
    if rec.get("up_target"):
        out["up_hit"] = bool(seg_hi >= rec["up_target"])
    if rec.get("down_target"):
        out["down_hit"] = bool(seg_lo <= rec["down_target"])
    return out


def _evaluate_one(rec):
    """评估单条记录中尚未评估的周期。返回是否有新评估完成。"""
    scale = int(rec.get("scale", 240))
    df = add_indicators(fetch_kline(rec["symbol"],
                                    datalen=int(rec.get("datalen", 700)) + 80,
                                    scale=scale))
    idx = _locate_ref(df, rec.get("ref_dt", ""))
    if idx is None:
        df = add_indicators(fetch_kline(rec["symbol"], datalen=1023, scale=scale))
        idx = _locate_ref(df, rec.get("ref_dt", ""))
        if idx is None:
            fails = int(rec.get("eval_fails", 0)) + 1
            rec["eval_fails"] = fails
            if fails >= 3:
                rec["status"] = "stale"
            return False
    # 横截面基准: 同周期上证指数, 用于超额收益命中评估 (个别股票停牌/指数源失败可缺省)
    bench = None
    try:
        mdf = add_indicators(fetch_kline("sh000001", datalen=len(df), scale=scale))
        if len(mdf) >= len(df):
            bench = {"close": mdf["close"].to_numpy()[:len(df)]}
    except Exception as e:
        log_exc("获取上证基准失败 (降级为无 bench)", e)
        bench = None
    results = dict(rec.get("results") or {})
    changed = False
    for h in HORIZONS:
        k = str(h)
        if k in results:
            continue
        if idx + h < len(df):
            r = _horizon_result(df, idx, h, rec, bench=bench)
            if r is not None:
                results[k] = r
                changed = True
    rec["results"] = results
    rec["status"] = "done" if len(results) >= len(HORIZONS) else "pending"
    if not changed and rec["status"] == "pending" and idx + min(HORIZONS) >= len(df):
        # 记录落在行情末端: 未来行情尚未走满, 标记 waiting 供界面区分
        # (区别于 eval_fails 的"定位失败", 前者等数据、后者是异常)。
        rec["waiting"] = True
    else:
        rec["waiting"] = False
    # 顺带自动标注阶段带反馈 (无头 cron 评估时也累积样本)。
    try:
        from .phases import phase_segments
        piv = find_pivots(df, order=6)
        ev = detect_all(df, piv)
        auto_evaluate_feedback(df, rec.get("symbol") or "", scale,
                               phase_segments(df, piv, ev))
    except Exception as e:
        log_exc("评估时自动标注阶段带反馈失败", e)
    return changed


def evaluate_pending(records, force=False, min_interval=3600, max_records=15):
    """对未完成的记录评估缺失周期, 返回新增评估条数。评估期间不持文件锁 (网络可能慢)。"""
    return run_pending_eval(records, _evaluate_one, HORIZONS,
                            load_accuracy, save_accuracy, _key, _LOCK,
                            force=force, min_interval=min_interval,
                            max_records=max_records)


def export_accuracy_csv(records, path=None):
    """导出分析准确度记录到 CSV。"""
    import csv
    import os
    path = path or os.path.join(DATA_DIR, "wx_accuracy.csv")
    cols = ["symbol", "code", "name", "scale", "ref_dt", "ref_close", "phase",
            "phase_tone", "pnf_dir", "fusion_score", "fusion_bias", "trade_dir",
            "up_target", "down_target", "events", "status", "created_ts"]
    for h in HORIZONS:
        cols += [f"ret_{h}", f"up_hit_{h}", f"down_hit_{h}"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in records:
            res = r.get("results") or {}
            row = [r.get("symbol"), r.get("code"), r.get("name"), r.get("scale"),
                   r.get("ref_dt"), r.get("ref_close"), r.get("phase"),
                   r.get("phase_tone"), r.get("pnf_dir"), r.get("fusion_score"),
                   r.get("fusion_bias"), r.get("trade_dir"), r.get("up_target"),
                   r.get("down_target"), ",".join(r.get("events") or []),
                   r.get("status"), r.get("created_ts")]
            for h in HORIZONS:
                hh = res.get(str(h)) or {}
                row += [hh.get("ret"), hh.get("up_hit"), hh.get("down_hit")]
            wr.writerow(row)
    return path


def run_auto_accuracy_eval(force=False):
    """加载全部记录并评估到期部分, 落盘。返回本次新增评估数。"""
    with _LOCK:
        records = load_accuracy()
    if not records:
        return 0
    n = evaluate_pending(records, force=force)
    # 评估完成后顺带重算新闻贡献自校准因子 (样本充足才生成), 供 fusion.
    # _news_cal_factor 热加载 —— 新闻维度权重由实测预测力驱动, 而非固定拍脑袋值。
    try:
        update_news_calibration(records)
    except Exception as e:
        log_exc("update_news_calibration 失败", e)
    # 评估时会顺带标注新阶段带 (auto_evaluate_feedback), 这里一并重算实测先验。
    # update_fb_prior 让阶段→方向映射 (含均值回归反转) 随真实行情演进自适应。
    try:
        update_fb_prior()
    except Exception as e:
        log_exc("update_fb_prior 重算失败", e)
    return n


# ── 新闻贡献自校准 ──
NEWS_CAL_MIN_TOTAL = 40    # 已评估且带 news_score 的最少样本量
NEWS_CAL_MIN_STRONG = 15   # 其中 |news_score|>=0.3 的强情绪最少样本量


def update_news_calibration(records=None, horizon=20):
    """统计 news_score 与到期实际收益的方向一致性, 落盘自校准因子文件。

    口径 (与 fusion.NEWS_MIN_ABS 门控一致, 只看强情绪):
      baseline p0 = 该周期全样本上涨占比;
      hit = |news_score|>=0.3 样本中 sign(news) 与 sign(ret) 一致的占比;
      hit < p0-3% → factor=0.5 (新闻整体反向/无效, 缩权);
      hit > p0+5% → factor=1.3 (确有增量, 放权);
      其余 → factor=1.0。样本不足时 factor=1.0 保持中性。
    结果写入 wx_news_calibration.json (fusion 按 mtime 热加载)。
    """
    from .paths import DATA_DIR
    cal_file = os.path.join(DATA_DIR, "wx_news_calibration.json")
    if records is None:
        records = load_accuracy()
    k = str(horizon)
    samples = []       # (news_score, ret)
    up_cnt = total = 0
    for r in records:
        res = (r.get("results") or {}).get(k)
        if not res or res.get("ret") is None:
            continue
        ns = r.get("news_score")
        if ns is None:
            continue
        ret = float(res["ret"])
        samples.append((float(ns), ret))
        total += 1
        up_cnt += int(ret > 0)
    payload = {"factor": 1.0, "horizon": horizon, "n": total,
               "reason": "样本不足, 未启用校准", "updated_ts": time.time()}
    if total >= NEWS_CAL_MIN_TOTAL:
        p0 = up_cnt / total
        strong = [(s, v) for s, v in samples if abs(s) >= 0.3]
        if len(strong) >= NEWS_CAL_MIN_STRONG:
            hit = sum(1 for s, v in strong if (v > 0) == (s > 0)) / len(strong)
            payload.update({
                "p0": round(p0, 4),
                "strong_n": len(strong),
                "strong_hit": round(hit, 4),
            })
            if hit < p0 - 0.03:
                payload["factor"] = 0.5
                payload["reason"] = f"强新闻方向命中{hit:.0%}<基线{p0:.0%}, 缩权"
            elif hit > p0 + 0.05:
                payload["factor"] = 1.3
                payload["reason"] = f"强新闻方向命中{hit:.0%}>基线{p0:.0%}, 放权"
            else:
                payload["reason"] = f"强新闻方向命中{hit:.0%}≈基线{p0:.0%}, 维持中性"
    try:
        atomic_write_json(cal_file, payload)
    except Exception as e:
        log_exc("update_news_calibration 落盘失败", e)
    return payload


# ── 汇总与导出 ──
def confusion_matrix(records):
    """阶段-方向混淆矩阵: 预测阶段/方向 vs 未来N根实际方向。
    供校准用: 若某阶段实际方向与预设 tone 长期相反(如"底部整固"多为跌),
    说明该阶段误判系统性偏多/偏空, 应下调其 tone 或修正判定规则。"""
    out = {}
    for h in HORIZONS:
        k = str(h)
        rows = {}
        for r in records:
            res = (r.get("results") or {}).get(k)
            if not res or res.get("ret") is None:
                continue
            base = r.get("phase", "")
            actual = "涨" if res["ret"] > 0 else "跌"
            key = (base, r.get("phase_tone", ""))
            rows.setdefault(key, {"n": 0, "up": 0, "dn": 0,
                                  "mean": [], "ex": []})
            row = rows[key]
            row["n"] += 1
            row["up"] += int(res["ret"] > 0)
            row["dn"] += int(res["ret"] < 0)
            row["mean"].append(res["ret"])
            if res.get("bench") is not None:
                row["ex"].append(res["ret"] - res["bench"])
        agg = []
        for (phase, tone), s in sorted(rows.items(), key=lambda kv: -kv[1]["n"]):
            mean = statistics.mean(s["mean"]) if s["mean"] else 0.0
            agg.append({
                "phase": phase, "tone": tone, "n": s["n"],
                "win": round(s["up"] / max(1, s["n"]), 4),
                "mean": round(mean, 6),
                "ex_mean": round(statistics.mean(s["ex"]), 6) if s["ex"] else None,
                "aligned": bool(tone == "bullish" and s["up"] / max(1, s["n"]) > 0.5)
                           or bool(tone == "bearish" and s["dn"] / max(1, s["n"]) > 0.5)
                           or tone in ("", "neutral"),
            })
        out[k] = agg
    return out


def accuracy_stats(records):
    """汇总方向命中率。返回 {total, evaluated, pending, horizons:{h:{...}}}。"""
    total = len(records)
    evaled = sum(1 for r in records if r.get("results"))
    stale = sum(1 for r in records if r.get("status") == "stale")
    out = {"total": total, "evaluated": evaled, "pending": total - evaled,
           "stale": stale, "horizons": {}, "confusion": confusion_matrix(records)}
    for h in HORIZONS:
        k = str(h)
        e = {"n": 0, "up_ratio": None, "mean": None,
             "phase_bull": {"n": 0, "hit": 0},
             "phase_bear": {"n": 0, "hit": 0},
             "pnf_up": {"n": 0, "hit": 0},
             "pnf_down": {"n": 0, "hit": 0},
             "fusion_bull": {"n": 0, "hit": 0},
             "fusion_bear": {"n": 0, "hit": 0},
             # 新闻情绪 A/B: 同一样本, 带新闻融合 vs 纯技术面融合的方向命中率
             "news_with": {"n": 0, "hit": 0},
             "news_without": {"n": 0, "hit": 0},
             "news_diff": None,
             "trade_bull": {"n": 0, "hit": 0},
             "trade_bear": {"n": 0, "hit": 0},
             "up_target": {"n": 0, "hit": 0},
             "down_target": {"n": 0, "hit": 0},
             # 超额收益 (vs 同期上证指数): 偏多→跑赢大盘, 偏空→跑输大盘
             "ex_phase_bull": {"n": 0, "hit": 0},
             "ex_phase_bear": {"n": 0, "hit": 0},
             "ex_trade_bull": {"n": 0, "hit": 0},
             "ex_trade_bear": {"n": 0, "hit": 0},
             "ex_mean": None}
        rets = []
        for r in records:
            res = (r.get("results") or {}).get(k)
            if not res or res.get("ret") is None:
                continue
            ret = res["ret"]
            bench = res.get("bench")
            e["n"] += 1
            rets.append(ret)
            tone = r.get("phase_tone")
            if tone == "bullish":
                e["phase_bull"]["n"] += 1
                e["phase_bull"]["hit"] += int(ret > 0)
                if bench is not None:
                    e["ex_phase_bull"]["n"] += 1
                    e["ex_phase_bull"]["hit"] += int(ret > bench)
            elif tone == "bearish":
                e["phase_bear"]["n"] += 1
                e["phase_bear"]["hit"] += int(ret < 0)
                if bench is not None:
                    e["ex_phase_bear"]["n"] += 1
                    e["ex_phase_bear"]["hit"] += int(ret < bench)
            if r.get("pnf_dir") == "up":
                e["pnf_up"]["n"] += 1
                e["pnf_up"]["hit"] += int(ret > 0)
            elif r.get("pnf_dir") == "down":
                e["pnf_down"]["n"] += 1
                e["pnf_down"]["hit"] += int(ret < 0)
            ftone = r.get("fusion_tone")
            if ftone == "bullish":
                e["fusion_bull"]["n"] += 1
                e["fusion_bull"]["hit"] += int(ret > 0)
            elif ftone == "bearish":
                e["fusion_bear"]["n"] += 1
                e["fusion_bear"]["hit"] += int(ret < 0)
            # 新闻 A/B: 仅对带 news_score 的样本, 比较带/不带新闻的方向命中。
            if r.get("news_score") is not None:
                _wn = e["news_with"]
                _won = e["news_without"]
                if ftone == "bullish":
                    _wn["n"] += 1
                    _wn["hit"] += int(ret > 0)
                elif ftone == "bearish":
                    _wn["n"] += 1
                    _wn["hit"] += int(ret < 0)
                ftone0 = r.get("fusion_no_news_tone")
                if ftone0 == "bullish":
                    _won["n"] += 1
                    _won["hit"] += int(ret > 0)
                elif ftone0 == "bearish":
                    _won["n"] += 1
                    _won["hit"] += int(ret < 0)
            ttone = r.get("trade_tone")
            if ttone == "bullish":
                e["trade_bull"]["n"] += 1
                e["trade_bull"]["hit"] += int(ret > 0)
                if bench is not None:
                    e["ex_trade_bull"]["n"] += 1
                    e["ex_trade_bull"]["hit"] += int(ret > bench)
            elif ttone == "bearish":
                e["trade_bear"]["n"] += 1
                e["trade_bear"]["hit"] += int(ret < 0)
                if bench is not None:
                    e["ex_trade_bear"]["n"] += 1
                    e["ex_trade_bear"]["hit"] += int(ret < bench)
            if r.get("up_target"):
                e["up_target"]["n"] += 1
                e["up_target"]["hit"] += int(res.get("up_hit"))
            if r.get("down_target"):
                e["down_target"]["n"] += 1
                e["down_target"]["hit"] += int(res.get("down_hit"))
        if rets:
            e["mean"] = round(statistics.mean(rets), 6)
            e["up_ratio"] = round(sum(1 for v in rets if v > 0) / len(rets), 4)
        exs = []
        for r in records:
            res = (r.get("results") or {}).get(k)
            if res and res.get("ret") is not None and res.get("bench") is not None:
                exs.append(res["ret"] - res["bench"])
        if exs:
            e["ex_mean"] = round(statistics.mean(exs), 6)
        # 新闻 A/B 汇总: 带新闻命中率 - 纯技术面命中率 (>0 表示新闻有增量)。
        if e["news_with"]["n"] and e["news_without"]["n"]:
            wr = e["news_with"]["hit"] / e["news_with"]["n"]
            wor = e["news_without"]["hit"] / e["news_without"]["n"]
            e["news_diff"] = round(wr - wor, 4)
        out["horizons"][k] = e
    return out


def export_accuracy(records, path=None):
    """导出全部记录 + 汇总到 JSON (供人工核查 / 交给 AI 修正阈值)。"""
    path = path or os.path.join(DATA_DIR, "wx_accuracy_export.json")
    payload = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": VERSION,
        "horizons": list(HORIZONS),
        "note": "evaluated 记录已用真实行情评估; status=pending 为尚未到期。",
        "stats": accuracy_stats(records),
        "records": records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


# ── 无头自动评估 / 定时任务 ──
def _sched_command():
    """生成供 cron / Windows 计划任务执行的评估命令。
    源码运行用 `python -m wyckoff.accuracy --eval`; 打包后用可执行文件 `--acc-eval`。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --acc-eval'
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'cd "{proj}" && "{sys.executable}" -m wyckoff.accuracy --eval'


def install_cron(hour=None, minute=1):
    """Linux: 在 crontab 安装/移除每日评估任务 (默认每日 15:01 收盘后)。
    hour=None 时移除。"""
    import subprocess
    try:
        cur = subprocess.check_output(["crontab", "-l"], stderr=subprocess.STDOUT,
                                      text=True)
    except subprocess.CalledProcessError:
        cur = ""
    lines = [l for l in cur.splitlines() if "wyckoff.accuracy" not in l]
    if hour is not None:
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        lines.append(f"{minute} {hour} * * * {_sched_command()} >> /dev/null 2>&1")
    new = "\n".join(lines).strip() + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    return hour is not None


def install_task(hour="15:01", remove=False):
    """Windows: 创建/移除"威科夫准确度"计划任务 (默认每日 15:01 收盘后执行无头评估)。"""
    import subprocess
    if os.name != "nt":
        print("install_task 仅支持 Windows; Linux 请用 --install-cron")
        return
    if remove:
        subprocess.run(["schtasks", "/Delete", "/TN", "WyckoffAccuracy", "/F"])
        return
    bat = os.path.join(DATA_DIR, "wx_accuracy_daily.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f"@echo off\n{_sched_command()}\n")
    subprocess.run(["schtasks", "/Create", "/TN", "WyckoffAccuracy",
                    "/SC", "DAILY", "/ST", hour, "/TR", bat, "/F"], check=True)


def daemon_eval(minutes=60):
    """常驻循环: 每 minutes 分钟评估一次到期记录 (跨平台, 可手动后台运行)。"""
    print(f"[accuracy] 常驻评估已启动, 每 {minutes} 分钟一次, Ctrl+C 退出", flush=True)
    while True:
        try:
            n = run_auto_accuracy_eval()
            if n:
                print(time.strftime("%H:%M:%S"), f"本次评估 {n} 条", flush=True)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), f"评估异常: {e}", flush=True)
        time.sleep(max(5, minutes) * 60)


if __name__ == "__main__":
    import sys
    if "--eval" in sys.argv:
        n = run_auto_accuracy_eval(force=True)
        records = load_accuracy()
        print(json.dumps(accuracy_stats(records), ensure_ascii=False, indent=2,
                         default=str))
        print(f"\n本次新增评估 {n} 条, 累计 {len(records)} 条")
        # 阶段带实测先验概览 (update_fb_prior 已随评估落盘; 此处热加载展示)
        try:
            labels = (load_fb_prior() or {}).get("labels") or {}
            if labels:
                print("\n阶段带实测先验 (结束后续变方向, ~80.2% 口径的来源):")
                tot_n, tot_w = 0, 0.0
                for k, v in sorted(labels.items(), key=lambda kv: -kv[1].get("n", 0)):
                    n_ = v.get("n", 0)
                    up_ratio = v.get("up_ratio", 0.5)
                    d = v.get("dir") or v.get("fallback")
                    corr = (1 - up_ratio) if d == "down" else up_ratio
                    tot_n += n_
                    tot_w += n_ * corr
                    print(f"  {k:<14} n={n_:<4} up_ratio={up_ratio:.3f} "
                          f"expected={d} -> 同向率约 {corr*100:.1f}%")
                if tot_n:
                    print(f"  加权合计: {tot_w/tot_n*100:.1f}% (n={tot_n})")
        except Exception as e:
            print(f"阶段带先验概览跳过: {e}")
        # 信号级准确度同步评估 (同一 cron 钩子)
        try:
            from .signal_accuracy import (
                _fmt_stats,
                load_signals,
                run_auto_signal_eval,
                signal_stats,
            )
            ns = run_auto_signal_eval(force=True)
            print("\n" + _fmt_stats(signal_stats(load_signals())))
            print(f"本次新增信号评估 {ns} 条")
        except Exception as e:
            print(f"信号评估跳过: {e}")
        # 在线校准模型同步重训 (同一 cron 钩子): 评估产出新标签后立即刷新模型
        try:
            from .online_model import run_auto_model_retrain
            st = run_auto_model_retrain()
            print(f"\n模型重训: 标签 {st.get('n_labels', 0)} 条, "
                  f"AUC={st.get('auc_oos')}, "
                  f"接管conf={'是' if st.get('ready') else '否'}")
        except Exception as e:
            print(f"模型重训跳过: {e}")
    elif "--export" in sys.argv:
        p = export_accuracy(load_accuracy())
        print(f"已导出: {p}")
    elif "--daemon" in sys.argv:
        i = sys.argv.index("--daemon")
        minutes = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 \
            and sys.argv[i + 1].isdigit() else 60
        daemon_eval(minutes)
    elif "--install-cron" in sys.argv:
        i = sys.argv.index("--install-cron")
        arg = sys.argv[i + 1] if len(sys.argv) > i + 1 else "15:01"
        if ":" in arg:
            hh, mm = arg.split(":", 1)
        else:
            hh, mm = arg, "1"
        try:
            hh, mm = int(hh), int(mm)
        except ValueError:
            hh, mm = 15, 1
        install_cron(hh, mm)
        print(f"已安装每日 {hh:02d}:{mm:02d} 的自动评估任务 (默认收盘后15:01)")
    elif "--uninstall-cron" in sys.argv:
        install_cron(None)
        print("已移除自动评估任务")
    elif "--install-task" in sys.argv:
        i = sys.argv.index("--install-task")
        hour = sys.argv[i + 1] if len(sys.argv) > i + 1 else "15:01"
        install_task(hour)
        print(f"已安装每日 {hour} 的计划任务 (默认收盘后15:01)")
    elif "--uninstall-task" in sys.argv:
        install_task(remove=True)
        print("已移除计划任务")
    else:
        records = load_accuracy()
        print(json.dumps(accuracy_stats(records), ensure_ascii=False, indent=2,
                         default=str))
        print("累计 %d 条; 命令: --eval 评估 / --export 导出 / "
              "--daemon [分] 常驻 / --install-cron [HH:MM] 装Linux定时 / "
              "--install-task [HH:MM] 装Windows计划" % len(records))
