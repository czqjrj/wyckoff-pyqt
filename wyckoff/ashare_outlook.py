"""A股市场状态 / 历史类比 / 风险预算系统。

设计原则:
- **不预测单日涨跌**。系统只负责描述"当前市场处于什么状态", 并用历史上
  相似状态下的未来收益分布给出经验概率。
- 所有历史特征只使用当日及以前的数据; 未来收益只用于统计, 不参与当前判断。
- 数据源全部 fail-soft: 单个接口失败不影响整体报告。
- 输出的是"状态 + 概率 + 关键位 + 风险预算", 不是买卖指令。

对外主入口:
    build_outlook(primary="sh000001") -> dict
    render_report(result) -> str
"""

from __future__ import annotations

import bisect
import json
import math
import os
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

from ._log import log_exc
from .datasource import fetch_kline
from .events import detect_all
from .indicators import add_indicators, find_pivots
from .market import fetch_market_env
from .market_dashboard import (
    build_market_resonance,
    fetch_emotion_board,
    fetch_market_amount,
    fetch_market_breadth,
    fetch_market_fund_flow,
    fetch_sector_ranking,
)
from .paths import DATA_DIR
from .phases import judge_phase
from .pnf import build_pnf, pnf_targets, pnf_volume

# ── 默认覆盖的宽基指数 ──
OUTLOOK_INDICES = [
    ("sh000001", "上证指数"),
    ("sh000300", "沪深300"),
    ("sz399006", "创业板指"),
    ("sh000905", "中证500"),
    ("sh000688", "科创50"),
]

HORIZONS = (5, 20, 60)
MIN_HISTORY = 260  # 至少 260 根日线才参与历史类比/回测
PCT_MIN_HISTORY = 120  # 分位计算最少有效样本 (短样本也能给出分位)
ANALOG_K = 60  # 取最近邻数量
ANALOG_MIN = 20  # 少于此数量视为样本不足

# 历史类比使用的特征及权重 (顺序须与 _analog_matrix 一致)
_ANALOG_COLS = [
    "regime_score",
    "ma200_dev",
    "ret20",
    "ret60",
    "vola_pct250",
    "dd250",
    "vol_pct120",
]
_ANALOG_W = np.array([0.35, 0.15, 0.15, 0.10, 0.10, 0.10, 0.05])

# 行业风格篮子 (名称与项目行业板块口径一致)
DEFENSIVE_SECTORS = {
    "银行",
    "电力",
    "公路铁路运输",
    "煤炭开采加工",
    "燃气",
    "种植业与林业",
    "养殖业",
    "农产品加工",
    "白色家电",
}
GROWTH_SECTORS = {
    "半导体",
    "医疗服务",
    "电池",
    "软件开发",
    "通信设备",
    "元件",
    "光学光电子",
    "游戏",
    "计算机设备",
    "电子化学品",
    "自动化设备",
}

DISCLAIMER = (
    "本报告由项目内置量化/技术模块生成, 仅用于研究复盘, 不构成投资建议。历史概率不代表未来结果。"
)


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────


def _f(x, default=None):
    """安全转 float。"""
    if x is None:
        return default
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _round(x, digits=2):
    v = _f(x)
    return None if v is None else round(v, digits)


def _last_pct(window) -> float:
    """当前值在窗口内的分位 (0-100), 只用窗口内历史值比较。"""
    if window is None or len(window) < 2:
        return float("nan")
    last = window[-1]
    if not math.isfinite(float(last)):
        return float("nan")
    return float(np.mean(np.asarray(window[:-1], dtype=float) < last) * 100)


def _stats(values) -> dict:
    """未来收益分布统计。"""
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "up_prob": round(float(np.mean(arr > 0)), 3),
        "median": round(float(np.median(arr)), 2),
        "mean": round(float(np.mean(arr)), 2),
        "p10": round(float(np.percentile(arr, 10)), 2),
        "p90": round(float(np.percentile(arr, 90)), 2),
        "min": round(float(np.min(arr)), 2),
        "max": round(float(np.max(arr)), 2),
    }


def _safe(label, fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - 数据源必须 fail-soft
        log_exc(f"A股展望数据失败: {label}", e)
        return default


DATA_CACHE_FILE = os.path.join(DATA_DIR, "wx_ashare_cache.json")


class _TTLCache:
    """带过期回退 + 磁盘持久化的简单缓存。

    - TTL 内直接复用;
    - 新进程启动时, 磁盘里 TTL 内的数据可直接用, 避免重复拉;
    - 过了 TTL 重新拉, 失败时回退内存/磁盘上一次成功值并标记 stale=True;
    - 没有历史值时返回 None + ok=False。
    """

    def __init__(self, disk_path: str | None = DATA_CACHE_FILE):
        self._data: dict[str, dict] = {}
        self._fail_until: dict[str, float] = {}  # 失败退避: key -> 可重试时间戳
        self._lock = threading.Lock()
        self._disk_path = disk_path
        self._disk = self._load_disk()
        raw_stats = self._disk.pop("__stats__", {})
        self._stats: dict[str, dict] = raw_stats if isinstance(raw_stats, dict) else {}

    def _load_disk(self) -> dict:
        if not self._disk_path or not os.path.exists(self._disk_path):
            return {}
        try:
            with open(self._disk_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            log_exc("读取A股缓存失败", e)
            return {}

    def _save_disk(self) -> None:
        if not self._disk_path:
            return
        try:
            folder = os.path.dirname(self._disk_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self._disk_path + ".tmp"
            payload = dict(self._disk)
            payload["__stats__"] = self._stats
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=str)
            os.replace(tmp, self._disk_path)
        except Exception as e:  # noqa: BLE001
            log_exc("写入A股缓存失败", e)

    @staticmethod
    def _ok(value) -> bool:
        if isinstance(value, dict) and "ok" in value:
            return bool(value.get("ok"))
        if value is None:
            return False
        try:
            return len(value) > 0
        except TypeError:
            return True

    def _record_stat(self, key: str, ok: bool, elapsed_ms: float, error: str = "") -> None:
        st = self._stats.get(key)
        if not isinstance(st, dict):
            st = {}
        st["ok"] = int(st.get("ok") or 0) + (1 if ok else 0)
        st["fail"] = int(st.get("fail") or 0) + (0 if ok else 1)
        st["last_ms"] = round(float(elapsed_ms), 1)
        now_s = time.strftime("%Y-%m-%d %H:%M:%S")
        st["last_attempt"] = now_s
        if ok:
            st["last_success"] = now_s
            st["last_error"] = ""
        else:
            st["last_error"] = str(error)[:200]
        self._stats[key] = st

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            out = {}
            for k, v in self._stats.items():
                if not isinstance(v, dict):
                    continue
                row = dict(v)
                remain = self._fail_until.get(k, 0.0) - now
                if remain > 0:
                    row["cooldown_s"] = int(round(remain))
                out[k] = row
            return out

    def get(self, key: str, ttl: float, fn, fail_cooldown: float = 0.0) -> tuple[object, dict]:
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry and now - entry["ts"] < ttl:
                return entry["value"], dict(entry["quality"], stale=False)
            # 新进程: 磁盘里 TTL 内的数据可直接用
            disk = self._disk.get(key)
            if disk and now - _f(disk.get("ts"), 0.0) < ttl:
                self._data[key] = disk
                return disk["value"], dict(disk["quality"], stale=False)
            # 失败退避: 冷却期内不再打同一个数据源, 直接回退旧值
            until = self._fail_until.get(key, 0.0)
            if until > now:
                remain = int(round(until - now))
                msg = f"退避中（上次失败，{remain}s 后重试）"
                stale = entry or disk
                if stale:
                    q = dict(stale["quality"])
                    q["stale"] = True
                    q["error"] = msg
                    return stale["value"], q
                return None, {
                    "ok": False,
                    "stale": False,
                    "as_of": None,
                    "error": msg,
                }
        started = time.perf_counter()
        try:
            value = fn()
            elapsed_ms = (time.perf_counter() - started) * 1000
            quality = {
                "ok": self._ok(value),
                "stale": False,
                "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": "",
            }
            entry = {"ts": now, "value": value, "quality": quality}
            with self._lock:
                self._data[key] = entry
                self._disk[key] = entry
                self._fail_until.pop(key, None)
                self._record_stat(key, True, elapsed_ms)
                # 空结果也视为软失败: 短时间内不再重复打源
                if fail_cooldown > 0 and not quality["ok"]:
                    self._fail_until[key] = time.time() + fail_cooldown
                self._save_disk()
            return value, dict(quality)
        except Exception as e:  # noqa: BLE001
            elapsed_ms = (time.perf_counter() - started) * 1000
            err = f"{type(e).__name__}: {e}"
            log_exc(f"数据源失败: {key}", e)
            with self._lock:
                self._record_stat(key, False, elapsed_ms, err)
                if fail_cooldown > 0:
                    self._fail_until[key] = time.time() + fail_cooldown
                entry = self._data.get(key) or self._disk.get(key)
                self._save_disk()
            if entry:
                q = dict(entry["quality"])
                q["stale"] = True
                q["error"] = err
                return entry["value"], q
            return None, {
                "ok": False,
                "stale": False,
                "as_of": None,
                "error": err,
            }


_DATA_CACHE = _TTLCache()


# ─────────────────────────────────────────────────────────────
# 数据与特征
# ─────────────────────────────────────────────────────────────


def prepare_index_frame(code: str, datalen: int = 1200) -> pd.DataFrame | None:
    """拉取指数日线并补指标; 数据不足返回 None。"""
    try:
        df = fetch_kline(code, datalen=datalen, scale=240)
        if df is None or len(df) < MIN_HISTORY:
            return None
        df = df.copy()
        df["day"] = pd.to_datetime(df["day"])
        df = df.sort_values("day").reset_index(drop=True)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df) < MIN_HISTORY:
            return None
        return add_indicators(df, symbol=code)
    except Exception as e:  # noqa: BLE001
        log_exc(f"指数日线准备失败 {code}", e)
        return None


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """在日线基础上计算状态特征 (全部只使用当日及以前数据)。"""
    out = df.copy()
    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce")

    ma20 = out.get("price_ma20")
    if ma20 is None:
        ma20 = close.rolling(20, min_periods=20).mean()
    ma50 = out.get("price_ma50")
    if ma50 is None:
        ma50 = close.rolling(50, min_periods=50).mean()
    ma200 = out.get("price_ma200")
    if ma200 is None:
        ma200 = close.rolling(200, min_periods=120).mean()

    out["ma20"] = pd.to_numeric(ma20, errors="coerce")
    out["ma50"] = pd.to_numeric(ma50, errors="coerce")
    out["ma200"] = pd.to_numeric(ma200, errors="coerce")

    out["ret5"] = close.pct_change(5) * 100
    out["ret20"] = close.pct_change(20) * 100
    out["ret60"] = close.pct_change(60) * 100
    out["ma20_dev"] = (close / out["ma20"] - 1) * 100
    out["ma50_dev"] = (close / out["ma50"] - 1) * 100
    out["ma200_dev"] = (close / out["ma200"] - 1) * 100
    out["ma20_slope"] = (out["ma20"] / out["ma20"].shift(5) - 1) * 100
    out["ma50_slope"] = (out["ma50"] / out["ma50"].shift(20) - 1) * 100

    out["vol20"] = close.pct_change().rolling(20, min_periods=20).std() * math.sqrt(252) * 100
    atr = out.get("atr14")
    if atr is None:
        atr = out.get("atr")
    if atr is None:
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14, min_periods=14).mean()
    out["atr_pct"] = pd.to_numeric(atr, errors="coerce") / close * 100
    out["dd250"] = (close / high.rolling(250, min_periods=60).max() - 1) * 100
    out["vol_pct120"] = volume.rolling(120, min_periods=40).apply(_last_pct, raw=True)
    out["vola_pct250"] = out["vol20"].rolling(250, min_periods=60).apply(_last_pct, raw=True)
    return out


REGIME_SCORE_VERSION = 2


def regime_score_series(f: pd.DataFrame) -> pd.Series:
    """价格状态分 v2 (0-100)。

    v2 = 0.6×趋势 + 0.4×回撤 (2026-11 消融后配方)。

    消融依据 (2021-11~2026-11, 上证/沪深300/创业板/中证500, 未来20日收益):
    - 趋势是唯一在全部指数 IC 为正的组件;
    - 动量 (上证 t≈-4.7) 与低波动 (沪深300 t≈-5.5) 出现显著负 IC, 移除;
    - v2 配方在 4 个指数的全样本与最近 252 日回测均不劣于旧配方, 换手更低。
    全部为价量特征, 可在历史任意一天用当日及以前数据计算。
    """
    from .ashare_backtest import score_components

    comp = score_components(f)
    if comp is None:
        return pd.Series(50.0, index=f.index)
    score = 0.6 * comp["trend"] + 0.4 * comp["drawdown"]
    return pd.Series(score, index=f.index).clip(0, 100)


def state_label(score: float | None) -> str:
    v = _f(score, 0.0)
    if v >= 70:
        return "进攻"
    if v >= 50:
        return "中性偏多"
    if v >= 30:
        return "防御"
    if v >= 20:
        return "谨慎"
    return "风险规避"


LABEL_VERSION = 3  # 1/2=绝对分档; 3=历史分位制
STATE_NAMES = ("风险规避", "谨慎", "防御", "中性偏多", "进攻")


def state_label_pct(pct: float | None) -> str:
    """按“分数在自身历史中的分位”给状态 (0-100, 无前视)。

    ≤10% 风险规避 / 10-25% 谨慎 / 25-50% 防御 / 50-75% 中性偏多 / >75% 进攻。
    分位制让 5 档都能出现, 避免绝对分档下“风险规避”不可达的问题。
    """
    v = _f(pct, 50.0)
    if v <= 10:
        return "风险规避"
    if v <= 25:
        return "谨慎"
    if v <= 50:
        return "防御"
    if v <= 75:
        return "中性偏多"
    return "进攻"


def score_percentile_series(scores, valid_mask=None) -> np.ndarray:
    """逐日分数在“截至当日”历史中的分位 (0-100), 不使用未来数据。

    valid_mask 用于排除预热行 (如 ma200 为 NaN 时的 0 分假值), 避免污染分布。
    """
    arr = np.asarray(scores, dtype=float)
    mask = None if valid_mask is None else np.asarray(valid_mask, dtype=bool)
    out = np.full(len(arr), np.nan, dtype=float)
    hist: list[float] = []
    for i, v in enumerate(arr):
        if not np.isfinite(v):
            continue
        if mask is not None and not mask[i]:
            continue
        bisect.insort(hist, float(v))
        if len(hist) >= PCT_MIN_HISTORY:
            out[i] = bisect.bisect_right(hist, float(v)) / len(hist) * 100
    return out


