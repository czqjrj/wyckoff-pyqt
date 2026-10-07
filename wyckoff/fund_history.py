"""历史财报 (公告日前视安全) 与基本面 A/B/C/D 分层 — 门禁与仓位权重单一权威。

数据源: 东财 datacenter 业绩报表 (RPT_LICO_FN_CPD), 两路拉取:
  - backfill():     全市场按报告期分页回填 (~286 请求覆盖 2019 以来全部报告期,
    东财 datacenter pageSize 硬上限 500 → 每期 ~13 页 × 6000 只)
  - backfill_one(): 单股回补 (CLI 缺数据时自动触发, 1 请求)
缓存: paths.FUND_HISTORY_FILE = wyckoff_fund_history.json (gitignored),
  格式 {code6: [row...]}, row = {d 报告期, n 公告日, rev 营收同比%, np 净利同比%,
  eps 基本每股收益, roe 加权ROE%, gm 毛利率%, bps 每股净资产}。

无前视 (单一取数口径, 全项目共用):
  asof(code, day) 只取 公告日(n) <= day 的最近一份报告 —— 公告日才是市场得知日;
  报告期本身早于公告数周至数月, 按报告期取数即前视。公告日缺失时回退到法定
  披露截止日 (Q1/年报→次年或当年04-30, 半年报→08-31, 三季报→10-31, 永不早于
  报告期), 仍是"最晚可能得知日", 不泄漏。

分层 tier_of() — 回测门禁 / 实盘仓位 / 准确率报告 / CLI 报告共用同一判断:
  A 优     净利同比 ≥ +15% 且营收非负增长
  B 中性   有数据但不够 A/C (温和增长 / 营收微降但利润仍增)
  C 弱     净利负增长 或 营收同比 < -10%
  D 排雷   报告期亏损 / 净利断崖 ≤ -30% / 负增长+高估值(价值陷阱) / 收入利润双杀
  ""       无数据 → fail-open: 门禁放行、仓位权重 1.0 (缺数据不惩罚)
仓位权重 TIER_WEIGHT: A=1.0 / B=0.8 / C=0.5 / D=0.25 (D 由硬门禁拦, 关门禁时给小仓)。

接线点:
  - 回放硬门禁/权重: scripts/paper_replay_bt.py --fund-gate / --fund-weight
  - 实盘: paper.run_cycle (paper_fund_gate / paper_fund_tier_weight 设置)
  - 单仓资金权重: paper._trading._make_order(fund_weight=...)
  - 准确率分层表: paper_strategy_accuracy.tier_stats
  - 个股报告: python -m wyckoff.fundamental_trend <code>
"""
from __future__ import annotations

import json
import time
from datetime import date
from threading import Lock

from ._shared import atomic_write_json
from .paths import FUND_HISTORY_FILE

# 东财 datacenter 业绩报表 (探针验证: columns=ALL + HSF10/PC 稳定返回;
# pagesize 超 500 被截断, 单股/全市场按期均可用)
_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json",
}
_PAGE_SIZE = 500

# ── 分层阈值 (百分比单位, 与 SJLTZ/YSTZ 同口径) ──
D_NP_CRASH = -30.0     # 净利同比断崖
D_PE_HIGH = 30.0       # 负增长 + 高 PE = 价值陷阱 (与 filters.fundamental_filter 同口径)
D_REV_CRASH = -20.0    # 营收断崖 + 利润负增长 = 双杀
C_REV_DECLINE = -10.0  # 营收萎缩 (利润口径单独判 C)
A_NP_MIN = 15.0        # A 档净利同比下限
# 仓位权重: D 关硬门禁时给小仓 (试探性), "" 无数据 fail-open = 1.0
TIER_WEIGHT = {"A": 1.0, "B": 0.8, "C": 0.5, "D": 0.25, "": 1.0}
TIERS = ("A", "B", "C", "D")

# 法定披露截止日 (公告日缺失时的回退, 永不早于报告期 → 不泄漏)
_DEADLINE_MD = {"03-31": "04-30", "06-30": "08-31", "09-30": "10-31", "12-31": "04-30"}

_LOCK = Lock()
_HIST = None          # 进程内缓存 {code6: [row...] 按公告日升序}
_HIST_LOADED = False


def _notice_fallback(report_date: str) -> str:
    """公告日缺失 → 法定披露截止日 (Q1/年报→04-30, 半年报→08-31, 三季报→10-31)。"""
    md = report_date[5:10]
    y = int(report_date[:4]) if report_date[:4].isdigit() else 0
    if md == "12-31":
        y += 1
    return f"{y}-{_DEADLINE_MD.get(md, '04-30')}"


