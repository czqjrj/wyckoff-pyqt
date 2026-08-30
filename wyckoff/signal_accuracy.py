"""信号准确度追踪: 对每个独立信号 (威科夫事件 / VSA) 做逐信号命中追踪。

与 accuracy.py (整份分析的阶段/目标追踪) 互补: 这里记录每一次分析中检测出的
每个事件信号 (Spring/ST/UTAD/SOS/JOC/SC/AR/BC/PSY) 与 VSA 信号,
用之后真实行情评估未来 5/10/20/40 根收益, 汇总出每类信号的
胜率 / 均值 / 置信度分档表现, 用于持续校准事件检测规则与置信度打分。

写入/评估时机:
  - record_signals: 每次分析 (record_analysis) 时记录, 并从当次 df 立即评估
    已出满未来行情的历史信号 (无需等待);
  - evaluate_pending / run_auto_signal_eval: 到期但缺未来行情的信号, 由每日
    定时任务补抓行情评估 (与 accuracy.py 的 cron 同钩子)。

存储: ~/.wyckoff/wx_signal_accuracy.json
评估口径: 信号出现后 N 根收盘收益 (ret 原始存库); 汇总胜率时方向化:
标称多头/中性信号 ret>0 记命中, 标称空头信号 (config.event_dir/vsa_dir<0)
ret<0 记命中 —— 否则 UTAD/LPSY/SUP 这类看空信号会被"涨了"误记为正确。
"""
import json
import math
import statistics
import threading
import time

import numpy as np
import pandas as pd

from ._shared import atomic_write_json
from .config import STRONG_TIER_TYPES, event_dir, vsa_dir
from .datasource import fetch_kline
from .events import detect_all
from .indicators import add_indicators, find_pivots
from .paths import SIGNAL_ACCURACY_FILE
from .vsa import vsa_classify

# 评估周期 (根)
HORIZONS = (5, 10, 20, 40)

_LOCK = threading.Lock()


# ── 存取 ──
def _key(rec):
    return f"{rec.get('symbol')}|{rec.get('scale')}|{rec.get('kind')}|{rec.get('type')}|{rec.get('date')}"


def _sig_date(rec):
    """解析记录日期 → datetime; 失败返回 None。"""
    try:
        return pd.to_datetime(str(rec.get("date", "")))
    except Exception:
        return None


def _cooldown_dup(existing, rec, df, cooldown_bars):
    """在冷却窗内找同标的同类型未评估记录。

    返回命中记录的 key (该记录将被合并更新), 无则 None。
    冷却窗按 bar 数衡量: 定位 df 中 rec 的 idx, 与同 symbol+scale+kind+type 记录的
    idx 差 <= cooldown_bars 视为重复 (用 bar 差, 不受停牌/假日影响)。
    """
    if df is None or df.empty:
        return None
    rec_idx = _locate(df, rec["date"])
    if rec_idx is None:
        return None
    for key, old in existing.items():
        if old.get("kind") != rec.get("kind"):
            continue
        if old.get("symbol") != rec.get("symbol") or old.get("scale") != rec.get("scale"):
            continue
        if old.get("type") != rec.get("type"):
            continue
        old_idx = old.get("idx")
        if old_idx is None:
            continue
        try:
            if abs(int(old_idx) - int(rec_idx)) <= cooldown_bars:
                return key
        except (TypeError, ValueError):
            continue
    return None


def _merge_cooldown(old, new):
    """合并冷却窗内的重复信号: 保留较早记录, 补充名称等元信息, 不覆盖评估结果。"""
    if not old.get("name") and new.get("name"):
        old["name"] = new.get("name")
    if not old.get("code") and new.get("code"):
        old["code"] = new.get("code")
    old["datalen"] = max(int(old.get("datalen", 0) or 0),
                         int(new.get("datalen", 0) or 0))
    old["conf"] = max(int(old.get("conf", 0) or 0), int(new.get("conf", 0) or 0))
    return old


def expire_stale_signals(max_age_days: int = 365, keep_done_days: int = 730):
    """清理/标记过期信号记录。

    - 未评估 (pending/waiting) 且创建超过 max_age_days → 直接删除 (无法评估)
    - 已评估 (done) 且超过 keep_done_days → 保留, 不删 (历史样本仍用于胜率统计)
    - 返回删除条数。窗口小概率误删已评估记录, 故 done 只按超长年限清理。
    """
    now = time.time()
    secs_max = max_age_days * 86400
    secs_done = keep_done_days * 86400
    with _LOCK:
        records = load_signals()
        keep = []
        n_del = 0
        for r in records:
            created = r.get("created_ts") or 0
            if r.get("results"):
                # 已评估: 仅清理超久远历史 (防止文件无限膨胀)
                if now - created > secs_done:
                    n_del += 1
                    continue
                keep.append(r)
                continue
            if now - created > secs_max:
                n_del += 1
                continue
            keep.append(r)
        if n_del:
            save_signals(keep)
            invalidate_win_rate_cache()
        return n_del


