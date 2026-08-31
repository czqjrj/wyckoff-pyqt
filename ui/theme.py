"""UI 主题: 颜色 / 字体 / 全局样式表。复用 wyckoff.config 的调色板, 保证图表与界面一致。

支持浅色 (light) 与深色护眼 (dark) 两套主题, 通过 set_theme() 在运行时切换:
切换会重绑本模块 C_* 常量并重建 QSS, 图表控件均通过 theme.C_* 运行时取色, 因此
只需要调用方重新渲染 (set_data) 即可整体换肤。

提供 ThemeManager 单例 (唯一实例), 所有颜色/字体/QSS 状态均集中在它上面;
模块级函数与其 __getattr__ 代理仅是向后兼容的转发层, 不持有任何独立状态。
"""
from PyQt6.QtGui import QColor, QFont

from wyckoff.config import FONT_CANDIDATES, FONT_DISPLAY_CANDIDATES, MONO_FONT, THEMES

from ._tokens import (
    RADIUS,
    SEMANTIC_KEYS,
    SPACING,
    TYPE_SCALE,
)
from ._tokens import (
    radius as _radius,
)
from ._tokens import (
    spacing as _spacing,
)

# ── Type Scale 动态缩放 ──
# 基准标称值 (UI_FONT_SIZE=12pt); set_ui_font 按用户界面字号等比缩放
# TYPE_SCALE (就地 update), 使全局 QSS 与内联样式 (theme.font_pt) 同步跟随设置。
_BASE_TYPE_SCALE = dict(TYPE_SCALE)
_MIN_FONT_PT = 8          # 缩放下限, 避免过小不可读
_SCALE_FACTOR_RANGE = (0.75, 1.75)   # 界面字号允许的缩放倍率区间


def apply_type_scale(base_size):
    """按界面基准字号等比缩放整套 Type Scale (就地更新, 所有引用同步生效)。

    base_size: 设置→基本→界面字号 (pt); 12pt 为标称基准 (倍率 1.0)。
    """
    try:
        base = float(base_size)
    except (TypeError, ValueError):
        return
    if base <= 0:
        return
    k = max(_SCALE_FACTOR_RANGE[0], min(_SCALE_FACTOR_RANGE[1], base / 12.0))
    for role, (fs_, lh_) in _BASE_TYPE_SCALE.items():
        TYPE_SCALE[role] = (max(_MIN_FONT_PT, round(fs_ * k)),
                            max(10, round(lh_ * k)))


def font_pt(role="body"):
    """内联样式用字号串 (跟随界面基准字号): f"font-size:{theme.font_pt('caption')}"。"""
    return f"{TYPE_SCALE.get(role, TYPE_SCALE['body'])[0]}pt"


