# wyckoff-pyqt

**威科夫 (Wyckoff) 方法 A 股桌面分析客户端** —— 基于 PyQt6 构建的单机版技术分析工作台，覆盖从「看盘 → 结构研判 → 选股 → 回测验证 → AI 解读」的完整闭环。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Qt](https://img.shields.io/badge/GUI-PyQt6-green)
![License](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)

---

## ✨ 功能特性

### 📊 五大图表页

| 页面 | 内容 |
|------|------|
| **K线图** | 主图 + 成交量 + 指标多面板联动；威科夫事件自动标注（Spring / SC / ST / SOS / PSY 等） |
| **P&F 点数图** | 传统点数图绘制 + 量能投影 + 历史目标价测算 |
| **技术指标** | 八宫格指标面板，每格下方附「当前信号 → 预示」解读行 |
| **资金透视** | 供需区、成交量分布 (Volume Profile)、相对强弱、交易区间判定 |
| **解读** | AI 对整篇分析报告的通俗化解读，支持重新生成与语音朗读 |

所有图表支持：滚轮以光标为锚点缩放、键盘平移/复位、双击全幅、跨面板 X 轴联动、十字光标读数。

### 🧠 分析引擎 (`wyckoff/`)

- **阶段判定**：吸筹 / 上升 / 派发 / 下跌 / 区间五阶段 + 阶段分段追踪，结构进度按因果链推进
- **事件体系**：威科夫经典事件 + VSA 信号分类 + 多周期共振 (multi-timeframe) + 信号融合
- **回测与实证**：单事件回测、VSA 胜率统计、九大稳健性测试、反证机制、方向化胜率口径统一
- **在线校准**：校准中心跟踪信号实测胜率，综合选股只认实证有效的信号门槛

### 🔍 选股体系

- **综合选股**：威科夫阶段 + 基本面 + 资金流 + 技术指标多维评分排序，支持市值/PE/PB/板块/最低总分过滤，严格无果时自动放宽条件
- **扫描中心**：20 类内置策略扫描（含板块扫描、全市场扫描）
- **待观察候选池**：扫描结果一键入池跟踪

### 🤖 AI 能力（可选）

- 整篇报告通俗解读、重新生成、TTS 语音播报
- **AI 问股**：注入当前报告 + 该标的历史信号实证的多轮对话追问
- 单个信号标签的解释与证伪
- 支持 DeepSeek / OpenAI 及任意兼容 API，不配置 Key 时全部功能照常可用

### 💼 组合工具

- **自选股管理** + 键盘精灵（全市场 A 股代码/名称/拼音搜索，本地索引离线命中）
- **我的持仓**：盈亏跟踪
- **资金监测**：国家队持仓透视（汇金/证金/社保）、国家队 ETF 三因子跟踪
- **多股票对比**、指数行情（上证/深成/创业板/科创50…）
- **自选股预警**、个股备注、分析报告导出、图表导出

---

## 📊 准确率实证

内置信号评估闭环（评估 → 重训 → 置信度接管全自动化），基于 **6,000+ 条真实 A 股信号**的滚动实测统计：

- **强信号梯队**（20 根 K 线方向命中率，显著优于 54% 随机基准）：

  | 事件 | Spring | Shakeout | UTAD | LPSY | ST | LPS | SC |
  |------|--------|----------|------|------|----|----|-----|
  | 命中率 | **84.9%** | **82.9%** | **79.9%** | **78.8%** | **72.4%** | 65.5% | 65.3% |

- **置信度校准模型**：样本外 IC = **0.446**、方向准确率 **67.0%**，已在线接管置信度输出
- **PnF 三档目标测算**（602 段）：近端基准命中 **96.7%**，下方档位概率校准误差 < 1.5pt
- **诚实的负结论**：SOS / JOC / PSY 及全部 VSA 类与随机无异，引擎自动将其降权为确认证据

完整数据、方法论与改进建议见 [docs/accuracy_report.md](docs/accuracy_report.md)，信号复盘周报见 [docs/signal_review.md](docs/signal_review.md)。

---

## 🚀 安装运行

```bash
# 1) 创建环境 (Python >= 3.10)
conda create -n wyckoff-pyqt python=3.10 -y
conda activate wyckoff-pyqt

# 2) 安装依赖
pip install -r requirements.txt

# 可选: 解读语音播报
pip install edge-tts        # 微软在线语音, 音质最佳 (需网络)
pip install pyttsx3         # 离线方案

# 3) 启动
./run.sh                    # Linux (自动处理 Qt 平台插件问题)
python wyckoff_desktop.py   # 通用方式
```

> 数据来源为东方财富 / 新浪 / 腾讯公开接口，无需任何 Token；历史 K 线走本地缓存，首次拉取后明显提速。

---

## ⌨️ 常用快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+O` | 加载分析 / 键盘精灵搜索股票 |
| `Ctrl+Shift+S` | 切换到综合选股 |
| `Ctrl+,` | 打开设置 |
| `Home` / `R` | 图表复位全幅 |
| `Backspace` / `F` | 视图历史后退 / 前进 |

完整清单见程序内 **帮助 → 使用说明**（`docs/help.html`）。

---

## 🔄 多端协同 (可选)

多台机器 / 多个用户各自积累的校准数据（信号评估记录、阶段带反馈标注）可通过一个**私有 Git 仓库**汇合，合并后自动重训在线模型并回传，所有参与端共享更大的样本覆盖：

```bash
# GitHub 建一个私有空仓后, 各端执行一次:
python -m sync setup git@github.com:<user>/<repo>.git
# 日常同步 (也可在校准中心 → 模型校准 → 数据同步区点击「立即同步」):
python -m sync sync
```

- 协议: `pull 远端 → 按键确定性合并 → 合并新增>0 时全量重训 → push`；push 被拒自动重拉合并重推
- 身份: 首次运行生成 `~/.wyckoff/wx_machine_id`，仅用于贡献统计，无账号体系；仓库 SSH 密钥即身份
- 模型互采: 仅当特征集版本一致且训练时间更新才采纳对方模型，避免旧系数污染

详见 [docs/plan_multiuser_sync.md](docs/plan_multiuser_sync.md)。

---

## 📁 项目结构

```
├── wyckoff_desktop.py    # 入口
├── run.sh                # Linux 启动脚本 (修复 Deepin dxcb 插件问题)
├── desktop/              # PyQt6 界面层
│   ├── main_window.py    #   主窗口: 三栏布局 + 后台分析线程
│   ├── base_plot.py      #   pyqtgraph 图表基类 (缩放/平移/十字光标/联动)
│   ├── kline_widget.py   #   K线图 · pnf_widget.py 点数图
│   ├── ind_widget.py     #   技术指标八宫格 · mkt_widget.py 资金透视
│   ├── extra_windows.py  #   综合选股 / 扫描中心 / 国家队等次级窗口
│   └── theme.py          #   浅色/深色双主题 (图纸墨水风浅色)
├── wyckoff/              # 纯 Python 分析内核 (无 GUI 依赖, 可独立复用)
│   ├── analysis.py       #   run_analysis 完整流水线
│   ├── events.py phases.py vsa.py fusion.py structure.py
│   ├── screener.py backtest.py calibration.py online_model.py
│   └── datasource.py     #   东财/新浪/腾讯 行情接口 + 缓存
└── docs/help.html        # 内置使用手册
```

---

## ⚠️ 免责声明

本项目仅供学习与技术交流使用，所输出的信号、评分与结论均来自历史数据的算法统计，**不构成任何投资建议**。股市有风险，入市需谨慎。
