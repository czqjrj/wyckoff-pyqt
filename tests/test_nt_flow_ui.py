"""国家队资金流向判定窗口冒烟测试: 面板渲染三通道评分/Reasoning/明细表。

不触发网络: 构造窗口后把线程替换为 no-op, 手动喂入合成聚合结果。
回归目标: 窗口可创建、评分条/状态/说明/明细表全部按 res 正确回填,
且断连通道显示"数据断连"占位。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThread  # noqa: E402
from PyQt6.QtWidgets import QApplication, QProgressBar, QTableWidget, QTextEdit  # noqa: E402

from wyckoff.national_team import aggregate_nt  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class _NoopThread(QThread):
    def start(self, *args, **kwargs):
        pass


def _canned():
    etf = [{"verdict": "疑似买入", "y1": 3.0, "y5": 10.0, "y20": 60.0,
            "source": "flow", "code": "510300", "name": "沪深300ETF"}]
    res = aggregate_nt(etf, [], [])
    res["fetched_at"] = "2026-09-17 10:00:00"
    res["etf_rows"] = etf
    return res


def test_nt_flow_window_renders(monkeypatch):
    from ui import extra_windows
    monkeypatch.setattr(extra_windows, "_NationalFlowThread", _NoopThread)

    win = extra_windows.NationalTeamFlowWindow()
    win._on_done(_canned())

    assert "净流入" in win.status.text()
    assert "在并发聚合" not in win.status.text()  # 已不再是加载态
    # 三根评分条按 (score+1)/2 映射到 0~100
    for key, bar in win._bars.items():
        assert isinstance(bar, QProgressBar)
        score = win._bars[key].value()
        assert 0 <= score <= 100
    assert isinstance(win.reason, QTextEdit)
    assert "国家" in win.reason.toPlainText() or "ETF" in win.reason.toPlainText()
    table = win.table
    assert isinstance(table, QTableWidget)
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "510300"
    assert "买入" in table.item(0, 5).text()


def test_nt_flow_window_channel_down_placeholder(monkeypatch):
    from ui import extra_windows
    monkeypatch.setattr(extra_windows, "_NationalFlowThread", _NoopThread)

    win = extra_windows.NationalTeamFlowWindow()
    res = _canned()
    res["levels"]["factor"]["avail"] = False
    res["levels"]["factor"]["score"] = 0.0
    win._on_done(res)

    assert win._bar_labels["factor"].text() == "数据断连"
