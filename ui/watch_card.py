"""自选股卡片式列表: 阶段徽标 + 名称/代码 + 现价 + 彩色涨跌。

用 QStyledItemDelegate 绘制, 数据经 QListWidgetItem.setData 存于扩展 role:
  ROLE_NAME / ROLE_TAG / ROLE_TAG_COLOR / ROLE_PRICE / ROLE_PCT
wx 原版 WatchCard 的 PyQt6 等价物。

优化: paint() 热路径预计算字体/度量/颜色, 避免逐帧创建对象。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics
from PyQt6.QtWidgets import QStyledItemDelegate

from . import theme

ROLE_NAME = Qt.ItemDataRole.UserRole + 1
ROLE_TAG = Qt.ItemDataRole.UserRole + 2
ROLE_TAG_COLOR = Qt.ItemDataRole.UserRole + 3
ROLE_PRICE = Qt.ItemDataRole.UserRole + 4
ROLE_PCT = Qt.ItemDataRole.UserRole + 5

PHASE_TAG = {
    "底部整固": ("底", "up"),
    "上升趋势": ("升", "up"),
    "区间整理": ("区", "amber"),
    "顶部构筑": ("顶", "down"),
    "下跌趋势": ("跌", "down"),
}


def tag_for(base):
    """返回 (徽标文字, 颜色)。颜色运行时从 theme.C 取, 主题切换后即时生效。"""
    key = PHASE_TAG.get(base or "")
    if key is None:
        return "", theme.C_MUTED
    txt, color_key = key
    return txt, theme.C.get(color_key, theme.C_MUTED)


class WatchCardDelegate(QStyledItemDelegate):
    ROW_H = 40

    # ── 类级缓存: 字体/度量/颜色, paint() 热路径复用 ──
    _base_font = None
    _tag_font = None
    _price_font = None
    _pct_font = None
    _mono_family = None
    _colors = {}        # hex -> QColor (纯色值映射, 与主题无关)
    _sem_colors = {}    # semantic key -> (hex, QColor); hex 变化(主题切换)即失效

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ensure_cached_fonts()

    @classmethod
    def _ensure_cached_fonts(cls):
        """首次使用时初始化字体/度量缓存, 后续复用。"""
        if cls._base_font is not None:
            return
        # 基础字体 (从 theme 获取当前界面字体)
        cls._base_font = theme.app_font()
        # 标签字体: 小一号 + 粗体
        cls._tag_font = QFont(cls._base_font)
        cls._tag_font.setPointSizeF(cls._base_font.pointSizeF() - 1)
        cls._tag_font.setBold(True)
        # 等宽字体族 (价格/涨跌幅)
        cls._mono_family = theme.pick_mono_font_family()
        # 价格字体: 等宽 + 粗体
        cls._price_font = QFont(cls._base_font)
        cls._price_font.setBold(True)
        cls._price_font.setFamily(cls._mono_family)
        # 涨跌幅字体: 等宽 + 小一号 + 粗体
        cls._pct_font = QFont(cls._base_font)
        cls._pct_font.setPointSizeF(cls._base_font.pointSizeF() - 1)
        cls._pct_font.setBold(True)
        cls._pct_font.setFamily(cls._mono_family)

    def sizeHint(self, option, index):
        from PyQt6.QtCore import QSize
        return QSize(option.rect.width() or 120, self.ROW_H)

    def paint(self, painter, option, index):
        painter.save()
        w = option.rect.width()
        h = option.rect.height()
        r = option.rect
        from PyQt6.QtWidgets import QStyle
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        # 背景色 (语义键: sel / btn-hover / surface-1)
        bg_color = self._color("sel" if selected else ("btn-hover" if hover else "surface-1"))
        painter.fillRect(r, bg_color)
        # 左侧选中指示条
        if selected:
            painter.fillRect(r.x(), r.y() + 6, 3, h - 12, self._color("accent"))
        code = index.data(Qt.ItemDataRole.UserRole) or ""
        name = index.data(ROLE_NAME) or ""
        tag = index.data(ROLE_TAG) or ""
        tag_color = index.data(ROLE_TAG_COLOR) or theme.C_MUTED
        price = index.data(ROLE_PRICE)
        pct = index.data(ROLE_PCT)

        fm = QFontMetrics(self._base_font)
        x = r.x() + 12
        if selected:
            x += 3
        # 阶段徽标 (圆角色块 + 白字)
        tag_fm = QFontMetrics(self._tag_font)
        if tag:
            tw = tag_fm.horizontalAdvance(tag)
            th = tag_fm.height()
            pad = 5
            tx = x
            ty = r.y() + (h - th) // 2
            painter.setBrush(self._color_from_str(tag_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(tx - pad // 2, ty - 2, tw + pad, th + 4, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(self._tag_font)
            painter.drawText(tx, ty + tag_fm.ascent(), tag)
            x = tx + tw + pad
        # 名称 (无名称时回退代码)
        label = name or code
        painter.setFont(self._base_font)
        painter.setPen(self._color("text-primary"))
        painter.drawText(x, r.y() + (h + fm.ascent() - fm.descent()) // 2, label)
        # 右侧现价 + 涨跌幅
        px = r.x() + w - 12
        if price is not None:
            price_txt = f"{price:.2f}"
            pw = QFontMetrics(self._price_font).horizontalAdvance(price_txt)
            px -= pw
            painter.setFont(self._price_font)
            painter.setPen(self._color("text-primary"))
            painter.drawText(px, r.y() + (h + QFontMetrics(self._price_font).ascent() - QFontMetrics(self._price_font).descent()) // 2, price_txt)
            px -= 8
        if pct is not None:
            pct_txt = f"{abs(pct):.2f}%"
            pfm = QFontMetrics(self._pct_font)
            pfw = pfm.horizontalAdvance(pct_txt)
            px -= pfw
            pc = self._color("up" if pct >= 0 else "down")
            painter.setFont(self._pct_font)
            painter.setPen(pc)
            painter.drawText(px, r.y() + (h + pfm.ascent() - pfm.descent()) // 2, pct_txt)
        painter.restore()

    # ── 颜色访问器 (带实例缓存; 主题切换后 hex 变化即自动失效重取) ──
    def _color(self, key):
        """获取语义色; 缓存 (hex, QColor), 主题切换引起 hex 变化时重新构建。"""
        sem = theme.semantic_color(key)
        entry = self._sem_colors.get(key)
        if entry is None or entry[0] != sem:
            self._sem_colors[key] = (sem, QColor(sem))
        return self._sem_colors[key][1]

    def _color_from_str(self, hexstr):
        """纯色值字符串转 QColor, 实例级缓存 (与主题无关)。"""
        c = self._colors.get(hexstr)
        if c is None:
            c = QColor(hexstr)
            self._colors[hexstr] = c
        return c
