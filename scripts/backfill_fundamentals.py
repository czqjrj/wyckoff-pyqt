#!/usr/bin/env python3
"""历史财报回填 (东财业绩报表, 含公告日 → 基本面分层无前视数据源)。

产物: wyckoff_fund_history.json {code: [{d 报告期, n 公告日, rev, np, eps, ...}]}
供 fund_history.asof/tier_at 消费 (回放门禁 --fund-gate、实盘分层仓位、
准确率分层表、python -m wyckoff.fundamental_trend)。可随时重跑增量合并。

用法:
  python scripts/backfill_fundamentals.py                 # 全市场 2019 以来全部报告期
  python scripts/backfill_fundamentals.py --from 2023     # 指定起始年 (更快)
  python scripts/backfill_fundamentals.py --code 600104   # 单股回补
  python scripts/backfill_fundamentals.py --data-dir data/paper_replay_data
      # 回放/消融隔离目录 (paper_replay_bt 设了 WYCKOFF_DATA_DIR 读不到主缓存)
"""
import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="历史财报回填 (东财业绩报表)")
    ap.add_argument("--from", dest="start_year", type=int, default=2019,
                    help="起始年份 (默认 2019, 覆盖 ~7 年报告期)")
    ap.add_argument("--code", default="", help="只回补单股 (6 位代码, 1 请求)")
    ap.add_argument("--data-dir", default="",
                    help="WYCKOFF_DATA_DIR 目标目录 (回放隔离环境用)")
    args = ap.parse_args(argv)
    if args.data_dir:
        # 必须在导入 wyckoff.* 之前设置: paths.DATA_DIR 在 import 时定型
        os.environ["WYCKOFF_DATA_DIR"] = args.data_dir
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

    from wyckoff import fund_history as fh

    if args.code:
        n = fh.backfill_one(args.code)
        print(f"fund_history: {args.code} 缓存 {n} 份报告 -> {fh.FUND_HISTORY_FILE}")
        return 0 if n else 1
    import time

    t0 = time.time()
    n = fh.backfill(start_year=args.start_year, log=print)
    print(f"fund_history: {n} 行 / {len(fh.load_history())} 只 / "
          f"{time.time() - t0:.0f}s -> {fh.FUND_HISTORY_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
