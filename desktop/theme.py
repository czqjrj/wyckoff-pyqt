# -*- coding: utf-8 -*-
"""UI 主题: 颜色 / 字体 / 全局样式表。复用 wyckoff.config 的调色板, 保证图表与界面一致。

支持浅色 (light) 与深色护眼 (dark) 两套主题, 通过 set_theme() 在运行时切换:
切换会重绑本模块 C_* 常量并重建 QSS, 图表控件均通过 theme.C_* 运行时取色, 因此
只需要调用方重新渲染 (set_data) 即可整体换肤。"""
from PyQt6.QtGui import QColor, QFont

from wyckoff.config import THEMES, FONT_CANDIDATES, FONT_DISPLAY_CANDIDATES, MONO_FONT

C = THEMES["light"]
_active = "light"

C_UP = C["up"]
C_DOWN = C["down"]
C_AMBER = C["amber"]
C_MUTED = C["muted"]
C_TEXT = C["text"]
C_BG = C["bg"]
C_PANEL = C["panel"]
C_BORDER = C["border"]
C_ACCENT = C["accent"]
C_GRID = C["grid"]

C_ZONE_ACC = C["zone_acc"]
C_ZONE_DIST = C["zone_dist"]
C_ZONE_NEUT = C["zone_neut"]

TONE_COLOR = {
    "bullish": C_UP,
    "bearish": C_DOWN,
    "neutral": C_MUTED,
    "caution": C_AMBER,
}


def active_theme():
    return _active


def set_theme(name):
    """切换主题 (light/dark), 重绑颜色常量并重建 QSS。"""
    global _active, C, C_UP, C_DOWN, C_AMBER, C_MUTED, C_TEXT, C_BG, C_PANEL, \
        C_BORDER, C_ACCENT, C_GRID, C_ZONE_ACC, C_ZONE_DIST, C_ZONE_NEUT, \
        TONE_COLOR, QSS
    pal = THEMES.get(name)
    if pal is None:
        name = "light"
        pal = THEMES["light"]
    _active = name
    C = pal
    C_UP = pal["up"]
    C_DOWN = pal["down"]
    C_AMBER = pal["amber"]
    C_MUTED = pal["muted"]
    C_TEXT = pal["text"]
    C_BG = pal["bg"]
    C_PANEL = pal["panel"]
    C_BORDER = pal["border"]
    C_ACCENT = pal["accent"]
    C_GRID = pal["grid"]
    C_ZONE_ACC = pal["zone_acc"]
    C_ZONE_DIST = pal["zone_dist"]
    C_ZONE_NEUT = pal["zone_neut"]
    TONE_COLOR = {
        "bullish": C_UP,
        "bearish": C_DOWN,
        "neutral": C_MUTED,
        "caution": C_AMBER,
    }
    QSS = _build_qss(C)


def pick_font_family(preferred=""):
    """在系统可用字体中挑选 FONT_CANDIDATES 里第一个存在的 (参考 config._pick_font)。"""
    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        # 模块导入期 (QSS = _build_qss(C)) 尚无 QApplication, QFontDatabase 不可用;
        # 先回退静态候选, set_theme 在 MainWindow 构造时 (QApplication 已存在) 会重建。
        return FONT_CANDIDATES[0] if FONT_CANDIDATES else (preferred or "sans-serif")
    families = set(QFontDatabase.families())
    for f in FONT_CANDIDATES:
        if f in families:
            return f
    return preferred or "sans-serif"


def pick_display_font_family(preferred=""):
    """在系统可用字体中挑选衬线显示字体候选里第一个存在的。"""
    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        return FONT_DISPLAY_CANDIDATES[0] if FONT_DISPLAY_CANDIDATES else (preferred or "serif")
    families = set(QFontDatabase.families())
    for f in FONT_DISPLAY_CANDIDATES:
        if f in families:
            return f
    return preferred or "serif"


def pick_mono_font_family(preferred=""):
    """在系统可用字体中挑选等宽数字字体 (价格/百分比等 tabular 数字)。"""
    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        return MONO_FONT or (preferred or "monospace")
    families = set(QFontDatabase.families())

    def _has(name):
        return name in families
    for f in (MONO_FONT, "Noto Sans Mono CJK SC", "Source Han Mono SC",
              "Sarasa Mono SC", "DejaVu Sans Mono", "Consolas", "Menlo"):
        if _has(f):
            return f
    return preferred or "monospace"


