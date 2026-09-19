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

---

# 进度记录：5.3 三道硬门禁·历史消融 (2026-09-18 会话收尾)

> 目的一：用数据闭合 docs 里 ③号盲区「三道硬门禁从未被回测验证」。
> 详见 **`docs/paper_gate_ablation.md`**（8组合分项开→关对照）
> 脚本：`scripts/paper_gate_ablation.py`（可复现）

## 消融结论（24白马池·Spring-only·conf≥100·止损-4%·止盈+30%·240分钟K历史回放）
| 门禁组合 | 条件 | 平仓 | 胜率 | 累计 | 盈亏比 | 回撤 | 平均/笔 |
|---|---|---|---|---|---|---|---|
| none 三门全关(基线) | 事件即买 | 63 | 54.0% | +216.45% | 4.12 | -13.36% | +10.085% |
| **flow 仅资金流** | 资金 | 61 | 59.0% | **+269.41%** | 4.58 | **-7.59%** | +11.773% |
| mkt 仅大盘MA20 | 大盘 | 28 | 64.3% | +122.02% | 4.83 | -7.58% | +15.613% |
| sect 仅板块强度 | 板块 | 63 | 54.0% | +216.45% | 4.12 | -13.36% | +10.085% |
| all 三门齐开(生产) | 大盘+资金+板块 | 27 | 66.7% | +126.53% | 4.57 | -7.61% | +16.457% |

## 一句话结论（三个"现在知道"）
1. **flow 资金流门正贡献唯一始终成立** — 较基线收益 +216%→+269% 且最大回撤 -13.4%→-7.6% 双改善 → 生产保持开启。
2. **mkt 大盘门胜率↑但累计腰斩**（+216%→+122%，笔数 63→28 过度过滤）→ 大盘门与纪律 Spring 自身入场点高度重叠，靠单一大盘 MA20 既不必要也伤量；保留仅作风控回撤削平。
3. **sect 板块门在历史回测中空转（= 基线逐项完全一致）** → 因个股所挂板块（饰品/专业工程等）在 2026-08 前的历史快照无强度记录 → `strength_at` 恒 fail-open 放行。**板块门历史贡献至今无档案能实证**（与 ③号盲区表述一致，现已量化出自证：该门在 2023→2026-07 窗口内从不拦截）。

## 已落地
- `scripts/paper_gate_ablation.py`：mkt/flow/sect 分项开→关 × 8 组合单人账户回放，事件+板块门禁专用抓取口径，`--report` 写 md。
- `docs/paper_gate_ablation.md`：对照表 + 口径 + 结论。
- 板块历史快照（2026-08 起）：`scripts/backfill_board_snap.py --start 2019-01-01` 已验证有整段往返（373 快照跨度 2019→今），swift 消融所需的历史板块强度在池内已可取得（饰品等仅有近两周快照 → 历史 fail-open）。

## 续跑入口
- 消融: `python scripts/paper_gate_ablation.py --report docs/paper_gate_ablation.md`
- 板块历史回填: `python scripts/backfill_board_snap.py --start 2019-01-01 --report docs/backfill_board_snap.md`
- 网格: `python scripts/paper_replay_bt.py --max-codes 200 --qlib-veto --qlib-veto-hi 0.60`

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

---

# 进度记录：5.4 P&F 概率校准线 + 胜率库性能/断链修复 (2026-09-19)

## P&F 三档目标概率·低概率端收缩 (落地 + 实测)
- **完成进行中的 `pnf.py` 折扣实现**: `_low_prob_discount` 抽出为模块级函数
  (p<0.60 全档 ×0.70/0.85/0.94, 0.60→0.70 线性回 1.0, ≥0.70 不动), 6 处调用补 tier_key。
- **档位顺序守卫 `_enforce_tier_order`**: 折扣 (保守最重) 把接近的三档概率翻转成倒序
  (实测 791/1216 违规)。守卫只降不升、收敛到最保守档, 修复后 →226 (≈基线取整噪音)。
  同时在 `_apply_zone_calibration` 末再守一遍 (派发下方 ×1.05/1.08/1.15 升序系数也会翻转)。
- **实测 (608段=合成530+真实78)**: `<50%` 档校准差从 +6~15pt 收敛到 ±2pt 内;
  真实股票子集 (n=78) 低概率端两方向均 +24~32pt 高估, 折扣是正确方向且系数与
  归档实证 (09-10/09-15) 完全对齐。`eval_pnf_tier_accuracy.py` 联动。
