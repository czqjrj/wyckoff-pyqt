#!/usr/bin/env python3
"""A股市场状态独立 GUI 启动器。

用法:
    python scripts/ashare_outlook_gui.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    from PyQt6.QtWidgets import QApplication

    from ui import theme
    from ui.ashare_outlook_window import AshareOutlookWindow

    app = QApplication(sys.argv)
    app.setApplicationName("A股市场状态")
    theme.set_theme("light")
    app.setStyleSheet(theme.QSS)
    win = AshareOutlookWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
