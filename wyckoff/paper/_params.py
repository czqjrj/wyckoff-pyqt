"""模拟盘纯参数层: 常量 / 枚举 / 数据类 (无共享可变状态, 叶子模块)。

原 paper.py 顶部的可配置策略参数、风控参数、订单枚举与数据类。
被 __init__.py (内核/门面) 以 `from ._params import *` 吸收, 对外命名不变。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


# ── 可配置策略参数 ─────────────────────────────────────────
# 持仓周期 K 根 (日线, ~1 个月)
HOLD_BARS = 20
# 同持最大股票数 (资金/风险分散, 参考组合回测 MAXPOS=3)
MAX_POSITIONS = 3
# 每笔资金占比 (1/MAX_POSITIONS 等权)
_POS_WEIGHT = 1.0 / MAX_POSITIONS
# 单边成本 (含佣金+印花税+滑点, 参考 backtest.cost=0.004)
COST = 0.004
# 买入滑点 (价格摩擦, 占成交价比例)
SLIP_BUY = 0.001
SLIP_SELL = 0.001
# 止损: 回测折中配置取 -4% (docs/paper3_backtrader_bt_improvement.md:
# 全周期 +93%/夏普1.07/回撤-13.8%, 优于 -3% 被噪音反复洗出与 -6% 风险敞口过大)
STOP_LOSS = 0.04
# 追踪止损(移动止盈): 默认开启。语义= 先让利润奔跑至激活浮盈价
# (TRAIL_ACTIVATE_PCT, 默认=TAKE_PROFIT), 之后才从峰值回落 TRAIL_BACK_PCT 平仓;
# 未激活前仅由固定 -STOP_LOSS 兜底 (与回测移动止盈口径一致)。
TRAILING_STOP = True
# 追踪止损 ATR 缓冲: 仅在 TRAILING_STOP=True 时生效 (无网格实证, 默认关闭)
TRAIL_ATR_MULT = 0.0
# 移动止盈: 高位回落触发幅度 (峰值* (1-此值) 平仓)
TRAIL_BACK_PCT = 0.08
# 移动止盈激活浮盈比例: 浮盈达到该值后才启用回落卖出; <=0 表示取 TAKE_PROFIT
TRAIL_ACTIVATE_PCT = 0.0
# 弱市过滤: 指数收盘 < MA20 判定为弱势 → 新开仓上限 WEAK_MAX_POS 且停用价值吸筹
WEAK_FILTER = True
WEAK_MAX_POS = 1
# 弱市断言指数代码 (与回测脚本一致, 上证指数)
WEAK_INDEX_CODE = "sh000001"
# 价值吸筹单仓资金权重 (其余策略=1.0; 降低弱策略敞口)
VA_WEIGHT = 0.6
# 价值吸筹策略总开关 (实测为三策略最弱, 可整体停用仅保留纪律+左侧)
ENABLE_VA = True
# 周期级等权再平衡: 满仓且现金富余时, 把权重过低的持仓补足到 总权益/max_pos,
# 消除"先买的大、后买的小"的顺序衰减与资金闲置 (利用率仅 ~66% 的根因)。
REBALANCE = True
# ── 微信推送 (交易发生时通知, 渠道见 wechat_push.push_to_wechat) ─────
PUSH_ENABLED = False
PUSH_METHOD = "server_chan"  # "server_chan" | "wechat_work" | "wxpusher"
PUSH_SERVER_CHAN_KEY = ""
PUSH_WECHAT_CORP_ID = ""
PUSH_WECHAT_CORP_SECRET = ""
PUSH_WECHAT_AGENT_ID = ""
PUSH_WECHAT_TO_USER = ""
PUSH_WXPUSHER_APP_TOKEN = ""
PUSH_WXPUSHER_TOPIC_IDS = ""  # 逗号分隔主题 ID
PUSH_WXPUSHER_UIDS = ""       # 逗号分隔用户 UID
# 止盈: 盈利 +15% 落袋 (结合止损的不对称盈亏比)
TAKE_PROFIT = 0.15
# 初始终端资金 (模拟资产)
INIT_CASH = 1_000_000.0
# 单笔最低可交易金额 (避免碎股/零股本)
MIN_LOT = 100.0
# conf 过滤下限: 优化后提升至 100 (2026-09-04 大样本回测验证: conf=100
# 胜率 61%, 显著优于 conf=90-99 段的 33%)
MIN_CONF = 100

# ── 风控参数 ──────────────────────────────────────────────
# 最大账户回撤限制 (触发时停止开新仓, 仅平仓)
MAX_DRAWDOWN_PCT = 0.15
# 单笔最大风险预算 (账户净值的百分比, Kelly 计算上限)
MAX_RISK_PCT = 0.02
# 最大行业集中度 (单行业持仓市值占总市值上限)
MAX_SECTOR_CONCENTRATION = 0.40
# 最大单股集中度 (单股持仓市值占总市值上限)
MAX_SINGLE_CONCENTRATION = 0.25
# 相关性阈值 (拒绝开仓高相关标的, 需外部相关性矩阵)
CORRELATION_THRESHOLD = 0.70
# 波动率调整: 高波动降低仓位, 低波动提高仓位 (ATR 百分位)
VOL_ADJUST_ENABLED = True
VOL_PERCENTILE_HIGH = 0.80
VOL_PERCENTILE_LOW = 0.20
# 资金利用率上限 (防止满仓无现金应对机会)
MAX_CAPITAL_USAGE = 0.95

# ── 订单类型 ──────────────────────────────────────────────
class OrderType(Enum):
    MARKET = "market"           # 市价单
    LIMIT = "limit"             # 限价单
    STOP = "stop"               # 止损单
    STOP_LIMIT = "stop_limit"   # 止损限价单
    OCO = "oco"                 # 一单撤一单 (止盈+止损)
    BRACKET = "bracket"         # 括号单 (入场+止盈+止损)
    ICEBERG = "iceberg"         # 冰山单 (分批显示)
    SCALE_IN = "scale_in"       # 分批建仓
    SCALE_OUT = "scale_out"     # 分批平仓
    TRAILING = "trailing"       # 追踪止损

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class OrderStatus(Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

class PositionSizingMethod(Enum):
    EQUAL_WEIGHT = "equal_weight"           # 等权
    KELLY = "kelly"                         # Kelly 公式
    VOLATILITY_ADJUSTED = "vol_adjusted"    # 波动率调整
    RISK_PARITY = "risk_parity"             # 风险平价
    FIXED_FRACTIONAL = "fixed_fractional"   # 固定分数
    CONF_WEIGHTED = "conf_weighted"         # 置信度加权


@dataclass
class AdvancedOrder:
    """高级订单数据类"""
    order_id: str
    symbol: str
    name: str
    order_type: OrderType
    side: OrderSide
    qty: int
    price: float | None = None          # 限价
    stop_price: float | None = None     # 止损触发价
    limit_price: float | None = None    # 止损限价
    trail_pct: float | None = None      # 追踪止损百分比
    trail_price: float | None = None    # 追踪止损激活价
    parent_id: str | None = None        # 父订单 ID (用于 OCO/括号单)
    child_ids: list = field(default_factory=list)  # 子订单 ID
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    created_ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    updated_ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    expiry_ts: str | None = None        # 过期时间
    tags: dict = field(default_factory=dict)  # 自定义标签 (如 strategy, confidence)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "name": self.name,
            "order_type": self.order_type.value,
            "side": self.side.value,
            "qty": self.qty,
            "price": self.price,
            "stop_price": self.stop_price,
            "limit_price": self.limit_price,
            "trail_pct": self.trail_pct,
            "trail_price": self.trail_price,
            "parent_id": self.parent_id,
            "child_ids": self.child_ids,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "expiry_ts": self.expiry_ts,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AdvancedOrder:
        return cls(
            order_id=d["order_id"],
            symbol=d["symbol"],
            name=d.get("name", ""),
            order_type=OrderType(d["order_type"]),
            side=OrderSide(d["side"]),
            qty=d["qty"],
            price=d.get("price"),
            stop_price=d.get("stop_price"),
            limit_price=d.get("limit_price"),
            trail_pct=d.get("trail_pct"),
            trail_price=d.get("trail_price"),
            parent_id=d.get("parent_id"),
            child_ids=d.get("child_ids", []),
            status=OrderStatus(d.get("status", "pending")),
            filled_qty=d.get("filled_qty", 0),
            avg_fill_price=d.get("avg_fill_price", 0.0),
            created_ts=d.get("created_ts", ""),
            updated_ts=d.get("updated_ts", ""),
            expiry_ts=d.get("expiry_ts"),
            tags=d.get("tags", {}),
        )


@dataclass
class PositionRisk:
    """持仓风险指标"""
    symbol: str
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    var_95: float = 0.0                    # 95% VaR
    var_99: float = 0.0                    # 99% VaR
    beta: float = 1.0                      # 相对大盘 Beta
    correlation_risk: float = 0.0          # 组合相关性风险
    sector_exposure: float = 0.0           # 行业敞口
    concentration_risk: float = 0.0        # 集中度风险
    liquidity_risk: float = 0.0            # 流动性风险 (基于换手率/市值)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "var_95": self.var_95,
            "var_99": self.var_99,
            "beta": self.beta,
            "correlation_risk": self.correlation_risk,
            "sector_exposure": self.sector_exposure,
            "concentration_risk": self.concentration_risk,
            "liquidity_risk": self.liquidity_risk,
        }