class ThemeManager:
    """主题管理器 (单例): 封装调色板/字体/QSS 全部状态。

    唯一实例由模块级 __new__ 保证; 如无必要请勿直接构造, 用 get_theme_manager()。

    用法:
        tm = get_theme_manager()
        tm.set_theme("dark")
        tm.set_ui_font(family="微软雅黑", size=12)
        qss = tm.QSS
    """

    _singleton = None
    _initialized = False

    def __new__(cls, name="light"):
        if cls._singleton is None:
            cls._singleton = super().__new__(cls)
        return cls._singleton

    def __init__(self, name="light"):
        if self._initialized:
            return
        self._initialized = True
        self._active = "light"
        self.C = THEMES["light"]
        self.UI_FONT_FAMILY = ""
        self.UI_FONT_SIZE = 12
        self.WATCH_FONT_SIZE = 12
        self.QSS = ""
        self._semantic = {}
        self._sync_attrs()
        self.set_theme(name)

    def _sync_attrs(self):
        """从 self.C 刷新 C_* 便捷属性 + 语义色。"""
        pal = self.C
        self.C_UP = pal["up"]
        self.C_DOWN = pal["down"]
        self.C_AMBER = pal["amber"]
        self.C_MUTED = pal["muted"]
        self.C_TEXT = pal["text"]
        self.C_BG = pal["bg"]
        self.C_PANEL = pal["panel"]
        self.C_BORDER = pal["border"]
        self.C_ACCENT = pal["accent"]
        self.C_GRID = pal["grid"]
        self.C_ZONE_ACC = pal["zone_acc"]
        self.C_ZONE_DIST = pal["zone_dist"]
        self.C_ZONE_NEUT = pal["zone_neut"]
        self.TONE_COLOR = {
            "bullish": self.C_UP,
            "bearish": self.C_DOWN,
            "neutral": self.C_MUTED,
            "caution": self.C_AMBER,
        }
        # 语义色映射 (由 _build_semantic() 填充)
        for k in SEMANTIC_KEYS:
            setattr(self, f"S_{k.upper().replace('-', '_')}", self._semantic.get(k, pal["text"]))

    def _build_semantic(self, pal: dict) -> dict:
        """根据基础调色板生成语义色表。"""
        up = pal["up"]
        down = pal["down"]
        amber = pal["amber"]
        muted = pal["muted"]
        text = pal["text"]
        bg = pal["bg"]
        panel = pal["panel"]
        border = pal["border"]
        accent = pal["accent"]
        grid = pal["grid"]
        zone_acc = pal["zone_acc"]
        zone_dist = pal["zone_dist"]
        zone_neut = pal["zone_neut"]
        zebra = pal.get("zebra", panel)
        header = pal.get("header", panel)
        sel = pal.get("sel", accent)
        btn_hover = pal.get("btn_hover", border)

        def alpha(c, a):
            qc = QColor(c)
            qc.setAlphaF(a)
            return qc.name(QColor.NameFormat.HexArgb)

        return {
            # Brand
            "brand": accent,
            "brand-hover": alpha(accent, 0.9),
            "brand-pressed": alpha(accent, 0.8),
            # Surface
            "surface-0": bg,
            "surface-1": panel,
            "surface-2": alpha(panel, 0.95),
            "surface-3": alpha(panel, 0.9),
            "surface-4": alpha(panel, 0.85),
            # Border
            "border": border,
            "border-strong": alpha(border, 0.8),
            "border-focus": accent,
            # Text
            "text-primary": text,
            "text-secondary": alpha(text, 0.8),
            "text-muted": muted,
            "text-inverse": bg if pal == THEMES["dark"] else "#ffffff",
            "text-disabled": alpha(text, 0.4),
            # State
            "success": up,
            "success-hover": alpha(up, 0.9),
            "success-bg": alpha(up, 0.12),
            "warning": amber,
            "warning-hover": alpha(amber, 0.9),
            "warning-bg": alpha(amber, 0.12),
            "error": down,
            "error-hover": alpha(down, 0.9),
            "error-bg": alpha(down, 0.12),
            "info": accent,
            "info-hover": alpha(accent, 0.9),
            "info-bg": alpha(accent, 0.12),
            # Accent
            "accent": accent,
            "accent-hover": alpha(accent, 0.9),
            "accent-pressed": alpha(accent, 0.8),
            "accent-bg": alpha(accent, 0.12),
            # Chart compat
            "up": up,
            "down": down,
            "amber": amber,
            "muted": muted,
            "zone-acc": zone_acc,
            "zone-dist": zone_dist,
            "zone-neut": zone_neut,
            "grid": grid,
            "zebra": zebra,
            "header": header,
            "sel": sel,
            "btn-hover": btn_hover,
        }

    def active_theme(self):
        return self._active

    def set_theme(self, name):
        pal = THEMES.get(name)
        if pal is None:
            name = "light"
            pal = THEMES["light"]
        self._active = name
        self.C = pal
        self._semantic = self._build_semantic(pal)
        self._sync_attrs()
        self.QSS = self._build_qss()

    def set_ui_font(self, family="", size=0, watch=0):
        if family:
            self.UI_FONT_FAMILY = family
        if size and int(size) > 0:
            self.UI_FONT_SIZE = int(size)
        if watch and int(watch) > 0:
            self.WATCH_FONT_SIZE = int(watch)
        # 字号变化 → 整套 Type Scale 等比缩放, 保证层级比例一致
        apply_type_scale(self.UI_FONT_SIZE)
        self.QSS = self._build_qss()

    def _build_qss(self):
        return _build_qss(self)

    def ui_font_family(self):
        return self.UI_FONT_FAMILY or pick_font_family()

    def color(self, hexstr, alpha=None):
        c = QColor(hexstr)
        if alpha is not None:
            c.setAlpha(alpha)
        return c

    def css_rgba(self, hexstr, alpha=255):
        c = QColor(hexstr)
        c.setAlpha(alpha)
        return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"

    def app_font(self, size=10, bold=False):
        f = QFont()
        f.setFamily(self.ui_font_family())
        f.setPointSize(int(size))
        f.setBold(bold)
        return f

    def display_font(self, size=10, bold=False):
        f = QFont()
        f.setFamily(FONT_DISPLAY_CANDIDATES[0] if FONT_DISPLAY_CANDIDATES else "serif")
        f.setPointSize(int(size))
        f.setBold(bold)
        return f

    def mono_font(self, size=10, bold=False):
        f = QFont()
        f.setFamily(MONO_FONT or "monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(int(size))
        f.setBold(bold)
        return f

    # ── 语义色 / Type Scale 访问器 ──
    def semantic(self, key: str) -> str:
        return self._semantic.get(key, self.C_TEXT)

    def semantic_rgba(self, key: str, alpha: int = 255) -> str:
        c = QColor(self._semantic.get(key, self.C_TEXT))
        c.setAlpha(alpha)
        return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"

    def type_size(self, role: str) -> int:
        return TYPE_SCALE.get(role, TYPE_SCALE["body"])[0]

    def type_line_height(self, role: str) -> int:
        return TYPE_SCALE.get(role, TYPE_SCALE["body"])[1]

    def font(self, role: str = "body", bold: bool = False) -> QFont:
        """根据 role 获取对应字体 (自动应用字号/行高/字族)。"""
        fs, lh = TYPE_SCALE.get(role, TYPE_SCALE["body"])
        fam = self.ui_font_family() if role not in ("mono", "mono-sm") else (MONO_FONT or "monospace")
        if role in ("display", "h1", "h2"):
            fam = FONT_DISPLAY_CANDIDATES[0] if FONT_DISPLAY_CANDIDATES else "serif"
        f = QFont(fam)
        f.setPointSize(fs)
        f.setBold(bold)
        return f


def active_theme():
    """当前生效的主题名 (light/dark)。"""
    return _theme_manager.active_theme()


def set_theme(name):
    """切换主题 (light/dark) — 转发给 ThemeManager 单例。"""
    _theme_manager.set_theme(name)


def set_ui_font(family="", size=0, watch=0):
    """设置全局界面字体/字号与自选股栏字号 — 转发给 ThemeManager 单例。

    family 为空 / size 或 watch 非 >0 时保留原值。
    字号变化时整套 Type Scale 等比缩放: QSS 各角色与 theme.font_pt() 内联样式
    均跟随界面字号, 全局字号层级保持一致。"""
    _theme_manager.set_ui_font(family, size, watch)


def get_theme_manager():
    """获取 ThemeManager 单例实例。"""
    return _theme_manager


def ui_font_family():
    """当前生效的界面字体族 (未自定义时回退系统候选)。"""
    return _theme_manager.ui_font_family()


def spacing(key: str) -> int:
    return _spacing(key)


def radius(key: str) -> int:
    return _radius(key)


def pick_font_family(preferred=""):
    """在系统可用字体中挑选 FONT_CANDIDATES 里第一个存在的 (参考 config._pick_font)。"""
    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        # 模块导入期 (构造单例时 _build_qss) 尚无 QApplication, QFontDatabase 不可用;
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
    return _theme_manager.app_font(size, bold)


def ui_font(size=10, bold=False):
    return _theme_manager.app_font(size, bold)


def display_font(size=10, bold=False):
    """衬线显示字体: 用于品牌标题/面板标题, 建立与正文的层级。"""
    return _theme_manager.display_font(size, bold)


def mono_font(size=10, bold=False):
    """等宽 tabular 字体: 用于价格/百分比/数据, 数字对齐便于扫读。"""
    return _theme_manager.mono_font(size, bold)


def color(hexstr, alpha=None):
    return _theme_manager.color(hexstr, alpha)


def css_rgba(hexstr, alpha=255):
    return _theme_manager.css_rgba(hexstr, alpha)


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


def _build_qss(tm):
    # tm: ThemeManager 单例 (颜色/字体/QSS 状态唯一来源)
    _df = _display_family()
    _mf = _mono_family()
    C = tm.C
    _fam = tm.ui_font_family()
    _ui_size = tm.UI_FONT_SIZE
    _watch_size = tm.WATCH_FONT_SIZE
    _sem = tm._semantic

    # 直接内联所有值 (Qt QSS 不支持 CSS 变量)
    def s(key, default=""):
        return _sem.get(key, default)

    def tint(c, f):
        """按系数缩放 RGB 亮度: f>1 变亮 / f<1 变暗 (主按钮渐变/按压反馈用)"""
        qc = QColor(c)
        qc.setRed(max(0, min(255, round(qc.red() * f))))
        qc.setGreen(max(0, min(255, round(qc.green() * f))))
        qc.setBlue(max(0, min(255, round(qc.blue() * f))))
        return qc.name()

    def sp(key, default="0"):
        return f"{SPACING.get(key, 0)}px"

    def rd(key, default="0"):
        return f"{RADIUS.get(key, 0)}px"

    def fs(key, default="12"):
        return f"{TYPE_SCALE.get(key, TYPE_SCALE['body'])[0]}pt"

    def lh(key, default="18"):
        return f"{TYPE_SCALE.get(key, TYPE_SCALE['body'])[1]}pt"

    return f"""
* {{
    font-family: "{_fam}";
    font-size: {_ui_size}pt;
    line-height: {lh("body")};
}}

QListWidget#watchList {{
    font-size: {_watch_size}pt;
}}

QMainWindow, QDialog {{
    background: {s("surface-0", C["bg"])};
}}

QWidget {{
    color: {s("text-primary", C["text"])};
}}

QToolBar {{
    background: {s("surface-1", C["panel"])};
    border-bottom: 1px solid {s("border", C["border"])};
    padding: {sp("1")} {sp("2")};
    spacing: {sp("2")};
}}

QWidget#topBar {{
    background: {s("surface-1", C["panel"])};
    border-bottom: 1px solid {s("border", C["border"])};
}}

QFrame#vsep {{
    color: {s("border", C["border"])};
}}

QLabel#brandTitle {{
    color: {s("brand", C["accent"])};
    font-family: "{_df}";
    font-weight: bold;
    font-size: {fs("h1")};
    padding-right: {sp("1")};
}}

QLabel#tbName {{
    font-weight: bold;
    font-size: {fs("h2")};
    color: {s("text-primary", C["text"])};
}}

QLabel#tbCode {{
    color: {s("text-muted", C["muted"])};
    font-family: "{_mf}";
    font-size: {fs("body")};
}}

QLabel#tbPrice {{
    font-family: "{_mf}";
    font-weight: bold;
    font-size: {fs("mono")};
    color: {s("text-primary", C["text"])};
}}

QLabel#tbPct {{
    font-family: "{_mf}";
    font-weight: bold;
    font-size: {fs("mono")};
}}

QLabel#tbAdvice {{
    font-weight: bold;
    font-size: {fs("body")};
}}

QLabel#panelHead {{
    color: {s("text-primary", C["text"])};
    font-family: "{_df}";
    font-weight: bold;
    font-size: {fs("h2")};
    padding-left: {sp("1")};
}}

QLabel#chartHint {{
    color: {s("text-muted", C["muted"])};
    font-size: {fs("caption")};
    padding: 0 {sp("2")};
}}

QPushButton#ttsBtn:checked {{
    color: {s("accent", C["accent"])};
    border-color: {s("accent", C["accent"])};
    background: {s("accent-soft", C.get("accent_soft", C["panel"]))};
}}

QWidget#rightPanel {{
    background: {s("surface-1", C["panel"])};
    border-left: 1px solid {s("border", C["border"])};
}}

QWidget#cardGrid {{
    background: transparent;
}}

QScrollArea#summaryScroll {{
    background: transparent;
    border: none;
}}

QScrollArea#summaryScroll > QWidget > QWidget {{
    background: transparent;
}}

QListWidget#sectionList {{
    background: {s("surface-0", C["bg"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")};
    outline: none;
}}

QListWidget#sectionList::item {{
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("2")};
    margin: {sp("1")} 0;
    color: {s("text-primary", C["text"])};
}}

QListWidget#sectionList::item:hover {{
    background: {s("surface-3", C["panel"])};
}}

QListWidget#sectionList::item:selected {{
    background: {s("accent-bg", C["accent"])};
    color: {s("brand", C["accent"])};
    font-weight: bold;
    border-left: 3px solid {s("brand", C["accent"])};
}}

QTextBrowser#sectionText {{
    background: {s("surface-0", C["bg"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("2")};
    line-height: {lh("body")};
}}

QFrame#canvasFrame {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("md")};
}}

/* 新闻面板三区块: 与 canvasFrame 同规格卡片 */
QFrame#newsHeaderBox, QFrame#newsEventsBox, QFrame#newsAllBox {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("md")};
}}

QTextBrowser#newsList {{
    background: transparent;
    border: none;
}}

QTableWidget {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    gridline-color: {s("zebra", C.get("zebra", C["panel"]))};
    alternate-background-color: {s("zebra", C.get("zebra", C["panel"]))};
    selection-background-color: {s("sel", C.get("sel", C["accent"]))};
    selection-color: {s("text-primary", C["text"])};
    font-family: "{_mf}";
}}

QTableWidget::item {{
    padding: {sp("1")} {sp("2")};
}}

QHeaderView::section {{
    background: {s("header", C.get("header", C["panel"]))};
    color: {s("text-primary", C["text"])};
    border: none;
    border-right: 1px solid {s("border", C["border"])};
    border-bottom: 1px solid {s("border", C["border"])};
    padding: {sp("1")} {sp("2")};
    font-weight: bold;
    font-size: {fs("body")};
}}

QToolBar QToolButton {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("2")};
    color: {s("text-primary", C["text"])};
}}

QToolBar QToolButton:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
}}

QToolBar QToolButton:pressed {{
    background: {s("sel", C.get("sel", C["accent"]))};
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QTextBrowser {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("2")};
    selection-background-color: {s("sel", C.get("sel", C["accent"]))};
    font-size: {fs("body")};
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {s("border-focus", C["accent"])};
}}

QComboBox::drop-down {{
    border: none;
    width: 18px;
}}

QComboBox QAbstractItemView {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    selection-background-color: {s("sel", C.get("sel", C["accent"]))};
    selection-color: {s("text-primary", C["text"])};
}}

/* 下拉箭头: CSS 三角, 替代 Fusion 原生亮色箭头 */
QComboBox::down-arrow {{
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {s("text-muted", C["muted"])};
    margin-right: {sp("1")};
}}

QComboBox::down-arrow:on {{
    border-top: 5px solid {s("brand", C["accent"])};
}}

QCheckBox, QRadioButton {{
    spacing: {sp("2")};
    background: transparent;
}}

QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {s("border-strong", C["border"])};
    border-radius: 3px;
    background: {s("surface-1", C["panel"])};
}}

QCheckBox::indicator:hover {{
    border-color: {s("brand", C["accent"])};
}}

QCheckBox::indicator:checked {{
    background: {s("brand", C["accent"])};
    border-color: {s("brand", C["accent"])};
}}

QCheckBox::indicator:disabled {{
    background: {s("surface-0", C["bg"])};
    border-color: {s("border", C["border"])};
}}

QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {s("border-strong", C["border"])};
    border-radius: 8px;
    background: {s("surface-1", C["panel"])};
}}

QRadioButton::indicator:hover {{
    border-color: {s("brand", C["accent"])};
}}

QRadioButton::indicator:checked {{
    background: {s("surface-1", C["panel"])};
    border: 5px solid {s("brand", C["accent"])};
}}

QRadioButton::indicator:disabled {{
    background: {s("surface-0", C["bg"])};
    border-color: {s("border", C["border"])};
}}

QProgressBar {{
    background: {s("surface-2", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    text-align: center;
    color: {s("text-primary", C["text"])};
    font-size: {fs("caption")};
    min-height: 14px;
}}

QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {tint(s("brand", C["accent"]), 1.18)},
                stop:1 {s("brand", C["accent"])});
    border-radius: {RADIUS["sm"] - 1}px;
}}

QSplitter::handle {{
    background: {s("border", C["border"])};
    width: 5px;
    height: 5px;
}}

QSplitter::handle:hover {{
    background: {s("brand", C["accent"])};
}}

QTabWidget::pane {{
    border: 1px solid {s("border", C["border"])};
    background: {s("surface-1", C["panel"])};
    top: -1px;
}}

QTabBar::tab {{
    background: transparent;
    padding: {sp("1")} {sp("3")};
    border: 1px solid transparent;
    border-bottom: none;
    margin-right: {sp("1")};
    font-size: {fs("body")};
}}

QTabBar::tab:selected {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-bottom: 3px solid {s("brand", C["accent"])};
    color: {s("brand", C["accent"])};
    font-weight: bold;
}}

QTabBar::tab:hover:!selected {{
    color: {s("brand", C["accent"])};
}}

QListWidget {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    outline: none;
}}

QListWidget::item {{
    padding: {sp("1")} {sp("2")};
    border-bottom: 1px solid {s("zebra", C.get("zebra", C["panel"]))};
}}

QListWidget::item:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
}}

QListWidget::item:selected {{
    background: {s("sel", C.get("sel", C["accent"]))};
    color: {s("text-primary", C["text"])};
}}

QScrollBar:vertical {{
    background: {s("surface-0", C["bg"])};
    width: 12px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {s("border", C["border"])};
    border-radius: 6px;
    min-height: 30px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background: {s("surface-0", C["bg"])};
    height: 12px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background: {s("border", C["border"])};
    border-radius: 6px;
    min-width: 30px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

QStatusBar {{
    background: {s("surface-1", C["panel"])};
    border-top: 1px solid {s("border", C["border"])};
    padding: {sp("1")} {sp("2")};
    spacing: {sp("2")};
}}

QStatusBar::item {{
    border: none;
}}

QGroupBox {{
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("md")};
    margin-top: {sp("3")};
    padding-top: {sp("2")};
    background: {s("surface-1", C["panel"])};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: {sp("2")};
    padding: 0 {sp("1")};
    color: {s("text-muted", C["muted"])};
    font-size: {fs("body-sm")};
}}

QPushButton {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("3")};
    font-size: {fs("body")};
}}

QPushButton:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
}}

QPushButton:pressed {{
    background: {s("sel", C.get("sel", C["accent"]))};
}}

QPushButton:disabled {{
    color: {s("text-disabled", C["muted"])};
}}

QPushButton#primaryBtn {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {tint(s("brand", C["accent"]), 1.22)},
                stop:1 {s("brand", C["accent"])});
    border: 1px solid {tint(s("brand", C["accent"]), 0.88)};
    border-radius: {rd("sm")};
    color: #ffffff;
    font-weight: 600;
    padding: {sp("1")} {sp("4")};
}}

QPushButton#primaryBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {tint(s("brand", C["accent"]), 1.34)},
                stop:1 {tint(s("brand", C["accent"]), 1.12)});
    border-color: {tint(s("brand", C["accent"]), 1.08)};
}}

QPushButton#primaryBtn:pressed {{
    background: {tint(s("brand", C["accent"]), 0.82)};
    border-color: {tint(s("brand", C["accent"]), 0.78)};
    padding-top: {SPACING["1"] + 1}px;
    padding-bottom: {max(SPACING["1"] - 1, 0)}px;
}}

/* 分析进行中: 琥珀色渐变, 与状态栏"正在分析"提示呼应 */
QPushButton#primaryBtn[analyzing="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {tint(s("warning", C["amber"]), 1.25)},
                stop:1 {s("warning", C["amber"])});
    border: 1px solid {tint(s("warning", C["amber"]), 0.9)};
    color: #ffffff;
    font-weight: 600;
    padding: {sp("1")} {sp("4")};
}}

QPushButton#dangerBtn {{
    color: {s("error", C["down"])};
    border-color: {s("error", C["down"])};
}}

QPushButton#ttsBtn {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("2")};
    font-size: {fs("caption")};
    color: {s("text-primary", C["text"])};
    outline: none;
}}

QPushButton#ttsBtn:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
}}

QPushButton#ttsBtn.playing {{
    color: {s("success", C["up"])};
    border-color: {s("success", C["up"])};
}}

QMessageBox QLabel {{
    font-size: {fs("h2")};
}}

QToolTip {{
    background: {s("surface-1", C["panel"])};
    color: {s("text-primary", C["text"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("sm")};
    padding: {sp("1")} {sp("2")};
    font-size: {fs("caption")};
}}

QMenuBar {{
    background: {s("surface-1", C["panel"])};
    border-bottom: 1px solid {s("border", C["border"])};
}}

QMenuBar::item {{
    padding: {sp("1")} {sp("2")};
    background: transparent;
}}

QMenuBar::item:selected {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
    border-radius: {rd("sm")};
}}

QMenu {{
    background: {s("surface-1", C["panel"])};
    border: 1px solid {s("border", C["border"])};
    border-radius: {rd("md")};
    padding: {sp("1")};
}}

QMenu::item {{
    padding: {sp("1")} {sp("3")} {sp("1")} {sp("2")};
    border-radius: {rd("sm")};
}}

QMenu::item:selected {{
    background: {s("sel", C.get("sel", C["accent"]))};
}}

QMenu::separator {{
    height: 1px;
    background: {s("border", C["border"])};
    margin: {sp("1")} {sp("2")};
}}

QDockWidget {{
    background: {s("surface-1", C["panel"])};
    titlebar-close-icon: none;
}}

QDockWidget::title {{
    background: {s("header", C.get("header", C["panel"]))};
    padding: {sp("1")} {sp("2")};
    border-bottom: 1px solid {s("border", C["border"])};
}}

QDockWidget::close-button {{
    background: transparent;
    border: none;
    padding: {sp("1")};
}}

QDockWidget::close-button:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
    border-radius: {rd("sm")};
}}

QDockWidget::float-button {{
    background: transparent;
    border: none;
    padding: {sp("1")};
}}

QDockWidget::float-button:hover {{
    background: {s("btn-hover", C.get("btn_hover", C["border"]))};
    border-radius: {rd("sm")};
}}
"""
# Module-level semantic color accessor (backward compat)
def semantic_color(key: str) -> str:
    """Get semantic color value (e.g. 'success', 'warning-bg', 'accent')."""
    return _theme_manager.semantic(key)


def semantic_rgba(key: str, alpha: int = 255) -> str:
    """Get semantic color as rgba string."""
    return _theme_manager.semantic_rgba(key, alpha)


# Type Scale accessors
def type_size(role: str) -> int:
    """Get Type Scale font size (pt). role: display/h1/h2/body/body-sm/caption/mono/mono-sm"""
    return _theme_manager.type_size(role)


def type_line_height(role: str) -> int:
    """Get Type Scale line height (pt)."""
    return _theme_manager.type_line_height(role)


def mono_font_family() -> str:
    """Current effective mono font family."""
    return _mono_family()


def display_font_family() -> str:
    """Current effective display font family."""
    return _display_family()


def font(role: str = "body", bold: bool = False):
    """Get font for role (auto applies size/line-height/family)."""
    return _theme_manager.font(role, bold)


# ── 默认 ThemeManager 单例 (唯一实例; 所有模块级函数/属性均转发到它) ──
_theme_manager = ThemeManager("light")


def __getattr__(name):
    """向后兼容: 将 theme.C_* / theme.QSS 等模块级常量读取透明转发到单例实例。

    模块级函数 (spacing/font_pt/css_rgba 等) 优先命中, 不受影响;
    此处仅兜底那些已移除为实例属性的常量名 (C, C_UP, C_TEXT, QSS, TONE_COLOR, ...)。
    """
    return getattr(_theme_manager, name)
