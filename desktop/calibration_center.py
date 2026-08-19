# -*- coding: utf-8 -*-
"""校准中心: 分层校准管线的观测窗口 (总览 / 信号校准 / 模型校准 / 分析校准)。

数据全部来自 wyckoff 包:
  - accuracy / signal_accuracy / calibration: 各层评估样本与胜率库;
  - validation: Rank IC / Bootstrap CI / 置换检验 / OOS 验证;
  - online_model: L4 特征级在线 LR 模型 (系数/样本外 AUC/接管状态)。
"""
import statistics
from collections import OrderedDict

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QScrollArea, QSplitter, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from wyckoff.accuracy import (
    HORIZONS, accuracy_stats, export_accuracy, load_accuracy,
    run_auto_accuracy_eval,
)
from wyckoff.calibration import calibration_status, record_calibration
from wyckoff.config import _PHASE_STYLE, event_dir
from wyckoff.signal_accuracy import (
    _fmt_stats, export_signals, load_signals, run_auto_signal_eval,
    signal_stats,
)
from wyckoff.storage import (
    build_feedback_record, feedback_key, load_feedback, save_feedback,
)

from . import theme


class _EvalThread(QThread):
    finished = pyqtSignal()

    def run(self):
        try:
            run_auto_accuracy_eval()
        except Exception:
            pass
        try:
            run_auto_signal_eval()
        except Exception:
            pass
        self.finished.emit()


class _AiVerifyThread(QThread):
    finished = pyqtSignal(object)

    def __init__(self, records, settings, parent=None):
        super().__init__(parent)
        self._records = records
        self._settings = settings

    def run(self):
        from wyckoff.validation import validation_ai_interpret
        try:
            self.finished.emit(validation_ai_interpret(self._records, self._settings))
        except Exception as e:
            self.finished.emit({"ai": None, "rule": f"AI 解读失败: {e}"})


class _RetrainThread(QThread):
    """后台重训 L4 在线模型 (训练涉及 numpy/sklearn, 放线程避免卡界面)。"""
    finished = pyqtSignal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work

    def run(self):
        try:
            self.finished.emit(self._work())
        except Exception as e:
            self.finished.emit({"error": str(e)})


# ── 样式辅助 ──

def _card(parent=None, accent=None):
    """创建一个卡片容器 (panel背景 + 圆角 + 可选顶部色条)。"""
    f = QFrame(parent)
    style = (f"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
             f"border-radius:6px;")
    f.setStyleSheet(style)
    return f


def _card_title(text, size="11pt"):
    lb = QLabel(text)
    lb.setStyleSheet(f"font-weight:bold;font-size:{size};color:{theme.C_TEXT};")
    return lb


def _card_value(text, color=None):
    lb = QLabel(text)
    c = color or theme.C_TEXT
    lb.setStyleSheet(f"font-size:18pt;font-weight:bold;color:{c};")
    return lb


def _card_sub(text):
    lb = QLabel(text)
    lb.setStyleSheet(f"font-size:9pt;color:{theme.C_MUTED};")
    return lb


