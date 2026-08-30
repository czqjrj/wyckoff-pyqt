# 账户私有数据同步方案（已实施）

> 状态: 已实施并测试 (2026-08-30)。核心 `wyckoff/profile_sync.py` + CLI + 设置页 UI。
> 关联: `docs/plan_multiuser_sync.md`（多用户**校准数据**合并）——两者是**两套独立同步**，共用传输层，但对象/范围/冲突语义不同。

## 1. 目标与定位

同一用户在多台设备跑本桌面端时，让**账户私有数据**保持一致：
UI 布局、主题、字体、自选、候选、观察清单、笔记、组合、模拟盘账户等。
**与多用户校准合并的区别**：

| | 校准同步 (plan_multiuser_sync) | **档案私有同步（本文）** |
|---|---|---|
| 参与者 | 多用户共享 | **同一账户多设备** |
| 数据 | 信号/反馈/模型（喂模型） | UI布局/自选/笔记/组合/模拟盘（个人偏好与状态） |
| 目标 | 扩大样本、提高准确度 | 多设备无缝一致 |
| 仓库 | 1 个**共享**私有仓 (`calib`) | 每账户 **1 个独立**私有仓 (`profile`) |
| 合并 | 并集 + 新者胜（喂重训） | 轻量文件级合并 / last-write-wins |
| 隐私 | 不脱敏全量（公开股票） | **排除敏感键**（API key 等）+ 只存私有偏好 |

## 2. 身份/账户（保持简单，不引第三方账号体系）

沿用现有 Git 私有仓思路，**用「私有仓库 = 账户云端」，SSH key 即身份**：

```
calib_repo_url   → 共享校准仓 (多用户, 已规划)
profile_repo_url → 该账户的私有档案仓 (新)
```

- 一个账户 = 一个私有 Git 仓；不需要用户名/密码服务器。
- 首次设置输入 `profile_repo_url`（如 `git@github.com:<user>/<repo>.git`），
  凭据用本机 SSH key / git credential helper。
- 多设备 = 同一个 `profile_repo_url` 同时 clone，靠 git pull-merge-push 保持一致。

> 若今后需要真·账号（web 登录），可后续加一层 `wx_profile_server` 代理，但保持
> 本方案不依赖任何外部服务器（完全离线可跑 + WYCKOFF_NO_NET=1 静默跳过）。

## 3. 同步对象清单（明确「同步什么 / 不同步什么」）

### 3.1 同步（私有，同账户一致）
| 对象 | 文件 | 键/结构 | 合并语义 |
|---|---|---|---|
| 设置（UI/主题/字体/面板/自选宽度） | `wyckoff_settings.json` | `General.**`,`UI.**`,`Chart.**`,`Watch.**` 等（白名单） | last-write-wins 按 key |
| 自选股 | `wyckoff_watchlist.json` | list[code] | **并集**（保留两端） |
| 候选 | `wyckoff_candidates.json` | 列表 | last-write-wins |
| 待观察清单 | settings `candidates` 相关 | 表 | last-write-wins |
| 组合 | `wx_portfolio.json` | 持仓 | 并集按 code + 新者胜 |
| 笔记 | `wx_notes.json` | dict | 并集按 key + 新者胜 |
| 交易日志 | `wx_entry_journal.json` | list | 并集 + 去重 |
| 模拟盘账户 | `wx_paper.json` | 资金/持仓/流水 | **整文件 last-write-wins**（需防同设备并发） |

### 3.2 不同步（排除）
- **敏感**：`AI.API_KEY`（密钥）、任何 token/password。
- **机器本地**：`last_analyzed_*`（运行时记忆，各设备各自）、`STOCK_NAMES`、`ALL_STOCKS`、缓存 DB。
- **共享校准**：`wx_signal_accuracy / wx_feedback / wx_online_model`（走校准仓）。
- 大缓存/只读衍生：`wyckoff_all_stocks.json`、`wx_accuracy.json`、`wyckoff_stock_names.json`。

### 3.3 敏感过滤机制
`wyckoff_settings.json` 混有 `ai_api_key`。同步前用**白名单**抽取，绝不整文件上传：
- 白名单 = `S.General.*` + `S.UI.*` + `S.Chart.*` + `S.Watch.*`（可加 `S.Auto.*` 开关类）
- 其余键（AI.* / Runtime.* / 其它）一律**不进入档案仓**。
- 保险：任何值含 `key/token/secret/password` 字样的键名直接跳过。

## 4. 架构

```
各端本地 JSON ←→ git 私有档案仓 (profile_repo_url)
                    ├─ profile.settings.json   (白名单抽取后的设置)
                    ├─ watchlist.json
                    ├─ notes.json / journal.json
                    ├─ candidates.json / portfolio.json
                    └─ paper.json              (可选用)
协议: pull → 合并 → 本地写 → push (push 冲突自动重拉合并重推 ≤2 次)
```

