# 选股策略挖掘：会话成果存档

> 日期：2026-09-01
> 目标：基于现有系统（策略4为基准）挖掘新的选股策略
> 状态：**验证未通过，未入库**，可迁移至其它机器继续

## 结论速览（必须读完）

挖掘过程分四轮，最终**样本外验证未通过**，新策略**不建议入库**：

1. **样本内 28 只蓝筹**（近500天，5根步长滚动窗口，next_open 入场 + -5%止损/+15%止盈/最多20天/0.4%成本）：
   挖出"Spring/Shakeout 低位反转"组合——仅 Spring/Shakeout 胜率 64%、加 MA20下方/pos<0.5 后 74~75%、加 RSI≥50 后 88%（PF 12+）。
2. **样本外 20 只（不同行业，与28只零重叠）验证 → 结论被推翻**：
   Spring/Shakeout 只有 45.2% 胜率、+0.60%、PF 1.28；叠加过滤全部失效（n=6 的 RSI 组合变负）。
3. **48 只合并 + 上证MA20牛/熊分层**：
   - 发现反直觉规律：Spring/Shakeout 在**熊市（大盘MA20下方）66.7% 胜率、PF 4.77**，牛市仅 46.2%。（震仓本质=弱势市场吓出筹码）
   - 策略4的"大盘MA20向上"硬门禁对这类信号方向相反。
4. **最终稳定性验证（拆分股票池+时间）**：
   - 熊市 Spring/Shakeout：样本内 80% vs **样本外 47.1%（PF 1.17 ≈ 无正期望）**
   - 时间前半 76.2% → 后半 57.1%（收益腰斩）
   - → **幸存者偏差，不具备样本外可靠性**。与策略1/2/3 被删同因。

**额外确认**：ST 事件全线负（熊市37%/牛市41%），基线中排除 ST 合理。

## 待继续的方向（换机后候选）

- **A. 测策略4硬门禁的真实增益**：用 48 只 + 板块>60分位 / 资金流>中位，验证门禁是否在不依赖"好股票池"下贡献正期望。
- **B. 逆势/震荡市因子**：市场门禁方向（熊市做多震仓股）在更大随机股票池上重做，避免蓝筹偏差；注意时间衰减。
- **C. ETF/板块联动因子**：`wyckoff/etf_factor.py`、`chain.py` 未纳入本次挖掘。

## 代码与数据文件

| 文件 | 作用 |
|------|------|
| `factor_mining.py` | 步骤1：28只滚动采集信号+因子+纪律收益，输出 `mining_data.json` |
| `comb_search.py` | 步骤1后：因子分层 + 组合搜索 + 时间稳定性 |
| `strategy5_verify.py` | 步骤2前：28只口径回测各组合（对照） |
| `strategy5_verify_oos.py` | 步骤2：20只样本外回测（**关键推翻结论**） |
| `market_gate_verify.py` | 步骤3：48只合并 + 上证MA20牛/熊分层 |
| `final_stability.py` | 步骤4：最终稳定性（时间+股票池拆分） |
| `mining_data.json` | 101 条信号+全因子向量（步骤1产出） |
| `verify_trades.json` / `verify_trades_oos.json` | 各组合逐笔收益（28只 / 20只） |
| `verify_48.json` / `verify_final.json` | 48只逐信号记录（含牛熊标记） |

**回测口径（与 `three_strategies_backtest.py` 一致）**：采样点 90..len-H-10 步长5；
滚动窗口每点重算 add_indicators+find_pivots(order6)+detect_all+nine_tests；
事件去重（每股票每事件idx只一笔）；next_open 入场；
-5%止损 / +15%止盈（盘中触发）/ 最多持仓20天 / 0.4%成本。

## 关键公式/参数速查

- 策略4基线选股：`{Spring, Shakeout, ST, LPS, SC}` 且 `conf≥90` 且事件在近10根内。
- Spring/Shakeout（震仓诱空类）= 排除 ST 后余下强多头事件。
- 大盘牛熊判据：上证 `sh000001` 日线 `add_indicators` 后 `close > price_ma20`。
- 日期对齐：市场字典键为 `"YYYY-MM-DD"`（`Timestamp.date()`），直接 `str(Timestamp)` 会带时间导致查找失败（已在脚本修复，勿回退）。

