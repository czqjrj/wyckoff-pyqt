"""自选股卡片式列表: 阶段徽标 + 名称/代码 + 现价 + 彩色涨跌。

用 QStyledItemDelegate 绘制, 数据经 QListWidgetItem.setData 存于扩展 role:
  ROLE_NAME / ROLE_TAG / ROLE_TAG_COLOR / ROLE_PRICE / ROLE_PCT
wx 原版 WatchCard 的 PyQt6 等价物。
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
        if selected:
            bg = QColor(theme.C["sel"])
        elif hover:
            bg = QColor(theme.C["btn_hover"])
        else:
            bg = QColor(theme.C_PANEL)
        painter.fillRect(r, bg)
        # 左侧选中指示条
        if selected:
            painter.fillRect(r.x(), r.y() + 6, 3, h - 12, QColor(theme.C_ACCENT))
        code = index.data(Qt.ItemDataRole.UserRole) or ""
        name = index.data(ROLE_NAME) or ""
        tag = index.data(ROLE_TAG) or ""
        tag_color = index.data(ROLE_TAG_COLOR) or theme.C_MUTED
        price = index.data(ROLE_PRICE)
        pct = index.data(ROLE_PCT)
        fm = QFontMetrics(option.font)
        base_font = option.font
        x = r.x() + 12
        if selected:
            x += 3
        # 阶段徽标 (圆角色块 + 白字)
        tag_font = QFont(base_font)
        tag_font.setPointSizeF(base_font.pointSizeF() - 1)
        tag_font.setBold(True)
        if tag:
            tfm = QFontMetrics(tag_font)
            tw = tfm.horizontalAdvance(tag)
            th = tfm.height()
            pad = 5
            tx = x
            ty = r.y() + (h - th) // 2
            painter.setBrush(QColor(tag_color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(tx - pad // 2, ty - 2, tw + pad, th + 4, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(tag_font)
            painter.drawText(tx, ty + tfm.ascent(), tag)
            x = tx + tw + pad
        # 名称 (无名称时回退代码)
        label = name or code
        painter.setFont(base_font)
        painter.setPen(QColor(theme.C_TEXT))
        painter.drawText(x, r.y() + (h + fm.ascent() - fm.descent()) // 2, label)
        # 右侧现价 + 涨跌幅
        pct_font = QFont(base_font)
        pct_font.setPointSizeF(base_font.pointSizeF() - 1)
        pct_font.setBold(True)
        # 价格/涨跌幅用等宽 tabular 数字, 便于多行扫读对齐
        _mono = theme.pick_mono_font_family()
        pct_font.setFamily(_mono)
        price_font = QFont(base_font)
        price_font.setBold(True)
        price_font.setFamily(_mono)
        pfm = QFontMetrics(pct_font)
        prfm = QFontMetrics(price_font)
        px = r.x() + w - 12
        if price is not None:
            price_txt = f"{price:.2f}"
            pw = prfm.horizontalAdvance(price_txt)
            px -= pw
            painter.setFont(price_font)
            painter.setPen(QColor(theme.C_TEXT))
            painter.drawText(px, r.y() + (h + prfm.ascent() - prfm.descent()) // 2, price_txt)
            px -= 8
        if pct is not None:
            arrow = "↑" if pct >= 0 else "↓"
            pct_txt = f"{arrow}{abs(pct):.2f}%"
            pfw = pfm.horizontalAdvance(pct_txt)
            px -= pfw
            pc = QColor(theme.C_UP if pct >= 0 else theme.C_DOWN)
            painter.setFont(pct_font)
            painter.setPen(pc)
            painter.drawText(px, r.y() + (h + pfm.ascent() - pfm.descent()) // 2, pct_txt)
        painter.restore()