def app_font(size=10, bold=False):
    f = QFont()
    f.setFamily(FONT_CANDIDATES[0] if FONT_CANDIDATES else "sans-serif")
    f.setPointSize(int(size))
    f.setBold(bold)
    return f


def ui_font(size=10, bold=False):
    return app_font(size, bold)


def display_font(size=10, bold=False):
    """衬线显示字体: 用于品牌标题/面板标题, 建立与正文的层级。"""
    f = QFont()
    f.setFamily(FONT_DISPLAY_CANDIDATES[0] if FONT_DISPLAY_CANDIDATES else "serif")
    f.setPointSize(int(size))
    f.setBold(bold)
    return f


def mono_font(size=10, bold=False):
    """等宽 tabular 字体: 用于价格/百分比/数据, 数字对齐便于扫读。"""
    f = QFont()
    f.setFamily(MONO_FONT or "monospace")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(int(size))
    f.setBold(bold)
    return f


def color(hexstr, alpha=None):
    c = QColor(hexstr)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def css_rgba(hexstr, alpha=255):
    c = QColor(hexstr)
    c.setAlpha(alpha)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def _display_family():
    try:
        return pick_display_font_family()
    except Exception:
        return FONT_DISPLAY_CANDIDATES[0] if FONT_DISPLAY_CANDIDATES else "serif"


def _mono_family():
    try:
        return pick_mono_font_family()
    except Exception:
        return MONO_FONT or "monospace"


