# 项目质量与分析准确度提升方案

- 生成: 2026-09-16
- 性质: **方案 / 提案**；随后按 Round 1 实施并完成（见下方进度）。
- 基准: `python -m pytest tests -q` = **624 passed**；`ruff check .` = **267 处报错**；CI 仅 `ci.yml`（ubuntu, ruff+compileall+pytest, `signal_accuracy --eval` 以 `|| true` 放行）。

---

## 0.5 实施进度

### Round 1（已完成 2026-09-16，随 docs 提交）

| 工作项 | 状态 | 备注 |
|---|---|---|
| S1 账户静默重置 | ✅ | `paper/_state.py`: 损坏备份 `.corrupt-<ts>` + `meta.rebuilt_after_corruption`；`save_state` 失败记日志返 False；`run_cycle/run_scan` 落盘失败记 `meta.save_failed` |
| S2 行情缺失静默跳股 | ✅ | `run_cycle` 失败改 `logger.warning` + `risk_metrics.kline_missing` 计数 |
| S3 弱市 fail-open + 封板买入 | ✅ | `_weak_market_flag` except → True（fail-close）；封板取数失败/为空 → fail-close `continue` + `risk_metrics.limit_unknown` 计数 |
| S4 trailing 默认三方漂移 | ✅ | UI 回退默认改引 `paper/_params.TRAILING_STOP`（单一源）；config `DEFAULT_SETTINGS` 已有 `paper_trailing_stop=True` |
| A1 qlib 分位前视 | ✅ | `_spread_probabilities` 改**前缀秩**（`bisect` O(log n)/bar，含自身），无前视；docstring 同步 |
| A2 中性事件特征缺失 | ✅ | `events.py`：bw_pct/rsi_6/kdj_d/up_i 移出 `if d:`，SC/BC/AR/SOS/PSY/BU 也填真实值；方向打分不变 |
| E601 vsa_explain 重复键 | ✅ | 删除 `VSA_EXPLAIN` 顶部 ER/EF 重复定义（保留"宽幅但无结果"版） |
| 新增回归测试 | ✅ | `test_paper_state.py`(6)、`test_qlib_adapter` 前缀秩(2)、`test_tags_audit` 中性特征(2)、`test_vsa_explain` 键唯一(1) |
| 回归结果 | ✅ | `python -m pytest tests -q` = **636 passed**（+12）；`ruff check .` = **264**（基线 267, F601 清零, 无新增） |

遗留说明: A2 附加项（`backfill_ctx` 全量 backfill 重建中性事件历史特征）未在本轮执行，归 Round 3 前重跑 survey 时一并处理。

### Round 2（已完成 2026-09-16，工作树未提交）

| 工作项 | 状态 | 备注 |
|---|---|---|
| A3 阈值注册表 | ✅ | 新增 `wyckoff/calib_registry.py`（`CalibEntry`/`Bucket` + `bucket_value`/`resolve`，带 `as_of`/`min_n`/过期回退 `default`）；`events.py` 的 SC/BC 环境门、SOW 放量门、boll 高位门与 `fusion.py` 弱事件半权改走注册表；`tests/test_calib_registry.py`(7 例) |
| S8 ruff 264→0 | ✅ | `ruff check .` = **0**；修 `scripts/paper_replay_grid.py:473` f-string 复用引号（Py3.10 下 invalid-syntax）；`paper/__init__.py` star import(F405) 改显式导入 + `__all__`；F401/F841/E702 逐项清理 |
| S7 死代码摘除 | ✅ | 删 `wyckoff/backtrader_engine.py`(297行)、`wyckoff/ensemble.py`(134行)、`paper/_orders.py`；`_stats.advanced_stats/signal_stats_text`、`fundamental.fetch_board_flow_by_code`、`screener.recommended_presets`、`chain.{install_snapshot_cron,chain_evidence,strength_history}`、`_state.file_path`、`_POS_WEIGHT` 均确认全仓 0 引用后删除 |
| S9 配置补登记 | ⏸️ | 本轮未做（按指示跳过） |
| 回归结果 | ✅ | `python -m pytest tests -q` = **643 passed**；`ruff check .` = **0** |

