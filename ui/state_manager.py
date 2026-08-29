"""状态管理器: 统一管理设置持久化、窗口状态、面板宽度等。"""
from __future__ import annotations

from typing import Any
from datetime import datetime as _dt

from wyckoff.config import SCALE_OPTIONS, PERIOD_OPTIONS
from wyckoff.settings_keys import S, DEFAULTS
from wyckoff.storage import load_settings, save_settings, load_watchlist, save_watchlist

from . import theme


class StateManager:
    """状态管理器: 统一管理设置/窗口状态/面板宽度/最近分析记录等持久化。"""

    def __init__(self, main_window, load_settings_fn=None, save_settings_fn=None,
                 load_watchlist_fn=None, save_watchlist_fn=None) -> None:
        self._mw = main_window
        self._load_settings_fn = load_settings_fn or load_settings
        self._save_settings_fn = save_settings_fn or save_settings
        self._load_watchlist_fn = load_watchlist_fn or load_watchlist
        self._save_watchlist_fn = save_watchlist_fn or save_watchlist
        self._settings: dict = {}
        self._watchlist: list = []
        self._panel_widths: dict = {}
        self._boot_done = False
        self._full_market_index_done = False

    # ── 设置加载/保存 ──
    def load_settings(self) -> dict:
        """加载设置并应用默认值。"""
        self._settings = self._load_settings_fn()
        # 确保所有默认键存在
        for k, v in DEFAULTS.items():
            self._settings.setdefault(k, v)
        return self._settings

    def save_settings(self):
        """保存设置到磁盘。"""
        try:
            self._save_settings_fn(self._settings)
        except Exception:
            pass

    def get_setting(self, key: str, default=None):
        return self._settings.get(key, default)

    def set_setting(self, key: str, value):
        self._settings[key] = value

    # ── 自选股 ──
    def load_watchlist(self) -> list:
        self._watchlist = self._load_watchlist_fn()
        return self._watchlist

    def save_watchlist(self):
        try:
            self._save_watchlist_fn(self._watchlist)
        except Exception:
            pass

    def add_watch(self, code: str, name: str = ""):
        if not any(w[0] == code for w in self._watchlist):
            self._watchlist.append((code, name))
            self.save_watchlist()

    def remove_watch(self, code: str):
        self._watchlist = [w for w in self._watchlist if w[0] != code]
        self.save_watchlist()

    # ── 面板宽度记忆 ──
    def persist_panel_widths(self):
        """记忆左右面板宽度 (dock 分割位置)。"""
        try:
            # 这里需要根据实际 dock 布局获取宽度
            pass
        except Exception:
            pass

    def restore_panel_widths(self):
        """恢复面板宽度。"""
        pass

    # ── 最近分析记录 ──
    def remember_last_analyzed(self, code: str, scale_key: str, period_key: str):
        """记录本次分析的股票与周期/时间段。"""
        if not code:
            return
        self._settings["last_analyzed_code"] = code
        self._settings["last_analyzed_scale"] = scale_key
        self._settings["last_analyzed_period"] = period_key
        self.save_settings()

    def get_last_analyzed(self) -> tuple[str, str, str]:
        """获取上次分析的 (code, scale_key, period_key)。"""
        return (
            self._settings.get("last_analyzed_code", ""),
            self._settings.get("last_analyzed_scale", "日线"),
            self._settings.get("last_analyzed_period", "最近 700 根"),
        )

    # ── 启动/关闭状态 ──
    @property
    def boot_done(self) -> bool:
        return self._boot_done

    @boot_done.setter
    def boot_done(self, v: bool):
        self._boot_done = v

    @property
    def full_market_index_done(self) -> bool:
        return self._full_market_index_done

    @full_market_index_done.setter
    def full_market_index_done(self, v: bool):
        self._full_market_index_done = v

    # ── 主题切换同步 ──
    def toggle_theme(self):
        """切换主题并持久化。"""
        current = self._settings.get(S.UI.THEME, "light")
        new_theme = "dark" if current == "light" else "light"
        self._settings[S.UI.THEME] = new_theme
        self.save_settings()
        return new_theme

    def apply_theme(self, theme_name: str):
        """应用主题到设置。"""
        self._settings[S.UI.THEME] = theme_name
        self.save_settings()

    # ── 停靠窗口状态 ──
    def save_dock_state(self, state_bytes: bytes):
        """保存 dock 面板状态 (base64 编码)。"""
        import base64
        self._settings[S.UI.DOCK_STATE] = base64.b64encode(state_bytes).decode()
        self.save_settings()

    def load_dock_state(self) -> bytes | None:
        """加载 dock 面板状态。"""
        import base64
        s = self._settings.get(S.UI.DOCK_STATE)
        if s:
            try:
                return base64.b64decode(s)
            except Exception:
                pass
        return None

    # ── 字体/字号同步 ──
    def sync_chart_font(self) -> int:
        """获取图表字体大小 (来自设置)。"""
        return int(self._settings.get(S.UI.CHART_FONT_SIZE, 11))

    def sync_text_font(self) -> int:
        """获取正文字体大小。"""
        return int(self._settings.get(S.UI.TEXT_FONT_SIZE, 10))

    def set_chart_font(self, size: int):
        self._settings[S.UI.CHART_FONT_SIZE] = size
        self.save_settings()

    def set_text_font(self, size: int):
        self._settings[S.UI.TEXT_FONT_SIZE] = size
        self.save_settings()

    # ── 视图记忆 ──
    def remember_kline_view(self, code: str, scale: int, view_range: tuple):
        """记忆 K线图视图范围。"""
        self._settings.setdefault("_view_mem", {})
        self._settings["_view_mem"][(code, scale)] = list(view_range)
        self.save_settings()

    def get_kline_view(self, code: str, scale: int) -> tuple | None:
        v = self._settings.get("_view_mem", {}).get((code, scale))
        return tuple(v) if v else None

    # ── 默认视野同步 ──
    def sync_chart_defaults(self, ind_widget, mkt_widget):
        """把设置里的默认柱数同步到图表控件。"""
        ind_widget.default_bars = int(self._settings.get(S.Chart.IND_DEFAULT_BARS, 250) or 0)
        mkt_widget.default_bars = int(self._settings.get(S.Chart.MKT_DEFAULT_BARS, 120) or 0)

    # ── P&F 格值同步 ──
    def sync_pnf_box_scale(self, box_mode: str, atr_factor: float):
        self._settings[S.Chart.PNF_BOX_MODE] = box_mode
        self._settings[S.Chart.PNF_ATR_FACTOR] = atr_factor
        self.save_settings()

    def get_pnf_box_settings(self) -> tuple[str, float]:
        return (
            self._settings.get(S.Chart.PNF_BOX_MODE, "pct"),
            float(self._settings.get(S.Chart.PNF_ATR_FACTOR, 0.5)),
        )

    # ── 视图记忆字典 ──
    def get_view_mem(self) -> dict:
        return self._settings.get("_view_mem", {})

    def set_view_mem(self, mem: dict):
        self._settings["_view_mem"] = mem
        self.save_settings()

    @property
    def settings(self) -> dict:
        return self._settings

    @property
    def watchlist(self) -> list:
        return self._watchlist

    @property
    def panel_widths(self) -> dict:
        return self._panel_widths