def _build_qss(C):
    _df = _display_family()
    _mf = _mono_family()
    return f"""
* {{
    font-family: "{FONT_CANDIDATES[0]}";
    font-size: 12pt;
}}
QMainWindow, QDialog {{
    background: {C_BG};
}}
QWidget {{
    color: {C_TEXT};
}}
QToolBar {{
    background: {C_PANEL};
    border-bottom: 1px solid {C_BORDER};
    padding: 6px;
    spacing: 10px;
}}
QWidget#topBar {{
    background: {C_PANEL};
    border-bottom: 1px solid {C_BORDER};
}}
QFrame#vsep {{
    color: {C_BORDER};
}}
QLabel#brandTitle {{
    color: {C_ACCENT};
    font-family: "{_df}";
    font-weight: bold;
    font-size: 17pt;
    padding-right: 2px;
}}
QLabel#tbName {{
    font-weight: bold;
    font-size: 12pt;
    color: {C_TEXT};
}}
QLabel#tbCode {{
    color: {C_MUTED};
    font-family: "{_mf}";
}}
QLabel#tbPrice {{
    font-family: "{_mf}";
    font-weight: bold;
    font-size: 14pt;
}}
QLabel#tbPct {{
    font-family: "{_mf}";
    font-weight: bold;
}}
QLabel#tbAdvice {{
    font-weight: bold;
}}
QLabel#panelHead {{
    color: {C_TEXT};
    font-family: "{_df}";
    font-weight: bold;
    font-size: 12pt;
    padding-left: 4px;
}}
QWidget#rightPanel {{
    background: {C_PANEL};
    border-left: 1px solid {C_BORDER};
}}
QWidget#cardGrid {{
    background: transparent;
}}
QScrollArea#summaryScroll {{ background: transparent; border: none; }}
QScrollArea#summaryScroll > QWidget > QWidget {{ background: transparent; }}
QSplitter#rightSplitter::handle {{ background: {C_BORDER}; }}
QSplitter#rightSplitter::handle:vertical {{ height: 6px; }}
QListWidget#sectionList {{
    background: {C["bg"]};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 2px;
    outline: none;
}}
QListWidget#sectionList::item {{
    border-radius: 3px;
    padding: 7px 12px;
    margin: 1px 0;
    color: {C_TEXT};
}}
QListWidget#sectionList::item:hover {{
    background: {C["btn_hover"]};
}}
QListWidget#sectionList::item:selected {{
    background: {C["sel"]};
    color: {C_ACCENT};
    font-weight: bold;
    border-left: 3px solid {C_ACCENT};
}}
QTextBrowser#sectionText {{
    background: {C["bg"]};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 6px;
}}
QListWidget {{
    border-radius: 4px;
}}
QFrame#canvasFrame {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
}}
QTableWidget {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    gridline-color: {C["zebra"]};
    alternate-background-color: {C["zebra"]};
    selection-background-color: {C["sel"]};
    selection-color: {C_TEXT};
}}
QTableWidget::item {{
    padding: 5px 8px;
}}
QHeaderView::section {{
    background: {C["header"]};
    color: {C_TEXT};
    border: none;
    border-right: 1px solid {C_BORDER};
    border-bottom: 1px solid {C_BORDER};
    padding: 6px 8px;
    font-weight: bold;
}}
QToolBar QToolButton {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 6px 12px;
    color: {C_TEXT};
}}
QToolBar QToolButton:hover {{
    background: {C["btn_hover"]};
}}
QToolBar QToolButton:pressed {{
    background: {C["sel"]};
}}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QTextBrowser {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 3px 6px;
    selection-background-color: {C["sel"]};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {C_ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    selection-background-color: {C["sel"]};
    selection-color: {C_TEXT};
}}
QSplitter::handle {{
    background: {C_BORDER};
    width: 1px;
    height: 1px;
}}
QSplitter::handle:hover {{
    background: {C_ACCENT};
}}
QTabWidget::pane {{
    border: 1px solid {C_BORDER};
    background: {C_PANEL};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 18px;
    border: 1px solid transparent;
    border-bottom: none;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-bottom: 3px solid {C_ACCENT};
    color: {C_ACCENT};
    font-weight: bold;
}}
QTabBar::tab:hover:!selected {{
    color: {C_ACCENT};
}}
QListWidget {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 5px 10px;
    border-bottom: 1px solid {C["zebra"]};
}}
QListWidget::item:hover {{
    background: {C["btn_hover"]};
}}
QListWidget::item:selected {{
    background: {C["sel"]};
    color: {C_TEXT};
}}
QScrollBar:vertical {{
    background: {C_BG};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    border-radius: 6px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: {C_BG};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {C_BORDER};
    border-radius: 6px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QStatusBar {{
    background: {C_PANEL};
    border-top: 1px solid {C_BORDER};
    padding: 4px 8px;
    spacing: 8px;
}}
QStatusBar::item {{
    border: none;
}}
QGroupBox {{
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 6px;
    background: {C_PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {C_MUTED};
}}
QPushButton {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 4px 12px;
}}
QPushButton:hover {{
    background: {C["btn_hover"]};
}}
QPushButton:pressed {{
    background: {C["sel"]};
}}
QPushButton:disabled {{
    color: {C_MUTED};
}}
QPushButton#primaryBtn {{
    background: {C_ACCENT};
    border: 1px solid {C_ACCENT};
    color: #ffffff;
}}
QPushButton#primaryBtn:hover {{
    background: {C["accent_dark"]};
}}
QPushButton#dangerBtn {{
    color: {C_UP};
}}
QPushButton#ttsBtn {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 10pt;
    color: {C_TEXT};
}}
QPushButton#ttsBtn:hover {{
    background: {C["btn_hover"]};
    border-color: {C_ACCENT};
}}
QPushButton#ttsBtn.playing {{
    color: {C_UP};
    border-color: {C_UP};
}}
QMessageBox QLabel {{
    font-size: 12pt;
}}
QToolTip {{
    background: {C_PANEL};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    padding: 4px 8px;
}}
QMenuBar {{
    background: {C_PANEL};
    border-bottom: 1px solid {C_BORDER};
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background: {C["btn_hover"]};
    border-radius: 4px;
}}
QMenu {{
    background: {C_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 22px 5px 12px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background: {C["sel"]};
}}
QMenu::separator {{
    height: 1px;
    background: {C_BORDER};
    margin: 4px 8px;
}}
QDialog {{
    background: {C_PANEL};
    font-size: 13pt;
}}
QDockWidget {{
    background: {C_PANEL};
    titlebar-close-icon: none;
}}
QDockWidget::title {{
    background: {C["header"]};
    padding: 4px 8px;
    border-bottom: 1px solid {C_BORDER};
}}
QDockWidget::close-button {{
    background: transparent;
    border: none;
    padding: 2px;
}}
QDockWidget::close-button:hover {{
    background: {C["btn_hover"]};
    border-radius: 3px;
}}
QDockWidget::float-button {{
    background: transparent;
    border: none;
    padding: 2px;
}}
QDockWidget::float-button:hover {{
    background: {C["btn_hover"]};
    border-radius: 3px;
}}
"""


QSS = _build_qss(C)
