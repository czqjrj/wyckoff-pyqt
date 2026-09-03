#!/usr/bin/env python3
"""威科夫卖出策略管理器

A股 多头主导框架:
- 开仓: 只做多
- 卖出策略:
  - 空头信号 (UTAD/LPSY): 风险信号，触发平仓多头仓位
  - 多头事件 (Spring/Shakeout/ST/LPS): 不触发平仓，视为加仓或持有机会
"""

# 多头事件类型 (A股 可买入)
SPRING = "Spring"
SHAKEOUT = "Shakeout"
ST = "ST"
LPS = "LPS"
# 空头信号类型 (A股 做多风险管理用)
UTAD = "UTAD"
LPSY = "LPSY"


def evaluate_sell_reason(event_type: str, reason: str = None) -> str:
    """根据事件类型评估卖出原因 (A股 多头主导)。
    
    返回值含义:
    - "空头信号卖出": 触发平仓多头仓位 (针对 UTAD/LPSY)
    - "多头事件加仓信号": 不平仓，视为加仓或持有机会 (针对 Spring/Shakeout/ST/LPS)
    - None: 无事件类型或传统平仓理由 (如止盈、止损)
    - 原 reason: 保留原有理由 (如 "止盈", "止损")
    """
    if not event_type:
        return reason
    
    # 情况一：空头信号 - A股 做多风险管理，触发平仓
    if event_type in [UTAD, LPSY]:
        return "空头信号卖出"
    
    # 情况二：多头事件 - A股 多头主导，不平仓，甚至可以加仓
    if event_type in [SPRING, SHAKEOUT, ST, LPS]:
        return "多头事件加仓信号"
    
    # 情况三：无事件类型，返回原有理由 (止盈/止损/破位)
    return reason