**收尾说明**: 后续 Round 3/4（T1/T2、A5、S6、P2 批次、S9 等）按指示不再执行。

---

## 0. 结论速览

软件质量的主风险不是"跑不起来"，而是**静默降级**：数据/账户/撮合失败时不报错、继续跑，账目失真无人知。
分析准确度的主风险是**前视 + 特征缺失 + 阈值漂移**：qlib 分位前视、中性事件特征缺失、多处分桶阈值一次性固化不重算。

优先级排序（P0=直接影响资金/信号正确性，P1=影响一致性与可维护性，P2=性价比提升）：

| P0（先做） | P1 | P2 |
|---|---|---|
| S1 账户静默重置 / S2 行情缺失静默跳股 / S3 弱市 fail-open+封板买入 / S4 trailing 默认三方漂移 / S5 风控键不可配置 | S6 `_CUR` 无锁重建 / S7 死 API 摘除 / S8 ruff 267 处清理 / S9 settings defaults 缺 20 键 / S10 QLib 前视 / S11 中性事件特征缺失 | S12 热路径 import / 死配置键 / UI 单位 / 复权一致性 / 弱信号图表降权 |
| A1 qlib 分位前视（同 S10） | A2 分桶阈值持续重算 / A3 训练池偏置 / A4 探测器阈值统一 | （并入左边） |
| T1 精度快照测试 | T2 补齐 4 个无测模块 / T3 survey 可重放流水线 | T4 CI 精度门槛 |

---

## 1. 软件质量（证据 → 措施）

### P0

**S1 账户状态损坏→静默重置（高）**
- 证据: `wyckoff/paper/_state.py:51-53` `load_state` 吞异常后 `return _new_state()`；`:76-86` `save_state` 失败返回 `False`，但 `paper/__init__.py:596` 与 `:700-702` 均忽略返回值。
- 风险: 磁盘/JSON 损坏时账户被静默重建（历史/持仓/回撤全丢）；落盘失败但流程照常，交易"以为成交了"。
- 改法:
  1. `load_state` 失败时把损坏文件改名留存（`.corrupt-<ts>`）+ `logger.error`，再重置；UI 显式提示"账户已重建"。
  2. `save_state` 返回值接入交易关键路径：落盘失败时记 `st["_last_save_failed"]` + 错误计数，UI 状态栏告警。

**S2 持仓行情缺失→止盈止损整段静默跳过（高）**
- 证据: `paper/__init__.py:577-582` `run_cycle` 对持仓/待撮合标的 `fetch_kline+add_indicators` 失败 `except: pass`；`step()` 对缺码标的静默不判。
- 风险: 个股连续取数失败时，该仓无止损保护。
- 改法: 失败计数进 `st["risk_metrics"]`，日志 `logger.warning(f"行情缺失: {code}")`；连续 N 根缺失（如 3）时标记该仓"数据中断"，UI 高亮。

**S3 弱市保护 fail-open + 涨停封板股被买入（高）**
- 证据: `_selection.py:39-40` `_weak_market_flag` 指数异常→返回 `False`（=非弱市）→ 弱市限仓/停 VA 被静默取消；`paper/__init__.py:532-539` 涨停判断取数失败按普通撮合。
- 改法: 两项改为 **fail-close**（数据缺失=保守：弱市判定为"不确定"→临时限仓；封板未知→当日不买），并分别记录 `_weak_unknown` / `_limit_unknown` 计数供审计。

**S4 追踪止损默认值三方漂移（高）**
- 证据: UI 默认 `False`（`ui/paper_window.py:972`），引擎默认 `True`（`paper/_params.py:43`、`config.py:445`）；live `wyckoff_settings.json` 亦为 `False`。设置缺失（新档/重置）时界面显示关、引擎按开跑。
- 改法: 以 `wyckoff/paper/_params.py` 为唯一默认源，`config.py` / `settings_keys.DEFAULTS` / UI 默认值三处改为同一常量（或 `_params` 导入）；补一条"缺键时引擎与 UI 一致"的单元测试。

