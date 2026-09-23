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

- [x] **5.1 高价值 VSA 独立策略入池 (策略2)** (✅ 2026-09-18, commit `ccf2cb7`)
  - 落地: `candidates.py` 已注册 `STRATEGY_EVENT_VSA` producer (`event_vsa_candidate`,
    强多头事件 ∧ 高价值VSA `{CHOC,DEM,SUP,TEST,SPR,SC}` 且 `vr≥1.5` conf≥85, 共时窗口
    `EVENT_VSA_CO_WINDOW`), 入 `STRATEGY_ORDER` 第三优先兜底、`CANDIDATE_GATED=False`
    独立赛道 (不受门禁)。常量在 `strategies/constants.py`, 测试 `tests/test_candidates_event_vsa.py` (7 条)。

- [x] **5.2 多周期共振接入入场硬门禁** (✅ 2026-09-18/19, commits `b08e4d3`/`66c1fe0`)
  - 落地: `find_entry_signals` 新增 `htf` 可选参数 + `_scan_one` 从 `multi_tf_analysis`
    → `htf_direction` 注入 (纯本地指标, 无网络); `ENTRY_HTF_GATE` fail-open/fail-close 双档。
  - 实证: `scripts/htf_gate_survey.py` (n=2419) 证伪该门 —— 周/月线偏空组强多头事件
    20根命中 69.0% 不低于非偏空组 68.6%, 且开门会拦下 ~50% 样本 (远超 30% 验收线);
    Spring 诞生于超跌环境, 高周期偏空不构成反向 → **门默认关 (IDLE), 保留开关**。

- [x] **5.3 板块级去重后的组合回测** (✅ 2026-09-23, 见 `docs/bt_dedup_comparison.md`)
  - 落地: `python scripts/build_stock_sector_map.py` (东财板块→成分股反解, 2573/3109 标的,
    83%; per-stock f127 被墙时走板块批量交接) + `conservative_bt --dedup none/date/sector/chain`
    对照。结论: date 最激进 (12,931→698 笔), sector/chain 保留 ~44% (5.6~5.9k 笔) 且盈亏比
    2.25 稳健; 组合槽位 CAGR 四模式仅差 ≤0.8pt; `sector` 建议为保守基准。
  - 遗留: `port_max_drawdown` 恒 0 (无逐日路径), 相关性真实回撤需逐日净值验证。

### P1 — 验证与校准

- [ ] **5.4 样本外 / 前向验证**
  - 依据: `docs/profitability_bt.md` §六.1、§七② (样本含 2024 偏强行情, 未做独立前向验证)。
  - 建议: 留出未参与校准的时段/标的, 或走"评估→重训→接管"闭环积累前向样本后再定阈值。

- [ ] **5.5 极端流动性敏感性 (涨/跌停)**
  - 依据: `docs/profitability_bt.md` §六.5、§七③ (回测用收盘价, 未计涨停买不进/跌停卖不出)。

- [ ] **5.6 PnF 上方目标概率高估下调**
  - 依据: `docs/accuracy_report.md` §五/§七.5, 上方三档系统性高估 ~8pt (下方标定良好)。
  - 建议: 下调上方档位预测概率参数 (合成 530 + 真实 72 段)。

- [x] **5.7 模型衰减监控 / 重训** (✅ 2026-09-19, 见 PROGRESS 5.5)
  - 落地: `MODEL_MIN_AUC` 0.55→0.60; 接管权重 = 样本爬坡 × AUC 质量标尺 (0.50→0/0.65→1 线性);
    `history[]` 轨迹 + 连续劣化 (近2均值≤既往-0.03) 折减 50% + `warnings` 告警;
    CLI `--history`; 校准中心告警横幅。当前生产 auc=0.679 行为不变, 轨迹随 cron 重训积累。
  - 归因: 09-15 的 0.5614 → 09-17 0.679 主因是标签 6.5k→12.3k 翻倍 (信号基座 99→173 扩容 +
    全事件无偏入库), 且该指标对 seed±0.09 不稳定 → 单点 AUC 不作判决, 只看轨迹。
  - 遗留: AUC 告警阈值告警 (当前仅 status 展示); 若贵模型继续劣化需回退纯规则 conf 的
    一键开关。重训链定位: cron 15:11 (`--install-cron`) / CLI `--train`。

### P2 — 工程债

