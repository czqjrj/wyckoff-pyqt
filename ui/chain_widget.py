"""产业链地图标签页 (极简重设计): 三列强度矩阵 + 成份股面板。

设计语言: 留白与细线做层次, 不用嵌套容器/彩色填充/箭头 —
- 链条 = 小节标题行 (色点+名称+发丝线, 右侧传导状态文字) + 三列板块行
- 板块行 = 幽灵圆角行: 名称居左, 强度百分位居右(着色), 底部 3px 强度计
- 当前股所属板块: 琥珀左缘条 + ▶ 前缀; 悬停/选中轻微加深
- 成份股列表同语言: 名称/代码 · 价格 · 当日涨跌幅(着色), 双击加载个股
"""
from PyQt6.QtCore import QRectF, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .components import PanelHeader

_ROW_W, _ROW_H, _ROW_GAP = 212, 38, 7
_COL_GAP = 30
_TIER_NAMES = ("upstream", "midstream", "downstream")
_TIER_CN = {"upstream": "上 游", "midstream": "中 游", "downstream": "下 游"}
_RADIUS = 8


class _SnapThread(QThread):
    """链条快照抓取 (chain_snapshot 含网络排名)。"""
    got = pyqtSignal(object)

    def __init__(self, sector_name, parent=None):
        super().__init__(parent)
        self._sector = sector_name

    def run(self):
        try:
            from wyckoff.chain import chain_snapshot
            self.got.emit(chain_snapshot(self._sector))
        except Exception:
            self.got.emit([])


class _ConstThread(QThread):
    """板块成份股抓取。"""
    got = pyqtSignal(str, object)

    def __init__(self, node_name, parent=None):
        super().__init__(parent)
        self._name = node_name

    def run(self):
        try:
            from wyckoff.chain import board_bk_code
            from wyckoff.fundamental import fetch_board_constituents
            bk = board_bk_code(self._name)
            rows = fetch_board_constituents(bk, limit=30, name=self._name)
            self.got.emit(self._name, rows)
        except Exception:
            self.got.emit(self._name, [])


# ── 配色辅助 ──

def _mix(a_hex, b_hex, t):
    ca, cb = QColor(a_hex), QColor(b_hex)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t))


def _strength_color(pct):
    if pct is None:
        return QColor(theme.C_MUTED)
    return QColor(theme.C_UP if pct >= 0.5 else theme.C_DOWN)


def _chg_color(pct):
    """当日涨跌幅着色 (A 股约定: 红涨绿跌)。"""
    if pct is None:
        return QColor(theme.C_MUTED)
    return QColor(theme.C_UP if pct >= 0 else theme.C_DOWN)


def _pct_desc(pct):
    if pct is None:
        return "数据不可用"
    if pct >= 0.85:
        return "强度领先 (前15%)"
    if pct <= 0.15:
        return "强度垫底 (后15%)"
    return "偏强" if pct >= 0.5 else "偏弱"


class _RowItem(QGraphicsPathItem):
    """板块幽灵行: 悬停加深, 状态 normal/selected 由外部切换。"""

    def __init__(self, w, h, name, pct, tier, current, font, font_pct):
        super().__init__()
        p = QPainterPath()
        p.addRoundedRect(QRectF(0, 0, w, h), _RADIUS, _RADIUS)
        self.setPath(p)
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self._base = _mix(theme.C_BG, theme.C_PANEL, 0.9)
        self._hover = _mix(theme.C_PANEL, theme.C_BORDER, 0.30)
        self._sel = _mix(theme.C_PANEL, theme.C_ACCENT, 0.14)
        self._selected = False
        c_main = _strength_color(pct)
        if current:
            # 当前股所属板块: 琥珀左缘条 + 微琥珀底
            self._base = _mix(theme.C_PANEL, theme.C_AMBER, 0.10)
            _child_strip(self, 0, 6, 3, h - 12, QColor(theme.C_AMBER))
        self.setBrush(QBrush(self._base))
        # 名称
        ti = QGraphicsTextItem(("▶ " if current else "") + name, self)
        ti.setFont(font)
        ti.setDefaultTextColor(QColor(theme.C_TEXT))
        ti.setPos(12, (h - ti.boundingRect().height()) / 2 - 1)
        ti.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # 强度百分位 (右对齐, 着色)
        pt = "--" if pct is None else f"{pct*100:.0f}%"
        tp = QGraphicsTextItem(pt, self)
        tp.setFont(font_pct)
        tp.setDefaultTextColor(c_main)
        est = tp.boundingRect().width()
        tp.setPos(w - 12 - est, (h - tp.boundingRect().height()) / 2 - 1)
        tp.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        # 底部强度计 (轨道 + 填充)
        track_w = w - 24
        _child_strip(self, 12, h - 9, track_w, 3,
                     _mix(theme.C_PANEL, theme.C_BORDER, 0.55))
        if pct is not None:
            _child_strip(self, 12, h - 9, max(3.0, track_w * float(pct)), 3,
                         c_main)
        self.setData(0, name)
        self.setData(1, pct)
        self.setData(2, tier)
        tip = f"{_TIER_CN.get(tier, tier).replace(' ', '')} · {name}\n强度 {_pct_desc(pct)}"
        if pct is not None:
            tip += f" ({pct*100:.0f}%)"
        if current:
            tip += "\n▶ 当前个股所属板块"
        self.setToolTip(tip)
        self.setAcceptHoverEvents(True)

    def set_selected(self, sel):
        self._selected = bool(sel)
        self._apply()

    def _apply(self):
        if self._selected:
            self.setBrush(QBrush(self._sel))
            self.setPen(QPen(_mix(theme.C_PANEL, theme.C_ACCENT, 0.55), 1.0))
        else:
            self.setBrush(QBrush(self._base))
            self.setPen(QPen(Qt.PenStyle.NoPen))

    def hoverEnterEvent(self, ev):
        self.setBrush(QBrush(self._sel if self._selected else self._hover))

    def hoverLeaveEvent(self, ev):
        self._apply()


