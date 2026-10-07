"""个股基本面趋势报告 (CLI): as-of 财报趋势表 + A/B/C/D 分层 + 趋势判读 + 实时估值。

用法:
  python -m wyckoff.fundamental_trend 600104
  python -m wyckoff.fundamental_trend sh600104 --since 2021
  python -m wyckoff.fundamental_trend 600104 --json --no-live

数据: wyckoff/fund_history.py (东财业绩报表, 财报按公告日取数无前视); 该股无
缓存时自动单股回补 (1 个请求, --no-fetch 关闭)。实时估值 (PE/PB/市值) 走
fetch_fundamental 快照, 失败跳过 (--no-live 关闭)。
退出码: 0 = 报告输出成功; 1 = 无任何财报数据 (缓存缺失且回补失败)。
"""
from __future__ import annotations

import argparse
import json
from datetime import date

from .fund_history import backfill_one, gate_ok, load_history, tier_at, tier_of, weight
from .utils import normalize_symbol

# 表格默认展示最近 N 份报告 (--since/--all 覆盖)
DEFAULT_ROWS = 12

_TIER_NAME = {"A": "优", "B": "中性", "C": "弱", "D": "排雷", "": "无数据"}
_TIER_DESC = {
    "A": "净利同比≥+15% 且营收非负增长",
    "B": "有数据但不够 A/C (温和增长)",
    "C": "净利负增长 或 营收同比<-10%",
    "D": "报告期亏损 / 净利断崖≤-30% / 负增长+高估值 / 双杀",
    "": "无历史财报 (门禁 fail-open 放行, 权重 1.0)",
}


def _series(rows, key, n=4):
    return [r.get(key) for r in rows[-n:] if r.get(key) is not None]


def _fmt(v, signed=False):
    if v is None:
        return "-"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def trend_verdict(rows, tier=""):
    """近 4 期财报 → 趋势判读 bullets (dict)。rows 按公告日升序, tier 为当前分层。"""
    out = {}
    rev = _series(rows, "rev", 4)
    if rev:
        w = "走平"
        if len(rev) >= 2:
            if rev[-1] > rev[0] + 1:
                w = "上行"
            elif rev[-1] < rev[0] - 1:
                w = "下行"
        out["rev"] = (f"营收同比 {'→'.join(f'{v:+.1f}%' for v in rev)}, 整体{w}")
    else:
        out["rev"] = "营收同比数据缺失"
    np_ = _series(rows, "np", 4)
    if np_:
        neg = 0
        for r in reversed(rows):
            v = r.get("np")
            if v is None:
                break
            if v < 0:
                neg += 1
            else:
                break
        txt = f"净利同比 {'→'.join(f'{v:+.1f}%' for v in np_)}"
        if neg:
            txt += f"; 最近连续 {neg} 期负增长"
        out["np"] = txt
    else:
        out["np"] = "净利同比数据缺失"
    roe = _series(rows, "roe", 4)
    gm = _series(rows, "gm", 4)
    q = []
    if roe:
        q.append(f"加权ROE {'→'.join(f'{v:.1f}%' for v in roe)}")
    if gm:
        q.append(f"毛利率 {'→'.join(f'{v:.1f}%' for v in gm)}")
    out["quality"] = " · ".join(q) if q else "盈利质量指标缺失"
    if tier == "A":
        out["conclusion"] = "增长健康 (A): 基本面支撑方向, 按分层权重 1.0 正常配置"
    elif tier == "B":
        out["conclusion"] = "中性 (B): 无明显恶化亦无加速, 权重 0.8, 跟踪下期财报"
    elif tier == "C":
        out["conclusion"] = "偏弱 (C): 利润端承压, 权重降至 0.5, 需等增速转正信号"
    elif tier == "D":
        out["conclusion"] = "排雷 (D): 亏损/断崖, 硬门禁应拦截不建仓 (权重 0.25 仅适用门禁关闭时)"
    else:
        out["conclusion"] = "无历史财报: fail-open (门禁放行/权重 1.0), 建议先回填数据"
    return out