def state_transitions(f: pd.DataFrame, horizons=HORIZONS) -> dict:
    """经验状态转移: 当前状态之后 h 日落在各状态的历史比例。

    只统计已知历史, 不是预测; 相邻样本重叠, 独立样本少。
    """
    n = len(f)
    if n <= MIN_HISTORY + max(horizons):
        return {"ok": False, "reason": "历史样本不足"}
    score = pd.to_numeric(f["regime_score"], errors="coerce").to_numpy(dtype=float)
    ma200 = (
        pd.to_numeric(f["ma200"], errors="coerce").to_numpy(dtype=float) if "ma200" in f else None
    )
    pct = score_percentile_series(score, np.isfinite(ma200) if ma200 is not None else None)
    labels = np.full(n, "", dtype=object)
    for i in range(MIN_HISTORY - 1, n):
        if np.isfinite(pct[i]):
            labels[i] = state_label_pct(pct[i])
    valid_idx = np.array([i for i in range(n) if labels[i]], dtype=int)
    if len(valid_idx) < 60:
        return {"ok": False, "reason": "有效状态样本不足"}
    last = n - 1
    current = state_label_pct(pct[last]) if np.isfinite(pct[last]) else "未知"
    cur_i = STATE_NAMES.index(current) if current in STATE_NAMES else None
    out = {
        "ok": True,
        "current": current,
        "current_score": _round(score[last], 1),
        "current_pct": _round(pct[last], 1),
        "label_version": LABEL_VERSION,
        "label_basis": "历史分位",
        "horizons": {},
        "durations": {},
    }
    for h in horizons:
        counts = np.zeros((len(STATE_NAMES), len(STATE_NAMES)), dtype=float)
        for i in valid_idx:
            j = i + h
            if j > last or not labels[j]:
                continue
            counts[STATE_NAMES.index(labels[i]), STATE_NAMES.index(labels[j])] += 1
        row = counts[cur_i] if cur_i is not None else np.zeros(len(STATE_NAMES))
        total = float(row.sum())
        probs = row / total if total > 0 else np.zeros(len(STATE_NAMES))
        hz = {
            "n": int(total),
            "probs": [round(float(p), 3) for p in probs],
        }
        if cur_i is not None:
            hz["better"] = round(float(probs[cur_i + 1 :].sum()), 3)
            hz["same"] = round(float(probs[cur_i]), 3)
            hz["worse"] = round(float(probs[:cur_i].sum()), 3)
        out["horizons"][str(h)] = hz

    # 状态持续期 (连续相同状态的交易日数)
    runs: dict[str, list[int]] = {}
    prev = None
    length = 0
    for i in valid_idx:
        lab = labels[i]
        if lab == prev:
            length += 1
        else:
            if prev is not None:
                runs.setdefault(prev, []).append(length)
            prev = lab
            length = 1
    if prev is not None:
        runs.setdefault(prev, []).append(length)
    out["durations"] = {s: round(float(np.mean(v)), 1) for s, v in runs.items() if v}
    out["durations_median"] = {s: round(float(np.median(v)), 1) for s, v in runs.items() if v}
    out["counts"] = {s: int(np.sum(labels == s)) for s in STATE_NAMES}
    out["unreachable"] = [s for s in STATE_NAMES if out["counts"][s] == 0]
    out["note"] = "经验转移概率，不是预测；相邻样本重叠，独立样本少。"
    return out


def feature_snapshot(f: pd.DataFrame, code: str, name: str) -> dict:
    """取最新一根 bar 的状态快照。"""
    row = f.iloc[-1]
    close = _f(row.get("close"), 0.0)
    prev = _f(f["close"].iloc[-2], close) if len(f) >= 2 else close
    score = _f(row.get("regime_score"), 0.0)
    mask = None
    if "ma200" in f:
        mask = np.isfinite(pd.to_numeric(f["ma200"], errors="coerce").to_numpy(dtype=float))
    pct_series = score_percentile_series(
        pd.to_numeric(f["regime_score"], errors="coerce").to_numpy(dtype=float), mask
    )
    score_pct = round(float(pct_series[-1]), 1) if np.isfinite(pct_series[-1]) else None

    trend_flags = {
        "价>MA20": bool(close > _f(row.get("ma20"), close + 1)),
        "MA20>MA50": bool(_f(row.get("ma20"), 0) > _f(row.get("ma50"), 1e18)),
        "价>MA200": bool(close > _f(row.get("ma200"), close + 1)),
        "MA20向上": bool(_f(row.get("ma20_slope"), 0) > 0),
        "MA50向上": bool(_f(row.get("ma50_slope"), 0) > 0),
    }
    ret20 = _f(row.get("ret20"), 0.0) or 0.0
    momentum_score = 50 + 50 * math.tanh(ret20 / 10.0)
    vol_score = 100 - (_f(row.get("vola_pct250"), 50.0) or 50.0)
    dd_score = max(0.0, min(100.0, 100 + 2.0 * (_f(row.get("dd250"), -50.0) or -50.0)))
    return {
        "code": code,
        "name": name,
        "date": str(row.get("day"))[:10],
        "close": _round(close),
        "day_pct": _round((close / prev - 1) * 100 if prev else 0.0),
        "score": _round(score, 1),
        "score_pct": score_pct,
        "state": state_label_pct(score_pct),
        "state_abs": state_label(score),
        "label_version": LABEL_VERSION,
        "trend_flags": trend_flags,
        "trend_score": _round(sum(20 for ok in trend_flags.values() if ok), 1),
        "momentum_score": _round(momentum_score, 1),
        "vol_score": _round(vol_score, 1),
        "drawdown_score": _round(dd_score, 1),
        "ret5": _round(row.get("ret5")),
        "ret20": _round(row.get("ret20")),
        "ret60": _round(row.get("ret60")),
        "ma20": _round(row.get("ma20")),
        "ma50": _round(row.get("ma50")),
        "ma200": _round(row.get("ma200")),
        "ma20_dev": _round(row.get("ma20_dev")),
        "ma50_dev": _round(row.get("ma50_dev")),
        "ma200_dev": _round(row.get("ma200_dev")),
        "atr_pct": _round(row.get("atr_pct")),
        "vola_pct250": _round(row.get("vola_pct250"), 0),
        "vol_pct120": _round(row.get("vol_pct120"), 0),
        "dd250": _round(row.get("dd250")),
    }


# ─────────────────────────────────────────────────────────────
# 历史类比 / 回测
# ─────────────────────────────────────────────────────────────


def _analog_matrix(f: pd.DataFrame) -> np.ndarray:
    return f[_ANALOG_COLS].to_numpy(dtype=float)


def _analog_candidates(n: int, max_h: int) -> np.ndarray:
    """历史类比候选: 至少 MIN_HISTORY 根历史, 且未来 max_h 根已走完。"""
    if n <= MIN_HISTORY + max_h:
        return np.empty(0, dtype=int)
    return np.arange(MIN_HISTORY, n - max_h, dtype=int)


def _wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """Wilson 95% 置信区间 (小样本比正态近似稳)。"""
    if n <= 0:
        return None, None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _independent_subset(sel, fwd, horizon: int) -> np.ndarray:
    """按非重叠窗口挑样本: 相邻相似日高度相关, 不能当独立样本。"""
    picked = []
    last = None
    for i, r in zip(sel, fwd):
        i = int(i)
        if last is None or i >= last + horizon:
            picked.append(float(r))
            last = i
    return np.asarray(picked, dtype=float)


def historical_analogs(f: pd.DataFrame, horizons=HORIZONS, k: int = ANALOG_K) -> dict:
    """找历史相似状态, 统计未来 5/20/60 日收益分布。

    相似度: 状态分/均线偏离/20-60日动量/波动分位/回撤/量能分位,
    标准化后加权欧氏距离。候选样本只取未来收益已知的历史日。
    """
    n = len(f)
    max_h = max(horizons)
    cand = _analog_candidates(n, max_h)
    if len(cand) < ANALOG_MIN:
        return {"ok": False, "reason": "历史样本不足", "n_analogs": 0}

    mat = _analog_matrix(f)
    cur = mat[-1]
    hist = mat[cand]

    # 用当前可见的历史分布做标准化 (不含当前 bar, 避免自我匹配偏差)
    base = mat[MIN_HISTORY - 1 : -1]
    base = base[np.isfinite(base).all(axis=1)]
    if len(base) < ANALOG_MIN:
        return {"ok": False, "reason": "有效历史样本不足", "n_analogs": 0}

    med = np.nanmedian(base, axis=0)
    q75 = np.nanpercentile(base, 75, axis=0)
    q25 = np.nanpercentile(base, 25, axis=0)
    scale = q75 - q25
    std = np.nanstd(base, axis=0)
    scale = np.where(scale > 1e-9, scale, std)
    scale = np.where(scale > 1e-9, scale, 1.0)

    z_cur = np.nan_to_num((cur - med) / scale, nan=0.0, posinf=0.0, neginf=0.0)
    z_hist = np.nan_to_num((hist - med) / scale, nan=0.0, posinf=0.0, neginf=0.0)
    dist = np.sqrt(((z_hist - z_cur) ** 2 * _ANALOG_W).sum(axis=1))
    order = np.argsort(dist)
    kk = min(int(k), len(order))
    sel = cand[order[:kk]]
    avg_dist = float(np.mean(dist[order[:kk]]))
    dist_pct = float(np.mean(dist < avg_dist) * 100)

    close = f["close"].to_numpy(dtype=float)
    out = {
        "ok": True,
        "n_analogs": int(kk),
        "avg_distance": round(avg_dist, 2),
        "distance_pct": round(dist_pct, 1),
        "horizons": {},
    }
    for h in horizons:
        fwd = (close[sel + h] / close[sel] - 1) * 100
        st = _stats(fwd)
        if st.get("n", 0) >= 10:
            ind = _independent_subset(sel, fwd, h)
            n_eff = int(len(ind))
            if n_eff > 0:
                hits_eff = int(np.sum(ind > 0))
                lo, hi = _wilson_interval(hits_eff, n_eff)
                st["up_prob_ind"] = round(hits_eff / n_eff, 3)
                st["up_prob_adj"] = round((hits_eff + 0.5) / (n_eff + 1.0), 3)
                st["up_ci_low"] = round(lo, 3)
                st["up_ci_high"] = round(hi, 3)
            st["n_eff"] = n_eff
            st["samples"] = [round(float(x), 2) for x in fwd]
            out["horizons"][str(h)] = st
    return out


def regime_backtest(f: pd.DataFrame, horizon: int = 20) -> dict:
    """状态分 → 未来收益的历史分档统计 (含 MA200 基线)。"""
    n = len(f)
    if n <= MIN_HISTORY + horizon:
        return {"ok": False, "reason": "历史样本不足"}
    close = f["close"].to_numpy(dtype=float)
    fwd = np.full(n, np.nan, dtype=float)
    fwd[: n - horizon] = (close[horizon:] / close[: n - horizon] - 1) * 100
    score = f["regime_score"].to_numpy(dtype=float)
    idx = np.arange(n)
    valid = np.isfinite(score) & np.isfinite(fwd) & (idx >= MIN_HISTORY)

    buckets = [
        (0, 20, "风险规避"),
        (20, 30, "谨慎"),
        (30, 50, "防御"),
        (50, 70, "中性偏多"),
        (70, 100.1, "进攻"),
    ]
    rows = []
    for lo, hi, label in buckets:
        mask = valid & (score >= lo) & (score < hi)
        if mask.sum() == 0:
            continue
        st = _stats(fwd[mask])
        rows.append(
            {
                "label": label,
                "range": f"{int(lo)}-{int(min(hi, 100))}",
                "small_sample": bool(st.get("n", 0) < 20),
                **st,
            }
        )

    ma200 = pd.to_numeric(f["ma200"], errors="coerce").to_numpy(dtype=float)
    base_valid = valid & np.isfinite(ma200)
    base_rows = []
    for label, mask in (
        ("价>MA200", base_valid & (close > ma200)),
        ("价<MA200", base_valid & (close <= ma200)),
    ):
        if mask.sum():
            base_rows.append({"label": label, **_stats(fwd[mask])})

    overall = _stats(fwd[valid])
    return {
        "ok": True,
        "horizon": horizon,
        "buckets": rows,
        "baseline": base_rows,
        "overall": overall,
    }


# ─────────────────────────────────────────────────────────────
# 关键位 / 情景
# ─────────────────────────────────────────────────────────────


