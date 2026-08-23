"""用户数据持久化: 自选股 / 设置 / 阶段带反馈标注 / 持仓簿。"""
import json
import os
import re

from ._log import log_exc
from ._shared import atomic_write_json
from .config import DEFAULT_SETTINGS
from .paths import (
    CANDIDATES_FILE,
    FEEDBACK_FILE,
    NOTES_FILE,
    PORTFOLIO_FILE,
    SETTINGS_FILE,
    WATCHLIST_FILE,
)

# 优先使用环境变量中的 AI API Key, 避免把密钥明文写入配置文件
# (wyckoff_settings.json 可能被误提交/同步; env 方式密钥不入盘)。
API_KEY_ENV = "WYCKOFF_API_KEY"


def load_watchlist():
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return ["600104", "000001", "300750", "688981"]


def save_watchlist(codes):
    try:
        atomic_write_json(WATCHLIST_FILE, codes)
    except Exception as e:
        log_exc("保存自选股失败", e)


def load_candidates():
    """待观察清单: [{code, name, score, phase, conf_q, signals, date}], 按保存时间倒序。"""
    try:
        with open(CANDIDATES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_candidates(records):
    try:
        atomic_write_json(CANDIDATES_FILE, records)
    except Exception as e:
        log_exc("保存待观察清单失败", e)


# ── 持仓簿 (个人持仓) ──
# 记录: [{code, name, shares, cost, buy_date, stop, note, created_ts}]
def load_portfolio():
    try:
        with open(PORTFOLIO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_portfolio(records):
    try:
        atomic_write_json(PORTFOLIO_FILE, records)
    except Exception as e:
        log_exc("保存持仓簿失败", e)


# ── 自选股备注/笔记 ──
# {code: note}
def load_notes():
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notes(notes):
    try:
        atomic_write_json(NOTES_FILE, notes)
    except Exception as e:
        log_exc("保存笔记失败", e)


def _dedupe_api_key(key):
    """修复粘贴/保存时把 key 拼重复的键值 (sk-xxx 反复拼接), 只保留第一段。"""
    key = (key or "").strip()
    if not key.startswith("sk-"):
        return key
    m = re.match(r"^(sk-[A-Za-z0-9]+?)\1+$", key)
    if m:
        return m.group(1)
    return key


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            s.update(saved)
        s["ai_api_key"] = _dedupe_api_key(s.get("ai_api_key", ""))
    except Exception:
        pass
    # 环境变量密钥优先: 存在则覆盖配置文件中的值 (运行时生效, 不落盘)
    env_key = (os.environ.get(API_KEY_ENV) or "").strip()
    if env_key:
        s["ai_api_key"] = env_key
    return s


def save_settings(s):
    if isinstance(s, dict) and s.get("ai_api_key"):
        s = dict(s)
        s["ai_api_key"] = _dedupe_api_key(s["ai_api_key"])
    try:
        atomic_write_json(SETTINGS_FILE, s)
    except Exception as e:
        log_exc("保存设置失败", e)


def feedback_key(symbol, scale, start_dt, end_dt):
    """阶段带标注键: 标的 + 周期 + 起止时间定位, 与 datalen (窗口长度) 无关。

    同一段行情在不同 datalen 窗口下切出的波段索引不同, 若按索引定位标注会
    全部失配; 改为按实际起止时间关联, 换周期/时间段后反馈仍能对上。
    """
    return f"{symbol}|{scale}|{start_dt}|{end_dt}"


def load_feedback():
    try:
        with open(FEEDBACK_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_feedback(records):
    try:
        atomic_write_json(FEEDBACK_FILE, records)
    except Exception as e:
        log_exc("保存阶段带反馈失败", e)


# L5 阶段可信度: 按阶段类型统计标注判定正确率 (L1 收缩向全阶段基线回归)。
_PHASE_PRIOR_ALPHA = 5


def phase_reliability(feedback=None, prior_alpha=_PHASE_PRIOR_ALPHA):
    """统计各阶段类型的判定可信度 (自动+人工标注, L1 收缩).

    返回 {阶段label: {"n", "correct", "wrong", "raw", "shrunk"}} 按样本降序。
    label 为阶段 key (markdown/accumulation/markup/distribution)。
    """
    fb = feedback if feedback is not None else load_feedback()
    counts = {}
    for r in fb:
        lb = r.get("label")
        verdict = r.get("verdict")
        if not lb or verdict not in ("correct", "wrong"):
            continue
        s = counts.setdefault(lb, {"n": 0, "correct": 0})
        s["n"] += 1
        s["correct"] += 1 if verdict == "correct" else 0
    if not counts:
        return {}
    total_n = sum(s["n"] for s in counts.values())
    total_c = sum(s["correct"] for s in counts.values())
    p0 = (total_c / total_n) if total_n else 0.5
    out = {}
    for lb, s in counts.items():
        raw = s["correct"] / s["n"]
        shrunk = (s["correct"] + prior_alpha * p0) / (s["n"] + prior_alpha)
        out[lb] = {"n": s["n"], "correct": s["correct"],
                   "wrong": s["n"] - s["correct"], "raw": round(raw, 4),
                   "shrunk": round(shrunk, 4)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def _day_fmt(dt):
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)


def build_feedback_record(symbol, datalen, scale, df, a, e, key, label):
    """把一个阶段带存为可校准的标注记录, 附带用于调阈值的特征。

    记录带起始/结束时间 (start_dt/end_dt), 用于跨 datalen 窗口关联标注。
    特征对四类阶段带统一落库: lo1/lo2 (前后半段最低) + hi1/hi2 (前后半段
    最高) 及派生比值 low_defense/high_cap —— 此前只给吸筹/派发写特征,
    markup/markdown 与过短段全为空, 导致校准样本大量缺特征无法训练。
    """
    cl = df["close"].values
    lo = df["low"].values
    hi = df["high"].values
    net = float(cl[e] / cl[a] - 1) if cl[a] else 0.0
    mid = max(a + 1, (a + e) // 2)
    feat = {}
    try:
        if e <= a:  # 单根段: 前后半段同为该根
            feat["lo1"] = feat["lo2"] = round(float(lo[a]), 4)
            feat["hi1"] = feat["hi2"] = round(float(hi[a]), 4)
        else:
            feat["lo1"] = float(lo[a:mid + 1].min())
            feat["lo2"] = float(lo[mid + 1:e + 1].min()) if mid < e else float(lo[e])
            feat["hi1"] = float(hi[a:mid + 1].max())
            feat["hi2"] = float(hi[mid + 1:e + 1].max()) if mid < e else float(hi[e])
            for k in ("lo1", "lo2", "hi1", "hi2"):
                feat[k] = round(feat[k], 4)
        if feat.get("lo1"):
            feat["low_defense"] = round(feat["lo2"] / feat["lo1"], 4)
        if feat.get("hi1"):
            feat["high_cap"] = round(feat["hi2"] / feat["hi1"], 4)
    except Exception:
        feat = {}
    return {
        "symbol": symbol,
        "datalen": datalen,
        "scale": scale,
        "start": int(a),
        "end": int(e),
        "start_dt": _day_fmt(df["day"].iloc[a]),
        "end_dt": _day_fmt(df["day"].iloc[e]),
        "label": key,
        "label_cn": label,
        "net": round(net, 4),
        "verdict": "",
        "date": "",
        "features": feat,
    }