**S5 8 个风控键完全不可配置（高）**
- 证据: `paper/__init__.py:186-194` 读取 `paper_max_drawdown/max_risk_pct/sector_conc/single_conc/correlation_threshold/vol_adjust/max_capital_usage/sizing_method`，但既不在 `settings_keys.Paper` 枚举，也不在 `config.DEFAULT_SETTINGS`，UI 无控件——只能手改 JSON 且无默认登记。
- 改法: 在 `settings_keys` 补齐 `S.Paper.*` 与 `DEFAULTS`；`ui/paper_window.py` 增加对应设置控件（至少 drawdown/risk 两项）；补默认值一致性测试。

### P1

**S6 `_CUR` 全局配置无锁重建 + 私有符号泄漏（中高）**
- 证据: `paper/__init__.py:167-168` `global _CUR; _CUR={...}` 不入 `_LOCK`；`paper_cron.py:83-88` 与 UI 随时重建，"锁只挡交易函数挡不住配置快照一致性"。私有符号 `_CUR/_LOCK/_new_state/_reset_logs` 被 `ui/paper_window.py:173,299,1350` 与脚本/测试直读，成为跨层契约。
- 改法: ① 配置读取统一走 `paper._cfg()` 快照字典型接口，重建时整体替换引用（原子, GIL 已保证指针替换）；② 文件重建与 run_cycle 用同一 `_LOCK`；③ 收敛私有符号：新增公开 `get_config()/new_state()`，逐步迁移 UI/脚本/测试。

**S7 死代码/孤儿模块清理（中）**
- 证据（AST 全仓 0 引用）: `paper/_orders.py` 全套高级订单 API（create_oco/bracket/scale_in/scale_out/trailing、check/cancel）+ `_stats.py:295 advanced_stats`、`:355 signal_stats_text` 仅在 facade 重导出 0 调用；`fundamental.py:493 fetch_board_flow_by_code`、`screener.py:766 recommended_presets`、`chain.py:233/470/538`、`paper/_state.py:16 file_path`、`paper/__init__.py:55 _POS_WEIGHT`；整模块 `backtrader_engine.py`(297行) 与 `ensemble.py`(134行) 无任何引用。
- 改法: ① 从 facade 摘除是否真导出（先用 AST 确认 `from wyckoff.paper import *` 的调用方），确无调用则删；② `backtrader_engine.py/ensemble.py` 二选一：删除，或标注 `@deprecated` + 登记到 README "不走该路径"。删除前跑全量测试 + `git grep` 双保险。

**S8 ruff 267 处清理（中）**
- 构成: F405×91（`paper/__init__.py` star import 未定义名）, F401×77（多数为 facade 重导出）, I001×30, E702×22, W292×18, F841×8, F821×4, E401×4, E741×3, F541×3, F601×2, F811×2, W291×2。
- 其中**真 bug 类**:
  - `wyckoff/vsa_explain.py:168,176` 字典键 `"ER"/"EF"` 重复（与 `:64,72` 冲突，后者静默覆盖前者）→ 修复后补"解释表键唯一"测试。
  - `wyckoff/qlib_adapter.py:413,438,440` F821 `np/pd`：因 `from __future__ import annotations` 目前不炸，但依赖太脆 → 顶部显式 `import numpy as np`/`import pandas as pd`（顺带消掉函数级重复 import）。
  - F841 残余: `buypoints.py:126 vol_ref`、`_stats.py:20 c`、`analysis.py:176 qlib_bull_strength` 等 → 删除或 `_` 前缀，并核对 `analysis.py:176` 处是否漏用（疑似"算了没用"＝评估项未落地）。
- 手法: 先 `ruff check --fix`（75 处 I001/E702/W292/W291/F541 自动），再人工处理 F401 重导出（改 `__import__`-free 的 `__all__` 声明或 `as` 冗余别名），F405 用 `# noqa: F405` 或把 star import 改显式遍历。

