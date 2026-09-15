# SC/BC 环境门 — 进度存档 (会话恢复用)

- 更新: 2026-09-15 (已落地, 见文末"已完成落地")
- 目标: 给量价事件 SC/BC 加"环境门"/分桶收紧触发, 提升方向命中率
- 来源: 上一轮事件层 survey 结论里"还有什么可提高"的第 1 项

## 背景数字 (全量 survey, `scripts/event_label_survey.py`, 5345 只)

池基准 (20 根上涨占比) ≈ 49%; 各事件 20 根方向命中率:

| 事件 | n | 命中 | 相对基准 | 均值 |
|---|---|---|---|---|
| Spring | 20513 | 81.1% | +32.2pt | +inf% |
| UTAD | 23086 | 79.8% | +30.9pt | -8.57% |
| Shakeout | 3770 | 79.0% | +30.1pt | +10.14% |
| LPS | 76 | 78.9% | +30.0pt | +10.69% |
| ST | 2061 | 74.2% | +25.3pt | +6.75% |
| SOW | 306 | 69.0% | +20.1pt | -4.03% |
| LPSY | 4491 | 68.9% | +20.0pt | -3.88% |
| UT | 1593 | 67.2% | +18.3pt | -3.31% |
| **SC** | 3488 | **59.5%** | +10.6pt | +3.84% |
| **BC** | 15762 | **55.5%** | +6.6pt | +0.77% |
| SOS | 413 | 29.3% | -19.6pt | (已降中性, 见 8f05b51) |

SC +10.6pt、BC +6.5pt 都是大样本但明显弱于 Spring/UTAD。当前 SC/BC 走
`_REVERSAL_CONFIRM_DIR` 确认方向, **没做过环境分桶**。本轮就是补这个。

## 已完成

1. 新建 `scripts/event_by_trend.py` (未提交, untracked):
   - 复用 `event_label_survey` 的全量事件管线 (SQLite 缓存 → add_indicators → find_pivots → detect_all)。
   - 对 SC/BC 按 4 个维度分桶统计 20 根方向命中率:
     - `trend`: MA20>MA50 且 close>MA50 (直接读 K 线列)
     - `boll`: boll_pct <0.35 / 中 / >0.65
     - `prior`: 事件前 20 根涨跌幅 (跌>8% / 横盘 / 涨>8%)
     - `prior_fine`: <-15% / -15~-8% / -8~-4% / 横盘 / +4~8% / +8~15% / >+15%
2. **踩坑 + 修复**: 初版读 `e["feat"]["trend"]` 得到全 0 (中性事件 SC/BC 在
   `event_confidence` 的 `if d:` 分支被跳过, `feat.trend` 没补)。已改为**直接
   从 K 线列** `close/price_ma20/price_ma50/boll_up/boll_dn` 现算环境, 并新增
   `prior_fine` 细分桶。

## 已有初步结果 (旧版脚本, 仅 trend/boll/prior)

存档 `docs/event_by_trend_baseline.txt` (即旧版 `/tmp/opencode/event_by_trend.txt`, 注意: 是修复前版本, trend 全 0):

- SC (n=3488, 59.5%): prior `跌>8%` → **64.6% (+5.2pt)**; `横盘` → 51.3% (-8.1pt, 随机);
  boll `低<0.35` 59.5%, `高>0.65` 50%。
- BC (n=15762, 55.5%): prior `涨>8%` → **57.7% (+2.1pt)**; `横盘` → 50.5% (-5.1pt, 随机)。

→ 假设: SC 需"前置大跌"环境、BC 需"前置大涨"环境才有效; 横盘触发的应降权/不触发。
待 `prior_fine` 细分找到精确拐点。

## 下一步 (下次继续)

1. 重跑修复版脚本 (约 6~7 分钟, 5425 只):
   ```bash
   python scripts/event_by_trend.py --limit 6000 > /tmp/opencode/event_by_trend2.txt 2>&1
   tail -60 /tmp/opencode/event_by_trend2.txt
   ```
   (上次用户中止了长跑, 未执行)
