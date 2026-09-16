# 高准确度做多事件能否开发出 Spring-only 类似策略? (实证)

> 背景 (2026-09-16): 用户问: "Spring-only 以 Spring 为主指标, 系统还有不少高准确度的
> A股做多事件 (Shakeout 81% / ST 74% / SC 77%…20根命中), 能不能开发出类似策略?"
> 本文用受控回测回答: **不能** —— 这是第二次系统验证 (初验见 paper/__init__.py
> 事件集收敛注释; 本文补有信号池对照 + 三级独立槽位设计, 结论一致且更强)。

## 一、事件准确度盘点 (wx_signal_accuracy.json, 20根方向命中 + 均值收益)

| 事件 | 方向 | conf≥90 样本 | wr20 | 说明 |
|---|---|---|---|---|
| Spring | 多 | 232 | 86% +0.10% | 现网策略唯事件 |
| **Shakeout** | 多 | 45 | **84% +0.09%** | 最深假破位, 质量接近 Spring |
| SC | 反转多 | 36 | 83% +0.08% | 恐慌抛售 |
| ST | 多 | 70 | 79% +0.08% | 二次测试 |
| LPS | 多 | 3 | 33% (池中近无) | 信号过稀, 弃 |

Shakeout 横截面 20根命中 84%, 与 Spring 86% 在同一档 —— 从"信号库口径"看
确实像可复制的第二策略。但回测给出相反结论。

## 二、受控回测 (复核为什么收敛到 Spring-only)

**口径**: `paper_replay_bt.py`, 生产对齐参数 conf=90 · maxpos=5 · 持20K · 止损-4% ·
止盈+15% · 移动止盈回落8% · 成本0.4% · 止损冷却20根 · 无空头主动卖出 ·
区间 2023-06-01~2026-09-16。
**股票池**: 信号富集 62 只主板 (信号库内 Spring/Shakeout/ST/SC conf≥90 的交集,
补齐了既有 200 只 603xxx 池里 Shakeout=0 的采样偏差)。同一份 superset 缓存。

| 变体 | 累计收益 | 最大回撤 | CAGR | 拆分 (笔·胜率·均收) |
|---|---|---|---|---|
| **Spring-only** | **+268%** | -13.8% | +48.6% | Spring 156·61%·+4.44% |
| Spring+Shakeout (同槽) | +235% | -14.1% | +44.3% | Spring 153·63%·+4.27% + 震仓9·56%·**-0.80%** |
| Shakeout-only | +29% | -3.6% | +8.1% | 震仓 26·65%·+5.19% |
| Spring+SC+ST+Shakeout | +216% | -12.9% | +41.8% | Sp118·ST55·SC6(负)·SK6(负) |
| Spring + [Shakeout,ST] 二级1槽 | +184% | -12.6% | +37.3% | Sp141·ST36·SK3(负) |
| Spring + [Shakeout,ST] 二级2槽 | +179% | -9.6% | +36.6% | Sp116·ST25·SK4 |
| Spring + [Shakeout,ST] 二级3槽 | +102% | -8.4% | +23.8% | Sp82·ST14·SK2(负) |

## 三、结论

1. **Shakeout 单跑成立但对冲不过量**: 65% 胜率、+5.19%/笔, 确属高质量做多事件;
   但它在 62 只信号富集池里 3 年仅 26 笔 (Spring 156 笔)。单独成策略收益过小
   (+29% vs Spring-only +268%)。
2. **任何与 Spring 混用的方式都稀释**: 无论同槽最新事件优先、还是独立槽位,
   加入 Shakeout/ST/SC 都系统性拉低总收益 —— 因为 maxpos=5 是稀缺资源,
   它们挤占的恰恰是质量更高的 Spring 槽位 (混合后 Spring 笔数 156→118~153)。
3. **与现网决策一致**: `paper/__init__.py` 事件集收敛到 `{Spring}` 是被本实验
   二次印证的实证最优; ST/Shakeout/SC 维持信号库口径 (图表标注/准确度追踪),
   不落地为独立交易策略。

## 四、可复用工具 (本次新增)

- `paper_replay_bt.py --events Spring,Shakeout,...` — 事件类型白名单 (加载 sup
  equivalent/买入双层过滤), 同一 superset 缓存可切换子集对照。
- `paper_replay_bt.py --universe-file stocks.txt` — 自定义股票池 (覆盖默认全A前N)。
- `paper_replay_bt.py --secondary-events Shakeout,ST --secondary-slots N` —
  二级事件独立槽位 (先填主事件, 剩余槽给二级), 直接检验"并行赛道"假设。
- 修复 `build_report` 中 `paper.time` 不存在导致的报告崩溃 (`datetime.now`)。

*历史回放, 不构成投资建议。*