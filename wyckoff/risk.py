"""仓位风险管理: 单笔风险预算 → 建议仓位/股数/盈亏比。

借鉴 WyckoffPro RiskManager 的思路 (单笔风险 ≤ 账户总资产 2%, 最低盈亏比 3:1),
适配 A 股 100 股/手取整。纯规则、无外部依赖。
"""


def calc_position_size(entry, stop, target=None, portfolio_value=0.0,
                       max_risk_pct=0.02, min_rr=3.0):
    """按单笔风险预算计算建议仓位。

    entry: 入场价 (买/卖方向都按"每股风险 = 入场与止损的绝对值差"计);
    stop:  止损价 (多头必须 < 入场, 空头必须 > 入场);
    target: 目标1 (用于盈亏比; 可为 None)。
    返回 dict:
      valid / error          输入是否有效
      risk_per_share         每股风险
      shares                 建议股数 (100 股整数手)
      position_value / position_pct   建议市值与仓位占比
      risk_amount / risk_pct 单笔最大风险金额与占账户比例
      rr_ratio               盈亏比
      meets_rr               是否满足最低盈亏比
    portfolio_value <= 0 时只做盈亏比/合法性校验, 不输出股数建议。
    """
    if entry <= 0 or stop <= 0 or entry == stop:
        return {"valid": False, "error": "入场价/止损价无效"}
    risk_per_share = abs(entry - stop)
    rr = round((target - entry) / risk_per_share, 1) if target and target > entry else 0.0

    out = {"valid": True, "error": "",
           "risk_per_share": round(risk_per_share, 4),
           "rr_ratio": rr, "meets_rr": rr >= min_rr}

    if portfolio_value <= 0:
        return out

    max_risk_amount = portfolio_value * max_risk_pct
    shares = int(max_risk_amount / risk_per_share)
    shares = (shares // 100) * 100
    if shares <= 0:
        shares = 100
    position_value = shares * entry
    out.update({
        "shares": shares,
        "position_value": round(position_value, 2),
        "position_pct": round(position_value / portfolio_value * 100, 1),
        "risk_amount": round(shares * risk_per_share, 2),
        "risk_pct": round(shares * risk_per_share / portfolio_value * 100, 2),
    })
    return out


def position_lines(plan, last_close, portfolio_value=0.0,
                   max_risk_pct=0.02, min_rr=3.0):
    """把交易计划 (build_trade_plan 输出的 dict) 转成仓位建议文本行。

    传入的 plan 只需含 direction / entry / stop / t1 字段, 可为 None。
    """
    if not plan:
        return ["  (无交易计划, 不计算仓位)"]
    direction = plan.get("direction", "")
    entry = plan.get("entry")
    stop = plan.get("stop")
    t1 = plan.get("t1")
    lines = []
    if not entry or not stop:
        return ["  方向: " + (direction or "观望"), "  无有效入场/止损, 不计算仓位"]
    # 方向-价位自洽防御: 多头止损必须 < 入场, 空头止损必须 > 入场。
    # 若计划本身自相矛盾 (止损落在错误一侧), 拒绝计算仓位, 避免误导。
    is_long = "多头" in direction
    is_short = "空头" in direction
    if (is_long and stop >= entry) or (is_short and stop <= entry):
        return ["  方向: " + direction,
                f"  计划自相矛盾: 止损 {stop:.2f} 与方向 ({direction}) 颠倒, 不计算仓位"]
    pos = calc_position_size(entry, stop, t1, portfolio_value,
                             max_risk_pct=max_risk_pct, min_rr=min_rr)
    lines.append(f"  方向: {direction}  现价: {last_close:.2f}")
    lines.append(f"  止损: {stop:.2f} (风险/股 {pos['risk_per_share']:.2f})")
    if t1:
        rr = pos["rr_ratio"]
        lines.append(f"  目标1: {t1:.2f}  盈亏比 {rr:.1f} "
                     f"{'✓满足' if pos['meets_rr'] else '✗不足'}(需≥{min_rr:.0f})")
    if portfolio_value > 0:
        lines.append(f"  单笔风险预算: {portfolio_value * max_risk_pct:.0f}元 "
                     f"(≤{max_risk_pct * 100:.0f}%×{portfolio_value:.0f}元)")
        lines.append(f"  建议仓位: {pos['position_pct']:.1f}% ({pos['shares']}股, "
                     f"市值{pos['position_value']:.0f}元)")
    return lines
