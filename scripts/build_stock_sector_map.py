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

from wyckoff.fundamental import fetch_sector
from wyckoff.utils import normalize_symbol


def main():
    recs = json.load(open("wx_signal_accuracy.json", encoding="utf-8"))
    keys = sorted({str(r.get("code") or "") for r in recs})
    keys = [k for k in keys if k]
    print(f"信号库 {len(recs)} 条 / 去重标的高代码 {len(keys)} 个")

    out = {}
    cache = "wyckoff_stock_sector.json"
    if os.path.exists(cache):
        out = json.load(open(cache, encoding="utf-8"))
        print(f"复用已有缓存 {len(out)} 个")

    pending = [(k, normalize_symbol(k)) for k in keys if str(k) not in out]
    if not pending:
        print("全部已映射")
        json.dump({k: out[str(k)] for k in sorted(out)}, open(cache, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return

    def _one(item):
        code, sym = item
        try:
            return code, fetch_sector(sym)
        except Exception:
            return code, None

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