2. 看 `prior_fine` 拐点 + trend 分桶是否终于有 1 分桶样本, 确定 SC/BC 环境门阈值。
3. 落地:
   - `wyckoff/events.py` `detect_climaxes`: SC 加"前置 N 根跌幅 ≥ 阈值"条件;
     BC 加"前置 N 根涨幅 ≥ 阈值"条件 (阈值按 prior_fine 结果定)。
   - 或保留检测、在 `event_confidence`/`_event_score` 里按环境调 conf (更保守, 先验证再定)。
4. 回归: `python -m pytest tests -q` (基线 576 passed, 2 个 buypoints 既存失败)。
5. 提交 (脚本 + 门 + 测试)。

## 已完成落地 (2026-09-15)

调查已重跑 (修复版, 全量 5345 只, 结果存档 `docs/event_env_gate_results.txt`),
确认的精确分桶拐点:

| 事件 | 分桶 | n | 命中 | 结论 |
|---|---|---|---|---|
| SC | 前置 ≤-15% | 1025 | **70.4%** (+11pt) | 深跌环境最强 |
| SC | 前置 -15~-8% | 1109 | 59.3% (±0) | 中跌无增益 |
| SC | 前置 >-8% / 横盘 | 1358 | **50.6-51.8%** (-8pt) | 接近随机 → 降权 |
| BC | 前置 ≥+15% | 5925 | **60.5%** (+5pt) | 深涨环境最强 |
| BC | 前置 +8~15% | 5124 | 54.2% (-1.4pt) | 靠前无益 |
| BC | 横盘 ≤+4% | 4677 | **47.8-50.5%** (-5~-8pt) | 反向随机 → 降权 |
| BC | trend=1 (上升趋势) | 9517 | 53.0% vs 非升 59.3% | 上升趋势内 BC 更弱 |

落地位置选了**保守路线** (保留检测, conf 门): 因为 SC/BC 是 AR/ST/PSY/PSUP
结构链路锚点, 硬删会破坏吸筹/派发链条。改动全在 `wyckoff/events.py`
`event_confidence` 中 SC/BC 分支 (原 `if d:` 趋势块对中性事件恒跳过, 补前置动量门):

- SC: `prior_r20 ≤ -0.15` → +8; `> -0.08` → -15; 中间不变。
- BC: `prior_r20 ≥ +0.15` → +8; `≤ +0.04` → -12; 上升趋势内再 -5。
- 记录 `feat.prior_r20` 供后续核对 (online_model feature_vector 按名取值, 不受新键影响)。
- 同时修掉旧逻辑的隐性缺陷: BC 因 `up_i` 恒 False 恒吃 -15 (代码注释与调查结论相左),
  现 SC/BC 的 -15 分支从弱信号过滤区移出, 由环境门统一接管。

验证:
- 新增 `tests/test_tags_audit.py` 环境门用例 (SC/BC 深环境 vs 平凡环境 conf 排序)。
- 全量回归 `616 passed`。

## 同轮列出的其它待办 (未开工)

- SOW 收紧: 69% 命中但 conf 均值仅 31 且样本 306, 可要求 UTAD/LPSY 共振或放量破位再计分。
- LPS/JOC 链路: LPS 78.9% 最强第二梯队但样本仅 76; `detect_joc_lps_bu` 里 JOC/BU
  分支 600 只产出 0 (疑死代码), 需先验 JOC 方向 (可能像 SOS 反向) 再决定是否放宽。
- conf→命中率校准校验: 对弱信号 (BC/SOW) 按 conf 分桶看高 conf 是否真高命中。
- VSA 看多标签复查: DEM/ETR/SPR/SV 两环境都贴近随机, 可按阶段/量能分位再挖子集。

## 相关文件

- `scripts/event_by_trend.py` (本轮, untracked)
- `scripts/event_label_survey.py` (已提交 8f05b51)
- `wyckoff/events.py` `detect_climaxes` (L203) / `_REVERSAL_CONFIRM_DIR`, `wyckoff/config.py` `event_dir`
- 基线输出存档 (已复制入仓库): `docs/event_by_trend_baseline.txt` (旧版), `docs/event_label_low_n.txt`
