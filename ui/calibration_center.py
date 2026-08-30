"""校准中心: 分层校准管线的观测窗口 (总览 / 信号校准 / 模型校准 / 分析校准)。

数据全部来自 wyckoff 包:
  - accuracy / signal_accuracy / calibration: 各层评估样本与胜率库;
  - validation: Rank IC / Bootstrap CI / 置换检验 / OOS 验证;
  - online_model: L4 特征级在线 LR 模型 (系数/样本外 AUC/接管状态)。

优化: 重度计算 (signal_stats/validation/accuracy_stats/PNF报告) 迁移至后台线程,
仅渲染可见 Tab, 按文件 mtime 缓存, 支持懒加载。
"""
import os
import statistics
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QThread, QThreadPool, QRunnable, pyqtSignal, QObject, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wyckoff.accuracy import (
    HORIZONS,
    accuracy_stats,
    export_accuracy,
    load_accuracy,
    run_auto_accuracy_eval,
)
from wyckoff.calibration import calibration_status, record_calibration
from wyckoff.config import _PHASE_STYLE, event_dir, vsa_dir
from wyckoff.paths import ACCURACY_FILE, FEEDBACK_FILE, SIGNAL_ACCURACY_FILE
from wyckoff.pnf_accuracy import (
    PNF_ACC_DIR as _PNF_ACC_DIR,
)
from wyckoff.pnf_accuracy import (
    load_latest_report as _pnf_load_latest,
)
from wyckoff.pnf_accuracy import (
    run_eval as _pnf_run_eval,
)
from wyckoff.settings_keys import S
from wyckoff.signal_accuracy import (
    _fmt_stats,
    export_signals,
    load_signals,
    run_auto_signal_eval,
    signal_stats,
)
from wyckoff.storage import (
    build_feedback_record,
    feedback_key,
    load_feedback,
    save_feedback,
)

from . import theme

# 时间线列表渲染上限 (每行 3 个 QWidget, 无上限会拖垮主线程)
_TL_MAX_ROWS = 400

# 校准数据同步仓库: 固定共享私有 git 仓, 用户无需填写 URL/凭据
CALIB_SYNC_REPO = "git@github.com:czqjrj/wyckoff-calib.git"


# ── 后台计算任务基类 ──
class _WorkerSignals(QObject):
    finished = pyqtSignal(object, object)  # (tab_key, result)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)


class _ComputeTask(QRunnable):
    """可放入 QThreadPool 的计算任务。"""

    def __init__(self, tab_key, func, *args, **kwargs):
        super().__init__()
        self.tab_key = tab_key
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            result = self.func(*self.args, **self.kwargs)
            self.signals.finished.emit(self.tab_key, result)
        except Exception as e:
            self.signals.error.emit(f"{self.tab_key}: {e}")


# ── 现有评估线程保留兼容 ──
class _EvalThread(QThread):
    finished = pyqtSignal(object)

    def run(self):
        errors = []
        for name, fn in (
            ("阶段准确度评估", run_auto_accuracy_eval),
            ("信号准确度评估", run_auto_signal_eval),
            ("点数图三档评估", lambda: _pnf_run_eval(
                print_stdout=False, export_json=True, n_seeds=15)),
        ):
            try:
                fn()
            except Exception as e:
                errors.append(f"{name}: {e}")
        self.finished.emit(errors)


class _PnfEvalThread(QThread):
    finished = pyqtSignal(object)

    def run(self):
        errors = []
        try:
            _pnf_run_eval(print_stdout=False, export_json=True, n_seeds=30)
        except Exception as e:
            errors.append(f"点数图三档评估: {e}")
        self.finished.emit(errors)


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


class _MtimeCache:
    """按文件 mtime 失效的只读缓存。

    单次 render_all 流程里同一 JSON (信号库 3MB) 会被多个 tab 各自解析,
    这里以 mtime 为键共享一份解析结果; 后台评估线程落盘后 mtime 变化自动重载。
    """

    def __init__(self):
        self._ent = {}  # key -> (mtime, value)

    def get(self, key, path, loader):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        ent = self._ent.get(key)
        if ent is None or ent[0] != mtime:
            ent = (mtime, loader())
            self._ent[key] = ent
        return ent[1]


def _card_title(text, size="11pt"):
    lb = QLabel(text)
    lb.setStyleSheet(f"font-weight:bold;font-size:{size};color:{theme.C_TEXT};")
    return lb


