"""模拟盘 (Paper Trading) 核心引擎。

自动筛选 → 自动下单 → 自动卖出 → 收益统计 全自动闭环, 无真实资金风险。

设计要点 (对齐项目已验证的实证参数, 见 docs/profitability_bt.md):
  - 选股: 全市场 universe 逐股 detect_all, 只取强多头事件 (Spring/
    ST/LPS) 且 conf 达标 (默认 ≥100) 的标的, 按 conf 排序。
  - 撮合: 候选按 conf 填充, 同持上限内用"最近收盘价 + 滑点"买入
    (无未来函数: 同一周期内新成交仓位不参与当期卖出评估)。
  - 卖出触发: 持有满 HOLD_BARS 根到期 / 结构位-4%止损 / 破位 /
     +15%移动止盈 (浮盈达止盈线后从峰值回落8%平仓)。
  - 统计: 分类型/总账户 胜率、盈亏比、净值曲线、最大回撤。
  - 存储: 单 JSON (wx_paper.json), 与项目其他 wx_* 数据文件同目录同风格。
数据目录用 paths.DATA_DIR, 测试用 WYCKOFF_DATA_DIR 隔离。

增强功能:
  - 风控: 最大回撤限制、相关性限制、Kelly 资金管理、波动率调整仓位
  - 高级订单: OCO、括号单、分批建仓/平仓、冰山单
  - 实时监控: WebSocket 行情推送、价格触发条件单
  - 绩效分析: 夏普/索提诺/卡尔马比率、归因分析、回撤分析
"""
from __future__ import annotations

import itertools
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from . import paper_log, paper_strategy_accuracy
from .paths import PAPER_FILE
from .settings_keys import S
from .strategies.constants import (
    STRATEGY_DISCIPLINE,
    STRATEGY_LONG_LEFT,
    STRATEGY_VALUE_ACC,
)
from .trading_time import gate_reason

_LOCK = threading.RLock()

# 条件单 cid 递增序号: 时间戳微秒在同一轮回/并发下会重复 (曾出现 trailing 与
# stop_loss 紧邻创建同 cid 的撞车), 追加序号保证全进程内唯一。
_CID_SEQ = itertools.count(1)

# 全市场重扫的冷却窗口 (分钟): run_cycle 在多周期场景下复用候选, 避免每 30 分钟
# 前一次全市场扫描的候选被下一次周期无条件重扫覆盖 (与 run_scan 的 next_scan_time 一致)。
_SCAN_COOLDOWN_MIN = 30

# 策略管理器单例占位 (延迟实例化, 见 _strategy_manager)
_SMGR = None

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


def apply_paper_params(settings=None):
    """从用户设置 dict 解析并覆盖模拟盘策略参数, 返回当前生效参数字典 `_CUR`.

    不传/键缺失时回退到模块常量默认值, 保证与旧行为完全一致 (测试兼容)。
    支持键 (见 settings_keys.Paper): INIT_CASH/MAX_POS/HOLD_BARS/STOP_LOSS/
    TAKE_PROFIT/COST/MIN_CONF。调用方 (run_cycle/UI) 在周期执行前调用即可生效。

    新增风控参数: MAX_DRAWDOWN/MAX_RISK_PCT/MAX_SECTOR_CONC/MAX_SINGLE_CONC/
    CORRELATION_THRESHOLD/VOL_ADJUST_ENABLED/MAX_CAPITAL_USAGE
    """
    settings = settings or {}

    def _get(key, default):
        v = settings.get(key)
        return default if v is None or v == "" else v

    global _CUR
    _CUR = {
        "init_cash": float(_get(S.Paper.INIT_CASH, INIT_CASH)),
        "max_pos": max(1, int(_get(S.Paper.MAX_POS, MAX_POSITIONS))),
        "hold_bars": max(1, int(_get(S.Paper.HOLD_BARS, HOLD_BARS))),
        "stop_loss": float(_get(S.Paper.STOP_LOSS, STOP_LOSS)),
        "take_profit": float(_get(S.Paper.TAKE_PROFIT, TAKE_PROFIT)),
        "cost": float(_get(S.Paper.COST, COST)),
        "min_conf": int(_get(S.Paper.MIN_CONF, MIN_CONF)),
        # 风控参数
        "max_drawdown": float(_get("paper_max_drawdown", MAX_DRAWDOWN_PCT)),
        "max_risk_pct": float(_get("paper_max_risk_pct", MAX_RISK_PCT)),
        "max_sector_conc": float(_get("paper_max_sector_conc", MAX_SECTOR_CONCENTRATION)),
        "max_single_conc": float(_get("paper_max_single_conc", MAX_SINGLE_CONCENTRATION)),
        "correlation_threshold": float(_get("paper_correlation_threshold", CORRELATION_THRESHOLD)),
        "vol_adjust_enabled": bool(_get("paper_vol_adjust_enabled", VOL_ADJUST_ENABLED)),
        "max_capital_usage": float(_get("paper_max_capital_usage", MAX_CAPITAL_USAGE)),
        # 资金管理方式
        "sizing_method": _get("paper_sizing_method", PositionSizingMethod.EQUAL_WEIGHT.value),
        # 板块权限: 未开通创业板/科创板 → 扫描/选股排除对应代码
        "enable_chinext": bool(_get(S.Paper.ENABLE_CHINEXT, False)),
        "enable_star": bool(_get(S.Paper.ENABLE_STAR, False)),
        # 移动止盈/追踪止损: 峰值回撤平仓; 开启 trail_back_pct 时由条件单先"激活后回落"
        "trailing_stop": bool(_get(S.Paper.TRAILING_STOP,
                                   _get("paper_trailing_stop", TRAILING_STOP))),
        "trail_atr_mult": float(_get(S.Paper.TRAIL_ATR_MULT,
                                     _get("paper_trail_atr_mult", TRAIL_ATR_MULT))),
        "trail_back_pct": float(_get(S.Paper.TRAIL_BACK_PCT, TRAIL_BACK_PCT)),
        "trail_activate_pct": float(_get(S.Paper.TRAIL_ACTIVATE_PCT,
                                         TRAIL_ACTIVATE_PCT)),
        # 弱市降仓过滤
        "weak_filter": bool(_get(S.Paper.WEAK_FILTER, WEAK_FILTER)),
        "weak_max_pos": max(1, int(_get(S.Paper.WEAK_MAX_POS, WEAK_MAX_POS))),
        "weak_index_code": _get(S.Paper.WEAK_INDEX_CODE, WEAK_INDEX_CODE),
        # 价值吸筹资金降权
        "va_weight": float(_get(S.Paper.VA_WEIGHT, VA_WEIGHT)),
        # 价值吸筹总开关 (False=彻底停用)
        "enable_va": bool(_get(S.Paper.ENABLE_VA, ENABLE_VA)),
        # 周期级等权再平衡
        "rebalance": bool(_get(S.Paper.REBALANCE, _get("paper_rebalance", REBALANCE))),
        # 微信推送配置
        "push_enabled": bool(_get(S.Paper.PUSH, PUSH_ENABLED)),
        "push_method": _get(S.Paper.PUSH_METHOD, PUSH_METHOD),
        "server_chan_key": _get(S.Paper.SERVER_CHAN_KEY, PUSH_SERVER_CHAN_KEY),
        "wechat_corp_id": _get(S.Paper.WECHAT_CORP_ID, PUSH_WECHAT_CORP_ID),
        "wechat_corp_secret": _get(S.Paper.WECHAT_CORP_SECRET, PUSH_WECHAT_CORP_SECRET),
        "wechat_agent_id": _get(S.Paper.WECHAT_AGENT_ID, PUSH_WECHAT_AGENT_ID),
        "wechat_to_user": _get(S.Paper.WECHAT_TO_USER, PUSH_WECHAT_TO_USER),
        "wxpusher_app_token": _get(S.Paper.WXPUSHER_APP_TOKEN, PUSH_WXPUSHER_APP_TOKEN),
        "wxpusher_topic_ids": _get(S.Paper.WXPUSHER_TOPIC_IDS, PUSH_WXPUSHER_TOPIC_IDS),
        "wxpusher_uids": _get(S.Paper.WXPUSHER_UIDS, PUSH_WXPUSHER_UIDS),
    }
    return _CUR


# 当前生效参数 (默认=模块常量; 由 apply_paper_params 按用户设置覆盖)。
_CUR = {
    "init_cash": INIT_CASH,
    "max_pos": MAX_POSITIONS,
    "hold_bars": HOLD_BARS,
    "stop_loss": STOP_LOSS,
    "take_profit": TAKE_PROFIT,
    "cost": COST,
    "min_conf": MIN_CONF,
    "max_drawdown": MAX_DRAWDOWN_PCT,
    "max_risk_pct": MAX_RISK_PCT,
    "max_sector_conc": MAX_SECTOR_CONCENTRATION,
    "max_single_conc": MAX_SINGLE_CONCENTRATION,
    "correlation_threshold": CORRELATION_THRESHOLD,
    "vol_adjust_enabled": VOL_ADJUST_ENABLED,
    "max_capital_usage": MAX_CAPITAL_USAGE,
    "sizing_method": PositionSizingMethod.EQUAL_WEIGHT.value,
    "enable_chinext": False,
    "enable_star": False,
    "trailing_stop": TRAILING_STOP,
    "trail_atr_mult": TRAIL_ATR_MULT,
    "trail_back_pct": TRAIL_BACK_PCT,
    "trail_activate_pct": TRAIL_ACTIVATE_PCT,
    "weak_filter": WEAK_FILTER,
    "weak_max_pos": WEAK_MAX_POS,
    "weak_index_code": WEAK_INDEX_CODE,
    "va_weight": VA_WEIGHT,
    "enable_va": ENABLE_VA,
    "rebalance": REBALANCE,
    "push_enabled": PUSH_ENABLED,
    "push_method": PUSH_METHOD,
    "server_chan_key": PUSH_SERVER_CHAN_KEY,
    "wechat_corp_id": PUSH_WECHAT_CORP_ID,
    "wechat_corp_secret": PUSH_WECHAT_CORP_SECRET,
    "wechat_agent_id": PUSH_WECHAT_AGENT_ID,
    "wechat_to_user": PUSH_WECHAT_TO_USER,
    "wxpusher_app_token": PUSH_WXPUSHER_APP_TOKEN,
    "wxpusher_topic_ids": PUSH_WXPUSHER_TOPIC_IDS,
    "wxpusher_uids": PUSH_WXPUSHER_UIDS,
}

# 强多头事件: 方向命中显著优于随机且可裸多落地 (见 docs/winrate_improve_eval.md §五)
# 采用完整强梯队 {Spring,Shakeout,UTAD,LPSY,ST,LPS,SC} (沿 config.STRONG_TIER_TYPES),
# 命中 76.8% vs 弱梯队 49.8% (差 27pt)。该强梯队为"必选"改进, 弱事件
# (AR/BC/SOS/JOC/PSY/SOW_INVALID 等) 不单独触发入场, 避免稀释组合质量。
# 注: UTAD/LPSY 命中 78.5%/78.3%, 高于旧含 SOW_INVALID 的口径, 一并纳入。
try:
    # 仅保留明确的多头事件（Spring, ST, LPS），
    # 移除 UTAD/LPSY (空头方向 event_dir=-1)、SC (中性)、Shakeout (胜率最差41.7%)
    # 预期：移除 Shakeout 可提升胜率 ~5个百分点
    LONG_EVENT_TYPES = frozenset({"Spring", "ST", "LPS"})
    # SC/SOW_INVALID 方向为中性 (event_dir==0), 但属底部反转/空头失效。
    # 其中 SC 命中 60.7% 在强梯队内, 保留; SOW_INVALID (71.1%) 非强梯队, 剔除.
except Exception:  # pragma: no cover - 防御首启缺失
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "ST", "LPS"})


def file_path() -> str:
    return PAPER_FILE


def load_state():
    """读取模拟盘状态; 文件不存在或损坏 → 全新默认账户。"""
    try:
        with open(PAPER_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("cash", float(_CUR["init_cash"]))
            st.setdefault("positions", [])   # 持仓: {symbol, name, qty, cost,
            st.setdefault("orders", [])      #       entry_ts, entry_bars, type, conf}
            st.setdefault("closed", [])      # 已平仓: {symbol, ..., buy_px, sell_px,
            st.setdefault("equity_hist", []) #        qty, ret, reason, type, close_ts}
            st.setdefault("candidates", [])  # 最新候选快照
            st.setdefault("pending", [])     # 等待下一根开盘买入的委托
            st.setdefault("conditions", [])  # 条件单: 价格触发/止盈止损/追踪止损
            st.setdefault("meta", {})
            # equity_hist 日期归一化 (兼容旧数据混用 "YYYY-MM-DD" 与 datetime 串),
            # 同日期只保留当天最后一条, 保证净值曲线/回撤按交易日对齐。
            _hist = st.get("equity_hist") or []
            if _hist:
                _pool = []
                _idx = {}
                for h in _hist:
                    d = str(h.get("ts", ""))[:10]
                    h["ts"] = d
                    if d in _idx:
                        _pool[_idx[d]] = h
                    else:
                        _idx[d] = len(_pool)
                        _pool.append(h)
                st["equity_hist"] = _pool
            return st
    except Exception:
        pass
    return _new_state()


def _new_state():
    return {
        "cash": float(_CUR["init_cash"]),
        "positions": [],
        "orders": [],
        "closed": [],
        "equity_hist": [],
        "candidates": [],
        "pending": [],
        "conditions": [],
        "advanced_orders": [],  # 高级订单
        "risk_metrics": {},     # 风险指标缓存
        "meta": {},
        "scan_count": 0,        # 今日扫描次数
        "last_scan_time": "",   # 上次扫描时间
        "next_scan_time": "",   # 下次扫描时间
        "last_scan_result": "", # 最后扫描结果
    }


def save_state(st):
    """原子写盘。"""
    try:
        os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
        tmp = PAPER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, PAPER_FILE)
        return True
    except Exception:
        return False


# ── 风控与资金管理 ──────────────────────────────────────────
def check_drawdown_limit(st) -> tuple[bool, str]:
    """检查账户回撤是否超过限制。返回 (是否通过, 信息)。"""
    hist = st.get("equity_hist") or []
    if len(hist) < 2:
        return True, ""
    eqs = [h.get("equity", _CUR["init_cash"]) for h in hist]
    peak = max(eqs)
    current = eqs[-1]
    dd = (peak - current) / peak if peak > 0 else 0
    if dd >= _CUR["max_drawdown"]:
        return False, f"账户回撤 {dd*100:.1f}% 超过限制 {_CUR['max_drawdown']*100:.1f}%"
    return True, ""


def check_risk_budget(st, symbol: str, entry_price: float, stop_price: float,
                      qty: int) -> tuple[bool, str]:
    """检查单笔风险预算是否超限。返回 (是否通过, 信息)。"""
    equity_val = st["cash"]
    for p in st["positions"]:
        df_px = p.get("last", p["buy_px"])
        equity_val += df_px * p["qty"]

    risk_per_share = abs(entry_price - stop_price)
    total_risk = risk_per_share * qty
    risk_pct = total_risk / equity_val if equity_val > 0 else 1.0

    if risk_pct > _CUR["max_risk_pct"]:
        return False, f"单笔风险 {risk_pct*100:.1f}% 超过限制 {_CUR['max_risk_pct']*100:.1f}%"
    return True, ""