- 测试: `tests/test_pnf_target_prob.py` (折扣单调/档序/过渡；顺序守卫只降不升)。

## 胜率库性能 + 断链修复
- **5.13 工程债部分落地**: `load_win_rates` cache miss 时一次解析信号库补齐
  5/10/20/40 全部周期 (原逐周期各 parse 12MB JSON), 冷算 4 周期 ~1s→0.54s, 热取 0。
- **实测胜率断链修复**: `entries.measured_win_rates` 误把 `scale=` 传给无此参数的
  `load_win_rates` → 恒抛 TypeError 被吞 → 恒返回 `{}` → 入场扫描 win_rate/win_n
  恒空。改为 `load_win_rates(20)`, 扫描行现在真实带上收缩胜率与样本数。
  (回归测试 `test_measured_win_rates_scale_bug`。)

## 其他
- `wyckoff_cache.db` (118MB, kline_cache 5427 行, WAL) 排查: 正常按 (symbol,scale)
  的持久 K 线缓存, 无膨胀/陈旧, 不动。
- paper 实盘/回测口径 (accuracy_improvements_todo 5.8): 生产默认已对齐
  (stop=4%/TP=30%/trail=6%/maxpos=5/conf=100), 无需再改。
- 全量回归: 720 passed; ruff 干净。

---

# 进度记录：5.5 在线校准模型质量监管 (2026-09-19)

> 对应 `accuracy_improvements_todo.md` 5.7 (模型退化). 用数据回答"模型该不该信",
> 并把"信多少"从拍脑袋的 0.55 硬开关改成 连续质量权重 + 轨迹趋势监控。

## 归因: AUC 0.56→0.68 回升的主因是标签集翻倍 + 指标自身极不稳定
- 09-15 文档快照 `n_train=6524, auc_oos=0.5614`; 现重训 (临时路径, 不碰生产) 得
  `n_labels=12338 / n_train=8636 / n_oos=3702`, 日期跨度 2004-09→2026-08。
- **标签翻倍原因**: 信号基座 99→173 标的扩容 + `record_events_batch` 全事件无偏入库
  (commits 6d90a4e), 让带特征+已评估标签的行从 ~6.5k → ~12.3k。
- **指标对 seed 极不稳定 (关键发现)**: 同数据集 seed=0/7/123 → AUC 0.70~0.71,
  seed=2024 → 0.62 (±0.09); oos_frac=0.2/0.3/0.4 → 0.72/0.62/0.67。生产 0.679 只是
  seed=42 的单点样本 → **单次重训的 AUC 不能作为接管/放弃的判据**, 必须看轨迹与区间。

## 落地 (wyckoff/online_model.py)
1. **门槛实证上调**: `MODEL_MIN_AUC` 0.55 → 0.60 (接线仍卡) — `_ready`/校准中心联动。
2. **连续质量权重**: 新增 `_auc_scale` (0.50→0, 0.65→1 线性), `_blend_weight` 改为
   `样本爬坡 × AUC 质量标尺`: 踩线 0.60 时权重 ~0.47 (原满 0.70) — 区分度而非只有样本量决定权重。
3. **连续劣化折减 + 告警**: 每次重训追加 `history[]` (截断 MODEL_HISTORY_MAX=20);
   `_degrade_factor` 当近 2 次 AUC 均值 ≤ 既往 4 次均值 -0.03 → 接管权重 ×50%;
   `_quality_overlay` 产出 `degraded` + `warnings` (低于下限/缺样本/连续劣化三种)。
4. **展示/排查**: `model_status` 增 `blend_eff` (生效权重) 与 `warnings`; 校准中心
   模型 tab 增红色告警横幅 + 门槛文案动态化; CLI 增 `python -m wyckoff.online_model --history` 打印轨迹。
5. 测试: `tests/test_online_model.py` +7 (标尺单调/权重质量化/劣化折减/告警/历史截断/接管幅度减半)。
   全量回归 726 passed; ruff 干净。

## 生产行为影响 (重要)
- 当前生产模型 (09-17, auc=0.679) 无历史 → 折减系数 1.0, 权重仍 0.70, **行为不变**;
  等 cron 每日重训开始自然积累 history 后轨迹监控才生效。特征集/样本不变时, 0.679≥0.60 仍达标。

## 续跑入口
- 轨迹: `python -m wyckoff.online_model --history`
- 状态: `python -m wyckoff.online_model --status`
- 归因复跑: `/tmp/opencode/om_probe.py` (临时路径探针, 不碰生产文件)