**S9 配置登记缺口（中）**
- 证据: `settings_keys.DEFAULTS` 缺 20 个 `Paper` 键（trailing/weak/va/push/st_confirm/rebalance…），UI `state_manager.py:29-30` 正用 `DEFAULTS` 兜底；`config.py:425` 注释自称"与 paper/_params.py、settings_keys.DEFAULTS 保持一致"，实际不符；`paper_st_confirm`（`+212%→+307%` 回测结论）无 UI 无 live 键，恒默认 True，关闭路径界面不可达。
- 改法: 以 `_params.py` 为源生成/核对 `DEFAULTS`；补 `paper_st_confirm` 的 live 键 + UI 开关；统一 `config.py` 注释为"单一数据源=paper/_params.py"。

### P2

- **S10 热路径 import**: `_selection.py:305,380`、`_risk.py:105`、`_trading.py:361` 循环内 `from ..X import f` 上提到模块顶部。
- **S11 死配置键**: `config.py:502-504` `hw_cpu_count/hw_parallel_max/hw_phase_cache_days` 从未被读；`wyckoff_settings.json` `calib_last_sync` 未登记 → 清理或接入实际逻辑。
- **S12 UI 单位不一致**: `ui/paper_window.py:996-1001,1078-1081` `sp_trail_back` 落 `0.08` 但后缀 `" %"`（同窗其他控件 ×100）→ 统一为 ×100 约定或改后缀 `0.08`（无单位）。
- **S13 数据源失败口径不统一**: `fetch_kline:392-439` 全源失败抛错 vs `_sina_qfq_factors` 失败返 `None`（用未复权数据）、`fetch_realtime` 失败返 `{}` → 统一"返回空 + 打日志"对外接口签名，UI 端展示"数据缺失"而非静默。
- **S14 运行时异常观测**: `wx_debug.log.old` 890 个 traceback 主因 SSL/数据源失败（历史日志，非当前代码 bug），但说明一个事实——**失败多数发生在开盘时点且无 UI 呈现**；建议仪表盘/扫描页增加"数据源健康度"红点（失败率>阈值）。

---

## 2. 分析准确度（证据 → 措施）

### P0

**A1 qlib 分位归一化前视（高，与 S10 冲突项的同一根）**
- 证据: `qlib_adapter.py:413-434` `_spread_probabilities` 用 `ranks=(preds[None,:]<=preds[:,None]).mean(axis=1)` ——在**整段样本**上算分位；`qlib_probability_series`（L437-460）整段传入；`fusion.py:33-39` 又把它当概率按 `(p-0.5)×2.4` 加权。回测/消融中历史 bar 的 prob 被其后 bar 拖拽 = **前视泄漏**。
- 辅助缺陷: AUC=0.5 时 spread=0.6，prob 恒落 0.32~0.68，永远到不了 docstring 声称的"强看多>0.6"绝对档；实时推理概率随回溯窗口长度漂移，跨时不可比。
- 改法: ① 提供 `--prefix` 模式：每 bar 只用 `preds[:i]` 的前缀秩（`expanding rank`），O(log n)/bar 的增量结构或 julia 式序数法；② fusion/消融统一走前缀口径；③ docstring 同步改写为"前缀分位，绝对档跨时可比"。
- 验证: `scripts/qlib_ablation.py` ON/OFF 重跑，确认"测试期"与"训练期"不再混入；新增"前件秩≠整体秩"单元测试。

**A2 中性事件特征缺失 → 最强特征被喂 None（高）**
- 证据: `events.py:754` `if d:` 分支只对方向事件计算趋势/布林/RSI/KDJ/共振；中性事件（SC/BC/AR/SOS/JOC/PSY/BU）`feat.boll_pct/rsi_6/kdj_d/reson` 恒 None（L821-826），仅靠 `online_model.py:56-62` `_NEUTRAL_FILL`(boll_pct=0.5) 兜底。而 boll_pct 是 `online_model.py:770` 自证的最强预测特征（rho=-0.36），SC/BC 又属七类强梯队（`config.py:275-276`）。
- 风险: 在线模型对 SC/BC 的 conf 接管质量被结构性削弱；`reson` 共振对中性事件恒 0，模型学不到同向共振。
- 改法: 把 `feat` 计算中**不依赖方向假设**的字段（boll_pct/rsi_6/kdj_d/sec_pct/rs_pct/vol_shrink等）从 `if d:` 移出，中性事件也填真实值；仅保留"趋势交互/方向共振"在 `if d:` 内。补 `test_tags_audit` 断言"SC/BC 事件 feat.boll_pct 不再为 None"。
- 附加: `backfill_ctx.py` 只对实时事件 enrich，历史 SC/BC 记录特征残缺 → 跑一次全量 backfill 重建 `wx_online_model` 训练/评估集。

