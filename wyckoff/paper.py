"""模拟盘 (Paper Trading) 核心引擎。

自动筛选 → 自动下单 → 自动卖出 → 收益统计 全自动闭环, 无真实资金风险。

设计要点 (对齐项目已验证的实证参数, 见 docs/profitability_bt.md):
  - 选股: 全市场 universe 逐股 detect_all, 只取强多头事件 (Spring/Shakeout/
    ST/LPS/SC/SOW_INVALID) 且 conf 达标 (默认 ≥90) 的标的, 按 conf 排序。
  - 撮合: 候选按 conf 填充, 同持上限内用"最近收盘价 + 滑点"买入
    (无未来函数: 同一周期内新成交仓位不参与当期卖出评估)。
  - 卖出触发: 持有满 HOLD_BARS 根到期 / 结构位-5%止损 / 破位 / +15%止盈。
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

from .paths import PAPER_FILE
from .settings_keys import S

_LOCK = threading.RLock()

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
# 止损: 结构位 ± 0.5×ATR, 简化用固定百分比 (回测实证 -5% 盈亏比最优)
STOP_LOSS = 0.05
# 止盈: 盈利 +15% 落袋 (结合止损的不对称盈亏比)
TAKE_PROFIT = 0.15
# 初始终端资金 (模拟资产)
INIT_CASH = 1_000_000.0
# 单笔最低可交易金额 (避免碎股/零股本)
MIN_LOT = 100.0
# conf 过滤下限: 只做实证收益显著为正的强信号 (参见 docs/profitability_bt.md)
MIN_CONF = 90

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
}

# 强多头事件: 方向命中显著优于随机且可裸多落地 (与 docs/profitability_bt.md 一致)
try:
    from .config import STRONG_TIER_TYPES
    # SC/SOW_INVALID 方向为中性 (event_dir==0) 但属底部反转/空头失效, 一并纳入
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "Shakeout", "ST", "LPS", "SC", "SOW_INVALID"}
    ) & frozenset(STRONG_TIER_TYPES)
except Exception:  # pragma: no cover - 防御首启缺失
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "Shakeout", "ST", "LPS", "SC", "SOW_INVALID"})


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
    """检查行业集中度。返回 (是否通过, 信息)。"""
    if not sector:
        return True, ""

    total_mv = new_mv
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
    """检查单股集中度。返回 (是否通过, 信息)。"""
    total_mv = new_mv
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
    ok, msg = check_drawdown_limit(st)
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
        return True
    # 预估 qty (与 _make_order 同口径)
    budget = st["cash"] * (1.0 / max(1, _CUR["max_pos"]))
    entry = float(price)
    qty = int(budget // (entry * (1 + SLIP_BUY)) // 100 * 100)
    if qty <= 0:
        return False
    stop = entry * (1 - _CUR["stop_loss"])
    ok, msg = check_risk_budget(st, cand["code"], entry, stop, qty)
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
        return True
    sector = cand.get("sector", "")
    if sector:
        new_mv = entry * qty
        ok, msg = check_sector_concentration(st, cand["code"], sector, new_mv, {})
        if not ok:
            st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
            return True
        ok, msg = check_single_concentration(st, cand["code"], new_mv, {})
        if not ok:
            st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
            return True
    ok, msg = check_capital_usage(st, entry * qty + entry * qty * _CUR["cost"])
    if not ok:
        st.setdefault("meta", {})["last_risk_skip"] = {"code": cand["code"], "reason": msg}
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
    """基于策略管理器的「价值吸筹」信号构造候选。

    命中条件 (与回测口径一致, 无 conf 门槛): 阶段=底部整固 且 近20根内出现
    {Spring,Shakeout,SC,ST,LPS}。返回 {"strategy","type","idx","conf"} 或 None。
    管理器不可用 / 评估异常时返回 None (纪律候选照常工作)。
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
    return {"strategy": "screener_value_accumulation",
            "type": ev["type"], "idx": int(ev.get("idx") or 0),
            "conf": int(ev.get("conf", 0) or 0)}


