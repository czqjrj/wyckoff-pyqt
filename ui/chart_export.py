"""图表导出: 统一 PNG 导出逻辑, 从 MainWindow 提取。"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QMessageBox

from ui import theme
from wyckoff.paths import DATA_DIR

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class ChartExporter:
    """图表导出器: 统一 K线/P&F/指标/资金流 的 PNG 导出。

    用法:
        exporter = ChartExporter(main_window)
        exporter.export_current()  # 导出当前 Tab
        exporter.export_all()      # 导出所有图表
    """

    def __init__(self, main_window: MainWindow) -> None:
        self._mw = main_window

    def _get_widget(self, tab_idx: int):
        """根据 Tab 索引获取对应的图表 Widget。"""
        widgets = {
            0: self._mw.kline_widget,
            1: self._mw.pnf_widget,
            2: self._mw.ind_widget,
            3: self._mw.mkt_widget,
        }
        return widgets.get(tab_idx)

    def _stamp_pixmap(self, pm, title: str = ""):
        """给导出的图表加时间戳水印 (左下角 时间 + 股票)。"""
        from datetime import datetime

        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor, QFont, QPainter

        painter = QPainter(pm)
        font = QFont(self._mw.font())
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(QColor(120, 130, 150, 200))
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{title}  {ts}" if title else ts
        painter.drawText(QPointF(6, pm.height() - 8), text)
        painter.end()
        return pm

    def save_png(self, widget, base: str, quiet: bool = False) -> str:
        """保存单个图表 Widget 为 PNG。"""

        pm = widget.grab_pixmap()
        code = self._mw._current_code or ""
        name = self._mw._current_name or code
        self._stamp_pixmap(pm, title=name)
        path = os.path.join(DATA_DIR, f"{base}.png")
        pm.save(path)
        if not quiet:
            QMessageBox.information(self._mw, "导出图表", f"已保存:\n{path}")
        else:
            self._mw._status(f"已保存 {path}", theme.C_DOWN)
        return path

    def export_current(self) -> None:
        """导出当前 Tab 的图表。"""
        idx = self._mw.tabs.currentIndex()
        widget = self._get_widget(idx)
        if widget is None:
            self._mw._status("当前 Tab 不支持导出", theme.C_AMBER)
            return
        code = self._mw._current_code or "chart"
        self.save_png(widget, f"wyckoff_{code}")

    def export_kline(self, quiet: bool = False) -> str:
        """导出 K线图。"""
        return self.save_png(self._mw.kline_widget,
                            f"wyckoff_{self._mw._current_code or 'chart'}_kline",
                            quiet=quiet)

    def export_pnf(self, quiet: bool = False) -> str:
        """导出 P&F 图。"""
        return self.save_png(self._mw.pnf_widget,
                            f"wyckoff_{self._mw._current_code or 'chart'}_pnf",
                            quiet=quiet)

    def export_ind(self, quiet: bool = False) -> str:
        """导出技术指标图。"""
        return self.save_png(self._mw.ind_widget,
                            f"wyckoff_{self._mw._current_code or 'chart'}_ind",
                            quiet=quiet)

    def export_mkt(self, quiet: bool = False) -> str:
        """导出资金流图。"""
        return self.save_png(self._mw.mkt_widget,
                            f"wyckoff_{self._mw._current_code or 'chart'}_mkt",
                            quiet=quiet)

    def export_all(self) -> None:
        """导出所有图表。"""
        n = 0
        self.export_kline(quiet=True)
        n += 1
        self.export_ind(quiet=True)
        n += 1
        self.export_mkt(quiet=True)
        n += 1
        self.export_pnf(quiet=True)
        n += 1
        QMessageBox.information(self._mw, "导出图表", f"已导出 {n} 张图表到数据目录。")
