# 板块/链条去重组合回测对照

- 生成: 2026-09-23
- 口径: `scripts/conservative_bt.py --conf 90`, 成本 0.8%, 结构位止损 -5%, 资金 100k/3 槽,
  持有 32 根, 信号库 `wx_signal_accuracy.json` (4.6 万条已评估)。
- 板块映射: `wyckoff_stock_sector.json` (3109 个目标标的中 2573 个已建, 83%; 由
  东财板块→成分股反解构建, per-stock f127 在当前网络被墙 → 走板块批量兜底)。

## 逐笔口径 (去重前 conf≥90 强多头可交易)

| 去重模式 | 笔数 | 每笔均值 | 胜率 | 盈亏比 | 最差单笔 |
|---|---|---|---|---|---|
| none | 12,931 | +9.02% | 79.4% | 2.45 | -33.66% |
| date (同日取最优) | 698 | +7.62% | 75.4% | 2.11 | -30.03% |
| sector (同日同板块) | 5,894 | +7.87% | 74.9% | 2.25 | -33.66% |
| chain (同日同产业链) | 5,632 | +7.80% | 74.7% | 2.25 | -33.66% |

## 组合口径 (资金 100k / 3 槽 / 顺序复利)

| 去重模式 | 近似交易数 | 槽位 CAGR |
|---|---|---|
| none | 305 | +19.3% |
| date | 279 | +18.6% |
| sector | 280 | +18.6% |
| chain | 279 | +18.6% |

> CAGR 差异 ≤0.8pt, 去重对槽位复利口径影响很小 (槽内顺序复利高度钳制相关性)。
> `port_max_drawdown` 恒为 0 (无逐日市值路径), 无法直接量化相关性回撤;
> `docs/profitability_bt.md §六.2` 的"同日同板块重叠使真实回撤 > 单笔模拟"
> 仍需逐日净值路径验证。

## 结论

1. **同日去重 (date) 最激进**: 36k→698 笔 (削减 ~95%), 每笔均值与胜率反而小幅下降
   (+9.02%→+7.62%, 79.4%→75.4%) —— 说明"同日最优"筛选掉的多数是重复信号里质量
   较高的, 去重并不提升单笔质量, 只减少交织。
2. **sector / chain 去重保留 ~44% 笔数** (12,931→5.6~5.9k), 每笔均值和胜率下降 ~1.2pt,
   盈亏比 2.25~2.45 保持稳健 —— 在"控制相关性"与"保留样本"之间更平衡。
3. **同产业链 (chain) 与同板块 (sector) 去重几乎等价** (5,894 vs 5,632 笔, 均值/胜率
   仅差 0.07pt) —— 上游链映射未引入额外可分性。
4. **建议**: 生产回测默认用 `--dedup sector` (同日同板块保留 conf 最高) 作为保守基准,
   与 `none` 对照看区间; 若后续论文验证相关性回撤需补逐日净值路径。

## 复现

```bash
python scripts/build_stock_sector_map.py
python scripts/conservative_bt.py --conf 90 --dedup none
python scripts/conservative_bt.py --conf 90 --dedup date
python scripts/conservative_bt.py --conf 90 --dedup sector --sector-map wyckoff_stock_sector.json
python scripts/conservative_bt.py --conf 90 --dedup chain  --sector-map wyckoff_stock_sector.json
```

> 历史统计, 不构成投资建议。