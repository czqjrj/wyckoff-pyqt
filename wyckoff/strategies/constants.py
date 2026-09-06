"""威科夫策略管理器 · 常量与策略 key 注册 (单一来源)。

拆分自 manager.py: 本模块只承载纯粹常量/标识, 不产生任何副作用。
candidates (模拟盘候选插件表) 与 evaluators (信号评估器) 各自独立模块,
manager 门面仅从这里导入再统一导出, 保证既有 import 路径全部兼容。
"""

# 多头吸筹事件集: 综合选股「价值吸筹」与策略4共用
LONG_EVENT_TYPES = ("Spring", "Shakeout", "ST", "LPS", "SC")

# SOS动态确认窗口 (源自 events.py DYNAMIC_WINDOW)
SOS_CONFIRM_WINDOW = 5

# Spring回踩确认窗口: Spring后确认窗口根数
SPRING_CONFIRM_WINDOW = 8

# ── 模拟盘策略 key (自描述字符串, 供注册表引用) ──────────────
STRATEGY_DISCIPLINE = "paper_discipline_bull"
STRATEGY_VALUE_ACC = "screener_value_accumulation"
STRATEGY_LONG_LEFT = "long_buy_left"

# 价值吸筹/候选池低质股票过滤 (北交所/ST·退市/低价 -> 排除)
VA_EXCLUDE_BJ = True
VA_EXCLUDE_ST = True
VA_MIN_PRICE = 3.0
# 价值吸筹 conf 下限: 兜底信号也须有足够置信度
VA_MIN_CONF = 80

# 威科夫完整做多买点·左侧起仓 (独立赛道, 不受大盘/板块/资金流门禁管束)
LONG_MIN_CONF = 60      # 实证左侧最佳为左侧试探/高盈亏比; 用较宽下限避免过度挑剔
LB_ENTRY_MARGIN = 0.0   # 左侧 buy_price 触发价: 直接挂买点入场价 (低风险左侧, 等回踩)
LB_MAX = 8              # 单期扫描最多回传的左侧候选数 (按 conf/盈亏比排序截断)

# 纪律强多头事件近端可买入窗口 (根)
DISCIPLINE_EVENT_WINDOW = 10