## 涉及路径

- 脚本全部在 `research/` 下，可直接 `python factor_mining.py` 等重跑（联网或走 `wyckoff_cache.db`）。
- 主策略代码：`wyckoff_strategies_manager.py`（当前仅策略4）。

---

# 综合选股预设回测验证：会话成果存档

> 日期：2026-09-02
> 目标：验证 `wyckoff/screener.py` 综合选股 5 个预设策略是否有实测正期望，决定是否推荐入库
> 状态：**价值吸筹唯一通过并入库，其余预设已从列表清除**

## 结论速览（必须读完）

5 个预设 + 4 个变体，在 **48 只样本**（含沪深300成分/二线/题材，行业多样）上做与 `three_strategies_backtest.py` 相同口径的滚动回测（采样点 90..len-H-10 步长 5，滚动重算指标/枢轴/事件，事件逐idx去重，next_open 入场，-5%止损/+15%止盈/最多20天/0.4%成本），并做**股票池内/外拆分 + 时间前半/后半**稳定性检查：

1. **价值吸筹（底部整固 + 20根内 {Spring,Shakeout,SC,ST,LPS}）→ 唯一通过，入库标推荐**：
   全样本 n=130、胜率 49.2%、均收益 +1.12%、PF 1.53、累计 +223.9%；样本内 47.4%（n=66）/ 样本外 51.9%（n=64）；时间前半 48% / 后半 51%。
   是**"正期望型"（大盈小亏，靠少数大赢单）而非 >60% 高胜率**——用 PF 而非胜率考核。
2. **强势突破 / 超跌反弹 / 小盘成长 / 资金流向 → 全部未通过**（负期望或不稳定甚至亏损），已从 `PRESET_STRATEGIES` 移除，不推荐使用。
3. **策略4基线口径（conf≥90 且近10根）当前缓存几乎不可复现**：全样本仅 n=21、胜率 33%。缓存窗口长度下无法获取足够的高 conf 信号，**不可用作判定依据**（这正是模拟盘接入价值吸筹作为无 conf 门槛回退信号的背景）。

## 入库动作

- `wyckoff/screener.py`：`PRESET_STRATEGIES` 精简为仅 `value_accumulation`，带 `verified` 元数据（2026-09-02，recommended=True，n/wr/avg/pf/cum/样本内外/半段），UI 下拉加 ★ 推荐徽标 + 悬浮显示实测统计。
- `wyckoff_strategies_manager.py`：新增 `evaluate_strategy_value_accumulation`（回测口径一致：阶段=底部整固 + 20根内吸筹事件，无 conf 门槛），接入 `analyze_stock`。
- 模拟盘 `wyckoff/paper.py`：候选生成改为双策略——纪律（强多头 conf≥阈值，`paper_discipline_bull`）优先，无高 conf 事件时回退价值吸筹（`screener_value_accumulation`）；四表（候选/订单/持仓/已平仓）均带策略标签。
- 已提交：`c68775f`（回测验证入库+推荐）、`12569ac`（清除未通过预设与 8 个旧策略脚本）、`bd8bdbd`（策略管理器接入模拟盘）。

## 代码与数据文件

| 文件 | 作用 |
|------|------|
| `screener_presets_verify.py` | 5 预设 + 变体回测，样本内外 + 时间半段稳定性 |
| `wyckoff/screener.py` | 综合选股预设（现仅 value_accumulation，含 verified 元数据） |

## 关键公式/参数速查

- 价值吸筹定义：`phases=["底部整固"]` AND 20根内出现 `{Spring, Shakeout, SC, ST, LPS}`（SOS 48% / PSY 42% 已剔除），辅助 `pe_max=35 / pb_max=4`。
- 重点：**此策略是正期望型，不是高胜率型**——只看胜率会误判，必须以 PF/累计+分段稳定性为准。