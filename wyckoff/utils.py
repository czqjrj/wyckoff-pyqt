"""通用工具函数。"""

def locate_bar(days_or_df, date_str, nearest_before=False, max_gap=7):
    """在 K 线中定位 date_str 对应的 bar 索引; 找不到返回 None。

    days_or_df: df (取其 day 列) 或直接传 day Series。
    匹配规则:
      精确模式 (默认): 先按完整字符串匹配 (兼容 "YYYY-MM-DD HH:MM" 与
        纯日期两种键), 再按前 10 位日期前缀回退; 多个命中取最后一个。
      nearest_before=True: searchsorted 取 date_str 之前最近的一个交易日,
        且与目标自然日差 ≤ max_gap 才算命中 (供停牌缺口容错)。

    统一此前 accuracy._locate_ref / signal_accuracy._locate /
    backfill_ctx._locate_idx 三份重复实现。
    """
    try:
        days = days_or_df["day"] if isinstance(days_or_df, pd.DataFrame) \
            else days_or_df
        if days is None or len(days) == 0:
            return None
        if nearest_before:
            ts = pd.Timestamp(date_str)
            j = int(days.searchsorted(ts, side="right")) - 1
            if j < 0:
                return None
            gap = abs((ts - pd.Timestamp(days.iloc[j])).days)
            if gap > max_gap:
                return None
            return j
        s = days.astype(str)
        idx = np.where(s.values == str(date_str))[0]
        if len(idx):
            return int(idx[-1])
        # 分钟精度键 ("YYYY-MM-DD HH:MM"): 两侧都截到分钟再比
        # (astype(str) 带秒, 直接比对会漏配分钟级 ref_dt)
        try:
            mins = pd.to_datetime(days).dt.strftime("%Y-%m-%d %H:%M").values
            idx = np.where(mins == str(date_str)[:16])[0]
            if len(idx):
                return int(idx[-1])
        except Exception:
            pass
        idx = np.where(s.str.startswith(str(date_str)[:10]).values)[0]
        if len(idx):
            return int(idx[-1])
    except Exception:
        return None
    return None


def normalize_symbol(code: str) -> str:
    """把用户输入规范化为 sina 风格代码, 如 600104 -> sh600104"""
    code = (code or "").strip().lower().replace(".sh", "").replace(".sz", "").replace(".bj", "")
    if len(code) == 6 and code.isdigit():
        if code.startswith(("6", "5")):
            return "sh" + code
        if code.startswith(("15", "16")):  # 深市 ETF/LOF
            return "sz" + code
        if code.startswith(("0", "2", "3")):
            return "sz" + code
        if code.startswith(("4", "8", "9")):
            return "bj" + code
    if len(code) == 8 and code[:2] in ("sh", "sz", "bj"):
        return code
    raise ValueError(f"无法识别的股票代码: {code}")
