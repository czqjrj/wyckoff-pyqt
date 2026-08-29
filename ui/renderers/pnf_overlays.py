"""P&F 覆盖层管理器 — 处理 HUD、回到最新按钮、帮助浮层、钉住文字等视口固定元素。"""
import pyqtgraph as pg

from .. import theme
from .pnf_grid import _DragTextItem, _LatestBtnItem, _pen


class PnfOverlaysManager:
    """管理所有视口固定的覆盖层元素。

    包括:
    - HUD (右上角视野指示)
    - 回到最新列按钮 (右下角)
    - 快捷键帮助浮层 (? 键切换)
    - 钉住的文字标注 (_pin 文本)
    """

    _HELP_LINES = (
        "滚轮 缩放   拖拽 平移   Shift+拖拽 框选放大   双击 复位全幅",
        "+/− 缩放   ←→↑↓ 平移   PgUp/PgDn 大步平移   End 回到最新列",
        "Home/R 全幅   Backspace/F 视图历史   [ ] 格值缩放",
        "Alt+←→ 十字逐列步进   数字键 取书签   Shift+数字 存书签",
        "? 关闭本帮助",
    )

    def __init__(self, host, vb, font_fn, fs_fn):
        """
        host: PnfWidget 实例 (回调用)
        vb: 主图 ViewBox
        font_fn: 字体获取函数 font(delta, bold)
        fs_fn: 字号获取函数 fs(delta)
        """
        self._host = host
        self._vb = vb
        self._font = font_fn
        self._fs = fs_fn

        self._pinned = []          # [(item, fy)]
        self._hud = None
        self._latest_btn = None
        self._help_item = None

    def build(self):
        """创建常驻覆盖层 (HUD/按钮)。"""
        if self._vb is None:
            return
        self._hud = self._pin("", theme.C_MUTED, 0.02, size=self._fs(-1),
                              anchor=(1, 0), fx=0.997, ephemeral=False)
        self._hud.hide()

        self._latest_btn = _LatestBtnItem(self._host)
        self._latest_btn._ephemeral = False
        self._latest_btn._pin_fx = 0.997
        self._latest_btn._pin_fy = 0.985
        f = self._font(bold=True)
        f.setPointSize(self._fs(0))
        self._latest_btn.setFont(f)
        self._latest_btn.setParentItem(self._vb)
        self._latest_btn.set_vp(0.997, 0.985)
        self._pinned.append((self._latest_btn, 0.985))

    def update_hud(self):
        """刷新右上角视野指示。"""
        if self._hud is None or not self._host._n or self._vb is None:
            if self._hud:
                self._hud.hide()
            return
        x0, x1 = self._vb.viewRange()[0]
        a = max(0, int(round(x0)))
        b = min(self._host._n - 1, int(round(x1)))
        self._hud.setText(f"列 {a}–{b} / {self._host._n} · 格值{self._host._box:.2f}")
        self._hud.show()

    def update_latest_btn(self):
        """更新回到最新按钮显隐。"""
        btn = self._latest_btn
        if btn is None:
            return
        if not self._host._n or self._vb is None:
            btn.hide()
            return
        _, x1 = self._vb.viewRange()[0]
        btn.setVisible(bool(x1 < self._host._n - 0.5))

    def toggle_help(self):
        """切换帮助浮层。"""
        if self._help_item is not None:
            self._help_item.setVisible(not self._help_item.isVisible())
            return
        ti = _DragTextItem("\n".join(self._HELP_LINES), color=theme.C_TEXT,
                           anchor=(0.5, 0.5), border=_pen(theme.C_BORDER, 1.0),
                           fill=pg.mkBrush(theme.C_PANEL))
        f = self._font()
        f.setPointSize(self._fs(0))
        ti.setFont(f)
        ti._ephemeral = False
        ti._pin_anchor = (0.5, 0.5)
        ti._pin_fx = 0.5
        ti._pin_fy = 0.40
        ti.setParentItem(self._vb)
        ti.set_vp(0.5, 0.40)
        self._pinned.append((ti, 0.40))
        self._help_item = ti

    def pin_text(self, text, color, fy, bold=False, size=10,
                 anchor=(0.5, 0.5), fx=0.5, ephemeral=True):
        """创建视口固定文字 (随视口缩放/平移保持相对位置, 可拖拽)。"""
        ti = _DragTextItem(text, color=color, anchor=anchor)
        f = self._font(bold=bold)
        f.setPointSize(size)
        ti.setFont(f)
        ti._ephemeral = bool(ephemeral)
        ti._pin_anchor = tuple(anchor)
        ti._pin_fx = float(fx)
        ti._pin_fy = float(fy)
        ti.setParentItem(self._vb)
        ti.set_vp(float(fx), float(fy))
        self._pinned.append((ti, float(fy)))
        self.reposition_all()
        return ti

    def reposition_all(self):
        """重新定位所有钉住的文字 (ViewBox 尺寸变化后调用)。"""
        if self._vb is None:
            return
        rect = self._vb.rect()
        for ti, fy in self._pinned:
            vx, vy = getattr(ti, "_vp",
                             (getattr(ti, "_pin_fx", 0.5), fy))
            ti.setPos(rect.width() * vx, rect.height() * vy)

    def clear_ephemeral(self):
        """清除临时钉住文字 (set_data 重建时调用)。"""
        for ti, _fy in self._pinned:
            if not getattr(ti, "_ephemeral", True):
                continue
            if ti.scene() is not None:
                ti.scene().removeItem(ti)
        self._pinned = [pt for pt in self._pinned
                        if not getattr(pt[0], "_ephemeral", True)]

    @property
    def help_item(self):
        return self._help_item

    @help_item.setter
    def help_item(self, value):
        self._help_item = value
