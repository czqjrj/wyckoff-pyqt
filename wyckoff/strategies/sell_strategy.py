#!/usr/bin/env python3
"""威科夫卖出策略管理器

A股 多头-only 约束: 严禁空头交易。

基于事件类型的卖出决策:
- Spring/Shakeout/ST/LPS (多头事件): 触发 "event_{event_type}_sell"
- 空头信号 (UTAD/LPSY): 在 A股 做多-only 框架下不触发，
  这些事件已从 LONG_EVENT_TYPES 中移除
"""

# 多头事件类型 (A股 可用)
SPRING = "Spring"
SHAKEOUT = "Shakeout"
ST = "ST"
LPS = "LPS"


def evaluate_sell_reason(event_type: str, reason: str = None) -> str:
    """根据事件类型评估卖出原因 (A股 多头-only)。
    
    规则:
    - Spring/Shakeout/ST/LPS (多头事件): "event_{event_type}_sell"
    - 无事件类型: 返回原 reason (默认止盈/止损/破位)
    """
    if not event_type:
        return reason
    
    if event_type in [SPRING, SHAKEOUT, ST, LPS]:
        # 多头事件卖出 - A股 合规
        return f"event_{event_type}_sell"
    
    # 兜底: 返回原 reason (用于非事件型平仓: 止盈、止损、破位)
    return reason