传输层**复用**校准同步的 git 封装（`wyckoff/sync.py` 内部共用 `_ProfileRepo`/`_git_cmd`），
同一套 pull-merge-push + WYCKOFF_NO_NET 逻辑，降低重复。

## 5. 合并语义

| 对象 | 策略 |
|---|---|
| 设置白名单 | 合并 dict：远端有而本地无 → 写入；同键 → **更新时间戳新者胜**（设置带 `_profile_ts`) |
| 自选 list | `本地 ∪ 远端` 去重保序（并集，最安全，不得罪任何设备） |
| 候选/组合/笔记 | 按各自唯一键并集 + 冲突取 `updated_ts` 新者 |
| 模拟盘 `paper.json` | 整文件 last-write-wins + 状态 `ts`；同设备双开由 GUI 单实例规避 |

确定性：任一设备执行得到相同结果（可复现、可测试）。

## 6. 分步实施清单

### P1 核心合并模块（`wyckoff/profile_sync.py`）
- `collect_profile()` → 从本地抽取白名单设置 + watchlist + notes 等 → bundle dict
- `apply_profile(bundle)` → 写回本地对应文件（敏感过滤后）
- `merge_settings(local, remote)` / `merge_watchlist(a,b)` / `merge_by_key(...)`
- `SENSITIVE_KEY_HINT` 过滤集 + `SETTINGS_WHITELIST`
- 测试 `tests/test_profile_sync.py`：白名单抽取、key 过滤、自选并集、新者胜

### P2 Git 传输（复用 `sync.py` git 封装）
- `config.DEFAULT_SETTINGS["profile_repo_url"] = ""`、`["profile_sync"] = False`（settings_keys 加键）
- `python -m wyckoff.sync profile-setup <url>` / `profile-pull` / `profile-push` / `profile-sync`
- push 冲突重拉重推≤2；WYCKOFF_NO_NET=1 静默跳过
- 测试用 `tmp_path` bare repo 端到端，含两设备分叉合并且不丢键

### P3 UI（`ui/threads/auto_sync_thread.py` 复用 + 校准中心/设置页）
- 设置页「同步」区：`profile_repo_url` 输入 + 「立即同步档案」
- 复用 `AutoSyncThread` 跑后台，完成刷新自选/面板
- 可选：`Auto.AUTO_SYNC` 开则退出/启动时自动同步档案

### P4 收尾
- 全量回归；README 增「账户数据同步」章节；与校准同步区分说明

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| settings 混入敏感 key | 白名单抽取 + 键名关键字兜底过滤，绝不整文件上传 |
| 多设备同时改自选 | 并集语义，两端都不丢；删除需用户在两端一起删（文档注明） |
| 模拟盘账户并发写 | 整文件 last-write-wins + GUI 单实例锁；文档提示不同时双开 |
| 仓库膨胀/冲突 | JSON 文本 MB 级暂不优化；git 层冲突自动重拉合并重推≤2 |
| 与校准同步混淆 | 两个独立 `.repo_url` 配置、两套子命令、UI 分两栏 |

## 8. 已确认决策（已按此实现）

1. **同步范围**：纳入模拟盘账户（`wx_paper.json` 整文件 LWW）。
2. **身份**：Git 私有仓 = 账户云端，无第三方账号服务器（SSH key / git 凭证即身份）。
3. **删除语义**：支持删除（tombstone + 逐条目时间戳 LWW）——自选删一只会在其它设备删除，而非并集不删。

## 9. 实施记录

- `wyckoff/profile_sync.py`：核心（collect/merge/apply + 白名单敏感过滤 + 影子跟踪删除）+ git 传输 + CLI。
- `wyckoff/account.py`：**账户档案与登录态**（`account.json`）——登录(可校验仓库可达)/退出/切当前账户/多账户；CLI `python -m wyckoff.account login|logout|switch|remove|status`。
- profile_sync 整合：`_active_repo_url()` 优先取当前登录账户仓库；`status()` 透出账户态；CLI `logout/account`。
- CLI：`python -m wyckoff.profile_sync setup <url>|pull|push|sync|status|logout|account`；`WYCKOFF_NO_NET=1` 静默跳过。
- settings_keys / config 新增 `profile_repo_url`、`profile_sync`。
- UI：`ui/settings_pages.py` GeneralPage「账户登录与同步」区（账户名 + 仓库地址 + 登录/退出 + 立即同步）。
- 测试：`tests/test_profile_sync.py`、`tests/test_profile_sync_e2e.py`（两设备端到端含删除传播）、`tests/test_account.py`（登录态）。
- .gitignore 忽略 `/profile_repo/`、`/profile_shadow.json`、`/account.json`。

---
*历史方案，供评审后实施。*
