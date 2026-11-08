"""A股流动性/信用确认层 (月频 + 利率)。

A股中期方向很吃“钱松不松”。本模块只用公开宏观数据做一张“资金面温度计”:
- M1/M2 同比与剪刀差 (M1-M2)
- 社融累计同比 (信用脉冲代理)
- SHIBOR 3M
- 中国 10 年期国债收益率

输出 0-100 流动性分 + “宽松/中性/偏紧”标签。月频数据有滞后, 只作中期确认,
不用于猜短线涨跌。所有数据源 fail-soft。
"""

from __future__ import annotations

import math
import re
import time

import pandas as pd

from ._log import log_exc

_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 6 * 3600


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


def _cached(key, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        val = fn()
    except Exception as e:  # noqa: BLE001
        log_exc(f"流动性数据失败: {key}", e)
        val = None
    _CACHE[key] = (now, val)
    return val


def _month_key(value) -> str | None:
    """'2026年9月份' / '2026-09' → '2026-09'。"""
    s = str(value or "")
    m = re.search(r"(\d{4})\D+(\d{1,2})", s)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def _date_key(value) -> str | None:
    """兼容毫秒时间戳 / 日期字符串, 统一成 YYYY-MM-DD。"""
    if value is None:
        return None
    s = str(value).strip()
    try:
        # pandas 读入后时间戳常变成 float (1793923200000.0), 要兼容
        if re.fullmatch(r"\d{10,13}(?:\.0+)?", s):
            ts = float(s)
            if ts > 1e11:
                ts /= 1000.0
            return pd.to_datetime(ts, unit="s").strftime("%Y-%m-%d")
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
            return None
        d = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(d) else d.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_money_supply() -> pd.DataFrame | None:
    """M1/M2 同比 (月频)。列: month, m1_yoy, m2_yoy。"""
    import akshare as ak

    df = ak.macro_china_money_supply()
    if df is None or len(df) == 0:
        return None
    rows = []
    for _, r in df.iterrows():
        month = _month_key(r.iloc[0])
        m2 = _f(r.iloc[2])
        m1 = _f(r.iloc[5])
        if month and m1 is not None and m2 is not None:
            rows.append({"month": month, "m1_yoy": m1, "m2_yoy": m2})
    if not rows:
        return None
    out = pd.DataFrame(rows).sort_values("month").reset_index(drop=True)
    return out


def _fetch_credit() -> pd.DataFrame | None:
    """社融累计同比 (月频)。列: month, credit_yoy。"""
    import akshare as ak

    df = ak.macro_china_new_financial_credit()
    if df is None or len(df) == 0:
        return None
    rows = []
    for _, r in df.iterrows():
        month = _month_key(r.iloc[0])
        yoy = _f(r.iloc[5])
        if month and yoy is not None:
            rows.append({"month": month, "credit_yoy": yoy})
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("month").reset_index(drop=True)


def _fetch_shibor() -> pd.DataFrame | None:
    """SHIBOR 3M (日频)。列: date, shibor3m。

    主源: akshare macro_china_shibor_all;
    备源: 东财 rate_interbank (上海银行同业拆借市场 3月)。
    """
    import akshare as ak

    try:
        df = ak.macro_china_shibor_all()
        if df is not None and len(df) > 0:
            rows = []
            for _, r in df.iterrows():
                d = _date_key(r.iloc[0])
                v = _f(r.iloc[9])
                if d and v is not None:
                    rows.append({"date": d, "shibor3m": v})
            if rows:
                return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        log_exc("SHIBOR主源失败", e)

    try:
        df = ak.rate_interbank(
            market="上海银行同业拆借市场",
            symbol="Shibor人民币",
            indicator="3月",
        )
        if df is not None and len(df) > 0:
            rows = []
            for _, r in df.iterrows():
                d = _date_key(r.iloc[0])
                v = _f(r.iloc[1])
                if d and v is not None:
                    rows.append({"date": d, "shibor3m": v})
            if rows:
                return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        log_exc("SHIBOR备源失败", e)
    return None


def _fetch_bond10() -> pd.DataFrame | None:
    """中国 10 年期国债收益率 (日频)。列: date, bond10。

    主源: akshare bond_zh_us_rate;
    备源: 中债国债收益率曲线 (bond_china_yield)。
    """
    import akshare as ak

    try:
        df = ak.bond_zh_us_rate()
        if df is not None and len(df) > 0:
            rows = []
            for _, r in df.iterrows():
                d = _date_key(r.iloc[0])
                v = _f(r.iloc[3])
                if d and v is not None:
                    rows.append({"date": d, "bond10": v})
            if rows:
                return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        log_exc("10Y国债主源失败", e)

    try:
        end = pd.Timestamp.today()
        start = end - pd.Timedelta(days=400)
        df = ak.bond_china_yield(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is not None and len(df) > 0:
            rows = []
            for _, r in df.iterrows():
                if "国债" not in str(r.iloc[0]):
                    continue
                d = _date_key(r.iloc[1])
                v = _f(r.iloc[8])
                if d and v is not None:
                    rows.append({"date": d, "bond10": v})
            if rows:
                return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    except Exception as e:  # noqa: BLE001
        log_exc("10Y国债备源失败", e)
    return None


def _pct_rank(series: pd.Series, value: float) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if len(s) < 20 or value is None:
        return None
    if s.nunique() <= 1:
        return 50.0  # 常数序列没有区分度, 按中性处理
    return round(float((s <= value).mean() * 100), 1)


def build_liquidity_context() -> dict:
    """汇总流动性温度计。"""
    money = _cached("money", _fetch_money_supply)
    credit = _cached("credit", _fetch_credit)
    shibor = _cached("shibor", _fetch_shibor)
    bond = _cached("bond10", _fetch_bond10)

    out: dict = {
        "ok": False,
        "label": "数据不足",
        "score": None,
        "as_of": None,
        "rate_as_of": None,
        "m1_yoy": None,
        "m2_yoy": None,
        "m1_m2": None,
        "m1_m2_pct": None,
        "m1_pct": None,
        "credit_yoy": None,
        "credit_pct": None,
        "shibor3m": None,
        "shibor_pct": None,
        "bond10": None,
        "bond_pct": None,
        "note": "月频/利率数据有滞后, 只作中期确认。",
    }
    parts: list[tuple[float, float]] = []

    if money is not None and len(money) >= 20:
        m1 = _f(money["m1_yoy"].iloc[-1])
        m2 = _f(money["m2_yoy"].iloc[-1])
        spread = (m1 - m2) if m1 is not None and m2 is not None else None
        spread_series = money["m1_yoy"] - money["m2_yoy"]
        spread_pct = _pct_rank(spread_series, spread) if spread is not None else None
        m1_pct = _pct_rank(money["m1_yoy"], m1) if m1 is not None else None
        out.update(
            {
                "as_of": str(money["month"].iloc[-1]),
                "m1_yoy": _round(m1),
                "m2_yoy": _round(m2),
                "m1_m2": _round(spread),
                "m1_m2_pct": spread_pct,
                "m1_pct": m1_pct,
            }
        )
        if spread_pct is not None:
            parts.append((spread_pct, 0.35))
        if m1_pct is not None:
            parts.append((m1_pct, 0.20))

    if credit is not None and len(credit) >= 20:
        cy = _f(credit["credit_yoy"].iloc[-1])
        cy_pct = _pct_rank(credit["credit_yoy"], cy) if cy is not None else None
        out.update(
            {
                "credit_yoy": _round(cy),
                "credit_pct": cy_pct,
                "credit_as_of": str(credit["month"].iloc[-1]),
            }
        )
        if cy_pct is not None:
            parts.append((cy_pct, 0.20))

    if shibor is not None and len(shibor) >= 20:
        s3 = _f(shibor["shibor3m"].iloc[-1])
        s3_pct = _pct_rank(shibor["shibor3m"], s3) if s3 is not None else None
        out.update(
            {
                "shibor3m": _round(s3),
                "shibor_pct": s3_pct,
                "rate_as_of": str(shibor["date"].iloc[-1]),
            }
        )
        if s3_pct is not None:
            parts.append((100 - s3_pct, 0.15))  # 利率越低越宽松

    if bond is not None and len(bond) >= 20:
        b10 = _f(bond["bond10"].iloc[-1])
        b10_pct = _pct_rank(bond["bond10"], b10) if b10 is not None else None
        out.update(
            {
                "bond10": _round(b10),
                "bond_pct": b10_pct,
                "rate_as_of": str(bond["date"].iloc[-1]),
            }
        )
        if b10_pct is not None:
            parts.append((100 - b10_pct, 0.10))  # 利率越低越宽松

    if parts:
        total_w = sum(w for _, w in parts)
        score = sum(v * w for v, w in parts) / total_w
        out["score"] = round(score, 1)
        out["ok"] = True
        if score >= 65:
            out["label"] = "宽松"
        elif score >= 35:
            out["label"] = "中性"
        else:
            out["label"] = "偏紧"
    return out