def _card_value(text, color=None):
    lb = QLabel(text)
    c = color or theme.C_TEXT
    lb.setStyleSheet(f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{c};")
    return lb


def _card_sub(text):
    lb = QLabel(text)
    lb.setStyleSheet(f"font-size:{theme.font_pt('mini')};color:{theme.C_MUTED};")
    return lb


class CalibrationCenter(QWidget):
    """校准中心: 分层校准 (L1 收缩 / L2 OOS / L4 模型) 的观测与操作入口。

    作为主窗口的嵌入式 Tab (紧跟「综合选股」), 不再是独立对话框。
    """

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setMinimumSize(680, 420)
        self._rendered_once = False
        self._settings = settings or {}
        self._fb_bands = []
        self._last_segs = None
        self._last_symbol = ""
        self._last_datalen = 700
        self._last_scale = 240
        self._last_df = None
        self._ai_tts_playing = False
        self._eval_thread = None
        self._pnf_th = None
        self._model_th = None
        self._sync_th = None
        # 渲染优化: 文件级 mtime 缓存 + 懒加载 (只渲染可见 tab)
        self._cache = _MtimeCache()
        # 后台线程池用于重度计算
        self._thread_pool = QThreadPool.globalInstance()
        # 加载状态追踪
        self._loading_tabs = set()
        self._tab_data_cache = {}  # tab_key -> computed data
        self._acc_stats_memo = None  # (mtime, accuracy_stats 结果)
        self._dirty_tabs = set()
        self._lazy_paused = False

        # 全局样式: 统一铺底色, 消除白色空隙 (随主题重建, 见 apply_theme)
        self.setStyleSheet(self._build_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # ── 顶栏 ──
        head = QHBoxLayout()
        head.setSpacing(8)
        strip = QLabel()
        strip.setFixedSize(5, 20)
        strip.setStyleSheet(f"background:{theme.C_ACCENT};border-radius:2px;")
        self._head_strip = strip  # 主题切换时重刷 (accent 两套不同)
        head.addWidget(strip)
        t = QLabel("校准中心")
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('h2')};")
        head.addWidget(t)
        t2 = QLabel("分层校准  ·  L1 贝叶斯收缩  ·  L4 在线模型  ·  每日自动收集  ·  定期提醒校准")
        t2.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};")
        self._head_sub = t2
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
        self._build_pnf_tab()
        self._build_timeline_tab()
        self.tabs.setCurrentIndex(0)  # 默认落在模型校准 (校准中心旗舰页)
        self._dirty_tabs = set(range(self.tabs.count()))
        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_stylesheet(self):
        """按当前主题生成全局样式表 (构造与 apply_theme 时重建)。"""
        _bg = theme.C_BG
        _panel = theme.C_PANEL
        _border = theme.C_BORDER
        _btn_bg = theme.C.get("btn", _panel)
        _btn_hv = theme.C.get("btn_hover", _btn_bg)
        return f"""
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
        """

    def _retheme_charts(self):
        """重刷 4 个 pyqtgraph 图表背景 (构造期烧入的配色在主题切换后会残留)。"""
        for name in ("acc_chart", "sig_chart",
                     "pnf_chart_tiers", "pnf_chart_calib"):
            chart = getattr(self, name, None)
            if chart is None:
                continue
            try:
                chart.setBackground(theme.C_BG)
                chart.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
            except Exception:
                pass

    def apply_theme(self):
        """主题切换后重建本 Tab 样式并重渲染 (深色护眼/浅色互换)。"""
        self.setStyleSheet(self._build_stylesheet())
        try:
            self._head_strip.setStyleSheet(
                f"background:{theme.C_ACCENT};border-radius:2px;")
            self._head_sub.setStyleSheet(
                f"color:{theme.C_MUTED};font-size:{theme.font_pt('caption')};")
        except Exception:
            pass
        self._retheme_charts()
        if self._rendered_once:
            self.render_all()

    # ══════════════════════════════════════════════════════════════════
    #  缓存读取 (mtime 失效, 单次渲染流程共享一份数据)
    # ══════════════════════════════════════════════════════════════════
    def _signals_cached(self):
        return self._cache.get("signals", SIGNAL_ACCURACY_FILE, load_signals)

    def _accuracy_cached(self):
        return self._cache.get("accuracy", ACCURACY_FILE, load_accuracy)

    def _feedback_cached(self):
        return self._cache.get("feedback", FEEDBACK_FILE, load_feedback)

    def _accuracy_stats_cached(self):
        """accuracy_stats 计算较重 (~0.3s), 按 mtime 记忆化。"""
        try:
            mtime = os.path.getmtime(ACCURACY_FILE)
        except OSError:
            mtime = None
        if self._acc_stats_memo and self._acc_stats_memo[0] == mtime:
            return self._acc_stats_memo[1]
        stats = accuracy_stats(self._accuracy_cached())
        self._acc_stats_memo = (mtime, stats)
        return stats

    # ══════════════════════════════════════════════════════════════════
    #  总览卡片
    # ══════════════════════════════════════════════════════════════════
    def _build_overview_cards(self, parent):
        row = QHBoxLayout()
        row.setSpacing(10)
        self._ov_cards = {}
        for key, title in [("acc", "分析准确度"), ("sig", "信号准确度"),
                           ("pnf", "点数图(PNF)"),
                           ("fb", "阶段带反馈"), ("calib", "校准状态")]:
            card = _card()
            card.setMinimumWidth(200)
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(2)
            ht = QHBoxLayout()
            t_label = _card_title(title)
            badge = QLabel()
            badge.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('mini')};")
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
        val_label.setStyleSheet(f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{color or theme.C_TEXT};")
        sub_label.setText(sub)
        if badge_text:
            badge.setText(badge_text)
            badge.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('mini')};color:{badge_color or theme.C_MUTED};")
        else:
            badge.setText("")

    def _render_overview(self):
        acc = self._accuracy_stats_cached()
        sig = signal_stats(self._signals_cached())
        fb = self._feedback_cached()
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
            # 统计20根方向命中占比 (多头涨记中, 空头跌记中)
            hits = 0
            n_all = 0
            for kind in ("event", "vsa"):
                for t, s in sig[kind].items():
                    d = event_dir(t) if kind == "event" else vsa_dir(t)
                    h20 = s["horizons"].get("20", [])
                    n_all += len(h20)
                    hits += sum(1 for v in h20 if (v < 0 if d < 0 else v > 0))
            if n_all:
                win = hits / n_all * 100
                sig_hit = f"{win:.0f}%"
                sig_color = theme.C_UP if win >= 55 else (theme.C_DOWN if win < 45 else theme.C_AMBER)
        self._set_card("sig", f"{sig_s['evaluated']}",
                       f"已评估  ·  待评估 {sig_s['pending']}",
                       color=sig_color, badge_text=sig_hit, badge_color=sig_color)

        # 点数图(PNF)测算卡
        pnf_rep = _pnf_load_latest()
        pnf_n = pnf_rep.get("total_segments", 0) or 0
        near_total = (pnf_rep.get("near") or {}).get("total") or {}
        pnf_hr = near_total.get("rate%") if near_total.get("n") else None
        # 概率校准:|bias|>10pt 的占比用来标色 (小=好)
        cal = pnf_rep.get("calibration") or []
        bad_bias = sum(1 for c in cal if abs(c.get("bias_pp", 0)) > 10)
        pnf_badge = ""
        pnf_color = theme.C_TEXT
        if pnf_n > 0 and pnf_hr is not None:
            pnf_badge = f"近端 {pnf_hr:.0f}%"
            if pnf_hr >= 85:
                pnf_color = theme.C_UP
            elif pnf_hr >= 65:
                pnf_color = theme.C_AMBER
            else:
                pnf_color = theme.C_DOWN
        if bad_bias > 0:
            pnf_color = theme.C_AMBER
        pnf_sub = f"样本 {pnf_n}段 · 校准偏差桶 {bad_bias}/{len(cal)}" if cal else (
            f"样本 {pnf_n}段" if pnf_n else "尚无评估 — 点 ⚡立即评估 或运行 cron")
        ts = pnf_rep.get("ts") or ""
        if ts:
            # ts 形如 2026-08-21T07:10:16, 截成 MM-DD HH:MM
            t = ts[5:16].replace("T", " ")
            pnf_sub = f"{t}  {pnf_sub}"
        self._set_card("pnf", f"{pnf_n}" if pnf_n else "—",
                       pnf_sub, color=pnf_color,
                       badge_text=pnf_badge, badge_color=pnf_color)

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
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
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
        note.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(note)

        # ── 数据同步区: 多端校准数据经固定共享私有 git 仓库汇合 (docs/plan_multiuser_sync.md) ──
        sync_box = QFrame()
        sync_box.setStyleSheet(f"QFrame {{ background:{theme.C_PANEL};"
                               f"border:1px solid {theme.C_BORDER};border-radius:4px; }}")
        sl = QVBoxLayout(sync_box)
        sl.setContentsMargins(10, 6, 10, 6)
        sl.setSpacing(6)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        st_title = QLabel("数据同步")
        st_title.setStyleSheet("font-weight:bold;")
        row1.addWidget(st_title)
        repo_tip = QLabel(CALIB_SYNC_REPO)
        repo_tip.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        row1.addWidget(repo_tip)
        row1.addStretch(1)
        self._sync_btn = QPushButton("⇅ 立即同步")
        self._sync_btn.setToolTip("pull 远端 → 合并进本地 → 有新增则重训模型 → push")
        self._sync_btn.clicked.connect(self._on_sync_now)
        row1.addWidget(self._sync_btn)
        sl.addLayout(row1)
        lay.addWidget(sync_box)
        self._sync_status = QLabel("")
        self._sync_status.setWordWrap(True)
        self._sync_status.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        lay.addWidget(self._sync_status)

        self._model_coef_scroll = QScrollArea()
        self._model_coef_scroll.setWidgetResizable(True)
        self._model_coef_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._model_coef_content = QWidget()
        self._model_coef_lay = QVBoxLayout(self._model_coef_content)
        self._model_coef_lay.setContentsMargins(0, 0, 0, 0)
        self._model_coef_lay.setSpacing(2)
        self._model_coef_scroll.setWidget(self._model_coef_content)
        lay.addWidget(self._model_coef_scroll, 1)

        self.tabs.addTab(page, "模型校准")
        self.refresh_sync_url()
        self._render_sync_status()

    def refresh_sync_url(self):
        """确保固定共享校准仓地址已落盘到 settings, 供 sync 服务读取。无输入 UI。"""
        try:
            from wyckoff.storage import load_settings, save_settings
            s = load_settings()
            if s.get("calib_repo_url") != CALIB_SYNC_REPO:
                s["calib_repo_url"] = CALIB_SYNC_REPO
                save_settings(s)
        except Exception:
            pass

    def _render_sync_status(self):
        """同步状态行: 上次同步摘要 (读 settings 缓存, 不触发网络)。"""
        from wyckoff.storage import load_settings

        rec = load_settings().get("calib_last_sync") or {}
        if not rec:
            self._sync_status.setText(
                "尚未同步。GitHub 建私有空仓后填入地址 → 保存 → 立即同步 "
                "(首推即初始全量库)。")
            return
        import time as _time
        parts = [f"上次同步 {_time.strftime('%Y-%m-%d %H:%M', _time.localtime(rec.get('ts', 0)))}"]
        if "signals_new" in rec or "feedback_new" in rec:
            n_new = int(rec.get("signals_new", 0) or 0) + int(rec.get("feedback_new", 0) or 0)
            parts.append(f"本次新增 {n_new} 条")
        elif rec.get("pushed"):
            parts.append("已推送")
        else:
            parts.append("无变更")
        if rec.get("retrained"):
            parts.append("已重训模型")
        color = theme.C_DOWN if rec.get("error") else theme.C_MUTED
        text = "  ·  ".join(parts)
        if rec.get("error"):
            text += f"  ·  ⚠ {rec['error']}"
        self._sync_status.setText(text)
        self._sync_status.setStyleSheet(f"color:{color};font-size:{theme.font_pt('mini')};")

    def _on_sync_now(self):
        if self._sync_th is not None and self._sync_th.isRunning():
            return
        # 固定共享校准仓: 无需用户填写 URL / 凭据
        from wyckoff.storage import load_settings, save_settings

        s = load_settings()
        if s.get("calib_repo_url") != CALIB_SYNC_REPO:
            s["calib_repo_url"] = CALIB_SYNC_REPO
            save_settings(s)
        btn = self._sync_btn
        btn.setEnabled(False)
        btn.setText("同步中...")

        def _work():
            from sync import service as sync_service

            r = sync_service.sync()
            # 附带远端概况供状态行展示 (fetch 失败不影响主流程)
            try:
                r["remote_info"] = sync_service.status()
            except Exception:
                pass
            return r

        th = _RetrainThread(_work, self)
        th.finished.connect(lambda res: self._on_sync_done(btn, th, res))
        self._sync_th = th
        th.start()

    def _on_sync_done(self, btn, th, result):
        self._sync_th = None
        btn.setEnabled(True)
        btn.setText("⇅ 立即同步")
        if result.get("skipped"):
            self._sync_status.setText(f"已跳过同步: {result['skipped']}")
            return
        err = result.get("error")
        if isinstance(result, dict) and result.get("ok"):
            n_new = (int(result.get("signals_new", 0) or 0)
                     + int(result.get("feedback_new", 0) or 0))
            n_upd = (int(result.get("signals_upd", 0) or 0)
                     + int(result.get("feedback_upd", 0) or 0))
            msg = f"同步完成: 新增 {n_new} 条, 更新 {n_upd} 条"
            if result.get("retrained"):
                st = result.get("model_metrics") or {}
                auc = st.get("auc_oos")
                msg += f", 已用合并数据重训模型 (OOS AUC {auc:.3f})" if auc else ", 已重训模型"
            ri = result.get("remote_info") or {}
            warn = ri.get("feat_version_warn")
            if warn:
                msg += f"\n⚠ {warn}"
            contributors = ri.get("n_contributors")
            if contributors:
                msg += f"  ·  远端贡献端 {contributors} 个"
            self._sync_status.setText(msg)
            self._sync_status.setStyleSheet(f"color:{theme.C_UP};font-size:{theme.font_pt('mini')};")
            self._render_model_tab()
        elif err:
            self._sync_status.setText(f"⚠ 同步失败: {err}")
            self._sync_status.setStyleSheet(f"color:{theme.C_DOWN};font-size:{theme.font_pt('mini')};")

    def _render_model_tab(self, data=None):
        from wyckoff.online_model import MODEL_MIN_AUC, MODEL_MIN_OOS, MODEL_MIN_TRAIN, model_status
        st = data.get("model_status") if data else model_status()
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
            val.setStyleSheet(f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{color or theme.C_TEXT};")
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
        # L5 语境特征覆盖度 + 特征集版本过期提示
        from wyckoff.online_model import FEATURE_VERSION as _CUR_FV
        n_ctx = int(st.get("n_ctx_labels", 0) or 0)
        if n_ctx:
            self._model_cards["labels"][1].setText(
                f"其中 {n_ctx} 条带 L5 语境特征")
        fv = int(st.get("feat_version", 0) or 0)
        if fv and fv != _CUR_FV and not ready:
            self._model_cards["gate"][1].setText(
                f"特征集 v{fv} 已过时 (当前 v{_CUR_FV}) — 请重新训练")

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
            nm.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('mini')};")
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
        if self._model_th is not None and self._model_th.isRunning():
            return
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
        self._model_th = None
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
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
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
        self.fb_reliability.setStyleSheet(f"color:{theme.C_ACCENT};font-size:{theme.font_pt('mini')};")
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
        """只存数据不渲染 (渲染由 render_all / tab 切换统一调度)。"""
        self._last_segs = segs
        self._last_symbol = symbol or ""
        self._last_datalen = datalen
        self._last_scale = scale
        self._last_df = df

    def _phase_reliability_text(self):
        """L5 阶段判定可信度: 各阶段标注正确率 (L1 收缩) 一行概览。"""
        try:
            from wyckoff.storage import phase_reliability
            rel = phase_reliability(self._feedback_cached())
        except Exception:
            return ""
        if not rel:
            return "(暂无阶段标注 — 分析后在下方标注对错, 自动累积判定可信度)"
        parts = []
        for lb, s in rel.items():
            tag = "样本不足" if s["n"] < 5 else f"{s['shrunk'] * 100:.0f}%"
            parts.append(f"{lb} {s['correct']}/{s['n']} 正确→{tag}")
        return "阶段判定可信度(L1收缩): " + "  ·  ".join(parts)

    def _render_feedback(self, data=None):
        _clear(self.fb_lay)
        # 使用预计算的数据
        if data:
            fb = data.get("feedback")
            reliability = data.get("reliability", {})
            # 更新可信度标签
            if reliability:
                parts = []
                for lb, s in reliability.items():
                    tag = "样本不足" if s["n"] < 5 else f"{s['shrunk'] * 100:.0f}%"
                    parts.append(f"{lb} {s['correct']}/{s['n']} 正确→{tag}")
                self.fb_reliability.setText("阶段判定可信度(L1收缩): " + "  ·  ".join(parts))
            else:
                self.fb_reliability.setText("(暂无阶段标注 — 分析后在下方标注对错, 自动累积判定可信度)")
        else:
            self.fb_reliability.setText(self._phase_reliability_text())
            fb = self._feedback_cached()

        if not self._last_segs:
            self._placeholder(self.fb_lay, "请先完成一次分析 (开始分析 或 双击自选股)")
            return
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
            meta.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
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
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
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
        self.acc_tbl.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
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

    def _render_accuracy(self, data=None):
        if data:
            records = data.get("records", [])
            stats = data.get("stats")
        else:
            records = self._accuracy_cached()
            stats = self._accuracy_stats_cached()
        # 更新筛选器
        codes = sorted({r.get("code") for r in records if r.get("code")})
        self.acc_filter_code.currentData()
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
        waiting = sum(1 for r in self._accuracy_cached() if r.get("waiting"))
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
        # 新闻情绪 A/B: 同一样本, 带新闻 vs 纯技术面融合的方向命中率
        nw = stats["horizons"][str(HORIZONS[0])].get("news_with", {})
        if nw.get("n"):
            lines.append("")
            lines.append(f"新闻情绪A/B (n={nw['n']})   带新闻 {self._hit(nw)}  "
                         f"纯技术面 {self._hit(stats['horizons'][str(HORIZONS[0])]['news_without'])}")
            for h in HORIZONS:
                e = stats["horizons"][str(h)]
                wn, won = e.get("news_with"), e.get("news_without")
                if not wn or not wn.get("n"):
                    continue
                d = e.get("news_diff")
                dstr = f"{d * 100:+.1f}%" if d is not None else "-"
                lines.append(
                    f"  {h}根  带新闻 {self._hit(wn):>8}  纯技术面 {self._hit(won):>8}  "
                    f"Δ {dstr:>8}")
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
            self.acc_chart.setTitle(
                f"暂无已评估数据 · 共 {stats['total']} 条预测, 需等未来行情 "
                f"({HORIZONS[0]}/{HORIZONS[1]}/{HORIZONS[2]} 根K线后逐级到期评估)",
                color=theme.C_MUTED, size="10pt")
            return
        self.acc_chart.setTitle("")
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

    def _render_accuracy_list(self, records=None):
        if records is None:
            records = self._accuracy_cached()
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
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
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
        self.sig_shrink.setStyleSheet(f"color:{theme.C_ACCENT};font-size:{theme.font_pt('mini')};")
        top_lay.addWidget(self.sig_shrink)

        # L5 多周期一致性 (5/10/20/40 收缩胜率 + 边缘衰减判定)
        self.sig_consistency = QLabel()
        self.sig_consistency.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_consistency.setWordWrap(True)
        self.sig_consistency.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        top_lay.addWidget(self.sig_consistency)

        # 验证结果
        self.sig_validation = QLabel()
        self.sig_validation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sig_validation.setWordWrap(True)
        self.sig_validation.setStyleSheet(f"color:{theme.C_ACCENT};font-size:{theme.font_pt('caption')};")
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
        vlab.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('caption')};")
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
        self.tabs.addTab(page, "信号校准")

    @staticmethod
    def _sig_mark(rec, ret):
        kind = rec.get("kind")
        t = rec.get("type", "")
        d = event_dir(t) if kind == "event" else vsa_dir(t)
        if d == 0:
            return ""
        from wyckoff.config import dir_hit
        return "✓" if dir_hit(kind, t, ret) else "✗"

    def _render_signal_accuracy(self, data=None):
        if data:
            records = data.get("records", [])
            stats = data.get("stats")
            vstats = data.get("vstats")
        else:
            records = self._signals_cached()
            stats = signal_stats(records)
            from wyckoff.validation import compute_validation_stats
            vstats = compute_validation_stats(records)

    def _consistency_summary(self):
        """L5 多周期一致性: 各类型 5/10/20/40 根收缩胜率 + 边缘判定。"""
        try:
            from wyckoff.signal_accuracy import load_win_rates, win_rate_profile
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
                d = event_dir(t) if kind == "event" else vsa_dir(t)
                win = sum(1 for v in h20 if (v < 0 if d < 0 else v > 0)) / len(h20) * 100
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

    def _render_signal_list(self, records=None):
        _clear(self.sig_lay)
        if records is None:
            records = self._signals_cached()
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
        cap = int(self._settings.get(S.TTS.MAX_CHARS, 3000) or 6000)
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
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
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

    def _render_timeline(self, data=None):
        _clear(self.tl_lay)
        if data:
            records = data.get("records", [])
        else:
            records = self._signals_cached()
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
        # 最近优先: 全局按日期降序再按股票分组, 截断时保住最新的记录
        sel.sort(key=lambda r: str(r.get("date", "")), reverse=True)
        groups = OrderedDict()
        for r in sel:
            groups.setdefault(r.get("code"), []).append(r)
        n_rows = 0
        truncated = False
        for code, recs in groups.items():
            name = next((r.get("name") or "" for r in recs), "")
            hd = QLabel(f"<b>{name} {code}</b>  ({len(recs)} 条信号)")
            hd.setStyleSheet(f"color:{theme.C_ACCENT};margin-top:6px;")
            self.tl_lay.addWidget(hd)
            for r in recs:
                if n_rows >= _TL_MAX_ROWS:
                    truncated = True
                    break
                n_rows += 1
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
            if truncated:
                break
        if truncated:
            note = QLabel(f"(记录过多, 仅显示最近 {_TL_MAX_ROWS} 条 — "
                          "用右上角股票筛选缩小范围)")
            note.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
            self.tl_lay.addWidget(note)
        self.tl_lay.addStretch(1)

    # ══════════════════════════════════════════════════════════════════
    #  点数图 (PNF) 三档目标测算校准
    # ══════════════════════════════════════════════════════════════════
    def _build_pnf_tab(self):
        page = QWidget()
        self._pnf_page = page
        lay = QVBoxLayout(page)
        lay.setContentsMargins(6, 6, 6, 6)

        head = QHBoxLayout()
        t = QLabel("点数图(PNF) 三档目标测算校准")
        t.setStyleSheet(f"font-weight:bold;font-size:{theme.font_pt('body-sm')};")
        head.addWidget(t)
        t2 = QLabel("TR/POC / 保守·中·激进三档 / 概率校准曲线")
        t2.setStyleSheet(f"color:{theme.C_MUTED};")
        head.addWidget(t2)
        head.addStretch(1)
        btn_eval = QPushButton("⚡ 立即评估")
        btn_eval.setToolTip("生成合成行情+真实股票,重新评估三档目标准确率")
        btn_eval.clicked.connect(self._on_pnf_eval)
        head.addWidget(btn_eval)
        btn_reload = QPushButton("⟳ 重载报告")
        btn_reload.setToolTip("从 latest.json 重新加载最近一次评估结果")
        btn_reload.clicked.connect(self._render_pnf_tab)
        head.addWidget(btn_reload)
        btn_export = QPushButton("📤 导出JSON")
        btn_export.setToolTip("导出最近一次 PNF 评估 JSON 报告副本")
        btn_export.clicked.connect(self._on_pnf_export)
        head.addWidget(btn_export)
        lay.addLayout(head)

        # ── 顶部:近端口径 + 三档(全部样本) 汇总卡片 ──
        self._pnf_cards = {}
        row = QHBoxLayout()
        row.setSpacing(10)
        for key, title in [
            ("near_total", "近端目标(±4%)"),
            ("near_up",    "近端·上涨"),
            ("near_dn",    "近端·下跌"),
            ("cons",       "保守档(到达)"),
            ("mid",        "中档(到达)"),
            ("agg",        "激进档(到达)"),
        ]:
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
            self._pnf_cards[key] = (val, sub)
            row.addWidget(card, 1)
        lay.addLayout(row)

        # 指标说明
        note = QLabel(
            "三档口径: 保守 (TR极值,最近) · 中档 (POC控制点) · 激进 (横向计数线,最远)。  "
            "理想: 保守档到达率最高 (≥65%) · 激进档最低 (30~50%) · 概率偏差 |bias|&lt;±10pt 为校准合格。  "
            f"报告目录: <code>{_PNF_ACC_DIR}</code>")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(note)

        # ── 中部: 两个图表 (左:三档到达率柱状图  右:概率校准可靠性曲线) ──
        charts = QHBoxLayout()
        charts.setSpacing(8)
        self.pnf_chart_tiers = pg.PlotWidget(title="三档到达率 · 按保守/中/激进")
        self.pnf_chart_tiers.setBackground(theme.C_BG)
        self.pnf_chart_tiers.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        self.pnf_chart_tiers.showGrid(x=False, y=True, alpha=0.3)
        self.pnf_chart_tiers.getAxis('bottom').setPen(pg.mkPen(theme.C_MUTED))
        self.pnf_chart_tiers.getAxis('left').setPen(pg.mkPen(theme.C_MUTED))
        self.pnf_chart_tiers.getAxis('left').setRange(0, 100)
        self.pnf_chart_tiers.getAxis('left').setLabel("到达率 %")
        charts.addWidget(self.pnf_chart_tiers, 1)

        self.pnf_chart_calib = pg.PlotWidget(title="概率校准 (预测x轴 vs 实际到达y轴)")
        self.pnf_chart_calib.setBackground(theme.C_BG)
        self.pnf_chart_calib.getViewBox().setBackgroundColor(pg.mkColor(theme.C_BG))
        self.pnf_chart_calib.showGrid(x=True, y=True, alpha=0.3)
        self.pnf_chart_calib.getAxis('bottom').setPen(pg.mkPen(theme.C_MUTED))
        self.pnf_chart_calib.getAxis('left').setPen(pg.mkPen(theme.C_MUTED))
        self.pnf_chart_calib.setAspectLocked(lock=False)
        self.pnf_chart_calib.getAxis('bottom').setRange(30, 100)
        self.pnf_chart_calib.getAxis('left').setRange(30, 100)
        self.pnf_chart_calib.getAxis('bottom').setLabel("均预测概率 %")
        self.pnf_chart_calib.getAxis('left').setLabel("实际到达率 %")
        charts.addWidget(self.pnf_chart_calib, 1)
        lay.addLayout(charts)

        # ── 底部: 滚动区 (上半=三档明细表  下半=概率校准表) ──
        self.pnf_scroll = QScrollArea()
        self.pnf_scroll.setWidgetResizable(True)
        self.pnf_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pnf_content = QWidget()
        self.pnf_lay = QVBoxLayout(self.pnf_content)
        self.pnf_lay.setContentsMargins(0, 0, 0, 0)
        self.pnf_lay.setSpacing(6)
        self.pnf_scroll.setWidget(self.pnf_content)
        lay.addWidget(self.pnf_scroll, 1)

        self.tabs.addTab(page, "点数图校准")

    def _on_pnf_eval(self):
        """在 PNF 标签页点击立即评估: 后台线程只跑 PNF (更快反馈)。"""
        if self._pnf_th is not None and self._pnf_th.isRunning():
            QMessageBox.information(self, "评估中", "点数图评估正在进行, 请稍候。")
            return
        self._pnf_th = _PnfEvalThread(self)
        self._pnf_th.finished.connect(self._after_pnf_eval)
        old_title = self.windowTitle()
        self.setWindowTitle("点数图校准 (评估中...)")
        self._pnf_old_title = old_title
        self._pnf_th.start()

    def _after_pnf_eval(self, errors=None):
        t = getattr(self, "_pnf_old_title", None) or "校准中心"
        self.setWindowTitle(t)
        if errors:
            QMessageBox.warning(self, "评估失败", "\n".join(errors))
        self.render_all()

    def _on_pnf_export(self):
        import json
        import os
        import shutil
        rep = _pnf_load_latest()
        if not rep:
            QMessageBox.information(self, "PNF 导出", "尚无最新评估报告 — 请先点 ⚡立即评估")
            return
        d = QFileDialog.getExistingDirectory(self, "选择导出目录 (PNF 三档目标准确率)")
        if not d:
            return
        ts = (rep.get("ts") or "").replace(":", "-")
        name = f"pnf_accuracy_{ts}.json" if ts else "pnf_accuracy.json"
        path = os.path.join(d, name)
        # 直接复制 latest.json 到用户目录 (保证与 UI 显示完全一致)
        src = os.path.join(_PNF_ACC_DIR, "pnf_latest.json")
        if os.path.exists(src):
            shutil.copy2(src, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
        QMessageBox.information(self, "导出完成", f"PNF 评估报告已导出到:\n{path}")

    def _render_pnf_tab(self, data=None):
        rep = data.get("report") if data else _pnf_load_latest()
        _clear(self.pnf_lay)
        # ── 汇总卡片 ──
        def _card_set(key, text, color=None, sub=""):
            if key not in self._pnf_cards:
                return
            val, s = self._pnf_cards[key]
            val.setText(str(text))
            c = color or theme.C_TEXT
            val.setStyleSheet(f"font-size:{theme.font_pt('h1')};font-weight:bold;color:{c};")
            s.setText(sub)
        near = rep.get("near") or {}
        def _near_sub(k):
            d = near.get(k) or {}
            n = d.get("n", 0) or 0
            return f"{d.get('hit', 0)}/{n}" if n else "—"
        def _hr_color(r, lo=45, hi=65):
            if r is None:
                return theme.C_MUTED
            if r >= hi:
                return theme.C_UP
            if r >= lo:
                return theme.C_AMBER
            return theme.C_DOWN
        tot = near.get("total") or {}
        _card_set("near_total",
                  f"{tot['rate%']:.0f}%" if tot.get("n") else "—",
                  color=_hr_color(tot.get("rate%"), 70, 85),
                  sub=_near_sub("total"))
        up = near.get("up") or {}
        _card_set("near_up",
                  f"{up['rate%']:.0f}%" if up.get("n") else "—",
                  color=_hr_color(up.get("rate%"), 70, 85),
                  sub=_near_sub("up"))
        dn = near.get("dn") or {}
        _card_set("near_dn",
                  f"{dn['rate%']:.0f}%" if dn.get("n") else "—",
                  color=_hr_color(dn.get("rate%"), 70, 85),
                  sub=_near_sub("dn"))

        # 三档汇总 (bucket="全部" × dir=上方/下方 × tier)
        tiers = rep.get("tiers") or []
        all_rows = [t for t in tiers if t.get("bucket") == "全部"]
        tmap = {}
        for r in all_rows:
            tmap.setdefault(r["tier"], []).append(r)
        def _merge(list_of_rows):
            n = sum(r.get("n_valid", 0) for r in list_of_rows)
            h = sum(r.get("n_hit", 0) for r in list_of_rows)
            return n, h, (h / n * 100.0 if n else None)
        for tk, key in [("保守", "cons"), ("中", "mid"), ("激进", "agg")]:
            n, h, hr = _merge(tmap.get(tk, []))
            if tk == "保守":
                lo, hi = 55, 65
            elif tk == "中":
                lo, hi = 40, 55
            else:
                lo, hi = 25, 40
            _card_set(key, f"{hr:.0f}%" if n else "—",
                      color=_hr_color(hr, lo, hi),
                      sub=f"{h}/{n}" if n else "—")

        # ── 图表1: 三档到达率 (全部分桶 × 档位) ──
        self.pnf_chart_tiers.clear()
        if tiers:
            buckets_uniq = list(dict.fromkeys(t["bucket"] for t in tiers))
            tier_order = ["保守", "中", "激进"]
            palette = [theme.C_UP, theme.C_AMBER, theme.C_DOWN]
            # x 轴: 每个 bucket 3 根
            xs_cons, xs_mid, xs_agg = [], [], []
            h_cons, h_mid, h_agg = [], [], []
            for bi, b in enumerate(buckets_uniq):
                for ti, tk in enumerate(tier_order):
                    rows = [t for t in tiers if t["bucket"] == b and t["tier"] == tk]
                    n, h, hr = _merge(rows)
                    if not n:
                        continue
                    xi = bi * 3 + ti
                    if tk == "保守":
                        xs_cons.append(xi)
                        h_cons.append(hr)
                    elif tk == "中":
                        xs_mid.append(xi)
                        h_mid.append(hr)
                    else:
                        xs_agg.append(xi)
                        h_agg.append(hr)
            if xs_cons:
                self.pnf_chart_tiers.addItem(pg.BarGraphItem(
                    x=xs_cons, height=h_cons, width=0.8,
                    brush=pg.mkBrush(palette[0]), name="保守"))
            if xs_mid:
                self.pnf_chart_tiers.addItem(pg.BarGraphItem(
                    x=xs_mid, height=h_mid, width=0.8,
                    brush=pg.mkBrush(palette[1]), name="中"))
            if xs_agg:
                self.pnf_chart_tiers.addItem(pg.BarGraphItem(
                    x=xs_agg, height=h_agg, width=0.8,
                    brush=pg.mkBrush(palette[2]), name="激进"))
            # x 轴刻度: bucket 名 → 中心位置 (保守的 xi + 1)
            ticks = []
            for bi, b in enumerate(buckets_uniq):
                center = bi * 3 + 1
                ticks.append((center, b))
            self.pnf_chart_tiers.getAxis('bottom').setTicks([ticks])

        # ── 图表2: 概率校准散点 (x=avg_prob% y=hit_rate%) + 45度理想线 ──
        self.pnf_chart_calib.clear()
        cal = rep.get("calibration") or []
        if cal:
            xs, ys, sizes, colors = [], [], [], []
            for c in cal:
                ap = c.get("avg_prob%")
                hr = c.get("hit_rate%")
                if ap is None or hr is None:
                    continue
                xs.append(ap)
                ys.append(hr)
                n = c.get("n", 1) or 1
                sizes.append(min(24, 6 + n // 30))
                bias = c.get("bias_pp", 0) or 0
                if abs(bias) <= 10:
                    colors.append(pg.mkColor(theme.C_UP))
                elif abs(bias) <= 20:
                    colors.append(pg.mkColor(theme.C_AMBER))
                else:
                    colors.append(pg.mkColor(theme.C_DOWN))
            if xs:
                sp = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None))
                sp.setData(x=xs, y=ys, size=sizes, brush=colors)
                self.pnf_chart_calib.addItem(sp)
            # 45° 理想线 y=x
            ideal_x = [30, 100]
            ideal_y = [30, 100]
            self.pnf_chart_calib.plot(
                ideal_x, ideal_y,
                pen=pg.mkPen(theme.C_MUTED, width=1, style=Qt.PenStyle.DashLine))

        # ── 没有报告: 提示占位 ──
        if not rep:
            self._placeholder(
                self.pnf_lay,
                "(尚无 PNF 评估结果 — 请点击 ⚡立即评估 或先运行 "
                "`python scripts/eval_pnf_tier_accuracy.py --run` 生成报告。\n"
                "生产使用: 把脚本加入 crontab (每日 02:10) 自动跑, 结果落盘到 latest.json)")
            return

        # ── 明细表 1: 三档 × 分桶 ──
        h_sec1, bs_sec1 = _row_frame(vertical=True)
        t1 = QLabel("三档到达率 / 概率校准明细")
        t1.setStyleSheet("font-weight:bold;")
        bs_sec1.addWidget(t1)
        hdr = (f"{'分桶':<12}{'方向':<6}{'档位':<6}{'样本':>6}{'到达':>6}"
               f"{'到达率':>8}{'均概率':>9}{'校准差':>8}{'均空间':>8}")
        hl = QLabel(hdr)
        hl.setStyleSheet(f"color:{theme.C_MUTED};font-weight:bold;font-size:{theme.font_pt('mini')};")
        hl.setFont(pg.QtGui.QFont("Monospace", 9))
        bs_sec1.addWidget(hl)
        lines = []
        for t in tiers:
            hr_str = f"{t['hit_rate%']:.1f}%"
            prob_str = f"{t['avg_prob%']:.1f}%" if t.get('avg_prob%') is not None else "  --"
            cal_str = f"{t['calib_pp']:+.1f}pt" if t.get('calib_pp') is not None else "    --"
            sp_str = f"{t['avg_space%']:.1f}%" if t.get('avg_space%') is not None else "   --"
            lines.append(
                f"{t['bucket']:<12}{t['dir']:<6}{t['tier']:<6}"
                f"{t['n_valid']:>6}{t['n_hit']:>6}"
                f"{hr_str:>8}{prob_str:>9}{cal_str:>8}{sp_str:>8}"
            )
        lbl1 = QLabel("\n".join(lines))
        lbl1.setStyleSheet(f"color:{theme.C_TEXT};font-size:{theme.font_pt('mini')};")
        lbl1.setFont(pg.QtGui.QFont("Monospace", 9))
        lbl1.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bs_sec1.addWidget(lbl1)
        self.pnf_lay.addWidget(h_sec1)

        # ── 明细表 2: 概率校准 (5段×3档=15桶) ──
        h_sec2, bs_sec2 = _row_frame(vertical=True)
        t2 = QLabel("概率校准表 (桶=预测概率分档 × 档位)")
        t2.setStyleSheet("font-weight:bold;")
        bs_sec2.addWidget(t2)
        hdr2 = f"{'预测概率档':<18}{'样本':>6}{'到达':>6}{'到达率':>8}{'均预测概率':>11}{'偏差':>8}"
        hl2 = QLabel(hdr2)
        hl2.setStyleSheet(f"color:{theme.C_MUTED};font-weight:bold;font-size:{theme.font_pt('mini')};")
        hl2.setFont(pg.QtGui.QFont("Monospace", 9))
        bs_sec2.addWidget(hl2)
        lines2 = []
        for c in cal:
            hr_str = f"{c['hit_rate%']:>7.1f}%"
            ap_str = f"{c['avg_prob%']:>10.1f}%"
            bias = c.get("bias_pp", 0) or 0
            mark = "  ⚠" if abs(bias) > 10 else ("  ✓" if abs(bias) <= 5 else "")
            lines2.append(
                f"{c['bucket']:<18}{c['n']:>6}{c['n_hit']:>6}"
                f"{hr_str:>8}{ap_str:>11}{bias:>+7.1f}pt{mark}"
            )
        lbl2 = QLabel("\n".join(lines2))
        lbl2.setStyleSheet(f"color:{theme.C_TEXT};font-size:{theme.font_pt('mini')};")
        lbl2.setFont(pg.QtGui.QFont("Monospace", 9))
        lbl2.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bs_sec2.addWidget(lbl2)
        self.pnf_lay.addWidget(h_sec2)

        # 评估时间
        ts = rep.get("ts") or ""
        meta = QLabel(f"评估时间: {ts}  ·  样本 {rep.get('total_segments')} 段  "
                      f"(合成 {rep.get('synthetic_n')} + 真实 {rep.get('real_n')})")
        meta.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('mini')};")
        self.pnf_lay.addWidget(meta)
        self.pnf_lay.addStretch(1)

    # ══════════════════════════════════════════════════════════════════
    #  动作
    # ══════════════════════════════════════════════════════════════════
    def on_eval(self):
        if self._eval_thread is not None and self._eval_thread.isRunning():
            QMessageBox.information(self, "评估中", "全面评估正在进行, 请稍候。")
            return
        self._eval_thread = _EvalThread(self)
        self._eval_thread.finished.connect(self._after_eval)
        self.setWindowTitle("校准中心 (评估中...)")
        self._eval_thread.start()

    def _after_eval(self, errors=None):
        self.setWindowTitle("校准中心")
        if errors:
            QMessageBox.warning(self, "部分评估失败", "\n".join(errors))
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
        from wyckoff.accuracy import load_accuracy, save_accuracy
        from wyckoff.signal_accuracy import invalidate_win_rate_cache, load_signals, save_signals
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
    def _tab_renderers(self):
        """tab index → 渲染方法。渲染开销大, 只在可见/请求时执行。"""
        return {
            0: self._render_model_tab,
            1: self._render_feedback,
            2: self._render_accuracy,
            3: self._render_signal_accuracy,
            4: self._render_pnf_tab,
            5: self._render_timeline,
        }

    def _on_tab_changed(self, idx):
        if self._lazy_paused:
            return
        if idx in self._dirty_tabs:
            self._dirty_tabs.discard(idx)
            fn = self._tab_renderers().get(idx)
            if fn:
                fn()

    def begin_update(self):
        """暂停懒加载渲染 (批量改数据/切 tab 前调用)。"""
        self._lazy_paused = True

    def end_update(self):
        """恢复渲染并刷新总览+当前 tab; 其余 tab 标脏待切换时再渲。"""
        self._lazy_paused = False
        self.render_all()

    def render_all(self):
        """总览卡片 + 当前可见 tab 立即渲染, 其余 tab 标脏 (切换时懒渲染)。

        全量渲染 6 个 tab 在主线程要数秒 (验证统计 ~2s + 上万控件重建),
        懒加载后打开窗口只付当前页成本。
        """
        self._rendered_once = True
        self._render_overview()
        cur = self.tabs.currentIndex()
        self._dirty_tabs = set(range(self.tabs.count())) - {cur}
        self._render_tab_async(cur)

    def _tab_renderers(self):
        return {
            0: lambda: self._render_tab_async(0, "model"),
            1: lambda: self._render_tab_async(1, "feedback"),
            2: lambda: self._render_tab_async(2, "accuracy"),
            3: lambda: self._render_tab_async(3, "signal"),
            4: lambda: self._render_tab_async(4, "pnf"),
            5: lambda: self._render_tab_async(5, "timeline"),
        }

    def _render_tab_async(self, index, key=None):
        """异步渲染指定 tab。key 用于缓存标识。"""
        if key is None:
            keys = ["model", "feedback", "accuracy", "signal", "pnf", "timeline"]
            key = keys[index] if index < len(keys) else f"tab{index}"

        # 检查缓存
        if key in self._tab_data_cache:
            self._apply_tab_data(index, key, self._tab_data_cache[key])
            return

        # 显示加载中
        self._show_tab_loading(index, True)
        self._loading_tabs.add(index)

        # 后台计算任务
        task = _ComputeTask(key, self._compute_tab_data, key)
        task.signals.finished.connect(lambda k, data: self._on_tab_data_ready(index, k, data))
        task.signals.error.connect(lambda msg: self._on_tab_error(index, msg))
        self._thread_pool.start(task)

    def _compute_tab_data(self, key):
        """后台线程计算 tab 所需的所有数据 (无 UI 操作)。"""
        if key == "model":
            from wyckoff.online_model import model_status
            return {"model_status": model_status()}
        elif key == "feedback":
            from wyckoff.storage import load_feedback, phase_reliability
            fb = load_feedback()
            return {
                "feedback": fb,
                "reliability": phase_reliability(fb) if fb else {},
            }
        elif key == "accuracy":
            acc = load_accuracy()
            return {
                "records": acc,
                "stats": accuracy_stats(acc),
            }
        elif key == "signal":
            sig = load_signals()
            stats = signal_stats(sig)
            from wyckoff.validation import compute_validation_stats
            vstats = compute_validation_stats(sig)
            return {
                "records": sig,
                "stats": stats,
                "vstats": vstats,
            }
        elif key == "pnf":
            rep = _pnf_load_latest()
            return {"report": rep}
        elif key == "timeline":
            tl = load_signals()
            return {"records": tl}
        return {}

    @pyqtSlot(int, object, object)
    def _on_tab_data_ready(self, index, key, data):
        """后台计算完成,主线程应用数据并渲染。"""
        self._tab_data_cache[key] = data
        self._loading_tabs.discard(index)
        self._show_tab_loading(index, False)
        self._apply_tab_data(index, key, data)

    @pyqtSlot(int, str)
    def _on_tab_error(self, index, msg):
        self._loading_tabs.discard(index)
        self._show_tab_loading(index, False)
        from wyckoff._log import log_exc
        log_exc(f"Tab {index} 渲染失败", Exception(msg))

    def _show_tab_loading(self, index, loading):
        """在 tab 内显示/隐藏加载指示器。"""
        widget = self.tabs.widget(index)
        if widget is None:
            return
        # 查找或创建加载标签
        loading_label = getattr(widget, "_loading_label", None)
        if loading:
            if loading_label is None:
                loading_label = QLabel("⏳ 正在加载数据...")
                loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                loading_label.setStyleSheet(f"color:{theme.C_MUTED};font-size:{theme.font_pt('body')};padding:40px;")
                widget._loading_label = loading_label
                lay = widget.layout()
                if lay:
                    lay.insertWidget(0, loading_label)
            loading_label.show()
        else:
            if loading_label:
                loading_label.hide()

    def _apply_tab_data(self, index, key, data):
        """根据计算好的数据渲染 tab。"""
        if key == "model":
            self._render_model_tab(data)
        elif key == "feedback":
            self._render_feedback(data)
        elif key == "accuracy":
            self._render_accuracy(data)
        elif key == "signal":
            self._render_signal_accuracy(data)
        elif key == "pnf":
            self._render_pnf_tab(data)
        elif key == "timeline":
            self._render_timeline(data)

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
