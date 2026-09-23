"""构建信号库全部标的的 code→行业 映射 (板块去重组合回测用)。

输出: 根目录 wyckoff_stock_sector.json {code: 东财行业名} (运行时数据, 已 gitignore)。

用法:
  python scripts/build_stock_sector_map.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.fundamental import _fetch_constituents_em, _load_board_map, fetch_sector


def main():
    recs = json.load(open("wx_signal_accuracy.json", encoding="utf-8"))
    keys = sorted({str(r.get("code") or "") for r in recs})
    keys = [k for k in keys if k]
    print(f"信号库 {len(recs)} 条 / 去重标的高代码 {len(keys)} 个")

    cache = "wyckoff_stock_sector.json"
    out = {}
    if os.path.exists(cache):
        out = json.load(open(cache, encoding="utf-8"))
        print(f"复用已有缓存 {len(out)} 个")

    # 优先走板块→成分股全量反解 (每板块一次批量请求, 远快于逐股 f127):
    # 个别环境股票级请求被墙时仍可完整建图; 已映射的股票跳过。
    pending = {k for k in keys if str(k) not in out}
    if pending:
        # 映射规则: 板块 fetcher 频控由内部信号量控制, 板块级并行即可
        def _board_job(item):
            name, bk = item
            try:
                stocks = _fetch_constituents_em(bk, limit=600) or []
            except Exception:
                stocks = []
            return name, stocks

        bmap = _load_board_map()
        hits = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            for name, stocks in ex.map(_board_job, sorted(bmap.items())):
                for code, _nm, _px in stocks:
                    c6 = str(code)[-6:]
                    if c6 in pending:
                        hits[c6] = name
        add = {c: s for c, s in hits.items() if c in pending}
        pending -= set(add)
        out.update(add)
        print(f"板块反解命中 {len(add)} 个 (剩余 {len(pending)} 个走逐股兜底)")

    def _one(code):
        try:
            return code, fetch_sector(code)
        except Exception:
            return code, None

    pending = sorted(pending)
    if pending:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, sec in ex.map(_one, pending):
                if sec:
                    out[str(code)] = sec
                    print(f"  {code} -> {sec}", flush=True)
                else:
                    print(f"  {code} -> (无)", flush=True)

    json.dump({k: out[k] for k in sorted(out)}, open(cache, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n完成: {len(out)}/{len(keys)} 个标的已映射 -> {cache}")


if __name__ == "__main__":
    main()
