#!/usr/bin/env python3
"""A股市场状态 / 历史类比 / 风险预算 CLI.

用法:
    python scripts/ashare_outlook.py
    python scripts/ashare_outlook.py --json
    python scripts/ashare_outlook.py --primary sh000300
    python scripts/ashare_outlook.py --out reports/ashare_outlook.md
    python scripts/ashare_outlook.py --no-sectors
    python scripts/ashare_outlook.py --no-etf
    python scripts/ashare_outlook.py --no-liquidity
    python scripts/ashare_outlook.py --record
    python scripts/ashare_outlook.py --backfill
    python scripts/ashare_outlook.py --stats
    python scripts/ashare_outlook.py --stock-backfill
    python scripts/ashare_outlook.py --stock-stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 项目根目录
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows 控制台中文输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="A股市场状态 / 历史类比 / 风险预算")
    parser.add_argument("--primary", default="sh000001", help="主分析指数 (默认 sh000001 上证指数)")
    parser.add_argument("--datalen", type=int, default=1200, help="指数日线根数 (默认 1200)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown 报告")
    parser.add_argument("--no-sectors", action="store_true", help="跳过行业板块抓取 (更快/离线)")
    parser.add_argument("--no-etf", action="store_true", help="跳过国家队/宽基ETF确认 (更快)")
    parser.add_argument("--no-liquidity", action="store_true", help="跳过流动性/信用确认 (更快)")
    parser.add_argument("--backtest", action="store_true", help="只跑状态分机械规则回测")
    parser.add_argument("--no-backtest", action="store_true", help="报告里跳过机械规则回测")
    parser.add_argument("--no-changes", action="store_true", help="报告里跳过与上次记录对比")
    parser.add_argument("--cost-bps", type=float, default=5.0, help="回测单边成本 (bp, 默认5)")
    parser.add_argument("--record", action="store_true", help="把本次报告写入预测日志")
    parser.add_argument("--backfill", action="store_true", help="回填已到期预测的实际结果")
    parser.add_argument("--stats", action="store_true", help="查看预测命中率/校准统计")
    parser.add_argument("--stock-backfill", action="store_true", help="回填个股信号")
    parser.add_argument("--stock-stats", action="store_true", help="查看个股信号统计")
    parser.add_argument("--stock-journal", default="", help="个股日志路径 (默认用户数据目录)")
    parser.add_argument(
        "--horizon", type=int, default=20, choices=(5, 20, 60), help="统计/回填主周期 (默认 20 日)"
    )
    parser.add_argument("--journal", default="", help="预测日志路径 (默认用户数据目录)")
    parser.add_argument("--out", default="", help="把 Markdown 报告写入文件")
    args = parser.parse_args()

    journal = args.journal or None
    stock_journal = args.stock_journal or None
    if args.stock_stats or args.stock_backfill:
        from wyckoff.ashare_journal import (
            backfill_stocks,
            compute_stock_stats,
            render_backfill_summary,
            render_stock_stats,
        )

        if args.stock_stats:
            stats = compute_stock_stats(path=stock_journal, horizon=args.horizon)
            text = (
                json.dumps(stats, ensure_ascii=False, indent=2, default=str)
                if args.json
                else render_stock_stats(stats)
            )
            print(text)
            return 0
        summary = backfill_stocks(path=stock_journal)
        text = (
            json.dumps(summary, ensure_ascii=False, indent=2, default=str)
            if args.json
            else render_backfill_summary(summary, title="个股预测回填")
        )
        print(text)
        return 0

    if args.stats or args.backfill:
        from wyckoff.ashare_journal import (
            backfill,
            compute_stats,
            render_backfill_summary,
            render_stats,
        )

        if args.stats:
            stats = compute_stats(path=journal, horizon=args.horizon)
            text = (
                json.dumps(stats, ensure_ascii=False, indent=2, default=str)
                if args.json
                else render_stats(stats)
            )
            print(text)
            return 0
        summary = backfill(path=journal)
        text = (
            json.dumps(summary, ensure_ascii=False, indent=2, default=str)
            if args.json
            else render_backfill_summary(summary)
        )
        print(text)
        return 0

    if args.backtest:
        from wyckoff.ashare_backtest import backtest_state, render_backtest
        from wyckoff.ashare_outlook import (
            compute_features,
            prepare_index_frame,
            regime_score_series,
        )

        df = prepare_index_frame(args.primary, datalen=args.datalen)
        if df is None:
            print(f"数据不足: {args.primary}")
            return 1
        f = compute_features(df)
        f["regime_score"] = regime_score_series(f)
        bt = backtest_state(f, cost_bps=args.cost_bps)
        text = (
            json.dumps(bt, ensure_ascii=False, indent=2, default=str)
            if args.json
            else render_backtest(bt)
        )
        print(text)
        return 0

    from wyckoff.ashare_outlook import build_outlook, render_report

    result = build_outlook(
        primary=args.primary,
        datalen=args.datalen,
        include_sectors=not args.no_sectors,
        include_etf=not args.no_etf,
        include_liquidity=not args.no_liquidity,
        include_backtest=not args.no_backtest,
        include_changes=not args.no_changes,
        journal_path=journal,
    )
    if args.record:
        from wyckoff.ashare_journal import record_outlook

        info = record_outlook(result, path=journal)
        msg = (
            f"已记录: {info.get('id')} "
            f"({'覆盖' if info.get('replaced') else '新增'}, "
            f"共 {info.get('total')} 条) -> {info.get('path')}"
        )
        print(msg, file=sys.stderr if args.json else sys.stdout)

    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    else:
        text = render_report(result)

    if args.out:
        out_path = os.path.abspath(args.out)
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"报告已写入: {out_path}")
    else:
        print(text)
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
