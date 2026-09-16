# Spring-only 降回撤 · 进度 (会话恢复用)

> 日期: 2026-09-15
> 起点: 用户反馈「Spring-only 回撤太大」
> 结论: 回撤大的主因是**净值/回撤统计口径 bug** + 回测净值被真实当天日期污染;
> 真实最大回撤仅 -16.4%。已提交并推送 `2652dfb`, 默认同持上限 3→4 (回撤 -13.1%)。

## 已完成 (已提交 2652dfb, 已 push origin/main)

修复:
1. `wyckoff/paper/_stats.py` `_record_equity`: `equity_hist` 为空时 `st.get(...) or []`
   把快照写入临时列表丢弃 → 净值曲线长期记不进。改为按需初始化并写回 `st`。
2. `wyckoff/paper/_stats.py` `stats`: 最大回撤峰值误用**单日因子**的滚动最高,
   应为**累计净值**的滚动最高 → 报告严重低估回撤。
3. 回放净值污染: `step()`/条件单/回测脚本平仓时按**真实当天**写 `equity_hist`,
   把回放曲线插入 `2026-09-15` 的点, 曾把 -16.4% 真实回撤算成 **-81.5%**。
   已把交易日 `day` 传到 `close_position` (`_trading.py` / `_conditions.py` /
   `paper_replay_bt.py` / `paper_priority_bt.py`), 统一按日 upsert。
4. 默认同持上限 3→4: `_params.py` `MAX_POSITIONS`, `config.py`,
   `settings_keys.py`, `strategies/evaluators.py` `trading_discipline`。

验证: `python -m pytest tests -q` → **624 passed**。

## 关键结论 (200只全A缓存, conf=100, 2023-06起, 止损-4%/止盈+15%)

| 移动止盈 | 持仓上限 | 累计 | 胜率 | 最大回撤 | CAGR |
|---|---|---|---|---|---|
| 8% | 3 (原基线) | +465% | 54.1% | -16.43% | +69.6% |
| **8%** | **4 (新默认)** | **+494%** | **59.1%** | **-13.14%** | **+72.2%** |
| 6% | 5 | +424% | 56.6% | -11.88% | +65.7% |
| 8% | 5 | +442% | 58.2% | -12.32% | +67.5% |
| 8% | 6 | +301% | 54.9% | -12.04% | +52.7% |
| 8% | 3 + 大盘MA20门禁 | +149% | 47.2% | -12.22% | +32.0% |

- 4 为最优: 收益/胜率/回撤三项同时改善 (分散化, 非拟合); 5 起回撤再降但收益回落。
- 大盘门禁压回撤但腰斩收益, 不建议。

## 待办 (下次继续)

1. **让运行中的模拟盘真正用 max_pos=4**: 本地 `wyckoff_settings.json` (gitignore)
   我改成 4 后被**运行中的客户端写回 3**。需在 UI 设置里把「同持上限」改成 4,
   或停程序→改文件→重启。提交的默认值只对新装/未显式设置的环境生效。
2. **实盘口径与回测对齐**: 本地设置 `paper_stop_loss=0.05` (回测 -4%)、
   `paper_trailing_stop=false` (回测开启)。若要实盘跟随回测结论, 需在 UI 同步调整。
3. 用实盘口径 (stop 0.05 / 无移动止盈 / maxpos 4) 重跑一次对照, 量化差异。
4. 可选: 复核 `_record_equity` 修复后, 运行中账户的历史净值曲线是否恢复正常。

## 复现命令

```bash
# 完整指标 + 回撤窗口 (默认读持久化缓存, maxpos=4)
python scripts/spring_only_dd_analysis.py
python scripts/spring_only_dd_analysis.py --maxpos 3            # 旧基线
python scripts/spring_only_dd_analysis.py --maxpos 5 --trail-back 0.06
python scripts/spring_only_dd_analysis.py --mkt-gate            # 大盘门禁对照

# 生成报告/逐笔 CSV (走 paper_replay_bt CLI)
python scripts/paper_replay_bt.py \
  --stocks-cache data/paper_replay_data/spring_only_conf100.pkl \
  --conf 100 --maxpos 4 --hold 20 --stop 0.04 --tp 0.15 --cost 0.004 \
  --start 2023-06-01 --trail-back 0.08 --no-bear-exit \
  --report /tmp/opencode/so_report.md --export /tmp/opencode/so_trades.csv
```

- 股票事件缓存 (200只, 69MB, gitignore): `data/paper_replay_data/spring_only_conf100.pkl`
  (本次已从 /tmp 复制持久化; 缺失时用上面 CLI 加 `--stocks-cache <同一路径>` 重新生成)。
- 分析脚本: `scripts/spring_only_dd_analysis.py` (本次新增)。

## 相关提交

- `2652dfb` fix: 修正净值/回撤口径 + Spring-only 同持上限 3→4 降回撤
- `049bb07` feat: 纪律事件集收敛至 Spring-only (回测 +456%→+491%)
- `546e930` refactor: 策略4·纪律 更名 Display 名称为 Spring-only