def key_levels(f: pd.DataFrame, code: str = "") -> dict:
    """支撑/阻力、P&F 目标、日线阶段。"""
    close = _f(f["close"].iloc[-1], 0.0)
    high = pd.to_numeric(f["high"], errors="coerce")
    low = pd.to_numeric(f["low"], errors="coerce")
    out: dict[str, Any] = {
        "close": _round(close),
        "ma20": _round(f["ma20"].iloc[-1]),
        "ma50": _round(f["ma50"].iloc[-1]),
        "ma200": _round(f["ma200"].iloc[-1]),
        "atr_pct": _round(f["atr_pct"].iloc[-1]),
        "boll_up": _round(f["boll_up"].iloc[-1]) if "boll_up" in f else None,
        "boll_dn": _round(f["boll_dn"].iloc[-1]) if "boll_dn" in f else None,
        "recent_low_60": _round(low.tail(60).min()),
        "recent_low_120": _round(low.tail(120).min()),
        "recent_high_60": _round(high.tail(60).max()),
        "recent_high_120": _round(high.tail(120).max()),
        "pivot_lows": [],
        "pivot_highs": [],
        "support_levels": [],
        "resistance_levels": [],
        "tr_bottom": None,
        "tr_top": None,
        "near_up": None,
        "near_down": None,
        "up_targets": [],
        "down_targets": [],
        "up_prob": [],
        "down_prob": [],
        "phase": "待定",
        "phase_detail": "",
    }
    pivots = None
    try:
        pivots = find_pivots(f, order=6)
        lows = sorted(
            {
                round(float(p["price"]), 2)
                for p in pivots
                if p["type"] == "low" and p["idx"] < len(f) - 1
            }
        )
        highs = sorted(
            {
                round(float(p["price"]), 2)
                for p in pivots
                if p["type"] == "high" and p["idx"] < len(f) - 1
            }
        )
        out["pivot_lows"] = lows[-3:]
        out["pivot_highs"] = highs[-3:]
    except Exception as e:  # noqa: BLE001
        log_exc(f"枢轴点失败 {code}", e)

    # P&F 只用最近 700 根, 保持“当前交易区间”口径稳定 (1200 根会混入更早的区间)
    try:
        pnf_df = f.tail(700).reset_index(drop=True)
        cols, box = build_pnf(pnf_df, box_mode="pct")
        vols = pnf_volume(pnf_df, cols, box)
        t = pnf_targets(pnf_df, cols, box, volumes=vols) or {}
        out["tr_bottom"] = _round(t.get("tr_bottom"))
        out["tr_top"] = _round(t.get("tr_top"))
        out["near_up"] = _round(t.get("近端上方目标"))
        out["near_down"] = _round(t.get("近端下方目标"))
        out["up_targets"] = [
            _round(t.get("横向计数上方目标_保守")),
            _round(t.get("横向计数上方目标_中")),
            _round(t.get("横向计数上方目标")),
        ]
        out["down_targets"] = [
            _round(t.get("横向计数下方目标_保守")),
            _round(t.get("横向计数下方目标_中")),
            _round(t.get("横向计数下方目标")),
        ]
        out["up_prob"] = [
            _f(t.get("上方概率_保守")),
            _f(t.get("上方概率_中")),
            _f(t.get("上方概率_激进")),
        ]
        out["down_prob"] = [
            _f(t.get("下方概率_保守")),
            _f(t.get("下方概率_中")),
            _f(t.get("下方概率_激进")),
        ]
        out["tr_position"] = _round(t.get("tr_position%"), 1)
    except Exception as e:  # noqa: BLE001
        log_exc(f"P&F 目标失败 {code}", e)

    try:
        if pivots is None:
            pivots = find_pivots(f, order=6)
        events = detect_all(f, pivots)
        phase, detail = judge_phase(f, pivots, events)
        out["phase"] = phase or "待定"
        out["phase_detail"] = detail or ""
    except Exception as e:  # noqa: BLE001
        log_exc(f"阶段判断失败 {code}", e)

    # 统一生成“现价下方支撑 / 上方阻力”列表, 避免把历史高点当支撑。
    supports: list[float] = []
    resistances: list[float] = []

    def _add_s(v):
        v = _f(v)
        if v is not None and close * 0.65 < v < close * 0.999:
            supports.append(round(v, 2))

    def _add_r(v):
        v = _f(v)
        if v is not None and close * 1.001 < v < close * 1.45:
            resistances.append(round(v, 2))

    for key in ("tr_bottom", "recent_low_60", "recent_low_120", "boll_dn", "ma20", "ma50", "ma200"):
        _add_s(out.get(key))
    for v in out["pivot_lows"]:
        _add_s(v)
    for key in ("tr_top", "recent_high_60", "recent_high_120", "boll_up", "ma20", "ma50", "ma200"):
        _add_r(out.get(key))
    for v in out["pivot_highs"]:
        _add_r(v)
    for v in out["up_targets"]:
        _add_r(v)
    out["support_levels"] = sorted(set(supports), reverse=True)[:6]
    out["resistance_levels"] = sorted(set(resistances))[:8]
    return out


def _dedupe_levels(values, limit: int = 3) -> list[float]:
    """去重并保留前 limit 个 (容差 0.1%)。"""
    out: list[float] = []
    for v in values:
        v = _f(v)
        if v is None or v <= 0:
            continue
        if any(abs(v - old) / max(old, 1.0) < 0.001 for old in out):
            continue
        out.append(round(v, 2))
        if len(out) >= limit:
            break
    return out


def build_scenarios(levels: dict) -> list[dict]:
    """基于 P&F/均线/枢轴生成三种条件情景 (不包含主观概率)。"""
    close = _f(levels.get("close"), 0.0)
    if close <= 0:
        return []

    supports = [v for v in (levels.get("support_levels") or []) if _f(v) and _f(v) < close]
    resistances = [v for v in (levels.get("resistance_levels") or []) if _f(v) and _f(v) > close]
    supports = sorted(set(supports), reverse=True)
    resistances = sorted(set(resistances))

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None

    # 若 P&F 区间已被现价穿越, 改用现价下方/上方最近的一档, 避免“触发条件已成立”。
    bear_line = _f(levels.get("tr_bottom")) or nearest_support
    if bear_line is not None and bear_line >= close:
        bear_line = nearest_support
    bull_line = _f(levels.get("tr_top")) or (resistances[-1] if resistances else None)
    if bull_line is not None and bull_line <= close:
        bull_line = nearest_resistance

    down_targets = _dedupe_levels(
        [
            v
            for v in [levels.get("near_down")] + (levels.get("down_targets") or [])
            if _f(v) and _f(v) < (bear_line or close)
        ]
    )
    up_targets = _dedupe_levels(
        [
            v
            for v in [levels.get("near_up")] + (levels.get("up_targets") or [])
            if _f(v) and _f(v) > (bull_line or close)
        ]
    )

    out = []
    if nearest_support and nearest_resistance:
        out.append(
            {
                "name": "基准: 区间震荡",
                "trigger": f"守住 {nearest_support:.0f}, 未站上 {nearest_resistance:.0f}",
                "path": f"{nearest_support:.0f} - {nearest_resistance:.0f} 区间反复",
                "note": "反弹到压力位若缩量滞涨, 仍是减仓/调仓机会。",
            }
        )
    if bear_line:
        path = " → ".join(f"{v:.0f}" for v in down_targets) if down_targets else "下探前低"
        out.append(
            {
                "name": "风险: 破位二次探底",
                "trigger": f"放量收盘跌破 {bear_line:.0f}",
                "path": path,
                "note": "周/月线偏空时, 破位后不要急着抄底, 等缩量止跌。",
            }
        )
    if bull_line:
        path = " → ".join(f"{v:.0f}" for v in up_targets) if up_targets else "打开上行空间"
        out.append(
            {
                "name": "反转: 右侧确认",
                "trigger": f"放量站上 {bull_line:.0f}, 且成交额/广度同步改善",
                "path": path,
                "note": "站上后仍需回踩确认; 只做确认后的右侧, 不猜左侧。",
            }
        )
    return out


# ─────────────────────────────────────────────────────────────
# 市场环境 / 行业轮动 / 风险预算
# ─────────────────────────────────────────────────────────────


def build_market_context() -> dict:
    """广度/量能/资金/情绪/共振, 全部 fail-soft。"""
    env = _safe("大盘环境", fetch_market_env, {}) or {}
    breadth = _safe("市场广度", fetch_market_breadth, {}) or {}
    amount = _safe("成交额", fetch_market_amount, {}) or {}
    fund = _safe("主力资金", fetch_market_fund_flow, {}) or {}
    emotion = _safe("市场情绪", fetch_emotion_board, {}) or {}
    resonance = _safe("指数共振", build_market_resonance, {}) or {}

    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    flat = int(breadth.get("flat") or 0)
    total = up + down + flat
    ladder = emotion.get("ladder") or {}
    zt_cnt = sum(int(v or 0) for v in ladder.values()) or len(emotion.get("zt") or [])

    return {
        "env": env.get("env"),
        "tone": env.get("tone"),
        "breadth": {
            "up": up,
            "down": down,
            "flat": flat,
            "total": total,
            "up_ratio": round(up / total, 3) if total else None,
            "limit_up": int(breadth.get("limit_up") or 0),
            "limit_down": int(breadth.get("limit_down") or 0),
        },
        "amount": {
            "total_yi": _round(amount.get("total_yi"), 0),
            "vol_pct": _round(amount.get("vol_pct"), 0),
            "date": amount.get("date"),
        },
        "fund": {
            "total_yi": _round(fund.get("total_yi"), 2),
            "items": fund.get("items") or [],
        },
        "emotion": {
            "phase": emotion.get("phase"),
            "zt_cnt": zt_cnt,
            "dt_cnt": int(emotion.get("dt_cnt") or 0),
            "zb_cnt": int(emotion.get("zb_cnt") or 0),
            "max_board": int(emotion.get("max_board") or 0),
            "premium": _round(emotion.get("premium"), 2),
        },
        "resonance": {
            "today_pct": _round(resonance.get("today_pct"), 0),
            "tone": resonance.get("tone"),
        },
        "quality": {
            "env": bool(env.get("env")),
            "breadth": bool(breadth.get("total") or (up + down)),
            "amount": _f(amount.get("total_yi")) is not None,
            "fund": _f(fund.get("total_yi")) is not None,
            "emotion": bool(emotion.get("phase") or emotion.get("zt")),
            "resonance": resonance.get("today_pct") is not None,
        },
    }


def _fetch_sina_sector_ranking() -> list[dict]:
    """同花顺/东财都挂掉时的行业兜底: 新浪行业 (只有当日涨跌, 无20日资金)。"""
    import akshare as ak

    df = ak.stock_sector_spot(indicator="新浪行业")
    if df is None or len(df) == 0:
        return []
    rows = []
    for _, r in df.iterrows():
        name = str(r.iloc[1] or "").strip()
        pct = _f(r.iloc[5])
        amount = _f(r.iloc[7])
        if not name or pct is None:
            continue
        if pct > 1:
            tone = "bullish"
        elif pct < -1:
            tone = "bearish"
        else:
            tone = "mixed"
        rows.append(
            {
                "name": name,
                "label": str(r.iloc[0] or "").strip(),
                "pct": pct,
                "flow20_yi": 0.0,
                "amount_yi": round((amount or 0.0) / 1e8, 2),
                "tone": tone,
                "source": "sina",
            }
        )
    return rows


def build_sector_rotation() -> dict | None:
    """行业强弱排序 + 防御/成长风格差。"""
    rows = _safe("行业排名", fetch_sector_ranking, []) or []
    source = "live"
    if not rows:
        rows = _safe("新浪行业", _fetch_sina_sector_ranking, []) or []
        source = "sina"
    rows = [r for r in rows if r.get("name")]
    if not rows:
        return None

    flows = np.array([_f(r.get("flow20_yi"), 0.0) for r in rows], dtype=float)
    pcts = np.array([_f(r.get("pct"), 0.0) for r in rows], dtype=float)
    flow_rank = pd.Series(flows).rank(pct=True).to_numpy() * 100
    pct_rank = pd.Series(pcts).rank(pct=True).to_numpy() * 100
    strength = 0.7 * flow_rank + 0.3 * pct_rank

    enriched = []
    for i, r in enumerate(rows):
        enriched.append(
            {
                "name": r.get("name"),
                "label": r.get("label"),
                "pct": _round(r.get("pct")),
                "flow20_yi": _round(r.get("flow20_yi")),
                "amount_yi": _round(r.get("amount_yi")),
                "tone": r.get("tone"),
                "strength": round(float(strength[i]), 1),
            }
        )
    enriched.sort(key=lambda x: -x["strength"])

    def _avg(names, key):
        vals = [r[key] for r in enriched if r["name"] in names and r.get(key) is not None]
        return round(float(np.mean(vals)), 2) if vals else None

    def_flow = _avg(DEFENSIVE_SECTORS, "flow20_yi")
    growth_flow = _avg(GROWTH_SECTORS, "flow20_yi")
    style_gap = (
        round(def_flow - growth_flow, 2)
        if def_flow is not None and growth_flow is not None
        else None
    )

    return {
        "n": len(enriched),
        "top": enriched[:8],
        "bottom": enriched[-8:],
        "all": enriched,
        "source": source,
        "has_flow": source != "sina",
        "defensive_flow20": def_flow,
        "growth_flow20": growth_flow,
        "style_gap": style_gap,
    }


_SINA_LABEL_CACHE: dict[str, str] = {}
_SINA_SECTOR_MAPS: dict[str, dict[str, str]] = {}
# THS/东财行业名 → (新浪指标, 新浪行业名)。只用于东财成分股接口限流时的兜底。
_SINA_SECTOR_ALIASES: dict[str, tuple[str, str]] = {
    "银行": ("行业", "货币金融服务"),
    "证券": ("行业", "资本市场服务"),
    "保险": ("行业", "保险业"),
    "多元金融": ("行业", "其他金融业"),
    "半导体": ("新浪行业", "电子器件"),
    "消费电子": ("新浪行业", "电子器件"),
    "光学光电子": ("新浪行业", "电子器件"),
    "元件": ("新浪行业", "电子器件"),
    "其他电子": ("新浪行业", "电子器件"),
    "电子化学品": ("新浪行业", "电子器件"),
    "军工电子": ("新浪行业", "电子器件"),
    "软件开发": ("新浪行业", "电子信息"),
    "IT服务": ("新浪行业", "电子信息"),
    "计算机设备": ("新浪行业", "电子信息"),
    "通信设备": ("新浪行业", "电子信息"),
    "通信服务": ("新浪行业", "电子信息"),
    "电池": ("新浪行业", "发电设备"),
    "光伏设备": ("新浪行业", "发电设备"),
    "风电设备": ("新浪行业", "发电设备"),
    "电网设备": ("新浪行业", "发电设备"),
    "其他电源设备": ("新浪行业", "发电设备"),
    "电机": ("新浪行业", "发电设备"),
    "汽车整车": ("新浪行业", "汽车制造"),
    "汽车零部件": ("新浪行业", "汽车制造"),
    "汽车服务及其他": ("新浪行业", "汽车制造"),
    "专用设备": ("新浪行业", "机械行业"),
    "通用设备": ("新浪行业", "机械行业"),
    "自动化设备": ("新浪行业", "机械行业"),
    "工程机械": ("新浪行业", "机械行业"),
    "轨交设备": ("新浪行业", "机械行业"),
    "化学制品": ("新浪行业", "化工行业"),
    "化学原料": ("新浪行业", "化工行业"),
    "化学纤维": ("新浪行业", "化纤行业"),
    "塑料制品": ("新浪行业", "塑料制品"),
    "橡胶制品": ("新浪行业", "化工行业"),
    "非金属材料": ("新浪行业", "建筑建材"),
    "农化制品": ("新浪行业", "农药化肥"),
    "钢铁": ("新浪行业", "钢铁行业"),
    "工业金属": ("新浪行业", "有色金属"),
    "小金属": ("新浪行业", "有色金属"),
    "贵金属": ("新浪行业", "有色金属"),
    "能源金属": ("新浪行业", "有色金属"),
    "金属新材料": ("新浪行业", "有色金属"),
    "煤炭开采加工": ("新浪行业", "煤炭行业"),
    "油气开采及服务": ("新浪行业", "石油行业"),
    "石油加工贸易": ("新浪行业", "石油行业"),
    "电力": ("新浪行业", "电力行业"),
    "燃气": ("新浪行业", "供水供气"),
    "环保设备": ("新浪行业", "环保行业"),
    "环境治理": ("新浪行业", "环保行业"),
    "建筑装饰": ("新浪行业", "建筑建材"),
    "建筑材料": ("新浪行业", "建筑建材"),
    "房地产开发": ("新浪行业", "房地产"),
    "房地产服务": ("新浪行业", "房地产"),
    "港口航运": ("新浪行业", "交通运输"),
    "公路铁路运输": ("新浪行业", "交通运输"),
    "机场航运": ("新浪行业", "交通运输"),
    "物流": ("新浪行业", "交通运输"),
    "零售": ("新浪行业", "商业百货"),
    "贸易": ("新浪行业", "物资外贸"),
    "互联网电商": ("新浪行业", "商业百货"),
    "旅游及酒店": ("新浪行业", "酒店旅游"),
    "教育": ("新浪行业", "综合行业"),
    "其他社会服务": ("新浪行业", "综合行业"),
    "养殖业": ("新浪行业", "农林牧渔"),
    "种植业与林业": ("新浪行业", "农林牧渔"),
    "农产品加工": ("新浪行业", "农林牧渔"),
    "服装家纺": ("新浪行业", "服装鞋类"),
    "纺织制造": ("新浪行业", "纺织行业"),
    "家居用品": ("新浪行业", "家具行业"),
    "造纸": ("新浪行业", "造纸行业"),
    "包装印刷": ("新浪行业", "印刷包装"),
    "白色家电": ("新浪行业", "家电行业"),
    "黑色家电": ("新浪行业", "家电行业"),
    "小家电": ("新浪行业", "家电行业"),
    "厨卫电器": ("新浪行业", "家电行业"),
    "文化传媒": ("新浪行业", "传媒娱乐"),
    "影视院线": ("新浪行业", "传媒娱乐"),
    "游戏": ("新浪行业", "传媒娱乐"),
    "军工装备": ("新浪行业", "飞机制造"),
    "食品加工制造": ("新浪行业", "食品行业"),
    "饮料制造": ("新浪行业", "酿酒行业"),
    "白酒": ("新浪行业", "酿酒行业"),
    "化学制药": ("新浪行业", "生物制药"),
    "中药": ("新浪行业", "生物制药"),
    "生物制品": ("新浪行业", "生物制药"),
    "医药商业": ("新浪行业", "生物制药"),
    "医疗服务": ("新浪行业", "生物制药"),
    "医疗器械": ("新浪行业", "医疗器械"),
    "综合": ("新浪行业", "综合行业"),
}

