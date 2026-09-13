#!/usr/bin/env python3
"""Qlib 模型升级 V4: 实验/训练脚手架 (市场上下文特征 + 消融 + 落盘)。

背景 (docs/replay_improvements_v2.md ⑥): 现网模型 AUC 0.6467 (horizon=5,
Alpha158 + 26 个威科夫领域特征, 215 只池)。本脚本用于继续提升:
在现有特征基础上加入「大盘/相对强度」上下文特征 (mk_*), 做消融对比,
胜出后用全池重训并写回 qlib_lgbm.joblib。

数据流:
  build   ├─ 拉 Alpha158 + domain(wy_*) + 市场特征(mk_*) + 标签 __ret{5,10,20}
          └─ 缓存为 pickle (体积大, 存 data/ 下, 已被 .gitignore 忽略)
  ablate  └─ 直接读缓存, 对比 {alpha} / {alpha+domain} / {+market} / {+own}
  train   └─ 读缓存 (或实时构建), 训练最优配置并保存

用法:
  python scripts/qlib_train_v4.py build --n 215
  python scripts/qlib_train_v4.py ablate
  python scripts/qlib_train_v4.py train --save

注: qlib 原始预测经 _spread_probabilities 做样本内分位归一化 (研究口径),
训练集/验证集按时间切分 (sort_index 后 80/20), 特征仅用截至当日滚动值。
"""
import argparse
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DEFAULT_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "qlib_train_cache", "qlib_cache.pkl")
START = "2019-01-01"
END = "2026-09-10"
HORIZONS = (5, 10, 20)

BASE_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "boosting_type": "gbdt",
    "learning_rate": 0.03,
    "num_leaves": 48,  # 消融后超参扫描: 24→48 微升 (60池 +0.001~0.002)
    "max_depth": 6,
    "min_child_samples": 40,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "feature_fraction": 0.7,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
    "num_threads": 8,
    "seed": 42,
    "is_unbalance": True,
}


def _qlib():
    from wyckoff import qlib_adapter
    return qlib_adapter


def build_market_features(qa, start=START, end=END):
    """大盘 (上证 SH000001) 因果特征, 按交易日索引。"""
    raw = qa._fetch_df("SH000001", start, end, ["$close", "$volume"])
    idx = qa._as_single(raw)
    close = idx["close"].astype(float)
    r1 = close.pct_change()
    mf = pd.DataFrame(index=close.index)
    mf["mk_idx_ret1"] = r1
    for w in (5, 10, 20, 60):
        mf[f"mk_idx_ret{w}"] = close.pct_change(w)
    ma20 = close.rolling(20).mean()
    mf["mk_idx_bias20"] = close / ma20 - 1
    mf["mk_idx_slope20"] = ma20 / ma20.shift(5) - 1
    mf["mk_idx_vol20"] = r1.rolling(20).std()
    mf["mk_idx_ret1_abs"] = r1.abs()
    mf["mk_idx_above_ma20"] = (close > ma20).astype(float)
    return mf