def pick_candidates(universe=None, max_codes=60, min_conf=None,
                    cancel_event=None, skip_gates=False):
    """扫描 universe 中触发强多头事件的高 conf 标的, 返回候选单 (降序 conf)。

    每只股票只留"最近 N 根内最新"的强多头事件; conf 由 event 的 conf 字段
    (启发式 + online model 校准 + 类型封顶后) 提供。universe 为空用全市场。
    min_conf 缺省取当前生效参数 (apply_paper_params 设置或模块默认 90)。

    纪律硬门禁 (缺一不可, 数据不可用即拦截; skip_gates=True 跳过全部门禁,
    供离线/测试/仅排序场景使用):
      - 大盘20日线向上 (fetch_market_env)
      - 板块强度 > 60 分位 (sector_strength_pct)
      - 资金流近5日主力净流入 > 候选池截面中位 (跨市场截面分位, fetch_main_flow)

    新增: 遇到强多头事件时自动添加价格买入条件单 (buy_price),
    触发价设为最近收盘价，触发条件为 "≥ 达上破" (above)。
    条件单将被写入状态, 供后续 _check_conditions 轮询触发。
    """
    if min_conf is None:
        min_conf = _CUR["min_conf"]
    from .datasource import fetch_kline
    from .events import detect_all
    from .fundamental import fetch_market_universe, fetch_sector
    from .indicators import add_indicators, find_pivots
    from .utils import normalize_symbol

    if universe is None:
        try:
            universe = fetch_market_universe(100) or []
        except Exception:
            universe = []
    universe = [normalize_symbol(c) for c in universe]
    # 纪律门禁 ①: 大盘20日线向上 (全市场统一, 一次判定; fail-close)
    if not skip_gates:
        market_ok, _market_reason = _market_trend_ok()
        if not market_ok:
            return []

    def _probe(code):
        """单只股票的重活: K线+指标+事件+板块+资金流 → 候选 (含 flow 值) 或 None。
        Gate ③ (资金流截面分位) 是跨市场判定, 故 flow 先随候选带回, 由调用方统一过滤。
        返回 (code, candidate_or_None, flow_or_None)。"""
        if cancel_event is not None and cancel_event.is_set():
            return code, None, None
        try:
            df = add_indicators(fetch_kline(code, datalen=400, scale=240),
                                symbol=code)
            if df is None or len(df) < 200:
                return code, None, None
            piv = find_pivots(df, order=6)
            evs = detect_all(df, piv)
            if not evs:
                return code, None, None
            # 纪律信号: 强多头事件 conf≥阈值 (策略管理器·策略4口径, 近10根)
            latest = None
            for e in evs:
                if e["type"] not in LONG_EVENT_TYPES:
                    continue
                if (e.get("idx") or 0) < len(df) - 10:
                    continue
                conf = int(e.get("conf", 0) or 0)
                if conf < min_conf:
                    continue
                if latest is None or (e.get("idx") or 0) > latest["idx"]:
                    latest = e
            strategy = "paper_discipline_bull"
            if latest is None:
                # 回退: 策略管理器·价值吸筹 (底部整固 + 20根内吸筹事件, 无 conf 门槛)
                va = _value_accum_candidate(code, df, piv, evs)
                if va is None:
                    return code, None, None
                latest = {"type": va["type"], "idx": va["idx"],
                          "conf": va["conf"]}
                strategy = va["strategy"]
            base_conf = int(latest.get("conf", 0) or 0)
            latest["code"] = code
            latest["name"] = _stock_name(code)
            latest["last"] = round(float(df["close"].iloc[-1]), 2)
            latest["strategy"] = strategy
            latest["sector"] = ""
            try:
                latest["sector"] = fetch_sector(code) or ""
            except Exception:
                pass
            flow = None
            # 纪律门禁 ②: 板块强度 > 60 分位 (fail-close)
            if not skip_gates:
                s_ok, _s_reason = _sector_strength_ok(latest["sector"])
                if not s_ok:
                    return code, None, None
                # 纪律门禁 ③: 收集主力净流入供截面分位判定 (稍后统一过滤)
                flow = _flow_net5(code)
            # A: 产业链加分/门禁 — 不改变 base_conf 的入池过滤, 只影响排序优先级;
            #    数据不可用/无板块 (离线) 时 adj=0 完全退化为原行为。
            try:
                if latest["sector"]:
                    from .chain import chain_conf_adjust
                    adj = chain_conf_adjust(latest["sector"], base_conf)
                    latest["base_conf"] = base_conf
                    latest["conf"] = max(min_conf, min(100, base_conf + adj))
                    latest["chain_adj"] = adj
            except Exception:
                pass

            # 纪律门禁已在此前判定; 生成自动买入条件单 (触发价=现价+0.2% 上破)
            try:
                cond_price = round(float(latest["last"]) * 1.002, 3)
                st = load_state()
                add_condition(st, kind="buy_price",
                              symbol=code, price=cond_price,
                              trigger="above",
                              reason=f"自动:{latest.get('strategy','')}:{latest['type']}({latest.get('conf',0)})")
            except Exception:
                pass
            return code, latest, flow
        except Exception:
            return code, None, None

    out = []
    _flow_map = {}  # code -> 近5日主力净流入
    _codes = universe[:max_codes]
    try:
        from ._shared import parallel_map
        for code, cand, flow in parallel_map(_codes, _probe, workers=6):
            if cand is not None:
                out.append(cand)
                _flow_map[code] = flow
    except Exception:
        for code in _codes:
            _code, cand, flow = _probe(code)
            if cand is not None:
                out.append(cand)
                _flow_map[_code] = flow
    # 纪律门禁 ③: 资金流"净流入>50 分位" —— 跨市场截面判定。
    # 只对拿到净流入数据的候选计算中位; 净流入缺失或低于中位 → 拦截。
    if not skip_gates:
        flows = [f for f in _flow_map.values() if f is not None]
        if flows:
            med = sorted(flows)[len(flows) // 2]
            out = [e for e in out
                   if _flow_map.get(e["code"]) is not None
                   and _flow_map[e["code"]] >= med]
        else:
            out = []  # 无任何资金流数据 → 纪律不满足, fail-close
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
                strategy=""):
    """构造买单订单公共字段 (qty 按当前现金预算分配, 整手)。

    sector 为持仓行业 (用于行业集中度风控 check_sector_concentration);
    strategy 记录信号来源策略 (策略管理器: paper_discipline_bull/价值吸筹)。
    """
    budget = cash * (1.0 / max(1, _CUR["max_pos"]))
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
        if len(st["positions"]) >= _CUR["max_pos"]:
            return None, "同持已满"
        order = _make_order(code, name, type_, conf, price, n_total, st["cash"],
                            strategy=strategy)
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
                        sector=sector, strategy=strategy)
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
          name="", reason="", amount=None):
    """构造一条条件单记录 (status="active")。"""
    c = {
        "cid": f"cond-{int(time.time() * 1_000_000)}",
        "kind": kind, "symbol": symbol, "name": name,
        "price": round(float(price), 3) if price is not None else None,
        "pct": float(pct) if pct is not None else None,
        "trigger": trigger, "qty": int(qty or 0), "amount": amount,
        "reason": reason,
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


def place_condition(kind, symbol, price=None, pct=None, trigger="above",
                    qty=0, name="", reason=""):
    """独立入口: 加载状态并添加条件单。返回 (cond, msg)。"""
    st = load_state()
    with _LOCK:
        return add_condition(st, kind, symbol, price=price, pct=pct,
                             trigger=trigger, qty=qty, name=name,
                             reason=reason, save=True)


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
                _fire_condition(st, c, last, df, side="buy")
                triggered += 1
        elif kind == "sell_price":
            if _match_trigger(c["trigger"], last, c["price"]):
                _fire_condition(st, c, last, df, side="sell")
                triggered += 1
        elif kind in ("take_profit", "stop_loss"):
            pos = _find_pos(st, sym)
            if pos is None:
                continue  # 无持仓的止盈止损无意义, 保留等待
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
            peak = c.get("peak") or max(last, float(pos.get("last", pos["buy_px"])))
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
        if peak is not None and pct is not None:
            # 触发时: 当前价是否已从峰值回撤 pct
            return last <= peak * (1 - pct)
        return None

    return None


def _fire_condition(st, c, last, df, side="buy", pos=None):
    """执行触发动作后把条件单标记为已触发 (status → "done")。"""
    # 首先判断正确性
    entry_price = None
    condition_ret = None
    condition_peak = None

    if side == "buy":
        budget = c.get("amount") or st["cash"]
        order = _make_order(c["symbol"], c.get("name", ""), "条件单",
                            c.get("qty", 0) or 0, last, 0, budget)
        if order is None or len(st["positions"]) >= _CUR["max_pos"]:
            # 预算不足/同持已满: 条件单转取消, 防止永久悬挂
            c["status"] = "cancelled"
            c["note"] = "资金/同持上限不足, 未成交"
            c["correct"] = None
        else:
            fill_buy(st, order)
            c["matched_price"] = order["price"]
            c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            c["status"] = "done"
            # 买入条件单: 判断触发价是否被满足
            # buy_price: 我们在 pick_candidates 中设置 price = last * 1.002 (略高于当前价)
            # 触发意味着 last >= price，所以 correct=True
            c["correct"] = True  # buy_price 已触发突破
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

        close_position(st, pos, sell_price, f"条件单:{c['kind']}")
        c["matched_price"] = sell_price
        c["matched_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        c["status"] = "done"

        # 判断卖出条件单是否正确
        c["correct"] = _judge_condition_correct(
            c, last, entry=entry_price, ret=condition_ret,
            peak=condition_peak, side="sell",
            cond_price=c.get("price"), pct=c.get("pct")
        )


def fill_buy(st, order):
    """口头成交: 扣现金、建仓。"""
    price = order["price"]
    qty = order["qty"]
    cost = qty * price * _CUR["cost"]
    spend = qty * price + cost
    st["cash"] -= spend
    st["positions"].append({
        "symbol": order["symbol"], "name": order.get("name", ""),
        "type": order["type"], "conf": order.get("conf", 50),
        "qty": qty, "buy_px": price, "cost": round(cost, 2),
        "entry_ts": order["ts"], "entry_bars": order.get("bars", 0),
        "sector": order.get("sector", ""),
        "strategy": order.get("strategy", ""),
        "staged": False,
    })
    st["orders"].append(order)
    save_state(st)
    return order, "成交"


def step(st, df_by_code):
    """推进一个周期: 用当前行情更新最新价 → 检查卖单条件 + 成交 in-arrow。

    df_by_code: {symbol: df(含 indicators)} 当前最新行情窗口 (由 UI/调度器提供)。
    不可用 (无行情) 时跳过。
    """
    # 先检查条件单: 用户自定义的价格触发/止盈/止损/追踪优先于默认止盈止损。
    _check_conditions(st, df_by_code)
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        if df is None or len(df) == 0:
            continue
        last = float(df["close"].iloc[-1])
        entry = float(pos["buy_px"])
        ret = last / entry - 1
        pos["last"] = round(last, 3)
        pos["last_ret"] = round(ret, 4)
        # 结构位: 用最近 10 根低点做动态支撑 (近似结构关键位)
        support = float(df["low"].iloc[-10:].min())
        stop_px = entry * (1 - _CUR["stop_loss"])
        # 卖出判定 (任一触发); 每周期推进持仓 K 数 (run_cycle 末尾落盘)
        pos["entry_bars"] = int(pos.get("entry_bars", 0)) + 1
        reason = None
        held = int(pos["entry_bars"])
        if ret >= _CUR["take_profit"]:
            reason = "止盈"
        elif last <= stop_px:
            reason = "止损"
        elif support < stop_px and last <= support:
            reason = "破位"
        elif held >= _CUR["hold_bars"]:
            reason = "到期"
        if reason:
            sell_price = max(stop_px, last) * (1 - SLIP_SELL)
            close_position(st, pos, sell_price, reason)
    # 处理待撮合买单
    for o in list(st["pending"]):
        df = df_by_code.get(o["symbol"])
        if df is not None and len(df):
            close = float(df["close"].iloc[-1])
            o["price"] = round(close * (1 + SLIP_BUY), 3)
            if not o.get("date"):
                o["date"] = str(df["day"].iloc[-1])
            order = dict(o)
            st["pending"].remove(o)
            fill_buy(st, order)


def close_position(st, pos, sell_price, reason):
    """平仓: 回收现金、记录已平仓与净值。"""
    price = round(sell_price, 3)
    gross = price * pos["qty"]  # 不含卖出成本的口径内部用
    fee = gross * _CUR["cost"]
    proceeds = gross - fee
    st["cash"] += proceeds
    ret_total = (price / pos["buy_px"] - 1)
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
    st["equity_hist"].append({
        "ts": time.strftime("%Y-%m-%d"),
        "cash": round(st["cash"], 2),
        "equity": round(equity(st, {}), 2),
    })
    save_state(st)


def equity(st, df_by_code):
    """总资产 = 现金 + 持仓市值。"""
    mv = 0.0
    for pos in st["positions"]:
        df = df_by_code.get(pos["symbol"])
        px = float(df["close"].iloc[-1]) if df is not None and len(df) else pos.get("last", pos["buy_px"])
        mv += px * pos["qty"]
    return st["cash"] + mv


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

    # 总收益率: 当前总资产 (现金+持仓市值) 相对初始模拟资金
    hist = st.get("equity_hist") or []
    init = float(_CUR["init_cash"])
    if hist:
        eqs = [h.get("equity", init) for h in hist]
        last_hist = eqs[-1]
        out["total_return"] = round(last_hist / init - 1, 4)

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
    L.append(f"- 累计收益: **{s['total_return']*100:+.2f}%**  "
             f"最大回撤: {s['max_drawdown']*100:.2f}% if s['max_drawdown'] is not None else '-'")
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
    L.append("")
    return "\n".join(L)


def run_cycle(settings=None, min_conf=None, universe=None):
    """无头自动运行一个周期: 筛选→下单→步进→统计。返回统计。

    供 cron / 调度线程 / 手动触发。每周期持仓 K 数 +1,
    到期/止盈/止损/破位在该周期内平仓。
    settings 传入界面设置 dict (S.Paper.* 键) 覆盖策略参数; min_conf 显式传入
    时优先于 settings (兼容旧调用方)。
    """
    from .datasource import fetch_kline
    from .indicators import add_indicators

    apply_paper_params(settings)
    if min_conf is None:
        min_conf = _CUR["min_conf"]

    st = load_state()
    # 1) 选股
    cand = pick_candidates(universe=universe, min_conf=min_conf)
    st["candidates"] = cand
    # 2) 下单: 仓位未满时取候选填补 (同持上限内), 进 pending 待本周期撮合。
    #    三大硬门槛已由 pick_candidates 在候选入池时 fail-close 判定
    #    (大盘↑+板块>60分位+资金>50分位), 这里直接消费精筛后的候选。
    for e in cand:
        if len(st["positions"]) >= _CUR["max_pos"]:
            break
        code = e["code"]
        if has_position(st, code) or any(o["symbol"] == code for o in st["pending"]):
            continue
        px = float(e.get("last", 0) or 0)
        if px <= 0:
            continue
        # 风控门禁 (此前为死代码, 现接入入场路径):
        #   回撤上限 / 单笔风险预算 / 行业集中度 / 单股集中度 / 资金利用率
        if _risk_blocks_entry(st, e, px):
            continue
        _enqueue_buy(st, code, e.get("name", ""), e["type"],
                     e.get("conf", 50), px, sector=e.get("sector", ""),
                     strategy=e.get("strategy", ""))
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
    step(st, df_by_code)
    save_state(st)
    return stats(st)


def run_scan(st, scan_type='discipline', n_codes=20):
    """运行纪律扫描并更新状态。

    按纪律执行: 强多头事件 (Spring/Shakeout/ST/LPS/SC) + conf≥阈值 + 三大硬门禁
    (大盘20日线向上 / 板块强度>60分位 / 资金流净流入>50分位截面)。候选写入
    st["candidates"] 并落盘 (save_state)。返回描述字符串。

    参数:
        st: 交易状态 dict
        scan_type: 保留兼容 (旧 "volume_surge"/"pnf_breakout"/"sector_driven"
                   在纪律下统一走 pick_candidates, 忽略具体类型)
        n_codes: 要扫描的代码数量 (universe 截取)

    返回:
        扫描结果字符串描述
    """
    st['scan_count'] = st.get('scan_count', 0) + 1
    now = datetime.now().isoformat()
    st['last_scan_time'] = now

    # 纪律扫描: 强多头事件 + conf≥min_conf + 三大硬门禁
    try:
        cand = pick_candidates(max_codes=n_codes, min_conf=_CUR["min_conf"])
    except Exception:
        cand = []
    cand.sort(key=lambda e: (-int(e.get("conf", 0) or 0), e.get("code", "")))
    st["candidates"] = cand

    label = "策略管理器扫描(纪律+价值吸筹)"
    result_str = (f"{label}扫描: 扫描{n_codes} 码, 命中 {len(cand)} 个候选"
                  if cand else
                  "纪律扫描: 无满足条件的候选 (强多头事件/conf/大盘/板块/资金流门禁)")
    st['last_scan_result'] = result_str
    st['next_scan_time'] = (datetime.now() + timedelta(minutes=30)).isoformat()

    # 落盘 (避免 UI 读回旧状态)
    try:
        save_state(st)
    except Exception:
        pass

    return result_str
