"""模拟盘风控层: 回撤/风险预算/集中度/资金利用率门禁 + 仓位计算 + 组合风险。"""

import wyckoff.paper as paper

from .. import paper_log
from ._params import PositionRisk, SLIP_BUY


# ── 风控与资金管理 ──────────────────────────────────────────
def check_drawdown_limit(st) -> tuple[bool, str]:
    """检查账户回撤是否超过限制。返回 (是否通过, 信息)。"""
    hist = st.get("equity_hist") or []
    if len(hist) < 2:
        return True, ""
    eqs = [h.get("equity", paper._CUR["init_cash"]) for h in hist]
    peak = max(eqs)
    current = eqs[-1]
    dd = (peak - current) / peak if peak > 0 else 0
    if dd >= paper._CUR["max_drawdown"]:
        return False, f"账户回撤 {dd*100:.1f}% 超过限制 {paper._CUR['max_drawdown']*100:.1f}%"
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

    if risk_pct > paper._CUR["max_risk_pct"]:
        return False, f"单笔风险 {risk_pct*100:.1f}% 超过限制 {paper._CUR['max_risk_pct']*100:.1f}%"
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
        if conc > paper._CUR["max_sector_conc"]:
            return False, f"行业 {sector} 集中度 {conc*100:.1f}% 超过限制 {paper._CUR['max_sector_conc']*100:.1f}%"
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
        if conc > paper._CUR["max_single_conc"]:
            return False, f"单股 {symbol} 集中度 {conc*100:.1f}% 超过限制 {paper._CUR['max_single_conc']*100:.1f}%"
    return True, ""


def check_capital_usage(st, required_cash: float) -> tuple[bool, str]:
    """检查资金利用率。返回 (是否通过, 信息)。"""
    equity_val = st["cash"]
    for p in st["positions"]:
        equity_val += p.get("last", p["buy_px"]) * p["qty"]

    usage = 1.0 - (st["cash"] - required_cash) / equity_val if equity_val > 0 else 1.0
    if usage > paper._CUR["max_capital_usage"]:
        return False, f"资金利用率 {usage*100:.1f}% 超过限制 {paper._CUR['max_capital_usage']*100:.1f}%"
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
    va_weight = float(paper._CUR.get("va_weight", 0.6) or 1.0)
    strategy = cand.get("strategy", "")
    budget = (float(st["cash"]) + mv) * (1.0 / max(1, paper._CUR["max_pos"]))
    if strategy == STRATEGY_VALUE_ACC:
        budget *= va_weight
    entry = float(price)
    qty = int(budget // (entry * (1 + SLIP_BUY)) // 100 * 100)
    if qty <= 0:
        return False
    # 止损位: 左侧买点自带御设止损 (直接以真止损算单笔风险); 其余按默认 -stop_loss
    stop = float(cand.get("stop_price") or entry * (1 - paper._CUR["stop_loss"]))
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
    ok, msg = check_capital_usage(st, entry * qty + entry * qty * paper._CUR["cost"])
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
        method = paper._CUR.get("sizing_method", PositionSizingMethod.EQUAL_WEIGHT.value)

    equity_val = st["cash"]
    for p in st["positions"]:
        equity_val += p.get("last", p["buy_px"]) * p["qty"]

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return 0

    max_pos = paper._CUR["max_pos"]
    base_budget = equity_val / max_pos

    if method == PositionSizingMethod.KELLY.value:
        # 基于历史统计计算 Kelly
        s = paper.stats(st)
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
        target_risk = equity_val * paper._CUR["max_risk_pct"]
        budget = target_risk / (risk_per_share / entry_price) if risk_per_share > 0 else base_budget
    elif method == PositionSizingMethod.FIXED_FRACTIONAL.value:
        budget = equity_val * paper._CUR["max_risk_pct"] / (risk_per_share / entry_price) if risk_per_share > 0 else base_budget
    elif method == PositionSizingMethod.CONF_WEIGHTED.value:
        # 置信度加权: conf 90~100 映射到 0.8~1.2 倍
        conf_mult = 0.8 + (conf - 90) / 10 * 0.4
        budget = base_budget * conf_mult
    else:
        budget = base_budget

    budget = min(budget, equity_val * paper._CUR["max_capital_usage"])
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
    pos = paper._find_pos(st, symbol)
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

