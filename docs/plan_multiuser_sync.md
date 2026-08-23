# 多用户校准数据合并方案（待实施）

> 状态: 方案已确认，未开始实现。创建于 2026-08-23。
> 决策记录: 传输=Git 私有仓库 · 共享范围=数据+模型 · 触发=手动 · 隐私=不脱敏全量合并。

## 1. 目标

多台机器 / 多个用户各自运行桌面端积累的校准信息（信号评估记录、人工反馈标注），
通过共享私有 Git 仓库汇合成全量数据集，用合并后数据重训在线模型并回传共享，
使所有参与端共同受益于更大的样本覆盖（当前单端仅 55 只标的 / 1850 条标签）。

## 2. 架构

```
各端 ~/.wyckoff/*.json ←→ 共享私有 Git 仓库 (canonical 单文件 + pull-merge-push)
                              ├─ signals.json    全量信号记录
                              ├─ feedback.json   全量反馈标注 (verdict 是核心价值)
                              ├─ model.json      共享训练产物
                              └─ meta.json       schema版本/contributors/updated_ts
```

同步协议（`python -m wyckoff.sync sync` 或校准中心按钮）：

```
pull 远端 → 按键合并到本地 → 合并新增>0 时用全量重训 → 本地+远端 model.json 更新 → push
push 被拒(他人先推) → 自动重拉合并再推, 最多重试 2 次
```

## 3. 合并语义（确定性，任何一端执行结果一致）

| 对象 | 唯一键 | 冲突规则 |
|---|---|---|
| 信号 | `symbol\|scale\|kind\|type\|date` | 并集；冲突取 `last_eval_ts` 较新者（评估结果随行情推进更新） |
| 反馈 | `symbol\|scale\|start_dt\|end_dt` | 并集；非空 `verdict` 优先于空；都非空取时间戳新者 |
| 模型 | 整文件 | 仅当 `feat_version == 当前 FEATURE_VERSION` 且 `trained_ts` 更新才采纳，否则忽略并在 status 中提示 |

身份：首次运行生成 `~/.wyckoff/wx_machine_id`（uuid4），仅用于 meta.contributors 统计；
不做账号体系（仓库 SSH 密钥即身份）。不脱敏（股票代码/名称为公开信息，保留自选池结构才能扩大覆盖）。

## 4. 分步实施清单

### P1 合并核心
- 新模块 `wyckoff/sync.py`：
  - `export_bundle(include_model=True)` → `{schema, machine, exported_ts, signals, feedback, model}`
  - `merge_signals(local, remote)` → `(merged, n_new, n_upd)`
  - `merge_feedback(local, remote)` → 同上
  - `merge_model(local_state, remote_state)` → 采纳判定
  - `import_bundle(bundle)` → 写库并返回计数
- 存储写入沿用现有原子替换路径
- 测试 `tests/test_sync_merge.py`：双库重叠合并、新者胜、非空 verdict 优先、round-trip 计数守恒

### P2 Git 传输 + CLI
- 缓存目录 `~/.wyckoff/calib_repo/`（clone/pull）
- `config.DEFAULT_SETTINGS["calib_repo_url"] = ""`
- CLI 子命令：`setup <url>` / `pull` / `push` / `sync` / `status`
- `WYCKOFF_NO_NET=1` 时所有 git 操作静默跳过（测试离线安全）
- 测试用 `tmp_path` 内 `git init --bare` 端到端验证，含分叉后重拉重推路径

### P3 重训集成
- `sync` 尾部调用 `online_model.train_model()`（仅当 n_new+n_upd>0）
- 产物同时写本地 `wx_online_model.json` 与远端 `model.json`

### P4 校准中心 UI
- 模型 tab 增加「数据同步」区：
  - 首次输入仓库 URL → 存 settings
  - 「立即同步」按钮（QThread 后台，完成后刷新卡片）
  - 状态行：上次同步时间 / 远端样本数 / 本次新增数 / feat_version 不一致警告

### P5 收尾
- 全量回归（预计 503 → ~515）
- README 增「多端协同」章节

## 5. 使用方式（实施完成后）

```bash
# GitHub 建私有空仓后:
python -m wyckoff.sync setup git@github.com:<user>/<repo>.git   # 实际走 ssh.github.com:443
python -m wyckoff.sync sync                                     # 首推即初始全量库
```
当前本端初始数据量: 信号 6511 条（事件 1999）、反馈 416 条、v2 模型 OOS AUC 0.740。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 并发推送冲突 | git 层拒绝 → 重拉合并重推 ≤2 次；手动触发场景频率低 |
| 过期记录复活 | 某端 expire_stale_signals 删除的记录会被他端推回——接受（仅为旧评估样本，无害），文档注明 |
| feat_version 不一致（一端未升级代码） | 模型互不采纳，数据照常合并，status 显示警告 |
| 仓库膨胀 | JSON 文本 MB 级暂不优化；后续可切 msgpack（已有依赖） |

## 7. 相关现状备忘

- 信号键生成: `wyckoff/signal_accuracy.py:_key()`；反馈键: `wyckoff/storage.py:feedback_key()`
- 模型状态字段含 `feat_version/model_version/trained_ts/n_ctx_labels`
- 校准中心已有 stale-version 提示逻辑，P4 复用其刷新路径
- 推送凭证: remote 已配置 `ssh://git@ssh.github.com:443/...`，22 端口不可用