### P1

**A3 分桶/权重阈值"一次性固化"→ 可重放自适应（中）**
- 证据: SC/BC 环境门（`events.py:874-897`）、SOW 放量门（`906-914`）、boll_pct>0.8 丢弃（L363）、fusion SOS/JOC 半权（`fusion.py:154`）都是全量调查一次调研后**写死**；分布漂移后不重算。BBC 侧 `bc_conf_buckets`（`docs/bc_conf_calibration.txt`）结论"No changes needed"也仅存快照。
- 改法: 建立"**阈值注册表**"模块（`wyckoff/config.py` 或新 `wyckoff/calib_registry.py`），把上述分桶映射集中登记；所有 survey 脚本（`event_by_trend/sow_tighten_survey/bc_conf_buckets/...`）统一输出该注册表期望格式；注册表带 `as_of` 日期与样本量，conf 打分器读取时自动跳过"过期/样本不足"桶（回退默认档）。
- 后续（可选）: CI/cron 每季度自动重算入表。

**A4 在线模型训练池结构性偏置（中）**
- 证据: `online_model.py:149-164` label 来自已录信号，而 WEAK_EVENT_TYPES/VSA_NOISE 在 record 阶段即跳过（`signal_accuracy.py:229,243`）→ SOS/BC/AR/PSY/NS/ND 整类从不进训练集，模型无法对弱类型降权，只能靠静态 `_cap_to_ceiling`。
- 改法: ① 至少为每个类型加"类型弱因子"特征（第 k 类别的历史准确率嵌入），让模型学"类型×市场状态"交互；② 或训练集纳入弱类型但其 label 权重 0.5。选轻方案（特征嵌入）先试，用 OOS AUC × conf 接管质量对照。

**A5 探测器阈值两处漂移 → 统一 config（中）**
- 证据: 高潮 vol 阈值 `events.py:210-216`（vol_ratio≥1.6/2.0, 影线>0.30）与 `vsa.py`（`DEFAULT_THRESHOLDS` L39-67）各自一套；SOS 双探测器 `events.py:278-291`（vr>1.3）与 `:353-379`（vol≥1.25）漂移；JOC `:353-355` 三重阈值叠死 + boll_pct>0.8 整根丢弃（60 根窗内 1/4 突破消失）。
- 改法: 抽 `config.py` 统一 `REVERSAL_EVENT_THRESHOLDS` / `SOS_THRESHOLDS`，`events.py` 与 `vsa.py` 共用；JOC 的 boll>0.8 硬丢改为降权分支（保留事件但 conf 减分）——降低正常行情误删率。

### P2

- **A6 复权口径**: 新浪因子折算（`datasource.py:5-12,183-185`）与东财 fqt=1 直出混用，复权因子 7d TTL → 除权当日缓存不刷则全段平移；加"除权日缓存失效"逻辑 + 单测。
- **A7 弱信号可视化区分**: 全量事件 42% 走 WEAK（`config.py:281-286`）、VSA 55.6% noise（`vsa.py:91-96`）但图表全标 → 图层用弱化样式（虚线圈/低透明度）标注 weak/noise，或加显式"该标签近随机"提示。
- **A8 置信天花板语义**: 收缩胜率<50% 钳制 `[1,99]`（`signal_accuracy.py:265`）与 `_cap_to_ceiling` 的 Wilson 上界 55% 语义需文档化，防后人误读。

---

