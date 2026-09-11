"""QLib 本地数据更新脚本。

策略：全量重写对齐日历。
- akshare 新浪 stock_zh_a_daily (qfq, 全量含上市日) 为主源, 东财 stock_zh_a_hist 兜底。
- 把每只股票的全历史行情对齐 qlib 日历 (day.txt 行号 = bin index),
  close/open/high/low/volume/vwap 全量重写, 头部 start_index 自动匹配现有 bin,
  缺交易日用 NaN 占位 (与 qlib dump_bin 格式一致: [start_idx, v0, v1, ...] float32 LE)。
- volume 单位: 新浪/东财默认手, 写入与 qlib 一致 (手); vwap = 成交额/成交量(股)。
- 同步:
  * calendars/day.txt  追加最新交易日 (akshare 交易日历, 仅 > 现有最后一天)
  * instruments/all.txt 更新被更新股票的 end_date 为最新数据日
- 不重写 factor/adjclose/amount (Alpha158 不使用, 保持 qlib 官方口径一致)。

注意: 已存在 bin 的 start_index 即该股首交易日在日历中的行号, 全量重写时用
akshare 数据首日在日历中的位置, 与旧 bin 保持一致, 从而不影响跨区间读取。

用法:
    python -m wyckoff.qlib_update --symbols sh600015 sz000001
    python -m wyckoff.qlib_update --watchlist
    python -m wyckoff.qlib_update --qlib-data <dir> --watchlist
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FIELD_COLS = ("close", "open", "high", "low", "volume", "vwap")

EM_COLMAP = {
    "日期": "day", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}


def _qlib_data_dir() -> str:
    from .qlib_adapter import QLIB_DATA_DIR

    return QLIB_DATA_DIR


# ── 日历 ──────────────────────────────────────────────────────────────

def load_calendar(provider_uri: str) -> pd.DatetimeIndex:
    cal_file = os.path.join(provider_uri, "calendars", "day.txt")
    if not os.path.isfile(cal_file):
        raise FileNotFoundError(f"qlib 日历不存在: {cal_file}")
    with open(cal_file, encoding="utf-8") as f:
        dates = [line.strip() for line in f if line.strip()]
    if not dates:
        raise ValueError(f"qlib 日历为空: {cal_file}")
    return pd.DatetimeIndex(sorted(set(pd.to_datetime(dates))))


def save_calendar(provider_uri: str, cal: pd.DatetimeIndex) -> None:
    cal_file = os.path.join(provider_uri, "calendars", "day.txt")
    os.makedirs(os.path.dirname(cal_file), exist_ok=True)
    with open(cal_file, "w", encoding="utf-8") as f:
        f.write("\n".join(d.strftime("%Y-%m-%d") for d in cal))
        f.write("\n")


def extend_calendar(provider_uri: str, cal: pd.DatetimeIndex | None = None) -> pd.DatetimeIndex:
    """用 akshare 交易日历把 day.txt 扩展到最新（只追加, 不动既有位置）。

    仅取 现有最后一天 < d <= 今天 的交易日, 避免写入未来交易日。
    """
    import akshare as ak

    cal = load_calendar(provider_uri) if cal is None else cal
    tt = ak.tool_trade_date_hist_sina()
    if tt is None or tt.empty:
        logger.warning("akshare 交易日历为空, 日历不扩展")
        return cal
    trade_days = pd.DatetimeIndex(sorted(set(pd.to_datetime(tt["trade_date"]))))
    today = pd.Timestamp.today().normalize()
    last = cal.max()
    new_days = trade_days[(trade_days > last) & (trade_days <= today)]
    if len(new_days):
        cal = pd.DatetimeIndex(sorted(set(cal.union(new_days))))
        save_calendar(provider_uri, cal)
        logger.info(f"日历扩展 {len(new_days)} 天: {last.date()} -> {cal.max().date()}")
    return cal


# ── 行情 ──────────────────────────────────────────────────────────────

def fetch_ak_ohlcv(symbol: str) -> pd.DataFrame:
    """akshare 全历史 qfq 日线, 返回列:
    day/open/high/low/close/volume(手)/amount(元)/vwap。
    """
    import akshare as ak

    code = symbol[-6:]
    is_index = symbol.startswith(("sh000", "sz399"))
    df = None
    # 主源: 新浪 stock_zh_a_daily (全量, 含上市日; volume 单位=股; 不适用指数)
    if not is_index:
        try:
            raw = ak.stock_zh_a_daily(
                symbol=symbol, start_date="19900101", end_date="21000101", adjust="qfq")
            if raw is not None and not raw.empty:
                keep = ["date", "open", "high", "low", "close", "volume", "amount"]
                df = raw[[c for c in keep if c in raw.columns]].rename(columns={"date": "day"}).copy()
                if "volume" in df.columns:
                    df["volume"] = df["volume"] / 100.0  # 股 -> 手
        except Exception as e:
            logger.warning(f"akshare 新浪源失败 {symbol}: {e}")
    # 兜底/指数: 东财 stock_zh_a_hist (volume 单位=手)
    if df is None or df.empty:
        try:
            raw = ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date="19900101",
                end_date="21000101", adjust="qfq")
            if raw is not None and not raw.empty:
                df = raw.rename(columns=EM_COLMAP)
                df = df[[c for c in EM_COLMAP.values() if c in df.columns]].copy()
        except Exception as e:
            logger.warning(f"akshare 东财源失败 {symbol}: {e}")
    # 指数专用: 新浪 stock_zh_index_daily (全量, 无复权概念; volume 单位=手)
    if (df is None or df.empty) and is_index:
        try:
            raw = ak.stock_zh_index_daily(symbol=symbol)
            if raw is not None and not raw.empty:
                keep = ["date", "open", "high", "low", "close", "volume"]
                df = raw[[c for c in keep if c in raw.columns]].rename(columns={"date": "day"}).copy()
                df["amount"] = np.nan
        except Exception as e:
            logger.warning(f"akshare 指数源失败 {symbol}: {e}")
    if df is None or df.empty:
        raise RuntimeError(f"akshare 未返回 {symbol} 数据")

    df["day"] = pd.to_datetime(df["day"])
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("day").reset_index(drop=True)
    df = df[df["close"] > 0]
    # 同日去重 (akshare 偶发重复行)
    df = df.drop_duplicates(subset=["day"], keep="last").reset_index(drop=True)
    # vwap = 成交额(元) / 成交量(股); 无成交额时退化为 close
    df["vwap"] = np.nan
    has_vol = df["volume"].abs() > 0
    if "amount" in df.columns:
        df.loc[has_vol, "vwap"] = df.loc[has_vol, "amount"] / (df.loc[has_vol, "volume"] * 100.0)
    df["vwap"] = df["vwap"].where(df["vwap"].notna() & has_vol, df["close"])
    return df


# ── 写 bin ────────────────────────────────────────────────────────────

def _day_indices(cal: pd.DatetimeIndex, days: pd.Series) -> tuple[np.ndarray, int]:
    """返回 (日历内的位置数组, 最小位置); 位置=行号即 qlib index。"""
    pos = {}
    for i, d in enumerate(cal):
        pos[d] = i
    idx = [pos[d] for d in days if d in pos]
    if not idx:
        return np.array([], dtype=np.int64), -1
    arr = np.array(idx, dtype=np.int64)
    return arr, int(arr.min())


def write_field(field_file: str, start: int, values: np.ndarray) -> None:
    """写单字段 bin: [start_idx(float32), v0..vn] float32 LE。"""
    os.makedirs(os.path.dirname(field_file), exist_ok=True)
    header = np.array([np.float32(start)])
    np.concatenate([header, values.astype("<f")]).tofile(field_file)


def rewrite_bins(provider_uri: str, symbol: str, df: pd.DataFrame, cal: pd.DatetimeIndex) -> int:
    sym = symbol.lower()
    feat_dir = os.path.join(provider_uri, "features", sym)
    day_pos, start = _day_indices(cal, df["day"])
    if not len(day_pos):
        logger.warning(f"{symbol} 日期均不在日历中, 跳过")
        return 0
    n = int(day_pos.max()) - start + 1
    rel = day_pos - start

    for col in FIELD_COLS:
        values = np.full(n, np.nan, dtype=np.float64)
        v = df[col].to_numpy(dtype=np.float64)
        if len(v) != len(rel):
            values.fill(np.nan)
        else:
            values[rel] = v
        write_field(os.path.join(feat_dir, f"{col}.day.bin"), start, values)
    return len(day_pos)


# ── instruments ───────────────────────────────────────────────────────

def _instrument_file(provider_uri: str) -> str:
    return os.path.join(provider_uri, "instruments", "all.txt")


def update_instrument_end(provider_uri: str, symbol: str, end_date: str) -> None:
    """把 all.txt 中该股票的 end_date 更新为最新交易日 (若更晚)。"""
    inst_file = _instrument_file(provider_uri)
    if not os.path.isfile(inst_file):
        return
    lines = open(inst_file, encoding="utf-8").read().splitlines()
    hit = False
    for i, line in enumerate(lines):
        parts = line.split("\t")
        if parts and parts[0].upper() == symbol.upper() and len(parts) >= 3:
            if parts[2] < end_date:
                parts[2] = end_date
                lines[i] = "\t".join(parts)
            hit = True
    if hit:
        with open(inst_file, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines))


# ── 主流程 ────────────────────────────────────────────────────────────

def update_symbols(symbols: list[str], provider_uri: str | None = None) -> dict:
    provider_uri = provider_uri or _qlib_data_dir()
    if not os.path.isdir(provider_uri):
        raise RuntimeError(f"qlib 数据目录不存在: {provider_uri}")
    cal = extend_calendar(provider_uri)

    ok, fail = 0, []
    for s in symbols:
        sym = s.lower()
        try:
            df = fetch_ak_ohlcv(sym)
            n = rewrite_bins(provider_uri, sym, df, cal)
            latest = df["day"].max()
            update_instrument_end(provider_uri, sym, latest.strftime("%Y-%m-%d"))
            if n:
                ok += 1
                logger.info(
                    f"✓ {sym}: {n} 行 ({df['day'].min().date()} ~ {latest.date()})")
            else:
                fail.append(sym)
        except Exception as e:
            fail.append(sym)
            logger.warning(f"✗ {sym}: {type(e).__name__}: {e}")
    return {"ok": ok, "fail": fail}


def load_watch_pool() -> list[str]:
    import json

    wf = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "wyckoff_watchlist.json")
    if os.path.isfile(wf):
        try:
            data = json.load(open(wf, encoding="utf-8"))
            if isinstance(data, list):
                return [str(x).lower() for x in data]
        except Exception as e:
            logger.warning(f"读取自选股失败: {e}")
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description="更新 qlib 本地数据 (akshare 全历史 qfq)")
    ap.add_argument("--symbols", nargs="*", help="股票代码列表 (sh600015 形式)")
    ap.add_argument("--watchlist", action="store_true", help="更新自选股")
    ap.add_argument("--qlib-data", default=None, help="qlib 数据目录 (默认项目内)")
    ap.add_argument("--max-symbols", type=int, default=0, help="最多处理 symbol 数 (调试用)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    symbols = list(args.symbols or [])
    if args.watchlist:
        symbols += load_watch_pool()
    if not symbols:
        ap.error("需要 --symbols 或 --watchlist")
    symbols = sorted(set(symbols))
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    logger.info(f"目标 {len(symbols)} 只: {', '.join(symbols)}")
    result = update_symbols(symbols, args.qlib_data)
    logger.info(f"完成: 成功 {result['ok']}, 失败 {len(result['fail'])}")
    if result["fail"]:
        logger.info("失败: " + ", ".join(result["fail"]))
    return 0 if not result["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