- [x] **5.8 模拟盘实盘口径与回测对齐** (✅ 2026-09-23)
  - 依据: `docs/spring_only_progress.md` 待办 1-3: 运行实例 `max_pos` 被写回 3 (应为 4);
    本地 `paper_stop_loss=0.05` 对回测 -4%、`paper_trailing_stop=false` 对回测开启。
  - 落实:
    - 实盘/默认参数已对齐: `wyckoff_settings.json` + `_params.py` = `max_pos=5`,
      `stop_loss=0.04`, `trailing_stop=true`, `trail_back_pct=0.06`, `stop_cooldown=20`
      (与 `paper_replay_bt` 基线一致; trail_back 0.06 为当前默认, 文档基线 0.08 需注意)。
    - `_record_equity` 已确认按交易日 upsert 最新净值 (同日多周期只留最新)。
    - **事件日锚点修复 (口径不一致头号原因)**: 回测用事件日+事件价, 实盘之前用扫描日+last
      (候选可在事件后 0~10 根才被识别, 评估整体右移 → 命中率系统低估)。
      修复: `paper/_selection.py` 给候选附加 `event_date`/`event_px` (取自候选事件 bar idx);
      `paper/_conditions.py` 记录信号优先用事件锚点, 缺时回退扫描日。
      新增 2 条回归测试 (事件锚点优先 / 旧格式回退) + 保留既有冷却合并逻辑 (df=None 用日期合并)。
  - 验收: `tests/test_strategy_accuracy.py` 9 passed。

- [ ] **5.9 板块强度历史快照回填**
  - 依据: `docs/backtest_comparison_report.md` §6: 快照仅自 2026-08 起, 历史回测门禁近乎空转。
  - 建议: 按日/周回填东财行业分位, 使板块门禁在历史区间真实生效。

- [ ] **5.10 分析级评估窗口成熟**
  - 依据: `docs/accuracy_report.md` §六: 109 条整股结论全 pending; cron 15:01 自动闭环。

- [x] **5.14 模拟盘信号库冷却合并 df=None 失效修复** (✅ 2026-09-23, commit `d73e7c5`)
  - 现状: `_conditions.py` 廉价记录不传 df → `_cooldown_dup` 遇 `df=None` 直接返回 None,
    冷却窗合并从未在实盘路径生效 → 同事件跨扫描日重复入库 (公牛 sh603195 / 老凤祥
    sh600612 各在 09-09/09-10 入两条同价)。
  - 落地: `paper_strategy_accuracy._cooldown_dup_by_date` 日历日近似合并 + 3 条 df-less
    用例 (窗内合并 / 窗外新增 / 合并刷新 conf·ref_px)。场景详见 `docs/spring_live_accuracy_review.md`。

- [x] **5.15 Spring-only 实盘准确率归因** (✅ 2026-09-23, commit `d73e7c5`)
  - 结论: n=18 唯一信号 H5 40% (Wilson 95%CI [20%,61%]), 与回测 93% / 随机 50% 均区间
    重叠 → 统计上无法确证退化; 真正根因是 4 系统性偏差: ①事件日↔扫描日锚点错位
    (回测用事件日 open, 实盘用扫描日 close)、②conf<100 垫底信号混入 (生产门槛 100,
    样本 18/20<100)、③冷却合并失效致重复入库 (已修)、④样本仅 2 个扫描日。
  - 待办: 需 `_conditions.py` 记录事件日/事件价后重估 (见 `docs/spring_live_accuracy_review.md`)。

- [x] **5.11 价值吸筹 producer 未在扫描序** (✅ 2026-09-23)
  - 结论: **有意下线** (09-10 commit `34031d0` "移除价值吸筹，纪律+左侧买点"), 依据
    `docs/replay_improvements_v2.md` ⑦ (56 笔 -53%, 均收 -0.95%) 默认关闭,
    `docs/strategy4_discipline_improvements.md` (价值吸筹回退纯负贡献)。
    但 `winrate_improve_eval.md` 四.1 与 五.④ 强调其依赖事件方向命中质量高、**不推荐砍死**,
    且 `settings_keys.py:307` 仍承诺"显式 `paper_enable_va=true` 可选开启"。
  - 修复: `STRATEGY_ORDER` 补回 `STRATEGY_VALUE_ACC` (置于末位=回退兜底, 语义与
    "纪律优先→无信号回退价值吸筹" 一致); 默认仍由 `enable_va=false` 门禁剔除, 行为不变;
    `enable_va=true` 时真实生效 (此前扫描序过滤掉该 producer, 开关形同虚设)。
  - 验收: `tests/test_paper_account.py` 价值吸筹开关三用例 + 事件VSA 专项 + 边界 conf 全绿 (53 passed)。

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