def _num(v):
    """东财数值字段 → float; 缺失/"—"/NaN → None (不把缺失当 0)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _parse_row(s) -> tuple[str, dict] | tuple[None, None]:
    """业绩报表原始行 → (code6, row); 代码/报告期非法返回 (None, None)。"""
    code = str(s.get("SECURITY_CODE") or "")
    d = str(s.get("REPORTDATE") or "")[:10]
    if len(code) != 6 or not code.isdigit() or len(d) != 10:
        return None, None
    n = str(s.get("NOTICE_DATE") or "")[:10]
    if len(n) != 10:
        n = _notice_fallback(d)
    return code, {
        "d": d, "n": n,
        "rev": _num(s.get("YSTZ")),      # 营业收入同比 %
        "np": _num(s.get("SJLTZ")),      # 净利润同比 %
        "eps": _num(s.get("BASIC_EPS")),  # 基本每股收益
        "roe": _num(s.get("WEIGHTAVG_ROE")),  # 加权ROE %
        "gm": _num(s.get("XSMLL")),      # 销售毛利率 %
        "bps": _num(s.get("BPS")),       # 每股净资产
    }


def _sort_rows(rows):
    rows.sort(key=lambda r: (str(r.get("n") or ""), str(r.get("d") or "")))
    return rows


def load_history(refresh: bool = False) -> dict:
    """读历史财报缓存 (进程内缓存一次; 文件缺失/损坏 → {})。"""
    global _HIST, _HIST_LOADED
    with _LOCK:
        if _HIST is not None and _HIST_LOADED and not refresh:
            return _HIST
    hist = {}
    try:
        with open(FUND_HISTORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            hist = {str(k): _sort_rows(v) for k, v in raw.items()
                    if isinstance(v, list)}
    except (OSError, ValueError):
        hist = {}
    with _LOCK:
        _HIST, _HIST_LOADED = hist, True
    return hist


def save_history(hist: dict) -> str:
    atomic_write_json(FUND_HISTORY_FILE, hist, indent=None)
    return FUND_HISTORY_FILE


def reset_cache():
    """丢弃进程内缓存 (测试 / 回填后重读)。"""
    global _HIST, _HIST_LOADED
    with _LOCK:
        _HIST, _HIST_LOADED = None, False


def _merge_rows(hist: dict, code: str, rows: list[dict]):
    cur = {str(r.get("d")): r for r in (hist.get(code) or [])}
    for r in rows:
        cur[str(r.get("d"))] = r
    hist[code] = _sort_rows(list(cur.values()))


def _periods(start_year: int = 2019) -> list[str]:
    """回填报告期列表 (截止 = 披露截止日已过; 未出完的报告期不抓, 按期重跑即可)。"""
    today = date.today().isoformat()
    out = []
    for y in range(start_year, date.today().year + 1):
        for md in ("03-31", "06-30", "09-30", "12-31"):
            p = f"{y}-{md}"
            if _notice_fallback(p) <= today:
                out.append(p)
    return out


def _fetch_period(period: str, page: int) -> tuple[list | None, int]:
    """全市场单期第 page 页原始行; 失败 (None, 0)。"""
    from .fundamental import _get

    params = {
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": f"(REPORTDATE='{period}')",
        "pageNumber": str(page),
        "pageSize": str(_PAGE_SIZE),
        "sortColumns": "SECURITY_CODE",
        "sortTypes": "1",
        "source": "HSF10",
        "client": "PC",
    }
    r = _get(_URL, params, _HEADERS, timeout=20, retries=2, cache_fail=False)
    if r is None:
        return None, 0
    try:
        res = r.json().get("result") or {}
    except ValueError:
        return None, 0
    return res.get("data") or [], int(res.get("count") or 0)


def backfill(start_year: int = 2019, periods: list[str] | None = None,
             save: bool = True, log=None) -> int:
    """全市场按报告期分页回填。返回解析成功的行数 (含重复期覆盖)。"""
    hist = load_history()
    periods = list(periods) if periods is not None else _periods(start_year)
    log = log or (lambda m: None)
    n_rows = 0
    for i, period in enumerate(periods):
        page = 1
        while True:
            rows, total = _fetch_period(period, page)
            if rows is None:
                log(f"fund_history: {period} 第{page}页失败, 跳过该期")
                break
            by_code = {}
            for s in rows:
                code, row = _parse_row(s)
                if code:
                    by_code.setdefault(code, []).append(row)
                    n_rows += 1
            for code, rs in by_code.items():
                _merge_rows(hist, code, rs)
            if not rows or page * _PAGE_SIZE >= total:
                break
            page += 1
        log(f"fund_history: {period} 完成 ({i + 1}/{len(periods)}), "
            f"累计 {len(hist)} 只")
    if save:
        save_history(hist)
    return n_rows


def backfill_one(code: str, save: bool = True) -> int:
    """单股回补 (1 请求)。返回该股缓存内报告条数。"""
    from .fundamental import _get

    code6 = str(code)[-6:]
    hist = load_history()
    params = {
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code6}")',
        "pageNumber": "1",
        "pageSize": "50",
        "sortColumns": "REPORTDATE",
        "sortTypes": "-1",
        "source": "HSF10",
        "client": "PC",
    }
    r = _get(_URL, params, _HEADERS, timeout=15, retries=2, cache_fail=False)
    if r is not None:
        try:
            rows = (r.json().get("result") or {}).get("data") or []
        except ValueError:
            rows = []
        parsed = []
        for s in rows:
            c, row = _parse_row(s)
            if c == code6:
                parsed.append(row)
        if parsed:
            _merge_rows(hist, code6, parsed)
            if save:
                save_history(hist)
    return len(hist.get(code6) or [])


def _day_str(day) -> str:
    if day is None:
        return ""
    if isinstance(day, str):
        return day[:10]
    try:
        return day.strftime("%Y-%m-%d")
    except Exception:
        return ""


def asof(code, day, hist: dict | None = None) -> dict | None:
    """公告日 <= day 的最近一份财报 row; 无则 None (无前视)。

    code 兼容 600104 / sh600104 / 600104.SH 等形态 (取末 6 位数字)。
    """
    if code is None:
        return None
    code6 = str(code)[-6:]
    if not code6.isdigit():
        return None
    day = _day_str(day)
    if not day:
        return None
    h = load_history() if hist is None else hist
    rows = h.get(code6)
    if not rows:
        return None
    # rows 已按公告日升序 → 二分找最后一个 n <= day
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if str(rows[mid].get("n") or "") <= day:
            lo = mid + 1
        else:
            hi = mid
    return rows[lo - 1] if lo else None


def tier_of(np_yoy=None, rev_yoy=None, eps=None, pe=None) -> str:
    """基本面分层 A/B/C/D (单一权威; 参数为百分比数值, 10 = +10%)。

    全部参数缺失 → "" (无数据 fail-open)。EPS/PE 判亏损优先于增速 (亏损股
    增速无意义), 之后按 D → C → A → B 顺序判定。
    """
    if np_yoy is None and rev_yoy is None and eps is None and pe is None:
        return ""
    if eps is not None and eps <= 0:
        return "D"
    if pe is not None and pe < 0:
        return "D"
    if np_yoy is not None:
        if np_yoy <= D_NP_CRASH:
            return "D"
        if np_yoy < 0 and pe is not None and pe > D_PE_HIGH:
            return "D"
        if np_yoy < 0 and rev_yoy is not None and rev_yoy <= D_REV_CRASH:
            return "D"
        if np_yoy < 0:
            return "C"
    if rev_yoy is not None and rev_yoy < C_REV_DECLINE:
        return "C"
    if np_yoy is not None and np_yoy >= A_NP_MIN \
            and (rev_yoy is None or rev_yoy >= 0):
        return "A"
    return "B"


def tier_at(code, day, hist: dict | None = None) -> str:
    """信号日 as-of 分层: 只用公告日 <= day 的财报 (无前视)。无数据 → ""。"""
    r = asof(code, day, hist=hist)
    if not r:
        return ""
    return tier_of(np_yoy=r.get("np"), rev_yoy=r.get("rev"), eps=r.get("eps"))


def gate_ok(tier: str) -> bool:
    """硬门禁判定: D 层拦截; "" 无数据 fail-open 放行。"""
    return tier != "D"


def weight(tier: str) -> float:
    """分层 → 单仓资金权重 (未知层级 = 1.0)。"""
    return float(TIER_WEIGHT.get(tier, 1.0))


def main(argv=None) -> int:
    """回填 CLI: python -m wyckoff.fund_history [--from 2021] [--code 600104]"""
    import argparse

    ap = argparse.ArgumentParser(description="历史财报回填 (东财业绩报表, 含公告日)")
    ap.add_argument("--from", dest="start_year", type=int, default=2019,
                    help="起始年份 (默认 2019)")
    ap.add_argument("--code", default="", help="只回补单股 (6 位代码)")
    args = ap.parse_args(argv)
    if args.code:
        n = backfill_one(args.code)
        print(f"fund_history: {args.code} 缓存 {n} 份报告 -> {FUND_HISTORY_FILE}")
        return 0 if n else 1
    t0 = time.time()
    n = backfill(start_year=args.start_year, log=print)
    print(f"fund_history: {n} 行 / {len(load_history())} 只 / "
          f"{time.time() - t0:.0f}s -> {FUND_HISTORY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