def check_sector_concentration(st, symbol: str, sector: str,
                               new_mv: float, df_by_code: dict) -> tuple[bool, str]:
    """检查行业集中度 (分母=账户总权益: 现金+已有持仓+新单)。返回 (是否通过, 信息)。"""
    if not sector:
        return True, ""

    # 行业集中度 = 该行业市值 / 账户总权益; 现金计入分母,
    # 否则空仓首笔新单会自认 100% 集中而总是被拦。
    total_mv = new_mv + float(st["cash"])
    sector_mv = new_mv

    for p in st["positions"]:
        df = df_by_code.get(p["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else p.get("last", p["buy_px"])
        mv = px * p["qty"]
        total_mv += mv
        if p.get("sector") == sector:
            sector_mv += mv

    if total_mv > 0:
        conc = sector_mv / total_mv
        if conc > _CUR["max_sector_conc"]:
            return False, f"行业 {sector} 集中度 {conc*100:.1f}% 超过限制 {_CUR['max_sector_conc']*100:.1f}%"
    return True, ""


def check_single_concentration(st, symbol: str, new_mv: float,
                               df_by_code: dict) -> tuple[bool, str]:
    """检查单股集中度 (分母=账户总权益: 现金+已有持仓+新单)。返回 (是否通过, 信息)。"""
    total_mv = new_mv + float(st["cash"])
    for p in st["positions"]:
        df = df_by_code.get(p["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else p.get("last", p["buy_px"])
        total_mv += px * p["qty"]

    if total_mv > 0:
        conc = new_mv / total_mv
        if conc > _CUR["max_single_conc"]:
            return False, f"单股 {symbol} 集中度 {conc*100:.1f}% 超过限制 {_CUR['max_single_conc']*100:.1f}%"
    return True, ""


def check_capital_usage(st, required_cash: float) -> tuple[bool, str]:
    """检查资金利用率。返回 (是否通过, 信息)。"""
    equity_val = st["cash"]
    for p in st["positions"]:
        equity_val += p.get("last", p["buy_px"]) * p["qty"]

    usage = 1.0 - (st["cash"] - required_cash) / equity_val if equity_val > 0 else 1.0
    if usage > _CUR["max_capital_usage"]:
        return False, f"资金利用率 {usage*100:.1f}% 超过限制 {_CUR['max_capital_usage']*100:.1f}%"
    return True, ""


def _risk_blocks_entry(st, cand, price) -> bool:
    """run_cycle 入场前的风控门禁聚合: 任一不满足则拦截该笔 (返回 True=拦截)。

    复用已定义的风控函数 (此前 5 个均为死代码, 现接入入场路径):
      回撤上限 / 单笔风险预算 / 行业集中度 / 单股集中度 / 资金利用率。
    按候选现价预估仓位; 数据不足以精确判定时 (同持有 sector 缺失) fail-soft 放行,
    仅在可判定且超限时拦截。
    """
    from wyckoff.strategies.constants import STRATEGY_VALUE_ACC
    ok, msg = check_drawdown_limit(st)
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
        try:
            paper_log.log_risk_block(cand["code"], cand.get("name", ""),
                                      msg or "回撤超限", risk_type="drawdown")
        except Exception:
            pass
        return True
    # 预估 qty (与 _make_order 同口径: 按账户总权益等权, 而非剩余现金)
    mv = sum(float(p.get("last", p["buy_px"])) * p["qty"]
             for p in st.get("positions", []))
    # 价值吸筹单仓资金权重: 降低弱策略敞口
    va_weight = float(_CUR.get("va_weight", 0.6) or 1.0)
    strategy = cand.get("strategy", "")
    budget = (float(st["cash"]) + mv) * (1.0 / max(1, _CUR["max_pos"]))
    if strategy == STRATEGY_VALUE_ACC:
        budget *= va_weight
    entry = float(price)
    qty = int(budget // (entry * (1 + SLIP_BUY)) // 100 * 100)
    if qty <= 0:
        return False
    # 止损位: 左侧买点自带御设止损 (直接以真止损算单笔风险); 其余按默认 -stop_loss
    stop = float(cand.get("stop_price") or entry * (1 - _CUR["stop_loss"]))
    ok, msg = check_risk_budget(st, cand["code"], entry, stop, qty)
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
        try:
            paper_log.log_risk_block(cand["code"], cand.get("name", ""),
                                     msg or "风险预算超限", risk_type="risk_budget")
        except Exception:
            pass
        return True
    sector = cand.get("sector", "")
    if sector:
        new_mv = entry * qty
        ok, msg = check_sector_concentration(st, cand["code"], sector, new_mv, {})
        if not ok:
            st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
            try:
                paper_log.log_risk_block(cand["code"], cand.get("name", ""),
                                         msg or "行业集中度超限", risk_type="sector")
            except Exception:
                pass
            return True
        ok, msg = check_single_concentration(st, cand["code"], new_mv, {})
        if not ok:
            st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
            try:
                paper_log.log_risk_block(cand["code"], cand.get("name", ""),
                                         msg or "单股集中度超限", risk_type="single")
            except Exception:
                pass
            return True
    ok, msg = check_capital_usage(st, entry * qty + entry * qty * _CUR["cost"])
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
        try:
            paper_log.log_risk_block(cand["code"], cand.get("name", ""),
                                     msg or "资金利用率超限", risk_type="capital")
        except Exception:
            pass
        return True
    return False


def calculate_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """计算 Kelly 分数。"""
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    b = avg_win / abs(avg_loss)  # 盈亏比
    p = win_rate
    q = 1 - p
    kelly = (b * p - q) / b if b > 0 else 0.0
    return max(0.0, min(kelly, 0.25))  # 限制在 25% 以内


def calculate_position_size(st, symbol: str, entry_price: float, stop_price: float,
                            conf: int, df: any, method: str = None) -> int:
    """计算仓位大小。

    支持多种资金管理方法:
    - equal_weight: 等权分配
    - kelly: Kelly 公式 (基于历史胜率/盈亏比)
    - vol_adjusted: 波动率调整 (ATR 百分位)
    - risk_parity: 风险平价 (目标风险预算相等)
    - fixed_fractional: 固定分数 (固定风险百分比)
    - conf_weighted: 置信度加权
    """
    if method is None:
        method = _CUR.get("sizing_method", PositionSizingMethod.EQUAL_WEIGHT.value)

    equity_val = st["cash"]
    for p in st["positions"]:
        equity_val += p.get("last", p["buy_px"]) * p["qty"]

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return 0

    max_pos = _CUR["max_pos"]
    base_budget = equity_val / max_pos

    if method == PositionSizingMethod.KELLY.value:
        # 基于历史统计计算 Kelly
        s = stats(st)
        if s["win_rate"] and s["pl_ratio"]:
            kelly = calculate_kelly_fraction(s["win_rate"], s["pl_ratio"], 1.0)
            budget = equity_val * kelly
        else:
            budget = base_budget
    elif method == PositionSizingMethod.VOLATILITY_ADJUSTED.value:
        # 基于 ATR 调整仓位
        try:
            atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else 0
            atr_pct = atr / entry_price if entry_price > 0 else 0
            # ATR 越大，仓位越小
            vol_mult = max(0.5, min(1.5, 1.0 / (atr_pct * 100) if atr_pct > 0 else 1.0))
            budget = base_budget * vol_mult
        except Exception:
            budget = base_budget
    elif method == PositionSizingMethod.RISK_PARITY.value:
        # 目标每笔风险相等
        target_risk = equity_val * _CUR["max_risk_pct"]
        budget = target_risk / (risk_per_share / entry_price) if risk_per_share > 0 else base_budget
    elif method == PositionSizingMethod.FIXED_FRACTIONAL.value:
        budget = equity_val * _CUR["max_risk_pct"] / (risk_per_share / entry_price) if risk_per_share > 0 else base_budget
    elif method == PositionSizingMethod.CONF_WEIGHTED.value:
        # 置信度加权: conf 90~100 映射到 0.8~1.2 倍
        conf_mult = 0.8 + (conf - 90) / 10 * 0.4
        budget = base_budget * conf_mult
    else:
        budget = base_budget

    budget = min(budget, equity_val * _CUR["max_capital_usage"])
    budget = max(budget, MIN_LOT)

    qty = int(budget // (entry_price * (1 + SLIP_BUY)) // 100 * 100)
    return max(0, qty)


def calculate_var(returns: list[float], confidence: float = 0.95) -> float:
    """计算历史模拟 VaR。"""
    if not returns or len(returns) < 2:
        return 0.0
    sorted_rets = sorted(returns)
    idx = int(len(sorted_rets) * (1 - confidence))
    return abs(sorted_rets[idx]) if idx < len(sorted_rets) else 0.0


def calculate_position_risk(st, symbol: str, df: any, benchmark_df: any = None) -> PositionRisk:
    """计算单只持仓的风险指标。"""
    pos = _find_pos(st, symbol)
    if pos is None:
        return PositionRisk(symbol, 0, 0, 0)

    last_px = pos.get("last", pos["buy_px"])
    mv = last_px * pos["qty"]
    entry_px = pos["buy_px"]
    unrealized = (last_px - entry_px) * pos["qty"]
    unrealized_pct = (last_px / entry_px - 1) if entry_px > 0 else 0

    # 计算收益率序列用于 VaR
    rets = []
    if df is not None and len(df) > 20:
        close = df["close"]
        rets = (close.pct_change().dropna()).tolist()

    var_95 = calculate_var(rets, 0.95) * mv if rets else 0
    var_99 = calculate_var(rets, 0.99) * mv if rets else 0

    # Beta 计算 (相对大盘)
    beta = 1.0
    if benchmark_df is not None and len(benchmark_df) == len(df) and rets:
        try:
            bench_rets = benchmark_df["close"].pct_change().dropna().tolist()
            if len(bench_rets) == len(rets) and len(rets) > 10:
                cov = np.cov(rets, bench_rets)[0, 1] if np is not None else 0
                bench_var = np.var(bench_rets) if np is not None else 1
                beta = cov / bench_var if bench_var > 0 else 1.0
        except Exception:
            pass

    # 流动性风险 (简化: 基于换手率/市值)
    liquidity_risk = 0.0
    try:
        if df is not None and "volume" in df.columns and "amount" in df.columns:
            avg_vol = df["volume"].iloc[-20:].mean()
            avg_amt = df["amount"].iloc[-20:].mean() if "amount" in df.columns else avg_vol * last_px
            if avg_amt > 0:
                # 日均成交额越小，流动性风险越高
                liquidity_risk = min(1.0, 1e8 / avg_amt)  # 1亿为基准
    except Exception:
        pass

    return PositionRisk(
        symbol=symbol,
        market_value=mv,
        unrealized_pnl=unrealized,
        unrealized_pnl_pct=unrealized_pct,
        var_95=var_95,
        var_99=var_99,
        beta=beta,
        liquidity_risk=liquidity_risk,
    )


def update_portfolio_risk(st, df_by_code: dict, benchmark_df: any = None) -> dict:
    """更新组合风险指标。"""
    risks = {}
    sector_mv = {}
    total_mv = 0

    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        risk = calculate_position_risk(st, pos["symbol"], df, benchmark_df)
        risks[pos["symbol"]] = risk
        total_mv += risk.market_value
        sector = pos.get("sector", "未知")
        sector_mv[sector] = sector_mv.get(sector, 0) + risk.market_value

    # 计算集中度风险
    for symbol, risk in risks.items():
        if total_mv > 0:
            risk.concentration_risk = risk.market_value / total_mv
        sector = st["positions"][0].get("sector", "未知") if st["positions"] else "未知"
        for p in st["positions"]:
            if p["symbol"] == symbol:
                sector = p.get("sector", "未知")
                break
        if total_mv > 0:
            risk.sector_exposure = sector_mv.get(sector, 0) / total_mv

    st["risk_metrics"] = {s: r.to_dict() for s, r in risks.items()}
    return risks


# ── 高级订单管理 ────────────────────────────────────────────
def create_oco_order(st, symbol: str, name: str, qty: int,
                     take_profit_price: float, stop_loss_price: float,
                     side: OrderSide = OrderSide.SELL) -> tuple[str, str]:
    """创建 OCO 订单 (一单成交，一单撤销)。

    返回: (take_profit_order_id, stop_loss_order_id)
    """
    parent_id = f"oco-{int(time.time() * 1_000_000)}"

    tp_order = AdvancedOrder(
        order_id=f"{parent_id}-tp",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=side,
        qty=qty,
        price=take_profit_price,
        parent_id=parent_id,
        tags={"oco_role": "take_profit"},
    )

    sl_order = AdvancedOrder(
        order_id=f"{parent_id}-sl",
        symbol=symbol,
        name=name,
        order_type=OrderType.STOP,
        side=side,
        qty=qty,
        stop_price=stop_loss_price,
        parent_id=parent_id,
        tags={"oco_role": "stop_loss"},
    )

    tp_order.child_ids = [sl_order.order_id]
    sl_order.child_ids = [tp_order.order_id]

    st.setdefault("advanced_orders", []).extend([tp_order.to_dict(), sl_order.to_dict()])
    save_state(st)

    return tp_order.order_id, sl_order.order_id


def create_bracket_order(st, symbol: str, name: str, qty: int,
                         entry_price: float, take_profit_price: float,
                         stop_loss_price: float,
                         side: OrderSide = OrderSide.BUY) -> dict:
    """创建括号单 (入场单 + 止盈单 + 止损单)。

    返回: 入场单、止盈单、止损单的 ID 字典
    """
    parent_id = f"bracket-{int(time.time() * 1_000_000)}"

    entry_order = AdvancedOrder(
        order_id=f"{parent_id}-entry",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=side,
        qty=qty,
        price=entry_price,
        parent_id=parent_id,
        tags={"bracket_role": "entry"},
    )

    exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

    tp_order = AdvancedOrder(
        order_id=f"{parent_id}-tp",
        symbol=symbol,
        name=name,
        order_type=OrderType.LIMIT,
        side=exit_side,
        qty=qty,
        price=take_profit_price,
        parent_id=parent_id,
        tags={"bracket_role": "take_profit", "depends_on": entry_order.order_id},
    )

    sl_order = AdvancedOrder(
        order_id=f"{parent_id}-sl",
        symbol=symbol,
        name=name,
        order_type=OrderType.STOP,
        side=exit_side,
        qty=qty,
        stop_price=stop_loss_price,
        parent_id=parent_id,
        tags={"bracket_role": "stop_loss", "depends_on": entry_order.order_id},
    )

    entry_order.child_ids = [tp_order.order_id, sl_order.order_id]
    tp_order.child_ids = [sl_order.order_id]
    sl_order.child_ids = [tp_order.order_id]

    orders = [entry_order.to_dict(), tp_order.to_dict(), sl_order.to_dict()]
    st.setdefault("advanced_orders", []).extend(orders)
    save_state(st)

    return {
        "entry": entry_order.order_id,
        "take_profit": tp_order.order_id,
        "stop_loss": sl_order.order_id,
    }


def create_scale_in_order(st, symbol: str, name: str, total_qty: int,
                          entry_prices: list[float],
                          side: OrderSide = OrderSide.BUY) -> list[str]:
    """创建分批建仓订单。

    entry_prices: 每批的限价列表
    """
    n_batches = len(entry_prices)
    if n_batches == 0:
        return []

    base_qty = total_qty // n_batches
    remainder = total_qty % n_batches
    order_ids = []
    parent_id = f"scalein-{int(time.time() * 1_000_000)}"

    for i, price in enumerate(entry_prices):
        qty = base_qty + (1 if i < remainder else 0)
        if qty <= 0:
            continue
        order = AdvancedOrder(
            order_id=f"{parent_id}-{i}",
            symbol=symbol,
            name=name,
            order_type=OrderType.LIMIT,
            side=side,
            qty=qty,
            price=price,
            parent_id=parent_id,
            tags={"scale_role": "entry", "batch": i},
        )
        order_ids.append(order.order_id)
        st.setdefault("advanced_orders", []).append(order.to_dict())

    save_state(st)
    return order_ids


def create_scale_out_order(st, symbol: str, name: str, total_qty: int,
                           exit_prices: list[float],
                           side: OrderSide = OrderSide.SELL) -> list[str]:
    """创建分批平仓订单。"""
    n_batches = len(exit_prices)
    if n_batches == 0:
        return []

    base_qty = total_qty // n_batches
    remainder = total_qty % n_batches
    order_ids = []
    parent_id = f"scaleout-{int(time.time() * 1_000_000)}"

    for i, price in enumerate(exit_prices):
        qty = base_qty + (1 if i < remainder else 0)
        if qty <= 0:
            continue
        order = AdvancedOrder(
            order_id=f"{parent_id}-{i}",
            symbol=symbol,
            name=name,
            order_type=OrderType.LIMIT,
            side=side,
            qty=qty,
            price=price,
            parent_id=parent_id,
            tags={"scale_role": "exit", "batch": i},
        )
        order_ids.append(order.order_id)
        st.setdefault("advanced_orders", []).append(order.to_dict())

    save_state(st)
    return order_ids


def create_trailing_stop_order(st, symbol: str, name: str, qty: int,
                               trail_pct: float, activation_price: float = None,
                               side: OrderSide = OrderSide.SELL) -> str:
    """创建追踪止损订单。"""
    order = AdvancedOrder(
        order_id=f"trail-{int(time.time() * 1_000_000)}",
        symbol=symbol,
        name=name,
        order_type=OrderType.TRAILING,
        side=side,
        qty=qty,
        trail_pct=trail_pct,
        trail_price=activation_price,
        tags={"trail_activated": activation_price is not None},
    )

    st.setdefault("advanced_orders", []).append(order.to_dict())
    save_state(st)
    return order.order_id


def check_advanced_orders(st, df_by_code: dict) -> int:
    """检查并执行高级订单。返回成交数量。"""
    filled = 0
    orders = st.get("advanced_orders", [])

    for order_dict in list(orders):
        if order_dict.get("status") != "pending":
            continue

        order = AdvancedOrder.from_dict(order_dict)
        df = df_by_code.get(order.symbol)
        if df is None or len(df) == 0:
            continue

        high = float(df["high"].iloc[-1])
        low = float(df["low"].iloc[-1])

        executed = False
        fill_price = None

        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and low <= order.price:
                executed = True
                fill_price = min(order.price, float(df["open"].iloc[-1]))
            elif order.side == OrderSide.SELL and high >= order.price:
                executed = True
                fill_price = max(order.price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.STOP:
            if order.side == OrderSide.BUY and high >= order.stop_price:
                executed = True
                fill_price = max(order.stop_price, float(df["open"].iloc[-1]))
            elif order.side == OrderSide.SELL and low <= order.stop_price:
                executed = True
                fill_price = min(order.stop_price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.STOP_LIMIT:
            # 止损限价: 触发止损价后按限价成交
            triggered = False
            if order.side == OrderSide.BUY and high >= order.stop_price:
                triggered = True
            elif order.side == OrderSide.SELL and low <= order.stop_price:
                triggered = True

            if triggered and order.limit_price is not None:
                if order.side == OrderSide.BUY and low <= order.limit_price:
                    executed = True
                    fill_price = min(order.limit_price, float(df["open"].iloc[-1]))
                elif order.side == OrderSide.SELL and high >= order.limit_price:
                    executed = True
                    fill_price = max(order.limit_price, float(df["open"].iloc[-1]))

        elif order.order_type == OrderType.TRAILING:
            # 追踪止损: 价格创新高后回撤 trail_pct 触发
            if order.trail_price is None:
                # 未激活: 价格突破激活价时激活
                if order.side == OrderSide.SELL and high >= (order.trail_price or 0):
                    order.trail_price = high
                    order_dict["trail_price"] = high
                    order_dict["tags"]["trail_activated"] = True
            else:
                # 已激活: 更新追踪价
                if order.side == OrderSide.SELL:
                    if high > order.trail_price:
                        order.trail_price = high
                        order_dict["trail_price"] = high
                    stop_trigger = order.trail_price * (1 - order.trail_pct)
                    if low <= stop_trigger:
                        executed = True
                        fill_price = stop_trigger
                else:  # BUY trailing (较少见)
                    if low < order.trail_price:
                        order.trail_price = low
                        order_dict["trail_price"] = low
                    stop_trigger = order.trail_price * (1 + order.trail_pct)
                    if high >= stop_trigger:
                        executed = True
                        fill_price = stop_trigger

        if executed and fill_price:
            # 执行成交
            fill_price = round(fill_price * (1 + SLIP_BUY if order.side == OrderSide.BUY else 1 - SLIP_SELL), 3)
            order.filled_qty = order.qty
            order.avg_fill_price = fill_price
            order.status = OrderStatus.FILLED
            order.updated_ts = time.strftime("%Y-%m-%d %H:%M:%S")

            # 更新状态
            order_dict.update(order.to_dict())

            # 处理 OCO/括号单的联动撤销
            if order.parent_id:
                _cancel_sibling_orders(st, order)

            filled += 1

    if filled > 0:
        save_state(st)
    return filled


def _cancel_sibling_orders(st, filled_order: AdvancedOrder):
    """成交时撤销同组的其他订单 (OCO/括号单)。"""
    orders = st.get("advanced_orders", [])
    for o in orders:
        if o.get("parent_id") == filled_order.parent_id and o.get("order_id") != filled_order.order_id:
            if o.get("status") == "pending":
                o["status"] = "cancelled"
                o["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")


def cancel_advanced_order(st, order_id: str) -> bool:
    """撤销高级订单。"""
    orders = st.get("advanced_orders", [])
    for o in orders:
        if o.get("order_id") == order_id and o.get("status") == "pending":
            o["status"] = "cancelled"
            o["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # 同时撤销子订单
            for child_id in o.get("child_ids", []):
                for oc in orders:
                    if oc.get("order_id") == child_id and oc.get("status") == "pending":
                        oc["status"] = "cancelled"
                        oc["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_state(st)
            return True
    return False


# ── 选股: 全市场自动筛选并自动生成条件单 ─────────────────────
# ── 三重共振纪律硬门禁 (统一数据源见 discipline.py) ──
# paper 是实际撮合的交关口; 门禁阈值与实现收敛到 discipline.py 单一源。
# 此处保留模块级名字 (供本模块内部与测试 monkeypatch 引用), 语义完全一致。
from .discipline import (  # noqa: E402
    flow_net5 as _flow_net5,
)
from .discipline import (
    market_trend_ok as _market_trend_ok,
)
from .discipline import (
    sector_strength_ok as _sector_strength_ok,
)


# ── 弱市过滤 (改进: 指数未站上MA20 → 降仓 + 停用价值吸筹) ──
def _weak_market_flag():
    """按 _CUR 弱市过滤设置评估当前市场强弱, 返回是否弱市。
    弱市过滤关闭或数据异常 → 视为不强 (不受限)。调用方负责把结果落 st["weak"]。"""
    if not _CUR.get("weak_filter"):
        return False
    try:
        ok, _reason = _market_trend_ok()
        return not ok
    except Exception:
        return False


def _strategy_manager():
    """策略管理器单例 (延迟实例化, 数据目录落在 DATA_DIR 下避免污染运行目录)。

    供模拟盘候选生成复用: 策略4 (模拟盘纪律, conf≥90) 与 综合选股·价值吸筹
    (底部整固 + 20根内吸筹事件)。导入失败或数据不可用时返回 None (纯离线降级)。
    """
    global _SMGR
    if _SMGR is None:
        try:
            from wyckoff_strategies_manager import WyckoffStrategyManager

            from .paths import DATA_DIR as _DD
            _SMGR = WyckoffStrategyManager(
                data_dir=os.path.join(_DD, "strategy_manager_data"))
        except Exception:
            _SMGR = False
    return _SMGR if _SMGR else None


def _value_accum_candidate(code, df, piv, evs):
    """(兼容垫片) 基于策略管理器「价值吸筹」信号构造候选。

    正式扫描统一走 manager.scan_individual; 此处保留旧逻辑供单测/外部引用。
    命中条件 (与回测口径一致): 阶段=底部整固 且 近20根内出现
    {Spring,Shakeout,SC,ST,LPS} 事件, conf≥VA_MIN_CONF 并通过低质过滤。
    管理器不可用 / 评估异常时返回 None。
    """
    m = _strategy_manager()
    if m is None:
        return None
    try:
        sig = m.evaluate_strategy_value_accumulation(df, len(df) - 1, evs, piv)
    except Exception:
        return None
    if not sig:
        return None
    ev = sig["event"]
    if int(ev.get("conf", 0) or 0) < VA_MIN_CONF:
        return None
    if _is_low_quality(sig["event"].get("code") or code):
        return None
    return {"strategy": STRATEGY_VALUE_ACC,
            "type": ev["type"], "idx": int(ev.get("idx") or 0),
            "conf": int(ev.get("conf", 0) or 0)}


# 兼容别名 (策略常量已随选股逻辑迁移入策略管理器; 保留引用供单测/外部导入)
VA_EXCLUDE_BJ = True
VA_EXCLUDE_ST = True
VA_MIN_PRICE = 3.0
VA_MIN_CONF = 80
LONG_STRATEGY = STRATEGY_LONG_LEFT
LONG_MIN_CONF = 60
LB_ENTRY_MARGIN = 0.0
LB_MAX = 8
try:
    from .strategies.manager import (  # noqa: E402 仅取值
        LONG_MIN_CONF as _M_LONG_MIN_CONF,
    )
    from .strategies.manager import (
        VA_MIN_CONF as _M_VA_MIN_CONF,
    )
    from .strategies.manager import (
        VA_MIN_PRICE as _M_VA_MIN_PRICE,
    )
    LONG_MIN_CONF, VA_MIN_PRICE, VA_MIN_CONF = (
        _M_LONG_MIN_CONF, _M_VA_MIN_PRICE, _M_VA_MIN_CONF)
except Exception:
    pass


def _long_buy_candidate(code, df, piv, evs):
    """(兼容垫片) 委托策略管理器·左侧买点候选生成。

    等价于 manager._left_buy_candidate, 保留仅供既有脚本/回测引用。
    """
    m = _strategy_manager()
    if m is None:
        return None
    try:
        res = m._left_buy_candidate(code, df, evs, piv, name=_stock_name(code))
    except Exception:
        return None
    return dict(res) if res else None


def _is_low_quality(code, price=None, name=None) -> bool:
    """(兼容垫片) 低质池过滤 (北交所/ST·退市/低价), 优先委托策略管理器。"""
    if name is None:
        try:
            name = _stock_name(code)
        except Exception:
            name = ""
    m = _strategy_manager()
    if m is not None and hasattr(m, "_is_low_quality"):
        try:
            return m._is_low_quality(code, price=price, name=name)
        except Exception:
            pass
    c = str(code).lower()
    if VA_EXCLUDE_BJ and c.startswith("bj"):
        return True
    if VA_EXCLUDE_ST:
        nm = name or ""
        if any(x in nm for x in ("ST", "退", "N ", "C ")):
            return True
    if price is not None and VA_MIN_PRICE > 0 and float(price) < VA_MIN_PRICE:
        return True
    return False


def _probe_workers():
    """自动选择扫描并行度: 利用机内逻辑核, 上限 12 (网络 I/O 也受连接池/带宽约束)。

    numpy/pandas 指标计算在执行时会释放 GIL, 多线程在 8 核 (兆芯/Intel/AMD)
    上可真实并行; 网络等待更是天然并发。旧实现死锁在 6 是低估了整机算力。
    """
    try:
        import os
        n = int(os.cpu_count() or 4)
    except Exception:
        n = 4
    return max(2, min(n, 12))


def _mainboard_universe(max_codes=6000):
    """解析全市场 universe (本地全A名单, 缺失降级东财Top-100) 并收敛到沪深主板。

    与 pick_candidates 的门槛逻辑同口径 (沪 600/601/603/605 + 深 000/001/002/003),
    供 run_scan 统计"实际扫描代码数"而非全程 6000。
    """
    from .utils import normalize_symbol
    try:
        from .fundamental import local_universe
        universe = local_universe(max_codes)
    except Exception:
        universe = []
    if not universe:
        try:
            from .fundamental import universe as market_universe
            universe = market_universe(100)[0] or []
        except Exception:
            universe = []
    universe = [normalize_symbol(c) for c in universe]
    from .fundamental import is_main_board
    return [c for c in universe if is_main_board(c)]


def pick_candidates(universe=None, max_codes=6000, min_conf=None,
                    cancel_event=None, progress=None, skip_gates=False,
                    strategies=None):
    """扫描 universe 中触发强多头事件的高 conf 标的, 返回候选单 (降序 conf)。

    选股统一由策略管理器 (WyckoffStrategyManager.scan_individual) 产出,
    本函数只负责编排: 拉K线 → 管理器选股 → 门禁 → 低质过滤 → 排序。
    单只股票优先级: 纪律 (受门禁) → 威科夫左侧买点 (独立赛道, 不受门禁)
                    → 价值吸筹 (受门禁, 纪律兜底)。

    strategies: 可选策略 key 子集 (如仅左侧买点); None/空=三策略并线。
    单策略扫描时直接把子集传给 scan_individual, 避免 STRATEGY_ORDER 优序
    掩盖低优先级策略候选。

    纪律硬门禁 (缺一不可, 数据不可用即拦截; skip_gates=True 跳过全部门禁,
    供离线/测试/仅排序场景使用):
      - 大盘20日线向上 (fetch_market_env)
      - 板块强度 > 60 分位 (sector_strength_pct)
      - 资金流近5日主力净流入 > 候选池截面中位 (跨市场截面分位, fetch_main_flow)
    左侧买点不经过上述门禁 (左侧买点诞生于大盘弱市, 属独立赛道)。

    遇到候选时自动添加价格买入条件单 (buy_price):
      - 纪律/价值吸筹: 触发价=最近收盘价×1.002, 条件 "≥ 达上破" (above)。
      - 左侧买点: 触发价=买点入场价, 条件 "回踩买入" (below)。
    """
    if min_conf is None:
        min_conf = _CUR["min_conf"]
    # 价值吸筹停用时: 统一在选股源头剔除该策略 (不产生候选/条件单)。
    if not _CUR.get("enable_va", True):
        if strategies is None:
            strategies = (STRATEGY_DISCIPLINE, STRATEGY_LONG_LEFT)
        else:
            strategies = tuple(s for s in strategies if s != STRATEGY_VALUE_ACC)
        if not strategies:
            # 显式仅指定价值吸筹 (如 UI 模式扫描) → 直接返回空, 避免回退全策略
            return []
    from .datasource import fetch_kline
    from .fundamental import fetch_sector
    from .indicators import add_indicators
    from .utils import normalize_symbol

    if universe is None:
        # 全A 市场扫描: 本地全A 名单 (去 ST/退市/新股, ~5900 只) 逐股扫描;
        # 名单缺失时降级东财成交额 Top-100 / 本地抽样兜底。
        universe = _mainboard_universe(max_codes)
    else:
        # 调用方显式传入: 同样归一化 + 主板收敛 (非主板代码忽略)
        universe = [normalize_symbol(c) for c in universe]
        from .fundamental import is_main_board
        universe = [c for c in universe if is_main_board(c)]
    # 纪律门禁 ①: 大盘20日线向上 (全市场统一, 一次判定; fail-close)。
    # 不再整池短路: 左侧买点属独立赛道, 大盘弱市也可出候选; 纪律/价值吸筹
    # 在 scan_individual 按 market_ok 拦截。
    market_ok = True
    _market_reason = ""
    if not skip_gates:
        market_ok, _market_reason = _market_trend_ok()

    def _probe(code):
        """单只股票的重活: K线 → 策略管理器选股 → 门禁/低质/触发价。
        Gate ③ (资金流截面分位) 是跨市场判定, 故 flow 先随候选带回, 由调用方统一过滤。
        返回 (code, candidate_or_None, flow_or_None)。"""
        if cancel_event is not None and cancel_event.is_set():
            return code, None, None
        flow = None
        try:
            df = add_indicators(fetch_kline(code, datalen=400, scale=240),
                                symbol=code)
            if df is None or len(df) < 200:
                return code, None, None
            name = _stock_name(code)
            m = _strategy_manager()
            if m is None:
                return code, None, None
            cand = m.scan_individual(
                code, df, min_conf=min_conf,
                gates_ok=(market_ok, _market_reason), name=name,
                event_types=LONG_EVENT_TYPES, strategies=strategies)
            if cand is None:
                return code, None, None
            cand["code"] = code
            cand["name"] = name
            cand["last"] = round(float(df["close"].iloc[-1]), 2)
            cand["day"] = str(df["day"].iloc[-1])
            # 低质池过滤 (北交所/ST/低价) — 各策略统一适用, 防垃圾入池
            if _is_low_quality(code, price=cand["last"], name=name):
                return code, None, None
            cand["sector"] = ""
            try:
                cand["sector"] = fetch_sector(code) or ""
            except Exception:
                pass
            if cand.get("gated"):
                # 纪律/价值吸筹 (受门禁): 板块强度 + 资金流 + 产业链 conf 调整
                if not skip_gates:
                    s_ok, _s_reason = _sector_strength_ok(cand["sector"])
                    if not s_ok:
                        return code, None, None
                    # 纪律门禁 ③: 收集主力净流入供截面分位判定 (稍后统一过滤)
                    flow = _flow_net5(code)
                else:
                    flow = None
                # A: 产业链加分 — 不改变入池过滤, 只影响排序优先级;
                #    数据不可用/无板块 (离线) 时 adj=0 完全退化为原行为。
                base_conf = int(cand.get("conf", 0) or 0)
                try:
                    if cand["sector"]:
                        from .chain import chain_conf_adjust
                        adj = chain_conf_adjust(cand["sector"], base_conf)
                        cand["base_conf"] = base_conf
                        cand["conf"] = max(min_conf, min(100, base_conf + adj))
                        cand["chain_adj"] = adj
                except Exception:
                    pass
                # 自动买入条件单: 触发价=现价+0.2% 上破 (above)。
                cand["auto_cond_price"] = round(float(cand["last"]) * 1.002, 3)
                cand["trigger"] = "above"
            else:
                # 独立赛道左侧买点: 直接挂买点入场价, 回踩触发 (below)。
                cand["auto_cond_price"] = round(float(cand["entry_price"]), 3)
                cand["trigger"] = "below"
            # 价值吸筹已停用: 候选层面兜底剔除 (管理器可能绕过 strategies 过滤,
            # 兜底保证 任何路径都不产出 VA 候选/条件单)。
            if not _CUR.get("enable_va", True) \
                    and cand.get("strategy") == STRATEGY_VALUE_ACC:
                return code, None, None
            return code, cand, flow
        except Exception:
            return code, None, None

    out = []
    _flow_map = {}  # code -> 近5日主力净流入
    # 主板块收敛已在上方统一完成 (沪 600/601/603/605 + 深 000/001/002/003),
    # 创业板/科创板/北交所等不再进入并行扫描 (_codes 无需再按权限过滤)。
    _codes = universe[:max_codes]
    _total = len(_codes)
    try:
        from ._shared import parallel_map
        for code, cand, flow in parallel_map(_codes, _probe, workers=_probe_workers(),
                                             progress=progress):
            if cand is not None:
                out.append(cand)
                _flow_map[code] = flow
    except Exception:
        # 串行兜底: 同样上报进度, 保证进度条口径一致
        for i, code in enumerate(_codes):
            if progress is not None:
                try:
                    progress(i + 1, _total, code)
                except Exception:
                    pass
            _code, cand, flow = _probe(code)
            if cand is not None:
                out.append(cand)
                _flow_map[_code] = flow
    # 纪律门禁 ③: 资金流"净流入>50 分位" —— 跨市场截面判定。
    # 只对拿到净流入数据的候选计算中位; 有数据但低于中位 → 拦截。
    # 若全部候选都拿不到资金流数据 (接口被拒/离线/新股无数据) → 数据缺失不代表
    # "资金弱", 降级跳过该门禁 (保留 ①大盘 ②板块 fail-close);
    # 部分有部分无数据时, 无数据者仍按不达标拦截。
    # 独立赛道左侧买点 (gated=False) 豁免: 不受资金流门禁管束。
    if not skip_gates:
        flows = [f for f in _flow_map.values() if f is not None]
        if flows:
            med = sorted(flows)[len(flows) // 2]
            out = [e for e in out
                   if not e.get("gated")
                   or (_flow_map.get(e["code"]) is not None
                       and _flow_map[e["code"]] >= med)]
        else:
            # 完全无资金流数据 → 门禁降级跳过 (见注释)
            pass
    out.sort(key=lambda e: -(int(e.get("conf", 0) or 0)))
    return out


def _stock_name(code):
    try:
        from .screener import _get_stock_name
        return _get_stock_name(str(code)[-6:])
    except Exception:
        return ""


# ── 撮合: 买入/卖出 ─────────────────────────────────────
def _next_open(code, datalen=420):
    """取最新一根日线 (模拟盘以收盘后运行, 下一根"开盘价"用最近收盘近似+滑点)。"""
    from .datasource import fetch_kline
    from .indicators import add_indicators
    df = add_indicators(fetch_kline(code, datalen=datalen, scale=240), symbol=code)
    return df


def execute_date(code):
    """最近一根 K 线的日期标识 (用于排重/进度)。"""
    try:
        df = _next_open(code)
        return str(df["day"].iloc[-1])
    except Exception:
        return time.strftime("%Y-%m-%d")


def has_position(st, code):
    return any(p["symbol"] == code for p in st["positions"])


def _make_order(code, name, type_, conf, price, n_total, cash, sector=None,
                strategy="", st=None, stop_pct=None, take_pct=None):
    """构造买单订单公共字段 (qty 按全账户总权益等权预算分配, 整手)。

    sector 为持仓行业 (用于行业集中度风控 check_sector_concentration);
    strategy 记录信号来源策略 (策略管理器: paper_discipline_bull/价值吸筹/威科夫左侧买点)。
    stop_pct/take_pct: 左侧买点自带御设 (由买点离场价折算), 落入持仓保护条件单,
    否则按账户默认止盈止损。
    等权口径: 传入 st 时按 账户总权益/max_pos 分配 (现金+已有持仓市值),
    避免"首批买入吞掉大部分现金、后续仓权重失衡" (曾出现三仓 33万/17万/14万)。
    未传 st 时回退 现金/max_pos (兼容旧调用方与测试)。
    """
    equity_base = cash
    if st is not None:
        mv = sum(float(p.get("last", p["buy_px"])) * p["qty"]
                 for p in st.get("positions", []))
        equity_base = float(st["cash"]) + mv
    budget = equity_base * (1.0 / max(1, _CUR["max_pos"]))
    # 价值吸筹降权: 单仓资金×va_weight (弱策略敞口控制, 其余策略=1.0)
    if strategy == STRATEGY_VALUE_ACC:
        budget *= float(_CUR.get("va_weight", 1.0) or 1.0)
    if budget < MIN_LOT:
        return None
    qty = int(budget // (price * (1 + SLIP_BUY)) // 100 * 100)
    if qty <= 0:
        return None
    return {
        "symbol": code, "name": name, "type": type_, "conf": int(conf),
        "qty": qty, "price": round(price * (1 + SLIP_BUY), 3), "side": "buy",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "date": "", "bars": int(n_total) if n_total else 0,
        "sector": sector or "", "strategy": strategy,
        "stop_pct": stop_pct, "take_pct": take_pct,
    }


def place_buy_order(code, name, type_, conf, price, n_total, execute=True,
                    strategy=""):
    """下单买入 (execute=True 直接按现价撮合成交; False 进 pending 待成交)。

    独立入口: 自行加载状态、校验持仓/同持上限/资金。返回 (order, msg)。
    """
    st = load_state()
    with _LOCK:
        if has_position(st, code):
            return None, "已持有"
        weak = _weak_market_flag()  # 刷新弱市标记 (UI 手动买入也走弱市限仓)
        st["weak"] = weak
        if weak and strategy == STRATEGY_VALUE_ACC:
            return None, "弱市已禁用价值吸筹"
        limit = _CUR["weak_max_pos"] if weak else _CUR["max_pos"]
        if len(st["positions"]) >= limit:
            return None, "同持已满"
        order = _make_order(code, name, type_, conf, price, n_total, st["cash"],
                            strategy=strategy, st=st)
        if order is None:
            return None, "金额不足一手"
        if execute:
            return fill_buy(st, order)
        st["pending"].append(order)
        save_state(st)
        return order, "已下单待撮合"


def _enqueue_buy(st, code, name, type_, conf, price, sector=None, strategy=""):
    """(进程内) 仅计入 pending, 不校验不落盘。调用方负责校验与 save_state。"""
    order = _make_order(code, name, type_, conf, price, 0, st["cash"],
                        sector=sector, strategy=strategy, st=st)
    if order is None:
        return None
    st["pending"].append(order)
    return order


# ── 条件单 (价格触发 / 止盈止损 / 追踪止损) ─────────────────
# kind:
#   "buy_price"   价格买入条件单: 现价 满足 trigger 与 price 时买入
#   "sell_price"  价格卖出条件单: 现价 满足 trigger 与 price 时卖出
#   "take_profit" 止盈: 浮盈 ≥ pct 时卖出
#   "stop_loss"   止损: 浮亏 ≥ pct 时卖出
#   "trailing"    追踪止损: 从持仓期内最高价回撤 pct 时卖出
# trigger: "above"(≥) / "below"(≤)  (仅 buy_price/sell_price 用)
_COND_KINDS = ("buy_price", "sell_price", "take_profit", "stop_loss", "trailing")


def _cond(kind, symbol, price=None, pct=None, trigger="above", qty=0,
          name="", reason="", amount=None, activation=None, strategy=""):
    """构造一条条件单记录 (status="active")。
    activation: 移动止盈专用 - 浮盈价, 收盘价≥该价才启用回落跟踪 (未激活不触发)。
    strategy: 信号来源策略 (scan_individual 带入, 供条件单路径补策略归属)。"""
    c = {
        "cid": f"cond-{int(time.time() * 1_000_000)}-{next(_CID_SEQ)}",
        "kind": kind, "symbol": symbol, "name": name,
        "price": round(float(price), 3) if price is not None else None,
        "pct": float(pct) if pct is not None else None,
        "trigger": trigger, "qty": int(qty or 0), "amount": amount,
        "reason": reason,
        "activation": round(float(activation), 3) if activation is not None else None,
        "activated": False,
        "strategy": strategy or "",
        "status": "active", "created_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "matched_ts": "", "matched_price": None,
        "peak": None, "correct": None,  # correct: True/False/None(未评估)
    }
    return c


def add_condition(st, kind, symbol, price=None, pct=None, trigger="above",
                  qty=0, name="", reason="", save=True):
    """添加一条条件单到状态并落盘 (save=True)。返回 (cond, msg)。"""
    if kind not in _COND_KINDS:
        return None, f"不支持的条件单类型: {kind}"
    if kind in ("buy_price", "sell_price") and price is None:
        return None, "价格条件单需指定触发价"
    if kind in ("take_profit", "stop_loss", "trailing") and pct is None:
        return None, "止盈/止损/追踪条件单需指定百分比"
    c = _cond(kind, symbol, price=price, pct=pct, trigger=trigger,
              qty=qty, name=name, reason=reason)
    st.setdefault("conditions", []).append(c)
    if save:
        save_state(st)
    return c, "已添加条件单"


def _apply_auto_conditions(st, cand, weak=False):
    """为迭代出的候选批量 upsert buy_price 条件单 (覆盖式去重, 不落盘)。

    由 run_scan/run_cycle 在最终 save_state 前调用一次, 避免扫描线程逐条
    写盘被最终快照覆盖。
    针对多轮扫描同一标的反复生成造成条件单堆积 (曾出现 42 个同代码重复) 的
    问题, 采用覆盖式去重:
      - 候选内同代码至多保留一条 (首次添加后, 后续同代码行仅刷新 name/reason)。
      - 若已存在同代码同类型 active 条件单, 则更新其触发价/名称/说明并视为新增,
        避免重复触发堆积; 同时把历史 outdated 的同代码 active 条件单转 cancelled。
    返回本次有效 upsert/新增的数量。
    """
    conds = st.setdefault("conditions", [])
    touched = 0
    held = {p["symbol"] for p in st.get("positions", [])}
    # 全局预清理: 把历史遗留的重复 active buy_price 条件合流为每代码一条
    # (曾因多轮扫描反复落盘堆积 42 个同代码重复), 先取消多余的再重建;
    # 已持标的的入场条件单一并取消 (持仓期间买入条件单无意义, 且会反复触发→取消)。
    seen = {}
    va_off = not _CUR.get("enable_va", True)
    for c in conds:
        if c.get("kind") != "buy_price" or c.get("status") != "active":
            continue
        code = c["symbol"]
        if va_off and c.get("strategy") == STRATEGY_VALUE_ACC:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "停用价值吸筹, 取消入场条件单"
            continue
        if code in held:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "已持仓, 取消入场条件单"
            continue
        if code in seen:
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["note"] = "批量去重: 重复条件单"
        else:
            seen[code] = c
    handled = set()  # 本批已处理(创建/更新)的代码, 保证候选内同代码只保留一条
    for e in cand or []:
        if weak and e.get("strategy") == STRATEGY_VALUE_ACC:
            continue  # 弱市停用价值吸筹入场条件单
        price = e.get("auto_cond_price")
        if not price:
            continue
        code = e["code"]
        if code in handled:
            continue
        if code in held:
            # 已持有: 不再为已持标的生成/刷新入场条件单 (防止条件单永续堆积与
            # 每次周期"触发→已持有→取消"的噪音; 该标的保护单由持仓防护回填管理)
            continue
        handled.add(code)
        reason = f"自动:{e.get('strategy','')}:{e.get('type')}({e.get('conf',0)})"
        # 策略级信号追踪: 廉价记录 (只落盘, 不拉行情), 供准确度/盈利聚合。
        # 同策略+同标的+同事件在冷却窗(20根)内重复扫描自动合并。
        try:
            paper_strategy_accuracy.record_signal(
                e.get("strategy", ""), code, code, e.get("name", ""),
                e.get("type", ""), e.get("conf", 0),
                time.strftime("%Y-%m-%d"), e.get("last", 0) or 0,
                fired=has_position(st, code))
        except Exception:
            pass
        # 覆盖式: 同代码同类型 active 条件单 → 更新触发价等, 而非跳过
        existing = seen.get(code)
        if existing is not None:
            existing["price"] = round(float(price), 3)
            existing["name"] = e.get("name", "")
            existing["reason"] = reason
            existing["trigger"] = e.get("trigger", "above")
            existing["strategy"] = e.get("strategy", "")
            existing["updated_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            touched += 1
            continue
        newc = _cond(
            "buy_price", code, price=float(price),
            trigger=e.get("trigger", "above"),
            name=e.get("name", ""), reason=reason,
            strategy=e.get("strategy", ""))
        conds.append(newc)
        seen[code] = newc
        touched += 1
    return touched


def place_condition(kind, symbol, price=None, pct=None, trigger="above",
                    qty=0, name="", reason=""):
    """独立入口: 加载状态并添加条件单。返回 (cond, msg)。"""
    st = load_state()
    with _LOCK:
        return add_condition(st, kind, symbol, price=price, pct=pct,
                             trigger=trigger, qty=qty, name=name,
                             reason=reason, save=True)


def _create_position_conditions(st, symbol, name="", buy_px=None):
    """建仓后为持仓自动生成止盈/止损条件单, 并消费同标的入场条件单。

    为 symbol 生成 take_profit (pct=_CUR.take_profit) + stop_loss
    (pct=_CUR.stop_loss), 使条件单 tab 展示持仓保护, 且 _check_conditions 优先
    触发; 同标的已有 active 止盈/止损时不重复生成。
    同时把同标的 active 的 buy_price 入场条件标记为 done (已买入), 防止以后被再次
    触发造成同仓重复买入。
    """
    conds = st.setdefault("conditions", [])
    for c in conds:
        if c.get("kind") == "buy_price" and c.get("symbol") == symbol \
                and c.get("status") == "active":
            c["status"] = "done"
            c["note"] = "已买入, 入场条件单消费"
    if buy_px is not None:
        # 止盈/止损百分比: 左侧买点带御设 (取该持仓自带比值), 否则账户默认。
        pos = next((p for p in st.get("positions", []) if p.get("symbol") == symbol), None)
        take_pct = (pos or {}).get("take_pct") if pos else None
        stop_pct = (pos or {}).get("stop_pct") if pos else None
        active_kinds = {c.get("kind") for c in conds
                        if c.get("symbol") == symbol and c.get("status") == "active"}
        trailing_on = bool(_CUR.get("trailing_stop")
                           and float(_CUR.get("trail_back_pct") or 0) > 0)
        if trailing_on and "trailing" not in active_kinds:
            # 移动止盈: 浮盈达激活价后才启用峰值回落保护; 激活价默认=止盈线
            # (左侧买点带御设止盈则优先用其目标作为激活线)
            act_pct = float(_CUR.get("trail_activate_pct") or 0) \
                or (take_pct or _CUR["take_profit"])
            conds.append(_cond(
                "trailing", symbol, pct=float(_CUR["trail_back_pct"]), name=name,
                reason="持仓保护:移动止盈",
                activation=buy_px * (1 + act_pct)))
        elif not trailing_on and "take_profit" not in active_kinds:
            conds.append(_cond(
                "take_profit", symbol, pct=take_pct or _CUR["take_profit"], name=name,
                reason="持仓保护:take_profit"))
        if "stop_loss" not in active_kinds:
            # 固定止损永远保留为底线保护 (未激活移动止盈时控制下行风险)
            conds.append(_cond(
                "stop_loss", symbol, pct=stop_pct or _CUR["stop_loss"], name=name,
                reason="持仓保护:stop_loss"))


def _backfill_position_protection(st):
    """对缺失止盈/止损/追踪保护条件单的持仓自动补齐 (幂等)。

    覆盖历史遗留或非 fill_buy 路径建立的仓位。_create_position_conditions
    已有"同标的 active 保护不重复"的防护, 并顺带消费同标的入场 buy_price 条件单。
    返回补齐的持仓数量。
    """
    n = 0
    for p in st.get("positions", []):
        sym = p["symbol"]
        if any(c.get("kind") in ("take_profit", "stop_loss", "trailing")
               and c.get("symbol") == sym and c.get("status") == "active"
               for c in st.get("conditions", [])):
            continue
        _create_position_conditions(st, sym, name=p.get("name", ""),
                                    buy_px=float(p.get("buy_px", 0) or 0))
        n += 1
    return n


def cancel_condition(st, cid, save=True):
    """取消一条条件单 (status → "cancelled")。返回是否命中。"""
    for c in st.get("conditions", []):
        if c.get("cid") == cid and c.get("status") == "active":
            c["status"] = "cancelled"
            c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if save:
                save_state(st)
            return True
    return False


def _match_trigger(trigger, price, cond_price):
    """价格条件比较: above 表示 ≥, below 表示 ≤。"""
    if trigger == "below":
        return price <= cond_price
    return price >= cond_price


def _check_conditions(st, df_by_code):
    """周期推进时检查条件单: 以最新收盘价判断是否触发。

    触发动作:
      - buy_price / sell_price: 现价满足 trigger 与 price 时执行买卖。
      - take_profit / stop_loss: 持仓浮盈/浮亏达到 pct 时平仓。
      - trailing: 持仓期内最高价回撤 pct 时平仓 (无持仓时触发作废)。
    返回触发的条件单数量。
    """
    triggered = 0
    for c in list(st.get("conditions", [])):
        if c.get("status") != "active":
            continue
        sym = c["symbol"]
        df = df_by_code.get(sym)
        if df is None or len(df) == 0:
            continue
        last = float(df["close"].iloc[-1])
        kind = c["kind"]

        if kind == "buy_price":
            if _match_trigger(c["trigger"], last, c["price"]):
                if has_position(st, sym):
                    # 已持有该标的则不重复买入, 直接取消入场条件单
                    c["status"] = "cancelled"
                    c["cancelled_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    c["note"] = "已持有该标的, 入场条件单取消"
                    continue
                _fire_condition(st, c, last, df, side="buy")
                triggered += 1
        elif kind == "sell_price":
            pos = _find_pos(st, sym)
            if pos is None:
                continue
            if _t1_blocked(pos, df):
                continue
            if _match_trigger(c["trigger"], last, c["price"]):
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
        elif kind in ("take_profit", "stop_loss"):
            pos = _find_pos(st, sym)
            if pos is None:
                continue  # 无持仓的止盈止损无意义, 保留等待
            if _t1_blocked(pos, df):
                continue  # A股 T+1: 当日买入禁售
            entry = float(pos["buy_px"])
            ret = last / entry - 1
            if kind == "take_profit" and ret >= c["pct"]:
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
            elif kind == "stop_loss" and ret <= -c["pct"]:
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
        elif kind == "trailing":
            pos = _find_pos(st, sym)
            if pos is None:
                # 无持仓时追踪止损无法建立峰值, 保留但跳过
                continue
            if _t1_blocked(pos, df):
                continue  # A股 T+1: 当日买入禁售
            if c.get("activation") is not None and not c.get("activated"):
                # 移动止盈: 浮盈价未达激活价不触发 (峰值回落保护未启用)
                if last < c["activation"]:
                    continue
                c["activated"] = True
                gold = last
            else:
                gold = float(pos.get("last", pos["buy_px"]))
            peak = c.get("peak") or max(gold, float(pos.get("last", pos["buy_px"])))
            if last > peak:
                peak = last
            c["peak"] = peak
            if peak and last <= peak * (1 - c["pct"]):
                _fire_condition(st, c, last, df, side="sell", pos=pos)
                triggered += 1
    return triggered


def _find_pos(st, symbol):
    for p in st["positions"]:
        if p["symbol"] == symbol:
            return p
    return None


def _t1_blocked(pos, df):
    """A股 T+1: 当日买入的持仓当日禁止卖出 (次一交易日方可平仓)。

    仅在 fill_buy 携带了 entry_day (= 买入触发日的 K 线交易日) 时生效;
    历史持仓 (entry_day 为空) 不受限, 兼容旧数据与不传 day 的调用方。
    返回 True 表示当日禁售。
    """
    entry_day = pos.get("entry_day") or ""
    if not entry_day:
        return False
    try:
        bar_day = str(df["day"].iloc[-1])
    except Exception:
        bar_day = ""
    return bool(bar_day) and bar_day == entry_day


def _judge_condition_correct(c, last, entry=None, ret=None, peak=None,
                             side="sell", cond_price=None, pct=None):
    """根据条件类型和行情判断是否正确 (True=绿钩, False=红叉, None=未评估)。

    判断规则:
      - buy_price/above: 价格 ≥ 触发价 → True
      - buy_price/below: 价格 ≤ 触发价 → True
      - sell_price/above: 价格 ≥ 触发价 → True
      - sell_price/below: 价格 ≤ 触发价 → True
      - take_profit: 实际收益 ≥ pct → True
      - stop_loss: 实际亏损 ≥ pct → True (ret <= -pct)
      - trailing: 从峰值回撤 ≥ pct → True
    """
    kind = c.get("kind", "")
    trigger = c.get("trigger", "above")
    price = c.get("price")

    if kind in ("buy_price", "sell_price"):
        if trigger == "above":
            return price is not None and last >= price
        else:  # below
            return price is not None and last <= price

    if kind == "take_profit":
        if ret is not None and pct is not None:
            return ret >= pct
        return None

    if kind == "stop_loss":
        if ret is not None and pct is not None:
            return ret <= -pct
        return None

    if kind == "trailing":
        if not c.get("activated"):
            return None
        if peak is not None and pct is not None:
            # 触发时: 当前价是否已从峰值回撤 pct
            return last <= peak * (1 - pct)
        return None

    return None


def _fire_condition(st, c, last, df, side="buy", pos=None,
                    strategy=None, stop_pct=None, take_pct=None):
    """执行触发动作后把条件单标记为已触发 (status → "done")。

    策略/止盈/止损参数均可由调用方传入; 未传入时沿用候选快照/原有逻辑回退。
    """
    # 首先判断正确性
    entry_price = None
    condition_ret = None
    condition_peak = None
    # 从候选快照取该标的的行业/止损位 (供买入风控门禁判定; 非候选标的全为空 → fail-soft)
    _cond_cand_meta = {}
    for _e in st.get("candidates", []):
        if _e.get("code") == c["symbol"]:
            _cond_cand_meta = _e
            break

    if side == "buy":
        # 统一权益/3 等权口径: 传 st 让 _make_order 按 账户总权益/max_pos 分配,
        # 避免条件单路径走现金/3 导致"先买的大、后买的小"的顺序衰减与资金闲置。
        budget = c.get("amount") or st["cash"]
        # 条件单路径补齐策略归属与离场特化: 与 run_cycle 直接买入同口径。
        # 策略优先取传入参数, 兼容旧数据则回退候选快照。
        if strategy is None:
            strategy = c.get("strategy") or _cond_cand_meta.get("strategy", "")
        if stop_pct is None or take_pct is None:
            entry_px = _cond_cand_meta.get("entry_price")
            if strategy == STRATEGY_LONG_LEFT and entry_px:
                stop_px = float(_cond_cand_meta.get("stop_price") or 0)
                target_px = float(_cond_cand_meta.get("target_price") or 0)
                if stop_px:
                    stop_pct = round((entry_px - stop_px) / entry_px, 4) if stop_pct is None else stop_pct
                if target_px > stop_px:
                    take_pct = round((target_px - entry_px) / entry_px, 4) if take_pct is None else take_pct
        order = _make_order(c["symbol"], c.get("name", ""), "条件单",
                            c.get("qty", 0) or 0, last, 0, budget, st=st,
                            strategy=strategy,
                            stop_pct=stop_pct, take_pct=take_pct)
        if has_position(st, c["symbol"]):
            # 已持有该标的: 取消条件单, 防止对同一标的重建仓 (避免资金/仓位被重复占用)
            c["status"] = "cancelled"
            c["note"] = "已持有该标的, 入场条件单取消"
            c["correct"] = None
        elif _risk_blocks_entry(st, {
                "code": c["symbol"], "name": c.get("name", ""),
                "stop_price": _cond_cand_meta.get("stop_price"),
                "sector": _cond_cand_meta.get("sector", "")}, last):
            # 买入风控门禁 (与 run_cycle 直接入场路径同口径): 回撤上限/单笔风险/
            # 行业集中度/单股集中度/资金利用率。此前条件单触发绕过全部门禁,
            # 只查现金与同持上限, 造成"被风控拦截的标的仍可能通过条件单买入"。
            c["status"] = "cancelled"
            c["note"] = "风控门禁拦截, 未成交"
            c["correct"] = None
        elif order is None or len(st["positions"]) >= (
                _CUR["weak_max_pos"] if st.get("weak") else _CUR["max_pos"]):
            # 预算不足/同持已满: 条件单转取消, 防止永久悬挂
            c["status"] = "cancelled"
            c["note"] = "资金/同持上限不足(含弱市限仓), 未成交"
            c["correct"] = None
        else:
            order["day"] = str(df["day"].iloc[-1]) if df is not None else ""
            res, msg = fill_buy(st, order)
            if res is None:
                # 现金不足等: 不成交, 取消条件单防止悬挂
                c["status"] = "cancelled"
                c["note"] = msg or "未成交"
                c["correct"] = None
            else:
                c["matched_price"] = order["price"]
                c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                c["status"] = "done"
                # 买入条件单: 判断触发价是否被满足
                # buy_price: 我们在 pick_candidates 中设置 price = last * 1.002 (略高于当前价)
                # 触发意味着 last >= price，所以 correct=True
                c["correct"] = True  # buy_price 已触发突破
                try:
                    paper_log.log_condition_fired(
                        c["symbol"], c.get("name", ""), c["kind"],
                        c.get("price"), last, action="买入", reason="价格触发")
                except Exception:
                    pass
    else:
        if pos is None:
            pos = _find_pos(st, c["symbol"])
        if pos is None:
            c["status"] = "cancelled"
            c["note"] = "无持仓可平"
            c["correct"] = None
            return
        sell_price = round(last * (1 - SLIP_SELL), 3)
        entry_price = float(pos["buy_px"])
        condition_ret = last / entry_price - 1

        # 计算 trailing 的 peak
        if c["kind"] == "trailing" and c.get("peak") is not None:
            condition_peak = c["peak"]
        elif c["kind"] == "trailing":
            # 从持仓峰值计算
            condition_peak = max(last, entry_price)

        close_position(st, pos, sell_price, f"条件单:{c['kind']}", event_type=None)
        c["matched_price"] = sell_price
        c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        c["status"] = "done"
        try:
            paper_log.log_condition_fired(
                c["symbol"], c.get("name", ""), c["kind"],
                c.get("price"), last, action="卖出", reason=f"条件单:{c['kind']}")
        except Exception:
            pass

        # 判断卖出条件单是否正确
        c["correct"] = _judge_condition_correct(
            c, last, entry=entry_price, ret=condition_ret,
            peak=condition_peak, side="sell",
            cond_price=c.get("price"), pct=c.get("pct")
        )


# ── 微信推送 (交易提醒) ─────────────────────────────────────
_STRATEGY_LABELS = {
    STRATEGY_DISCIPLINE: "纪律",
    STRATEGY_VALUE_ACC: "价值吸筹",
    STRATEGY_LONG_LEFT: "左侧买点",
}


def _split_list(v):
    """把"逗号/顿号分隔"的字符串拆成非空列表 (兼容中英文分隔符)。"""
    if not v:
        return []
    out = []
    for item in str(v).replace("，", ",").replace("、", ",").split(","):
        item = item.strip()
        if item:
            out.append(item)
    return out


def _push_dispatch(method, cfg, title, content):
    """后台线程发送微信推送; 失败静默 (不阻塞撮合引擎)。"""
    try:
        from .wechat_push import push_to_wechat

        push_to_wechat(method, title=title, content=content, **cfg)
    except Exception:
        pass


def _notify_trade(kind, **info):
    """交易发生时推送微信消息 (buy/sell)。未启用或配置缺失时静默跳过。"""
    if not bool(_CUR.get("push_enabled")):
        return
    method = str(_CUR.get("push_method") or "").lower()
    if method not in ("server_chan", "wechat_work", "wxpusher"):
        return
    cfg = {
        "server_chan": {"sckey": _CUR.get("server_chan_key", "")},
        "wechat_work": {
            "corp_id": _CUR.get("wechat_corp_id", ""),
            "corp_secret": _CUR.get("wechat_corp_secret", ""),
            "agent_id": _CUR.get("wechat_agent_id", ""),
            "to_user": _CUR.get("wechat_to_user", "") or "",
        },
        "wxpusher": {
            "app_token": _CUR.get("wxpusher_app_token", ""),
            "topic_ids": _split_list(_CUR.get("wxpusher_topic_ids", "")),
            "uids": _split_list(_CUR.get("wxpusher_uids", "")),
        },
    }[method]
    if method == "server_chan" and not cfg["sckey"]:
        return
    if method == "wechat_work" and not (cfg["corp_id"] and cfg["corp_secret"]):
        return
    if method == "wxpusher" \
            and not (cfg["app_token"] and (cfg["topic_ids"] or cfg["uids"])):
        return
    code = info.get("symbol", "")
    name = info.get("name", "") or str(code)[-6:]
    strat = _STRATEGY_LABELS.get(info.get("strategy", ""), info.get("strategy", ""))
    if kind == "buy":
        title = f"[模拟盘] 买入 {name} {code}"
        lines = [
            f"> **买入 {name} ({code})**",
            f"- 策略: {strat}",
            f"- 数量: {info.get('qty')} 股",
            f"- 价格: {info.get('price')}",
            f"- 金额: {info.get('amount', '')}",
            f"- 时间: {info.get('ts', '')}",
        ]
    else:
        ret = info.get("ret")
        ret_txt = f"{ret * 100:+.2f}%" if isinstance(ret, (int, float)) else ""
        title = f"[模拟盘] 卖出 {name} {code} {ret_txt}"
        lines = [
            f"> **卖出 {name} ({code})**",
            f"- 策略: {strat}",
            f"- 数量: {info.get('qty')} 股",
            f"- 买价: {info.get('buy_price')} / 卖价: {info.get('sell_price')}",
            f"- 收益率: {ret_txt}",
            f"- 原因: {info.get('reason', '')}",
        ]
    content = "\n".join(lines)
    try:
        threading.Thread(
            target=_push_dispatch, args=(method, cfg, title, content),
            daemon=True,
        ).start()
    except Exception:
        pass


def fill_buy(st, order, event_type: str = None):
    """口头成交: 扣现金、建仓。现金不足时不成交, 返回 (None, 原因)。"""
    price = order["price"]
    qty = order["qty"]
    cost = qty * price * _CUR["cost"]
    spend = qty * price + cost
    if st.get("cash", 0) < spend:
        return None, "现金不足"
    st["cash"] -= spend
    # 止损/止盈: 左侧买点自带御设 (run_cycle/条件单路径已折算为 stop_pct/take_pct);
    # 其余策略走账户默认 (价值吸筹无需特化: 实证无独立止盈/止损可采纳证据,
    # 与全局默认一致, 见 _rebalance/离场逻辑)。
    stop_pct = order.get("stop_pct")
    take_pct = order.get("take_pct")
    st["positions"].append({
        "symbol": order["symbol"], "name": order.get("name", ""),
        "type": order["type"], "conf": order.get("conf", 50),
        "qty": qty, "buy_px": price, "cost": round(cost, 2),
        "entry_ts": order["ts"], "entry_bars": order.get("bars", 0),
        "sector": order.get("sector", ""),
        "strategy": order.get("strategy", ""),
        "stop_pct": stop_pct,
        "take_pct": take_pct,
        "staged": False,
        "event_type": event_type,
        "entry_day": order.get("day")
        or datetime.now().strftime("%Y-%m-%d"),
    })
    st["orders"].append(order)
    _create_position_conditions(st, order["symbol"], name=order.get("name", ""),
                                buy_px=price)
    save_state(st)
    try:
        paper_strategy_accuracy.mark_fired(order.get("strategy", ""),
                                           order["symbol"])
    except Exception:
        pass
    try:
        paper_log.log_buy(
            symbol=order["symbol"], name=order.get("name", ""),
            qty=qty, price=price, conf=order.get("conf", 0),
            strategy=order.get("strategy", ""), event_type=event_type or "",
            sector=order.get("sector", ""), reason=order.get("reason", "买入")
        )
    except Exception:
        pass
    _notify_trade("buy", symbol=order["symbol"],
                  name=order.get("name", ""), qty=qty, price=price,
                  strategy=order.get("strategy", ""),
                  amount=round(spend, 2), ts=order["ts"])
    return order, "成交"


def step(st, df_by_code, trading=True):
    """推进一个周期: 用当前行情更新最新价 → 检查卖单条件 + 成交 in-arrow。

    df_by_code: {symbol: df(含 indicators)} 当前最新行情窗口 (由 UI/调度器提供)。
    不可用 (无行情) 时跳过。
    trading=False: 收盘/节假日后的"纯快照"模式 — 只 mark-to-market 更新最新价,
    不触发任何买卖撮合 (买/卖/止盈止损/待撮合买单全部冻结, 待下一个交易日时段内
    恢复)。供 run_cycle 在非交易时段禁止撮合用。
    """
    # 先检查条件单: 用户自定义的价格触发/止盈/止损/追踪优先于默认止盈止损。
    if trading:
        _check_conditions(st, df_by_code)
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        if df is None or len(df) == 0:
            continue
        bar_day = str(df["day"].iloc[-1]) or ""
        entry = float(pos["buy_px"])
        entry_day = str(pos.get("entry_day") or "")
        # 开仓当日 (最新 K 线交易日 == 买入触发日): 用成交价做基准标记,
        # 避免"买入前一日收盘价"对刚开的仓造成假浮亏; 次一交易日自动切换真实收盘价。
        if bar_day and entry_day and bar_day[:10] == entry_day[:10]:
            last = entry
        else:
            last = float(df["close"].iloc[-1])
        ret = last / entry - 1
        pos["last"] = round(last, 3)
        pos["last_ret"] = round(float_ret(entry, last), 4)
        # 止损价: 固定止损 或 追踪止损 (从持仓期内最高价回撤, 防噪音洗出)。
        # 左侧买点持仓优先按其自带御设止损/止盈百分比。
        pos_take = float(pos.get("take_pct") or _CUR["take_profit"])
        pos_stop = float(pos.get("stop_pct") or _CUR["stop_loss"])
        # 移动止盈模式: 峰值回撤交给 trailing 条件单 (浮盈达激活价后回落平仓),
        # 此处兜底只留固定结构止损 (相对买入价), 避免双重追踪截断回落窗口。
        trailing_mode = bool(_CUR.get("trailing_stop", TRAILING_STOP)
                             and float(_CUR.get("trail_back_pct", 0)) > 0)
        if trailing_mode:
            stop_px = entry * (1 - pos_stop)
        elif _CUR.get("trailing_stop", TRAILING_STOP):
            hi = float(df["high"].iloc[-1])
            peak = float(pos.get("peak") or entry)
            if hi > peak:
                peak = hi
                pos["peak"] = round(peak, 3)
            stop_px = peak * (1 - pos_stop)
            atr_mult = float(_CUR.get("trail_atr_mult", TRAIL_ATR_MULT) or 0.0)
            if atr_mult > 0 and "atr" in df.columns and len(df):
                atr = float(df["atr"].iloc[-1] or 0.0)
                stop_px -= atr * atr_mult
        else:
            stop_px = entry * (1 - pos_stop)
        # 结构位: 用最近 10 根低点做动态支撑 (近似结构关键位)
        support = float(df["low"].iloc[-10:].min())
        # 卖出判定 (任一触发); 持仓 K 数按"交易日"推进: 仅当行情 K 线交易日
        # 发生变化才 +1, 避免同一天多周期被重复计入 (曾出现 20 周期≈10 小时即
        # "到期"的高估)。
        if bar_day and pos.get("counted_day") != bar_day:
            pos["counted_day"] = bar_day
            pos["entry_bars"] = int(pos.get("entry_bars", 0)) + 1
        reason = None
        held = int(pos["entry_bars"])
        if _t1_blocked(pos, df):
            # A股 T+1: 当日买入的证券次一交易日方可卖出, 本周期跳过卖出判定
            continue
        if not trading:
            # 非交易时段: 只 mark-to-market (上面已更新 last), 冻结卖出判定
            continue
        if not trailing_mode and ret >= pos_take:
            reason = "止盈"
        elif last <= stop_px:
            reason = "止损"
        elif support < stop_px and last <= support:
            reason = "破位"
        elif held >= _CUR["hold_bars"]:
            reason = "到期"
        if reason:
            # 模拟盘按市价即时成交: 一律以现价(含卖滑点)结算, 不再用 max(stop_px,last)
            # 高估止损价。历史实现止损在 last<stop_px 时按 stop_px 成交, 低估了实际损失。
            sell_price = last * (1 - SLIP_SELL)
            close_position(st, pos, sell_price, reason, event_type=pos.get("event_type"))
    # 处理待撮合买单 (非交易时段冻结, 待恢复后撮合)
    if trading:
        for o in list(st["pending"]):
            df = df_by_code.get(o["symbol"])
            if df is not None and len(df):
                close = float(df["close"].iloc[-1])
                o["price"] = round(close * (1 + SLIP_BUY), 3)
                o["day"] = str(df["day"].iloc[-1])
                if not o.get("date"):
                    o["date"] = str(df["day"].iloc[-1])
                order = dict(o)
                st["pending"].remove(o)
                fill_buy(st, order)


def _rebalance_portfolio(st, df_by_code):
    """满仓时向等权目标收敛, 把权重过低的持仓补足到 总权益/有效持仓上限。

    触发条件: len(positions) == 有效持仓上限 (弱市=WEAK_MAX_POS, 否则=MAX_POS)
    且现金富余; 弱市的单仓若已超 MAX_SINGLE_CONCENTRATION 集中度上限则不再补。
    加仓直接合并进已有持仓 (摊薄成本), 不新增同标的多仓; 保留已建立的追踪 peak 与
    entry_bars (加仓不改变止损保护起点)。本次交易日加仓的份额受 T+1 约束:
    合并持仓的 entry_day 顺延为加仓交易日, 当日禁止卖出。返回补仓笔数。

    df_by_code: 需含待补仓标的当根行情 (用于现价与成交)。
    """
    from wyckoff.strategies.constants import STRATEGY_VALUE_ACC
    if not _CUR.get("rebalance", False):
        return 0
    eff_max = _CUR["weak_max_pos"] if st.get("weak") else _CUR["max_pos"]
    if len(st.get("positions", [])) < eff_max:
        return 0
    positions = st.get("positions", [])
    mv_total = sum(float(p.get("last", p["buy_px"])) * p["qty"] for p in positions)
    equity = float(st["cash"]) + mv_total
    target = equity / max(1, eff_max)
    rebalanced = 0
    for pos in positions:
        sym = pos["symbol"]
        last = float(pos.get("last") or pos["buy_px"])
        cur_mv = float(pos.get("last", pos["buy_px"])) * pos["qty"]
        # 价值吸筹单仓资金权重: 降低弱策略敞口
        va_weight = float(_CUR.get("va_weight", 0.6) or 1.0)
        strategy = pos.get("strategy", "")
        effective_target = target
        if strategy == STRATEGY_VALUE_ACC:
            effective_target = target * va_weight
        # 容忍带: 仅当权重明显不足 (< 目标*0.75) 且现金可覆盖时才补
        short = effective_target * 0.75 - cur_mv
        if short <= 0:
            continue
        if st.get("weak"):
            # 弱市单仓集中度上限: 补仓后该股市值不得超过 总权益*max_single_conc
            # (弱市目标=总权益/1 会过度集中, 以单股集中度兜底)
            short = min(short, _CUR["max_single_conc"] * equity - cur_mv)
            if short <= 0:
                continue
        cost = last * (1 + SLIP_BUY)
        qty = int(short // (cost) // 100 * 100)  # 整手(百股)
        if qty <= 0:
            continue
        fee = qty * cost * _CUR["cost"]
        spend = qty * cost + fee
        if st["cash"] < spend:
            continue
        # 合并进已有持仓: 追加数量 + 摊薄成本
        old_qty = pos["qty"]
        pos["qty"] = int(old_qty) + qty
        new_buy = (float(pos["buy_px"]) * old_qty + cost * qty) / pos["qty"]
        pos["buy_px"] = round(new_buy, 3)
        pos["cost"] = round(float(pos.get("cost", 0)) + fee, 2)
        pos["last"] = round(last, 3)
        st["cash"] -= spend
        # A股 T+1: 加仓份额当日不可卖, 合并持仓 entry_day 顺延为加仓交易日,
        # 使整仓当日禁止卖出 (与 fill_buy 的新仓同口径)。
        try:
            _d = df_by_code.get(sym)
            if _d is not None and len(_d):
                pos["entry_day"] = str(_d["day"].iloc[-1])
        except Exception:
            pass
        st["orders"].append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "symbol": sym,
            "name": pos.get("name", ""), "strategy": pos.get("strategy", ""),
            "qty": qty, "price": round(cost, 3), "type": "再平衡加仓",
            "conf": pos.get("conf", 0), "side": "buy", "date": "",
        })
        try:
            paper_log.log_rebalance(sym, pos.get("name", ""), qty,
                                    round(cost, 3), old_qty, pos["qty"])
        except Exception:
            pass
        rebalanced += 1
    return rebalanced


def close_position(st, pos, sell_price, reason, event_type=None):
    """平仓: 回收现金、记录已平仓与净值。"""
    from wyckoff.strategies.sell_strategy import evaluate_sell_reason
    # 基于事件类型的卖出策略
    reason = evaluate_sell_reason(event_type, reason)
    price = round(sell_price, 3)
    gross = price * pos["qty"]  # 不含卖出成本的口径内部用
    fee = gross * _CUR["cost"]
    proceeds = gross - fee
    st["cash"] += proceeds
    outlay = pos["buy_px"] * pos["qty"] * net_cost_rate()
    ret_total = (proceeds - outlay) / outlay
    st["closed"].append({
        "symbol": pos["symbol"], "name": pos.get("name", ""),
        "type": pos["type"], "conf": pos.get("conf", 50),
        "qty": pos["qty"], "buy_px": pos["buy_px"],
        "sell_px": price, "ret": round(ret_total, 4),
        "reason": reason, "entry_ts": pos["entry_ts"],
        "close_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bars": int(pos.get("entry_bars", 0)),
        "strategy": pos.get("strategy", ""),
    })
    st["positions"] = [p for p in st["positions"] if p is not pos]
    _record_equity(st)
    save_state(st)
    try:
        paper_log.log_sell(
            symbol=pos["symbol"], name=pos.get("name", ""),
            qty=pos["qty"], buy_price=pos["buy_px"], sell_price=price,
            reason=reason, ret=ret_total,
            bars_held=int(pos.get("entry_bars", 0)),
            strategy=pos.get("strategy", ""), event_type=pos.get("event_type", "")
        )
    except Exception:
        pass
    _notify_trade("sell", symbol=pos["symbol"], name=pos.get("name", ""),
                  qty=pos["qty"], buy_price=pos["buy_px"], sell_price=price,
                  ret=ret_total, reason=reason, strategy=pos.get("strategy", ""))


def force_close_position(st, symbol: str, reason: str = "手动平仓"):
    """按代码手动平仓 (供 UI 持仓行操作)。找不到持仓返回 None, 成功返回平仓记录。

    A股 T+1: 当日买入 (entry_day == 今天) 的持仓拒绝当日平仓, 返回 None。
    """
    for pos in st["positions"]:
        if pos["symbol"] == symbol:
            entry_day = pos.get("entry_day") or ""
            if entry_day and entry_day == datetime.now().strftime("%Y-%m-%d"):
                return None
            close_position(st, pos, pos.get("last", pos["buy_px"]), reason)
            return pos
    return None


def net_cost_rate():
    """含买入费用(单边成本)的每股成本系数 = 1 + cost。"""
    return 1.0 + _CUR["cost"]


def float_ret(buy_px, last):
    """当前持仓净浮盈 (%)。

    按当前价即时卖出 (扣卖滑点 + 卖出费用) 后的净收益,
    相对买入含费成本 (买入价含买滑点 + 买入费用)。
    与 close_position 记录的实际净收益口径一致。
    """
    buy_px = float(buy_px)
    if buy_px <= 0:
        return 0.0
    sell_net = float(last) * (1 - SLIP_SELL) * (1 - _CUR["cost"])
    cost = buy_px * net_cost_rate()
    return sell_net / cost - 1


def equity(st, df_by_code):
    """总资产 = 现金 + 持仓市值 (未扣未实现卖出费/滑点, 市值口径)。"""
    mv = 0.0
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else pos.get("last", pos["buy_px"])
        mv += px * pos["qty"]
    return st["cash"] + mv


def _record_equity(st, day=None):
    """按交易日 upsert 一条账户净值快照到 equity_hist (同一天多次周期只保留最新)。

    资金曲线与账户概览以这里记录的快照为准: 持仓开仓后每个周期都会刷新,
    避免曲线只能看到平仓点、与现状脱节。
    """
    ts = (day or time.strftime("%Y-%m-%d"))[:10]
    hist = st.get("equity_hist") or []
    for h in hist:
        if h.get("ts") == ts:
            h["equity"] = round(equity(st, {}), 2)
            h["cash"] = round(st["cash"], 2)
            return
    hist.append({
        "ts": ts,
        "cash": round(st["cash"], 2),
        "equity": round(equity(st, {}), 2),
    })


# ── 收益统计 ─────────────────────────────────────────────
def stats(st):
    """账户/策略统计。返回 dict (供 UI/报告)。

    包含基础统计 + 高级绩效指标:
    - 夏普比率, 索提诺比率, 卡尔马比率
    - 年化收益/波动率
    - 回撤分析 (最大回撤、平均回撤、回撤持续时间)
    - 交易分布统计
    - 风险调整收益指标
    """
    closed = st["closed"]
    out = {
        "cash": round(st["cash"], 2),
        "n_positions": len(st["positions"]),
        "n_closed": len(closed),
        "n_orders": len(st["orders"]),
        "n_pending": len(st["pending"]),
        "n_cond_active": sum(1 for c in st.get("conditions", [])
                             if c.get("status") == "active"),
        "n_cond_done": sum(1 for c in st.get("conditions", [])
                           if c.get("status") == "done"),
        "total_return": 0.0,
        "win_rate": None,
        "pl_ratio": None,
        "avg_ret": None,
        "best": None,
        "worst": None,
        "max_drawdown": None,
        # 高级绩效指标
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "calmar_ratio": None,
        "annual_return": None,
        "annual_volatility": None,
        "downside_volatility": None,
        "avg_drawdown": None,
        "max_drawdown_duration": None,
        "recovery_factor": None,
        "profit_factor": None,
        "expectancy": None,
        "kelly_fraction": None,
        "by_type": {},
        "by_reason": {},
        "by_sector": {},
        "by_strategy": {},
        "monthly_returns": {},
        "trade_distribution": {},
    }
    rets = [c["ret"] for c in closed if c.get("ret") is not None]
    if rets:
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r <= 0]
        out["win_rate"] = round(len(wins) / len(rets), 4)
        out["avg_ret"] = round(statistics.mean(rets), 4)
        out["best"] = round(max(rets), 4)
        out["worst"] = round(min(rets), 4)
        if losses:
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = abs(statistics.mean(losses))
            out["pl_ratio"] = round(avg_win / avg_loss, 3) if avg_loss > 0 else None
            out["profit_factor"] = round(sum(wins) / abs(sum(losses)), 3) if losses else None
            # 期望值
            out["expectancy"] = round(out["win_rate"] * avg_win - (1 - out["win_rate"]) * avg_loss, 4)
            # Kelly 分数
            if avg_loss > 0 and avg_win > 0:
                out["kelly_fraction"] = round((out["win_rate"] * avg_win - (1 - out["win_rate"]) * avg_loss) / avg_win, 4)
        # 分类型
        by_type = {}
        for c in closed:
            t = c.get("type", "?")
            b = by_type.setdefault(t, {"n": 0, "rets": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
        for t, b in by_type.items():
            rs = b["rets"]
            b["avg"] = round(statistics.mean(rs), 4)
            b["win"] = round(sum(1 for r in rs if r > 0) / len(rs), 4)
            if any(r > 0 for r in rs) and any(r <= 0 for r in rs):
                b["pl_ratio"] = round(
                    statistics.mean([r for r in rs if r > 0]) / abs(statistics.mean([r for r in rs if r <= 0])), 3)
            else:
                b["pl_ratio"] = 0
            b.pop("rets", None)
        out["by_type"] = by_type
        # 平仓原因分布
        by_reason = {}
        for c in closed:
            r = c.get("reason", "?")
            by_reason.setdefault(r, {"n": 0, "avg": []})["n"] += 1
            by_reason[r]["avg"].append(c["ret"])
        for r, b in by_reason.items():
            b["avg"] = round(statistics.mean(b["avg"]), 4)
        out["by_reason"] = by_reason
        # 行业分布
        by_sector = {}
        for c in closed:
            sec = c.get("sector", "未知")
            b = by_sector.setdefault(sec, {"n": 0, "rets": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
        for sec, b in by_sector.items():
            rs = b["rets"]
            b["avg"] = round(statistics.mean(rs), 4)
            b["win"] = round(sum(1 for r in rs if r > 0) / len(rs), 4)
            b.pop("rets", None)
        out["by_sector"] = by_sector
        # 策略分层 (模拟盘三策略并线: 纪律 / 左侧买点 / 价值吸筹) — 盈利能力口径。
        # cum 用 (1+r) 连乘再减一, 反映真实累计, 避免简单累加被单笔大单主导。
        by_strategy = {}
        for c in closed:
            s = c.get("strategy", "") or "未知"
            b = by_strategy.setdefault(s, {"n": 0, "rets": [], "bars": []})
            b["n"] += 1
            b["rets"].append(c["ret"])
            b["bars"].append(int(c.get("bars", 0) or 0))
        for s, b in by_strategy.items():
            rs = b["rets"]
            wins = [r for r in rs if r > 0]
            losses = [r for r in rs if r <= 0]
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = abs(statistics.mean(losses)) if losses else 0.0
            cum = 1.0
            for r in rs:
                cum *= 1.0 + r
            b["win"] = round(len(wins) / len(rs), 4)
            b["avg"] = round(statistics.mean(rs), 4)
            b["cum"] = round(cum - 1.0, 4)
            b["pl_ratio"] = round(avg_win / avg_loss, 3) if avg_loss > 0 else None
            b["expectancy"] = round(
                (len(wins) / len(rs)) * avg_win
                - (1 - len(wins) / len(rs)) * avg_loss, 4)
            b["avg_hold"] = round(statistics.mean(b["bars"]), 1) if b["bars"] else 0.0
            b.pop("rets", None)
            b.pop("bars", None)
        out["by_strategy"] = by_strategy
        # 交易分布统计
        out["trade_distribution"] = {
            "n_wins": len(wins),
            "n_losses": len(losses),
            "avg_win": round(statistics.mean(wins), 4) if wins else 0,
            "avg_loss": round(statistics.mean(losses), 4) if losses else 0,
            "largest_win": round(max(wins), 4) if wins else 0,
            "largest_loss": round(min(losses), 4) if losses else 0,
            "avg_hold_bars": round(statistics.mean([c.get("bars", 0) for c in closed]), 1),
        }

    # 总收益率: 当前总资产 (现金+持仓市值) 相对初始模拟资金, 含未平仓浮盈亏
    init = float(_CUR["init_cash"])
    mv = sum(float(p.get("last", p["buy_px"])) * p["qty"] for p in st["positions"])
    eq_now = out["cash"] + round(mv, 2)
    out["equity"] = round(eq_now, 2)
    out["total_return"] = round(eq_now / init - 1, 4) if init else 0.0

    hist = st.get("equity_hist") or []
    eqs = [h.get("equity", init) for h in hist] if hist else []
    if eqs:
        # 计算日收益率序列
        daily_rets = []
        for i in range(1, len(eqs)):
            if eqs[i-1] > 0:
                daily_rets.append(eqs[i] / eqs[i-1] - 1)

        if daily_rets and np is not None:
            arr = np.asarray(daily_rets, dtype=float)
            # 年化收益率 (假设日线, 250 个交易日)
            out["annual_return"] = round(float(arr.mean() * 250), 4)
            # 年化波动率
            out["annual_volatility"] = round(float(arr.std() * np.sqrt(250)), 4)
            # 下行波动率 (仅负收益)
            neg_rets = arr[arr < 0]
            out["downside_volatility"] = round(float(neg_rets.std() * np.sqrt(250)), 4) if len(neg_rets) > 1 else 0.0

            # 夏普比率 (假设无风险利率 3%)
            rf = 0.03 / 250  # 日无风险利率
            excess = arr - rf
            if excess.std() > 0:
                out["sharpe_ratio"] = round(float(excess.mean() / excess.std() * np.sqrt(250)), 3)

            # 索提诺比率
            if out["downside_volatility"] and out["downside_volatility"] > 0:
                out["sortino_ratio"] = round(float((arr.mean() - rf) * 250 / out["downside_volatility"]), 3)

            # 最大回撤
            peak = np.maximum.accumulate(arr + 1)  # 累积净值
            cum = np.cumprod(arr + 1)
            dd = cum / peak - 1
            out["max_drawdown"] = round(float(dd.min()), 4)

            # 卡尔马比率
            if out["max_drawdown"] and out["max_drawdown"] < 0:
                out["calmar_ratio"] = round(out["annual_return"] / abs(out["max_drawdown"]), 3)

            # 平均回撤
            out["avg_drawdown"] = round(float(dd[dd < 0].mean()), 4) if any(dd < 0) else 0.0

            # 最大回撤持续时间
            in_dd = dd < 0
            if any(in_dd):
                dd_starts = np.where(np.diff(np.concatenate(([False], in_dd))))[0]
                dd_ends = np.where(np.diff(np.concatenate((in_dd, [False]))))[0]
                if len(dd_starts) == len(dd_ends):
                    durations = dd_ends - dd_starts
                    out["max_drawdown_duration"] = int(durations.max()) if len(durations) > 0 else 0

            # 恢复因子 = 总净收益 / 最大回撤
            if out["max_drawdown"] and out["max_drawdown"] < 0:
                out["recovery_factor"] = round(out["total_return"] / abs(out["max_drawdown"]), 3)

    return out


def advanced_stats(st, benchmark_returns: list[float] = None) -> dict:
    """高级绩效分析，包含相对基准指标。

    Args:
        st: 模拟盘状态
        benchmark_returns: 基准日收益率序列 (如沪深300)

    Returns:
        包含 Alpha, Beta, 信息比率, 跟踪误差等的字典
    """
    base = stats(st)
    hist = st.get("equity_hist") or []
    if len(hist) < 2:
        return base

    eqs = [h.get("equity", _CUR["init_cash"]) for h in hist]
    daily_rets = []
    for i in range(1, len(eqs)):
        if eqs[i-1] > 0:
            daily_rets.append(eqs[i] / eqs[i-1] - 1)

    if not daily_rets or np is None:
        return base

    arr = np.asarray(daily_rets, dtype=float)
    out = base.copy()

    if benchmark_returns and len(benchmark_returns) == len(arr):
        bench = np.asarray(benchmark_returns, dtype=float)
        # Beta
        cov = np.cov(arr, bench)[0, 1]
        bench_var = np.var(bench)
        beta = cov / bench_var if bench_var > 0 else 1.0
        out["beta"] = round(float(beta), 3)
        # Alpha (年化)
        alpha = (arr.mean() - beta * bench.mean()) * 250
        out["alpha"] = round(float(alpha), 4)
        # 跟踪误差
        active_rets = arr - beta * bench
        tracking_error = active_rets.std() * np.sqrt(250)
        out["tracking_error"] = round(float(tracking_error), 4)
        # 信息比率
        if tracking_error > 0:
            out["information_ratio"] = round(float(active_rets.mean() * np.sqrt(250) / tracking_error), 3)
        # 上行/下行捕获率
        up_market = bench > 0
        down_market = bench < 0
        if any(up_market):
            out["up_capture"] = round(float(arr[up_market].mean() / bench[up_market].mean()), 3)
        if any(down_market):
            out["down_capture"] = round(float(arr[down_market].mean() / bench[down_market].mean()), 3)

    # 交易成本分析
    total_cost = sum(c.get("cost", 0) for c in st.get("closed", []) if "cost" in c)
    out["total_cost"] = round(total_cost, 2)
    out["cost_drag"] = round(total_cost / _CUR["init_cash"] * 100, 2) if _CUR["init_cash"] > 0 else 0

    return out


def signal_stats_text(st):
    """Markdown 统计段 (报告导出用)。"""
    s = stats(st)
    L = []
    L.append("### 模拟盘收益统计")
    L.append("")
    L.append(f"- 总资产: **{s['cash']:,}** 当前持仓 {s['n_positions']} 只, "
             f"已平仓 {s['n_closed']} 笔, 订单 {s['n_orders']} 笔")
    L.append(f"- 条件单: 激活 {s['n_cond_active']} · 已触发 {s['n_cond_done']}")
    # 修复: 原先三元表达式被字符串化 (max_drawdown 为 None 时也强制格式化),
    # 造成 "最大回撤: ... if ... is not None else '-'" 的样式错误与崩溃。
    dd = s["max_drawdown"]
    dd_txt = "-" if dd is None else f"{dd*100:.2f}%"
    L.append(f"- 累计收益: **{s['total_return']*100:+.2f}%**  最大回撤: {dd_txt}")
    if s["win_rate"] is not None:
        L.append(f"- 胜率: **{s['win_rate']*100:.1f}%**  平均每笔: "
                 f"{s['avg_ret']*100:+.2f}%  盈亏比: {s['pl_ratio']}")
    if s["by_type"]:
        L.append("")
        L.append("| 事件 | 笔数 | 胜率 | 平均收益 |")
        L.append("|------|------|------|----------|")
        for t, b in sorted(s["by_type"].items(), key=lambda kv: -kv[1]["n"]):
            L.append(f"| {t} | {b['n']} | {b['win']*100:.0f}% | "
                     f"{b['avg']*100:+.2f}% |")
    if s.get("by_strategy"):
        L.append("")
        L.append("| 策略 | 笔数 | 胜率 | 平均收益 | 累计 | 盈亏比 | 期望 | 平均持有 |")
        L.append("|------|------|------|----------|------|--------|------|----------|")
        for strat, b in sorted(s["by_strategy"].items(),
                               key=lambda kv: -kv[1]["n"]):
            plr = (f"{b['pl_ratio']:.2f}" if b.get("pl_ratio") is not None
                   else "-")
            L.append(f"| {paper_strategy_accuracy.STRATEGY_CN.get(strat, strat)} "
                     f"| {b['n']} | {b['win']*100:.0f}% | "
                     f"{b['avg']*100:+.2f}% | {b['cum']*100:+.2f}% | {plr} "
                     f"| {b['expectancy']:+.4f} | {b.get('avg_hold', 0):.1f}根 |")
    L.append("")
    return "\n".join(L)


def _reset_logs():
    """清空模拟盘当日全部日志 (paper_logs), 供账户重置时同步清理。"""
    try:
        paper_log.clear_today()
    except Exception:
        pass


def _scan_due(st):
    """距上次全市场扫描是否已过冷却窗口 (next_scan_time 文本即 isoformat 比较)。"""
    nxt = st.get("next_scan_time") or ""
    if not nxt:
        return True
    try:
        return datetime.now().isoformat() >= nxt
    except Exception:
        return True


PAPER_BUSY_MSG = ("另一实例 (界面或系统定时任务) 正在执行中, 本次已跳过")


def _acquire_paper_lock():
    """跨进程互斥锁 (Windows msvcrt / POSIX fcntl)。

    UI 内的自动周期线程与系统定时任务 (schtasks/cron) 会并发触发 run_scan /
    run_cycle, 两者争用同一份 wx_paper.json。这里保证同一时刻只允许一个执行者
    进入; 拿不到锁 (已被占用) 返回 None。进程崩溃/被杀时 OS 自动释放锁,
    不会残留卡死后续调度。
    """
    fh = None
    try:
        from .paths import DATA_DIR as _DD
        lock_path = os.path.join(_DD, "wx_paper.lock")
        os.makedirs(_DD, exist_ok=True)
        fh = open(lock_path, "a+b")
        fh.seek(0)
        fh.truncate(1)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _release_paper_lock(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


def _paper_locked(fail=None):
    """装饰器: 只在实际拿到跨进程锁时才执行 fn; 锁被占用时返回 fail。"""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            fh = _acquire_paper_lock()
            if fh is None:
                return fail() if callable(fail) else fail
            try:
                return fn(*args, **kwargs)
            finally:
                _release_paper_lock(fh)
        return wrapper

    return deco


@_paper_locked(fail=None)
def run_cycle(settings=None, min_conf=None, universe=None, candidates=None,
              force_scan=False, strategies=None, progress=None, anytime=False):
    """无头自动运行一个周期: 筛选→下单→步进→统计。返回统计。

    供 cron / 调度线程 / 手动触发。每周期持仓 K 数 +1,
    到期/止盈/止损/破位在该周期内平仓。
    settings 传入界面设置 dict (S.Paper.* 键) 覆盖策略参数; min_conf 显式传入
    时优先于 settings (兼容旧调用方)。candidates 传入时直接复用 (跳过选股),
    供"扫描完成→自动买入"避免二次全市场扫描。
    candidates 为 None 时, 若距上次扫描仍在冷却窗口内且已有候选快照, 则复用
    st["candidates"] 而非无条件重扫全市场 (多周期调度每 30 分钟触发一次全市场
    重扫会造成选股覆盖与节流), 用 force_scan=True 强制立即重扫。
    strategies: 可选策略 key 子集 (与 run_scan 单策略模式同语义), 仅在需要
    重扫时透传给 pick_candidates; 复用候选/传入候选时过滤由调用方保证。
    progress: 可选扫描进度回调 (done, total, code), 仅在真正执行全市场重扫时
    上报; 复用候选/传入候选时不触发 (供 UI 区分"正在扫描"与"仅执行周期")。
    anytime: True 时绕开交易时段门禁 (供手动补跑/测试); 默认 False — 非交易
    时段执行时自动降级为"纯快照"模式: 只更新持仓最新价与净值, 不重扫、不生成
    入场条件单、不撮合任何买卖 (防止收盘后按收盘价"成交"的不合理交易)。
    """
    from .datasource import fetch_kline
    from .indicators import add_indicators

    apply_paper_params(settings)
    if min_conf is None:
        min_conf = _CUR["min_conf"]

    # 撮合门禁: 非交易时段 → 纯快照 (anytime 可强制放行, 供手动补跑有交易)。
    trading = anytime or gate_reason() is None

    st = load_state()
    # 弱市过滤: 指数未站上 MA20 → 降仓上限与停用价值吸筹 (与回测移动止盈口径一致)
    st["weak"] = _weak_market_flag()
    weak = st["weak"]
    # 1) 持仓防护回填: 对缺失止盈/止损保护条件单的已有持仓自动补齐
    #    (_create_position_conditions 幂等: 已有 active 不重复; 并消费同标的入场单)。
    #    覆盖历史遗留/非 fill_buy 路径建立的仓位, 避免"裸奔"只有 step 兜底。
    _backfill_position_protection(st)
    # 1) 选股: candidates 传入时直接复用扫描结果 (扫描已完成选股)
    if not trading:
        # 非交易时段: 只快照 — 复用现有候选, 不重扫/不更新条件单/不撮合。
        cand = st.get("candidates") or []
    elif candidates is not None:
        cand = candidates
    else:
        reuse = (not force_scan) and st.get("candidates") and not _scan_due(st)
        if reuse:
            cand = st["candidates"]
        else:
            # 与 run_scan 一致地计为一次全市场扫描 (scan_count + scan 日志),
            # 否则"周期内嵌选股"命中的买入在日志里扫描次数恒为 0, 误导核查。
            st["scan_count"] = st.get("scan_count", 0) + 1
            cand = pick_candidates(universe=universe, min_conf=min_conf,
                                   strategies=strategies, progress=progress)
            _now = datetime.now()
            st["last_scan_time"] = _now.isoformat()
            st["next_scan_time"] = (
                _now + timedelta(minutes=_SCAN_COOLDOWN_MIN)).isoformat()
            try:
                scanned = len(_mainboard_universe(universe))
                paper_log.log_scan(
                    scan_count=st["scan_count"], codes_scanned=scanned,
                    candidates_found=len(cand), candidates=cand)
            except Exception:
                pass
    st["candidates"] = cand
    # 自动买入条件单: 与候选一同在最终 save_state 落盘 (不被快照覆盖);
    # 非交易时段 (纯快照) 不更新, 避免收盘后挂出"触发价=收盘价*1.002"的条件单。
    if trading:
        _apply_auto_conditions(st, cand, weak=weak)
    # 2) 下单: 仓位未满时取候选填补 (同持上限内), 进 pending 待本周期撮合。
    #    三大硬门槛已由 pick_candidates 在候选入池时 fail-close 判定
    #    (大盘↑+板块>60分位+资金>50分位), 这里直接消费精筛后的候选。
    #    左侧买点特殊: 挂买点入场价, 价格未回踩到位 (last > entry_price) 时
    #    不直接成交, 交给下方 buy_price 条件单 (below) 等回踩。
    if trading:
        # 冻结交割时段模式: cand 在非交易时段被截留为空/复用, 不执行任何撮合。
        for e in cand:
            eff_max = _CUR["weak_max_pos"] if weak else _CUR["max_pos"]
            if len(st["positions"]) >= eff_max:
                break
            code = e["code"]
            if has_position(st, code) or any(o["symbol"] == code for o in st["pending"]):
                continue
            # 弱市停用价值吸筹 (与回测弱市过滤口径一致)
            if weak and e.get("strategy") == STRATEGY_VALUE_ACC:
                continue
            # 开关停用价值吸筹: 存量候选兜底拦截 (防止历史候选重放入场)
            if not _CUR.get("enable_va", True) \
                    and e.get("strategy") == STRATEGY_VALUE_ACC:
                continue
            px = float(e.get("entry_price") or 0) or float(e.get("last", 0) or 0)
            last = float(e.get("last", 0) or 0)
            if e.get("trigger") == "below":
                # 左侧买点: 现价高于买点入场价 → 等回踩 (条件单 below 触发)
                if last <= 0 or last > px:
                    continue
            if px <= 0:
                continue
            # 风控门禁 (此前为死代码, 现接入入场路径):
            #   回撤上限 / 单笔风险预算 / 行业集中度 / 单股集中度 / 资金利用率
            if _risk_blocks_entry(st, e, px):
                continue
            # 直接按候选现价撮合成交, 不再依赖 step 二次拉行情的待撮合;
            # 避免全市场大扫描后行情接口节流导致 pending 悬空、界面永不显示建仓。
            stop_pct = take_pct = None
            if e.get("strategy") == STRATEGY_LONG_LEFT and px > 0:
                stop_px = float(e.get("stop_price") or 0)
                target_px = float(e.get("target_price") or 0)
                if stop_px:
                    stop_pct = round((px - stop_px) / px, 4)
                if target_px > stop_px:
                    take_pct = round((target_px - px) / px, 4)
            order = _make_order(code, e.get("name", ""), e["type"],
                                e.get("conf", 50), px, 0, st["cash"],
                                sector=e.get("sector", ""),
                                strategy=e.get("strategy", ""), st=st,
                                stop_pct=stop_pct, take_pct=take_pct)
            if order is None:
                continue
            order["day"] = str(e.get("day") or "")
            _filled, _msg = fill_buy(st, order)
            if _filled is not None and e.get("trigger", "above") == "above":
                # 直接成交 = 上方 buy_price 自动条件单触发 (below 回踩单由 _check_conditions
                # 撮合并已在那边记录 condition 事件); 这里补一条, 让当日日志"买入/条件单"
                # 联动可查 (此前 run_cycle 内嵌选股直接成交只记 buy, 条件单计数恒为 0)。
                try:
                    paper_log.log_condition_fired(
                        code, e.get("name", ""), "buy_price",
                        e.get("auto_cond_price") or float(e.get("last", 0) or 0),
                        float(e.get("last", 0) or 0), action="买入",
                        reason=f"自动:{e.get('strategy', '')}")
                except Exception:
                    pass
    # 3) 步进+平仓判定 (持仓 + 待撮合用最新行情)
    df_by_code = {}
    codes = {p["symbol"] for p in st["positions"]}
    codes |= {o["symbol"] for o in st["pending"]}
    for code in codes:
        try:
            df_by_code[code] = add_indicators(
                fetch_kline(code, datalen=420, scale=240), symbol=code)
        except Exception:
            pass
    step(st, df_by_code, trading=trading)
    # 4) 周期级等权再平衡: 满仓且现金富余时, 把权重过低的持仓补足到等权目标,
    #    消除资金利用率不足(~66%)与单仓过度集中。
    if trading:
        _rebalance_portfolio(st, df_by_code)
    _day = ""
    for _df in df_by_code.values():
        try:
            _day = str(_df["day"].iloc[-1])
            break
        except Exception:
            continue
    _record_equity(st, _day or None)
    save_state(st)
    try:
        paper_log.log_account_snapshot(
            equity_value=equity(st, {}), cash=st["cash"],
            positions_count=len(st["positions"]),
            closed_count=len(st["closed"]))
    except Exception:
        pass
    return stats(st)


@_paper_locked(fail=PAPER_BUSY_MSG)
def run_scan(st, scan_type='', n_codes=6000, progress=None, anytime=False):
    """运行扫描并更新状态。

    三策略并线扫描 (纪律 + 威科夫左侧买点 + 价值吸筹): 强多头事件 (Spring/
    Shakeout/ST/LPS/SC) + conf≥阈值; 纪律/价值吸筹受大盘门禁, 左侧买点独立观。
    候选写入 st["candidates"] 并落盘 (save_state)。返回描述字符串。

    参数:
        st: 交易状态 dict
        scan_type: 策略过滤。""=三策略并线; 或一个策略 key
                   (STRATEGY_DISCIPLINE/STRATEGY_VALUE_ACC/STRATEGY_LONG_LEFT)
                   单策略模式只产出该策略候选 (旧 "volume_surge"/"pnf_breakout"/
                   "discipline" 等历史值按纪律并线兼容)。
        n_codes: 要扫描的代码数量上限 (pick_candidates 内部会把 universe 收敛为
                 沪深主板 600/601/603/605 + 000/001/002/003, 实际扫描量以主板为准)
        progress: 可选进度回调 (done, total, code), 透传给 pick_candidates
        anytime: True 绕开交易时段门禁 (供手动补跑/测试); 默认 False — 非交易
                 时段直接返回门禁提示, 不执行扫描 (扫描本身不撮合, 但需避免
                 收盘后挂出次日开盘即触发的入场条件单)。

    返回:
        扫描结果字符串描述
    """
    reason = gate_reason(anytime=anytime)
    if reason:
        return f"跳过扫描: {reason}"
    st['scan_count'] = st.get('scan_count', 0) + 1
    now = datetime.now().isoformat()
    st['last_scan_time'] = now

    # 策略过滤: 历史兼容值统一按纪律; 未知值视为并线
    _LEGACY_SINGLE = {"discipline", "volume_surge", "pnf_breakout", "sector_driven"}
    if scan_type in _LEGACY_SINGLE:
        strategies = (STRATEGY_DISCIPLINE,)
    elif scan_type in (STRATEGY_DISCIPLINE, STRATEGY_VALUE_ACC, STRATEGY_LONG_LEFT):
        strategies = (scan_type,)
    else:
        strategies = None
    # 纪律扫描: 强多头事件 + conf≥min_conf + 三大硬门禁
    try:
        scanned_universe = _mainboard_universe(n_codes)
    except Exception:
        scanned_universe = []
    scanned = len(scanned_universe)
    try:
        cand = pick_candidates(universe=scanned_universe or None,
                               max_codes=n_codes, min_conf=_CUR["min_conf"],
                               progress=progress, strategies=strategies)
    except Exception:
        cand = []
    cand.sort(key=lambda e: (-int(e.get("conf", 0) or 0), e.get("code", "")))
    st["candidates"] = cand
    try:
        paper_log.log_scan(
            scan_count=st['scan_count'], codes_scanned=scanned,
            candidates_found=len(cand), candidates=cand)
    except Exception:
        pass

    if scan_type in (STRATEGY_DISCIPLINE, STRATEGY_VALUE_ACC, STRATEGY_LONG_LEFT):
        label = f"策略管理器扫描({paper_strategy_accuracy.STRATEGY_CN.get(scan_type, scan_type)})"
        empty_note = (f"{label}: 无满足条件的候选 (该策略的门禁/conf/事件未满足)")
    else:
        label = "策略管理器扫描(纪律+左侧买点+价值吸筹)"
        empty_note = ("策略管理器扫描: 无满足条件的候选 (纪律强多头事件/conf/大盘/板块/资金流门禁拦截, "
                      "左侧买点未现于可执行窗口, 价值吸筹未现于底部整固)")
    result_str = (f"{label}: 扫描{scanned} 码, 命中 {len(cand)} 个候选"
                  if cand else empty_note)
    st['last_scan_result'] = result_str
    st['next_scan_time'] = (datetime.now() + timedelta(minutes=30)).isoformat()

    # 落盘: 合并到扫描期间可能被并发修改的最新状态 (保留持仓/待撮合/条件单),
    # 并统一生成 buy_price 条件单 (由 _probe 带回 auto_cond_price), 避免快照覆盖。
    with _LOCK:
        fresh = load_state()
        fresh['scan_count'] = st['scan_count']
        fresh['last_scan_time'] = now
        fresh['next_scan_time'] = st['next_scan_time']
        fresh['candidates'] = cand
        fresh['last_scan_result'] = result_str
        _apply_auto_conditions(fresh, cand)
        try:
            save_state(fresh)
        except Exception:
            pass

    return result_str
