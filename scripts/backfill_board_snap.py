#!/usr/bin/env python3
"""回填 wx_board_snap.json 的板块强度历史周频快照。

让板块强度门禁在历史回测区间 (2023-06~2026-08) 内也能用 strength_at() 取到
当日真实板块分位, 免于 fail-open 空转 (docs/bt_discipline_3y_gated.md)。

数据源: 东财 push2his 板块日线 (secid=90.BKxxxx), 板块映射复用 fundamental
的磁盘缓存/单次拉取; 强度代理为板块级近5根量价净流入占比的逐日截面分位,
每周最后交易日 1 条快照, 与实盘快照按 ts 合并去重。

用法:
  python scripts/backfill_board_snap.py                # 默认回填 2020-01 起
  python scripts/backfill_board_snap.py --start 2022-01-01 --max-boards 60  # 试跑子集
  python scripts/backfill_board_snap.py --verify       # 只校验现有快照覆盖, 不抓取
  python scripts/backfill_board_snap.py --dry-run      # 只抓取计算, 不写盘
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff import chain  # noqa: E402


def _verify():
    from wyckoff.chain import BOARD_SNAP_FILE, _load_snaps, strength_at
    from wyckoff.fundamental import _load_board_map

    snaps = _load_snaps()
    n_boards_map = len(_load_board_map() or {})
    print(f"快照总数: {len(snaps)} · 板块映射: {n_boards_map}")
    if not snaps:
        print("!! 无任何快照, 需先执行回填")
        return 1
    first = snaps[0]
    last = snaps[-1]
    import datetime as _dt

    def _d(ts):
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    # 回测区间 2023-06-01 ~ 2026-08-31 的覆盖 (每季度抽查一条)
    print(f"覆盖: {_d(first['ts'])} ~ {_d(last['ts'])}")
    lo = _dt.datetime(2023, 6, 1).timestamp()
    hi = _dt.datetime(2026, 8, 31).timestamp()
    in_bt = [s for s in snaps if lo <= s["ts"] <= hi]
    n_pct = sum(len(s.get("strengths") or {}) for s in in_bt)
    print(f"回测区间内快照: {len(in_bt)} 周 · 平均板块数 "
          f"{n_pct / len(in_bt):.0f} / {n_boards_map}"
          if in_bt else "!! 回测区间内无快照")
    for probe in ("汽车", "半导体", "白酒Ⅱ"):
        pct = strength_at(probe, ts="2024-01-15", max_gap_days=45)
        print(f"strength_at({probe}, 2024-01-15) = {pct}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2020-01-01", help="回填起始日 (含30天热身后延)")
    ap.add_argument("--max-boards", type=int, default=None,
                    help="只处理前 N 个板块 (试跑)")
    ap.add_argument("--verify", action="store_true",
                    help="只校验现有快照覆盖与强度可查性, 不抓取")
    ap.add_argument("--dry-run", action="store_true",
                    help="抓取+计算但不写盘")
    args = ap.parse_args(argv)

    if args.verify:
        _verify()
        return 0

    def _cb(done, total, fetched):
        print(f"  板块抓取 {done}/{total} (成功 {fetched})", flush=True)

    stats = chain.backfill_board_strength_series(
        start=args.start, max_boards=args.max_boards, progress_cb=_cb,
        write=not args.dry_run)
    if not stats.get("ok"):
        print(f"回填失败: {stats.get('error')}")
        return 1
    print("回填统计:")
    for k in ("boards_fetched", "boards_failed", "weeks_written",
              "snapshots_total", "first_ts", "last_ts"):
        print(f"  {k}: {stats[k]}")
    if args.dry_run:
        print("(--dry-run 未写盘)")
    else:
        print(f"已写回 {chain.BOARD_SNAP_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())