def load_signals():
    try:
        with open(SIGNAL_ACCURACY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_signals(records):
    try:
        # 记录集可达数 MB, 紧凑序列化 (indent=None) 体积 -40%, 落盘更快
        atomic_write_json(SIGNAL_ACCURACY_FILE, records, indent=None)
        invalidate_win_rate_cache()
    except Exception as e:
        from ._log import log_exc
        log_exc("save_signals 落盘失败", e)


# ── 记录 ──
def _locate(df, date_str):
    """在 df 中定位 date_str 对应的 bar 索引; 找不到返回 None。"""
    try:
        s = df["day"].astype(str)
        idx = np.where(s.values == date_str)[0]
        if len(idx):
            return int(idx[-1])
        idx = np.where(s.str.startswith(str(date_str)[:10]).values)[0]
        if len(idx):
            return int(idx[-1])
    except Exception:
        return None
    return None


def _eval_against(df, idx, rec):
    """用 df 中 idx 之后的真实行情评估缺失周期。返回是否有新增评估。

    双口径 (与 docs/accuracy_report.md §P0-1 一致):
      ret   = 事件 bar 起算的历史可比收益;
      ret_c = 确认后首根可交易 bar (avail_idx/avail_date) 起算, 剔除前视。
    无确认信息 (avail 缺失) 时只写 ret, 不写 ret_c。
    """
    c = df["close"].values
    n = len(df)
    # 确认 bar 定位: 优先按日期 (跨 datalen 稳健), 回退存储索引。
    avail = rec.get("avail_idx")
    if rec.get("avail_date"):
        avail = _locate(df, str(rec["avail_date"])) or avail
    try:
        avail = int(avail)
    except (TypeError, ValueError):
        avail = None
    results = dict(rec.get("results") or {})
    changed = False
    for h in HORIZONS:
        k = str(h)
        if k in results:
            continue
        if idx is not None and idx + h < n:
            base = float(c[idx])
            if base > 0:
                r = {"ret": round(c[idx + h] / base - 1, 6)}
                if avail is not None and avail > 0 and c[avail] > 0 and avail + h < n:
                    r["ret_c"] = round(c[avail + h] / c[avail] - 1, 6)
                results[k] = r
                changed = True
    rec["results"] = results
    done = len(results) >= len(HORIZONS)
    rec["status"] = "done" if done else "pending"
    if not done and idx is not None and idx + min(HORIZONS) >= n:
        # 信号落在行情末端: 未来行情未走满, 标记 waiting (等数据, 非异常)
        rec["waiting"] = True
    else:
        rec["waiting"] = False
    return changed


def record_signals(df, symbol, code, scale, datalen, events=None, vsa_signals=None,
                   name="", cooldown_bars=15):
    """记录一次分析中检测到的全部信号 (事件 + VSA), 并立即评估已出未来行情的部分。

    events / vsa_signals 省略时内部重新检测 (find_pivots + detect_all + vsa_classify)。
    同 symbol+scale+kind+type+date 视为同一信号, 去重 (已评估的保留原结果)。
    cooldown_bars: 同一标的同一类型信号在 N 根内不重复新建记录 (如连续多日 NS/ND
    洪水式信号只留一条代表), 防止同类型信号在回测统计里刷屏。
    """
    if events is None or vsa_signals is None:
        pivots = find_pivots(df, order=6)
        if events is None:
            events = detect_all(df, pivots)
        if vsa_signals is None:
            vsa_signals = vsa_classify(df, scale=scale)
    recs = []
    for e in events:
        recs.append(dict(symbol=symbol, code=str(code)[-6:], name=name, scale=scale,
                         datalen=datalen, kind="event", type=e.get("type", "?"),
                         idx=e.get("idx"), date=str(e.get("date")),
                         conf=int(e.get("conf", 50)), price=float(e.get("price", 0)),
                         avail_idx=e.get("avail_idx"), avail_date=str(e.get("avail_date", "")),
                         features=e.get("feat"),
                         created_ts=time.time(), last_eval_ts=0, status="pending",
                         eval_fails=0, results={}))
    for s in vsa_signals:
        vtype = s.get("label", "?")
        # VSA 置信度: 用历史方向化命中率 (L1 贝叶斯收缩值) 作实证先验,
        # 缺失样本回退 50 (中性)。修复旧版恒 0 → 无法排序/过滤的问题。
        vconf = round(win_rate_of("vsa", vtype, 20) * 100)
        # 新闻情绪动态调整: 根据实测新闻方向一致性对 VSA conf 进行轻度校准。
        # factor > 1.0: 新闻有增量, 放大置信度; factor < 1.0: 新闻整体反向, 缩减置信度。
        # factor = 1.0: 维持中性, 无调整。默认 1.0 (文件不存在/样本不足时)。
        try:
            import json, os
            from .paths import DATA_DIR, NEWS_CALIBRATION_FILE
            cal_path = os.path.join(DATA_DIR, "wx_news_calibration.json")
            if os.path.exists(cal_path):
                with open(cal_path, encoding="utf-8") as f:
                    cal = json.load(f)
                factor = cal.get("factor", 1.0)
                # 温和校准: 映射 factor [0.5, 1.3] → [0.85, 1.15] 防止剧烈波动
                adj = 0.5 + 0.5 * factor  # factor=0.5→0.75, factor=1.0→1.0, factor=1.3→1.15
                vconf = int(round(vconf * adj))
                # 钳制在合理範圍 (1-99), 避免 conf 被校准完全抵消
                vconf = max(1, min(99, vconf))
        except Exception:
            pass  # 新闻校准异常不影响基本流程
        # 让在线模型也校准 VSA conf (模型 ready 时轻度接管):
        try:
            from .online_model import apply_model_conf
            _cand = {"kind": "vsa", "type": vtype, "conf": vconf,
                     **dict(s.get("features") or {})}
            apply_model_conf([_cand])
            vconf = int(_cand["conf"])
        except Exception:
            pass
        # 类型实证天花板封顶 (弱 VSA 标签高分幻觉修复):
        try:
            from .events import _cap_to_ceiling
            _cand = {"kind": "vsa", "type": vtype, "conf": vconf}
            _cap_to_ceiling([_cand])
            vconf = int(_cand["conf"])
        except Exception:
            pass
        recs.append(dict(symbol=symbol, code=str(code)[-6:], name=name, scale=scale,
                         datalen=datalen, kind="vsa", type=vtype,
                         idx=s.get("idx"), date=str(s.get("date")),
                         conf=vconf, price=0.0, created_ts=time.time(),
                         last_eval_ts=0, status="pending", eval_fails=0, results={},
                         features=s.get("features")))
    if not recs:
        return 0
    # 单次事务: 去重合并 + 立即评估 + 落盘都在一把锁内完成。
    # (旧版双读双写 3.9MB JSON: 每次分析 ~16MB 文件 IO; 合并后减半,
    # 且消除"评估期间他线程落盘被本线程旧视图覆盖"的窗口。)
    with _LOCK:
        records = load_signals()
        existing = {_key(r): r for r in records}
        n_new = 0
        for rec in recs:
            key = _key(rec)
            old = existing.get(key)
            if old is not None:
                # 已评估的保留原结果, 仅更新未评估快照
                if old.get("results"):
                    continue
                existing[key] = rec
                continue
            # 冷却窗去重: 同标的+同类型 在 cooldown_bars 根内有未评估记录 → 合并更新
            if cooldown_bars > 0 and not old:
                dup = _cooldown_dup(existing, rec, df, cooldown_bars)
                if dup is not None:
                    existing[dup] = _merge_cooldown(existing[dup], rec)
                    continue
            existing[key] = rec
            n_new += 1
        # 立即评估 (无需等 cron): 历史信号在当次 df 内已有未来行情
        for rec in recs:
            idx = _locate(df, rec["date"])
            if idx is not None:
                _eval_against(df, idx, rec)
        # 把评估结果同步回存储记录:
        # - 新建/替换的记录与 rec 是同一对象, 评估已就地生效;
        # - 同键保留的旧记录 (已有结果被跳过的) 用新评估刷新 (口径与数据更新)。
        for rec in recs:
            target = existing.get(_key(rec))
            if target is not None and target is not rec and rec.get("results"):
                target["results"] = rec["results"]
                target["status"] = rec["status"]
                target["waiting"] = rec.get("waiting", False)
        save_signals(list(existing.values()))
    invalidate_win_rate_cache()
    return n_new


# ── 评估 ──
def _evaluate_one(rec):
    """拉取最新行情评估单条记录缺失周期。"""
    scale = int(rec.get("scale", 240))
    datalen = max(300, int(rec.get("datalen", 700)) + 80)
    try:
        df = add_indicators(fetch_kline(rec["symbol"], datalen=datalen, scale=scale))
    except Exception:
        fails = int(rec.get("eval_fails", 0)) + 1
        rec["eval_fails"] = fails
        if fails >= 3:
            rec["status"] = "stale"
        return False
    idx = _locate(df, rec.get("date", ""))
    if idx is None:
        fails = int(rec.get("eval_fails", 0)) + 1
        rec["eval_fails"] = fails
        if fails >= 3:
            rec["status"] = "stale"
        return False
    return _eval_against(df, idx, rec)


def evaluate_pending(records, force=False, min_interval=3600, max_records=60):
    """对缺评估周期的记录补评估, 返回新增评估条数。"""
    from ._shared import run_pending_eval
    return run_pending_eval(records, _evaluate_one, HORIZONS,
                            load_signals, save_signals, _key, _LOCK,
                            force=force, min_interval=min_interval,
                            max_records=max_records)


def run_auto_signal_eval(force=False):
    with _LOCK:
        records = load_signals()
    if not records:
        return 0
    return evaluate_pending(records, force=force)


# ── 汇总 ──
# 胜率表缓存: GUI 线程 (fusion/结论) 与扫描线程 (record_signals 失效) 并发访问,
# 必须持锁; 独立于 _LOCK 避免与落盘锁互相嵌套。
_WINRATE_CACHE = None
_WINRATE_LOCK = threading.Lock()

# L1 贝叶斯收缩: 把样本胜率向市场整体基线回归, 替代"n<10 一刀切"的硬门槛。
#   小样本的类型胜率不可靠 (SOS n=77 是 50.6%, n=9 的 PSY 却可能 100%),
#   直接用原值会导致随机噪声被当作信号。收缩公式:
#     p_shrunk = (wins + alpha0 * p0) / (n + alpha0)
#   alpha0 为伪样本量 (先验权重): 20 意味着"n=20 时原值与先验各占一半"。
#   p0 取全池实测上涨占比 (市场基线), 并钳制在 40%~60% 防极端行情污染。
#   为不同 VSA 类型引入分层 p0: 优于随机类型 (UT/ER) p0 略升, 劣于随机类型 (SUP/BC/NS) p0 略降
VSA_PRIOR_P0_ADJ = {
    "UT": 0.03,   # 上升突破: 略升基线 (实际约 52-53%)
    "ER": 0.03,   # Engulfing: 略升基线 (实际约 52-53%)
    "SUP": -0.04, # 支撑反弹: 降低基线 (实际约 46-47%)
    "BC": -0.03,  # 宽振: 降低基线 (实际约 47-48%)
    "NS": -0.02,  # 非摊: 轻微降低基线 (实际约 48-49%)
    "TRU": -0.03, # 真实体: 降低基线
    "default": 0.0, # 其他类型使用全池基线
}
# 各类型的有效 p0 调整范围钳制: p0 = min(max(p0 + adj, 0.38), 0.62)
PRIOR_ALPHA0 = 20
PRIOR_P0_MIN = 0.38
PRIOR_P0_MAX = 0.62
MIN_SHRUNK_N = 3  # 少于该样本量连收缩也无意义 → 直接回退 baseline


def _wilson_ci(n, wins, z=1.96):
    """Wilson 分数区间 (胜率 95% CI), 避免正态近似在小样本下的越界。"""
    if n <= 0:
        return (None, None)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _bayes_shrink(wins, n, p0, alpha0=PRIOR_ALPHA0):
    """贝叶斯收缩胜率: (wins + alpha0*p0) / (n + alpha0)。"""
    return (wins + alpha0 * p0) / (n + alpha0)


def _winrate_key(kind, type_):
    return (kind, str(type_))


def load_win_rates(horizon: int = 20, force: bool = False) -> dict:
    """加载历史信号胜率表 (用于 fusion/结论校准)。

    返回 { (kind, type): {"n": 已评估数, "win": 原始方向命中占比(0~1),
    "shrunk": 贝叶斯收缩占比, "ci_lo"/"ci_hi": Wilson 95% CI,
    "mean": 均收益, "p0": 全池基线} }。n<MIN_SHRUNK_N 的类型不入表。
    shrunk 是校准用的主力值 (消除小样本噪声); win 保留原始口径供展示。
    方向化命中: 标称多头/中性 → ret>0 记命中; 标称空头 (event_dir/vsa_dir<0)
    → ret<0 记命中 (下跌才对)。
    """
    global _WINRATE_CACHE
    with _WINRATE_LOCK:
        if not isinstance(_WINRATE_CACHE, dict):
            _WINRATE_CACHE = {}
        cached = _WINRATE_CACHE.get(horizon)
        if cached is not None and not force:
            return cached
        records = load_signals()
        out = {}
        for r in records:
            kind = r.get("kind", "event")
            type_ = r.get("type", "?")
            res = (r.get("results") or {}).get(str(horizon))
            if not res or res.get("ret") is None:
                continue
            key = _winrate_key(kind, type_)
            s = out.setdefault(key, {"n": 0, "rets": []})
            s["n"] += 1
            s["rets"].append(res["ret"])
        # 方向化命中: 标称多头/中性信号 → 上涨记命中; 标称空头信号 → 下跌记命中。
        # (空头信号如 UTAD/LPSY/SUP 用"上涨占比"口径会把人家的"对"记成"错"。)
        def _hit(kind, type_, v):
            if kind == "event":
                d = event_dir(type_)
            else:
                d = vsa_dir(type_)
            return v < 0 if d < 0 else v >= 0
        # 全池基线 (方向化命中占比), 钳制防极端
        pool_wins = sum(1 for key in out for v in out[key]["rets"]
                        if _hit(key[0], key[1], v))
        pool_n = sum(s["n"] for s in out.values())
        p0_raw = (pool_wins / pool_n) if pool_n else 0.5
        # 按 VSA 类型调整基线: 优于随机类型升高, 劣于随机类型降低
        # 仅对 event kind 的 VSA 类型调整; event 类型保持原 p0
        p0_adj_map = {}
        for key in out:
            kind, type_ = key
            if kind == "vsa" and type_ in VSA_PRIOR_P0_ADJ:
                p0_adj_map[key] = VSA_PRIOR_P0_ADJ[type_]
            else:
                p0_adj_map[key] = 0.0  # event 类型或未列出 VSA 类型不调整
        # 计算加权平均 p0: 所有样本的 p0_raw + 各自调整, 但钳制在有效范围
        p0_sum = 0.0
        p0_count = 0
        for key, adj in p0_adj_map.items():
            # 按样本量加权: n 越大, 调整影响越应反映类型特性
            s = out[key]
            p0_sum += (p0_raw + adj) * s["n"]
            p0_count += s["n"]
        if p0_count > 0:
            p0 = p0_sum / p0_count
        else:
            p0 = p0_raw
        p0 = min(max(p0, PRIOR_P0_MIN), PRIOR_P0_MAX)
        result = {}
        for key, s in out.items():
            if not s["rets"] or s["n"] < MIN_SHRUNK_N:
                continue
            wins = sum(1 for v in s["rets"] if _hit(key[0], key[1], v))
            win = wins / s["n"]
            ci_lo, ci_hi = _wilson_ci(s["n"], wins)
            result[key] = {"n": s["n"], "win": round(win, 4),
                           "shrunk": round(_bayes_shrink(wins, s["n"], p0), 4),
                           "ci_lo": round(ci_lo, 4), "ci_hi": round(ci_hi, 4),
                           "mean": round(statistics.mean(s["rets"]), 6),
                           "p0": round(p0, 4), "alpha0": PRIOR_ALPHA0}
        _WINRATE_CACHE[horizon] = result
        return result


def win_rate_of(kind: str, type_: str, horizon: int = 20,
                baseline: float = 0.5) -> float:
    """取某类型信号的历史方向命中占比 (L1 贝叶斯收缩值; 空头信号以跌为命中)。

    缺失或样本 < MIN_SHRUNK_N → 回退 baseline。与旧版"n<10 一刀切"不同,
    收缩值在 n 较小时仍可安全使用 (向 p0 回归), 消除阈值悬崖效应。
    """
    rates = load_win_rates(horizon)
    key = _winrate_key(kind, str(type_))
    rec = rates.get(key)
    if not rec or rec["n"] < MIN_SHRUNK_N:
        return baseline
    return rec["shrunk"]


def invalidate_win_rate_cache():
    """记录变更后使胜率缓存失效 (下次 load 时重新计算)。"""
    global _WINRATE_CACHE
    with _WINRATE_LOCK:
        _WINRATE_CACHE = {}


def win_rate_profile(kind: str, type_: str, min_n: int = 3):
    """L5 多周期一致性档案: 5/10/20/40 根收缩胜率 + 判定.

    返回 {"horizons": {h: {"n","win","shrunk"}|None}, "consistent": bool,
          "verdict": 结论文本}。用于观测信号边缘是否随时间衰减/反转:
      方向反转   → 20根 与 40根 收缩偏离基线的方向相反 (危险);
      短期有效长期衰减 → 20根有正边缘但 40根大幅回落;
      边缘稳定   → 20根偏离基线且 40根未明显回撤;
      贴近随机   → 20根收缩贴近 50%。
    """
    prof = {}
    for h in HORIZONS:
        rates = load_win_rates(h)
        rec = rates.get(_winrate_key(kind, str(type_)))
        prof[str(h)] = ({n: rec[n] for n in ("n", "win", "shrunk")} if rec else None)
    h20, h40 = prof.get("20"), prof.get("40")
    consistent, verdict = True, "样本不足"
    if h20 and h40 and h20["n"] >= min_n and h40["n"] >= min_n:
        d20 = h20["shrunk"] - 0.5
        d40 = h40["shrunk"] - 0.5
        if d20 * d40 < 0 and abs(d20) >= 0.05 and abs(d40) >= 0.05:
            consistent, verdict = False, "方向反转"
        elif d20 >= 0.05 and d20 - d40 >= 0.10:
            consistent, verdict = True, "短期有效长期衰减"
        elif abs(d20) >= 0.05:
            consistent, verdict = True, "边缘稳定"
        else:
            consistent, verdict = True, "贴近随机"
    return {"horizons": prof, "consistent": bool(consistent), "verdict": verdict}


def signal_stats(records):
    """按 事件/VSA × 类型 汇总命中情况。返回 {kind: {type: {...}}, summary}。"""
    out = {}
    total = len(records)
    evaled = sum(1 for r in records if r.get("results"))
    for kind in ("event", "vsa"):
        by_type = {}
        for r in records:
            if r.get("kind") != kind:
                continue
            t = r.get("type", "?")
            s = by_type.setdefault(t, {"n": 0, "evaluated": 0, "horizons": {},
                                       "conf": {}})
            s["n"] += 1
            results = r.get("results") or {}
            if not results:
                continue
            s["evaluated"] += 1
            for h in HORIZONS:
                res = results.get(str(h))
                if not res or res.get("ret") is None:
                    continue
                rec = s["horizons"].setdefault(str(h), [])
                rec.append(res["ret"])
            conf = int(r.get("conf", 0))
            band = "≥80" if conf >= 80 else ("60-79" if conf >= 60 else
                                             ("40-59" if conf >= 40 else "<40"))
            s["conf"].setdefault(band, []).append(
                (results.get("20", {}).get("ret"), r.get("conf", 0)))
        out[kind] = by_type
    evaluated = evaled
    out["summary"] = {
        "total": total, "evaluated": evaluated, "pending": total - evaluated,
        "stale": sum(1 for r in records if r.get("status") == "stale"),
    }
    return out


def _fmt_stats(stats):
    """把 signal_stats 输出格式化为文本 (供 CLI / 桌面显示)。"""
    lines = []
    s = stats["summary"]
    lines.append(f"信号追踪: 累计 {s['total']} 条, 已评估 {s['evaluated']}, "
                 f"待评估 {s['pending']}, stale {s['stale']}")
    for kind, label in (("event", "威科夫事件"), ("vsa", "VSA")):
        lines.append(f"\n── {label} ──")
        by_type = stats[kind]
        if not by_type:
            lines.append("  (暂无)")
            continue
        rows = []
        for t, s in by_type.items():
            h20 = s["horizons"].get("20", [])
            if not h20:
                rows.append((t, s["n"], 0, 0.0, "无"))
                continue
            d = event_dir(t) if kind == "event" else vsa_dir(t)
            hits = sum(1 for v in h20 if (v < 0 if d < 0 else v > 0))
            hit = hits / len(h20)
            rows.append((t, s["n"], len(h20), hit, statistics.mean(h20)))
        for t, n, ev, up, mean in sorted(rows, key=lambda x: -x[3]):
            lines.append(f"  {t:<8s} n={n:<5d} 评估{ev:<4d} 20根方向命中占比="
                         f"{up*100:5.1f}% 均值={mean*100:+6.2f}%")
    return "\n".join(lines)


def export_signals(records, path=None):
    path = path or SIGNAL_ACCURACY_FILE.replace(".json", "_export.json")
    payload = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "horizons": list(HORIZONS),
        "note": "逐信号准确度追踪: 每个事件/VSA信号之后真实行情收益; "
                "status=pending 为尚未走满评估周期。",
        "stats": signal_stats(records),
        "records": records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


def export_signals_csv(records, path=None):
    """导出信号准确度记录到 CSV (便于 Excel/其他工具分析)。"""
    import csv
    path = path or SIGNAL_ACCURACY_FILE.replace(".json", ".csv")
    cols = ["symbol", "code", "name", "scale", "kind", "type", "date", "conf",
            "price", "status", "created_ts"]
    for h in HORIZONS:
        cols += [f"ret_{h}", f"hi_{h}", f"lo_{h}", f"up_hit_{h}", f"down_hit_{h}"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for r in records:
            res = r.get("results") or {}
            row = [r.get("symbol"), r.get("code"), r.get("name"), r.get("scale"),
                   r.get("kind"), r.get("type"), r.get("date"), r.get("conf"),
                   r.get("price"), r.get("status"), r.get("created_ts")]
            for h in HORIZONS:
                hh = res.get(str(h)) or {}
                row += [hh.get("ret"), hh.get("hi"), hh.get("lo"),
                        hh.get("up_hit"), hh.get("down_hit")]
            wr.writerow(row)
    return path


def export_review_report(records=None, path=None, days=7, markdown=True):
    """导出信号复盘周报 (Markdown/HTML): 近 days 天内新增信号的命中/止损明细。

    内容:
      - 周期内新增信号总数、已评估数、类型分布
      - 按信号类型汇总的 5/20 根胜率 (vs 历史累计)
      - 逐信号明细: 日期/标的/类型/判断方向/预期收益(类型历史20根方向化均值)/
        实际收益(20根方向化真实收益)/入场价/各周期收益
    返回写入路径。
    """
    import datetime
    records = records if records is not None else load_signals()
    stats = signal_stats(records)
    s = stats["summary"]
    cutoff_dt = pd.Timestamp(datetime.date.today() - datetime.timedelta(days=days))
    recent = []
    for r in records:
        d = _sig_date(r)
        if d is None or d.date() < cutoff_dt.date():
            continue
        recent.append(r)
    recent.sort(key=lambda r: str(r.get("date", "")), reverse=True)

    if markdown:
        return _render_markdown_report(recent, stats, s, days, path=path, records=records)
    return _render_html_report(recent, stats, s, days, path=path, records=records)


def _render_markdown_report(recent, stats, summary, days, path=None, records=None):
    import os
    lines = [f"# 威科夫信号复盘周报 ({days}天)", "",
             f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 周期内新增信号: {len(recent)} 条",
             f"- 累计: {summary['total']} 条 · 已评估 {summary['evaluated']} · "
             f"待评估 {summary['pending']}", ""]

    # 类型汇总
    lines += ["## 类型胜率 (近周期已评估信号)", "",
              "| 类别 | 类型 | 样本 | 5根胜率 | 20根胜率 | 20根均值 |",
              "|---|---|---|---|---|---|"]
    kinds = (("event", "威科夫事件"), ("vsa", "VSA"))
    for kind, label in kinds:
        by_type = stats.get(kind) or {}
        if not by_type:
            continue
        lines += [f"**{label}**", ""]
        rows = []
        for t, st in by_type.items():
            h5 = st["horizons"].get("5", [])
            h20 = st["horizons"].get("20", [])
            if not h20:
                continue
            d = event_dir(t) if kind == "event" else vsa_dir(t)
            w5 = (sum(1 for v in h5 if (v < 0 if d < 0 else v > 0)) / len(h5)
                  if h5 else None)
            w20 = sum(1 for v in h20 if (v < 0 if d < 0 else v > 0)) / len(h20)
            rows.append((t, st["evaluated"], w5, w20, statistics.mean(h20)))
        for t, n, w5, w20, m in sorted(rows, key=lambda x: -x[3]):
            w5s = f"{w5 * 100:.0f}%" if w5 is not None else "-"
            lines.append(f"| {label} | {t} | {n} | {w5s} | {w20 * 100:.0f}% | "
                         f"{m * 100:+.1f}% |")
        lines.append("")

    # 明细
    # 准确性验证 (Rank IC / Bootstrap CI / 置换显著性 / 样本外胜率)
    try:
        from .validation import validation_lines
        vlines = validation_lines(records or recent)
        if vlines:
            lines += ["## 信号准确性验证", ""] + [f"- {l}" for l in vlines] + [""]
    except Exception:
        pass

    def _dir_of(r):
        t = r.get("type", "")
        return event_dir(t) if r.get("kind") == "event" else vsa_dir(t)

    # 预期收益查找表: 该类型历史已评估 20 根收益的方向化均值 ( favorable move )
    _exp = {}
    for kind in ("event", "vsa"):
        for t, st in ((stats.get(kind) or {}).items()):
            h20 = st["horizons"].get("20") or []
            if h20:
                d = event_dir(t) if kind == "event" else vsa_dir(t)
                m = statistics.mean(h20)
                _exp[(kind, t)] = m if d >= 0 else -m

    # 近周期命中统计 (按实证强/弱梯队分组)
    evaled = []
    for r in recent:
        r20 = ((r.get("results") or {}).get("20") or {}).get("ret")
        if r20 is None:
            continue
        d = _dir_of(r)
        tier = "强" if (r.get("kind") == "event"
                        and r.get("type") in STRONG_TIER_TYPES) else "弱"
        evaled.append((r, r20 if d >= 0 else -r20, d, tier))
    lines += ["## 近周期命中统计", ""]
    if evaled:
        wins = sum(1 for _, a, _, _ in evaled if a > 0)
        exps = [_exp.get((r.get("kind"), r.get("type"))) for r, _, _, _ in evaled]
        exps = [e for e in exps if e is not None]
        lines += [
            f"- 窗口内新增 **{len(recent)}** 条, 已走满 20 根 **{len(evaled)}** 条",
            f"- 整体方向命中: **{wins}/{len(evaled)} ({wins / len(evaled) * 100:.1f}%)**"
            f" · 平均预期收益 {f'{statistics.mean(exps) * 100:+.2f}%' if exps else '-'}"
            f" · 平均实际收益 {statistics.mean(a for _, a, _, _ in evaled) * 100:+.2f}%",
        ]
        for tier, label in (("强", "强信号组 (Spring/Shakeout/UTAD/LPSY/ST/LPS/SC)"),
                            ("弱", "弱信号组 (其余事件 + 全部 VSA, 仅作确认证据)")):
            grp = [x for x in evaled if x[3] == tier]
            if not grp:
                continue
            gw = sum(1 for _, a, _, _ in grp if a > 0)
            gex = [_exp.get((r.get("kind"), r.get("type"))) for r, _, _, _ in grp]
            gex = [e for e in gex if e is not None]
            lines.append(
                f"- **{label}**: {gw}/{len(grp)} ({gw / len(grp) * 100:.1f}%)"
                f" · 平均实际收益 {statistics.mean(a for _, a, _, _ in grp) * 100:+.2f}%")
        lines.append("")
    else:
        lines += [f"- 窗口内新增 {len(recent)} 条, 尚无走满 20 根的信号", ""]

    # 明细
    lines += ["## 近周期信号明细", ""]
    lines += ["| 日期 | 代码 | 名称 | 类别 | 类型 | 组别 | 方向 | 预期收益 "
              "| 实际收益 | 入场价 | 5根 | 10根 | 20根 | 40根 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    if not recent:
        lines += ["| (无) | | | | | | | | | | | | | |"]
    for r in recent:
        kind_cn = "事件" if r.get("kind") == "event" else "VSA"
        res = r.get("results") or {}
        cells = []
        for h in HORIZONS:
            rr = res.get(str(h))
            cells.append(f"{rr['ret'] * 100:+.1f}%" if rr and rr.get("ret") is not None
                         else "-")
        d = _dir_of(r)
        dir_cn = "多头" if d > 0 else ("空头" if d < 0 else "中性")
        tier_cn = "强" if (r.get("kind") == "event"
                           and r.get("type") in STRONG_TIER_TYPES) else "弱"
        exp = _exp.get((r.get("kind"), r.get("type")))
        exp_s = f"{exp * 100:+.1f}%" if exp is not None else "-"
        # 实际收益: 20 根真实收益按方向化 (多头取 ret, 空头取 -ret), 正=信号做对
        r20 = (res.get("20") or {}).get("ret")
        act_s = "-"
        if r20 is not None:
            act_s = f"{(r20 if d >= 0 else -r20) * 100:+.1f}%"
        lines.append(f"| {r.get('date', '')[:10]} | {r.get('code')} | "
                     f"{r.get('name') or ''} | {kind_cn} | {r.get('type')} | "
                     f"{tier_cn} | {dir_cn} | {exp_s} | {act_s} | "
                     f"{r.get('price') or '-'} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("> 说明: 组别=实证梯队 (强: Spring/Shakeout/UTAD/LPSY/ST/LPS/SC; "
                 "弱: 其余), 依据 docs/accuracy_report.md 的 6000+ 样本实测; "
                 "方向=该信号类型的标称方向; "
                 "预期收益=同类型历史已评估信号的 20 根方向化均值收益; "
                 "实际收益=本条信号 20 根真实收益的方向化值 (正=做对, 负=做错); "
                 "5/10/20/40 根为未方向化的原始累计收益。未走满周期显示为 `-`。")

    path = path or os.path.join(os.path.dirname(SIGNAL_ACCURACY_FILE),
                                "wx_signal_review.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _render_html_report(recent, stats, summary, days, path=None, records=None):
    import os
    md = _render_markdown_report(recent, stats, summary, days, path=None,
                                 records=records)
    body = "\n".join(_html_escape(l) for l in md.splitlines())
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>威科夫信号复盘周报</title><style>
body{{font-family:'Noto Sans CJK SC',sans-serif;margin:24px;color:#1f2937}}
h1{{color:#2563eb}} h2{{color:#1d4fd7;border-left:4px solid #2563eb;padding-left:8px}}
table{{border-collapse:collapse;margin:8px 0}}
th,td{{border:1px solid #dce3f0;padding:4px 10px;font-size:13px}}
th{{background:#eef2f9}} code{{background:#eef2ff;padding:1px 5px}}
blockquote{{color:#8a94a6;border-left:3px solid #dce3f0;margin-left:0;padding-left:12px}}
</style></head><body>{body}</body></html>"""
    path = path or os.path.join(os.path.dirname(SIGNAL_ACCURACY_FILE),
                                "wx_signal_review.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def _html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


if __name__ == "__main__":
    import sys
    if "--eval" in sys.argv:
        n = run_auto_signal_eval(force=True)
        nd = expire_stale_signals()
        recs = load_signals()
        print(_fmt_stats(signal_stats(recs)))
        print(f"\n本次新增评估 {n} 条, 清理过期 {nd} 条")
    elif "--export" in sys.argv:
        p = export_signals(load_signals())
        print(f"已导出: {p}")
    elif "--export-csv" in sys.argv:
        p = export_signals_csv(load_signals())
        print(f"已导出 CSV: {p}")
    elif "--report" in sys.argv:
        i = sys.argv.index("--report")
        days = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 7
        p = export_review_report(days=days, markdown=True)
        print(f"已导出复盘周报: {p}")
    else:
        print(_fmt_stats(signal_stats(load_signals())))
        print("命令: --eval 评估到期信号 / --export 导出 / --report [天] 复盘周报")