_EM_CONS_COOLDOWN_UNTIL = 0.0


def _sina_sector_map(indicator: str) -> dict[str, str]:
    """新浪行业名 → label；按指标分别缓存。"""
    if indicator in _SINA_SECTOR_MAPS:
        return _SINA_SECTOR_MAPS[indicator]
    try:
        import akshare as ak

        df = ak.stock_sector_spot(indicator=indicator)
        mapping = {}
        if df is not None and len(df) > 0:
            for _, r in df.iterrows():
                n = str(r.iloc[1] or "").strip()
                label = str(r.iloc[0] or "").strip()
                if n and label:
                    mapping[n] = label
        _SINA_SECTOR_MAPS[indicator] = mapping
        return mapping
    except Exception as e:  # noqa: BLE001
        log_exc(f"新浪行业代码映射失败: {indicator}", e)
        _SINA_SECTOR_MAPS[indicator] = {}
        return {}


def _sina_label_for_name(name: str) -> str | None:
    """行业名 → 新浪板块 label（新浪行业/行业 + 别名）。"""
    if not name:
        return None
    if name in _SINA_LABEL_CACHE:
        return _SINA_LABEL_CACHE[name]
    for indicator in ("新浪行业", "行业"):
        label = _sina_sector_map(indicator).get(name)
        if label:
            _SINA_LABEL_CACHE[name] = label
            return label
    alias = _SINA_SECTOR_ALIASES.get(name)
    if alias:
        indicator, target = alias
        label = _sina_sector_map(indicator).get(target)
        if label:
            _SINA_LABEL_CACHE[name] = label
            return label
    return None


def _symbol_for_code(code: str) -> str:
    code = str(code or "").strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    return f"bj{code}"


def _fetch_em_sector_constituents(name: str, limit: int = 100) -> list[dict]:
    """东财行业成分股；限流后冷却 10 分钟，避免连续触发。"""
    global _EM_CONS_COOLDOWN_UNTIL
    if time.time() < _EM_CONS_COOLDOWN_UNTIL:
        return []
    try:
        import akshare as ak

        df = ak.stock_board_industry_cons_em(symbol=name)
        if df is None or len(df) == 0:
            return []
        rows = []
        for _, r in df.head(limit).iterrows():
            code = str(r.iloc[1] or "").strip().zfill(6)
            sname = str(r.iloc[2] or "").strip()
            if not code or not sname:
                continue
            amount = _f(r.iloc[7])
            rows.append(
                {
                    "symbol": _symbol_for_code(code),
                    "code": code,
                    "name": sname,
                    "price": _round(r.iloc[3]),
                    "pct": _round(r.iloc[4]),
                    "amount_yi": _round(amount / 1e8) if amount is not None else None,
                    "turnover": _round(r.iloc[13]),
                    "pe": _round(r.iloc[14]),
                    "pb": _round(r.iloc[15]),
                    "source": "eastmoney",
                }
            )
        rows.sort(key=lambda x: (-(_f(x.get("pct"), 0.0) or 0.0), x["code"]))
        return rows
    except Exception as e:  # noqa: BLE001
        _EM_CONS_COOLDOWN_UNTIL = time.time() + 600
        log_exc(f"东财行业成分股失败，10分钟内改用新浪: {name}", e)
        return []


def fetch_sector_constituents(name: str, label: str | None = None, limit: int = 100) -> list[dict]:
    """行业成分股。东财优先；限流时用新浪板块详情兜底。
    返回 [{symbol, code, name, price, pct, amount_yi, turnover, pe, pb, source}]。
    """
    if not label:
        rows = _fetch_em_sector_constituents(name, limit)
        if rows:
            return rows
        label = _sina_label_for_name(name)
    if not label:
        return []
    try:
        import akshare as ak

        df = ak.stock_sector_detail(sector=label)
        if df is None or len(df) == 0:
            return []
        rows = []
        for _, r in df.head(limit).iterrows():
            symbol = str(r.get("symbol") or "").strip().lower()
            code = str(r.get("code") or symbol[-6:] or "").strip()
            sname = str(r.get("name") or "").strip()
            if not code or not sname:
                continue
            amount = _f(r.get("amount"))
            rows.append(
                {
                    "symbol": symbol or _symbol_for_code(code),
                    "code": code,
                    "name": sname,
                    "price": _round(r.get("trade")),
                    "pct": _round(r.get("changepercent")),
                    "amount_yi": _round(amount / 1e8) if amount is not None else None,
                    "turnover": _round(r.get("turnoverratio")),
                    "pe": _round(r.get("per")),
                    "pb": _round(r.get("pb")),
                    "source": "sina",
                }
            )
        rows.sort(key=lambda x: (-(_f(x.get("pct"), 0.0) or 0.0), x["code"]))
        return rows
    except Exception as e:  # noqa: BLE001
        log_exc(f"新浪行业成分股失败: {name}", e)
        return []


def fetch_sector_constituents_cached(
    name: str, label: str | None = None, limit: int = 100
) -> list[dict]:
    key = f"cons:{name}:{label or ''}:{limit}"
    rows, _q = _DATA_CACHE.get(
        key,
        300,
        lambda: fetch_sector_constituents(name, label, limit),
        fail_cooldown=300,
    )
    rows = rows or []
    if rows:
        _record_constituents_by_code(name, rows)
    return rows


_CONS_BY_CODE: dict[str, set[str]] = {}
_INDUSTRY_RAW_CACHE: dict[str, tuple[float, dict]] = {}
_INDUSTRY_CACHE_TTL = 24 * 3600
_INDUSTRY_LOCK = threading.Lock()


def _record_constituents_by_code(name: str, rows: list[dict]) -> None:
    """记录“股票代码 → 本系统行业名”，供自动识别行业时优先命中。"""
    if not name:
        return
    with _INDUSTRY_LOCK:
        for r in rows or []:
            code = str(r.get("code") or "").strip().zfill(6)
            if code:
                _CONS_BY_CODE.setdefault(code, set()).add(str(name))


def _code6(code: str) -> str:
    code = str(code or "").strip().lower().replace(".sh", "").replace(".sz", "").replace(".bj", "")
    if len(code) == 8 and code[:2] in ("sh", "sz", "bj"):
        code = code[2:]
    return code[-6:].zfill(6)


def _fetch_cninfo_industry(code: str) -> list[str]:
    """巨潮/中证/申万等公开行业分类（非东财，通常可用作备源）。"""
    try:
        import akshare as ak

        end = pd.Timestamp.today().strftime("%Y%m%d")
        df = ak.stock_industry_change_cninfo(symbol=code, start_date="20000101", end_date=end)
        if df is None or len(df) == 0:
            return []
        df = df.copy()
        df["_date"] = pd.to_numeric(df.get("变更日期"), errors="coerce")
        std_priority = {
            "巨潮行业分类标准": 0,
            "申银万国行业分类标准": 1,
            "中证行业分类标准": 2,
            "中国上市公司协会上市公司行业分类标准": 3,
        }
        df["_prio"] = df.get("分类标准", pd.Series(dtype=str)).map(std_priority).fillna(9)
        # 每个分类标准只取最近一条，再按“巨潮 → 申万 → 中证 → 协会”的口径顺序输出。
        latest = (
            df.sort_values(["_prio", "_date"], ascending=[True, False])
            .drop_duplicates(subset="分类标准", keep="first")
            .sort_values("_prio", ascending=True)
        )
        candidates: list[str] = []
        for _, r in latest.iterrows():
            for field in ("行业中类", "行业大类", "行业次类", "行业门类"):
                v = str(r.get(field) or "").strip()
                if v and v.lower() not in {"null", "nan", "none"} and v not in candidates:
                    candidates.append(v)
        return candidates
    except Exception as e:  # noqa: BLE001
        log_exc(f"行业识别失败（巨潮/中证）: {code}", e)
        return []


def _fetch_em_industry(code: str) -> list[str]:
    """东财个股行业；受 push2 限流冷却约束。"""
    global _EM_CONS_COOLDOWN_UNTIL
    if time.time() < _EM_CONS_COOLDOWN_UNTIL:
        return []
    try:
        import akshare as ak

        df = ak.stock_individual_info_em(symbol=code)
        if df is None or len(df) == 0:
            return []
        for _, r in df.iterrows():
            item = str(r.iloc[0] or "")
            val = str(r.iloc[1] or "").strip()
            if "行业" in item and val and val.lower() not in {"null", "nan"}:
                return [val]
    except Exception as e:  # noqa: BLE001
        _EM_CONS_COOLDOWN_UNTIL = time.time() + 600
        log_exc(f"东财行业识别失败，10分钟内跳过: {code}", e)
    return []


def _fetch_industry_raw(code: str) -> dict:
    local = sorted(_CONS_BY_CODE.get(code, set()))
    if local:
        return {"candidates": local, "source": "local"}
    candidates = _fetch_cninfo_industry(code)
    if candidates:
        return {"candidates": candidates, "source": "cninfo"}
    candidates = _fetch_em_industry(code)
    if candidates:
        return {"candidates": candidates, "source": "eastmoney"}
    return {"candidates": [], "source": ""}


def _choose_industry(candidates: list[str], sector_names: list[str] | None) -> str | None:
    cands = [str(c).strip() for c in (candidates or []) if str(c).strip()]
    if not cands:
        return None
    if sector_names:
        sector_set = {str(s).strip() for s in sector_names if str(s).strip()}
        for c in cands:
            if c in sector_set:
                return c
        reverse: dict[str, str] = {}
        for sector, (_ind, target) in _SINA_SECTOR_ALIASES.items():
            reverse.setdefault(str(target).strip(), sector)
            reverse.setdefault(str(sector).strip(), sector)
        for c in cands:
            mapped = reverse.get(c)
            if mapped and mapped in sector_set:
                return mapped
    return cands[0]


def detect_stock_industry(code: str, sector_names: list[str] | None = None) -> dict:
    """自动识别个股所属行业。

    优先级：本地已加载的成分股 → 巨潮/中证行业 → 东财行业。
    返回 `{code, industry, candidates, source}`；识别失败时 `industry` 为 None。
    """
    code6 = _code6(code)
    if not code6.isdigit():
        return {}
    now = time.time()
    with _INDUSTRY_LOCK:
        hit = _INDUSTRY_RAW_CACHE.get(code6)
        if hit and now - hit[0] < _INDUSTRY_CACHE_TTL:
            raw = hit[1]
        else:
            raw = _fetch_industry_raw(code6)
            if raw.get("candidates"):
                _INDUSTRY_RAW_CACHE[code6] = (now, raw)
    industry = _choose_industry(raw.get("candidates") or [], sector_names)
    return {
        "code": code6,
        "industry": industry,
        "candidates": raw.get("candidates") or [],
        "source": raw.get("source") or "",
    }


def _nt_flow() -> dict:
    """国家队三通道聚合 (延迟导入, 便于测试替换)。"""
    from .national_team import national_team_flow

    return national_team_flow(max_stocks=3)


