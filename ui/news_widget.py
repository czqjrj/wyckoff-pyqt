"""新闻情绪控件: 展示个股/板块相关财经新闻与情绪分析。"""
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import theme


class NewsWidget(QWidget):
    """新闻情绪面板: 情绪评分卡 + 重点利好/利空新闻列表 + 全部新闻流。"""

    def __init__(self, parent=None, font_size=12):
        super().__init__(parent)
        self._font_size = int(font_size)
        self._news_font_size = int(font_size)
        self._news_data = None
        self._setup_ui()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        # ── 头部情绪评分卡 ──
        header_box = QFrame()
        header_box.setObjectName("newsHeaderBox")
        header_box.setFrameShape(QFrame.Shape.StyledPanel)
        h_lay = QHBoxLayout(header_box)
        h_lay.setContentsMargins(16, 12, 16, 12)
        h_lay.setSpacing(20)

        # 情绪大字分
        self.score_label = QLabel("--")
        self.score_label.setObjectName("newsScore")
        self.score_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.score_label.setMinimumWidth(80)
        h_lay.addWidget(self.score_label)

        # 关键指标
        metrics_box = QWidget()
        m_lay = QVBoxLayout(metrics_box)
        m_lay.setContentsMargins(0, 0, 0, 0)
        m_lay.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(16)
        self.bull_label = QLabel("利好: 0")
        self.bull_label.setObjectName("newsBull")
        self.bear_label = QLabel("利空: 0")
        self.bear_label.setObjectName("newsBear")
        self.count_label = QLabel("新闻: 0 条")
        self.count_label.setObjectName("newsCount")
        row1.addWidget(self.bull_label)
        row1.addWidget(self.bear_label)
        row1.addWidget(self.count_label)
        row1.addStretch(1)
        m_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        self.sector_label = QLabel("相关板块: --")
        self.sector_label.setObjectName("newsSector")
        self.latest_label = QLabel("最新: --")
        self.latest_label.setObjectName("newsLatest")
        self.latest_label.setWordWrap(True)
        row2.addWidget(self.sector_label)
        row2.addWidget(self.latest_label, 1)
        m_lay.addLayout(row2)

        # 前瞻事件日历 (解禁/业绩预告/财报披露): 事前排雷条
        self.cal_label = QLabel("")
        self.cal_label.setObjectName("newsCalendar")
        self.cal_label.setWordWrap(True)
        m_lay.addWidget(self.cal_label)

        h_lay.addWidget(metrics_box, 1)

        # 字号调节按钮
        btn_out = QPushButton("A−")
        btn_out.setObjectName("ttsBtn")
        btn_out.setFixedSize(26, 22)
        btn_out.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_out.setToolTip("文字缩小")
        btn_out.clicked.connect(lambda: self._zoom_news_text(-1))
        h_lay.addWidget(btn_out)
        btn_in = QPushButton("A+")
        btn_in.setObjectName("ttsBtn")
        btn_in.setFixedSize(26, 22)
        btn_in.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_in.setToolTip("文字放大")
        btn_in.clicked.connect(lambda: self._zoom_news_text(1))
        h_lay.addWidget(btn_in)

        # 刷新按钮
        self.refresh_btn = QPushButton("刷新新闻")
        self.refresh_btn.setObjectName("ttsBtn")
        self.refresh_btn.setFixedHeight(28)
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self._on_refresh)
        h_lay.addWidget(self.refresh_btn)

        lay.addWidget(header_box)

        # ── 重点事件区 (利好/利空分列) ──
        events_box = QFrame()
        events_box.setObjectName("newsEventsBox")
        events_box.setFrameShape(QFrame.Shape.StyledPanel)
        ev_lay = QHBoxLayout(events_box)
        ev_lay.setContentsMargins(12, 10, 12, 10)
        ev_lay.setSpacing(16)

        # 利好列
        bull_col = QWidget()
        bull_lay = QVBoxLayout(bull_col)
        bull_lay.setContentsMargins(0, 0, 0, 0)
        bull_lay.setSpacing(6)
        bull_title = QLabel("★ 重点利好")
        bull_title.setObjectName("newsSectionTitle")
        bull_title.setStyleSheet(f"color: {theme.C_UP}; font-weight: bold;")
        bull_lay.addWidget(bull_title)
        self.bull_list = QTextBrowser()
        self.bull_list.setObjectName("newsList")
        self.bull_list.setOpenExternalLinks(True)
        self.bull_list.anchorClicked.connect(self._open_link)
        self.bull_list.setMaximumHeight(180)
        self.bull_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bull_lay.addWidget(self.bull_list)
        ev_lay.addWidget(bull_col, 1)

        # 利空列
        bear_col = QWidget()
        bear_lay = QVBoxLayout(bear_col)
        bear_lay.setContentsMargins(0, 0, 0, 0)
        bear_lay.setSpacing(6)
        bear_title = QLabel("⚠ 重点利空")
        bear_title.setObjectName("newsSectionTitle")
        bear_title.setStyleSheet(f"color: {theme.C_DOWN}; font-weight: bold;")
        bear_lay.addWidget(bear_title)
        self.bear_list = QTextBrowser()
        self.bear_list.setObjectName("newsList")
        self.bear_list.setOpenExternalLinks(True)
        self.bear_list.anchorClicked.connect(self._open_link)
        self.bear_list.setMaximumHeight(180)
        self.bear_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bear_lay.addWidget(self.bear_list)
        ev_lay.addWidget(bear_col, 1)

        lay.addWidget(events_box)

        # ── 全部新闻流 ──
        all_box = QFrame()
        all_box.setObjectName("newsAllBox")
        all_box.setFrameShape(QFrame.Shape.StyledPanel)
        all_lay = QVBoxLayout(all_box)
        all_lay.setContentsMargins(12, 10, 12, 10)
        all_lay.setSpacing(6)

        all_title = QLabel("📰 全部新闻流")
        all_title.setObjectName("newsSectionTitle")
        all_title.setStyleSheet("font-weight: bold;")
        all_lay.addWidget(all_title)

        self.all_list = QTextBrowser()
        self.all_list.setObjectName("newsList")
        self.all_list.setOpenExternalLinks(True)
        self.all_list.anchorClicked.connect(self._open_link)
        self.all_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        all_lay.addWidget(self.all_list, 1)

        lay.addWidget(all_box, 1)

        # 占位符
        self._show_empty()

    def _show_empty(self):
        self.score_label.setText("--")
        self.score_label.setStyleSheet(f"color: {theme.C_MUTED}; font-size: {self._fs(4)}pt; font-weight: bold;")
        self.bull_label.setText("利好: 0")
        self.bear_label.setText("利空: 0")
        self.count_label.setText("新闻: 0 条")
        self.sector_label.setText("相关板块: --")
        self.latest_label.setText("最新: --")
        self.cal_label.setText("")
        self.bull_list.setHtml(f'<div style="color:{theme.C_MUTED};text-align:center;padding:20px;">暂无新闻数据</div>')
        self.bear_list.setHtml(f'<div style="color:{theme.C_MUTED};text-align:center;padding:20px;">暂无新闻数据</div>')
        self.all_list.setHtml(f'<div style="color:{theme.C_MUTED};text-align:center;padding:20px;">暂无新闻数据</div>')

    def set_data(self, news_data=None, symbol=None, name=None):
        """接收 analysis.py 返回的 news_sentiment 字典"""
        self._news_data = news_data
        self._symbol = symbol
        self._name = name

        if not news_data:
            self._show_empty()
            return

        score = news_data.get("score", 0.0)
        count = news_data.get("count", 0)
        bull_cnt = news_data.get("bull_count", 0)
        bear_cnt = news_data.get("bear_count", 0)
        sectors = news_data.get("sectors", [])
        key_events = news_data.get("key_events", [])
        risk_flags = news_data.get("risk_flags", [])
        latest = news_data.get("latest")

        # 情绪大字分
        if score > 0.15:
            score_color = theme.C_UP
            score_text = f"+{score:.2f}  偏多"
        elif score < -0.15:
            score_color = theme.C_DOWN
            score_text = f"{score:.2f}  偏空"
        else:
            score_color = theme.C_AMBER
            score_text = f"{score:.2f}  中性"
        self.score_label.setText(score_text)
        self.score_label.setStyleSheet(f"color: {score_color}; font-size: {self._fs(4)}pt; font-weight: bold;")

        # 指标
        self.bull_label.setText(f"利好: {bull_cnt}")
        self.bear_label.setText(f"利空: {bear_cnt}")
        self.count_label.setText(f"新闻: {count} 条")
        self.sector_label.setText(f"相关板块: {', '.join(sectors) if sectors else '无'}")

        # 最新新闻
        if latest:
            lt = latest.get("title", "")
            ls = latest.get("source", "")
            self.latest_label.setText(f"最新: {lt} [{ls}]")
        else:
            self.latest_label.setText("最新: --")

        # 前瞻事件日历 (解禁/业绩预告/财报披露): 临近偏空节点标警示色
        cal = news_data.get("forward_calendar") or {}
        cal_items = cal.get("items") or []
        if cal_items:
            parts = [f"{it.get('date', '')[5:]} {it.get('kind', '')}({it.get('detail', '')})"
                     for it in cal_items[:3]]
            self.cal_label.setText("前瞻日历: " + " · ".join(parts))
        else:
            self.cal_label.setText("")
        rd = cal.get("risk_days")
        if rd is not None and int(rd) <= 7:
            self.cal_label.setStyleSheet(
                f"color: {theme.C_DOWN}; font-size: {self._fs(-1)}pt; font-weight: bold;")
        else:
            self.cal_label.setStyleSheet(
                f"color: {theme.C_AMBER}; font-size: {self._fs(-1)}pt;")

        # 利好列表
        bull_html = self._render_news_list(key_events, theme.C_UP, "利好")
        self.bull_list.setHtml(bull_html)

        # 利空列表
        bear_html = self._render_news_list(risk_flags, theme.C_DOWN, "利空")
        self.bear_list.setHtml(bear_html)

        # 全部新闻流
        all_news = []
        all_news.extend([(n, "利好", theme.C_UP) for n in key_events])
        all_news.extend([(n, "利空", theme.C_DOWN) for n in risk_flags])
        if not all_news:
            all_html = f'<div style="color:{theme.C_MUTED};text-align:center;padding:20px;">暂无重点新闻</div>'
        else:
            all_html = self._render_news_list(all_news, theme.C_TEXT, "全部", max_items=20)
        self.all_list.setHtml(all_html)

    def _render_news_list(self, items, color, label, max_items=8):
        """渲染新闻列表。items 为纯 dict 时用统一 color/label;
        为 (news, label, color) 元组时每个条目单独着色/标注。"""
        if not items:
            return f'<div style="color:{theme.C_MUTED};text-align:center;padding:16px;">暂无{label}新闻</div>'

        parts = []
        for i, it in enumerate(items[:max_items]):
            if isinstance(it, tuple) and len(it) == 3:
                n, it_label, it_color = it
            else:
                n, it_label, it_color = it, label, color
            title = n.get("title", "") if isinstance(n, dict) else str(n)
            source = n.get("source", "") if isinstance(n, dict) else ""
            url = n.get("url", "") if isinstance(n, dict) else ""
            dt = n.get("datetime", "") if isinstance(n, dict) else ""
            score = n.get("score", 0.0) if isinstance(n, dict) else 0.0

            dt_str = ""
            if dt:
                try:
                    if isinstance(dt, str):
                        dt_str = dt[:16].replace("T", " ")
                    else:
                        dt_str = dt.strftime("%m-%d %H:%M")
                except Exception:
                    dt_str = str(dt)[:16]

            src_str = f" [{source}]" if source else ""
            tag_str = f" <span style='color:{it_color};font-weight:bold;'>[{it_label}]</span>" if isinstance(it, tuple) else ""
            # 价格反应验证徽标: 市场确认/证伪/待观察 (威科夫 effort-vs-result)
            valid = n.get("validation") if isinstance(n, dict) else ""
            badge = ""
            if valid == "confirmed":
                badge = f" <span style='color:{theme.C_UP};'>✓市场确认</span>"
            elif valid == "rejected":
                badge = f" <span style='color:{theme.C_DOWN};font-weight:bold;'>✗市场证伪</span>"
            elif valid == "pending":
                badge = f" <span style='color:{theme.C_MUTED};'>…待验证</span>"
            score_str = f" <span style='color:{it_color};'>(情绪{score:+.2f})</span>"
            link = f'<a href="{url}" style="color:{it_color};text-decoration:none;">{title}</a>' if url else f'<span style="color:{it_color};">{title}</span>'

            parts.append(
                f'<div style="margin:6px 0;padding:8px;background:{theme.css_rgba(it_color, 8)};'
                f'border-left:3px solid {it_color};border-radius:3px;">'
                f'<div style="font-size:{self._fs(0)}pt;">{link}{tag_str}{score_str}{badge}{src_str}</div>'
                f'<div style="color:{theme.C_MUTED};font-size:{self._fs(-2)}pt;margin-top:2px;">{dt_str}</div>'
                f'</div>'
            )
        return "".join(parts)

    def _on_refresh(self):
        """触发重新分析 (由主窗口连接实际刷新逻辑)"""
        if hasattr(self, "refresh_requested"):
            self.refresh_requested.emit(self._symbol)

    def _open_link(self, url: QUrl):
        """打开外部链接，使用 QDesktopServices 避免 ShellExecute 问题"""
        try:
            QDesktopServices.openUrl(url)
        except Exception:
            pass

    def _zoom_news_text(self, delta: int):
        """调节新闻面板字号"""
        new_size = max(7, min(18, self._news_font_size + delta))
        if new_size != self._news_font_size:
            self._news_font_size = new_size
            if self._news_data:
                self.set_data(self._news_data, self._symbol, self._name)
            else:
                self._show_empty()

    def _fs(self, delta=0):
        # 基于面板字号 (_news_font_size) + delta, 限制最小 8pt
        return max(8, self._news_font_size + delta)

    def apply_theme(self):
        """主题切换时刷新样式"""
        self.setStyleSheet(theme.QSS)
        if self._news_data:
            self.set_data(self._news_data, self._symbol, self._name)
        else:
            self._show_empty()
