"""Wyckoff 策略管理器模块

提供 WyckoffStrategyManager 类的导入, 供模拟盘和回测系统使用。
实际实现位于 wyckoff/strategies/manager.py.
"""
from wyckoff.strategies.manager import WyckoffStrategyManager

# 复用管理器类中的 LONG_EVENT_TYPES 定义
LONG_EVENT_TYPES = ("Spring", "Shakeout", "ST", "LPS", "SC")

__all__ = ["WyckoffStrategyManager", "LONG_EVENT_TYPES"]