def build_etf_confirmation() -> dict:
    """国家队/宽基 ETF 确认层。

    把 ETF 日频主力流、三因子份额、季报持仓压成一个“托底/平衡/撤退”标签。
    注意: 这是资金流代理, 不是官方持仓; 份额变化可能受拆分/分红影响。
    """
    res = _safe("国家队ETF确认", _nt_flow, None)
    if not res:
        return {
            "ok": False,
            "label": "数据不足",
            "verdict": "数据不足",
            "score": None,
            "vs_market": "数据不足，无法确认",
            "warnings": [],
            "reason": "national_team_flow 返回空",
        }

    etf = res.get("etf") or {}
    factor = res.get("factor") or {}
    holdings = res.get("holdings") or {}
    score = _f(res.get("score"))
    verdict = res.get("verdict") or "数据不足"
    etf_total = int(etf.get("total") or 0)
    net1 = _round(etf.get("net_y1"))
    net20 = _round(etf.get("net_y20"))
    # 20 日资金最能代表“托底/撤退”; 缺失或为 0 时不轻易下结论
    flow_ok = etf_total > 0 and abs(_f(net20, 0.0) or 0.0) > 1e-9

    # 东财资金流断连时的备选: 量价代理 (实体额×成交量), 不是真实申赎
    etf_rows = res.get("etf_rows") or []
    proxy_rows = [
        r for r in etf_rows if r.get("source") == "proxy" and r.get("proxy_y20") is not None
    ]
    proxy_ok = bool(proxy_rows)
    proxy_y1 = sum(_f(r.get("proxy_y1"), 0.0) or 0.0 for r in proxy_rows)
    proxy_y20 = sum(_f(r.get("proxy_y20"), 0.0) or 0.0 for r in proxy_rows)
    flow_source = "主力资金" if flow_ok else ""

    if score is None:
        label = "数据不足"
    elif not flow_ok and proxy_ok:
        # 不把量价代理说成“托底/流入”, 单独标注
        if proxy_y20 > 5:
            label = "量价代理偏多"
        elif proxy_y20 < -5:
            label = "量价代理偏空"
        else:
            label = "量价代理中性"
        verdict = "量价代理"
        net1 = round(proxy_y1, 2)
        net20 = round(proxy_y20, 2)
        flow_source = "量价代理"
    elif not flow_ok:
        # ETF 日频资金断连时, 季报/三因子不足以判断“托底/撤退”
        label = "ETF资金缺失"
        verdict = "资金数据缺失"
    elif score >= 0.2:
        label = "托底/流入"
    elif score <= -0.2:
        label = "撤退/流出"
    else:
        label = "双向平衡"

    warnings = []
    if not flow_ok and proxy_ok:
        warnings.append("东财ETF资金流断连，使用量价代理（非真实申赎，仅参考）")
    elif not flow_ok:
        warnings.append("ETF 20 日资金缺失/为 0，托底/撤退判断不可用")
        net20 = None
    rows = res.get("factor_rows") or []
    anomalies = [r for r in rows if abs(_f(r.get("share_5d"), 0.0) or 0.0) > 20]
    if anomalies:
        warnings.append(
            f"{len(anomalies)} 只 ETF 的 5 日份额变化超过 20%，"
            f"可能受拆分/分红/数据口径影响，不宜当作真实申购"
        )

    return {
        "ok": True,
        "label": label,
        "verdict": verdict,
        "score": _round(score, 3),
        "flow_ok": flow_ok,
        "proxy_ok": proxy_ok,
        "flow_source": flow_source,
        "net_y1": net1,
        "net_y20": net20,
        "factor_buy": int(factor.get("buy") or 0),
        "factor_sell": int(factor.get("sell") or 0),
        "factor_valid": int(factor.get("valid") or 0),
        "avg_diff": _round(factor.get("avg_diff"), 2),
        "holdings_add": int(holdings.get("add") or 0),
        "holdings_cut": int(holdings.get("cut") or 0),
        "holdings_sample": int(holdings.get("sample") or 0),
        "reasoning": res.get("reasoning") or [],
        "warnings": warnings,
        "vs_market": "",
        "reason": "",
    }


def _etf_vs_market(market_score: float | None, etf_label: str | None) -> str:
    """把 ETF 确认层与大盘状态拼成一句人话。"""
    score = _f(market_score, 50.0)
    label = etf_label or "数据不足"
    weak = score < 30
    strong = score >= 50
    if weak and label == "托底/流入":
        return "弱市有资金托底，但尚未确认（观察能否托住）"
    if weak and label == "撤退/流出":
        return "弱市且大资金撤退（防守优先）"
    if weak and label == "双向平衡":
        return "弱市、大资金未明显托底（防守/观望）"
    if strong and label == "托底/流入":
        return "价格与资金同向（偏积极）"
    if strong and label == "撤退/流出":
        return "价格强但大资金不跟（谨慎）"
    if strong and label == "双向平衡":
        return "价格强、资金未确认（谨慎偏多）"
    if label == "量价代理偏多":
        return (
            "弱市量价代理偏多（非真实申赎，仅参考）"
            if weak
            else "价格与量价代理同向（参考）"
            if strong
            else "量价代理偏多（非真实申赎）"
        )
    if label == "量价代理偏空":
        return (
            "弱市量价代理偏空（防守）"
            if weak
            else "价格强但量价代理偏空（谨慎）"
            if strong
            else "量价代理偏空（非真实申赎）"
        )
    if label == "量价代理中性":
        return "量价代理中性（非真实申赎）"
    if label in ("数据不足", "ETF资金缺失"):
        return "ETF资金数据缺失，无法确认"
    return "信号中性/矛盾（观望）"


def risk_budget(
    score: float | None,
    atr_pct: float | None,
    up_ratio: float | None,
    fund_total: float | None,
    score_pct: float | None = None,
) -> dict:
    """参考仓位区间 (0-80%), 只做风险预算, 不构成投资建议。

    有历史分位时按分位分档（与 v3 状态标签一致）；否则退回绝对分数分档。
    """
    pct = _f(score_pct)
    if pct is not None:
        if pct > 75:
            low, high = 60, 80
        elif pct > 50:
            low, high = 40, 60
        elif pct > 25:
            low, high = 20, 40
        elif pct > 10:
            low, high = 10, 25
        else:
            low, high = 0, 15
    else:
        v = _f(score, 50.0)
        if v >= 70:
            low, high = 60, 80
        elif v >= 50:
            low, high = 40, 60
        elif v >= 30:
            low, high = 20, 40
        elif v >= 20:
            low, high = 10, 25
        else:
            low, high = 0, 15

    atr = _f(atr_pct)
    if atr is not None:
        if atr > 3.5:
            low -= 10
            high -= 10
        elif atr < 1.5:
            high += 5

    ur = _f(up_ratio)
    if ur is not None:
        if ur < 0.20:
            low -= 10
            high -= 10
        elif ur > 0.60:
            low += 10
            high += 10

    ft = _f(fund_total)
    if ft is not None:
        if ft < -300:
            low -= 10
            high -= 10
        elif ft > 300:
            low += 10
            high += 10

    low = max(0, min(80, low))
    high = max(10, min(80, high))  # 最低保留 10% 观察仓, 避免报告出现 0%-0%
    if low > high:
        low, high = high, low
    return {
        "low": int(low),
        "high": int(high),
        "note": "按历史分位/波动/广度/资金做机械降档, 未考虑个人风险承受能力。",
    }


def build_alerts(result: dict) -> list[dict]:
    """机械预警：破位、状态切换、ETF 资金转向、广度极端。

    只根据当前结果和上一次记录做规则判断，不做单日预测。
    """
    alerts: list[dict] = []
    primary = result.get("primary")
    p = (result.get("indices") or {}).get(primary) or {}
    name = p.get("name") or primary or "主指数"
    close = _f(p.get("close"))
    lv = result.get("primary_levels") or {}
    ch = result.get("changes") or {}
    etf = result.get("etf") or {}
    market = result.get("market") or {}
    breadth = market.get("breadth") or {}

    # 1) 破位
    tr_bottom = _f(lv.get("tr_bottom"))
    ma200 = _f(lv.get("ma200"))
    if close and tr_bottom and close < tr_bottom:
        alerts.append(
            {
                "level": "warn",
                "title": "P&F 破位",
                "detail": f"{name} 收盘 {close:.2f} 低于 P&F 下沿 {tr_bottom:.2f}。",
            }
        )
    elif close and ma200 and close < ma200:
        alerts.append(
            {
                "level": "info",
                "title": "跌破 MA200",
                "detail": f"{name} 收盘 {close:.2f} 低于 MA200 {ma200:.2f}。",
            }
        )

    # 2) 状态切换
    if ch.get("state_changed"):
        delta = _f(ch.get("score_delta"), 0.0)
        direction = "转弱" if delta < 0 else "转强"
        level = "warn" if (delta < 0 or ch.get("state_to") in ("谨慎", "风险规避")) else "info"
        alerts.append(
            {
                "level": level,
                "title": "状态切换",
                "detail": f"{name} {ch.get('state_from')} → {ch.get('state_to')}（{direction}）。",
            }
        )
    elif ch.get("phase_changed"):
        alerts.append(
            {
                "level": "info",
                "title": "阶段切换",
                "detail": f"{name} {ch.get('phase_from')} → {ch.get('phase_to')}。",
            }
        )

    # 3) ETF 资金转向
    inflow = {"托底/流入", "量价代理偏多"}
    outflow = {"撤退/流出", "量价代理偏空"}
    etf_from = str(ch.get("etf_from") or "")
    etf_to = str(etf.get("label") or ch.get("etf_to") or "")
    if etf_from in outflow and etf_to in inflow:
        alerts.append(
            {
                "level": "info",
                "title": "ETF 由流出转流入",
                "detail": f"上次 {etf_from} → 本次 {etf_to}。",
            }
        )
    elif etf_from in inflow and etf_to in outflow:
        alerts.append(
            {
                "level": "warn",
                "title": "ETF 由流入转流出",
                "detail": f"上次 {etf_from} → 本次 {etf_to}。",
            }
        )
    net1 = _f(etf.get("net_y1"))
    net20 = _f(etf.get("net_y20"))
    if net1 is not None and net20 is not None:
        if net1 > 0 > net20:
            alerts.append(
                {
                    "level": "info",
                    "title": "ETF 短线转流入",
                    "detail": f"1 日 {net1:.2f} 亿，20 日仍为 {net20:.2f} 亿。",
                }
            )
        elif net1 < 0 < net20:
            alerts.append(
                {
                    "level": "info",
                    "title": "ETF 短线转流出",
                    "detail": f"1 日 {net1:.2f} 亿，20 日仍为 {net20:.2f} 亿。",
                }
            )

    # 4) 广度极端
    up_ratio = _f(breadth.get("up_ratio"))
    limit_up = int(breadth.get("limit_up") or 0)
    limit_down = int(breadth.get("limit_down") or 0)
    if up_ratio is not None:
        if up_ratio <= 0.15:
            alerts.append(
                {
                    "level": "warn",
                    "title": "广度极端弱",
                    "detail": f"上涨占比 {up_ratio * 100:.0f}%（≤15%）。",
                }
            )
        elif up_ratio >= 0.85:
            alerts.append(
                {
                    "level": "warn",
                    "title": "广度极端强",
                    "detail": f"上涨占比 {up_ratio * 100:.0f}%（≥85%，注意过热/一致预期）。",
                }
            )
    if limit_down >= 80:
        alerts.append(
            {
                "level": "warn",
                "title": "跌停家数偏多",
                "detail": f"跌停 {limit_down} 家（≥80）。",
            }
        )
    elif limit_up >= 150:
        alerts.append(
            {
                "level": "info",
                "title": "涨停家数偏多",
                "detail": f"涨停 {limit_up} 家（≥150，情绪可能过热）。",
            }
        )

    order = {"warn": 0, "info": 1}
    alerts.sort(key=lambda a: order.get(a.get("level"), 2))
    return alerts


# ─────────────────────────────────────────────────────────────
# 聚合入口
# ─────────────────────────────────────────────────────────────


def build_outlook(
    primary: str = "sh000001",
    datalen: int = 1200,
    include_sectors: bool = True,
    include_etf: bool = True,
    include_liquidity: bool = True,
    include_valuation: bool = True,
    include_backtest: bool = True,
    include_changes: bool = True,
    journal_path: str | None = None,
) -> dict:
    """构建完整 A 股状态报告数据。"""
    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label_version": LABEL_VERSION,
        "primary": primary,
        "indices": {},
        "market": {},
        "sectors": None,
        "etf": None,
        "liquidity": None,
        "valuation": None,
        "analogs": {},
        "backtest": {},
        "backtest_strategy": {},
        "changes": None,
        "data_quality": {},
        "risk_budget": {},
        "scenarios": [],
        "disclaimer": DISCLAIMER,
    }
    frames: dict[str, pd.DataFrame] = {}
    levels_map: dict[str, dict] = {}

    for code, name in OUTLOOK_INDICES:
        df = prepare_index_frame(code, datalen=datalen)
        if df is None:
            continue
        f = compute_features(df)
        f["regime_score"] = regime_score_series(f)
        frames[code] = f
        snap = feature_snapshot(f, code, name)
        levels = key_levels(f, code)
        levels_map[code] = levels
        snap.update(
            {
                "phase": levels.get("phase"),
                "tr_bottom": levels.get("tr_bottom"),
                "tr_top": levels.get("tr_top"),
                "near_up": levels.get("near_up"),
                "near_down": levels.get("near_down"),
                "pivot_lows": levels.get("pivot_lows"),
                "pivot_highs": levels.get("pivot_highs"),
                "up_targets": levels.get("up_targets"),
                "down_targets": levels.get("down_targets"),
                "up_prob": levels.get("up_prob"),
                "down_prob": levels.get("down_prob"),
            }
        )
        result["indices"][code] = snap

    if primary not in frames:
        primary = next(iter(frames), "")
        result["primary"] = primary
    if not primary:
        result["error"] = "无法获取任何指数日线数据"
        return result

    f = frames[primary]
    snap = result["indices"][primary]
    result["as_of"] = snap.get("date")
    # K线数据 (近 250 根), 给独立 GUI 图表页用; 不写入预测日志
    tail = f.tail(250)
    result["price_history"] = {
        "dates": [str(d)[:10] for d in tail["day"]],
        "open": [_round(v) for v in tail["open"]],
        "high": [_round(v) for v in tail["high"]],
        "low": [_round(v) for v in tail["low"]],
        "close": [_round(v) for v in tail["close"]],
        "ma20": [_round(v) for v in tail["ma20"]],
        "ma50": [_round(v) for v in tail["ma50"]],
        "ma200": [_round(v) for v in tail["ma200"]],
    }
    result["data_quality"]["indices"] = {
        "ok": bool(frames),
        "stale": False,
        "as_of": result["as_of"],
        "missing": [code for code, _ in OUTLOOK_INDICES if code not in frames],
        "error": "",
    }
    result["score_history"] = {
        "dates": [str(d)[:10] for d in f["day"]],
        "score": [round(float(v), 1) if pd.notna(v) else None for v in f["regime_score"]],
        "close": [round(float(v), 2) for v in f["close"]],
    }

    if include_backtest:
        try:
            from .ashare_backtest import backtest_state

            result["backtest_strategy"] = backtest_state(f)
        except Exception as e:  # noqa: BLE001
            log_exc("状态分回测失败", e)

    analogs = historical_analogs(f)
    result["analogs"] = analogs
    result["state_transitions"] = state_transitions(f)
    result["backtest"] = regime_backtest(f)

    levels = levels_map.get(primary) or key_levels(f, primary)
    result["primary_levels"] = levels
    result["scenarios"] = build_scenarios(levels)

    market = build_market_context()
    result["market"] = market
    result["data_quality"]["market"] = market.get("quality") or {}
    load_extras(
        result,
        include_sectors=include_sectors,
        include_etf=include_etf,
        include_liquidity=include_liquidity,
        include_valuation=include_valuation,
    )
    if include_changes:
        try:
            from .ashare_journal import compare_with_previous

            result["changes"] = compare_with_previous(result, path=journal_path)
        except Exception as e:  # noqa: BLE001
            log_exc("对比上次记录失败", e)
    return result


