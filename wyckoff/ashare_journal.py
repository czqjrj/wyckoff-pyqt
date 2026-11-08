"""A股预测日志: 记录每次状态报告, 到期回填结果, 统计命中率/校准度。

用法:
    from .ashare_journal import record_outlook, backfill, compute_stats, render_stats
    info = record_outlook(result)          # 把 build_outlook() 结果落盘
    summary = backfill()                  # 回填已到期记录
    stats = compute_stats(horizon=20)     # 统计命中率/Brier/校准
    print(render_stats(stats))
"""

from __future__ import annotations

import json
import math
import os
import time

import pandas as pd

from ._log import log_exc
from .datasource import fetch_kline
from .paths import DATA_DIR

JOURNAL_FILE = os.path.join(DATA_DIR, "wx_ashare_journal.json")
STOCK_JOURNAL_FILE = os.path.join(DATA_DIR, "wx_ashare_stock_journal.json")
HORIZONS = (5, 20, 60)
_CAL_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────


def _f(x, default=None):
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


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _num_list(values) -> list[float]:
    out = []
    for v in values or []:
        f = _f(v)
        if f is not None:
            out.append(round(f, 2))
    return out


def load_journal(path: str | None = None) -> dict:
    """读取日志; 文件不存在/损坏时返回空日志。"""
    path = path or JOURNAL_FILE
    if not os.path.exists(path):
        return {"version": 1, "records": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log_exc("读取A股预测日志失败", e)
        return {"version": 1, "records": []}
    if isinstance(data, list):
        return {"version": 1, "records": data}
    if not isinstance(data, dict):
        return {"version": 1, "records": []}
    data.setdefault("version", 1)
    data.setdefault("records", [])
    if not isinstance(data["records"], list):
        data["records"] = []
    return data


def save_journal(data: dict, path: str | None = None) -> None:
    """原子写入日志。"""
    path = path or JOURNAL_FILE
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _extract_record(result: dict) -> dict:
    """从 build_outlook() 结果提取需要留档的字段。"""
    primary = result.get("primary") or ""
    p = (result.get("indices") or {}).get(primary) or {}
    lv = result.get("primary_levels") or {}
    rb = result.get("risk_budget") or {}
    market = result.get("market") or {}
    breadth = market.get("breadth") or {}
    amount = market.get("amount") or {}
    fund = market.get("fund") or {}
    emotion = market.get("emotion") or {}
    etf = result.get("etf") or {}
    lq = result.get("liquidity") or {}

    analogs = {}
    an = result.get("analogs") or {}
    for h in HORIZONS:
        st = (an.get("horizons") or {}).get(str(h)) or {}
        analogs[str(h)] = {
            "up_prob": _f(st.get("up_prob")),
            "median": _f(st.get("median")),
            "p10": _f(st.get("p10")),
            "p90": _f(st.get("p90")),
            "n": int(st.get("n") or 0),
        }

    levels = {
        "tr_bottom": _round(lv.get("tr_bottom")),
        "tr_top": _round(lv.get("tr_top")),
        "near_up": _round(lv.get("near_up")),
        "near_down": _round(lv.get("near_down")),
        "ma20": _round(lv.get("ma20")),
        "ma50": _round(lv.get("ma50")),
        "ma200": _round(lv.get("ma200")),
        "support_levels": _num_list(lv.get("support_levels")),
        "resistance_levels": _num_list(lv.get("resistance_levels")),
        "up_targets": _num_list(lv.get("up_targets")),
        "down_targets": _num_list(lv.get("down_targets")),
    }

    scenarios = []
    for s in result.get("scenarios") or []:
        scenarios.append(
            {
                "name": s.get("name"),
                "trigger": s.get("trigger"),
                "path": s.get("path"),
            }
        )

    return {
        "id": f"{result.get('as_of')}:{primary}",
        "as_of": result.get("as_of"),
        "created_at": result.get("generated_at") or _now(),
        "updated_at": _now(),
        "primary": primary,
        "name": p.get("name"),
        "close": _round(p.get("close")),
        "day_pct": _round(p.get("day_pct")),
        "score": _round(p.get("score"), 1),
        "state": p.get("state"),
        "phase": p.get("phase"),
        "trend_score": _round(p.get("trend_score"), 1),
        "momentum_score": _round(p.get("momentum_score"), 1),
        "vol_score": _round(p.get("vol_score"), 1),
        "drawdown_score": _round(p.get("drawdown_score"), 1),
        "atr_pct": _round(p.get("atr_pct")),
        "risk_budget": {
            "low": rb.get("low"),
            "high": rb.get("high"),
        },
        "analogs": analogs,
        "levels": levels,
        "scenarios": scenarios,
        "market": {
            "env": market.get("env"),
            "up_ratio": _f(breadth.get("up_ratio")),
            "amount_yi": _round(amount.get("total_yi"), 0),
            "fund_total_yi": _round(fund.get("total_yi")),
            "emotion_phase": emotion.get("phase"),
            "limit_up": breadth.get("limit_up"),
            "limit_down": breadth.get("limit_down"),
        },
        "etf": {
            "label": etf.get("label"),
            "vs_market": etf.get("vs_market"),
            "net_y20": _round(etf.get("net_y20")),
            "score": _round(etf.get("score"), 3),
            "flow_source": etf.get("flow_source"),
            "proxy_ok": etf.get("proxy_ok"),
        }
        if etf
        else {},
        "liquidity": {
            "label": lq.get("label"),
            "score": _round(lq.get("score"), 1),
            "m1_m2": _round(lq.get("m1_m2")),
            "credit_yoy": _round(lq.get("credit_yoy")),
            "shibor3m": _round(lq.get("shibor3m")),
            "bond10": _round(lq.get("bond10")),
            "as_of": lq.get("as_of"),
        }
        if lq
        else {},
        "outcomes": {},
    }


def record_outlook(result: dict, path: str | None = None) -> dict:
    """记录一次 outlook; 同一 (as_of, primary) 覆盖更新但保留已回填结果。"""
    rec = _extract_record(result)
    if not rec.get("as_of") or not rec.get("primary"):
        return {"ok": False, "reason": "缺少 as_of/primary"}

    data = load_journal(path)
    records = data["records"]
    existing = next((r for r in records if r.get("id") == rec["id"]), None)
    replaced = False
    if existing is not None:
        rec["outcomes"] = existing.get("outcomes") or {}
        rec["created_at"] = existing.get("created_at") or rec["created_at"]
        existing.clear()
        existing.update(rec)
        replaced = True
    else:
        records.append(rec)
    save_journal(data, path)
    return {
        "ok": True,
        "id": rec["id"],
        "replaced": replaced,
        "total": len(records),
        "path": path or JOURNAL_FILE,
    }


def _parse_trade_plan(lines) -> dict:
    """从 run_analysis 的 trade_plan 文本里抽止损/目标/方向。"""
    out = {"direction": "", "entry": None, "stop": None, "target1": None, "target2": None}
    for raw in lines or []:
        s = str(raw).strip()
        for prefix, key in (
            ("方向:", "direction"),
            ("现价:", "entry"),
            ("止损:", "stop"),
            ("目标1:", "target1"),
            ("目标2:", "target2"),
        ):
            if s.startswith(prefix):
                val = s.split(prefix, 1)[1].strip()
                if key == "direction":
                    out[key] = val
                else:
                    out[key] = _f(val)
                break
    return out


def record_stock(data: dict, market_result: dict | None = None, path: str | None = None) -> dict:
    """记录一次个股分析 (来自旧 Wyckoff 系统)。

    data 需要: symbol/name/as_of/close/precomputed/summary。
    market_result 是当前大盘 outlook, 用于记录当时的市场环境。
    """
    pre = data.get("precomputed") or {}
    symbol = data.get("symbol") or data.get("code")
    as_of = data.get("as_of")
    if not symbol or not as_of:
        return {"ok": False, "reason": "缺少 symbol/as_of"}

    trade = _parse_trade_plan(pre.get("trade_plan"))
    fusion = pre.get("fusion") or {}
    market = {}
    if market_result:
        primary = market_result.get("primary")
        p = (market_result.get("indices") or {}).get(primary) or {}
        rb = market_result.get("risk_budget") or {}
        market = {
            "state": p.get("state"),
            "score": _f(p.get("score")),
            "risk_low": rb.get("low"),
            "risk_high": rb.get("high"),
        }

    rec = {
        "id": f"{as_of}:{symbol}",
        "as_of": as_of,
        "created_at": _now(),
        "symbol": symbol,
        "name": data.get("name"),
        "close": _round(data.get("close")),
        "phase": pre.get("phase"),
        "fusion_score": _round(fusion.get("score")),
        "fusion_conf": fusion.get("confidence"),
        "direction": trade.get("direction") or "",
        "entry": trade.get("entry"),
        "stop": trade.get("stop"),
        "target1": trade.get("target1"),
        "target2": trade.get("target2"),
        "summary": data.get("summary") or [],
        "market": market,
        "confirm": bool(data.get("confirm")),
        "outcomes": {},
    }

    journal = load_journal(path or STOCK_JOURNAL_FILE)
    records = journal.get("records") or []
    existing = next((r for r in records if r.get("id") == rec["id"]), None)
    replaced = False
    if existing is not None:
        rec["outcomes"] = existing.get("outcomes") or {}
        rec["created_at"] = existing.get("created_at") or rec["created_at"]
        existing.clear()
        existing.update(rec)
        replaced = True
    else:
        records.append(rec)
    journal["records"] = records
    save_journal(journal, path or STOCK_JOURNAL_FILE)
    return {
        "ok": True,
        "id": rec["id"],
        "replaced": replaced,
        "total": len(records),
        "path": path or STOCK_JOURNAL_FILE,
    }


def compare_with_previous(result: dict, path: str | None = None) -> dict | None:
    """把当前 outlook 和同一指数上一条记录对比, 用于“今天 vs 上次”。"""
    primary = result.get("primary")
    as_of = result.get("as_of")
    if not primary or not as_of:
        return None
    data = load_journal(path)
    prevs = [
        r
        for r in (data.get("records") or [])
        if r.get("primary") == primary and r.get("as_of") and str(r.get("as_of")) < str(as_of)
    ]
    if not prevs:
        return None
    prev = sorted(prevs, key=lambda r: str(r.get("as_of")))[-1]

    p = (result.get("indices") or {}).get(primary) or {}
    cur_score = _f(p.get("score"), 0.0)
    prev_score = _f(prev.get("score"), cur_score)
    cur_close = _f(p.get("close"), 0.0)
    prev_close = _f(prev.get("close"), cur_close)
    cur_state = p.get("state") or "--"
    prev_state = prev.get("state") or "--"
    cur_phase = p.get("phase") or "--"
    prev_phase = prev.get("phase") or "--"

    cur_rb = result.get("risk_budget") or {}
    prev_rb = prev.get("risk_budget") or {}
    cur_etf = (result.get("etf") or {}).get("label") or "无数据"
    prev_etf = (prev.get("etf") or {}).get("label") or "无数据"
    cur_lq = (result.get("liquidity") or {}).get("label") or "无数据"
    prev_lq = (prev.get("liquidity") or {}).get("label") or "无数据"
    cur_up = _f(((result.get("market") or {}).get("breadth") or {}).get("up_ratio"))
    prev_up = _f((prev.get("market") or {}).get("up_ratio"))
    cur_p20 = _f(((result.get("analogs") or {}).get("horizons") or {}).get("20", {}).get("up_prob"))
    prev_p20 = _f((prev.get("analogs") or {}).get("20", {}).get("up_prob"))

    return {
        "previous_as_of": prev.get("as_of"),
        "score_delta": round(cur_score - prev_score, 1),
        "state_from": prev_state,
        "state_to": cur_state,
        "state_changed": prev_state != cur_state,
        "phase_from": prev_phase,
        "phase_to": cur_phase,
        "phase_changed": prev_phase != cur_phase,
        "close_delta_pct": round((cur_close / prev_close - 1) * 100, 2) if prev_close else None,
        "risk_from": {
            "low": prev_rb.get("low"),
            "high": prev_rb.get("high"),
        },
        "risk_to": {
            "low": cur_rb.get("low"),
            "high": cur_rb.get("high"),
        },
        "etf_from": prev_etf,
        "etf_to": cur_etf,
        "liquidity_from": prev_lq,
        "liquidity_to": cur_lq,
        "up_ratio_from": prev_up,
        "up_ratio_to": cur_up,
        "analog20_from": prev_p20,
        "analog20_to": cur_p20,
    }


# ─────────────────────────────────────────────────────────────
# 回填
# ─────────────────────────────────────────────────────────────


def _direction(p: float | None) -> str:
    """把上涨概率翻译成粗略方向 (仅用于命中率统计)。"""
    if p is None:
        return "中性"
    if p >= 0.55:
        return "偏多"
    if p <= 0.45:
        return "偏空"
    return "中性"


def _scenario_result(bull: bool | None, bear: bool | None) -> str:
    if bull and bear:
        return "both"
    if bull:
        return "bull"
    if bear:
        return "bear"
    return "base"


def _backfill_one(rec: dict, df: pd.DataFrame, horizons) -> bool:
    """回填单条记录; 返回是否至少更新了一个周期。"""
    levels = rec.get("levels") or {}
    days = df["day"].tolist()
    try:
        idx = days.index(rec.get("as_of"))
    except ValueError:
        rec["backfill_error"] = f"行情中找不到 {rec.get('as_of')}"
        return False

    entry = _f(df["close"].iloc[idx])
    if entry is None:
        rec["backfill_error"] = "入场收盘价无效"
        return False

    updated = False
    outcomes = rec.setdefault("outcomes", {})
    for h in horizons:
        key = str(h)
        if idx + h >= len(df):
            outcomes[key] = {"pending": True}
            continue
        window = df.iloc[idx + 1 : idx + 1 + h]
        if len(window) == 0:
            outcomes[key] = {"pending": True}
            continue
        close_h = _f(df["close"].iloc[idx + h])
        if close_h is None:
            outcomes[key] = {"pending": True}
            continue
        ret = (close_h / entry - 1) * 100
        hi = _f(window["high"].max())
        lo = _f(window["low"].min())
        near_up = _f(levels.get("near_up"))
        near_down = _f(levels.get("near_down"))
        tr_top = _f(levels.get("tr_top"))
        tr_bottom = _f(levels.get("tr_bottom"))
        hit_near_up = (hi >= near_up) if hi is not None and near_up is not None else None
        hit_near_down = (lo <= near_down) if lo is not None and near_down is not None else None
        hit_tr_top = (hi >= tr_top) if hi is not None and tr_top is not None else None
        hit_tr_bottom = (lo <= tr_bottom) if lo is not None and tr_bottom is not None else None

        p = _f((rec.get("analogs") or {}).get(key, {}).get("up_prob"))
        direction = _direction(p)
        up = ret > 0
        direction_hit = None
        if direction == "偏多":
            direction_hit = up
        elif direction == "偏空":
            direction_hit = not up
        brier = (p - (1.0 if up else 0.0)) ** 2 if p is not None else None

        outcomes[key] = {
            "ret": round(ret, 2),
            "up": bool(up),
            "close": round(close_h, 2),
            "high": round(hi, 2) if hi is not None else None,
            "low": round(lo, 2) if lo is not None else None,
            "max_drawdown": round((lo / entry - 1) * 100, 2) if lo is not None else None,
            "hit_near_up": hit_near_up,
            "hit_near_down": hit_near_down,
            "hit_tr_top": hit_tr_top,
            "hit_tr_bottom": hit_tr_bottom,
            "scenario": _scenario_result(hit_tr_top, hit_tr_bottom),
            "analog_prob": p,
            "direction": direction,
            "direction_hit": direction_hit,
            "brier": round(brier, 4) if brier is not None else None,
        }
        updated = True
    rec["last_backfill_at"] = _now()
    rec.pop("backfill_error", None)
    return updated


def backfill(path: str | None = None, horizons=HORIZONS, datalen: int = 1200) -> dict:
    """回填所有已到期记录; 每个指数只拉一次日线。"""
    data = load_journal(path)
    records = data.get("records") or []
    if not records:
        return {"ok": False, "reason": "日志为空", "total": 0}

    tasks = [r for r in records if r.get("primary") and r.get("as_of")]
    primaries = sorted({r["primary"] for r in tasks})
    frames: dict[str, pd.DataFrame] = {}
    errors = []
    for primary in primaries:
        try:
            df = fetch_kline(primary, datalen=datalen, scale=240)
            if df is None or len(df) == 0:
                errors.append(f"{primary}: 行情为空")
                continue
            df = df.copy()
            df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
            for col in ("open", "high", "low", "close", "volume"):
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            frames[primary] = df.sort_values("day").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            log_exc(f"回填行情失败 {primary}", e)
            errors.append(f"{primary}: {type(e).__name__}")

    updated = 0
    for rec in tasks:
        df = frames.get(rec["primary"])
        if df is None:
            rec["backfill_error"] = "行情不可用"
            continue
        if _backfill_one(rec, df, horizons):
            updated += 1

    save_journal(data, path)
    matured = {
        str(h): sum(
            1
            for r in tasks
            if isinstance(r.get("outcomes"), dict)
            and (r["outcomes"].get(str(h)) or {}).get("ret") is not None
        )
        for h in horizons
    }
    pending = {
        str(h): sum(1 for r in tasks if (r.get("outcomes") or {}).get(str(h), {}).get("pending"))
        for h in horizons
    }
    return {
        "ok": True,
        "total": len(tasks),
        "updated": updated,
        "matured": matured,
        "pending": pending,
        "errors": errors,
        "path": path or JOURNAL_FILE,
    }


def _stock_direction(rec: dict) -> str:
    d = str(rec.get("direction") or "")
    if "多" in d or "买" in d:
        return "偏多"
    if "空" in d or "减" in d or "卖" in d:
        return "偏空"
    score = _f(rec.get("fusion_score"))
    if score is not None:
        if score > 0:
            return "偏多"
        if score < 0:
            return "偏空"
    return "中性"


def _stock_backfill_one(rec: dict, df: pd.DataFrame, horizons) -> bool:
    days = df["day"].tolist()
    try:
        idx = days.index(rec.get("as_of"))
    except ValueError:
        rec["backfill_error"] = f"行情中找不到 {rec.get('as_of')}"
        return False
    entry = _f(df["close"].iloc[idx])
    if entry is None:
        rec["backfill_error"] = "入场收盘价无效"
        return False

    target = _f(rec.get("target1"))
    stop = _f(rec.get("stop"))
    direction = _stock_direction(rec)
    updated = False
    outcomes = rec.setdefault("outcomes", {})

    for h in horizons:
        key = str(h)
        if idx + h >= len(df):
            outcomes[key] = {"pending": True}
            continue
        window = df.iloc[idx + 1 : idx + 1 + h]
        if len(window) == 0:
            outcomes[key] = {"pending": True}
            continue
        close_h = _f(df["close"].iloc[idx + h])
        if close_h is None:
            outcomes[key] = {"pending": True}
            continue
        ret = (close_h / entry - 1) * 100
        hi = _f(window["high"].max())
        lo = _f(window["low"].min())
        target_hit = (hi >= target) if hi is not None and target is not None else None
        stop_hit = (lo <= stop) if lo is not None and stop is not None else None
        first = "none"
        for _, bar in window.iterrows():
            hs = (
                stop is not None
                and _f(bar.get("low")) is not None
                and float(bar.get("low")) <= stop
            )
            ht = (
                target is not None
                and _f(bar.get("high")) is not None
                and float(bar.get("high")) >= target
            )
            if hs and ht:
                first = "both"
                break
            if hs:
                first = "stop"
                break
            if ht:
                first = "target"
                break
        up = ret > 0
        direction_hit = None
        if direction == "偏多":
            direction_hit = up
        elif direction == "偏空":
            direction_hit = not up
        outcomes[key] = {
            "ret": round(ret, 2),
            "up": bool(up),
            "close": round(close_h, 2),
            "high": round(hi, 2) if hi is not None else None,
            "low": round(lo, 2) if lo is not None else None,
            "max_drawdown": round((lo / entry - 1) * 100, 2) if lo is not None else None,
            "max_runup": round((hi / entry - 1) * 100, 2) if hi is not None else None,
            "target_hit": target_hit,
            "stop_hit": stop_hit,
            "first_event": first,
            "direction": direction,
            "direction_hit": direction_hit,
        }
        updated = True
    rec["last_backfill_at"] = _now()
    rec.pop("backfill_error", None)
    return updated


def backfill_stocks(path: str | None = None, horizons=HORIZONS, datalen: int = 800) -> dict:
    """回填个股信号: 5/20/60 日收益、回撤、目标/止损命中。"""
    path = path or STOCK_JOURNAL_FILE
    journal = load_journal(path)
    records = [r for r in (journal.get("records") or []) if r.get("symbol") and r.get("as_of")]
    if not records:
        return {"ok": False, "reason": "个股日志为空", "total": 0}

    symbols = sorted({r["symbol"] for r in records})
    frames: dict[str, pd.DataFrame] = {}
    errors = []
    for symbol in symbols:
        try:
            df = fetch_kline(symbol, datalen=datalen, scale=240)
            if df is None or len(df) == 0:
                errors.append(f"{symbol}: 行情为空")
                continue
            df = df.copy()
            df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d")
            for col in ("open", "high", "low", "close", "volume"):
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            frames[symbol] = df.sort_values("day").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            log_exc(f"个股回填行情失败 {symbol}", e)
            errors.append(f"{symbol}: {type(e).__name__}")

    updated = 0
    for rec in records:
        df = frames.get(rec["symbol"])
        if df is None:
            rec["backfill_error"] = "行情不可用"
            continue
        if _stock_backfill_one(rec, df, horizons):
            updated += 1

    journal["records"] = journal.get("records") or []
    save_journal(journal, path)
    matured = {
        str(h): sum(
            1 for r in records if (r.get("outcomes") or {}).get(str(h), {}).get("ret") is not None
        )
        for h in horizons
    }
    pending = {
        str(h): sum(1 for r in records if (r.get("outcomes") or {}).get(str(h), {}).get("pending"))
        for h in horizons
    }
    return {
        "ok": True,
        "total": len(records),
        "updated": updated,
        "matured": matured,
        "pending": pending,
        "errors": errors,
        "path": path,
    }


# ─────────────────────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────────────────────


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _horizon_stats(rows: list[dict], h: int) -> dict:
    outcomes = []
    for r in rows:
        o = (r.get("outcomes") or {}).get(str(h))
        if o and o.get("ret") is not None:
            outcomes.append((r, o))
    if not outcomes:
        return {"n": 0}

    rets = [o["ret"] for _, o in outcomes]
    ups = [1 if o.get("up") else 0 for _, o in outcomes]
    base_rate = sum(ups) / len(ups)
    ps = [o.get("analog_prob") for _, o in outcomes if o.get("analog_prob") is not None]
    briers = [o["brier"] for _, o in outcomes if o.get("brier") is not None]
    base_briers = [(base_rate - u) ** 2 for u in ups]
    scored = [o for _, o in outcomes if o.get("direction_hit") is not None]
    hits = [1 if o["direction_hit"] else 0 for o in scored]

    cal = []
    for lo, hi in _CAL_BUCKETS:
        bucket = [
            (r, o)
            for r, o in outcomes
            if o.get("analog_prob") is not None and lo <= o["analog_prob"] < hi
        ]
        if not bucket:
            continue
        cal.append(
            {
                "range": f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%",
                "n": len(bucket),
                "avg_p": round(_mean([o["analog_prob"] for _, o in bucket]), 3),
                "actual_up": round(_mean([1 if o.get("up") else 0 for _, o in bucket]), 3),
            }
        )

    return {
        "n": len(outcomes),
        "up_rate": round(base_rate, 3),
        "avg_ret": round(_mean(rets), 2),
        "median_ret": round(_median(rets), 2),
        "avg_drawdown": round(_mean([o.get("max_drawdown") for _, o in outcomes]), 2),
        "avg_prob": round(_mean(ps), 3) if ps else None,
        "brier": round(_mean(briers), 4) if briers else None,
        "base_brier": round(_mean(base_briers), 4),
        "direction_n": len(scored),
        "direction_hit": round(sum(hits) / len(hits), 3) if hits else None,
        "calibration": cal,
    }


def _group_stats(rows: list[dict], h: int, key_fn) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        o = (r.get("outcomes") or {}).get(str(h))
        if not o or o.get("ret") is None:
            continue
        key = key_fn(r) or "未标注"
        groups.setdefault(str(key), []).append(o)
    out = []
    for key, items in groups.items():
        rets = [o["ret"] for o in items]
        ups = [1 if o.get("up") else 0 for o in items]
        out.append(
            {
                "label": key,
                "n": len(items),
                "up_rate": round(sum(ups) / len(ups), 3),
                "avg_ret": round(_mean(rets), 2),
                "median_ret": round(_median(rets), 2),
            }
        )
    out.sort(key=lambda x: -x["n"])
    return out


def compute_stats(path: str | None = None, horizon: int = 20) -> dict:
    """统计已回填记录: 命中率/Brier/校准/分组表现。"""
    data = load_journal(path)
    records = [r for r in (data.get("records") or []) if r.get("outcomes")]
    if not records:
        return {"ok": False, "reason": "暂无已回填记录", "total_records": 0}

    matured = {}
    for h in HORIZONS:
        matured[str(h)] = sum(
            1 for r in records if (r.get("outcomes") or {}).get(str(h), {}).get("ret") is not None
        )
    target = str(horizon)
    if matured.get(target, 0) == 0:
        return {
            "ok": False,
            "reason": f"{horizon}日暂无已回填记录",
            "total_records": len(records),
            "matured": matured,
        }

    horizons = {str(h): _horizon_stats(records, h) for h in HORIZONS}
    return {
        "ok": True,
        "total_records": len(records),
        "matured": matured,
        "horizon": horizon,
        "horizons": horizons,
        "by_primary": _group_stats(
            records,
            horizon,
            lambda r: f"{r.get('name') or '--'}({r.get('primary') or '--'})",
        ),
        "by_state": _group_stats(records, horizon, lambda r: r.get("state")),
        "by_score": _group_stats(
            records,
            horizon,
            lambda r: (
                "0-20"
                if (_f(r.get("score"), 50) < 20)
                else "20-30"
                if (_f(r.get("score"), 50) < 30)
                else "30-50"
                if (_f(r.get("score"), 50) < 50)
                else "50-70"
                if (_f(r.get("score"), 50) < 70)
                else "70-100"
            ),
        ),
        "by_etf": _group_stats(
            records,
            horizon,
            lambda r: (r.get("etf") or {}).get("label") or "无ETF数据",
        ),
        "by_liquidity": _group_stats(
            records,
            horizon,
            lambda r: (r.get("liquidity") or {}).get("label") or "无流动性数据",
        ),
        "scenarios": {
            "base": sum(
                1
                for r in records
                if (r.get("outcomes") or {}).get(target, {}).get("scenario") == "base"
            ),
            "bull": sum(
                1
                for r in records
                if (r.get("outcomes") or {}).get(target, {}).get("scenario") == "bull"
            ),
            "bear": sum(
                1
                for r in records
                if (r.get("outcomes") or {}).get(target, {}).get("scenario") == "bear"
            ),
            "both": sum(
                1
                for r in records
                if (r.get("outcomes") or {}).get(target, {}).get("scenario") == "both"
            ),
        },
        "note": (
            "方向命中按历史类比概率定义: 20日概率>=55%看多、<=45%看空, "
            "中间不计分; Brier越低越好, 应和基线Brier比较。"
        ),
    }


def _fmt(x, digits=2) -> str:
    v = _f(x)
    return "--" if v is None else f"{v:.{digits}f}"


def _pct(x) -> str:
    v = _f(x)
    return "--" if v is None else f"{v * 100:.0f}%"


def render_stats(stats: dict) -> str:
    """把 compute_stats 结果渲染成 Markdown。"""
    if not stats.get("ok"):
        return f"# A股预测回填统计\n\n{stats.get('reason', '暂无数据')}"

    lines = [
        "# A股预测回填统计",
        "",
        f"- 记录总数: **{stats.get('total_records')}**",
        f"- 已回填: 5日 {stats['matured'].get('5', 0)} / "
        f"20日 {stats['matured'].get('20', 0)} / "
        f"60日 {stats['matured'].get('60', 0)}",
        "",
        "## 分周期表现",
        "",
        "| 周期 | 样本 | 上涨率 | 平均收益 | 中位数 | 平均最大回撤 | 方向命中 | Brier | 基线Brier |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for h in HORIZONS:
        s = (stats.get("horizons") or {}).get(str(h)) or {}
        if not s.get("n"):
            lines.append(f"| {h}日 | 0 | -- | -- | -- | -- | -- | -- | -- |")
            continue
        lines.append(
            f"| {h}日 | {s.get('n')} | {_pct(s.get('up_rate'))} | "
            f"{_fmt(s.get('avg_ret'))}% | {_fmt(s.get('median_ret'))}% | "
            f"{_fmt(s.get('avg_drawdown'))}% | {_pct(s.get('direction_hit'))} | "
            f"{_fmt(s.get('brier'), 4)} | {_fmt(s.get('base_brier'), 4)} |"
        )

    target = str(stats.get("horizon") or 20)
    hs = (stats.get("horizons") or {}).get(target) or {}
    if hs.get("calibration"):
        lines += [
            "",
            f"## 概率校准（{target}日）",
            "",
            "| 预测上涨概率 | 样本 | 平均预测 | 实际上涨率 |",
            "|---|---:|---:|---:|",
        ]
        for row in hs["calibration"]:
            lines.append(
                f"| {row.get('range')} | {row.get('n')} | "
                f"{_pct(row.get('avg_p'))} | {_pct(row.get('actual_up'))} |"
            )

    def _table(title, rows):
        if not rows:
            return
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| 分组 | 样本 | 上涨率 | 平均收益 | 中位数 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in rows:
            lines.append(
                f"| {row.get('label')} | {row.get('n')} | "
                f"{_pct(row.get('up_rate'))} | {_fmt(row.get('avg_ret'))}% | "
                f"{_fmt(row.get('median_ret'))}% |"
            )

    _table(f"按指数（{target}日）", stats.get("by_primary"))
    _table(f"按状态分（{target}日）", stats.get("by_state"))
    _table(f"按状态分档（{target}日）", stats.get("by_score"))
    _table(f"按ETF确认（{target}日）", stats.get("by_etf"))
    _table(f"按流动性（{target}日）", stats.get("by_liquidity"))

    sc = stats.get("scenarios") or {}
    if any(sc.values()):
        lines += [
            "",
            f"## 情景命中（{target}日）",
            "",
            "| 结果 | 次数 |",
            "|---|---:|",
            f"| 区间内 | {sc.get('base', 0)} |",
            f"| 向上突破 | {sc.get('bull', 0)} |",
            f"| 向下破位 | {sc.get('bear', 0)} |",
            f"| 先上后下/都摸到 | {sc.get('both', 0)} |",
        ]

    lines += [
        "",
        "> 方向命中按历史类比概率定义：概率≥55%看多、≤45%看空，中间不计分。",
        "> Brier 越低越好，但要和基线 Brier 比较；样本少时波动很大，别急着下结论。",
    ]
    return "\n".join(lines)


def _stock_horizon_stats(records: list[dict], h: int) -> dict:
    outcomes = []
    for r in records:
        o = (r.get("outcomes") or {}).get(str(h))
        if o and o.get("ret") is not None:
            outcomes.append((r, o))
    if not outcomes:
        return {"n": 0}
    rets = [o["ret"] for _, o in outcomes]
    ups = [1 if o.get("up") else 0 for _, o in outcomes]
    scored = [o for _, o in outcomes if o.get("direction_hit") is not None]
    hits = [1 if o["direction_hit"] else 0 for o in scored]
    target_known = [o for _, o in outcomes if o.get("target_hit") is not None]
    stop_known = [o for _, o in outcomes if o.get("stop_hit") is not None]
    events = {"target": 0, "stop": 0, "both": 0, "none": 0}
    for _, o in outcomes:
        ev = o.get("first_event") or "none"
        if ev in events:
            events[ev] += 1
    return {
        "n": len(outcomes),
        "up_rate": round(sum(ups) / len(ups), 3),
        "avg_ret": round(_mean(rets), 2),
        "median_ret": round(_median(rets), 2),
        "avg_drawdown": round(_mean([o.get("max_drawdown") for _, o in outcomes]), 2),
        "avg_runup": round(_mean([o.get("max_runup") for _, o in outcomes]), 2),
        "direction_n": len(scored),
        "direction_hit": round(sum(hits) / len(hits), 3) if hits else None,
        "target_n": len(target_known),
        "target_hit": round(sum(1 for o in target_known if o["target_hit"]) / len(target_known), 3)
        if target_known
        else None,
        "stop_n": len(stop_known),
        "stop_hit": round(sum(1 for o in stop_known if o["stop_hit"]) / len(stop_known), 3)
        if stop_known
        else None,
        "events": events,
    }


def compute_stock_stats(path: str | None = None, horizon: int = 20) -> dict:
    """统计个股信号回填结果。"""
    path = path or STOCK_JOURNAL_FILE
    journal = load_journal(path)
    records = [r for r in (journal.get("records") or []) if r.get("outcomes")]
    if not records:
        return {"ok": False, "reason": "暂无已回填个股记录", "total_records": 0}

    matured = {
        str(h): sum(
            1 for r in records if (r.get("outcomes") or {}).get(str(h), {}).get("ret") is not None
        )
        for h in HORIZONS
    }
    target = str(horizon)
    if matured.get(target, 0) == 0:
        return {
            "ok": False,
            "reason": f"{horizon}日暂无已回填个股记录",
            "total_records": len(records),
            "matured": matured,
        }

    return {
        "ok": True,
        "total_records": len(records),
        "matured": matured,
        "horizon": horizon,
        "horizons": {str(h): _stock_horizon_stats(records, h) for h in HORIZONS},
        "by_phase": _group_stats(records, horizon, lambda r: r.get("phase")),
        "by_conf": _group_stats(records, horizon, lambda r: r.get("fusion_conf")),
        "by_market": _group_stats(records, horizon, lambda r: (r.get("market") or {}).get("state")),
        "by_direction": _group_stats(
            records, horizon, lambda r: r.get("direction") or _stock_direction(r)
        ),
        "note": (
            "目标命中=回填期内最高价摸到目标1; 止损命中=最低价摸到止损; "
            "首次触发按日线顺序判断。样本少时只能看方向，不要当精确胜率。"
        ),
    }


def render_stock_stats(stats: dict) -> str:
    if not stats.get("ok"):
        return f"# 个股预测回填统计\n\n{stats.get('reason', '暂无数据')}"
    lines = [
        "# 个股预测回填统计",
        "",
        f"- 记录总数: **{stats.get('total_records')}**",
        f"- 已回填: 5日 {stats['matured'].get('5', 0)} / "
        f"20日 {stats['matured'].get('20', 0)} / "
        f"60日 {stats['matured'].get('60', 0)}",
        "",
        "## 分周期表现",
        "",
        "| 周期 | 样本 | 上涨率 | 平均收益 | 中位数 | 平均最大回撤 | 方向命中 | 目标命中 | 止损命中 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for h in HORIZONS:
        s = (stats.get("horizons") or {}).get(str(h)) or {}
        if not s.get("n"):
            lines.append(f"| {h}日 | 0 | -- | -- | -- | -- | -- | -- | -- |")
            continue
        lines.append(
            f"| {h}日 | {s.get('n')} | {_pct(s.get('up_rate'))} | "
            f"{_fmt(s.get('avg_ret'))}% | {_fmt(s.get('median_ret'))}% | "
            f"{_fmt(s.get('avg_drawdown'))}% | {_pct(s.get('direction_hit'))} | "
            f"{_pct(s.get('target_hit'))} | {_pct(s.get('stop_hit'))} |"
        )

    target = str(stats.get("horizon") or 20)
    hs = (stats.get("horizons") or {}).get(target) or {}
    events = hs.get("events") or {}
    if events:
        lines += [
            "",
            f"## 首次触发（{target}日）",
            "",
            "| 结果 | 次数 |",
            "|---|---:|",
            f"| 先到目标 | {events.get('target', 0)} |",
            f"| 先到止损 | {events.get('stop', 0)} |",
            f"| 同一天都摸到 | {events.get('both', 0)} |",
            f"| 都没到 | {events.get('none', 0)} |",
        ]

    def _table(title, rows):
        if not rows:
            return
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| 分组 | 样本 | 上涨率 | 平均收益 | 中位数 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in rows:
            lines.append(
                f"| {row.get('label')} | {row.get('n')} | "
                f"{_pct(row.get('up_rate'))} | {_fmt(row.get('avg_ret'))}% | "
                f"{_fmt(row.get('median_ret'))}% |"
            )

    _table(f"按阶段（{target}日）", stats.get("by_phase"))
    _table(f"按综合置信（{target}日）", stats.get("by_conf"))
    _table(f"按大盘环境（{target}日）", stats.get("by_market"))
    _table(f"按方向（{target}日）", stats.get("by_direction"))

    lines += ["", f"> {stats.get('note', '')}"]
    return "\n".join(lines)


def render_backfill_summary(summary: dict, title: str = "A股预测回填") -> str:
    """回填结果的一行摘要。"""
    if not summary.get("ok"):
        return f"回填未执行: {summary.get('reason', '--')}"
    matured = summary.get("matured") or {}
    pending = summary.get("pending") or {}
    lines = [
        f"# {title}",
        "",
        f"- 总记录: {summary.get('total', 0)}",
        f"- 本次更新: {summary.get('updated', 0)}",
        f"- 已到期: 5日 {matured.get('5', 0)} / 20日 {matured.get('20', 0)} / "
        f"60日 {matured.get('60', 0)}",
        f"- 待到期: 5日 {pending.get('5', 0)} / 20日 {pending.get('20', 0)} / "
        f"60日 {pending.get('60', 0)}",
    ]
    errors = summary.get("errors") or []
    if errors:
        lines.append(f"- 行情错误: {', '.join(errors[:5])}")
    lines += ["", f"- 日志文件: {summary.get('path')}"]
    return "\n".join(lines)
