# Qlib 模型升级 V4 — 进度存档 (会话恢复用)

- 更新: 2026-09-13 08:20
- 目标 (来自 `docs/replay_improvements_v2.md` ⑥): 在现网模型 **AUC 0.6467** 基础上继续提升, 重训后写回 `qlib_lgbm.joblib`
- 状态: **核心已完成** — 消融/超参/阈值实验出结论, 全池重训写回, 回归验证通过

## 重训结果 (qlib_lgbm.joblib @ 2026-09-13)

| 项 | 旧 (基线) | 新 (V4) |
|---|---|---|
| AUC (val) | 0.6467 | **0.6631** (+0.016) |
| acc | 0.6042 | 0.6140 (base_rate≈0.488) |
| 特征集 | alpha+domain (147) | alpha+domain (**184**) — **market 特征否决** |
| 超参 | num_leaves=24 | num_leaves=48 |
| 标签阈值 | |abs(5日收益)|≥1% | **≥2%** |
| 训练区间 | 2019-01-02 ~ 2026-06-26 | 2019-01-01 ~ 2026-09-10 |
| seed 集成 | 单模型 | 5 seed 平均 (推理仍走 `model.predict_proba`, 用 seed0 booster) |

## 现网模型基线 (旧, 存档)

| 项 | 值 |
|---|---|
| horizon | 5 |
| AUC | 0.6467 (h=10: 0.6290, h=20: 0.6083) |
| acc | 0.6042 · base_rate 0.4850 |
| n_train / n_valid | 245458 / 61365 |
| n_features | 147 (125 Alpha158 + 22 wy_*) |
| 池 | 215 只 (`data/train_pool.txt`) |
| 区间 | 2019-01-02 ~ 2026-06-26 |
| Top5 | wy_ev_conf20, wy_ev_net20, IMIN20, wy_ev_bear20, VWAP0 |

## 实验结论 (60 池缓存, horizon 5)

1. **消融 (thr=1%)**: domain 特征是大头 (+0.52→0.625); **market 特征全线负增益**
   (h5 0.6250→0.6147, h10 0.6018→0.5924, h20 0.5909→0.5598); `+own` 也未能反超。
   → 生产特征集保持 **alpha + domain**, 不做 mk_*。
2. **超参扫描** (alpha+domain, seeds×5): baseline 0.6255; num_leaves=48 最优 +0.002
   (nl16 -0.003 / ff0.5 -0.001 / lr/mcs/ff 平); 5-seed 集成 +0.0005。超参空间基本榨干。
3. **标签阈值扫描** (alpha+domain, seeds×5): thr 1%→0.628, **2%→0.651**, 3%→0.673
   (样本随之收缩 va≈9.4k)。取 **2%** 平衡样本量与区分度。
4. **全池重训** (215 池, 392,964×208, 缓存构建 144s): AUC 0.6631 · acc 0.6140 ·
   feats=184 (158 alpha + 26 domain, 未再做相关剔除)。

## 已完成

1. **实验脚手架** `scripts/qlib_train_v4.py`:
   - `build`: Alpha158 + 威科夫领域特征 + 市场上下文特征 (`mk_*`) + 标签 `__ret{5,10,20}`, 缓存 pickle
   - `ablate`: 对比 alpha / +domain / +market / +own, 各 horizon/r 阈值出 AUC
   - `train`: 多 seed 平均训练并写回 `QLIB_MODEL_FILE` (特征集=alpha+domain, `--threshold` 默认 0.02)
2. **60 只缓存** `data/qlib_train_cache/qlib_cache_60.pkl` 已落盘 (gitignore)
3. **215 只全池缓存** `data/qlib_train_cache/qlib_cache_215.pkl` 已落盘 (gitignore, 144s)

## 回归验证 (通过)

- `pytest tests/test_qlib_adapter.py`: **9 passed**
- `scripts/qlib_ablation.py` ON vs OFF (自选股池, 方向命中):
  - ON 53.3% / 53.4% / 52.9% (n=1031) vs OFF 54.3% / 54.3% / 53.0% (n=1265) — **持平不劣化**
  - 信号数 ON 略少于 OFF (否决路径过滤弱信号), 命中率未下降
- 推理冒烟: `qlib_signal_probability('sh600104')` → prob_buy 0.814 (>0.6 否决阈值) 正常

## 待办 / 后续可选

- [ ] 实盘观察新模型对交易的实质影响 (prob 分位分布更拉开, 校准档位是否需要重调)
- [ ] 若 AUC 再提升: 可试更精细的买入概率分位归一化 `_spread_probabilities` `spread` 上限
- [ ] 可选: 模型对象已含 `booster_ensemble`, 推理侧要启用集成需改 `qlib_probability_series`
- [ ] 更新 `docs/replay_improvements_v2.md` ⑥

## 注意 (续接时)

- 新模型特征集 = alpha + domain (184), 推理侧 `qlib_signal_probability` 自动 join `wy_*` 域特征后按 `feature_names` 重排, 无额外改动。
- `train()` 保存了 `booster_ensemble`, 但 `qlib_probability_series()` 目前只走 `model.predict_proba` (单模型 seed0)。
