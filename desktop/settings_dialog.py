# -*- coding: utf-8 -*-
"""设置对话框: 编辑与 wyckoff.config.DEFAULT_SETTINGS 同键的界面设置 dict。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from wyckoff.config import PERIOD_OPTIONS, SCALE_OPTIONS

from . import theme

CHART_FONT_KEYS = ("font_size", "chart_font_size")


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(520, 420)
        self._s = dict(settings)

        tabs = QTabWidget(self)
        tabs.addTab(self._tab_general(), "基本")
        tabs.addTab(self._tab_chart(), "图表")
        tabs.addTab(self._tab_backtest(), "回测与风控")
        tabs.addTab(self._tab_ai(), "AI")
        tabs.addTab(self._tab_tts(), "语音播报")
        # 已有 Key 但开关未开 (如旧版本只填了 Key) → 打开设置即自动补齐开关,
        # 避免"填了 Key 却报 AI 不可用"的困惑; 用户仍可手动取消。
        if (self._s.get("ai_api_key") or "").strip():
            self.cb_interpret.setChecked(True)
            self.cb_falsify.setChecked(True)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(btns)
        self._collect_tabs(tabs)

    def _collect_tabs(self, tabs):
        self._pages = []
        for i in range(tabs.count()):
            w = tabs.widget(i)
            for g in w.findChildren(QGroupBox):
                self._pages.append(g)

    # ── 基本 ──
    def _tab_general(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)

        self.ed_default_load = QLineEdit(self._s.get("default_load", ""))
        self.ed_default_load.setPlaceholderText("启动时自动加载的股票代码, 留空不自动加载")
        f.addRow("默认载入:", self.ed_default_load)

        self.cb_theme = QComboBox()
        self.cb_theme.addItem("浅色", "light")
        self.cb_theme.addItem("深色 (护眼)", "dark")
        cur = self._s.get("theme", "light")
        idx = self.cb_theme.findData(cur)
        self.cb_theme.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_theme.setToolTip("深色低亮度主题, 减少夜间刺眼光线; 保存后立即生效")
        f.addRow("界面主题:", self.cb_theme)

        self.cb_scale = QComboBox()
        self.cb_scale.addItems(list(SCALE_OPTIONS.keys()))
        self.cb_scale.setCurrentText(self._s.get("default_scale", "日线"))
        f.addRow("默认周期:", self.cb_scale)

        self.cb_period = QComboBox()
        self.cb_period.addItems(list(PERIOD_OPTIONS.keys()))
        self.cb_period.setCurrentText(self._s.get("default_period", "近3年"))
        f.addRow("默认时间段:", self.cb_period)

        self.cb_maximize = QCheckBox("启动时最大化窗口")
        self.cb_maximize.setChecked(bool(self._s.get("start_maximized", True)))
        f.addRow("", self.cb_maximize)

        self.cb_auto = QCheckBox("定时刷新行情")
        self.cb_auto.setChecked(bool(self._s.get("auto_refresh", False)))
        self.cb_auto.toggled.connect(lambda on: self.sp_interval.setEnabled(on))
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(5, 3600)
        self.sp_interval.setValue(int(self._s.get("refresh_interval", 30)))
        self.sp_interval.setSuffix(" 秒")
        self.sp_interval.setEnabled(self.cb_auto.isChecked())
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.cb_auto)
        rl.addWidget(self.sp_interval)
        rl.addStretch(1)
        f.addRow("", row)

        self.cb_confirm = QCheckBox("基本面/资金流确认机制 (离线快速模式关闭)")
        self.cb_confirm.setChecked(bool(self._s.get("confirm_enabled", True)))
        f.addRow("", self.cb_confirm)

        self.cb_scan = QCheckBox("定时扫描自选股信号 (重算威科夫信号+更新准确度)")
        self.cb_scan.setChecked(bool(self._s.get("auto_scan", False)))
        self.cb_scan.toggled.connect(lambda on: self.sp_scan.setEnabled(on))
        self.sp_scan = QSpinBox()
        self.sp_scan.setRange(30, 43200)
        self.sp_scan.setValue(int(self._s.get("scan_interval", 3600)))
        self.sp_scan.setSuffix(" 秒")
        self.sp_scan.setEnabled(self.cb_scan.isChecked())
        srow = QWidget()
        sl = QHBoxLayout(srow)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.cb_scan)
        sl.addWidget(self.sp_scan)
        sl.addStretch(1)
        f.addRow("", srow)

        g = QGroupBox("界面尺寸")
        gf = QFormLayout(g)
        self.sp_watch_w = QSpinBox()
        self.sp_watch_w.setRange(120, 500)
        self.sp_watch_w.setValue(int(self._s.get("watch_width", 190)))
        gf.addRow("自选股栏宽:", self.sp_watch_w)
        self.sp_right_w = QSpinBox()
        self.sp_right_w.setRange(280, 1000)
        self.sp_right_w.setValue(int(self._s.get("right_width", 560)))
        gf.addRow("右侧结论栏宽:", self.sp_right_w)
        f.addRow(g)
        return w

    # ── 图表 ──
    def _tab_chart(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)

        self.cb_font = QComboBox()
        from PyQt6.QtGui import QFontDatabase
        families = sorted(QFontDatabase.families())
        cand = [x for x in families if x]
        self.cb_font.addItems(cand)
        pref = self._s.get("font_family", "")
        if pref in cand:
            self.cb_font.setCurrentText(pref)
        f.addRow("界面字体:", self.cb_font)

        self.sp_font = QSpinBox()
        self.sp_font.setRange(8, 20)
        self.sp_font.setValue(int(self._s.get("font_size", 11)))
        f.addRow("界面字号:", self.sp_font)

        self.sp_chart_font = QSpinBox()
        self.sp_chart_font.setRange(6, 18)
        self.sp_chart_font.setValue(int(self._s.get("chart_font_size", 11)))
        f.addRow("图表字号:", self.sp_chart_font)

        self.cb_waves = QCheckBox("绘制维斯波波段 (横盘/推进)")
        self.cb_waves.setChecked(bool(self._s.get("draw_waves", True)))
        f.addRow("", self.cb_waves)

        self.cb_locks = QCheckBox("绘制买卖点锁 (触发/失效标记)")
        self.cb_locks.setChecked(bool(self._s.get("draw_locks", True)))
        f.addRow("", self.cb_locks)
        return w

    # ── 回测与风控 ──
    def _tab_backtest(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)

        g = QGroupBox("回测参数")
        gf = QFormLayout(g)
        self.sp_bt_h = QSpinBox()
        self.sp_bt_h.setRange(1, 120)
        self.sp_bt_h.setValue(int(self._s.get("bt_horizon", 20)))
        gf.addRow("持有根数:", self.sp_bt_h)
        self.sp_bt_n = QSpinBox()
        self.sp_bt_n.setRange(1, 30)
        self.sp_bt_n.setValue(int(self._s.get("bt_min_n", 3)))
        gf.addRow("最少样本:", self.sp_bt_n)
        self.sp_bt_c = QDoubleSpinBox()
        self.sp_bt_c.setRange(0, 0.05)
        self.sp_bt_c.setSingleStep(0.001)
        self.sp_bt_c.setDecimals(3)
        self.sp_bt_c.setValue(float(self._s.get("bt_cost", 0.004)))
        gf.addRow("单边成本:", self.sp_bt_c)
        f.addRow(g)

        g3 = QGroupBox("分析灵敏度")
        g3f = QFormLayout(g3)
        self.cb_sensitivity = QComboBox()
        self.cb_sensitivity.addItem("fast (细枢轴, 信号多)", "fast")
        self.cb_sensitivity.addItem("normal (默认)", "normal")
        self.cb_sensitivity.addItem("safe (粗枢轴, 假信号少)", "safe")
        idx = self.cb_sensitivity.findData(self._s.get("pivot_sensitivity", "normal"))
        self.cb_sensitivity.setCurrentIndex(idx if idx >= 0 else 1)
        self.cb_sensitivity.setToolTip("ZigZag 枢轴邻域半径: fast=3 / normal=6 / safe=9\n"
                                       "safe 档枢轴更少更稳, 假信号少; fast 档更激进")
        g3f.addRow("枢轴灵敏度:", self.cb_sensitivity)
        self.cb_box_mode = QComboBox()
        self.cb_box_mode.addItem("百分比 (最新价×1.5%)", "pct")
        self.cb_box_mode.addItem("动态ATR (0.5×ATR14, 随波动率自适应)", "atr")
        idx = self.cb_box_mode.findData(self._s.get("pnf_box_mode", "pct"))
        self.cb_box_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_box_mode.setToolTip("P&F 格值来源: 固定百分比 vs 动态 ATR 格值\n"
                                    "ATR 模式随波动率放大/收窄格值, 滤波效果更贴合行情")
        g3f.addRow("点数图格值:", self.cb_box_mode)
        self.sp_atr_factor = QDoubleSpinBox()
        self.sp_atr_factor.setRange(0.1, 3.0)
        self.sp_atr_factor.setSingleStep(0.1)
        self.sp_atr_factor.setDecimals(2)
        self.sp_atr_factor.setValue(float(self._s.get("pnf_atr_factor", 0.5)))
        self.sp_atr_factor.setEnabled(self.cb_box_mode.currentData() == "atr")
        self.cb_box_mode.currentIndexChanged.connect(
            lambda _: self.sp_atr_factor.setEnabled(self.cb_box_mode.currentData() == "atr"))
        g3f.addRow("ATR格值系数:", self.sp_atr_factor)
        f.addRow(g3)

        g2 = QGroupBox("仓位风险管理")
        g2f = QFormLayout(g2)
        self.sp_portfolio = QDoubleSpinBox()
        self.sp_portfolio.setRange(0, 1e9)
        self.sp_portfolio.setDecimals(0)
        self.sp_portfolio.setValue(float(self._s.get("portfolio_value", 0)))
        self.sp_portfolio.setSuffix(" 元")
        g2f.addRow("总资金:", self.sp_portfolio)
        self.sp_risk = QDoubleSpinBox()
        self.sp_risk.setRange(0.001, 1.0)
        self.sp_risk.setSingleStep(0.005)
        self.sp_risk.setDecimals(3)
        self.sp_risk.setValue(float(self._s.get("risk_pct", 0.02)))
        g2f.addRow("单笔风险:", self.sp_risk)
        self.sp_rr = QDoubleSpinBox()
        self.sp_rr.setRange(0.5, 10.0)
        self.sp_rr.setSingleStep(0.1)
        self.sp_rr.setDecimals(1)
        self.sp_rr.setValue(float(self._s.get("risk_min_rr", 3.0)))
        g2f.addRow("最小盈亏比:", self.sp_rr)
        f.addRow(g2)
        return w

    # ── AI ──
    def _tab_ai(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)

        self.cb_falsify = QCheckBox("启用 AI 反向证伪")
        self.cb_falsify.setChecked(bool(self._s.get("ai_falsify_enabled", False)))
        f.addRow("", self.cb_falsify)

        self.cb_interpret = QCheckBox("启用 AI 报告解读")
        self.cb_interpret.setChecked(bool(self._s.get("ai_interpret_enabled", False)))
        f.addRow("", self.cb_interpret)

        self.ed_key = QLineEdit(self._s.get("ai_api_key", ""))
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("DeepSeek/OpenAI 兼容 API Key")
        # 填入 Key 时自动启用 AI 解读/证伪, 避免"填了 Key 却没开开关"的困惑
        self.ed_key.textChanged.connect(self._on_key_changed)
        f.addRow("API Key:", self.ed_key)
        self.ed_base = QLineEdit(self._s.get("ai_api_base", "https://api.deepseek.com"))
        f.addRow("API Base:", self.ed_base)

        self.ed_model = QLineEdit(self._s.get("ai_model", "deepseek-chat"))
        f.addRow("模型:", self.ed_model)

        tip = QLabel("AI 调用为可选层; 未配置 Key 时自动跳过, 不影响离线分析。"
                     "填 Key 后下方两个开关会自动启用, 可手动关闭。")
        tip.setStyleSheet(f"color:{theme.C_MUTED};")
        tip.setWordWrap(True)
        f.addRow("", tip)
        return w

    def _on_key_changed(self, text):
        """API Key 非空 → 自动勾选 AI 解读与证伪开关 (用户可再手动取消)。"""
        if text and text.strip():
            self.cb_interpret.setChecked(True)
            self.cb_falsify.setChecked(True)

    # ── 语音播报 ──
    def _tab_tts(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(12, 12, 12, 12)

        self.cb_tts = QCheckBox("启用解读语音播报 (TTS)")
        self.cb_tts.setChecked(bool(self._s.get("tts_enabled", False)))
        f.addRow("", self.cb_tts)

        self.cb_tts_auto = QCheckBox("分析完成后自动播报")
        self.cb_tts_auto.setChecked(bool(self._s.get("tts_auto", False)))
        f.addRow("", self.cb_tts_auto)

        self.cb_engine = QComboBox()
        self.cb_engine.addItems(["auto", "edge-tts", "pyttsx3"])
        self.cb_engine.setCurrentText(self._s.get("tts_engine", "auto"))
        f.addRow("引擎:", self.cb_engine)

        self.ed_voice = QLineEdit(self._s.get("tts_voice", "zh-CN-XiaoxiaoNeural"))
        f.addRow("音色:", self.ed_voice)

        self.sp_rate = QSpinBox()
        self.sp_rate.setRange(-50, 50)
        self.sp_rate.setValue(int(self._s.get("tts_rate", 0)))
        self.sp_rate.setSuffix(" %")
        f.addRow("语速:", self.sp_rate)

        self.sp_chars = QSpinBox()
        self.sp_chars.setRange(100, 6000)
        self.sp_chars.setValue(int(self._s.get("tts_max_chars", 3000)))
        f.addRow("最大播报字数:", self.sp_chars)
        return w

    # ── 结果 ──
    def settings(self):
        self._s.update({
            "default_load": self.ed_default_load.text().strip(),
            "default_scale": self.cb_scale.currentText(),
            "default_period": self.cb_period.currentText(),
            "start_maximized": self.cb_maximize.isChecked(),
            "auto_refresh": self.cb_auto.isChecked(),
            "refresh_interval": self.sp_interval.value(),
            "auto_scan": self.cb_scan.isChecked(),
            "scan_interval": self.sp_scan.value(),
            "confirm_enabled": self.cb_confirm.isChecked(),
            "watch_width": self.sp_watch_w.value(),
            "right_width": self.sp_right_w.value(),
            "font_family": self.cb_font.currentText(),
            "font_size": self.sp_font.value(),
            "chart_font_size": self.sp_chart_font.value(),
            "draw_waves": self.cb_waves.isChecked(),
            "draw_locks": self.cb_locks.isChecked(),
            "bt_horizon": self.sp_bt_h.value(),
            "bt_min_n": self.sp_bt_n.value(),
            "bt_cost": self.sp_bt_c.value(),
            "pivot_sensitivity": self.cb_sensitivity.currentData(),
            "pnf_box_mode": self.cb_box_mode.currentData(),
            "pnf_atr_factor": self.sp_atr_factor.value(),
            "portfolio_value": self.sp_portfolio.value(),
            "risk_pct": self.sp_risk.value(),
            "risk_min_rr": self.sp_rr.value(),
            "ai_falsify_enabled": self.cb_falsify.isChecked(),
            "ai_interpret_enabled": self.cb_interpret.isChecked(),
            "ai_api_key": self.ed_key.text().strip(),
            "ai_api_base": self.ed_base.text().strip(),
            "ai_model": self.ed_model.text().strip(),
            "tts_enabled": self.cb_tts.isChecked(),
            "tts_auto": self.cb_tts_auto.isChecked(),
            "tts_engine": self.cb_engine.currentText(),
            "tts_voice": self.ed_voice.text().strip(),
            "tts_rate": self.sp_rate.value(),
            "tts_max_chars": self.sp_chars.value(),
            "theme": self.cb_theme.currentData(),
        })
        return self._s
