# 准确率提升 · 待落实清单 (会话存档)

> 日期: 2026-09-15
> 来源: 梳理「威科夫结合什么能显著提高准确率」后, 逐项对照代码核实落地状态
> 用途: 会话恢复 / 排期; 每项含现状、依据、建议做法、验收口径

## 一、总览 (6 项准确率杠杆)

| # | 杠杆 | 状态 | 位置 |
|---|---|---|---|
| 1 | 前置环境门 (SC/BC/SOW 分桶调 conf) | ✅ 已落实 | `wyckoff/events.py:874-914` |
| 2 | 置信度校准模型 (含 VSA one-hot) | ✅ 已落实 (激活) | `wyckoff/online_model.py` |
| 3 | 强梯队 + 确认制 + conf 门 | ✅ 已落实 | `config.STRONG_TIER_TYPES` / `entries.py` / `paper/_selection.py` |
| 4 | 三重共振 (大盘 MA20+板块+资金) | ✅ 已落实 | `wyckoff/discipline.py` → `entries.py:94` / `paper/_selection.py:16` |
| 5 | 高价值 VSA 标签交叉验证 | ⚠️ 部分 (仅融合维度) | `wyckoff/fusion.py`；独立策略未入池 |
| 6 | 九大检验点 / 多周期 / 阶段先验 | ✅ 已落实 (分散) | `ninetests` / `multitime.py` / `feedback` |

结论: **5 项已落实, 1 项部分落实**; 另有 11 项工程/验证缺口待补。

---

## 二、已落实明细 (避免重复劳动)

1. **环境门** (`events.py:874-914`): SC 前置 20 根 ≤-15% → +8、>-8% → -15;
   BC ≥+15% → +8、≤+4% → -12、上升趋势内再 -5; SOW vol_ratio≥2.2 → +8、
   <1.6 → -8。记录 `feat.prior_r20` / `feat.sow_vol_gate`。测试 `tests/test_tags_audit.py`。
2. **校准模型**: `wx_online_model.json` 现 `ready=True`, `version=3`, `feat_version=3`,
   `n_train=6524`, `auc_oos=0.5614`, `acc_oos=0.5715`, `ic_oos=0.1196`; blend 上限 0.70,
   `events.py:675` 运行时接管。
3. **强梯队+确认制**: 仅 Spring/Shakeout/ST/LPS/SC; 确认后首根可交易 bar (`avail_idx`)。
4. **三重共振**: `discipline.py` 单一数据源 (大盘 MA20 斜率>0 / 板块>0.60 / 资金>0.50, fail-close)。
6. **九大检验点**: `strategies/manager.py:197`; 多周期: `multitime.py` + `fusion._htf_direction`,
   展示于 `conclusion.py:537`; 阶段先验在 `feedback/analysis`。

---

## 三、待落实清单

### P0 — 有现成代码但未接入交易链

- [ ] **5.1 高价值 VSA 独立策略入池 (策略2)**
  - 现状: `wyckoff_backtrader_strategy.py` 定义了"事件+高价值VSA"策略, 但只是回测脚本;
    `wyckoff/strategies/candidates.py:28` 的 `STRATEGY_ORDER = (DISCIPLINE, LONG_LEFT)`,
    VSA 策略未注册。VSA 目前只作为 `fusion.py` 的一个融合维度。
  - 依据: `strategy_analysis_report.md` 策略2 胜率 76.9% (n=26); `accuracy_report.md` §二
    VSA 单独用贴近随机 (49–60%), 只有作强事件确认证据才增值。
  - 建议: 在 `candidates.py` 增加 `STRATEGY_EVENT_VSA` producer (强梯队事件 ∧ 高价值VSA
    `{CHOC,DEM,SUP,LPS,ST,Spring}` 且 `vr≥1.5`), 复用现有 `scan_individual` 优先序;
    先以独立赛道 (不受门禁) 或仅作加分项验证, 勿直接替换 Spring-only。
  - 验收: `tests/` 新增用例; 回测对照 Spring-only 的单笔期望/胜率不劣化。

