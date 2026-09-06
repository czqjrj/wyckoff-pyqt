"""Wyckoff 策略管理器模块

提供 WyckoffStrategyManager 类的导入, 供模拟盘和回测系统使用。
实际实现位于 wyckoff/strategies/manager.py.
"""
from wyckoff.strategies.manager import LONG_EVENT_TYPES, WyckoffStrategyManager

__all__ = ["WyckoffStrategyManager", "LONG_EVENT_TYPES"]