def load_extras(
    result: dict,
    *,
    include_sectors: bool = True,
    include_etf: bool = True,
    include_liquidity: bool = True,
    include_valuation: bool = True,
) -> dict:
    """把板块/ETF/流动性补进已构建的 result（渐进加载第二阶段）。

    可对 build_outlook 的核心结果重复调用；幂等、fail-soft。
    完成后重算风险预算、预警与数据源状态，返回同一个 result。
    """
    snap = (result.get("indices") or {}).get(result.get("primary") or "") or {}
    market = result.get("market") or {}
    dq = result.setdefault("data_quality", {})
    if include_sectors:
        try:
            sectors, q = _DATA_CACHE.get("sectors", 300, build_sector_rotation, fail_cooldown=180)
            q = dict(q)
            if sectors:
                q["source"] = sectors.get("source")
            result["sectors"] = sectors
            dq["sectors"] = q
        except Exception as e:  # noqa: BLE001
            log_exc("行业轮动加载失败", e)
    if include_liquidity:
        try:
            from .ashare_liquidity import build_liquidity_context

            liquidity, q = _DATA_CACHE.get(
                "liquidity", 6 * 3600, build_liquidity_context, fail_cooldown=600
            )
            result["liquidity"] = liquidity
            dq["liquidity"] = q
        except Exception as e:  # noqa: BLE001
            log_exc("流动性确认失败", e)
    if include_valuation:
        try:
            from .ashare_valuation import build_valuation_context

            bond10 = (result.get("liquidity") or {}).get("bond10")
            valuation, q = _DATA_CACHE.get(
                "valuation",
                6 * 3600,
                lambda: build_valuation_context(bond10),
                fail_cooldown=600,
            )
            result["valuation"] = valuation
            dq["valuation"] = q
        except Exception as e:  # noqa: BLE001
            log_exc("估值确认失败", e)
    if include_etf:
        try:
            etf, q = _DATA_CACHE.get("etf", 900, build_etf_confirmation, fail_cooldown=180)
            q = dict(q)
            if etf and not etf.get("flow_ok", True):
                if etf.get("proxy_ok"):
                    q["ok"] = True
                    q["error"] = "量价代理（非真实申赎）"
                else:
                    q["ok"] = False
                    q["error"] = "20日资金缺失"
            if etf:
                etf["vs_market"] = _etf_vs_market(snap.get("score"), etf.get("label"))
            result["etf"] = etf
            dq["etf"] = q
        except Exception as e:  # noqa: BLE001
            log_exc("ETF确认加载失败", e)

    result["risk_budget"] = risk_budget(
        snap.get("score"),
        snap.get("atr_pct"),
        (market.get("breadth") or {}).get("up_ratio"),
        (market.get("fund") or {}).get("total_yi"),
        snap.get("score_pct"),
    )
    result["alerts"] = build_alerts(result)
    result["source_stats"] = _DATA_CACHE.stats()
    return result


# ─────────────────────────────────────────────────────────────
# 报告渲染
# ─────────────────────────────────────────────────────────────


def _fmt(x, digits=2) -> str:
    v = _f(x)
    return "--" if v is None else f"{v:.{digits}f}"


def _pct(x) -> str:
    v = _f(x)
    return "--" if v is None else f"{v * 100:.0f}%"


def _credit_label(lq: dict) -> str:
    src = str((lq or {}).get("credit_source") or "")
    return "增量滚动同比(备源)" if src.startswith("增量") else "累计同比"


_VALUATION_ORDER = ("sh000300", "sh000905", "sh000001", "sh000688")


def _valuation_sentence(val: dict) -> str:
    """大白话：先说最常用的沪深300/中证500, 再补 ERP。"""
    items = (val or {}).get("indices") or {}
    parts = []
    for code in _VALUATION_ORDER:
        it = items.get(code)
        if not it or it.get("pe") is None:
            continue
        pct = it.get("pe_pct")
        tail = f"历史分位 {_fmt(pct, 0)}%" if pct is not None else "历史分位样本累积中"
        parts.append(f"{it.get('name')} {_fmt(it.get('pe'), 1)} 倍（{tail}）")
        if len(parts) >= 2:
            break
    erp = next((it.get("erp") for it in items.values() if it.get("erp") is not None), None)
    if erp is not None:
        parts.append(f"ERP {_fmt(erp)}%（股票相对国债的性价比，越高越便宜）")
    if not parts:
        return "估值数据不足。"
    return "；".join(parts) + "。估值只影响中期，不决定明天涨跌。"


_QUALITY_LABELS = {
    "indices": "指数",
    "sectors": "行业",
    "etf": "ETF",
    "liquidity": "流动性",
    "valuation": "估值",
}
_MARKET_QUALITY_LABELS = {
    "env": "大盘环境",
    "breadth": "市场广度",
    "amount": "成交额",
    "fund": "主力资金",
    "emotion": "情绪",
    "resonance": "指数共振",
}


def _quality_issues(dq: dict) -> list[str]:
    issues = []
    for key, label in _QUALITY_LABELS.items():
        q = dq.get(key)
        if not isinstance(q, dict):
            continue
        if not q.get("ok"):
            issues.append(f"{label}缺失")
        elif q.get("stale"):
            issues.append(f"{label}过期")
    mq = dq.get("market") or {}
    for key, label in _MARKET_QUALITY_LABELS.items():
        if key in mq and not mq.get(key):
            issues.append(f"{label}缺失")
    return issues


def _plain_conclusion(result: dict) -> list[tuple[str, str]]:
    """把系统结论翻译成大白话表格 (问题 → 回答)。"""
    p = (result.get("indices") or {}).get(result.get("primary")) or {}
    m = result.get("market") or {}
    breadth = m.get("breadth") or {}
    amount = m.get("amount") or {}
    emotion = m.get("emotion") or {}
    etf = result.get("etf") or {}
    sec = result.get("sectors") or {}
    lv = result.get("primary_levels") or {}
    an = result.get("analogs") or {}
    rb = result.get("risk_budget") or {}
    sup = _f(lv.get("tr_bottom"))
    res = _f(lv.get("tr_top"))
    score_v = _f(p.get("score"), 50.0)
    s_pct = _f(p.get("score_pct"))
    pct_hint = f"，历史分位 {s_pct:.0f}%" if s_pct is not None else ""

    rows: list[tuple[str, str]] = []
    low, high = rb.get("low"), rb.get("high")
    pos = f"{low}%-{high}%" if low is not None and high is not None else "--"

    # 0. 一句话结论 (给只想看一句的人)
    state = p.get("state") or "--"
    env = m.get("env") or "环境未知"
    ref = s_pct if s_pct is not None else score_v
    if ref <= 10:
        stance = "先防守，别急着抄底"
    elif ref <= 25:
        stance = "以防守为主，等信号"
    elif ref <= 75:
        stance = "可以适度参与，别追高"
    else:
        stance = "可以积极一些，但别梭哈"
    one = f"{stance}：{state}（{score_v:.0f} 分{pct_hint}）、{env}，仓位参考 {pos}"
    if sup and res:
        one += f"；守住 {sup:.0f}、站上 {res:.0f} 再转积极"
    rows.append(("一句话结论", one + "。"))

    # 1. 现在市场怎么样
    score = _fmt(p.get("score"), 1)
    trend = "趋势还在往下" if _f(p.get("trend_score"), 0) < 60 else "趋势偏强"
    extra = []
    up_n, down_n = breadth.get("up"), breadth.get("down")
    if up_n is not None and down_n is not None and (int(up_n) + int(down_n)) > 0:
        extra.append(f"涨 {up_n} / 跌 {down_n}")
    amt = _f(amount.get("total_yi"))
    if amt is not None and amt > 0:
        extra.append(f"成交 {_fmt(amt, 0)} 亿")
    if emotion.get("phase"):
        extra.append(f"情绪 {emotion.get('phase')}")
    tail = "；".join(extra)
    rows.append(
        (
            "现在市场怎么样？",
            f"{state}（{score} 分{pct_hint}），{env}，{trend}。" + (f"{tail}。" if tail else ""),
        )
    )

    # 1.5 和上次相比
    ch = result.get("changes") or {}
    if ch:
        bits = []
        delta = _f(ch.get("score_delta"))
        if delta is not None:
            bits.append(f"分数 {delta:+.1f}")
        if ch.get("state_changed"):
            bits.append(f"状态 {ch.get('state_from')}→{ch.get('state_to')}")
        if ch.get("phase_changed"):
            bits.append(f"阶段 {ch.get('phase_from')}→{ch.get('phase_to')}")
        close_delta = _f(ch.get("close_delta_pct"))
        if close_delta is not None:
            bits.append(f"收盘 {close_delta:+.1f}%")
        if ch.get("etf_from") != ch.get("etf_to"):
            bits.append(f"ETF {ch.get('etf_from')}→{ch.get('etf_to')}")
        if ch.get("liquidity_from") != ch.get("liquidity_to"):
            bits.append(f"流动性 {ch.get('liquidity_from')}→{ch.get('liquidity_to')}")
        if bits:
            rows.append(
                ("比上次怎么样？", "；".join(bits) + f"（上次 {ch.get('previous_as_of')}）。")
            )

    # 2. 钱在往哪走
    if sec:
        strong = "、".join(r["name"] for r in (sec.get("top") or [])[:3])
        weak = "、".join(r["name"] for r in (sec.get("bottom") or [])[:3])
        gap = _fmt(sec.get("style_gap"))
        note = "（新浪行业：仅当日涨跌，无20日资金）" if sec.get("source") == "sina" else ""
        rows.append(("钱在往哪走？", f"强：{strong}；弱：{weak}；防御/成长资金差 {gap} 亿。{note}"))
    else:
        rows.append(("钱在往哪走？", "行业数据暂缺。"))

    # 3. 国家队托不托
    if etf.get("ok"):
        proxy_suffix = (
            "（量价代理）"
            if etf.get("flow_source") == "量价代理" and "量价代理" not in str(etf.get("label"))
            else ""
        )
        rows.append(
            (
                "国家队在托吗？",
                f"{etf.get('label')}{proxy_suffix}；"
                f"宽基 ETF 近 20 日 {_fmt(etf.get('net_y20'))} 亿；"
                f"{etf.get('vs_market')}。",
            )
        )
    else:
        rows.append(("国家队在托吗？", "数据不足，无法判断。"))

    # 3.5 资金面松不松
    lq = result.get("liquidity") or {}
    if lq.get("ok"):
        plain_lq = {
            "宽松": "钱比较松（中期偏利好）",
            "中性": "钱不松不紧",
            "偏紧": "钱偏紧（中期偏不利）",
        }.get(lq.get("label"), lq.get("label"))
        rows.append(
            (
                "资金面松不松？",
                f"{plain_lq}（{_fmt(lq.get('score'), 1)} 分）；"
                f"细分：M1-M2 {_fmt(lq.get('m1_m2'))}，"
                f"社融{_credit_label(lq)} {_fmt(lq.get('credit_yoy'))}%，"
                f"SHIBOR 3M {_fmt(lq.get('shibor3m'))}%，"
                f"10Y {_fmt(lq.get('bond10'))}%"
                f"（月频数据截止 {lq.get('as_of') or '--'}）。",
            )
        )
    else:
        rows.append(("资金面松不松？", "数据不足。"))

    # 3.6 贵不贵 (估值只作中期锚)
    val = result.get("valuation") or {}
    if val.get("ok"):
        rows.append(("贵不贵？", _valuation_sentence(val)))

    # 3.6 数据质量
    dq = result.get("data_quality") or {}
    if dq:
        issues = _quality_issues(dq)
        rows.append(("数据靠谱吗？", "；".join(issues) if issues else "主要数据正常。"))

    # 3.7 预警
    alerts = result.get("alerts") or []
    if alerts:
        brief = "；".join(f"{a.get('title')}（{a.get('detail')}）" for a in alerts[:3])
        if len(alerts) > 3:
            brief += "…"
        rows.append(("有没有预警？", brief + "。"))

    # 4. 关键看什么
    name = p.get("name") or "主指数"
    if sup and res:
        rows.append(
            (
                "关键看什么？",
                f"{name} {sup:.0f} 是防线（跌破要小心）；{res:.0f} 是转强线（站上才算转多）。",
            )
        )
    elif sup:
        rows.append(("关键看什么？", f"{name} {sup:.0f} 是防线（跌破要小心）。"))
    else:
        rows.append(("关键看什么？", "关键位数据不足。"))

    # 5. 后面可能怎么走
    scenarios = result.get("scenarios") or []

    def _scene(key):
        return next((s for s in scenarios if key in s.get("name", "")), None)

    base, bear, bull = _scene("基准"), _scene("风险"), _scene("反转")
    parts = []
    if base:
        parts.append(f"基准 {base.get('path')}")
    if bear:
        parts.append(f"破位看 {bear.get('path')}")
    if bull:
        parts.append(f"站上看 {bull.get('path')}")
    rows.append(("后面可能怎么走？", "；".join(parts) + "。" if parts else "情景数据不足。"))

    # 6. 该怎么做
    if ref <= 25:
        action = (
            f"轻仓、防守、别追高；等 {sup:.0f} 守住或 {res:.0f} 站上再说。"
            if sup and res
            else "轻仓、防守、别追高，等信号。"
        )
    else:
        action = "别追高，等回踩确认；方向没确认前别重仓。"
    rows.append(("现在该怎么做？", f"仓位参考 {pos}（不是必须满仓）；{action}"))

    # 6.5 判断错了怎么办 (可证伪)
    if sup and res:
        rows.append(
            (
                "什么情况下算判断错了？",
                f"如果收盘重新站上 {res:.0f} 并站稳，说明偏弱判断要修正，可以转积极；"
                f"如果跌破 {sup:.0f} 收不回来，说明风险升级，别抄底。",
            )
        )

    # 7. 历史概率
    if an.get("ok"):
        hz = an.get("horizons") or {}

        def _part(h: int, label: str) -> str:
            st = hz.get(str(h)) or {}
            v = st.get("up_prob_ind")
            if v is None:
                v = st.get("up_prob")
            s = f"{label} {_pct(v)}"
            lo, hi = st.get("up_ci_low"), st.get("up_ci_high")
            if lo is not None and hi is not None:
                s += f"（区间 {_pct(lo)}~{_pct(hi)}）"
            if st.get("n_eff"):
                s += f"，独立样本 {st.get('n_eff')}"
            return s

        rows.append(
            (
                "历史概率怎么说？",
                f"类似状态：{_part(5, '5日')}；{_part(20, '20日')}；{_part(60, '60日')}。"
                "区间宽/独立样本少时接近抛硬币，只能参考。",
            )
        )

    # 7.5 状态转移 (经验概率)
    tr = result.get("state_transitions") or {}
    if tr.get("ok"):

        def _tp(h: int) -> str:
            x = (tr.get("horizons") or {}).get(str(h)) or {}
            if x.get("better") is None:
                return f"{h}日 数据不足"
            return (
                f"{h}日 更积极 {_pct(x.get('better'))} / "
                f"维持 {_pct(x.get('same'))} / 更弱 {_pct(x.get('worse'))}"
            )

        rows.append(
            (
                "接下来更可能是什么状态？",
                f"{_tp(5)}；{_tp(20)}。经验概率，不是预测；"
                "某档若在样本内不可达，对应方向会显示 0%，不代表不会发生。",
            )
        )

    # 8. 术语 (大白话)
    rows.append(
        (
            "看不懂术语？",
            "MA200=200日均线（常当牛熊分界）；P&F=点数图的支撑/阻力；"
            "状态档=分数在自身历史中的分位（≤10% 风险规避、10-25% 谨慎、"
            "25-50% 防御、50-75% 中性偏多、>75% 进攻）；"
            "ERP=股票相对国债的性价比（越高越便宜）；"
            "量价代理=用成交量估的资金方向，不是真实申赎；"
            "历史概率=相似状态后的经验分布，不是预测。",
        )
    )

    return rows