- [ ] **5.2 多周期共振接入入场硬门禁**
  - 现状: `multitime.py` 已产出日/周/月方向, 但 `entries.py` 未调用; 只在 `conclusion.py`
    展示层出现。
  - 依据: 高周期同向确认可过滤日线假信号 (与"大盘 MA20 门"同源逻辑)。
  - 建议: 在 `find_entry_signals` 增加可选参数 `htf_dir` (由 `_scan_one` 从 `multi_tf_analysis`
    注入), 日线多头入场要求周线不反向; 数据缺失 fail-open 或 fail-close 需实测选定。
  - 验收: 回测对照 (开/关) 单笔期望与回撤; 不得显著削减样本 (目标 n 下降 <30%)。

- [ ] **5.3 板块级去重后的组合回测**
  - 现状: `conservative_bt.dedup_signals` 已支持 `sector`/`chain` 模式, 但**未用其重跑**
    组合口径。
  - 依据: `docs/profitability_bt.md` §六.2 指出同日同板块重叠使真实回撤 > 单笔模拟;
    §七 待验证①。
  - 建议: `python -m scripts.conservative_bt --conf 90 --dedup sector --report ...` 与
    `--dedup chain` 对照 `none`, 量化相关性回撤。
  - 验收: 产出对照报告存档 `docs/`。

### P1 — 验证与校准

- [ ] **5.4 样本外 / 前向验证**
  - 依据: `docs/profitability_bt.md` §六.1、§七② (样本含 2024 偏强行情, 未做独立前向验证)。
  - 建议: 留出未参与校准的时段/标的, 或走"评估→重训→接管"闭环积累前向样本后再定阈值。

- [ ] **5.5 极端流动性敏感性 (涨/跌停)**
  - 依据: `docs/profitability_bt.md` §六.5、§七③ (回测用收盘价, 未计涨停买不进/跌停卖不出)。

- [ ] **5.6 PnF 上方目标概率高估下调**
  - 依据: `docs/accuracy_report.md` §五/§七.5, 上方三档系统性高估 ~8pt (下方标定良好)。
  - 建议: 下调上方档位预测概率参数 (合成 530 + 真实 72 段)。

- [ ] **5.7 模型衰减监控 / 重训**
  - 现状: `auc_oos=0.5614 / acc_oos=0.5715` 明显低于 8 月文档 v3 的 AUC 0.6896 / 61.2%
    (`docs/accuracy_report.md` §四)。
  - 建议: 核对 cron 重训链是否正常、特征是否漂移; 设 AUC 告警阈值, 低于时回退纯规则 conf。

### P2 — 工程债

- [ ] **5.8 模拟盘实盘口径与回测对齐**
  - 依据: `docs/spring_only_progress.md` 待办 1-3: 运行实例 `max_pos` 被写回 3 (应为 4);
    本地 `paper_stop_loss=0.05` 对回测 -4%、`paper_trailing_stop=false` 对回测开启。
  - 建议: UI 设置改 4 + stop/trail 对齐后重跑对照; 复核 `_record_equity` 修复后净值曲线。

- [ ] **5.9 板块强度历史快照回填**
  - 依据: `docs/backtest_comparison_report.md` §6: 快照仅自 2026-08 起, 历史回测门禁近乎空转。
  - 建议: 按日/周回填东财行业分位, 使板块门禁在历史区间真实生效。

- [ ] **5.10 分析级评估窗口成熟**
  - 依据: `docs/accuracy_report.md` §六: 109 条整股结论全 pending; cron 15:01 自动闭环。

- [ ] **5.11 价值吸筹 producer 未在扫描序**
  - 现状: `candidates.py` 注册了 `_produce_value_acc` 与 `STRATEGY_CN`, 但 `STRATEGY_ORDER`
    只含 discipline + left_buy, 价值吸筹实际不参与扫描 (与文件注释"纪律>价值吸筹>左侧"不一致)。
  - 建议: 确认是有意下线还是遗漏; 若保留, 需按 `winrate_improve_eval.md` 结论设 conf 门槛。

- [ ] **5.12 类型检查 + 覆盖率门槛**
  - 现状: 仅 ruff(E/F/W/I/UP) + compileall + pytest, 无 mypy/pyright 类型检查、无 coverage 门槛;
    本地环境未装 ruff (仅 CI 装)。
  - 建议: CI 增加 mypy 核心包 (`wyckoff/`, 先 `ignore_missing_imports`) 与
    `pytest --cov` 门槛 (建议核心模块 ≥70%); 本地 `pip install -e ".[lint]"`。