## 3. 测试与验证扩充

- **T1 精度快照（防回归）**: 用 `wyckoff_cache.db` 固定样本（如 200 只全A）跑 events+vsa+phases 全链，断言既有 survey 结论的高阶区间（SC 深跌≥70%、BC 深涨≥60%、Spring≥80%…），作为"分析准确度回归基线"。落到 `tests/test_accuracy_regression.py`（标记 slow，可 `--runslow`）。
- **T2 补无测模块**: `news.py`(863行)、`paper_log.py`(483行)、`pnf_accuracy.py`(363行) 为最薄 3 模块，至少补"参数→输出"骨架单测（防 refactor 破坏）；`discipline.py` 门禁抽独立测试文件。
- **T3 survey 可重放流水线**: scripts 26 个文件目前靠文档串联 → 新增 `scripts/run_all_surveys.py` 或 `README` 固定命令表，输出统一落 `docs/survey_YYYYMMDD/`，并在 `ci.yml` 增加"蒸馏结果已入注册表"的 diff 检查（门槛只告警）。
- **T4 CI 精度门槛**: `ci.yml` 末步 `signal_accuracy --eval || true` 从不 gate → 改为阈值告警 action（如 accuracy_oos < 上期-2pt 标记）。train_pool/train_cache 缺失与文档不符 → 补数据或修文档。
- 摸底工具: 全库 0 个 `@pytest.mark.skip/xfail`（好事）；动态跳过仅 qlib/sklearn/PyQt importorskip ≤14 例。

---

## 4. 建议执行顺序（每项独立可验收）

| 轮次 | 工作项 | 验收 |
|---|---|---|
| 1 | S1+S2+S3（失败可观测、fail-close 化）；S4%（默认单一数据源）；**A1 前缀秩**；**A2 中性特征**；vsa_explain F601 修 | `pytest` 全绿 + 新增用例各 ≥1；ruff 关键项清零 |
| 2 | 2-A3 阈值注册表；S8 全面清理 ruff（267→0）；S7 死代码摘除；S9 配置补登记 | `ruff check .` = 0；`git grep` 确认无引用后删 |
| 3 | T1 精度快照基线；T2 补 4 模块骨架测试；A5 阈值统一；S6 `_CUR` 快照化 | 精度基线入库；4 新测试文件 |
| 4 | P2 批次（S10-S14、A6-A8）+ T3/T4 流水线 | CI 达标 + 文档更新 |

> 说明: 各轮之间独立可提交；优先 P0 的"资金/信号正确性"类（S1-S3、A1-A2）。

## 5. 复现与检查命令

```bash
python -m pytest tests -q                    # Round-2 后为 643 passed
ruff check .                                 # Round-2 后为 0 处
ruff check . --output-format=concise | Select-String -Pattern "F601|F821|F841|F405"  # 看重点
git grep -n -- "create_oco_order" . || :      # 死代码确认手法
python -m pytest tests -q -k tags_audit       # SC/BC 环境门既有测试
```

## 相关文件索引

- 质量: `wyckoff/paper/_state.py`、`paper/__init__.py`、`paper/_selection.py`、`ui/paper_window.py`、`wyckoff/settings_keys.py`、`wyckoff/config.py`、`pyproject.toml`
- 精度: `wyckoff/qlib_adapter.py:413-460`、`wyckoff/events.py:210-216,278-291,353-379,737-897`、`wyckoff/vsa.py`、`wyckoff/online_model.py`、`wyckoff/signal_accuracy.py`、`wyckoff/fusion.py`
- 测试: `tests/test_validation.py`（唯一统计精度检验）、`tests/test_tags_audit.py`、`tests/test_vsa_enhanced.py`
- 资产: `wyckoff_cache.db`、`wx_signal_accuracy.json`、`wx_online_model.json`、`wx_board_snap.json`（仅 2026-09-01~15）、`docs/event_env_gate_progress.md`

*本方案由代码扫描得出，非投资建议。金色提示：任何改动方向正确性都与真实市场数据分布相关，落地前先在对应的 survey/回测脚本上复验。*