"""A股指数估值确认层 (PE / 股息率 / ERP)。

数据源与局限 (2026-11 实测):
- 中证指数官网 `stock_zh_index_value_csindex` 可用, 但只给最近约 20 个交易日,
  且无 PB 列; 乐咕/百度估值源当前不可用。
- 因此历史分位靠**本地每日累积** (`wx_ashare_valuation.json`): 每次刷新把最新
  PE/股息率并入本地历史; 累计 >=250 个交易日才给分位, 之前明确显示“样本累积中”,
  不伪造历史、不拿 20 天样本冒充长期分位。

ERP = 100 / PE(TTM) - 10Y国债收益率; 需要流动性层的 10Y 数据。
估值只作中期锚, 不预测短期涨跌。
"""

from __future__ import annotations

import json
import math
import os

import pandas as pd

from ._log import log_exc
from .paths import DATA_DIR

VALUATION_FILE = os.path.join(DATA_DIR, "wx_ashare_valuation.json")
MIN_HISTORY = 250  # 给分位所需的最少本地样本 (交易日)
MAX_HISTORY = 3000  # 每个指数最多保留的本地样本

# 主指数 → 中证指数代码。创业板指 (399006) 不在中证官网估值文件里, 单独标注缺失。
VALUATION_INDICES = [
    ("sh000001", "000001", "上证指数"),
    ("sh000300", "000300", "沪深300"),
    ("sh000905", "000905", "中证500"),
    ("sh000688", "000688", "科创50"),
]


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


def _date_key(value) -> str | None:
    try:
        d = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(d) else d.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_csindex(code: str) -> pd.DataFrame | None:
    """中证指数官网估值 (最近约 20 个交易日)。

    列: date, pe (滚动/TTM), pe_static, div。
    官网文件按日期倒序, 这里统一转成升序。
    """
    import akshare as ak

    try:
        df = ak.stock_zh_index_value_csindex(symbol=code)
    except Exception as e:  # noqa: BLE001
        log_exc(f"中证估值失败 {code}", e)
        return None
    if df is None or len(df) == 0:
        return None
    rows = []
    for _, r in df.iterrows():
        date = _date_key(r.iloc[0])
        pe_static = _f(r.iloc[6])
        pe = _f(r.iloc[7]) or pe_static  # 第二列为滚动市盈率
        div = _f(r.iloc[8])
        if date and pe is not None:
            rows.append({"date": date, "pe": pe, "pe_static": pe_static, "div": div})
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _load(path: str | None = None) -> dict:
    path = path or VALUATION_FILE
    if not os.path.exists(path):
        return {"version": 1, "indices": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        log_exc("读取估值历史失败", e)
        return {"version": 1, "indices": {}}
    if not isinstance(data, dict):
        return {"version": 1, "indices": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("indices"), dict):
        data["indices"] = {}
    return data


def _save(data: dict, path: str | None = None) -> None:
    path = path or VALUATION_FILE
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _merge(history: dict, code: str, name: str, df: pd.DataFrame) -> dict:
    idx = history.setdefault(code, {"name": name, "rows": []})
    idx["name"] = name or idx.get("name") or code
    by_date = {str(r.get("date")): r for r in idx.get("rows") or [] if r.get("date")}
    for _, r in df.iterrows():
        d = str(r["date"])
        by_date[d] = {
            "date": d,
            "pe": _round(r.get("pe")),
            "pe_static": _round(r.get("pe_static")),
            "div": _round(r.get("div")),
        }
    idx["rows"] = sorted(by_date.values(), key=lambda x: x["date"])[-MAX_HISTORY:]
    return idx


def _pct(values, v) -> float | None:
    arr = [float(x) for x in values if _f(x) is not None]
    if len(arr) < MIN_HISTORY or v is None:
        return None
    return round(float(sum(1 for x in arr if x <= v)) / len(arr) * 100, 1)


def build_valuation_context(bond10: float | None = None, path: str | None = None) -> dict:
    """汇总指数估值。分位不足 MIN_HISTORY 时只给当前值, 明确标注“样本累积中”。"""
    path = path or VALUATION_FILE
    history = _load(path)
    book = history.setdefault("indices", {})
    fetched_any = False
    indices: dict[str, dict] = {}

    for code, cs_code, name in VALUATION_INDICES:
        df = _fetch_csindex(cs_code)
        if df is not None and len(df) > 0:
            _merge(book, code, name, df)
            fetched_any = True
        idx = book.get(code) or {}
        rows = idx.get("rows") or []
        if not rows:
            continue
        last = rows[-1]
        pe = _f(last.get("pe"))
        div = _f(last.get("div"))
        erp = round(100.0 / pe - bond10, 2) if pe and pe > 0 and bond10 is not None else None
        if erp is not None:
            last["erp"] = erp
            last["bond10"] = _round(bond10)
        pe_pct = _pct([r.get("pe") for r in rows], pe)
        erp_pct = _pct([r.get("erp") for r in rows], erp)
        ready = len(rows) >= MIN_HISTORY
        if not ready:
            label = "样本累积中"
        elif pe_pct is None:
            label = "分位缺失"
        elif pe_pct <= 20:
            label = "低估"
        elif pe_pct >= 80:
            label = "偏高"
        else:
            label = "合理"
        indices[code] = {
            "code": code,
            "name": idx.get("name") or name,
            "date": last.get("date"),
            "pe": _round(pe),
            "pe_static": _round(last.get("pe_static")),
            "pe_pct": pe_pct,
            "div": _round(div),
            "erp": _round(erp),
            "erp_pct": erp_pct,
            "n": len(rows),
            "ready": ready,
            "label": label,
        }

    if fetched_any:
        _save(history, path)

    dates = [v.get("date") for v in indices.values() if v.get("date")]
    n_max = max((v.get("n") or 0 for v in indices.values()), default=0)
    return {
        "ok": bool(indices),
        "as_of": max(dates) if dates else None,
        "indices": indices,
        "source": "中证指数官网 + 本地累积",
        "n_max": n_max,
        "ready": any(v.get("ready") for v in indices.values()),
        "note": (
            f"PE为中证官网滚动口径，历史分位按本地每日累积样本计算；"
            f"当前最多 {n_max}/{MIN_HISTORY} 个交易日"
            + ("（已可给分位）" if n_max >= MIN_HISTORY else "，不足 250 日前标注“样本累积中”")
            + "。PB 数据源当前不可用；估值只作中期锚，不预测短期涨跌。"
        ),
    }
