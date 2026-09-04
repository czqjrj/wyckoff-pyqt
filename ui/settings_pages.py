"""设置对话框: 通用页面。"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import theme


class SettingsPage(QWidget):
    """设置页面基类: 提供统一的网格布局和滚动支持。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._s = settings
        self._row = 0

        # 主布局
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(theme.spacing("2"), theme.spacing("2"), theme.spacing("2"), theme.spacing("2"))
        self._main_layout.setSpacing(theme.spacing("2"))

        # 滚动区内容容器
        self._content = QWidget()
        self._content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(theme.spacing("2"), theme.spacing("2"), theme.spacing("2"), theme.spacing("2"))
        self._grid.setHorizontalSpacing(theme.spacing("3"))
        self._grid.setVerticalSpacing(theme.spacing("2"))
        self._grid.setColumnStretch(1, 1)  # 第 1 列 (控件) 可拉伸

        # 滚动区
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidget(self._content)
        self._scroll.setStyleSheet(
            "QScrollArea, QAbstractScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        self._main_layout.addWidget(self._scroll)

    def add_widget(self, label_text, widget, row=None, col=1, tooltip=""):
        """添加一行: 标签 + 控件。"""
        if row is None:
            row = self._row
        if tooltip:
            widget.setToolTip(tooltip)

        label = QLabel(label_text)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label.setStyleSheet(f"color: {theme.C_TEXT};")
        self._grid.addWidget(label, row, 0)
        self._grid.addWidget(widget, row, col)
        self._row = row + 1
        return widget

    def add_checkbox(self, text, key, row=None, tooltip=""):
        """添加复选框 (独占一行)。"""
        if row is None:
            row = self._row
        cb = QCheckBox(text)
        cb.setChecked(bool(self._s.get(key, False)))
        if tooltip:
            cb.setToolTip(tooltip)
        self._grid.addWidget(cb, row, 0, 1, 2)
        self._row = row + 1
        setattr(self, f"cb_{key}", cb)
        return cb

    def add_group(self, title):
        """添加分组框, 返回 (group, grid_layout)。"""
        group = QGroupBox(title)
        grid = QGridLayout(group)
        grid.setContentsMargins(theme.spacing("2"), theme.spacing("2"), theme.spacing("2"), theme.spacing("2"))
        grid.setHorizontalSpacing(theme.spacing("3"))
        grid.setVerticalSpacing(theme.spacing("2"))
        grid.setColumnStretch(1, 1)
        self._grid.addWidget(group, self._row, 0, 1, 2)
        self._row += 1
        return group, grid

    def _add_spacer(self, row=None):
        """添加垂直间距。"""
        if row is None:
            row = self._row
        spacer = QSpacerItem(0, theme.spacing("2"), QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._grid.addItem(spacer, row, 0)
        self._row = row + 1

    @property
    def settings(self):
        return self._s

    @property
    def row(self):
        return self._row


class GeneralPage(SettingsPage):
    """基本设置页面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._build_ui()

    def _build_ui(self):
        from wyckoff.config import PERIOD_OPTIONS, SCALE_OPTIONS

        # 默认载入
        self.ed_default_load = QLineEdit(self._s.get("default_load", ""))
        self.ed_default_load.setPlaceholderText("启动时自动加载的股票代码, 留空不自动加载")
        self.add_widget("默认载入:", self.ed_default_load)

        # 界面主题
        self.cb_theme = QComboBox()
        self.cb_theme.addItem("浅色", "light")
        self.cb_theme.addItem("深色 (护眼)", "dark")
        cur = self._s.get("theme", "light")
        idx = self.cb_theme.findData(cur)
        self.cb_theme.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_theme.setToolTip("深色低亮度主题, 减少夜间刺眼光线; 保存后立即生效")
        self.add_widget("界面主题:", self.cb_theme)

        # 默认周期
        self.cb_scale = QComboBox()
        self.cb_scale.addItems(list(SCALE_OPTIONS.keys()))
        self.cb_scale.setCurrentText(self._s.get("default_scale", "日线"))
        self.add_widget("默认周期:", self.cb_scale)

        # 默认时间段
        self.cb_period = QComboBox()
        self.cb_period.addItems(list(PERIOD_OPTIONS.keys()))
        self.cb_period.setCurrentText(self._s.get("default_period", "近3年"))
        self.add_widget("默认时间段:", self.cb_period)

        # 启动时最大化
        self.cb_maximize = QCheckBox("启动时最大化窗口")
        self.cb_maximize.setChecked(bool(self._s.get("start_maximized", True)))
        self._grid.addWidget(self.cb_maximize, self._row, 0, 1, 2)
        self._row += 1

        # 启动时不分析任何股票
        self.cb_no_startup_analysis = QCheckBox("启动时不分析任何股票")
        self.cb_no_startup_analysis.setChecked(
            bool(self._s.get("startup_no_analysis", True)))
        self.cb_no_startup_analysis.setToolTip(
            "勾选后启动时不会自动加载/分析上次或默认股票, 也不做自选股首扫。")
        self._grid.addWidget(self.cb_no_startup_analysis, self._row, 0, 1, 2)
        self._row += 1

        # 启动自动显示: 综合选股 / 校准中心 / 今日入场点 (均为懒创建 Tab)
        lab = QLabel("启动时自动显示:")
        lab.setStyleSheet(f"color:{theme.C_MUTED};")
        self._grid.addWidget(lab, self._row, 0, 1, 2)
        self._row += 1
        self.cb_auto_show = {}
        for key, text in (("auto_show_screener", "综合选股"),
                          ("auto_show_calib", "校准中心"),
                          ("auto_show_entries", "今日入场点")):
            cb = QCheckBox(text)
            cb.setChecked(bool(self._s.get(key, False)))
            self._grid.addWidget(cb, self._row, 0, 1, 2)
            self.cb_auto_show[key] = cb
            self._row += 1

        self._add_spacer()

        # 定时刷新行情
        self.cb_auto = QCheckBox("定时刷新行情")
        self.cb_auto.setChecked(bool(self._s.get("auto_refresh", False)))
        self._grid.addWidget(self.cb_auto, self._row, 0)
        self._row += 1

        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(5, 3600)
        self.sp_interval.setValue(int(self._s.get("refresh_interval", 30)))
        self.sp_interval.setSuffix(" 秒")
        self.sp_interval.setEnabled(self.cb_auto.isChecked())
        self.cb_auto.toggled.connect(self.sp_interval.setEnabled)
        self._grid.addWidget(self.sp_interval, self._row - 1, 1)
        self._row += 1

        # 基本面/资金流确认机制
        self.cb_confirm = QCheckBox("基本面/资金流确认机制 (离线快速模式关闭)")
        self.cb_confirm.setChecked(bool(self._s.get("confirm_enabled", True)))
        self._grid.addWidget(self.cb_confirm, self._row, 0, 1, 2)
        self._row += 1

        # 定时扫描自选股
        self.cb_scan = QCheckBox("定时扫描自选股信号 (重算威科夫信号+更新准确度)")
        self.cb_scan.setChecked(bool(self._s.get("auto_scan", False)))
        self._grid.addWidget(self.cb_scan, self._row, 0)
        self._row += 1

        self.sp_scan = QSpinBox()
        self.sp_scan.setRange(30, 43200)
        self.sp_scan.setValue(int(self._s.get("scan_interval", 3600)))
        self.sp_scan.setSuffix(" 秒")
        self.sp_scan.setEnabled(self.cb_scan.isChecked())
        self.cb_scan.toggled.connect(self.sp_scan.setEnabled)
        self._grid.addWidget(self.sp_scan, self._row - 1, 1)
        self._row += 1

        # 校准数据自动同步
        self.cb_autosync = QCheckBox("校准数据变更后自动同步 (需先在校准中心配置同步仓库)")
        self.cb_autosync.setChecked(bool(self._s.get("auto_sync", False)))
        self._grid.addWidget(self.cb_autosync, self._row, 0)
        self._row += 1

        self.sp_sync_debounce = QSpinBox()
        self.sp_sync_debounce.setRange(15, 3600)
        self.sp_sync_debounce.setValue(int(self._s.get("sync_debounce", 60)))
        self.sp_sync_debounce.setSuffix(" 秒")
        self.sp_sync_debounce.setEnabled(self.cb_autosync.isChecked())
        self.cb_autosync.toggled.connect(self.sp_sync_debounce.setEnabled)
        self._grid.addWidget(self.sp_sync_debounce, self._row - 1, 1)
        self._row += 1

        self._add_spacer()

        # 界面尺寸
        group, grid = self.add_group("界面尺寸")
        row = 0
        self.sp_watch_w = QSpinBox()
        self.sp_watch_w.setRange(120, 500)
        self.sp_watch_w.setValue(int(self._s.get("watch_width", 190)))
        grid.addWidget(QLabel("自选股栏宽:"), row, 0)
        grid.addWidget(self.sp_watch_w, row, 1)
        row += 1

        self.sp_right_w = QSpinBox()
        self.sp_right_w.setRange(280, 1000)
        self.sp_right_w.setValue(int(self._s.get("right_width", 560)))
        grid.addWidget(QLabel("右侧结论栏宽:"), row, 0)
        grid.addWidget(self.sp_right_w, row, 1)
        row += 1

        self._add_spacer()

        # 界面字体
        group, grid = self.add_group("界面字体")
        row = 0
        fams = [x for x in QFontDatabase.families() if x]
        self.cb_font = QFontComboBox()
        pref = str(self._s.get("font_family", "") or "")
        if pref in fams:
            self.cb_font.setCurrentText(pref)
        else:
            self.cb_font.setCurrentText(theme.ui_font_family())
        self.cb_font.setToolTip("全局界面字体, 影响所有栏目/面板/对话框; 留为默认即自动挑选")
        grid.addWidget(QLabel("界面字体:"), row, 0)
        grid.addWidget(self.cb_font, row, 1)
        row += 1

        self.sp_font = QSpinBox()
        self.sp_font.setRange(8, 20)
        self.sp_font.setValue(int(self._s.get("font_size", 12)))
        self.sp_font.setSuffix(" pt")
        self.sp_font.setToolTip("全局界面基准字号 (工具栏/表格/按钮/列表等)")
        grid.addWidget(QLabel("界面字号:"), row, 0)
        grid.addWidget(self.sp_font, row, 1)
        row += 1

        self.sp_watch_font = QSpinBox()
        self.sp_watch_font.setRange(8, 18)
        self.sp_watch_font.setValue(int(self._s.get("watch_font_size", 12)))
        self.sp_watch_font.setSuffix(" pt")
        self.sp_watch_font.setToolTip("仅左侧自选股卡片列表的字号")
        grid.addWidget(QLabel("自选股栏字号:"), row, 0)
        grid.addWidget(self.sp_watch_font, row, 1)
        row += 1

        self.sp_text_font = QSpinBox()
        self.sp_text_font.setRange(7, 18)
        self.sp_text_font.setValue(int(self._s.get("text_font_size", 11)))
        self.sp_text_font.setSuffix(" pt")
        self.sp_text_font.setToolTip("右侧分析结论 / AI 解读的文字字号 (与 A-/A+ 联动)")
        grid.addWidget(QLabel("结论面板字号:"), row, 0)
        grid.addWidget(self.sp_text_font, row, 1)
        row += 1

        # 账户私有数据同步 (同账户多设备一致: UI布局/自选/候选/笔记/组合/模拟盘)
        group, grid = self.add_group("账户登录与同步")
        row = 0
        self._profile_login_label = QLabel("")
        self._profile_login_label.setWordWrap(True)
        self._profile_login_label.setStyleSheet(f"color:{theme.C_MUTED};")
        grid.addWidget(self._profile_login_label, row, 0, 1, 2)
        row += 1

        self.ed_profile_user = QLineEdit()
        self.ed_profile_user.setPlaceholderText("账户名")
        grid.addWidget(QLabel("账户:"), row, 0)
        grid.addWidget(self.ed_profile_user, row, 1)
        row += 1

        self.ed_profile_pass = QLineEdit()
        self.ed_profile_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_profile_pass.setPlaceholderText("密码 (至少 6 位)")
        grid.addWidget(QLabel("密码:"), row, 0)
        grid.addWidget(self.ed_profile_pass, row, 1)
        row += 1

        self.cb_profile_sync = QCheckBox("启用账户私有数据同步")
        self.cb_profile_sync.setChecked(bool(self._s.get("profile_sync", False)))
        grid.addWidget(self.cb_profile_sync, row, 0, 1, 2)
        row += 1

        btns = QHBoxLayout()
        self._profile_login_btn = QPushButton("登录")
        self._profile_login_btn.setToolTip("校验密码并登记为该账户登录态")
        self._profile_login_btn.clicked.connect(self._on_profile_login)
        btns.addWidget(self._profile_login_btn)
        self._profile_register_btn = QPushButton("注册")
        self._profile_register_btn.setToolTip("注册新账户 (云端登记用户名密码)")
        self._profile_register_btn.clicked.connect(self._on_profile_register)
        btns.addWidget(self._profile_register_btn)
        self._profile_logout_btn = QPushButton("退出")
        self._profile_logout_btn.setToolTip("清除本机登录态 (保留账户云端档案)")
        btns.addWidget(self._profile_logout_btn)
        self._profile_sync_btn = QPushButton("立即同步")
        self._profile_sync_btn.setToolTip("云端多设备同步: 拉取远端 → 与本机合并 (逐条目+删除) → 写回本机 → 推送")
        self._profile_sync_btn.clicked.connect(self._on_profile_sync_now)
        btns.addWidget(self._profile_sync_btn)
        self._profile_sync_status = QLabel("")
        self._profile_sync_status.setWordWrap(True)
        self._profile_sync_status.setStyleSheet(f"color:{theme.C_MUTED};")
        btns.addWidget(self._profile_sync_status, 1)
        grid.addLayout(btns, row, 0, 1, 2)
        row += 1

        self._refresh_profile_login()
        self._add_spacer()

    def _refresh_profile_login(self):
        """根据账户档案刷新登录态显示与输入框状态。"""
        try:
            import wyckoff.account as acc
            st = acc.status()
            if st.get("logged_in"):
                self._profile_login_label.setText(
                    f"已登录: {st['current']}  (云端同步)")
                self.ed_profile_user.setText(st["current"])
                self.ed_profile_user.setEnabled(False)
                self.ed_profile_pass.setEnabled(False)
                self._profile_login_btn.setEnabled(False)
                self._profile_register_btn.setEnabled(False)
                self._profile_logout_btn.setEnabled(True)
            else:
                self._profile_login_label.setText("未登录。登录后可在多台设备间通过云端同步账户私有数据。")
                self.ed_profile_user.setEnabled(True)
                self.ed_profile_pass.setEnabled(True)
                self._profile_login_btn.setEnabled(True)
                self._profile_register_btn.setEnabled(True)
                self._profile_logout_btn.setEnabled(False)
        except Exception:  # noqa: BLE001
            self._profile_login_label.setText("未登录")

    def _on_profile_login(self):
        """登录: 校验密码并登记账户登录态 (走云端后端)。"""
        try:
            import wyckoff.account as acc
            user = self.ed_profile_user.text().strip()
            ok, msg = acc.login(user, self.ed_profile_pass.text())
            if ok:
                self._profile_sync_status.setText(msg)
            else:
                self._profile_sync_status.setText(f"登录失败: {msg}")
            self._refresh_profile_login()
        except Exception as e:  # noqa: BLE001
            self._profile_sync_status.setText(f"登录异常: {e}")

    def _on_profile_register(self):
        """注册新账户 (用户名+密码) 并自动登录。"""
        try:
            import wyckoff.account as acc
            user = self.ed_profile_user.text().strip()
            ok, msg = acc.register(user, self.ed_profile_pass.text())
            if ok:
                self._profile_sync_status.setText(msg)
            else:
                self._profile_sync_status.setText(f"注册失败: {msg}")
            self._refresh_profile_login()
        except Exception as e:  # noqa: BLE001
            self._profile_sync_status.setText(f"注册异常: {e}")

    def _on_profile_logout(self):
        try:
            import wyckoff.account as acc
            _, msg = acc.logout()
            self._profile_sync_status.setText(msg)
            self._refresh_profile_login()
        except Exception as e:  # noqa: BLE001
            self._profile_sync_status.setText(f"退出异常: {e}")

    def _on_profile_sync_now(self):
        """立即执行账户私有数据同步 (低频操作, 同步执行并刷新状态行)。"""
        try:
            import wyckoff.account as acc
            import wyckoff.cloud_db as cdb
            import wyckoff.profile_sync as psync
            if cdb.enabled():
                # 云端后端: sync_once 同时覆盖首次同步与增量合并
                result = psync.sync_once()
            elif not psync.status().get("configured"):
                result = psync.setup("")
            else:
                result = psync.sync_once()
            if result.get("ok"):
                self._s["profile_sync"] = True
                self.cb_profile_sync.setChecked(True)
                from wyckoff.storage import save_settings
                save_settings(dict(self._s))
                self._profile_sync_status.setText("同步成功")
            else:
                self._profile_sync_status.setText(
                    f"同步失败: {result.get('error', '未知错误')}")
        except Exception as e:  # noqa: BLE001
            self._profile_sync_status.setText(f"同步异常: {e}")

    def collect(self):
        self._s.update({
            "default_load": self.ed_default_load.text().strip(),
            "default_scale": self.cb_scale.currentText(),
            "default_period": self.cb_period.currentText(),
            "start_maximized": self.cb_maximize.isChecked(),
            "startup_no_analysis": self.cb_no_startup_analysis.isChecked(),
            "auto_show_screener": self.cb_auto_show["auto_show_screener"].isChecked(),
            "auto_show_calib": self.cb_auto_show["auto_show_calib"].isChecked(),
            "auto_show_entries": self.cb_auto_show["auto_show_entries"].isChecked(),
            "auto_refresh": self.cb_auto.isChecked(),
            "refresh_interval": self.sp_interval.value(),
            "auto_scan": self.cb_scan.isChecked(),
            "scan_interval": self.sp_scan.value(),
            "auto_sync": self.cb_autosync.isChecked(),
            "sync_debounce": self.sp_sync_debounce.value(),
            "profile_sync": self.cb_profile_sync.isChecked(),
            "confirm_enabled": self.cb_confirm.isChecked(),
            "watch_width": self.sp_watch_w.value(),
            "right_width": self.sp_right_w.value(),
            "font_family": self.cb_font.currentText(),
            "font_size": self.sp_font.value(),
            "watch_font_size": self.sp_watch_font.value(),
            "text_font_size": self.sp_text_font.value(),
            "theme": self.cb_theme.currentData(),
        })

    # (_add_spacer 此前在子类原样重复定义了基类实现, 已删除, 直接用基类版本。)


class ChartPage(SettingsPage):
    """图表设置页面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._build_ui()

    def _build_ui(self):
        # 图表字号
        self.sp_chart_font = QSpinBox()
        self.sp_chart_font.setRange(6, 18)
        self.sp_chart_font.setValue(int(self._s.get("chart_font_size", 11)))
        self.add_widget("图表字号:", self.sp_chart_font)

        # 绘制波浪
        self.cb_waves = QCheckBox("绘制维斯波波段 (横盘/推进)")
        self.cb_waves.setChecked(bool(self._s.get("draw_waves", True)))
        self._grid.addWidget(self.cb_waves, self._row, 0, 1, 2)
        self._row += 1

        # 绘制锁点
        self.cb_locks = QCheckBox("绘制买卖点锁 (触发/失效标记)")
        self.cb_locks.setChecked(bool(self._s.get("draw_locks", True)))
        self._grid.addWidget(self.cb_locks, self._row, 0, 1, 2)
        self._row += 1

        # 指标/资金视图默认视野 (0=全幅, 与 K线/P&F 一致默认聚焦最近 N 根)
        group, grid = self.add_group("指标 / 资金视图")
        row = 0
        self.sp_ind_bars = QSpinBox()
        self.sp_ind_bars.setRange(0, 2000)
        self.sp_ind_bars.setValue(int(self._s.get("ind_default_bars", 250)))
        self.sp_ind_bars.setSuffix(" 根")
        self.sp_ind_bars.setSpecialValueText("全幅")
        self.sp_ind_bars.setToolTip("技术指标加载后默认显示的柱数; 0=全幅。\n"
                                    "Home/复位视图/双击 随时可回全幅。")
        grid.addWidget(QLabel("技术指标默认柱数:"), row, 0)
        grid.addWidget(self.sp_ind_bars, row, 1)
        row += 1

        self.sp_mkt_bars = QSpinBox()
        self.sp_mkt_bars.setRange(0, 2000)
        self.sp_mkt_bars.setValue(int(self._s.get("mkt_default_bars", 120)))
        self.sp_mkt_bars.setSuffix(" 根")
        self.sp_mkt_bars.setSpecialValueText("全幅")
        self.sp_mkt_bars.setToolTip("资金透视各日期面板加载后默认显示的柱数; 0=全幅。")
        grid.addWidget(QLabel("资金透视默认柱数:"), row, 0)
        grid.addWidget(self.sp_mkt_bars, row, 1)
        row += 1

    def collect(self):
        self._s.update({
            "chart_font_size": self.sp_chart_font.value(),
            "draw_waves": self.cb_waves.isChecked(),
            "draw_locks": self.cb_locks.isChecked(),
            "ind_default_bars": self.sp_ind_bars.value(),
            "mkt_default_bars": self.sp_mkt_bars.value(),
        })


class BacktestPage(SettingsPage):
    """回测与风控页面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._build_ui()

    def _build_ui(self):
        # 回测参数
        group, grid = self.add_group("回测参数")
        row = 0
        self.sp_bt_h = QSpinBox()
        self.sp_bt_h.setRange(1, 120)
        self.sp_bt_h.setValue(int(self._s.get("bt_horizon", 20)))
        grid.addWidget(QLabel("持有根数:"), row, 0)
        grid.addWidget(self.sp_bt_h, row, 1)
        row += 1

        self.sp_bt_n = QSpinBox()
        self.sp_bt_n.setRange(1, 30)
        self.sp_bt_n.setValue(int(self._s.get("bt_min_n", 3)))
        grid.addWidget(QLabel("最少样本:"), row, 0)
        grid.addWidget(self.sp_bt_n, row, 1)
        row += 1

        self.sp_bt_c = QDoubleSpinBox()
        self.sp_bt_c.setRange(0, 0.05)
        self.sp_bt_c.setSingleStep(0.001)
        self.sp_bt_c.setDecimals(3)
        self.sp_bt_c.setValue(float(self._s.get("bt_cost", 0.004)))
        grid.addWidget(QLabel("单边成本:"), row, 0)
        grid.addWidget(self.sp_bt_c, row, 1)
        row += 1

        # 分析灵敏度
        group, grid = self.add_group("分析灵敏度")
        row = 0
        self.cb_sensitivity = QComboBox()
        self.cb_sensitivity.addItem("fast (细枢轴, 信号多)", "fast")
        self.cb_sensitivity.addItem("normal (默认)", "normal")
        self.cb_sensitivity.addItem("safe (粗枢轴, 假信号少)", "safe")
        idx = self.cb_sensitivity.findData(self._s.get("pivot_sensitivity", "normal"))
        self.cb_sensitivity.setCurrentIndex(idx if idx >= 0 else 1)
        self.cb_sensitivity.setToolTip("ZigZag 枢轴邻域半径: fast=3 / normal=6 / safe=9\n"
                                       "safe 档枢轴更少更稳, 假信号少; fast 档更激进")
        grid.addWidget(QLabel("枢轴灵敏度:"), row, 0)
        grid.addWidget(self.cb_sensitivity, row, 1)
        row += 1

        self.cb_box_mode = QComboBox()
        self.cb_box_mode.addItem("百分比 (最新价×1.5%)", "pct")
        self.cb_box_mode.addItem("动态ATR (0.5×ATR14, 随波动率自适应)", "atr")
        idx = self.cb_box_mode.findData(self._s.get("pnf_box_mode", "pct"))
        self.cb_box_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_box_mode.setToolTip("P&F 格值来源: 固定百分比 vs 动态 ATR 格值\n"
                                    "ATR 模式随波动率放大/收窄格值, 滤波效果更贴合行情")
        grid.addWidget(QLabel("点数图格值:"), row, 0)
        grid.addWidget(self.cb_box_mode, row, 1)
        row += 1

        self.sp_atr_factor = QDoubleSpinBox()
        self.sp_atr_factor.setRange(0.1, 3.0)
        self.sp_atr_factor.setSingleStep(0.1)
        self.sp_atr_factor.setDecimals(2)
        self.sp_atr_factor.setValue(float(self._s.get("pnf_atr_factor", 0.5)))
        self.sp_atr_factor.setEnabled(self.cb_box_mode.currentData() == "atr")
        self.cb_box_mode.currentIndexChanged.connect(
            lambda _: self.sp_atr_factor.setEnabled(self.cb_box_mode.currentData() == "atr"))
        grid.addWidget(QLabel("ATR格值系数:"), row, 0)
        grid.addWidget(self.sp_atr_factor, row, 1)
        row += 1

        # 仓位风险管理
        group, grid = self.add_group("仓位风险管理")
        row = 0
        self.sp_portfolio = QDoubleSpinBox()
        self.sp_portfolio.setRange(0, 1e9)
        self.sp_portfolio.setDecimals(0)
        self.sp_portfolio.setValue(float(self._s.get("portfolio_value", 0)))
        self.sp_portfolio.setSuffix(" 元")
        grid.addWidget(QLabel("总资金:"), row, 0)
        grid.addWidget(self.sp_portfolio, row, 1)
        row += 1

        self.sp_risk = QDoubleSpinBox()
        self.sp_risk.setRange(0.001, 1.0)
        self.sp_risk.setSingleStep(0.005)
        self.sp_risk.setDecimals(3)
        self.sp_risk.setValue(float(self._s.get("risk_pct", 0.02)))
        grid.addWidget(QLabel("单笔风险:"), row, 0)
        grid.addWidget(self.sp_risk, row, 1)
        row += 1

        self.sp_rr = QDoubleSpinBox()
        self.sp_rr.setRange(0.5, 10.0)
        self.sp_rr.setSingleStep(0.1)
        self.sp_rr.setDecimals(1)
        self.sp_rr.setValue(float(self._s.get("risk_min_rr", 3.0)))
        grid.addWidget(QLabel("最小盈亏比:"), row, 0)
        grid.addWidget(self.sp_rr, row, 1)
        row += 1

    def collect(self):
        self._s.update({
            "bt_horizon": self.sp_bt_h.value(),
            "bt_min_n": self.sp_bt_n.value(),
            "bt_cost": self.sp_bt_c.value(),
            "pivot_sensitivity": self.cb_sensitivity.currentData(),
            "pnf_box_mode": self.cb_box_mode.currentData(),
            "pnf_atr_factor": self.sp_atr_factor.value(),
            "portfolio_value": self.sp_portfolio.value(),
            "risk_pct": self.sp_risk.value(),
            "risk_min_rr": self.sp_rr.value(),
        })


class AIPage(SettingsPage):
    """AI 设置页面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._build_ui()

    def _build_ui(self):
        self.cb_falsify = QCheckBox("启用 AI 反向证伪")
        self.cb_falsify.setChecked(bool(self._s.get("ai_falsify_enabled", False)))
        self._grid.addWidget(self.cb_falsify, self._row, 0, 1, 2)
        self._row += 1

        self.cb_interpret = QCheckBox("启用 AI 报告解读")
        self.cb_interpret.setChecked(bool(self._s.get("ai_interpret_enabled", False)))
        self._grid.addWidget(self.cb_interpret, self._row, 0, 1, 2)
        self._row += 1

        self.ed_key = QLineEdit(self._s.get("ai_api_key", ""))
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("DeepSeek/OpenAI 兼容 API Key")
        self.ed_key.textChanged.connect(self._on_key_changed)
        self.add_widget("API Key:", self.ed_key)

        self.ed_base = QLineEdit(self._s.get("ai_api_base", "https://api.deepseek.com"))
        self.add_widget("API Base:", self.ed_base)

        self.ed_model = QLineEdit(self._s.get("ai_model", "deepseek-chat"))
        self.add_widget("模型:", self.ed_model)

        from PyQt6.QtWidgets import QLabel
        tip = QLabel("AI 调用为可选层; 未配置 Key 时自动跳过, 不影响离线分析。"
                     "填 Key 后下方两个开关会自动启用, 可手动关闭。")
        tip.setStyleSheet(f"color:{theme.C_MUTED};")
        tip.setWordWrap(True)
        self._grid.addWidget(tip, self._row, 0, 1, 2)
        self._row += 1

    def _on_key_changed(self, text):
        """API Key 非空 → 自动勾选 AI 解读与证伪开关 (用户可再手动取消)。"""
        if text and text.strip():
            self.cb_interpret.setChecked(True)
            self.cb_falsify.setChecked(True)

    def collect(self):
        self._s.update({
            "ai_falsify_enabled": self.cb_falsify.isChecked(),
            "ai_interpret_enabled": self.cb_interpret.isChecked(),
            "ai_api_key": self.ed_key.text().strip(),
            "ai_api_base": self.ed_base.text().strip(),
            "ai_model": self.ed_model.text().strip(),
        })


class TTSPage(SettingsPage):
    """语音播报页面。"""

    def __init__(self, settings, parent=None):
        super().__init__(settings, parent)
        self._build_ui()

    def _build_ui(self):
        self.cb_tts = QCheckBox("启用解读语音播报 (TTS)")
        self.cb_tts.setChecked(bool(self._s.get("tts_enabled", False)))
        self._grid.addWidget(self.cb_tts, self._row, 0, 1, 2)
        self._row += 1

        self.cb_tts_auto = QCheckBox("分析完成后自动播报")
        self.cb_tts_auto.setChecked(bool(self._s.get("tts_auto", False)))
        self._grid.addWidget(self.cb_tts_auto, self._row, 0, 1, 2)
        self._row += 1

        self.cb_engine = QComboBox()
        self.cb_engine.addItems(["auto", "edge-tts", "pyttsx3"])
        self.cb_engine.setCurrentText(self._s.get("tts_engine", "auto"))
        self.add_widget("引擎:", self.cb_engine)

        self.ed_voice = QLineEdit(self._s.get("tts_voice", "zh-CN-XiaoxiaoNeural"))
        self.add_widget("音色:", self.ed_voice)

        self.sp_rate = QSpinBox()
        self.sp_rate.setRange(-50, 50)
        self.sp_rate.setValue(int(self._s.get("tts_rate", 0)))
        self.sp_rate.setSuffix(" %")
        self.add_widget("语速:", self.sp_rate)

        self.sp_chars = QSpinBox()
        self.sp_chars.setRange(100, 6000)
        self.sp_chars.setValue(int(self._s.get("tts_max_chars", 3000)))
        self.add_widget("最大播报字数:", self.sp_chars)

    def collect(self):
        self._s.update({
            "tts_enabled": self.cb_tts.isChecked(),
            "tts_auto": self.cb_tts_auto.isChecked(),
            "tts_engine": self.cb_engine.currentText(),
            "tts_voice": self.ed_voice.text().strip(),
            "tts_rate": self.sp_rate.value(),
            "tts_max_chars": self.sp_chars.value(),
        })
