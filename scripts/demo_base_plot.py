# -*- coding: utf-8 -*-
"""BasePlotWidget 手动体验脚本。

用法:
    python scripts/demo_base_plot.py

窗口左侧为多面板 BasePlotWidget (三栏 X 联动), 右侧为 SimplePlot
(pg.PlotWidget 替换品, 双轴自由缩放)。用于人工核对交互需求:
  - 滚轮      以光标为锚点缩放; 悬停哪个面板缩哪个 (SimplePlot 双轴生效);
              缩放前后光标下的数据点保持在光标下方
  - 键盘      上箭头 / + 放大, 下箭头 / - 缩小 (视图中心锚点);
              左/右箭头平移; Home 复位; Backspace 回退 / F 前进视图历史
  - 左键拖拽  平移 (范围限制在数据全幅内)
  - 双击面板  复位到全幅
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt.QtCore import Qt

from desktop.base_plot import BasePlotWidget, SimplePlot


class DemoChart(BasePlotWidget):
    """两栏联动示例。"""

    def __init__(self):
        super().__init__()
        rng = np.random.default_rng(7)
        n = 500
        x = np.arange(n)
        y = np.cumsum(rng.normal(0, 1, n))
        vol = rng.uniform(0.2, 1.0, n) * 1e6

        self.p_price = pg.PlotItem(title="价格 (滚轮/键盘缩放演示)")
        self.p_price.plot(x, y, pen=pg.mkPen("#e03131", width=1))
        self.p_vol = pg.PlotItem(title="成交量")
        self.p_vol.plot(x, np.abs(vol) / 1e6, pen=None,
                        fillLevel=0, brush="#4dabf7")

        self.ci.addItem(self.p_price, 0, 0)
        self.ci.addItem(self.p_vol, 1, 0)
        self.register_plot(self.p_price, full_x=(0, n - 1), sync=True,
                           primary=True)
        self.set_full_x(self.p_vol, (0, n - 1))
        self.register_plot(self.p_vol, sync=True)
        self._has_data = True
        self.apply_view(0, n - 1, push=False)


def main():
    from PyQt6.QtWidgets import QApplication, QHBoxLayout, QWidget
    app = QApplication.instance() or QApplication([])
    win = QWidget()
    win.setWindowTitle("BasePlotWidget 演示")
    lay = QHBoxLayout(win)
    chart = DemoChart()
    simple = SimplePlot(title="散点 (双轴锚点缩放)")
    rng = np.random.default_rng(3)
    xs, ys = rng.normal(50, 15, 400), rng.normal(30, 8, 400)
    simple.plot(xs, ys, pen=None, symbol="o", symbolSize=4,
                symbolBrush="#7048e8")
    simple.showGrid(x=True, y=True, alpha=0.3)
    simple.resize(420, 300)
    lay.addWidget(chart, 3)
    lay.addWidget(simple, 2)
    win.resize(1200, 560)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
