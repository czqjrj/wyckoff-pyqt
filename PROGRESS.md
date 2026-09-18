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

## 本会话先前已完成（背景）
- 模拟盘纪律策略已加入策略管理器 = 策略4（`evaluate_strategy_4`，强多头 conf≥90 + 硬门禁）
- `_trading_discipline()` 统一交易纪律（同持3/持20K/-5%止损/+15%止盈/结构破位/cost0.4%），前3策略均已附带 `trading` 字段
- 每个策略信号新增可读 `name` 字段（4个策略）

---

# 进度记录：主策略4·纪律 提升空间评估（跨机续作用）

> 日期：2026-09-15
> 详见 **`docs/strategy4_discipline_improvements.md`**（现状参数 / 提升点 / 已证伪项 / 续跑命令 / 相关文件）

## 一句话结论
主策略4·纪律**有提升空间**，最大且未落地的点是 **移动止盈参数**：最新 80 格网格最优为
**STOP=-4% · TP=+30% · trail_back=6%（+224.9%）**，而生产仍是 **TP=15% · trail=8%**
（`wyckoff/paper/_params.py:74,45`）。其余为：QLib 卖出否决未启用、三道硬门禁从未被回测验证（历史快照缺失）、事件集/仓位可分层微调。

## 落地状态 (2026-09-17 会话收尾)

- **✅ 参数对齐 (校准 v2)**: 止盈 15%→30% (移动止盈激活线) / 追踪 8%→6% 已落
  `_params`+`config`+UI; storage 校准版本 1→2 对磁盘旧值一次性回迁, 版本对齐后
  用户主动调优仅告警不覆盖。
- **✅ QLib 卖出否决 (试点, 默认关)**: `_qlib_veto_exit` 接入 step/条件单
  (止盈/到期/追踪否决一次, 止损永不), 真实模型路径才生效; 回测参考
  `scripts/paper_replay_bt.py --qlib-veto`。
- **✅ 选股排序·期望融合**: `events.sort_candidates` 按方向化均值期望
  (edge_conf = conf ± 期望偏离映射) 取代纯 conf 降序; 缺样本退化为原 conf。
- **✅ 实盘验证点自动化**: `paper/_verify.py` run_cycle 每周期末观测
  (窗口胜率/累计期望/连败/盈亏比), 幂等推送, 只告警不改交易行为。
- 仍待办: 三道硬门禁历史快照回测 (bear_exit 门禁保持开启)、事件集/仓位分层微调。

## 续跑入口
- 网格: `python scripts/paper_replay_grid.py --max-codes 200 --conf 100`
- 单次: `python scripts/paper_replay_bt.py --stop 0.04 --tp 0.30 --conf 100`
- 产品口径: `python scripts/paper3_backtrader_bt.py --stop 0.04 --tp 0.30 --trail 0.06`
- QLib 否决: `python scripts/paper_replay_bt.py --qlib-veto --qlib-veto-hi 0.60`

---

# 进度记录：5.2 多周期共振门 + 事件+VSA 双因 (2026-09-18)

## 多周期共振门 (ENTRY_HTF_GATE) — ✅ 已证伪, 默认关闭
- survey `scripts/htf_gate_survey.py` (n=2419 强多头事件, 本地缓存回放, 无前视):
  周/月线合并偏空组命中率 68.6%(740条) **不低于**非偏空组 67.2%(320条),
  均值收益亦持平 —— 弹簧/震仓本就诞生于短期超跌环境, 高周期"偏空"不构成反向;
- fail-open 会拦下 30.6% 样本 (压线验收), fail-close 拦下 86.8% 严重过度。
- **落地**: `entries.ENTRY_HTF_GATE=False` 默认关闭, 保留实现+开关供未来口径重测;
  `multitime.htf_direction` 收敛为融合/入场两处唯一实现 (防漂移);
  `entries._scan_one` 顺带修复宏观因子污染 bug (板块强度/资金流原被首股写进共享
  macro_ctx 污染整批, 改为标的级就地计算)。
- 测试: `tests/test_entries_htf.py` (门开关/偏空拦截/偏多放行/fail-open 两档/单源收敛)。

## 事件+VSA 双因策略 (STRATEGY_EVENT_VSA, paper_enable_event_vsa) — ✅ 实现, 回测证伪, 默认关
- 口径: 强多头事件 conf≥85 ∧ 高价值VSA标签 {CHOC,DEM,SUP,TEST,SPR,SC} ∧ vr≥1.5 ∧
  与事件共时 ≤4 根; 独立赛道不受大盘门禁, 兜底最低优先
  (`wyckoff/strategies/candidates.event_vsa_candidate`)。
- 回测 (paper_priority_bt.py --event-vsa, 修复 evsa_on 未传入 replay_once 的断链):
  - 24 白马池: evsa 仅 4 笔, 25% 胜 / +0.0% 均值;
  - 133 只抽样池: evsa 5 笔, 0% 胜 / -6.3% 均值 —— 两池均不增厚, 维持默认停用。
- 落地: `_params`+`config`+`settings_keys`+`_conditions`/`_selection` 开关联动,
  UI 策略概览跟随, 候选层面兜底剔除; 测试 `tests/test_candidates_event_vsa.py` +
  `tests/test_paper_account.py` 开关两档。
- 仍待办: 三道硬门禁历史快照回测 / 事件集·仓位分层微调 (同上期)。

## 续跑入口
- HTF 门重测: `python scripts/htf_gate_survey.py` (需改 `entries.ENTRY_HTF_GATE=True`)
- 事件+VSA 回测: `python scripts/paper_priority_bt.py --event-vsa [--pool-size N]`
- 事件+VSA 口径调优: `wyckoff/strategies/constants.py` EVENT_VSA_MIN_CONF/MIN_VR/CO_WINDOW/HIGH_LABELS
