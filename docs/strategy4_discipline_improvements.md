# 主策略4·纪律 — 提升空间评估与续跑清单（跨机续作用）

- 生成: 2026-09-15
- 目标: 评估 `paper_discipline_bull`（策略4·纪律，当前**唯一默认启用**的主策略）是否还有提升空间，并落成可在另一台机器继续执行的清单。
- 结论: **有**。存在若干已量化、但尚未落地的提升点（第 2 节），另有已证伪项不必再试（第 3 节）。

---

## 1. 现状（生产默认参数）

选股（入场）:
- 事件集 `{Spring, ST, LPS}`，`MIN_CONF=100`，事件窗口近 10 根
  - `wyckoff/paper/__init__.py:315`（paper 生产口径，已收紧）
  - `wyckoff/strategies/constants.py:9,35`（策略管理器口径 5 事件 / 窗口 10）
- 三道硬门禁（缺一不可，fail-close）：大盘 MA20 向上 · 板块强度 >60 分位 · 资金流近5日净流入 >0
  - `wyckoff/discipline.py:16-20`、`wyckoff/strategies/evaluators.py:30`

出场 / 风控纪律（`wyckoff/paper/_params.py`）:
| 参数 | 值 | 行 |
|---|---|---|
| 止损 `STOP_LOSS` | -4% | :37 |
| 移动止盈 `TRAILING_STOP` | 开 | :41 |
| 移动回落 `TRAIL_BACK_PCT` | 8% | :45 |
| 止盈 `TAKE_PROFIT` | +15% | :74 |
| 持有上限 `HOLD_BARS` | 20 | :15 |
| 同持 `MAX_POSITIONS` | 3 | :17 |
| 弱市过滤 `WEAK_FILTER` | 开（→1仓+停VA） | :49 |
| 价值吸筹 `ENABLE_VA` | False | :56 |
| 左侧买点 `ENABLE_LONG_LEFT` | False | :58 |
| conf 下限 `MIN_CONF` | 100 | :81 |

> 注意：VA / 左侧已默认关闭，纪律策略实际是**单主策略**。

---

## 2. 提升点（按证据强度排序，均尚未落地）

### ① 移动止盈参数没跟上最新网格最优 —— 最大、最直接
- `docs/replay_improvements_v2.md:33`（2026-09-12，修复风控口径后 · 纪律-only · 200 只全A · 80 格）:
  最优 **STOP=-4% · TP=+30% · trail_back=6% → 累计 +224.9%** · 胜率 46.2% · 盈亏比 2.43 · CAGR +43.3%。
- 当前仍是 **TP=15% · trail=8%**（`_params.py:74,45`）→ 止盈未放宽、回落未收紧。
- 旁证: `docs/paper3_backtrader_bt_improvement.md:36` 产品口径推荐 `--stop 0.04 --trail 0.06`（回落 6% 一致）。
- **动作**: 先在产品口径复测 `TP=30% / trail=6%`，通过则改 `_params.py:45,74` 默认值。
- **注意**: v2 网格为"风控门禁放开"的研究口径，回撤 -73.8%，量级不可直接当实盘账户预期，必须按 `paper3_backtrader_bt.py` 产品口径复验。

### ② QLib 卖出端否决未启用
- `docs/replay_improvements_v2.md:14,53-56`: 开否决后胜率 38→45%、回撤 -21.3→-19.1%，方向正确但幅度小。
- 模型已于 2026-09-13 升级（AUC 0.6467→0.6631，`docs/qlib_v4_progress.md`）。
- **动作**: 用新模型重跑 `--qlib-veto --qlib-veto-hi 0.60`，确认无损伤后再考虑默认开。

### ③ 三道硬门禁从未被回测验证（盲区）
- `docs/replay_improvements_v2.md:83`: 板块强度/资金流历史快照缺失，`sect_gate` 两次回测**均未施加**。
- 即"板块>60分位 + 资金净流入>0"是否有正贡献，目前**无实证**。
- **动作**: 回填历史板块快照（及资金流），做门禁消融（开/关、分项），确认是否过度过滤。

### ④ 事件集 / 仓位可分层微调
- `docs/three_strategy_analysis.md:38`: conf≥100 口径正确（100+ 胜率 79% vs 90-99 的 66%），维持。
- 事件层面 Spring/ST 明显强于 SC/Shakeout（`docs/three_strategy_analysis.md:18-26`）。
- `docs/realtime_bt_report.md:16-29`: 真实逐日持仓下，持有 2 支收益最优、3~4 支风险调整最优、单吊最差；当前 3 支为折中。
- **动作**: 可按事件分别设止盈/仓位（而非全局一套）；按行情段（牛/熊/震荡）评估 maxpos 是否应动态化。

---

## 3. 已证伪 / 不建议再试

| 项 | 结论 | 出处 |
|---|---|---|
| 已确认事件买点 `disc-confirm` | 劣化：on -14.4% vs off +75.0%，过度过滤 | `docs/replay_improvements_v2.md:13,40-46` |
| 价值吸筹回退 | 纯负贡献（56 笔 -53%），已默认关闭 | `docs/replay_improvements_v2.md:18,68-74` |
| 放宽 conf 阈值到 <100 | 100+ 段胜率 79% vs 90-99 段 66%，维持 100 | `docs/three_strategy_analysis.md:38` |
| 持仓上限 3→5 | 因池而异，默认维持 3 | `docs/replay_improvements_v2.md:15,59-66` |

---

## 4. 续跑命令（在另一台机器复现）

```bash
# 网格（默认 stops/tps/trail-backs 已覆盖 v2 最优格）
python scripts/paper_replay_grid.py --max-codes 200 --conf 100 \
  --stocks-cache <cache> --report docs/paper_replay_grid.md --export docs/paper_replay_grid.csv

# 单次回放（指定 4%/30%/trail6%）
python scripts/paper_replay_bt.py --stop 0.04 --tp 0.30 --conf 100 \
  --report docs/paper_replay_bt_0430.md

# 产品口径（三策略引擎 / backtrader）复验 trail 6%
python scripts/paper3_backtrader_bt.py --stop 0.04 --tp 0.30 --trail 0.06 \
  --report docs/paper3_backtrader_bt_tp30.md

# QLib 否决（新模型）
python scripts/paper_replay_bt.py --qlib-veto --qlib-veto-hi 0.60 \
  --report docs/paper_replay_veto_v4.md
```

> 脚本参数见 `scripts/paper_replay_grid.py`、`scripts/paper_replay_bt.py`、`scripts/paper3_backtrader_bt.py`。
> 复测通过后再改 `wyckoff/paper/_params.py` 默认值，并同步 `wyckoff/settings_keys.py` 与相关单测。

---

## 5. 相关文件

- `wyckoff/paper/_params.py` — 生产默认参数（止损/止盈/移动/持仓/开关）
- `wyckoff/paper/__init__.py:315` — 生产事件集 `{Spring,ST,LPS}`
- `wyckoff/paper/_selection.py` — 选股编排与三道门禁落地
- `wyckoff/discipline.py` — 门禁阈值单一数据源
- `wyckoff/strategies/evaluators.py:62` — `evaluate_strategy_4` 策略定义
- `docs/replay_improvements_v2.md` — 最新网格/否决/VA 对照（2026-09-12）
- `docs/three_strategy_analysis.md`、`docs/realtime_bt_report.md`、`docs/paper3_backtrader_bt_improvement.md`
- `docs/qlib_v4_progress.md` — QLib 模型升级（AUC 0.6631）

*历史回测与统计推断，不构成投资建议。*