- [ ] **5.13 状态存储规模化**
  - 现状: `wx_signal_accuracy.json` 单文件 8.9MB (13,523 条), 靠 atomic write 保证一致性;
    评估/落盘在单锁内全量读写, 规模再增会拖慢且易冲突。
  - 建议: 迁到 `wyckoff_cache.db` (SQLite, 项目已有 `sqldb.py`) 或分片/增量写入。

---

## 四、复现与验证命令

```bash
# 环境门回归
python -m pytest tests/test_tags_audit.py -q

# 板块/链条去重组合回测对照
python scripts/conservative_bt.py --conf 90 --dedup sector --report docs/bt_dedup_sector.md
python scripts/conservative_bt.py --conf 90 --dedup chain  --report docs/bt_dedup_chain.md

# 全量回归基线
python -m pytest tests -q
```

## 五、相关文件

- `wyckoff/events.py` (环境门)、`wyckoff/online_model.py` (校准模型)
- `wyckoff/entries.py` / `wyckoff/discipline.py` (入场 + 三重共振)
- `wyckoff/strategies/candidates.py` (策略注册/优先序)
- `wyckoff/fusion.py` / `wyckoff/multitime.py` / `wyckoff/ninetests.py`
- `docs/profitability_bt.md` / `docs/accuracy_report.md` / `docs/event_env_gate_progress.md`
- `docs/winrate_improve_eval.md` / `docs/spring_only_progress.md` / `docs/backtest_comparison_report.md`

---

## 六、现状体检 (2026-09-15 实测)

### 6.1 工程质量: 高

- 规模: 263 个 py / 74,211 行 / 198 commits。
- 结构: `wyckoff/` 核心 80 模块、`ui/` 拆 components/renderers/threads、`sync/` 独立包、
  `scripts/`、`tests/`; `paper/`、`strategies/` 均已模块化; `discipline.py`/`config.py` 单一数据源。
- 测试: `pytest tests -q` → **617 passed, 7 skipped (624 collected), 56.5s**。
- CI (`.github/workflows/ci.yml`): ruff + compileall + pytest(offscreen) + 信号评估。
- 缺口: 无类型检查 / 覆盖率门槛 (见 5.12); 状态单 JSON 8.9MB (见 5.13)。

### 6.2 分析准确度: 分层明显

信号库 (wx_signal_accuracy.json): 13,523 条 / 已评估 13,483 / pending 40 / stale 1。

| 层级 | 代表类型 (20根方向命中, n) | 结论 |
|---|---|---|
| 强多头 | Spring 84.8% (671)、Shakeout 80.0% (135)、ST 74.7% (186)、LPS 65.0% (137)、SC 60.6% (327) | 统计显著 edge |
| 强空头 | LPSY 79.9% (134)、UTAD 78.6% (676) | A股无裸空, 仅反向证据 |
| 弱事件 | AR 52.6% (681)、SOS 46.2% (277)、BC 46.1% (386)、JOC 40.0% (95)、PSY 35.0% (100) | ~随机, 已剔除 |
| VSA | 全部 48–60% | ~随机, 仅上下文确认 |

- 强梯队加权 ~77% vs 弱梯队 ~45% (与 `docs/accuracy_report.md` 一致)。
- 校准模型: `wx_online_model.json` `ready=True`, `version=3`, `auc_oos=0.5614`,
  `acc_oos=0.5715`, `ic_oos=0.1196` (退化, 见 5.7)。
- 回测 (样本内): paper3 改进 +93.1%/Sharpe 1.07/回撤 -13.8%; Spring-only +494%/胜率 59.1%/
  回撤 -13.14%; conservative 年化 +15.4%~+29.5%。

### 6.3 总评与风险

- **事件识别层可信** (强梯队 60–85% 有统计意义); 弱事件与 VSA 接近随机, 已正确降权。
- **系统层仅为"有正期望候选"**: 回测方向一致 (单笔 +2.9~6%、胜率 53~83%、盈亏比 2~3),
  但三处硬伤必须打折——① 模型退化; ② 全部样本内、含 2024 偏强行情, 无前向验证;
  ③ 板块强度历史快照缺失致门禁历史空转、组合为数学叠加。
- 落地前先补 P0/P1 (VSA 入池、多周期门、板块去重回测、样本外验证、模型重训监控)。

> 免责: 以上均为样本内历史统计与工程缺口, 落地前需样本外验证; 不构成投资建议。
