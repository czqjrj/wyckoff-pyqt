#!/usr/bin/env python3
"""刷新 Qlib 训练股票池 data/train_pool.txt。

从东财 A 股实时行情取全市场成交额, 按成交额降序取 Top N 流动性样本,
统一为 sh/sz 前缀后写盘 (每行一个), 供 ``wyckoff.qlib_adapter.load_train_pool``
读取训练/扩充股票池。

若网络不可用 (东财接口被限流等), 保留现有 pool 文件并提示。

用法:
  python scripts/refresh_train_pool.py [--top 220]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_POOL_FILE = os.path.join(_ROOT, "data", "train_pool.txt")


def _normalize(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=220, help="按成交额取前 N 只 (默认 220)")
    ap.add_argument("--min-amount", type=float, default=5e8,
                    help="最低成交额过滤 (元, 默认 5 亿)")
    args = ap.parse_args()

    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
    except Exception as e:
        print(f"东财全市场列表不可用: {type(e).__name__}: {e}")
        print(f"保留现有 {_POOL_FILE} 不动")
        return 1

    df = df.dropna(subset=["成交额"])
    df = df[df["成交额"] >= args.min_amount]
    pool = sorted({_normalize(c) for c in df.nlargest(args.top, "成交额")["代码"]})

    os.makedirs(os.path.dirname(_POOL_FILE), exist_ok=True)
    with open(_POOL_FILE, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(pool) + "\n")
    print(f"写入 {len(pool)} 只 -> {_POOL_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())