def _child_strip(parent, x, y, w, h, color, radius=None):
    """圆角小色条。parent 可为 QGraphicsItem 或 None (None 时调用方需自行
    addItem 入场景)。"""
    it = QGraphicsPathItem()
    r = h / 2 if radius is None else radius
    p = QPainterPath()
    p.addRoundedRect(QRectF(0, 0, max(1.0, w), h), r, r)
    it.setPath(p)
    it.setBrush(QBrush(color))
    it.setPen(QPen(Qt.PenStyle.NoPen))
    if parent is not None:
        it.setParentItem(parent)
    it.setPos(x, y)
    it.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    return it


class ChainWidget(QWidget):
    """产业链地图: 三列强度矩阵 + 成份股联动。"""

    def __init__(self, font_size=12, on_load=None, parent=None):
        super().__init__(parent)
        self._font_size = font_size
        self._on_load = on_load or (lambda code: None)
        self._sector = None       # 当前股所属板块名
        self._symbol = None
        self._snaps = None
        self._snap_thread = None
        self._const_thread = None
        self._loaded_once = False
        self._sel_row = None      # 当前选中行 (选中态)
        self._sel_name = None     # 选中行板块名 (重绘后恢复选中态)
        self._placeholder_msg = None
        self._build_ui()

    # ── UI ──
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.spacing("1"))

        header = PanelHeader("产业链地图")
        self.header = header
        self.status_label = QLabel("")
        header.add_action(self.status_label)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("ttsBtn")
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refresh.setFixedHeight(22)
        self.btn_refresh.clicked.connect(self.refresh)
        header.add_action(self.btn_refresh)
        lay.addWidget(header)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor(theme.C_BG)))
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.mousePressEvent = self._view_click
        lay.addWidget(self.view, 1)

        # 成份股标题行: 板块名 + 操作提示
        cap = QHBoxLayout()
        cap.setContentsMargins(theme.spacing("2"), 0, theme.spacing("2"), 0)
        self.const_title = QLabel("成份股")
        f = self.const_title.font()
        f.setBold(True)
        self.const_title.setFont(f)
        hint = QLabel("单击节点加载 · 双击行加载个股")
        self._hint_label = hint
        cap.addWidget(self.const_title)
        cap.addStretch(1)
        cap.addWidget(hint)
        lay.addLayout(cap)

        self.const_list = QListWidget()
        self.const_list.setObjectName("constList")
        self.const_list.setMaximumHeight(148)
        self.const_list.itemDoubleClicked.connect(self._load_stock)
        self._style_const_list()
        self._style_labels()
        lay.addWidget(self.const_list)

        self._placeholder()

    def _style_labels(self):
        """状态/提示文字配色 (apply_theme 时重刷, 避免构造期烧入残留)。"""
        muted = QColor(theme.C_MUTED).name()
        self.status_label.setStyleSheet(f"color:{muted};")
        self._hint_label.setStyleSheet(f"color:{muted};")

    def _style_const_list(self):
        """成份股列表样式 (apply_theme 时重刷): 无边框幽灵行, 与地图同语言。"""
        hover = _mix(theme.C_PANEL, theme.C_BORDER, 0.32).name()
        sel = _mix(theme.C_PANEL, theme.C_ACCENT, 0.16).name()
        muted = QColor(theme.C_MUTED).name()
        self.const_list.setStyleSheet(
            f"QListWidget#constList {{"
            f" background:transparent; border:none; padding:1px;}}"
            f"QListWidget#constList::item {{ margin:1px 4px;"
            f" border-radius:7px; background:transparent; }}"
            f"QListWidget#constList::item:hover {{ background:{hover}; }}"
            f"QListWidget#constList::item:selected {{ background:{sel}; }}"
            f"QLabel#constCode, QLabel#constPrice {{ color:{muted}; }}")

    def _placeholder(self, msg=None):
        self._placeholder_msg = msg   # 记住占位文案, 切主题后按当前配色重绘
        self.scene.clear()
        f = QFont()
        f.setPointSize(self._font_size + 1)
        t = self.scene.addText(
            msg or "选择股票后自动定位其产业链;\n点击\"刷新\"拉取全市场板块强度", f)
        t.setDefaultTextColor(QColor(theme.C_MUTED))

    # ── 对外接口 ──
    def set_symbol(self, code, name="", sector=None):
        """主窗口分析完成后调用: 记录当前标的并定位其板块。"""
        self._symbol = code
        self._sector = sector or None
        if sector:
            self.status_label.setText(f"{name or code} · 板块 {sector}")
        if self.isVisible() and self._loaded_once:
            self.refresh()

    def apply_theme(self):
        self.view.setBackgroundBrush(QBrush(QColor(theme.C_BG)))
        self.header.apply_theme()
        self._style_const_list()
        self._style_labels()
        if self._snaps:
            self._draw(self._snaps)
        else:
            # 无数据 (含空快照): 重绘占位符, 避免残留孤儿列头/旧配色文字
            self._placeholder(self._placeholder_msg)

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._loaded_once:
            self.refresh()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_width()

    def refresh(self):
        """拉取链条快照并重绘 (线程内网络, 完成后回主线程渲染)。"""
        if self._snap_thread and self._snap_thread.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.status_label.setText("拉取板块强度中…")
        self._loaded_once = True
        self._snap_thread = _SnapThread(self._sector, self)
        self._snap_thread.got.connect(self._on_snaps)
        self._snap_thread.start()

    # ── 渲染 ──
    def _on_snaps(self, snaps):
        self.btn_refresh.setEnabled(True)
        snaps = snaps or []
        self._snaps = snaps
        if not snaps:
            self._placeholder("板块强度数据不可用\n(网络受限或数据源暂不可达)")
            self.status_label.setText("")
            return
        n_trans = sum(1 for s in snaps if s.get("trans"))
        hi = sum(len(s.get("highlight") or []) for s in snaps)
        extra = f" · {n_trans}/{len(snaps)} 条链传导有序"
        if self._sector and not hi:
            extra += f" · [{self._sector}] 不在图谱内"
        base_txt = self.status_label.text().split(" · ")[0]
        self.status_label.setText(base_txt + extra)
        self._draw(snaps)

    def _fit_width(self):
        sr = self.scene.sceneRect()
        if sr.width() <= 10:
            return
        vw = max(200.0, self.view.viewport().width() - 12.0)
        scale = min(1.2, max(0.45, vw / sr.width()))
        self.view.resetTransform()
        self.view.scale(scale, scale)

    def _draw(self, snaps):
        self.scene.clear()
        self._sel_row = None
        rows = []
        f_name = QFont()
        f_name.setPointSize(self._font_size)
        f_title = QFont()
        f_title.setPointSize(self._font_size + 1)
        f_title.setBold(True)
        f_small = QFont()
        f_small.setPointSize(max(8, self._font_size - 3))
        f_small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        col_w, gap = _ROW_W, _COL_GAP
        inner_w = len(_TIER_NAMES) * col_w + (len(_TIER_NAMES) - 1) * gap
        band_w = inner_w + 16

        # 全局列头 (精确居中)
        y = 6.0
        for ci, t in enumerate(_TIER_NAMES):
            cx = 8 + ci * (col_w + gap) + col_w / 2
            h = self.scene.addText(_TIER_CN[t], f_small)
            h.setDefaultTextColor(QColor(theme.C_MUTED))
            h.setPos(cx - h.boundingRect().width() / 2, y)
        y += 28

        muted = QColor(theme.C_MUTED)
        for snap in snaps:
            tiers = snap["tiers"]
            max_rows = max(len(tiers[t]) for t in _TIER_NAMES)
            # 小节标题行: 色点 + 链名 + 发丝线 (+传导状态文字)
            avg_all = [v for v in snap["avg"].values() if v is not None]
            dot_c = _strength_color(sum(avg_all) / len(avg_all)) \
                if avg_all else muted
            dot = _child_strip(None, 10, y + 8, 7, 7, dot_c, radius=2)
            self.scene.addItem(dot)
            ti = QGraphicsTextItem(snap["name"])
            ti.setFont(f_title)
            ti.setDefaultTextColor(QColor(theme.C_TEXT))
            ti.setPos(22, y - 2)
            self.scene.addItem(ti)
            if snap.get("trans"):
                tc = QColor(theme.C_UP if snap["trans"] == "上游→下游"
                            else theme.C_DOWN)
                arrow = "→" if snap["trans"] == "上游→下游" else "←"
                tt = QGraphicsTextItem(f"{arrow} {snap['trans']}")
                tt.setFont(f_small)
                tt.setDefaultTextColor(tc)
                tt.setPos(band_w - 16 - tt.boundingRect().width(), y + 4)
                self.scene.addItem(tt)
            line_y = y + 26
            self.scene.addLine(8, line_y, band_w - 8, line_y,
                               QPen(_mix(theme.C_PANEL, theme.C_BORDER,
                                         0.45), 1.0))
            y = line_y + 12
            for ci, t in enumerate(_TIER_NAMES):
                x0 = 8 + ci * (col_w + gap)
                for ri, nd in enumerate(tiers[t]):
                    current = any(tt == t and nn == nd["name"]
                                  for tt, nn in (snap.get("highlight") or []))
                    row = _RowItem(col_w, _ROW_H, nd["name"], nd.get("pct"),
                                   t, current, f_name, f_name)
                    self.scene.addItem(row)
                    row.setPos(x0, y + ri * (_ROW_H + _ROW_GAP))
                    rows.append(row)
            y += max_rows * (_ROW_H + _ROW_GAP) - _ROW_GAP + 20
        # 恢复主题切换前的选中态
        if self._sel_name:
            for r in rows:
                if r.data(0) == self._sel_name:
                    self._sel_row = r
                    r.set_selected(True)
                    break
        self.scene.setSceneRect(QRectF(0, 0, band_w, y + 4))
        self._fit_width()

    # ── 交互 ──
    def _view_click(self, ev):
        item = self.view.itemAt(ev.pos())
        while item is not None and not isinstance(item, _RowItem):
            item = item.parentItem()
        if isinstance(item, _RowItem) and item.data(0):
            if self._sel_row is not None and self._sel_row is not item:
                self._sel_row.set_selected(False)
            self._sel_row = item
            self._sel_name = item.data(0)
            item.set_selected(True)
            self._select_const(item.data(0), item.data(1))
        QGraphicsView.mousePressEvent(self.view, ev)

    def _select_const(self, name, pct):
        if self._const_thread and self._const_thread.isRunning():
            return
        ptxt = "--" if pct is None else f"{pct*100:.0f}%"
        self.const_title.setText(f"{name} · 强度 {ptxt}")
        self.const_list.clear()
        self._const_thread = _ConstThread(name, self)
        self._const_thread.got.connect(self._on_constituents)
        self._const_thread.start()

    def _on_constituents(self, name, rows):
        if not rows:
            self.const_list.addItem(QListWidgetItem("(成份股获取失败或为空)"))
            return
        for r0 in rows:
            code, nm, last = r0[0], r0[1], r0[2]
            pct = r0[3] if len(r0) > 3 else None
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, code)
            it.setSizeHint(QSize(0, 30))
            it.setToolTip(f"{name} · {nm} {code} (双击加载)")
            self.const_list.addItem(it)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 2, 12, 2)
            h.setSpacing(8)
            nm_lab = QLabel(nm)
            f = nm_lab.font()
            f.setBold(True)
            nm_lab.setFont(f)
            code_lab = QLabel(code[-6:])
            code_lab.setObjectName("constCode")
            h.addWidget(nm_lab)
            h.addWidget(code_lab)
            h.addStretch(1)
            price_lab = QLabel(f"{last:.2f}" if last else "--")
            price_lab.setObjectName("constPrice")
            h.addWidget(price_lab)
            chg_txt = "--" if pct is None else f"{pct:+.2f}%"
            chg_lab = QLabel(chg_txt)
            chg_lab.setStyleSheet(
                f"color:{_chg_color(pct).name()}; font-weight:bold;")
            chg_lab.setMinimumWidth(64)
            chg_lab.setAlignment(Qt.AlignmentFlag.AlignRight |
                                 Qt.AlignmentFlag.AlignVCenter)
            h.addWidget(chg_lab)
            self.const_list.setItemWidget(it, row)

    def _load_stock(self, item):
        code = item.data(Qt.ItemDataRole.UserRole)
        if code:
            self._on_load(code)
