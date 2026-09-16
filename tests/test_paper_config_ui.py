"""PaperWindow 策略参数配置页冒烟测试。

背景: S5 风控8键配置化曾引入启动即崩 —— fields 元组引用了从未创建的
sp_drawdown/sp_risk, 且 tooltip 字典缺对应键, _build_config_group() 直接抛
AttributeError/KeyError。引擎层全量测试全绿也拦不住 (UI 无测试)。

本测试只构造「策略参数」这一个组, 不触发 PaperWindow.__init__ 的网络/线程重活:
  1) fields 引用的每个控件都会被创建 (AttributeError 回归);
  2) 组内每个交互控件 (spinbox/checkbox/combo) 都有非空 tooltip —— 一条不变量
     同时覆盖「建了控件却没接进 fields」与「接进 fields 却漏 tooltip」两类回归;
  3) 空配置下各控件默认值与引擎常量一致, 防止 UI 兜底值与引擎真源漂移。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QSpinBox,
)

from ui.paper_window import PaperWindow  # noqa: E402

# fields 元组 + 创建代码里出现过的全部交互控件
_ALL_WIDGETS = (
    "sp_maxpos", "sp_conf", "sp_hold", "sp_stop", "sp_tp", "sp_cost",
    "sp_cash", "sp_drawdown", "sp_risk", "sp_comm", "sp_mincomm",
    "sp_stamp", "sp_transfer", "sp_cooldown", "sp_trail_atr",
    "sp_trail_act", "sp_trail_back", "ck_limit_fill", "ck_trailing",
    "ck_weak",
)

# QApplication 必须常驻: 若只在辅助函数内创建并随函数返回被 GC, Qt 会连带
# 销毁所有顶层/孤儿控件 (测试易误报 "C/C++ object ... has been deleted")。
_APP = QApplication.instance() or QApplication([])


def _build_group():
    win = PaperWindow.__new__(PaperWindow)
    win._settings = {}
    return win, win._build_config_group()


def test_config_group_builds_all_widgets():
    win, group = _build_group()
    assert isinstance(group, QGroupBox)
    for attr in _ALL_WIDGETS:
        assert getattr(win, attr, None) is not None, f"缺少控件 {attr}"
    assert isinstance(group.layout(), QGridLayout)


def test_config_group_every_interactive_widget_has_tooltip():
    win, group = _build_group()
    for i in range(group.layout().count()):
        w = group.layout().itemAt(i).widget()
        if isinstance(w, (QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox)):
            assert w.toolTip() != "", (
                f"控件 {w} 没有 tooltip —— 可能被创建但没接进 fields, "
                f"或接进 fields 却漏了 tooltip 字典键")


def test_config_group_defaults_match_engine():
    from wyckoff.paper._params import (
        COST,
        HOLD_BARS,
        INIT_CASH,
        MAX_DRAWDOWN_PCT,
        MAX_POSITIONS,
        MAX_RISK_PCT,
        MIN_CONF,
        STOP_COOLDOWN,
        STOP_LOSS,
        TAKE_PROFIT,
    )

    win, _ = _build_group()
    assert win.sp_maxpos.value() == MAX_POSITIONS
    assert win.sp_conf.value() == MIN_CONF
    assert win.sp_hold.value() == HOLD_BARS
    assert win.sp_stop.value() == pytest.approx(STOP_LOSS, rel=1e-3)
    assert win.sp_tp.value() == pytest.approx(TAKE_PROFIT, rel=1e-3)
    assert win.sp_cost.value() == pytest.approx(COST, rel=1e-3)
    assert win.sp_cash.value() == pytest.approx(INIT_CASH, rel=1e-3)
    assert win.sp_drawdown.value() == pytest.approx(MAX_DRAWDOWN_PCT, rel=1e-3)
    assert win.sp_risk.value() == pytest.approx(MAX_RISK_PCT, rel=1e-3)
    assert win.sp_cooldown.value() == STOP_COOLDOWN