def build(n, out, start=START, end=END):
    qa = _qlib()
    pool = qa.load_train_pool()[:n]
    print(f"build pool={len(pool)} -> {out}", flush=True)
    market = build_market_features(qa, start, end)
    frames, t0 = [], time.time()
    for i, sym in enumerate(pool):
        try:
            f, _ = qa.fetch_alpha158_features(sym, start, end)
            d, _ = qa.compute_domain_features(sym, start, end)
            if f is None or len(f) < 80 or d is None or len(d) < 80:
                continue
            df = f.join(d, how="inner")
            cdf = qa._as_single(qa._fetch_df(sym, start, end, ["$close"]))
            close = cdf["close"].astype(float).reindex(df.index)
            df = df.copy()
            df["__symbol"] = sym
            for h in HORIZONS:
                df[f"__ret{h}"] = close.shift(-h) / close - 1
            df["__close"] = close
            for w in (5, 10, 20, 60):
                df[f"__ret{w}_own"] = close.pct_change(w)
            df["__vol20_own"] = close.pct_change().rolling(20).std()
            mk = market.reindex(df.index)
            for w in (5, 20, 60):
                df[f"mk_rel_ret{w}"] = df[f"__ret{w}_own"] - mk[f"mk_idx_ret{w}"]
            df["mk_rel_bias20"] = (
                close / close.rolling(20).mean() - 1 - mk["mk_idx_bias20"])
            df = df.join(mk, how="left")
            frames.append(df)
        except Exception as e:
            print(f"  {sym} FAIL {type(e).__name__}: {e}", flush=True)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(pool)} {time.time()-t0:.0f}s", flush=True)
    data = pd.concat(frames, axis=0)
    print(f"matrix {data.shape} in {time.time()-t0:.0f}s", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as fh:
        pickle.dump({"data": data, "pool": pool}, fh, protocol=4)
    print("saved", out, flush=True)


def feature_groups(cols):
    alpha = [c for c in cols if not c.startswith(("wy_", "mk_", "__"))]
    domain = [c for c in cols if c.startswith("wy_")]
    market = [c for c in cols if c.startswith("mk_")]
    own = [c for c in cols if c.endswith("_own")]
    return alpha, domain, market, own


def run(data, feats, horizon=5, threshold=0.02, test_ratio=0.2,
        params=None, n_rounds=500, seeds=(42,), drop_redundant=True):
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    params = dict(BASE_PARAMS if params is None else params)
    col = f"__ret{horizon}"
    tmp = data.dropna(subset=[col]).copy()
    mask = tmp[col].abs() >= threshold
    tmp = tmp[mask].copy()
    tmp["__label"] = (tmp[col] > 0).astype(np.int8)
    tmp = tmp.sort_index()
    feats = [f for f in feats if f in tmp.columns]
    if drop_redundant:
        fc = tmp[feats].corr().abs()
        up = fc.where(np.triu(np.ones(fc.shape), k=1).astype(bool))
        drop = set()
        for c in up.columns:
            hs = up.index[up[c] > 0.95].tolist()
            if hs:
                cand = [c] + hs
                var = {x: tmp[x].var() for x in cand if x in tmp.columns}
                keep = max(var, key=var.get)
                drop.update(set(cand) - {keep})
        feats = [f for f in feats if f not in drop]
    n = len(tmp)
    split = int(n * (1 - test_ratio))
    tr, va = tmp.iloc[:split], tmp.iloc[split:]
    Xtr, ytr = tr[feats].astype(float), tr["__label"].values
    Xva, yva = va[feats].astype(float), va["__label"].values
    preds = np.zeros(len(Xva))
    for s in seeds:
        p = dict(params, seed=s)
        dtr = lgb.Dataset(Xtr, label=ytr)
        dva = lgb.Dataset(Xva, label=yva, reference=dtr)
        b = lgb.train(p, dtr, num_boost_round=n_rounds, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
        preds += b.predict(Xva, num_iteration=b.best_iteration)
    preds /= len(seeds)
    auc = roc_auc_score(yva, preds)
    return auc, len(feats), len(Xtr), len(Xva)


def ablate(cache, threshold=0.02):
    with open(cache, "rb") as f:
        data = pickle.load(f)["data"]
    alpha, domain, market, own = feature_groups(list(data.columns))
    print(f"groups: alpha={len(alpha)} domain={len(domain)} "
          f"market={len(market)} own={len(own)}  threshold={threshold}")
    sets = {
        "alpha": alpha,
        "alpha+domain": alpha + domain,
        "alpha+domain+market": alpha + domain + market,
        "alpha+domain+market+own": alpha + domain + market + own,
    }
    for h in HORIZONS:
        print(f"\n--- horizon {h} ---")
        for name, feats in sets.items():
            auc, nf, ntr, nva = run(data, feats, horizon=h, threshold=threshold)
            print(f"  {name:28s} auc={auc:.4f} feats={nf} tr={ntr} va={nva}")


def train(cache, save, seeds, horizon=5, threshold=0.02):
    import joblib
    from sklearn.metrics import accuracy_score, roc_auc_score

    with open(cache, "rb") as f:
        data = pickle.load(f)["data"]
    alpha, domain, market, own = feature_groups(list(data.columns))
    # 消融结论: market 特征全 horizon 负增益 → 生产特征集 = alpha + domain
    feats = alpha + domain
    col = f"__ret{horizon}"
    tmp = data.dropna(subset=[col]).copy()
    tmp = tmp[tmp[col].abs() >= threshold].copy()
    tmp["__label"] = (tmp[col] > 0).astype(np.int8)
    tmp = tmp.sort_index()
    feats = [f for f in feats if f in tmp.columns]
    n = len(tmp)
    split = int(n * 0.8)
    tr, va = tmp.iloc[:split], tmp.iloc[split:]
    Xtr, ytr = tr[feats].astype(float), tr["__label"].values
    Xva, yva = va[feats].astype(float), va["__label"].values
    preds = np.zeros(len(Xva))
    boosters = []
    for s in seeds:
        import lightgbm as lgb
        p = dict(BASE_PARAMS, seed=s)
        dtr = lgb.Dataset(Xtr, label=ytr)
        dva = lgb.Dataset(Xva, label=yva, reference=dtr)
        b = lgb.train(p, dtr, num_boost_round=500, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
        boosters.append(b)
        preds += b.predict(Xva, num_iteration=b.best_iteration)
    preds /= len(seeds)
    auc = float(roc_auc_score(yva, preds))
    acc = float(accuracy_score(yva, (preds > 0.5).astype(int)))
    print(f"train horizon={horizon} thr={threshold} auc={auc:.4f} acc={acc:.4f} "
          f"feats={len(feats)} seeds={len(seeds)}")
    if save:
        qa = _qlib()
        best = boosters[0]
        obj = {
            "model": best,
            "booster_ensemble": boosters if len(boosters) > 1 else None,
            "feature_names": feats,
            "horizon": horizon,
            "auc": auc,
            "acc": acc,
            "buy_class": 1,
            "n_features": len(feats),
            "trained_at": pd.Timestamp.now().isoformat(),
        }
        joblib.dump(obj, qa.QLIB_MODEL_FILE)
        print("saved", qa.QLIB_MODEL_FILE)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--n", type=int, default=215)
    b.add_argument("--out", default=DEFAULT_CACHE)
    b.add_argument("--start", default=START)
    b.add_argument("--end", default=END)
    a = sub.add_parser("ablate")
    a.add_argument("--cache", default=DEFAULT_CACHE)
    a.add_argument("--threshold", type=float, default=0.02)
    t = sub.add_parser("train")
    t.add_argument("--cache", default=DEFAULT_CACHE)
    t.add_argument("--save", action="store_true")
    t.add_argument("--seeds", type=int, default=3)
    t.add_argument("--horizon", type=int, default=5)
    t.add_argument("--threshold", type=float, default=0.02)
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.n, args.out, args.start, args.end)
    elif args.cmd == "ablate":
        ablate(args.cache, args.threshold)
    else:
        train(args.cache, args.save, tuple(range(42, 42 + args.seeds)),
              args.horizon, args.threshold)


if __name__ == "__main__":
    main()
