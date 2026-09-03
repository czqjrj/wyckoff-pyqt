#!/usr/bin/env python3
"""威科夫卖出策略管理器

基于事件类型的卖出决策:
- UTAD/LPSY (空头信号): 触发 "空头信号卖出_{event_type}"
- Spring/Shakeout/ST/LPS (多头事件): 触发 "event_{event_type}_sell"
"""

# 事件类型定义 (与 wyckoff/paper.py 保持一致)
SPRING = "Spring"
SHAKEOUT = "Shakeout"
ST = "ST"
LPS = "LPS"
UTAD = "UTAD"
LPSY = "LPSY"


def evaluate_sell_reason(event_type: str, reason: str = None) -> str:
    """根据事件类型评估卖出原因。
    
    规则:
    - UTAD/LPSY (event_dir=-1, 空头信号): "空头信号卖出_{event_type}"
    - Spring/Shakeout/ST/LPS (多头事件): "event_{event_type}_sell"
    - 无事件类型: 返回原 reason
    """
    if not event_type:
        return reason
    
    if event_type in [UTAD, LPSY]:
        # 空头信号卖出
        return f"空头信号卖出_{event_type}"
    
    if event_type in [SPRING, SHAKEOUT, ST, LPS]:
        # 多头事件卖出
        return f"event_{event_type}_sell"
    
    # 兜底: 返回原 reason
    return reason