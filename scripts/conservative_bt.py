#!/usr/bin/env python3
"""威科夫保守年化回测 CLI (薄封装: 库逻辑在 wyckoff.conservative_bt)。

用法:
  python scripts/conservative_bt.py                          # 默认 conf=0, 打印摘要
  python scripts/conservative_bt.py --conf 90                # 只看 conf≥90
  python scripts/conservative_bt.py --conf 90 --dedup date   # 同日去重
  python scripts/conservative_bt.py --dedup sector --sector-map wyckoff_board_map.json
  python scripts/conservative_bt.py --export docs/trades.csv # 导出逐笔明细
  python scripts/conservative_bt.py --report docs/conservative_bt_report.md  # 写报告
  python scripts/conservative_bt.py --capital 500000 --slots 5  # 定制资金/持仓
  python scripts/conservative_bt.py --cost 0.008 --stop 0.05
"""
import argparse
import json
import os
import sys

# 让脚本被直接执行时能 import wyckoff
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff import conservative_bt as cbt  # noqa: E402
from wyckoff.paths import SIGNAL_ACCURACY_FILE  # noqa: E402


def _load_dedup_args(dedup, sector_map_path):
    """构造 (dedup, sector_map)。--dedup sector 而无地图时模块自动退化为 date。"""
    sector_map = None
    if sector_map_path:
        with open(sector_map_path, encoding="utf-8") as f:
            sector_map = json.load(f)
    return dedup, sector_map


def main():
    ap = argparse.ArgumentParser(description="威科夫保守年化回测")
    ap.add_argument("--conf", type=int, default=0, help="最低置信度 (0/80/90)")
    ap.add_argument("--cost", type=float, default=cbt.DEFAULT_COST,
                    help="往返成本比例, 默认0.008")
    ap.add_argument("--stop", type=float, default=0.05,
                    help="结构位止损绝对值, 默认0.05; 0 表示不止损")
    ap.add_argument("--capital", type=float, default=100_000.0, help="总资金")
    ap.add_argument("--slots", type=int, default=3, help="并发槽位数")
    ap.add_argument("--hold", type=int, default=32, help="持有天数")
    ap.add_argument("--dedup", choices=("none", "date", "sector"),
                    default="none", help="去重模式")
    ap.add_argument("--sector-map", default="", help="stock->板块映射 JSON 路径")
    ap.add_argument("--export", default="", help="导出逐笔明细 CSV 到该路径")
    ap.add_argument("--report", default="", help="写保守回测报告 md 到该路径")
    args = ap.parse_args()

    stop = None if args.stop == 0 else -abs(args.stop)
    dedup, sector_map = _load_dedup_args(args.dedup, args.sector_map)

    records = json.load(open(SIGNAL_ACCURACY_FILE, encoding="utf-8"))

    if args.export:
        path = cbt.export_csv(records, conf_min=args.conf, path=args.export,
                              cost=args.cost, stop=stop, dedup=dedup,
                              sector_map=sector_map)
        print(f"已导出逐笔明细: {path}")
    if args.report:
        md = cbt.build_report(records, conf_min=args.conf, cost=args.cost,
                              stop=stop, capital=args.capital,
                              position_count=args.slots, dedup=dedup,
                              sector_map=sector_map)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"已写出报告: {args.report}")

    pt = cbt.per_trade_stats(records, conf_min=args.conf, cost=args.cost,
                             stop=stop, dedup=dedup, sector_map=sector_map)
    pv = cbt.portfolio_backtest(records, conf_min=args.conf,
                                capital=args.capital,
                                position_count=args.slots, hold_days=args.hold,
                                cost=args.cost, stop=stop, dedup=dedup,
                                sector_map=sector_map)
    print("=" * 64)
    print(f"conf≥{args.conf}  去重={dedup}")
    print(f"  逐笔: n={pt.n} 每笔均值={pt.mean_net:+.2f}% 胜率={pt.win_rate:.1f}% "
          f"盈亏比={pt.pl_ratio:.2f} 最差={pt.worst:+.2f}%")
    print(f"  保守年化: {pt.cagr_low:+.1f}% ~ {pt.cagr_high:+.1f}% / 年")
    print(f"  组合(资金{args.capital:,.0f}/{args.slots}槽): 笔数={pv.n_trades} "
          f"槽位CAGR(偏高参考)={pv.cagr:+.1f}%")


if __name__ == "__main__":
    main()