def build_report(code, since="", show_rows=DEFAULT_ROWS, fetch=True, live=True):
    """组装报告数据 dict (CLI 与 --json 共用)。无财报数据 → 附 error 字段。"""
    symbol = normalize_symbol(str(code))
    code6 = symbol[-6:]
    hist = load_history()
    rows = list(hist.get(code6) or [])
    if not rows and fetch:
        backfill_one(code6)
        rows = list(load_history().get(code6) or [])
    if since:
        rows = [r for r in rows if str(r.get("d") or "") >= since]
    n_total = len(rows)
    today = date.today().isoformat()
    tier = ""
    if rows:
        tier = tier_at(code6, today)
        if not tier:
            tier = tier_of(np_yoy=rows[-1].get("np"), rev_yoy=rows[-1].get("rev"),
                           eps=rows[-1].get("eps"))
    out = {
        "code": code6,
        "symbol": symbol,
        "asof_day": today,
        "tier": tier,
        "tier_name": _TIER_NAME.get(tier, tier),
        "tier_desc": _TIER_DESC.get(tier, ""),
        "weight": weight(tier),
        "gate_ok": gate_ok(tier),
        "n_reports": n_total,
        "rows": rows[-show_rows:] if show_rows else rows,
        "verdict": trend_verdict(rows, tier) if rows else {},
    }
    if not rows:
        out["error"] = "无财报数据 (缓存缺失且回补失败, 检查网络)"
        return out
    try:
        from .datasource import fetch_name

        out["name"] = fetch_name(symbol) or ""
    except Exception:
        out["name"] = ""
    if live:
        try:
            from .fundamental import fetch_fundamental

            f = fetch_fundamental(symbol) or {}
            out["live"] = {
                "price": f.get("price"), "pe_ttm": f.get("pe_ttm"),
                "pb": f.get("pb"), "mcap_yi": f.get("mcap_yi"),
            }
        except Exception:
            out["live"] = None
    return out


def _render(rep) -> str:
    if rep.get("error"):
        return f"{rep['code']}: {rep['error']}"
    title = f"{rep['code']} {rep.get('name') or ''}".strip()
    gate_txt = ("拦截D层" if rep["tier"] == "D" else "放行") if rep["tier"] \
        else "放行 (无数据 fail-open)"
    L = [
        f"基本面趋势报告 · {title}",
        f"- 生成时间: {date.today().isoformat()} · 数据: 财报按公告日 as-of (无前视)",
        f"- 当前分层: {rep['tier'] or '-'} ({rep['tier_name']}) — {rep['tier_desc']}",
        f"- 分层仓位权重: {rep['weight']} · 基本面硬门禁: {gate_txt}",
        "",
        f"## 财报趋势 (最近 {len(rep['rows'])} 份, 共 {rep['n_reports']} 份)",
        "",
        "| 报告期 | 公告日 | 营收同比 | 净利同比 | ROE | 毛利率 | EPS |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in reversed(rep["rows"]):
        eps = "-" if r.get("eps") is None else f"{r['eps']:.3f}"
        L.append(f"| {r.get('d', '')} | {r.get('n', '')} "
                 f"| {_fmt(r.get('rev'), True)} | {_fmt(r.get('np'), True)} "
                 f"| {_fmt(r.get('roe'))} | {_fmt(r.get('gm'))} | {eps} |")
    L += ["", "## 趋势判读", ""]
    v = rep.get("verdict") or {}
    for k, label in (("rev", "收入"), ("np", "利润"), ("quality", "质量"),
                     ("conclusion", "结论")):
        if v.get(k):
            L.append(f"- {label}: {v[k]}")
    lv = rep.get("live")
    if lv:
        L += ["", "## 实时估值 (快照)", ""]
        if lv.get("price") is not None:
            L.append(f"- 现价 {lv['price']}")
        if lv.get("pe_ttm") is not None:
            L.append(f"- PE(TTM) {lv['pe_ttm']:.2f}" if lv["pe_ttm"] > 0
                     else f"- PE(TTM) {lv['pe_ttm']:.2f} (亏损)")
        if lv.get("pb") is not None:
            L.append(f"- PB {lv['pb']:.2f}")
        if lv.get("mcap_yi") is not None:
            L.append(f"- 总市值 {lv['mcap_yi']:.0f} 亿")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m wyckoff.fundamental_trend",
        description="个股基本面趋势报告 (as-of 财报 + A/B/C/D 分层, 公告日前视安全)")
    ap.add_argument("code", help="股票代码 (600104 / sh600104 均可)")
    ap.add_argument("--since", default="", help="只看该年份以来 (如 2021)")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                    help=f"表格展示份数 (默认 {DEFAULT_ROWS}, 0=全部)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="无缓存时不自动单股回补 (只读本地)")
    ap.add_argument("--no-live", action="store_true", help="不抓实时估值快照")
    ap.add_argument("--json", action="store_true", help="输出 JSON (供脚本消费)")
    args = ap.parse_args(argv)
    since = f"{args.since}-01-01" if args.since.isdigit() and len(args.since) == 4 \
        else args.since
    rep = build_report(args.code, since=since, show_rows=args.rows,
                       fetch=not args.no_fetch, live=not args.no_live)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(_render(rep))
    return 1 if rep.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
