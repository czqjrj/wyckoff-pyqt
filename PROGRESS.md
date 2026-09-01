# 进度记录：策略1优化（会话恢复用）

> 会话目标：优化策略1 → 跑回测 → 评估是否"有价值"
> 日期：2026-09-01

## 结论（已完成）
策略1 **确实有价值**。放宽 `buy_passed` 门槛后，信号从 1 笔 → 9~15 笔，
胜率 50~56%、正期望（平均每笔 +0.5%~+1.25%）、盈亏比 ~1.3~1.6。

**放宽方向 vs 收紧方向**（重要教训）：
原策略1 `buy_passed>=7` 门槛导致信号近零（28只×76采样点中 buy7 仅占 2%）。
越收紧越没信号，**应放宽而非收紧**。

## 关键数据
`buy_passed` 在 608 个采样点的占比：
- >=7: 2.0% (信号几乎为零)
- >=6: 13.3%
- >=5: 45.1%
- >=4: 78.3%

## 28只股票×500天 扫描结果（自带交易纪律：同持3/持20K/-5%止损/+15%止盈）
| 条件 | n笔 | 胜率 | 平均/笔 | 总收益 | 盈亏比 | 最大回撤 |
|------|-----|------|---------|--------|--------|----------|
| 基线 buy>=7 | 1 | 0% | -5.40% | -5.40% | 0.00 | 0% |
| **A: buy>=6** | 9 | 55.6% | +1.00% | +7.73% | 1.55 | -10.2% |
| buy>=5 | 15 | 46.7% | +0.54% | +5.20% | 1.23 | -24.0% |
| buy6+conf75+win15 | 6 | 50.0% | +0.59% | +2.68% | 1.32 | -5.4% |
| **B: buy>=5+VR1.4+pos.8** | 13 | 53.8% | +1.25% | +14.29% | 1.60 | -19.7% |
| buy6+VR1.3 | 9 | 55.6% | +1.00% | +7.73% | 1.55 | -10.2% |

**最优候选**：
- A = `min_buy_passed=6`：最简单稳健，55.6%胜率，回撤小（-10%）
- B = `min_buy_passed=5, min_vr=1.4, max_position=0.8`：平均每笔最赚(+1.25%)、总收益最高(+14.29%)但回撤大(-19.7%)

## ⚠️ 下一步（待完成）
1. **用更大股票池（40+只）验证 A 和 B 的稳健性**（上次跑到一半被用户中止）
   - 已把 `strategy1_opt_sweep.py` 的 STOCKS 扩展到 46 只、variants 缩为 4 个（基线/A/B/D）
   - 命令：`python3 strategy1_opt_sweep.py`（耗时较长，建议后台跑或分片）
2. 选定最优参数后，**落库为 `evaluate_strategy_1` 的默认值**（当前默认仍是基线 buy>=7！）
   - 改 `wyckoff_strategies_manager.py` 的 `evaluate_strategy_1` 中 `p` 默认 dict
   - 例如最终选 A：`"min_buy_passed": 6`
3. 跑完整 `three_strategies_backtest.py` 看四大策略整体。
4. 需要时提交 git（当前 wyckoff_strategies_manager.py / wyckoff_simulated_trading.py 未提交）

## 涉及文件
- `wyckoff_strategies_manager.py`：已重构 `evaluate_strategy_1(df,i,wevents,nt,vsa_labels,params=None)` 支持参数扫描；默认仍是基线。
- `strategy1_opt_sweep.py`：**新建**（未跟踪），策略1条件优化扫描脚本，含交易纪律回测与汇总。
- 未跟踪：`three_strategies_backtest.py`、`three_strategy_backtest_data/`
- 未提交改动：`simulation_results.json`、`three_strategy_data/performance_history.json`

## 本会话先前已完成（背景）
- 模拟盘纪律策略已加入策略管理器 = 策略4（`evaluate_strategy_4`，强多头 conf≥90 + 硬门禁）
- `_trading_discipline()` 统一交易纪律（同持3/持20K/-5%止损/+15%止盈/结构破位/cost0.4%），前3策略均已附带 `trading` 字段
- 类名 `ThreeStrategyManager` → `WyckoffStrategyManager`（3处文件同步改）
- 每个策略信号新增可读 `name` 字段（4个策略）
