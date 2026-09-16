# Spring-only 降回撤 · 进度 (会话恢复用)

> 日期: 2026-09-16
> 起点: 用户反馈「Spring-only 回撤太大」… 目标: 在当前口径上继续压低。
>
> ## 本期结论 (已提交 + 已同步默认)
>
> **对齐实盘与回测口径 + 新增「止损后再入冷却」，共两级措施：**

> 上一期 (2652dfb 后) 已把回撤从违规口径 -81.5% 修回到真实 -13.1%。本期用户仍觉大，
> 从两条线入手：

### 1) 实盘/回测口径对齐
- 本地 `wyckoff_settings.json` 默认 `paper_max_pos` 4→**5**、
  `paper_stop_loss` 0.05→**0.04**、开启 `paper_trailing_stop`，
  使运行中实盘口径与回测 (尾部 8% 移动止盈 / 止损-4%) 一致。
- 根因: 实盘 UI 止损默认 5%、无移动止盈、maxpos=3，与回测 (4%,8%trail,4) 不同 → 高估回撤观感。
- 附: 更新 ui/paper_window 参数标签默认 (最大 1~8, 最优5) + 冷却 SpinBox 范围/tooltip。
- [做] 本地 `wyckoff_settings.json` 已改；运行中的客户端会把它写回，若被覆盖需重启客户端生效。

### 2) 新增「止损后再入冷却」+ 持仓上限 4→5 (本期主线)
定位: 2024 年 5-7 月回撤窗口 (24-07-24 谷底) 全是**同标的反复止损重入**——
sh605338 在 06-19/06-21/06-28 三连损 -14.2% (每笔均单独止损, 冷却后消失)。
同一标的止损平仓后 N 个交易日内禁止再开仓。回测最优冷却 20 根。

| 口径 | 累计收益 | 最大回撤 | 峰值→谷底 |
|---|---|---|---|
| 原 maxpos4 无冷却 (≈上期基线) | +494% | -13.1% | 24-05→07 |
| maxpos4 + 冷却20 | +478% | -13.3% | 26-05→06 |
| **maxpos5 + 冷却20 (推荐默认)** | **+551%** | **-11.9%** | 26-05→06 |
| maxpos6 + 冷却20 | +413% | -11.6% | 24-10 |
| maxpos5 弱市门禁 | +150% | -12.4% | (收益腰斩, 不取) |

- 5+冷却为双赢: 收益 +551% (原 494%) 且回撤 -13.1%→-11.9%。6 起收益明显回落。
- 弱市过滤/指数门禁压回撤更狠 (-12%→?) 但收益腰斩 (文档不采纳)。

改动文件:
- `wyckoff/paper/_params.py` `MAX_POSITIONS 4→5`, 新增 `STOP_COOLDOWN=20`;
- `wyckoff/strategies/evaluators.py` trading_discipline max_pos 4→5;
- `wyckoff/paper/_risk.py` `_stop_loss_reentry_blocked` (经 `_risk_blocks_entry`)+冷却;
- `wyckoff/paper/_trading.py`/`_conditions.py` `close_position` closed 记录增加 `day` 交易日;
- `wyckoff/settings_keys.py` Paper.STOP_COOLDOWN=`paper_stop_cooldown` + default 20;
- `wyckoff/config.py`/`settings_keys.py` DEFAULTS `paper_max_pos: 5` + `paper_stop_cooldown: 20`;
- `paper_replay_bt.py` / `spring_only_dd_analysis.py` 加 `--stop-cooldown N`。

## 复现命令
```bash
# 默认: maxpos5 + 冷却20, 从缓存回放
python scripts/spring_only_dd_analysis.py
python scripts/paper_replay_bt.py \
  --stocks-cache data/paper_replay_data/spring_only_conf100.pkl \
  --conf 100 --maxpos 5 --hold 20 --stop 0.04 --tp 0.15 --cost 0.004 \
  --start 2023-06-01 --trail-back 0.08 --no-bear-exit --stop-cooldown 20
```

## 待办 (下次优先)
1. **冷却用「交易日数」而非根数 (bars)** —— 现在是 bars(持有期)口径, 含非交易日更严;
   若要与 UI「N根」一致可改用 equity_hist 交易日序列计算 (当前用 bars 近似)。
2. 冷却是否也应用到 **手动/条件单平仓** (当前只对 stop_loss reason) —— 让手动止损
   也能触发冷却更贴近实盘预期。
3. 运行中的模拟盘: restart 后确认实盘真用 maxpos5+冷却20 (客户端若写回本地配置需重启)。