def render_report(result: dict) -> str:
    """把 build_outlook 的结果渲染成 Markdown 文本。"""
    if result.get("error"):
        return f"# A股市场状态报告\n\n生成失败: {result['error']}"

    primary = result.get("primary")
    p = (result.get("indices") or {}).get(primary) or {}

    lines = [
        "# A股市场状态报告",
        "",
        f"- 数据日期: **{result.get('as_of') or '--'}**",
        f"- 生成时间: {result.get('generated_at')}",
        f"- 主分析指数: **{p.get('name')} ({primary})**",
        "",
        "## 大白话结论",
        "",
        "| 问题 | 大白话回答 |",
        "|---|---|",
    ]
    for q, a in _plain_conclusion(result):
        lines.append(f"| {q} | {a} |")
    if p:
        lines += [
            "",
            f"> 状态分构成（v2 消融后）：趋势 {_fmt(p.get('trend_score'), 0)} ×60% + "
            f"回撤 {_fmt(p.get('drawdown_score'), 0)} ×40%；"
            f"动量 {_fmt(p.get('momentum_score'), 0)} / "
            f"低波动 {_fmt(p.get('vol_score'), 0)} 已消融剔除，仅展示参考",
        ]

    alerts = result.get("alerts") or []
    if alerts:
        lines += ["", "## 预警", ""]
        for a in alerts:
            mark = "⚠" if a.get("level") == "warn" else "•"
            lines.append(f"- {mark} **{a.get('title')}**: {a.get('detail')}")
        lines.append("")

    lines += ["", "## 指数状态", ""]
    lines.append("| 指数 | 收盘 | 当日 | 状态分 | 历史分位 | 状态 | 日线阶段 | 20日 | 距250日高 |")
    lines.append("|---|---:|---:|---:|---:|---|---|---:|---:|")
    for code, _name in OUTLOOK_INDICES:
        it = (result.get("indices") or {}).get(code)
        if not it:
            continue
        pct = it.get("score_pct")
        pct_txt = f"{_fmt(pct, 0)}%" if pct is not None else "--"
        lines.append(
            f"| {it.get('name')} | {_fmt(it.get('close'))} | "
            f"{_fmt(it.get('day_pct'))}% | {_fmt(it.get('score'), 1)} | "
            f"{pct_txt} | {it.get('state')} | {it.get('phase') or '--'} | "
            f"{_fmt(it.get('ret20'))}% | {_fmt(it.get('dd250'))}% |"
        )
    lines.append("")
    lines.append(
        "> 状态按历史分位（v3）：≤10% 风险规避 / 10–25% 谨慎 / 25–50% 防御 / "
        "50–75% 中性偏多 / >75% 进攻；历史分位=当前分数在自身历史中的位置（越低越弱）。"
    )

    # 和上次相比
    ch = result.get("changes")
    if ch:
        lines += ["", "## 和上次相比", ""]
        lines.append(f"- 上次记录: **{ch.get('previous_as_of')}**")
        delta = _f(ch.get("score_delta"))
        if delta is not None:
            lines.append(f"- 状态分变化: **{delta:+.1f}**")
        if ch.get("state_changed"):
            lines.append(f"- 状态: {ch.get('state_from')} → **{ch.get('state_to')}**")
        if ch.get("phase_changed"):
            lines.append(f"- 阶段: {ch.get('phase_from')} → **{ch.get('phase_to')}**")
        close_delta = _f(ch.get("close_delta_pct"))
        if close_delta is not None:
            lines.append(f"- 收盘变化: **{close_delta:+.2f}%**")
        if ch.get("etf_from") != ch.get("etf_to"):
            lines.append(f"- ETF 确认: {ch.get('etf_from')} → **{ch.get('etf_to')}**")
        if ch.get("liquidity_from") != ch.get("liquidity_to"):
            lines.append(f"- 流动性: {ch.get('liquidity_from')} → **{ch.get('liquidity_to')}**")
        up_from, up_to = _f(ch.get("up_ratio_from")), _f(ch.get("up_ratio_to"))
        if up_from is not None and up_to is not None:
            lines.append(f"- 上涨占比: {_pct(up_from)} → **{_pct(up_to)}**")

    # 历史类比
    lines += ["", "## 历史类比（相似状态之后怎么走）", ""]
    an = result.get("analogs") or {}
    if an.get("ok"):
        lines.append(
            f"匹配到 **{an.get('n_analogs')}** 个历史相似日；"
            f"平均距离 {_fmt(an.get('avg_distance'))}（越小越像）。"
        )
        lines.append("")
        lines.append("| 未来 | 样本 | 有效样本 | 原始概率 | 独立概率 | 95%区间 | 中位数 |")
        lines.append("|---:|---:|---:|---:|---:|---|---:|")
        for h in HORIZONS:
            st = (an.get("horizons") or {}).get(str(h))
            if not st:
                lines.append(f"| {h}日 | -- | -- | -- | -- | -- | -- |")
                continue
            ci = "--"
            if st.get("up_ci_low") is not None and st.get("up_ci_high") is not None:
                ci = f"{_pct(st.get('up_ci_low'))}~{_pct(st.get('up_ci_high'))}"
            lines.append(
                f"| {h}日 | {st.get('n')} | {st.get('n_eff') or '--'} | "
                f"{_pct(st.get('up_prob'))} | {_pct(st.get('up_prob_ind'))} | "
                f"{ci} | {_fmt(st.get('median'))}% |"
            )
        lines.append("")
        lines.append(
            "> 有效样本 = 按非重叠窗口挑出的独立样本（相邻相似日高度相关，"
            "60 个样本实际可能只有 3~8 个独立样本）；区间越宽越不确定，"
            "单点概率不能当方向依据。"
        )

        # 自动解读: 极端弱状态在历史上是“继续跌”还是“跌深反抽”
        score = _f(p.get("score"), 50.0)
        hz = an.get("horizons") or {}

        def _p_ind(st: dict):
            v = st.get("up_prob_ind")
            return st.get("up_prob") if v is None else v

        st20 = hz.get("20") or {}
        st60 = hz.get("60") or {}
        up20, up60, med60 = (_p_ind(st20), _p_ind(st60), st60.get("median"))
        if score < 30 and up60 is not None and up60 >= 0.55:
            weak = "极端弱状态" if score < 20 else "偏弱状态"
            lines.append(
                f"> 解读: 当前是{weak}，但历史上类似状态的 60 日上涨概率 "
                f"{_pct(up60)}、中位数 {_fmt(med60)}%——更像“跌深反抽”，"
                f"不是单边继续崩。反抽不等于反转，仍需右侧确认。"
            )
        elif score < 30 and up60 is not None and up60 < 0.45:
            lines.append(
                f"> 解读: 历史上类似弱状态继续走弱概率偏高 "
                f"(60日上涨概率 {_pct(up60)})，以防守为主。"
            )
        elif score >= 70 and up20 is not None and up20 < 0.50:
            lines.append(
                f"> 解读: 强势状态下 20 日上涨概率只有 {_pct(up20)}，"
                f"历史上反而容易过热回落，不宜追高。"
            )
    else:
        lines.append(f"样本不足或数据不可用：{an.get('reason', '--')}")

    # 状态转移
    tr = result.get("state_transitions") or {}
    if tr.get("ok"):
        cur = tr.get("current") or "未知"
        dur = (tr.get("durations") or {}).get(cur)
        lines += ["", "## 状态转移（经验概率，非预测）", ""]
        lines.append(
            f"- 当前状态: **{cur}**"
            + (f"；历史上该状态平均持续 {_fmt(dur, 1)} 个交易日" if dur is not None else "")
        )
        lines.append("")
        lines.append("| 未来 | " + " | ".join(STATE_NAMES) + " |")
        lines.append("|---:|" + "---:|" * len(STATE_NAMES))
        for h in HORIZONS:
            hz = (tr.get("horizons") or {}).get(str(h)) or {}
            probs = hz.get("probs") or []
            if len(probs) != len(STATE_NAMES):
                lines.append(f"| {h}日 | -- | -- | -- | -- | -- |")
                continue
            lines.append(f"| {h}日 | " + " | ".join(_pct(x) for x in probs) + " |")
        lines.append("")
        parts = []
        for h in (5, 20):
            hz = (tr.get("horizons") or {}).get(str(h)) or {}
            if hz.get("better") is not None:
                parts.append(
                    f"{h}日：更积极 {_pct(hz.get('better'))} / "
                    f"维持 {_pct(hz.get('same'))} / 更弱 {_pct(hz.get('worse'))}"
                )
        if parts:
            lines.append("> " + "；".join(parts) + "。")
        counts = tr.get("counts") or {}
        if counts:
            lines.append(
                "> 历史天数: " + " / ".join(f"{s} {counts.get(s, 0)}" for s in STATE_NAMES)
            )
        unreachable = tr.get("unreachable") or []
        if unreachable:
            lines.append(
                "> 注意: "
                + "、".join(unreachable)
                + " 档在该样本内不可达（分数未进入该区间），对应转移概率为 0 "
                "是分档结构问题，不代表市场不会更弱。"
            )
        lines.append(f"> {tr.get('note', '')}")

    # 历史回测
    bt = result.get("backtest") or {}
    if bt.get("ok"):
        lines += ["", "## 状态分历史分档（未来20日）", ""]
        lines.append("| 状态档 | 样本 | 上涨概率 | 中位数 | 10%分位 | 90%分位 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in bt.get("buckets") or []:
            flag = " ⚠样本少" if row.get("small_sample") else ""
            lines.append(
                f"| {row.get('label')} ({row.get('range')}){flag} | {row.get('n')} | "
                f"{_pct(row.get('up_prob'))} | {_fmt(row.get('median'))}% | "
                f"{_fmt(row.get('p10'))}% | {_fmt(row.get('p90'))}% |"
            )
        if bt.get("baseline"):
            lines.append("")
            lines.append("**对照基线**：")
            lines.append("")
            lines.append("| 基线 | 样本 | 上涨概率 | 中位数 |")
            lines.append("|---|---:|---:|---:|")
            for row in bt.get("baseline") or []:
                lines.append(
                    f"| {row.get('label')} | {row.get('n')} | "
                    f"{_pct(row.get('up_prob'))} | {_fmt(row.get('median'))}% |"
                )

    # 状态分机械规则回测
    bts = result.get("backtest_strategy") or {}
    if bts.get("ok"):
        lines += ["", "## 状态分机械规则回测（仅参考）", ""]
        lines.append(
            f"- 区间: {bts.get('start')} ~ {bts.get('end')}；"
            f"成本: 单边 {_fmt(bts.get('cost_bps'), 1)} bp"
        )
        lines.append("")
        lines.append("| 策略 | 总收益 | 年化 | 最大回撤 | Sharpe | 平均仓位 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for key, label in (
            ("strategy", "状态分"),
            ("overlay", "MA200+状态分"),
            ("ma200", "MA200"),
            ("buy_hold", "买入持有"),
        ):
            mm = bts.get(key) or {}
            lines.append(
                f"| {label} | {_fmt(mm.get('total_return'))}% | "
                f"{_fmt(mm.get('cagr'))}% | {_fmt(mm.get('max_drawdown'))}% | "
                f"{_fmt(mm.get('sharpe'))} | {_fmt(mm.get('avg_exposure'), 1)}% |"
            )
        lines.append("")
        s_ret = _f((bts.get("strategy") or {}).get("total_return"))
        o_ret = _f((bts.get("overlay") or {}).get("total_return"))
        m_ret = _f((bts.get("ma200") or {}).get("total_return"))
        if s_ret is not None and m_ret is not None and s_ret < m_ret:
            lines.append(
                "> ⚠ 本次回测状态分跑输 MA200：它更适合当仪表盘/风险预算，不要单独当择时策略。"
            )
        if o_ret is not None and m_ret is not None:
            if o_ret >= m_ret:
                lines.append(
                    f"> Overlay 跑赢 MA200：{_fmt(o_ret)}% vs {_fmt(m_ret)}%，"
                    "说明“MA200 打底 + 状态分降风险”比单独状态分更稳。"
                )
            else:
                lines.append(
                    f"> ⚠ Overlay 跑输 MA200：{_fmt(o_ret)}% vs {_fmt(m_ret)}%，"
                    "状态分作为风险 overlay 也没有稳定加分，只当参考。"
                )
        rolling = bts.get("rolling") or {}
        if rolling.get("n"):
            lines.append(
                f"> 滚动 {rolling.get('window')} 日窗口（步长 {rolling.get('step')} 日）: "
                f"Overlay 跑赢 {_fmt(rolling.get('win_rate'), 1)}% 的窗口，"
                f"平均超额 {_fmt(rolling.get('avg_excess'))}%。"
            )
        oos = bts.get("oos") or {}
        if oos.get("strategy"):
            lines.append(
                f"> 样本外（最近 {oos.get('days')} 日，{oos.get('start')} 起）: "
                f"状态分 {_fmt(oos['strategy'].get('total_return'))}% / "
                f"MA200 {_fmt(oos['ma200'].get('total_return'))}% / "
                f"买入持有 {_fmt(oos['buy_hold'].get('total_return'))}%。"
                "近期趋势型打分跑输属弱市常态，不代表配方失效。"
            )
        abla = bts.get("ablation") or {}
        abla_rows = {r.get("name"): r for r in abla.get("rows") or []}
        if abla_rows:
            ic_parts = "，".join(
                f"{name} IC {_fmt(abla_rows[name].get('ic'), 3)}"
                for name in ("趋势", "动量", "低波动", "回撤")
                if name in abla_rows
            )
            lines.append(f"> 成分消融: {ic_parts} → v2 = 趋势×60% + 回撤×40%。")
        wf = bts.get("walkforward") or {}
        wfic = wf.get("ic") or {}
        wfy = wf.get("yearly") or {}
        if wfic.get("n"):
            lines.append(
                f"> Walk-forward: 全样本 IC {_fmt(wfic.get('full_ic'), 3)}，"
                f"滚动窗口 IC>0 的仅 {_fmt(wfic.get('positive_rate'), 1)}%"
                f"（中位 {_fmt(wfic.get('median'), 3)}）；"
                f"逐年跑赢 MA200 {_fmt(wfy.get('beat_ma200'), 1)}%"
                f"（{wfy.get('n_years') or 0} 年），"
                f"平均超额 {_fmt(wfy.get('avg_excess'))}%。"
                "→ 状态分稳定性弱，只当仪表盘。"
            )
        lines.append(
            "> 机械仓位映射，非最优参数；含单边成本。跑不赢 MA200/买入持有，"
            "就说明状态分只适合当仪表盘，不适合单独择时。"
        )

    # 关键位
    lv = result.get("primary_levels") or {}
    if lv:
        lines += ["", "## 关键位", ""]
        lines.append(f"- 现价: **{_fmt(lv.get('close'))}**")
        lines.append(
            f"- 均线: MA20 {_fmt(lv.get('ma20'))} / "
            f"MA50 {_fmt(lv.get('ma50'))} / MA200 {_fmt(lv.get('ma200'))}"
        )
        if lv.get("tr_bottom") or lv.get("tr_top"):
            lines.append(f"- P&F 区间: **{_fmt(lv.get('tr_bottom'))} ~ {_fmt(lv.get('tr_top'))}**")
        if lv.get("near_up") or lv.get("near_down"):
            lines.append(
                f"- 近端目标: 上 {_fmt(lv.get('near_up'))} / 下 {_fmt(lv.get('near_down'))}"
            )
        if lv.get("support_levels"):
            lines.append(f"- 近期支撑: {', '.join(_fmt(v) for v in lv['support_levels'])}")
        if lv.get("resistance_levels"):
            lines.append(f"- 近期阻力: {', '.join(_fmt(v) for v in lv['resistance_levels'][:6])}")
        if lv.get("atr_pct") is not None:
            lines.append(f"- ATR: {_fmt(lv.get('atr_pct'))}%")

        def _target_line(label, targets, probs):
            parts = []
            for t, pr in zip(targets or [], probs or []):
                if _f(t) is None:
                    continue
                if _f(pr) is not None:
                    parts.append(f"{_fmt(t, 0)} ({_pct(pr)})")
                else:
                    parts.append(_fmt(t, 0))
            if parts:
                lines.append(f"- P&F {label}: {' / '.join(parts)}")

        _target_line("上方三档", lv.get("up_targets"), lv.get("up_prob"))
        _target_line("下方三档", lv.get("down_targets"), lv.get("down_prob"))

    # 国家队 / 宽基 ETF 确认
    etf = result.get("etf")
    if etf:
        lines += ["", "## 国家队/宽基ETF确认", ""]
        if etf.get("ok"):
            lines.append(f"- 结论: **{etf.get('label')}**（聚合分 {_fmt(etf.get('score'), 3)}）")
            lines.append(
                f"- 宽基ETF: 近1日 {_fmt(etf.get('net_y1'))} 亿 / "
                f"近20日 {_fmt(etf.get('net_y20'))} 亿"
            )
            if etf.get("flow_source") == "量价代理":
                lines.append("- 来源: **量价代理**（东财资金流断连，非真实申赎，仅参考）")
            lines.append(
                f"- 三因子: 买 {etf.get('factor_buy')} / 卖 {etf.get('factor_sell')}，"
                f"平均买-卖 {_fmt(etf.get('avg_diff'))}"
            )
            lines.append(
                f"- 季报: 加仓/新进 {etf.get('holdings_add')} / "
                f"减仓/退出 {etf.get('holdings_cut')}"
                f"（{etf.get('holdings_sample')} 只样本）"
            )
            lines.append(f"- 与大盘: **{etf.get('vs_market')}**")
            for w in etf.get("warnings") or []:
                lines.append(f"- 注意: {w}")
        else:
            lines.append(f"- 数据不足: {etf.get('reason', '--')}")

    # 流动性 / 信用确认
    lq = result.get("liquidity")
    if lq:
        lines += ["", "## 流动性/政策确认", ""]
        if lq.get("ok"):
            lines.append(f"- 结论: **{lq.get('label')}**（{_fmt(lq.get('score'), 1)}/100）")
            lines.append(
                f"- M1 同比 {_fmt(lq.get('m1_yoy'))}% / M2 同比 "
                f"{_fmt(lq.get('m2_yoy'))}%；M1-M2 {_fmt(lq.get('m1_m2'))} "
                f"（历史分位 {_fmt(lq.get('m1_m2_pct'), 0)}%）"
            )
            lines.append(
                f"- 社融{_credit_label(lq)} {_fmt(lq.get('credit_yoy'))}% "
                f"（历史分位 {_fmt(lq.get('credit_pct'), 0)}%）"
            )
            if str(lq.get("credit_source") or "").startswith("增量"):
                lines.append(
                    "  - 注：主源断连，改用社融增量 12 个月滚动同比；"
                    "与官方累计同比口径不完全可比，仅作方向参考。"
                )
            lines.append(
                f"- SHIBOR 3M {_fmt(lq.get('shibor3m'))}% "
                f"（历史分位 {_fmt(lq.get('shibor_pct'), 0)}%）"
            )
            lines.append(
                f"- 中国 10Y {_fmt(lq.get('bond10'))}% （历史分位 {_fmt(lq.get('bond_pct'), 0)}%）"
            )
            lines.append(
                f"- 数据截止: 月频 {lq.get('as_of') or '--'} / 利率 {lq.get('rate_as_of') or '--'}"
            )
            lines.append("> 月频数据有滞后；利率低不等于马上涨，只说明资金环境偏松。")
        else:
            lines.append(f"- 数据不足: {lq.get('note', '--')}")

    # 估值 (中期锚, 非短期择时)
    val = result.get("valuation") or {}
    if val.get("ok"):
        lines += [
            "",
            "## 估值（中期锚，非择时信号）",
            "",
            f"- 数据: 截至 {val.get('as_of') or '--'}；{val.get('source') or '--'}",
            "",
            "| 指数 | PE(滚动) | 历史分位 | 股息率 | ERP | 状态 |",
            "|---|---:|---:|---:|---:|---|",
        ]
        v_items = val.get("indices") or {}
        for code in _VALUATION_ORDER:
            it = v_items.get(code)
            if not it:
                continue
            lines.append(
                f"| {it.get('name')} | {_fmt(it.get('pe'), 1)} | "
                f"{_fmt(it.get('pe_pct'), 0)}% | {_fmt(it.get('div'))}% | "
                f"{_fmt(it.get('erp'))}% | {it.get('label') or '--'} |"
            )
        if "sz399006" not in v_items:
            lines.append("")
            lines.append("> 创业板指不在中证官网估值文件内，暂缺。")
        lines.append("")
        lines.append(f"> {val.get('note', '')}")

    # 情景
    if result.get("scenarios"):
        lines += ["", "## 情景与触发条件", ""]
        for s in result["scenarios"]:
            lines.append(f"**{s.get('name')}**")
            lines.append(f"- 触发: {s.get('trigger')}")
            lines.append(f"- 路径: {s.get('path')}")
            if s.get("note"):
                lines.append(f"- 备注: {s.get('note')}")
            lines.append("")

    # 行业
    sec = result.get("sectors")
    if sec:
        lines += ["", "## 行业与风格", ""]
        lines.append(f"- 覆盖行业: {sec.get('n')} 个")
        if sec.get("source") == "sina":
            lines.append("- 数据源: 新浪行业（只有当日涨跌，无20日资金）")
        lines.append(f"- 防御/成长 20日资金差: **{_fmt(sec.get('style_gap'))} 亿**")
        lines.append("")
        lines.append("**相对强（20日资金 + 当日强度）**")
        for r in sec.get("top") or []:
            lines.append(
                f"- {r.get('name')}: {_fmt(r.get('pct'))}%，20日资金 {_fmt(r.get('flow20_yi'))} 亿"
            )
        lines.append("")
        lines.append("**相对弱**")
        for r in sec.get("bottom") or []:
            lines.append(
                f"- {r.get('name')}: {_fmt(r.get('pct'))}%，20日资金 {_fmt(r.get('flow20_yi'))} 亿"
            )

    # 风险预算
    rb = result.get("risk_budget") or {}
    if rb:
        lines += ["", "## 风险预算", ""]
        lines.append(f"- 权益仓位参考区间: **{rb.get('low')}% - {rb.get('high')}%**")
        lines.append(f"- 说明: {rb.get('note')}")

    # 数据质量
    dq = result.get("data_quality") or {}
    if dq:
        lines += ["", "## 数据质量", ""]
        lines.append("| 数据 | 状态 | 更新/截止 | 备注 |")
        lines.append("|---|---|---|---|")
        for key, label in _QUALITY_LABELS.items():
            q = dq.get(key)
            if not isinstance(q, dict):
                continue
            if q.get("ok") and not q.get("stale"):
                status = "正常"
            elif q.get("stale"):
                status = "过期"
            else:
                status = "缺失"
            note = q.get("error") or ""
            if key == "indices" and q.get("missing"):
                note = "缺失: " + ", ".join(q["missing"])
            elif key == "sectors" and q.get("source") == "sina":
                note = "新浪行业（无20日资金）"
            lines.append(
                f"| {label} | {status} | {q.get('as_of') or result.get('as_of') or '--'} | "
                f"{note[:60] or '--'} |"
            )
        mq = dq.get("market") or {}
        for key, label in _MARKET_QUALITY_LABELS.items():
            if key not in mq:
                continue
            status = "正常" if mq.get(key) else "缺失"
            lines.append(f"| {label} | {status} | {result.get('as_of') or '--'} | -- |")
        lines.append("")
        lines.append("> 过期=本次拉取失败，用了上次缓存；缺失=没有数据，相关结论不可用。")

        ss = result.get("source_stats") or {}
        if ss:
            lines += [
                "",
                "### 数据源状态",
                "",
                "| 数据源 | 当前状态 | 最后成功 | 上次拉取 | 失败/拉取 | 最近错误 |",
                "|---|---|---|---|---|---|",
            ]
            for key, label in _QUALITY_LABELS.items():
                st = ss.get(key)
                if not isinstance(st, dict):
                    continue
                q = dq.get(key) or {}
                if q.get("ok") and not q.get("stale"):
                    status = "正常"
                elif q.get("stale"):
                    status = "过期"
                else:
                    status = "缺失"
                cd = st.get("cooldown_s")
                if isinstance(cd, (int, float)) and cd > 0:
                    status = f"退避 {int(cd)}s"
                ok_n = int(st.get("ok") or 0)
                fail_n = int(st.get("fail") or 0)
                attempts = ok_n + fail_n
                rate = f"{fail_n}/{attempts} ({fail_n / attempts * 100:.0f}%)" if attempts else "--"
                ms = st.get("last_ms")
                ms_text = f"{float(ms):.0f} ms" if isinstance(ms, (int, float)) else "--"
                err = str(st.get("last_error") or "")[:48]
                lines.append(
                    f"| {label} | {status} | "
                    f"{st.get('last_success') or q.get('as_of') or '--'} | "
                    f"{ms_text} | {rate} | {err or '--'} |"
                )
            lines.append("")
            lines.append("> 延迟=上次真实拉取耗时；缓存命中不重复计时。")

    # 方法与限制
    lines += [
        "",
        "## 方法与限制",
        "",
        "- 状态分 v2 = 0.6×趋势 + 0.4×回撤，只使用价量特征，不使用未来数据；"
        "动量/低波动成分经消融（显著负 IC）剔除，详见“成分消融”表。",
        "- 状态分回测含全样本 + 最近 252 日样本外两段；样本外偏弱时不应上调对其的置信度。",
        "- 历史类比是“相似状态的经验分布”，不是预测；样本少时可信度低。",
        "- 相似日/分档样本可能重叠（相邻交易日），有效样本数小于显示数量。",
        "- P&F 目标概率来自历史校准，主要是个股/合成样本，不是指数专用。",
        "- 国家队/ETF 是资金流代理，不是官方持仓；份额变化可能受拆分/分红影响。",
        "- 流动性/信用是月频数据，有滞后；利率低不等于马上涨，只说明资金环境偏松。",
        "- 估值只作中期锚：PE 来自中证指数官网（最近约 20 个交易日），历史分位靠本地每日累积，"
        "不足 250 日前显示“样本累积中”；PB 数据源当前不可用。估值不预测短期涨跌。",
        "",
        f"> {result.get('disclaimer', DISCLAIMER)}",
    ]
    return "\n".join(lines)
