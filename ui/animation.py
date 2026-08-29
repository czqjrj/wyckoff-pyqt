"""微动效工具: 面板折叠/展开、标签页切换淡入淡出等。"""
from PyQt6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation


class AnimationManager:
    """动画管理器: 统一管理界面微动效。"""

    def __init__(self, duration=200):
        self._duration = duration
        self._animations = []

    def animate_width(self, widget, start_width, end_width, finished_callback=None):
        """宽度动画 (用于面板折叠/展开)。"""
        anim = QPropertyAnimation(widget, b"maximumWidth")
        anim.setDuration(self._duration)
        anim.setStartValue(start_width)
        anim.setEndValue(end_width)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if finished_callback:
            anim.finished.connect(finished_callback)
        anim.finished.connect(lambda: self._animations.remove(anim) if anim in self._animations else None)
        self._animations.append(anim)
        anim.start()
        return anim

    def animate_opacity(self, widget, start_opacity, end_opacity, finished_callback=None):
        """透明度动画 (用于标签页切换淡入淡出)。"""
        # 确保 widget 有图形效果
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        effect = widget.graphicsEffect()
        if effect is None:
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(self._duration)
        anim.setStartValue(start_opacity)
        anim.setEndValue(end_opacity)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if finished_callback:
            anim.finished.connect(finished_callback)
        anim.finished.connect(lambda: self._animations.remove(anim) if anim in self._animations else None)
        self._animations.append(anim)
        anim.start()
        return anim

    def animate_splitter(self, splitter, index, start_size, end_size):
        """QSplitter 手柄位置动画 (使用 QPropertyAnimation)。"""
        # 创建一个可动画的对象来驱动 splitter 大小变化
        class _SplitterAnimObj(QObject):
            def __init__(self, parent=None):
                super().__init__(parent)
                self._val = 0.0
            @property
            def anim_val(self):
                return self._val
            @anim_val.setter
            def anim_val(self, v):
                self._val = v

        obj = _SplitterAnimObj()
        anim = QPropertyAnimation(obj, b"anim_val")
        anim.setDuration(self._duration)
        anim.setStartValue(float(start_size))
        anim.setEndValue(float(end_size))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        sizes = splitter.sizes()

        def on_value_change(v):
            new_sizes = list(sizes)
            new_sizes[index] = int(v)
            if index + 1 < len(new_sizes):
                new_sizes[index + 1] = sizes[index] + sizes[index + 1] - new_sizes[index]
            splitter.setSizes(new_sizes)

        anim.valueChanged.connect(on_value_change)
        anim.finished.connect(lambda: obj.deleteLater())
        self._animations.append(anim)
        anim.start()
        return anim


# 便捷函数
def fade_in(widget, duration=140, on_finished=None):
    """透明度淡入 (0→1), 结束后自动移除图形效果。

    非阻塞、不影响 isVisible() 同步语义; 用于停靠面板展开等微动效。
    注意: 动画对象须挂 parent 防止 Python 局部变量回收导致动画
    未起跑即被销毁、透明度永久卡在 0 (控件看似"消失")。
    """
    from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    eff = widget.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
    eff.setOpacity(0.0)
    anim = QPropertyAnimation(eff, b"opacity", widget)  # parent=widget 保活
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _cleanup():
        eff.setOpacity(1.0)
        widget.setGraphicsEffect(None)  # 移除效果, 恢复原生绘制性能
        anim.deleteLater()
        if on_finished:
            on_finished()
    anim.finished.connect(_cleanup)
    anim.start()
    return anim


def animate_panel_toggle(dock, visible, duration=200, on_finished=None):
    """面板折叠/展开动画。"""
    from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
    from PyQt6.QtWidgets import QDockWidget

    if not isinstance(dock, QDockWidget):
        return

    if visible:
        # 展开: 先显示, 然后动画宽度
        dock.setVisible(True)
        target_width = dock.width() or 200
        dock.setMaximumWidth(0)
        anim = QPropertyAnimation(dock, b"maximumWidth")
        anim.setDuration(duration)
        anim.setStartValue(0)
        anim.setEndValue(target_width)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    else:
        # 折叠: 动画宽度到 0, 然后隐藏
        target_width = dock.width()
        anim = QPropertyAnimation(dock, b"maximumWidth")
        anim.setDuration(duration)
        anim.setStartValue(target_width)
        anim.setEndValue(0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(lambda: dock.setVisible(False))

    if on_finished:
        anim.finished.connect(on_finished)
    anim.start()
    return anim


def animate_tab_switch(tab_widget, new_index, duration=150):
    """标签页切换淡入淡出动画。"""
    from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
    from PyQt6.QtWidgets import QGraphicsOpacityEffect

    old_widget = tab_widget.currentWidget()
    if old_widget is None:
        return

    # 旧 widget 淡出
    old_effect = old_widget.graphicsEffect()
    if old_effect is None:
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        old_effect = QGraphicsOpacityEffect(old_widget)
        old_widget.setGraphicsEffect(old_effect)

    # 新 widget 淡入
    new_widget = tab_widget.widget(new_index)
    if new_widget is None:
        return

    new_effect = new_widget.graphicsEffect()
    if new_effect is None:
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        new_effect = QGraphicsOpacityEffect(new_widget)
        new_widget.setGraphicsEffect(new_effect)

    # 并行动画
    group = QParallelAnimationGroup()

    anim_out = QPropertyAnimation(old_effect, b"opacity")
    anim_out.setDuration(duration)
    anim_out.setStartValue(1.0)
    anim_out.setEndValue(0.0)
    anim_out.setEasingCurve(QEasingCurve.Type.OutCubic)

    anim_in = QPropertyAnimation(new_effect, b"opacity")
    anim_in.setDuration(duration)
    anim_in.setStartValue(0.0)
    anim_in.setEndValue(1.0)
    anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

    group.addAnimation(anim_out)
    group.addAnimation(anim_in)

    def on_finished():
        tab_widget.setCurrentIndex(new_index)
        # 重置旧 widget 透明度
        old_effect.setOpacity(1.0)

    group.finished.connect(on_finished)
    group.start()
    return group