class CalibrationCenter(QDialog):
    """校准中心: 分层校准 (L1 收缩 / L2 OOS / L4 模型) 的观测与操作入口。"""

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("校准中心")
        self.resize(1200, 780)
        self.setMinimumSize(900, 560)
        self._settings = settings or {}
        self._fb_bands = []
        self._last_segs = None
        self._last_symbol = ""
        self._last_datalen = 700
        self._last_scale = 240
        self._last_df = None
        self._ai_tts_playing = False

        # 全局样式: 统一铺底色, 消除白色空隙
        _bg = theme.C_BG
        _panel = theme.C_PANEL
        _border = theme.C_BORDER
        _btn_bg = theme.C.get("btn", _panel)
        _btn_hv = theme.C.get("btn_hover", _btn_bg)
        self.setStyleSheet(f"""
            CalibrationCenter {{ background:{_bg}; }}
            QScrollArea {{ background:{_bg}; border:none; }}
            QScrollArea > QWidget > QWidget {{ background:{_bg}; }}
            QSplitter::handle {{ background:{_border}; height:3px; }}
            QTabWidget::pane {{ border:1px solid {_border}; background:{_bg}; }}
            QComboBox {{ background:{_panel}; border:1px solid {_border};
                         border-radius:3px; padding:2px 6px; }}
            QPushButton {{ background:{_btn_bg}; border:1px solid {_border};
                           border-radius:4px; padding:4px 10px; }}
            QPushButton:hover {{ background:{_btn_hv}; }}
            QFrame#row {{ background:{_panel}; border:1px solid {_border};
                          border-radius:4px; }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ── 顶栏 ──
        head = QHBoxLayout()
        head.setSpacing(8)
        strip = QLabel()
        strip.setFixedSize(5, 20)
        strip.setStyleSheet(f"background:{theme.C_ACCENT};border-radius:2px;")
        head.addWidget(strip)
        t = QLabel("校准中心")
        t.setStyleSheet("font-weight:bold;font-size:14pt;")
        head.addWidget(t)
        t2 = QLabel("分层校准  ·  L1 贝叶斯收缩  ·  L4 在线模型  ·  每日自动收集  ·  定期提醒校准")
        t2.setStyleSheet(f"color:{theme.C_MUTED};font-size:10pt;")
        head.addWidget(t2)
        head.addStretch(1)
        btn_eval = QPushButton("⚡ 立即评估")
        btn_eval.setToolTip("拉取最新行情, 评估所有待评估记录")
        btn_eval.clicked.connect(self.on_eval)
        head.addWidget(btn_eval)
        btn_calib = QPushButton("📐 校准基线")
        btn_calib.setToolTip("记录当前样本量为校准基线")
        btn_calib.clicked.connect(self.on_calibrate)
        head.addWidget(btn_calib)
        btn_export = QPushButton("📤 导出全部")
        btn_export.clicked.connect(self.on_export_all)
        head.addWidget(btn_export)
        btn_clear = QPushButton("🗑 清空")
        btn_clear.setToolTip("清空全部准确度记录 (不可撤销)")
        btn_clear.clicked.connect(self.on_clear_all)
        head.addWidget(btn_clear)
        root.addLayout(head)

        # ── 总览卡片 ──
        self._build_overview_cards(root)

        # ── Tab 区域 ──
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self._build_model_tab()
        self._build_feedback_tab()
        self._build_accuracy_tab()
        self._build_signal_tab()
        self._build_timeline_tab()
        self.tabs.setCurrentIndex(0)  # 默认落在模型校准 (校准中心旗舰页)

    # ══════════════════════════════════════════════════════════════════
    #  总览卡片
    # ══════════════════════════════════════════════════════════════════
    def _build_overview_cards(self, parent):
        row = QHBoxLayout()
        row.setSpacing(10)
        self._ov_cards = {}
        for key, title in [("acc", "分析准确度"), ("sig", "信号准确度"),
                           ("fb", "阶段带反馈"), ("calib", "校准状态")]:
            card = _card()
            card.setMinimumWidth(200)
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(2)
            ht = QHBoxLayout()
            t_label = _card_title(title)
            badge = QLabel()
            badge.setStyleSheet(f"font-weight:bold;font-size:9pt;")
            ht.addWidget(t_label)
            ht.addStretch(1)
            ht.addWidget(badge)
            lay.addLayout(ht)
            val_label = _card_value("0")
            lay.addWidget(val_label)
            sub_label = _card_sub("")
            lay.addWidget(sub_label)
            self._ov_cards[key] = (badge, val_label, sub_label)
            row.addWidget(card, 1)
        parent.addLayout(row)

    def _set_card(self, key, value, sub="", color=None, badge_text="", badge_color=None):
        badge, val_label, sub_label = self._ov_cards[key]
        val_label.setText(str(value))
        val_label.setStyleSheet(f"font-size:18pt;font-weight:bold;color:{color or theme.C_TEXT};")
        sub_label.setText(sub)
        if badge_text:
            badge.setText(badge_text)
            badge.setStyleSheet(f"font-weight:bold;font-size:9pt;color:{badge_color or theme.C_MUTED};")
        else:
            badge.setText("")

    def _render_overview(self):
        acc = accuracy_stats(load_accuracy())
        sig = signal_stats(load_signals())
        fb = load_feedback()
        sig_s = sig["summary"]

        # 分析准确度卡
        acc_hit = ""
        acc_color = theme.C_TEXT
        if acc["evaluated"] > 0:
            # 计算20根方向命中率
            e20 = acc["horizons"].get("20", {})
            total_bull = e20.get("phase_bull", {}).get("hit", 0)
            total_bear = e20.get("phase_bear", {}).get("hit", 0)
            total_n = (e20.get("phase_bull", {}).get("n", 0) +
                       e20.get("phase_bear", {}).get("n", 0))
            if total_n > 0:
                hit_rate = (total_bull + total_bear) / total_n * 100
                acc_hit = f"{hit_rate:.0f}%"
                acc_color = theme.C_UP if hit_rate >= 55 else (theme.C_DOWN if hit_rate < 45 else theme.C_AMBER)
        self._set_card("acc", f"{acc['evaluated']}",
                       f"已评估  ·  待评估 {acc['pending']}",
                       color=acc_color, badge_text=acc_hit, badge_color=acc_color)

        # 信号准确度卡
        sig_hit = ""
        sig_color = theme.C_TEXT
        if sig_s["evaluated"] > 0:
            # 统计20根上涨占比
            all_rets_20 = []
            for kind in ("event", "vsa"):
                for t, s in sig[kind].items():
                    all_rets_20.extend(s["horizons"].get("20", []))
            if all_rets_20:
                win = sum(1 for v in all_rets_20 if v > 0) / len(all_rets_20) * 100
                sig_hit = f"{win:.0f}%"
                sig_color = theme.C_UP if win >= 55 else (theme.C_DOWN if win < 45 else theme.C_AMBER)
        self._set_card("sig", f"{sig_s['evaluated']}",
                       f"已评估  ·  待评估 {sig_s['pending']}",
                       color=sig_color, badge_text=sig_hit, badge_color=sig_color)

        # 阶段带反馈卡
        fb_correct = sum(1 for r in fb if r.get("verdict") == "correct")
        fb_wrong = sum(1 for r in fb if r.get("verdict") == "wrong")
        fb_color = theme.C_TEXT
        fb_sub = f"正确 {fb_correct}  ·  错误 {fb_wrong}  ·  待标注 {len(fb) - fb_correct - fb_wrong}"
        if fb_correct + fb_wrong > 0:
            fb_rate = fb_correct / (fb_correct + fb_wrong) * 100
            fb_color = theme.C_UP if fb_rate >= 60 else (theme.C_DOWN if fb_rate < 40 else theme.C_AMBER)
            fb_sub = f"正确率 {fb_rate:.0f}%  ·  {fb_sub}"
        self._set_card("fb", f"{len(fb)}", fb_sub, color=fb_color)

        # 校准状态卡
        due, msg = calibration_status(acc["evaluated"], sig_s["evaluated"], len(fb))
        calib_color = theme.C_AMBER if due else theme.C_UP
        calib_badge = "⚠ 待校准" if due else "✓ 正常"
        self._set_card("calib", "提醒" if due else "正常", msg, color=calib_color,
                       badge_text=calib_badge, badge_color=calib_color)

    # ══════════════════════════════════════════════════════════════════
    #  模型校准 (L4 在线 LR)
    # ══════════════════════════════════════════════════════════════════
    def _build_model_tab(self):
        page = QWidget()
        self._model_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        t = QLabel("模型校准 (L4 在线逻辑回归)")
        t.setStyleSheet("font-weight:bold;font-size:11pt;")
        head.addWidget(t)
        t2 = QLabel("结构特征 + 类型 one-hot + 强 L2; 样本外达标后接管 conf")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        btn = QPushButton("⟳ 重新训练")
        btn.setToolTip("用信号准确度库全部已标注事件样本重训模型")
        btn.clicked.connect(self._on_model_retrain)
        head.addWidget(btn)
        lay.addLayout(head)

        # 状态卡片区 (数据积累 / 样本外表现 / 接管状态)
        self._model_cards = {}
        row = QHBoxLayout()
        row.setSpacing(10)
        for key, title in [("labels", "已标注样本"), ("train", "训练样本"),
                           ("oos", "样本外评估"), ("auc", "OOS AUC"),
                           ("ic", "OOS RankIC"), ("gate", "conf 接管")]:
            card = _card()
            card.setMinimumWidth(150)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 8, 12, 8)
            cl.setSpacing(2)
            cl.addWidget(_card_title(title, "9pt"))
            val = _card_value("—")
            cl.addWidget(val)
            sub = _card_sub("")
            cl.addWidget(sub)
            self._model_cards[key] = (val, sub)
            row.addWidget(card, 1)
        lay.addLayout(row)

        # 特征系数 (按 |coef| 排序) + 说明
        note = QLabel("特征系数: 正系数推高上涨概率 (P(up)), 负系数压低。"
                      "接管需要同时满足 n_train≥60 · n_oos≥15 · OOS AUC≥0.55。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.C_MUTED};font-size:9pt;")
        lay.addWidget(note)

        self._model_coef_scroll = QScrollArea()
        self._model_coef_scroll.setWidgetResizable(True)
        self._model_coef_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._model_coef_content = QWidget()
        self._model_coef_lay = QVBoxLayout(self._model_coef_content)
        self._model_coef_lay.setContentsMargins(0, 0, 0, 0)
        self._model_coef_lay.setSpacing(2)
        self._model_coef_scroll.setWidget(self._model_coef_content)
        lay.addWidget(self._model_coef_scroll, 1)

        self._render_model_tab()
        self.tabs.addTab(page, "模型校准")

    def _render_model_tab(self):
        from wyckoff.online_model import (MODEL_MIN_AUC, MODEL_MIN_OOS,
                                          MODEL_MIN_TRAIN, model_status)
        st = model_status()
        if not st:
            for k, (val, sub) in self._model_cards.items():
                val.setText("—")
                sub.setText("")
            _clear(self._model_coef_lay)
            self._placeholder(self._model_coef_lay,
                              "(尚无模型状态 — 完成分析并积累标签后点「重新训练」。"
                              "标签来自 record_signals 捕获的事件特征 + 之后真实行情评估)")
            return
        def _card_set(key, text, color=None, sub=""):
            val, s = self._model_cards[key]
            val.setText(str(text))
            val.setStyleSheet(f"font-size:18pt;font-weight:bold;color:{color or theme.C_TEXT};")
            s.setText(sub)
        n_labels = int(st.get("n_labels", 0))
        n_train = int(st.get("n_train", 0))
        n_oos = int(st.get("n_oos", 0))
        auc = st.get("auc_oos")
        ic = st.get("ic_oos")
        ready = bool(st.get("ready"))
        _card_set("labels", n_labels, theme.C_TEXT,
                  f"需 ≥{MODEL_MIN_TRAIN} 才起步训练")
        _card_set("train", n_train, theme.C_TEXT, f"门槛 {MODEL_MIN_TRAIN}")
        _card_set("oos", n_oos, theme.C_TEXT, f"门槛 {MODEL_MIN_OOS}")
        if auc is not None:
            _card_set("auc", f"{auc * 100:.1f}%",
                      theme.C_UP if auc >= MODEL_MIN_AUC else theme.C_AMBER,
                      f"门槛 {MODEL_MIN_AUC * 100:.0f}%")
        else:
            _card_set("auc", "—", theme.C_MUTED, "样本不足")
        _card_set("ic", f"{ic:+.3f}" if ic is not None else "—",
                  theme.C_UP if ic is not None and abs(ic) >= 0.05 else theme.C_MUTED)
        gate_color = theme.C_UP if ready else theme.C_AMBER
        _card_set("gate", "已接管" if ready else "未接管", gate_color,
                  f"混合权重 {st.get('blend', 0) * 100:.0f}%"
                  if ready else "样本/AUC 未达标")
        if ready:
            import time as _time
            self._model_cards["gate"][1].setText(
                f"混合权重 {st.get('blend', 0) * 100:.0f}% · "
                f"训练于 {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(st.get('trained_at', 0)))}")

        # 系数表
        _clear(self._model_coef_lay)
        coefs = st.get("coef") or []
        feats = st.get("features") or []
        rows = sorted(zip(feats, coefs), key=lambda kv: -abs(kv[1]))
        if not rows:
            self._placeholder(self._model_coef_lay, "(模型尚未训练, 暂无系数)")
            return
        for name, c in rows[:24]:
            r, bs = _row_frame(vertical=False)
            nm = QLabel(name)
            nm.setFixedWidth(150)
            nm.setStyleSheet("font-weight:bold;font-size:9pt;")
            bs.addWidget(nm)
            bar_host = QWidget()
            hl = QHBoxLayout(bar_host)
            hl.setContentsMargins(0, 0, 0, 0)
            sign = theme.C_UP if c > 0 else theme.C_DOWN
            width = max(4, int(abs(c) / 1.0 * 300)) if abs(c) > 1e-9 else 2
            bar = QLabel()
            bar.setFixedSize(min(300, width), 10)
            bar.setStyleSheet(f"background:{sign};border-radius:3px;")
            hl.addWidget(bar)
            hl.addWidget(QLabel(f"{c:+.3f}"))
            hl.addStretch(1)
            bs.addWidget(bar_host, 1)
            self._model_coef_lay.addWidget(r)
        self._model_coef_lay.addStretch(1)

    def _on_model_retrain(self):
        btn = self.sender() or self
        btn.setEnabled(False)
        btn.setText("训练中...")
        def _work():
            from wyckoff.online_model import train_model
            return train_model()
        th = _RetrainThread(_work, self)
        th.finished.connect(lambda: self._on_model_retrain_done(btn, th))
        self._model_th = th
        th.start()

    def _on_model_retrain_done(self, btn, th):
        btn.setEnabled(True)
        btn.setText("⟳ 重新训练")
        self._render_model_tab()

    # ══════════════════════════════════════════════════════════════════
    #  阶段带反馈标注
    # ══════════════════════════════════════════════════════════════════
    def _build_feedback_tab(self):
        page = QWidget()
        self._fb_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        t = QLabel("阶段带反馈标注")
        t.setStyleSheet("font-weight:bold;font-size:11pt;")
        head.addWidget(t)
        t2 = QLabel("标对错 → 攒标注集, 校准阶段判定")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        btn = QPushButton("导出")
        btn.clicked.connect(self.on_export_feedback)
        head.addWidget(btn)
        lay.addLayout(head)

        # L5 阶段判定可信度 (自动+人工标注, L1 收缩)
        self.fb_reliability = QLabel()
        self.fb_reliability.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fb_reliability.setWordWrap(True)
        self.fb_reliability.setStyleSheet(f"color:{theme.C_ACCENT};font-size:9pt;")
        lay.addWidget(self.fb_reliability)
        self.fb_reliability.setText(self._phase_reliability_text())

        self.fb_scroll = QScrollArea()
        self.fb_scroll.setWidgetResizable(True)
        self.fb_content = QWidget()
        self.fb_lay = QVBoxLayout(self.fb_content)
        self.fb_lay.setContentsMargins(0, 0, 0, 0)
        self.fb_lay.setSpacing(2)
        self.fb_scroll.setWidget(self.fb_content)
        lay.addWidget(self.fb_scroll)
        self.tabs.addTab(page, "阶段带反馈标注")

    def refresh_feedback(self, segs, symbol, datalen, scale, df):
        self._last_segs = segs
        self._last_symbol = symbol or ""
        self._last_datalen = datalen
        self._last_scale = scale
        self._last_df = df
        self._render_feedback()

    def _phase_reliability_text(self):
        """L5 阶段判定可信度: 各阶段标注正确率 (L1 收缩) 一行概览。"""
        try:
            from wyckoff.storage import phase_reliability, load_feedback
            rel = phase_reliability(load_feedback())
        except Exception:
            return ""
        if not rel:
            return "(暂无阶段标注 — 分析后在下方标注对错, 自动累积判定可信度)"
        parts = []
        for lb, s in rel.items():
            tag = "样本不足" if s["n"] < 5 else f"{s['shrunk'] * 100:.0f}%"
            parts.append(f"{lb} {s['correct']}/{s['n']} 正确→{tag}")
        return "阶段判定可信度(L1收缩): " + "  ·  ".join(parts)

    def _render_feedback(self):
        _clear(self.fb_lay)
        self.fb_reliability.setText(self._phase_reliability_text())
        if not self._last_segs:
            self._placeholder(self.fb_lay, "请先完成一次分析 (开始分析 或 双击自选股)")
            return
        segs = self._last_segs
        feedback = load_feedback()
        fmap = {}
        for r in feedback:
            if r.get("start_dt") and r.get("end_dt"):
                fmap[feedback_key(r["symbol"], r.get("scale", 240),
                                  r["start_dt"], r["end_dt"])] = r
        self._fb_bands = []
        labeled = 0
        for a, e, key, label in segs:
            rec = build_feedback_record(self._last_symbol, self._last_datalen,
                                        self._last_scale, self._last_df, a, e, key, label)
            k = feedback_key(self._last_symbol, self._last_scale,
                             rec["start_dt"], rec["end_dt"])
            if k in fmap:
                rec.update(fmap[k])
            self._fb_bands.append((k, rec))
            if rec.get("verdict"):
                labeled += 1
            row, row_lay = _row_frame()
            color = _PHASE_STYLE[key][1] if key in _PHASE_STYLE else theme.C_AMBER
            strip = QLabel()
            strip.setFixedWidth(4)
            strip.setStyleSheet(f"background:{color};border-radius:2px;")
            row_lay.addWidget(strip)
            body = QVBoxLayout()
            body.setSpacing(1)
            lb = QLabel(label)
            lb.setStyleSheet(f"color:{color};font-weight:bold;")
            meta = QLabel(f"{rec.get('start_dt', a)} ~ {rec.get('end_dt', e)}"
                          f"  净变动 {rec['net'] * 100:+.1f}%"
                          + (f"  · 反馈 20根后 {rec.get('fwd_ret', 0):+.1f}%"
                             if rec.get("source") == "auto" else "")
                          + ("  · [自动]" if rec.get("source") == "auto"
                             else "  · [人工]"))
            meta.setStyleSheet(f"color:{theme.C_MUTED};font-size:9pt;")
            body.addWidget(lb)
            body.addWidget(meta)
            row_lay.addLayout(body, 1)
            verdict = rec.get("verdict", "")
            ok = QPushButton("✓ 正确")
            ok.setCheckable(True)
            ok.setChecked(verdict == "correct")
            ok.clicked.connect(lambda _, k=k: self._set_verdict(k, "correct"))
            bad = QPushButton("✗ 错误")
            bad.setCheckable(True)
            bad.setChecked(verdict == "wrong")
            bad.clicked.connect(lambda _, k=k: self._set_verdict(k, "wrong"))
            row_lay.addWidget(ok)
            row_lay.addWidget(bad)
            self.fb_lay.addWidget(row)
        note = QLabel(f"已标注 {labeled}/{len(segs)} 带  ·  累计 {len(feedback)} 条")
        note.setStyleSheet(f"color:{theme.C_MUTED};")
        self.fb_lay.addWidget(note)
        self.fb_lay.addStretch(1)

    def _set_verdict(self, key, verdict):
        rec = None
        for k, r in self._fb_bands:
            if k == key:
                rec = r
                break
        if rec is None:
            return
        rec["verdict"] = verdict
        rec["date"] = _today()
        rec["source"] = "manual"
        feedback = load_feedback()
        fmap = {}
        for r in feedback:
            if r.get("start_dt") and r.get("end_dt"):
                fmap[feedback_key(r["symbol"], r.get("scale", 240),
                                  r["start_dt"], r["end_dt"])] = r
        if key in fmap:
            fmap[key].update(rec)
            feedback = list(fmap.values())
        else:
            feedback.append(rec)
        save_feedback(feedback)
        self._render_feedback()
        self._render_overview()

    # ══════════════════════════════════════════════════════════════════
    #  分析准确度
    # ══════════════════════════════════════════════════════════════════
    def _build_accuracy_tab(self):
        page = QWidget()
        self._acc_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        t = QLabel("分析准确度")
        t.setStyleSheet("font-weight:bold;font-size:11pt;")
        head.addWidget(t)
        t2 = QLabel("记录每次分析预测, 到期后用真实行情评估")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        btn1 = QPushButton("⚡ 立即评估")
        btn1.clicked.connect(self.on_eval)
        head.addWidget(btn1)
        btn2 = QPushButton("📤 导出")
        btn2.clicked.connect(self.on_export_accuracy)
        head.addWidget(btn2)
        btn3 = QPushButton("📊 CSV")
        btn3.clicked.connect(self.on_export_accuracy_csv)
        head.addWidget(btn3)
        lay.addLayout(head)

        # 筛选栏
        flt = QHBoxLayout()
        flt.addWidget(QLabel("筛选:"))
        self.acc_filter_code = QComboBox()
        self.acc_filter_code.setMinimumWidth(120)
        self.acc_filter_code.currentIndexChanged.connect(lambda _: self._render_accuracy_list())
        flt.addWidget(self.acc_filter_code)
        flt.addStretch(1)
        lay.addLayout(flt)

        # 汇总统计卡
        self.acc_stats_card = _card()
        acc_stats_lay = QVBoxLayout(self.acc_stats_card)
        acc_stats_lay.setContentsMargins(14, 10, 14, 10)
        acc_stats_lay.setSpacing(4)
        self.acc_header = QLabel()
        self.acc_header.setStyleSheet(f"font-weight:bold;color:{theme.C_TEXT};")
        acc_stats_lay.addWidget(self.acc_header)
        self.acc_tbl = QLabel()
        self.acc_tbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.acc_tbl.setStyleSheet(f"color:{theme.C_MUTED};font-size:9pt;")
        acc_stats_lay.addWidget(self.acc_tbl)
        lay.addWidget(self.acc_stats_card)

        # 图表区
        self.acc_chart = pg.PlotWidget()
        self.acc_chart.setFixedHeight(180)
        self.acc_chart.setBackground(theme.C_BG)
        self.acc_chart.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        self.acc_chart.showGrid(x=False, y=True, alpha=0.3)
        self.acc_chart.getAxis('bottom').setPen(pg.mkPen(theme.C_MUTED))
        self.acc_chart.getAxis('left').setPen(pg.mkPen(theme.C_MUTED))
        lay.addWidget(self.acc_chart)

        # 记录列表
        self.acc_scroll = QScrollArea()
        self.acc_scroll.setWidgetResizable(True)
        self.acc_content = QWidget()
        self.acc_lay = QVBoxLayout(self.acc_content)
        self.acc_lay.setContentsMargins(0, 0, 0, 0)
        self.acc_lay.setSpacing(2)
        self.acc_scroll.setWidget(self.acc_content)
        lay.addWidget(self.acc_scroll, 1)
        self._render_accuracy()
        self.tabs.addTab(page, "分析准确度")

    @staticmethod
    def _acc_mark(rec, ret):
        tone = rec.get("phase_tone")
        if tone == "bullish":
            return "✓" if ret > 0 else "✗"
        if tone == "bearish":
            return "✓" if ret < 0 else "✗"
        return ""

    @staticmethod
    def _hit(txt):
        return f"{txt['hit']}/{txt['n']} {txt['hit'] / txt['n'] * 100:.0f}%" if txt["n"] else "-"

    def _render_accuracy(self):
        records = load_accuracy()
        stats = accuracy_stats(records)
        # 更新筛选器
        codes = sorted({r.get("code") for r in records if r.get("code")})
        cur = self.acc_filter_code.currentData()
        self.acc_filter_code.blockSignals(True)
        if self.acc_filter_code.count() == 0:
            self.acc_filter_code.addItem("全部股票", None)
        existing = {self.acc_filter_code.itemData(i) for i in range(self.acc_filter_code.count())}
        for c in codes:
            if c not in existing:
                name = next((r.get("name") or "" for r in records if r.get("code") == c), "")
                self.acc_filter_code.addItem(f"{name} {c}".strip(), c)
        self.acc_filter_code.blockSignals(False)
        # 汇总统计
        self._render_acc_stats(stats)
        self._render_acc_chart(stats)
        self._render_accuracy_list()

    def _render_acc_stats(self, stats):
        waiting = 0
        records = load_accuracy()
        waiting = sum(1 for r in records if r.get("waiting"))
        self.acc_header.setText(
            f"累计 {stats['total']}  ·  已评估 {stats['evaluated']}  ·  "
            f"待评估 {stats['pending']}  ·  等行情 {waiting}  ·  "
            f"无法定位 {stats.get('stale', 0)}")
        lines = [f"{'周期':<6}{'n':>4}  {'偏多':>8}  {'偏空':>8}  "
                 f"{'多头计划':>8}  {'空头计划':>8}  {'均收益':>8}  "
                 f"{'↑目标':>7}  {'↓目标':>7}"]
        for h in HORIZONS:
            e = stats["horizons"][str(h)]
            mean = f"{e['mean'] * 100:+.1f}%" if e["mean"] is not None else "-"
            lines.append(
                f"{h:>4d}根 {e['n']:>4d}  {self._hit(e['phase_bull']):>8}  "
                f"{self._hit(e['phase_bear']):>8}  "
                f"{self._hit(e['trade_bull']):>8}  "
                f"{self._hit(e['trade_bear']):>8}  {mean:>8}  "
                f"{self._hit(e['up_target']):>7}  "
                f"{self._hit(e['down_target']):>7}")
        self.acc_tbl.setText("\n".join(lines))
        self.acc_tbl.setFont(pg.QtGui.QFont("Monospace", 9))

    def _render_acc_chart(self, stats):
        """绘制分析准确度柱状图: 按周期展示偏多/偏空/计划方向命中率。"""
        self.acc_chart.clear()
        horizon_keys = [str(h) for h in HORIZONS]
        cats = []
        bull_vals = []
        bear_vals = []
        trade_bull_vals = []
        trade_bear_vals = []
        for i, hk in enumerate(horizon_keys):
            e = stats["horizons"][hk]
            n = e["n"]
            if n == 0:
                continue
            cats.append(f"{hk}根")
            bull_vals.append(e["phase_bull"]["hit"] / e["phase_bull"]["n"] * 100
                            if e["phase_bull"]["n"] else 0)
            bear_vals.append(e["phase_bear"]["hit"] / e["phase_bear"]["n"] * 100
                            if e["phase_bear"]["n"] else 0)
            trade_bull_vals.append(e["trade_bull"]["hit"] / e["trade_bull"]["n"] * 100
                                   if e["trade_bull"]["n"] else 0)
            trade_bear_vals.append(e["trade_bear"]["hit"] / e["trade_bear"]["n"] * 100
                                   if e["trade_bear"]["n"] else 0)
        if not cats:
            return
        x = list(range(len(cats)))
        bar_w = 0.2
        bg = pg.BarGraphItem(x=[xi - 1.5*bar_w for xi in x], height=bull_vals,
                             width=bar_w, brush=pg.mkBrush(theme.C_UP), name="偏多阶段")
        self.acc_chart.addItem(bg)
        bg2 = pg.BarGraphItem(x=[xi - 0.5*bar_w for xi in x], height=bear_vals,
                              width=bar_w, brush=pg.mkBrush(theme.C_DOWN), name="偏空阶段")
        self.acc_chart.addItem(bg2)
        bg3 = pg.BarGraphItem(x=[xi + 0.5*bar_w for xi in x], height=trade_bull_vals,
                              width=bar_w, brush=pg.mkBrush(theme.C_UP + "80"), name="多头计划")
        self.acc_chart.addItem(bg3)
        bg4 = pg.BarGraphItem(x=[xi + 1.5*bar_w for xi in x], height=trade_bear_vals,
                              width=bar_w, brush=pg.mkBrush(theme.C_DOWN + "80"), name="空头计划")
        self.acc_chart.addItem(bg4)
        self.acc_chart.getAxis('bottom').setTicks([list(enumerate(cats))])
        self.acc_chart.getAxis('left').setRange(0, 100)
        self.acc_chart.getAxis('left').setLabel("命中率 %")

    def _render_accuracy_list(self):
        _clear(self.acc_lay)
        records = load_accuracy()
        cur_code = self.acc_filter_code.currentData()
        if cur_code:
            records = [r for r in records if r.get("code") == cur_code]
        if not records:
            self._placeholder(self.acc_lay, "(暂无记录, 完成一次分析后自动记录)")
            return
        for r in records:
            row, bs = _row_frame(vertical=True)
            scale_txt = "日线" if r.get("scale") == 240 else f"{r.get('scale')}分钟"
            head = QLabel(f"{r.get('name') or ''} {r.get('code')}  "
                          f"{r.get('ref_dt')}  {scale_txt}")
            head.setStyleSheet("font-weight:bold;")
            bs.addWidget(head)
            conf = {"high": "(高置信)", "caution": "(谨慎)"}.get(r.get("phase_conf"), "")
            parts = []
            for h in HORIZONS:
                res = (r.get("results") or {}).get(str(h))
                if not res or res.get("ret") is None:
                    parts.append(f"{h}根 -")
                else:
                    parts.append(f"{h}根 {res['ret'] * 100:+.1f}%{self._acc_mark(r, res['ret'])}")
            meta = (f"阶段:{r.get('phase')}{conf}  P&F:{r.get('pnf_dir') or '-'}  "
                    f"计划:{r.get('trade_dir') or '-'}   " + "  ".join(parts))
            if r.get("up_target"):
                meta += f"  ↑目标{r['up_target']:.2f}"
            if r.get("down_target"):
                meta += f"  ↓目标{r['down_target']:.2f}"
            mt = QLabel(meta)
            mt.setStyleSheet(f"color:{theme.C_MUTED};")
            mt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bs.addWidget(mt)
            self.acc_lay.addWidget(row)
        self.acc_lay.addStretch(1)

    # ══════════════════════════════════════════════════════════════════
    #  信号准确度
    # ══════════════════════════════════════════════════════════════════
    def _build_signal_tab(self):
        page = QWidget()
        self._sig_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        t = QLabel("信号校准 (L1 贝叶斯收缩)")
        t.setStyleSheet("font-weight:bold;font-size:11pt;")
        head.addWidget(t)
        t2 = QLabel("每类信号的实测命中 → 收缩占比 + 置信区间 + 显著性验证")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        btn1 = QPushButton("⚡ 立即评估")
        btn1.clicked.connect(self.on_eval)
        head.addWidget(btn1)
        btn2 = QPushButton("📤 导出")
        btn2.clicked.connect(self.on_export_signals)
        head.addWidget(btn2)
        btn3 = QPushButton("📋 周报")
        btn3.clicked.connect(self.on_export_review)
        head.addWidget(btn3)
        btn4 = QPushButton("📊 CSV")
        btn4.clicked.connect(self.on_export_signals_csv)
        head.addWidget(btn4)
        lay.addLayout(head)

        # 筛选栏
        flt = QHBoxLayout()
        flt.addWidget(QLabel("筛选:"))
        self.sig_filter_kind = QComboBox()
        self.sig_filter_kind.addItems(["全部", "威科夫事件", "VSA"])
        self.sig_filter_kind.currentIndexChanged.connect(lambda _: self._render_signal_list())
        flt.addWidget(self.sig_filter_kind)
        self.sig_filter_type = QComboBox()
        self.sig_filter_type.setMinimumWidth(100)
        self.sig_filter_type.currentIndexChanged.connect(lambda _: self._render_signal_list())
        flt.addWidget(self.sig_filter_type)
        self.sig_filter_code = QComboBox()
        self.sig_filter_code.setMinimumWidth(120)
        self.sig_filter_code.currentIndexChanged.connect(lambda _: self._render_signal_list())
        flt.addWidget(self.sig_filter_code)
        flt.addStretch(1)
        lay.addLayout(flt)

        # ── 上半区: 摘要 + 验证 + 图表 + 解读 (可滚动) ──
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_content = QWidget()
        top_lay = QVBoxLayout(top_content)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(4)

        # 统计摘要
        self.sig_summary = QLabel()
        self.sig_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_summary.setWordWrap(True)
        top_lay.addWidget(self.sig_summary)

        # L1 贝叶斯收缩胜率 (原始 vs 收缩 vs 95% CI)
        self.sig_shrink = QLabel()
        self.sig_shrink.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_shrink.setWordWrap(True)
        self.sig_shrink.setStyleSheet(f"color:{theme.C_ACCENT};font-size:9pt;")
        top_lay.addWidget(self.sig_shrink)

        # L5 多周期一致性 (5/10/20/40 收缩胜率 + 边缘衰减判定)
        self.sig_consistency = QLabel()
        self.sig_consistency.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_consistency.setWordWrap(True)
        self.sig_consistency.setStyleSheet(f"color:{theme.C_MUTED};font-size:9pt;")
        top_lay.addWidget(self.sig_consistency)

        # 验证结果
        self.sig_validation = QLabel()
        self.sig_validation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_validation.setWordWrap(True)
        self.sig_validation.setStyleSheet(f"color:{theme.C_ACCENT};font-size:10pt;")
        top_lay.addWidget(self.sig_validation)

        # 胜率排行图
        self.sig_chart = pg.PlotWidget()
        self.sig_chart.setFixedHeight(160)
        self.sig_chart.setBackground(theme.C_BG)
        self.sig_chart.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        self.sig_chart.showGrid(x=False, y=True, alpha=0.3)
        self.sig_chart.getAxis('bottom').setPen(pg.mkPen(theme.C_MUTED))
        self.sig_chart.getAxis('left').setPen(pg.mkPen(theme.C_MUTED))
        top_lay.addWidget(self.sig_chart)

        # 准确性解读
        vh = QHBoxLayout()
        vlab = QLabel("准确性解读")
        vlab.setStyleSheet("font-weight:bold;font-size:10pt;")
        vh.addWidget(vlab)
        vh.addStretch(1)
        self.btn_ai_tts = QPushButton("🔊 朗读")
        self.btn_ai_tts.setToolTip("朗读下方解读内容")
        self.btn_ai_tts.clicked.connect(self._on_ai_tts)
        vh.addWidget(self.btn_ai_tts)
        self.btn_ai_verify = QPushButton("🤖 AI 解读")
        self.btn_ai_verify.clicked.connect(self._on_ai_verify)
        vh.addWidget(self.btn_ai_verify)
        top_lay.addLayout(vh)
        self.sig_verdict = QLabel()
        self.sig_verdict.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_verdict.setWordWrap(True)
        self.sig_verdict.setStyleSheet(
            f"color:{theme.C_TEXT};background:{theme.C_BG};"
            f"border:1px solid {theme.C_BORDER};border-radius:4px;padding:6px;")
        top_lay.addWidget(self.sig_verdict)
        top_lay.addStretch(1)
        top_scroll.setWidget(top_content)

        # ── 下半区: 信号列表 ──
        self.sig_scroll = QScrollArea()
        self.sig_scroll.setWidgetResizable(True)
        self.sig_content = QWidget()
        self.sig_lay = QVBoxLayout(self.sig_content)
        self.sig_lay.setContentsMargins(0, 0, 0, 0)
        self.sig_lay.setSpacing(2)
        self.sig_scroll.setWidget(self.sig_content)

        # ── QSplitter 分割 ──
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top_scroll)
        splitter.addWidget(self.sig_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        lay.addWidget(splitter, 1)
        self._render_signal_accuracy()
        self.tabs.addTab(page, "信号校准")

    @staticmethod
    def _sig_mark(rec, ret):
        kind = rec.get("kind")
        t = rec.get("type", "")
        if kind == "event":
            d = event_dir(t)
        else:
            d = 1 if t in ("SPR",) else (-1 if t in ("ND",) else 0)
        if d > 0:
            return "✓" if ret > 0 else "✗"
        if d < 0:
            return "✓" if ret < 0 else "✗"
        return ""

    def _render_signal_accuracy(self):
        records = load_signals()
        stats = signal_stats(records)
        try:
            self.sig_summary.setText(_fmt_stats(stats))
        except Exception:
            self.sig_summary.setText(f"信号追踪: 累计 {stats['summary']['total']} 条")
        self.sig_shrink.setText(self._shrink_summary())
        self.sig_consistency.setText(self._consistency_summary())
        # 验证
        try:
            from wyckoff.validation import validation_lines, validation_verdict
            vlines = validation_lines(records)
            self.sig_validation.setText("\n".join(vlines) if vlines else "")
            self.sig_verdict.setText(validation_verdict(records))
        except Exception as e:
            self.sig_validation.setText(f"(验证不可用: {e})")
            self.sig_verdict.setText("")
        waiting = sum(1 for r in records if r.get("waiting"))
        if waiting:
            self.sig_summary.setText(
                self.sig_summary.toPlainText() +
                f"\n说明: {waiting} 条信号在行情末端, 等待未来K线走满后再评估 (非故障)。")
        # 更新筛选器
        self._update_signal_filters(records)
        self._render_sig_chart(stats)
        self._render_signal_list()

    def _consistency_summary(self):
        """L5 多周期一致性: 各类型 5/10/20/40 根收缩胜率 + 边缘判定。"""
        try:
            from wyckoff.signal_accuracy import win_rate_profile
            from wyckoff.signal_accuracy import load_win_rates
            keys = set()
            for h in (5, 10, 20, 40):
                keys.update(load_win_rates(h).keys())
        except Exception:
            return ""
        if not keys:
            return ""
        lines = []
        for (kind, t) in sorted(keys):
            try:
                p = win_rate_profile(kind, t)
            except Exception:
                continue
            cells = []
            for h in (5, 10, 20, 40):
                rec = p["horizons"].get(str(h))
                if rec:
                    cells.append(f"{h}根:{rec['shrunk'] * 100:.0f}%")
                else:
                    cells.append(f"{h}根:-")
            mark = "⚠" if not p["consistent"] else ""
            lines.append(f"{kind}:{t} " + " ".join(cells) + f" [{p['verdict']}]{mark}")
        return "L5多周期(收缩): " + "  ".join(lines)

    def _shrink_summary(self):
        """L1 贝叶斯收缩胜率一览: 原始 vs 收缩 vs Wilson 95% CI (20根口径)。"""
        try:
            from wyckoff.signal_accuracy import load_win_rates
            rates = load_win_rates(20)
        except Exception:
            return ""
        if not rates:
            return ""
        rows = []
        for (kind, t), s in rates.items():
            raw = s["win"] * 100
            sh = s["shrunk"] * 100
            rows.append(f"{kind}:{t} {raw:.0f}%→{sh:.0f}% "
                        f"[{s['ci_lo'] * 100:.0f}-{s['ci_hi'] * 100:.0f}] n={s['n']}")
        rows.sort(key=lambda x: -float(x.split('→')[1].split('%')[0]))
        return "L1收缩(20根): " + "  ".join(rows)

    def _update_signal_filters(self, records):
        """更新信号类型和股票筛选器。"""
        # 类型筛选
        types = sorted({r.get("type") for r in records if r.get("type")})
        cur_type = self.sig_filter_type.currentData()
        self.sig_filter_type.blockSignals(True)
        self.sig_filter_type.clear()
        self.sig_filter_type.addItem("全部类型", None)
        for t in types:
            self.sig_filter_type.addItem(t, t)
        # 恢复之前的选择
        if cur_type:
            idx = self.sig_filter_type.findData(cur_type)
            if idx >= 0:
                self.sig_filter_type.setCurrentIndex(idx)
        self.sig_filter_type.blockSignals(False)

        # 股票筛选
        codes = sorted({r.get("code") for r in records if r.get("code")})
        cur_code = self.sig_filter_code.currentData()
        self.sig_filter_code.blockSignals(True)
        self.sig_filter_code.clear()
        self.sig_filter_code.addItem("全部股票", None)
        for c in codes:
            name = next((r.get("name") or "" for r in records if r.get("code") == c), "")
            self.sig_filter_code.addItem(f"{name} {c}".strip(), c)
        if cur_code:
            idx = self.sig_filter_code.findData(cur_code)
            if idx >= 0:
                self.sig_filter_code.setCurrentIndex(idx)
        self.sig_filter_code.blockSignals(False)

    def _render_sig_chart(self, stats):
        """绘制信号胜率排行图: 按类型展示20根胜率。"""
        self.sig_chart.clear()
        items = []
        for kind in ("event", "vsa"):
            for t, s in stats[kind].items():
                h20 = s["horizons"].get("20", [])
                if len(h20) < 3:
                    continue
                win = sum(1 for v in h20 if v > 0) / len(h20) * 100
                mean = statistics.mean(h20) * 100
                items.append((t, len(h20), win, mean, kind))
        if not items:
            return
        items.sort(key=lambda x: -x[2])
        x = list(range(len(items)))
        names = [f"{i[0]}({i[1]})" for i in items]
        for i, item in enumerate(items):
            win_rate = item[2]
            bar_color = theme.C_UP if win_rate >= 55 else (theme.C_DOWN if win_rate < 45 else theme.C_AMBER)
            bar = pg.BarGraphItem(x=[i], height=[win_rate], width=0.6,
                                  brush=pg.mkBrush(bar_color))
            self.sig_chart.addItem(bar)
        # 添加均值线
        mean_line = pg.PlotDataItem(x=x, y=[i[3] for i in items],
                                    pen=pg.mkPen(theme.C_ACCENT, style=Qt.PenStyle.DashLine, width=2))
        self.sig_chart.addItem(mean_line)
        self.sig_chart.getAxis('bottom').setTicks([list(enumerate(names))])
        self.sig_chart.getAxis('left').setRange(0, 100)
        self.sig_chart.getAxis('left').setLabel("胜率 %")

    def _render_signal_list(self):
        _clear(self.sig_lay)
        records = load_signals()
        if not records:
            self._placeholder(self.sig_lay, "(暂无记录, 完成一次分析后自动记录)")
            return
        # 应用筛选
        kind_map = {"威科夫事件": "event", "VSA": "vsa"}
        sel_kind = kind_map.get(self.sig_filter_kind.currentText())
        sel_type = self.sig_filter_type.currentData()
        sel_code = self.sig_filter_code.currentData()
        filtered = records
        if sel_kind:
            filtered = [r for r in filtered if r.get("kind") == sel_kind]
        if sel_type:
            filtered = [r for r in filtered if r.get("type") == sel_type]
        if sel_code:
            filtered = [r for r in filtered if r.get("code") == sel_code]
        filtered.sort(key=lambda x: x.get("date", ""), reverse=True)
        filtered = filtered[:400]
        if not filtered:
            self._placeholder(self.sig_lay, "(筛选后无匹配记录)")
            return
        for r in filtered:
            row, bs = _row_frame(vertical=True)
            kind_cn = "事件" if r.get("kind") == "event" else "VSA"
            scale_txt = "日线" if r.get("scale") == 240 else f"{r.get('scale')}分钟"
            conf = f"  置信{r.get('conf')}" if r.get("conf") else ""
            head = QLabel(f"{kind_cn} {r.get('type')}  {r.get('code')}  "
                          f"{r.get('date')}  {scale_txt}{conf}")
            head.setStyleSheet("font-weight:bold;")
            bs.addWidget(head)
            parts = []
            for h in HORIZONS:
                res = (r.get("results") or {}).get(str(h))
                if not res or res.get("ret") is None:
                    parts.append(f"{h}根 -")
                else:
                    parts.append(f"{h}根 {res['ret'] * 100:+.1f}%{self._sig_mark(r, res['ret'])}")
            mt = QLabel("  ".join(parts))
            mt.setStyleSheet(f"color:{theme.C_MUTED};")
            bs.addWidget(mt)
            self.sig_lay.addWidget(row)
        self.sig_lay.addStretch(1)

    def _on_ai_verify(self):
        records = load_signals()
        self.btn_ai_verify.setEnabled(False)
        self.btn_ai_verify.setText("解读中...")
        self.sig_verdict.setText("正在请求 AI 解读...")
        self._ai_th = _AiVerifyThread(records, self._settings, self)
        self._ai_th.finished.connect(self._on_ai_verify_done)
        self._ai_th.start()

    def _on_ai_verify_done(self, res):
        self.btn_ai_verify.setEnabled(True)
        self.btn_ai_verify.setText("🤖 AI 解读")
        ai = (res or {}).get("ai")
        rule = (res or {}).get("rule", "")
        if ai:
            self.sig_verdict.setText(rule + "\n\n─ AI 解读 ─\n" + ai)
        elif rule and rule.startswith("AI 解读失败"):
            self.sig_verdict.setText(rule)
        else:
            self.sig_verdict.setText(
                rule + "\n\nAI 解读不可用: 未配置 API Key 或模型调用失败。\n"
                       "请在 设置→AI 中填写 DeepSeek/OpenAI 兼容 API Key 后重试。")

    def _on_ai_tts(self):
        from wyckoff.tts import is_enabled, speak, stop
        if self._ai_tts_playing:
            stop()
            self._ai_tts_playing = False
            self.btn_ai_tts.setText("🔊 朗读")
            return
        if not is_enabled(self._settings):
            self.btn_ai_tts.setText("🔊 朗读")
            QMessageBox.information(
                self, "语音播报",
                "语音朗读未启用: 请在 设置→语音播报 中启用并配置引擎。")
            return
        text = (self.sig_verdict.text() or "").strip()
        if not text or text.startswith("正在请求"):
            QMessageBox.information(self, "语音播报", "暂无可朗读的解读内容。")
            return
        cap = int(self._settings.get("tts_max_chars", 3000) or 6000)
        if cap > 0 and len(text) > cap:
            text = text[:cap]
        self._ai_tts_playing = True
        self.btn_ai_tts.setText("■ 停止")
        ok = speak(text, self._settings, on_done=self._on_ai_tts_done)
        if not ok:
            self._ai_tts_playing = False
            self.btn_ai_tts.setText("🔊 朗读")

    def _on_ai_tts_done(self, ok, err):
        self._ai_tts_playing = False
        self.btn_ai_tts.setText("🔊 朗读")

    # ══════════════════════════════════════════════════════════════════
    #  信号时间线
    # ══════════════════════════════════════════════════════════════════
    def _build_timeline_tab(self):
        page = QWidget()
        self._tl_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        t = QLabel("信号时间线")
        t.setStyleSheet("font-weight:bold;font-size:11pt;")
        head.addWidget(t)
        t2 = QLabel("按股票 → 日期浏览每个信号的出现位置与事后涨跌")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        self.tl_filter = QComboBox()
        self.tl_filter.currentIndexChanged.connect(lambda _: self._render_timeline())
        head.addWidget(self.tl_filter)
        lay.addLayout(head)

        self.tl_scroll = QScrollArea()
        self.tl_scroll.setWidgetResizable(True)
        self.tl_content = QWidget()
        self.tl_lay = QVBoxLayout(self.tl_content)
        self.tl_lay.setContentsMargins(0, 0, 0, 0)
        self.tl_lay.setSpacing(2)
        self.tl_scroll.setWidget(self.tl_content)
        lay.addWidget(self.tl_scroll, 1)
        self.tabs.addTab(page, "信号时间线")

    def _render_timeline(self):
        _clear(self.tl_lay)
        records = load_signals()
        if not records:
            self._placeholder(self.tl_lay, "(暂无信号记录, 完成分析后自动收集)")
            return
        codes = sorted({r.get("code") for r in records if r.get("code")})
        cur = self.tl_filter.currentData()
        self.tl_filter.blockSignals(True)
        if self.tl_filter.count() == 0:
            self.tl_filter.addItem("全部股票", None)
            for c in codes:
                name = next((r.get("name") or "" for r in records
                             if r.get("code") == c), "")
                self.tl_filter.addItem(f"{name} {c}".strip(), c)
        self.tl_filter.blockSignals(False)
        sel = [r for r in records if not cur or r.get("code") == cur]
        sel.sort(key=lambda r: (str(r.get("code", "")), str(r.get("date", ""))))
        groups = OrderedDict()
        for r in sel:
            groups.setdefault(r.get("code"), []).append(r)
        for code, recs in groups.items():
            name = next((r.get("name") or "" for r in recs), "")
            hd = QLabel(f"<b>{name} {code}</b>  ({len(recs)} 条信号)")
            hd.setStyleSheet(f"color:{theme.C_ACCENT};margin-top:6px;")
            self.tl_lay.addWidget(hd)
            for r in sorted(recs, key=lambda x: str(x.get("date", ""))):
                row, bs = _row_frame(vertical=True)
                kind_cn = "事件" if r.get("kind") == "event" else "VSA"
                head = QLabel(f"{r.get('date', '')[:10]}  {kind_cn} {r.get('type')}")
                head.setStyleSheet("font-weight:bold;")
                bs.addWidget(head)
                parts = []
                for h in HORIZONS:
                    res = (r.get("results") or {}).get(str(h))
                    if not res or res.get("ret") is None:
                        parts.append(f"{h}根 -")
                    else:
                        parts.append(f"{h}根 {res['ret'] * 100:+.1f}%"
                                     f"{self._sig_mark(r, res['ret'])}")
                mt = QLabel("  ".join(parts))
                mt.setStyleSheet(f"color:{theme.C_MUTED};")
                bs.addWidget(mt)
                self.tl_lay.addWidget(row)
        self.tl_lay.addStretch(1)

    # ══════════════════════════════════════════════════════════════════
    #  动作
    # ══════════════════════════════════════════════════════════════════
    def on_eval(self):
        self._eval_thread = _EvalThread(self)
        self._eval_thread.finished.connect(self._after_eval)
        self.setWindowTitle("校准中心 (评估中...)")
        self._eval_thread.start()

    def _after_eval(self):
        self.setWindowTitle("校准中心")
        self.render_all()

    def on_calibrate(self):
        acc = accuracy_stats(load_accuracy())
        sig = signal_stats(load_signals())
        record_calibration(acc["evaluated"], sig["summary"]["evaluated"], len(load_feedback()))
        self._render_overview()

    def on_export_feedback(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (阶段带反馈)")
        if not d:
            return
        import os
        path = os.path.join(d, "wx_feedback_export.json")
        with open(path, "w", encoding="utf-8") as f:
            import json
            json.dump(load_feedback(), f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, "导出完成", f"已导出标注集到:\n{path}")

    def on_export_accuracy(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (分析准确度)")
        if not d:
            return
        import os
        path = export_accuracy(load_accuracy(), os.path.join(d, "wx_accuracy_export.json"))
        QMessageBox.information(self, "导出完成",
                                f"已导出 {len(load_accuracy())} 条记录到:\n{path}")

    def on_export_accuracy_csv(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (分析准确度 CSV)")
        if not d:
            return
        import os
        from wyckoff.accuracy import export_accuracy_csv
        path = export_accuracy_csv(load_accuracy(),
                                   os.path.join(d, "wx_accuracy.csv"))
        QMessageBox.information(self, "导出完成",
                                f"已导出 {len(load_accuracy())} 条记录到:\n{path}")

    def on_export_signals(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (信号准确度)")
        if not d:
            return
        import os
        path = export_signals(load_signals(), os.path.join(d, "wx_signal_accuracy_export.json"))
        QMessageBox.information(self, "导出完成",
                                f"已导出 {len(load_signals())} 条信号记录到:\n{path}")

    def on_export_signals_csv(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (信号准确度 CSV)")
        if not d:
            return
        import os
        from wyckoff.signal_accuracy import export_signals_csv
        path = export_signals_csv(load_signals(),
                                  os.path.join(d, "wx_signal_accuracy.csv"))
        QMessageBox.information(self, "导出完成",
                                f"已导出 {len(load_signals())} 条信号记录到:\n{path}")

    def on_export_review(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (复盘周报)")
        if not d:
            return
        import os
        from wyckoff.signal_accuracy import export_review_report
        path = export_review_report(days=7, markdown=True,
                                    path=os.path.join(d, "wx_signal_review.md"))
        QMessageBox.information(self, "导出完成",
                                f"已导出近7天信号复盘周报到:\n{path}")

    def on_export_all(self):
        self.on_export_accuracy()
        self.on_export_signals()
        self.on_export_feedback()

    def on_clear_all(self):
        from wyckoff.accuracy import save_accuracy, load_accuracy
        from wyckoff.signal_accuracy import save_signals, load_signals, \
            invalidate_win_rate_cache
        from wyckoff.storage import load_feedback, save_feedback
        n_acc = len(load_accuracy())
        n_sig = len(load_signals())
        n_fb = len(load_feedback())
        if n_acc + n_sig + n_fb == 0:
            QMessageBox.information(self, "清空", "当前没有需要清空的记录")
            return
        ret = QMessageBox.question(
            self, "确认清空",
            f"将清空全部准确度记录:\n  - 分析准确度 {n_acc} 条\n"
            f"  - 信号准确度 {n_sig} 条\n  - 阶段带反馈 {n_fb} 条\n\n"
            "该操作不可撤销, 是否继续?")
        if ret != QMessageBox.StandardButton.Yes:
            return
        save_accuracy([])
        save_signals([])
        save_feedback([])
        invalidate_win_rate_cache()
        self.render_all()
        QMessageBox.information(self, "已清空", "已清空全部准确度记录。")

    # ── 渲染入口 ──
    def render_all(self):
        self._render_overview()
        self._render_model_tab()
        self._render_accuracy()
        self._render_signal_accuracy()
        self._render_timeline()
        if self._last_segs:
            self._render_feedback()

    def _placeholder(self, lay, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{theme.C_MUTED};")
        lay.addWidget(lbl)
        lay.addStretch(1)

    def _pick_fb_tab(self):
        self.tabs.setCurrentIndex(1)  # 0=模型校准, 1=阶段带反馈标注


def _row_frame(vertical=False):
    row = QFrame()
    row.setObjectName("row")
    row.setStyleSheet(f"background:{theme.C_PANEL};border:1px solid {theme.C_BORDER};"
                      f"border-radius:4px;")
    if vertical:
        lay = QVBoxLayout(row)
    else:
        lay = QHBoxLayout(row)
    lay.setContentsMargins(6, 4, 6, 4)
    lay.setSpacing(6)
    return row, lay


def _clear(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


def _today():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")
