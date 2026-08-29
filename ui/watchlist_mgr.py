"""自选股管理器: 从 MainWindow 提取自选股列表相关逻辑。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QListWidgetItem

from wyckoff._log import log_exc
from wyckoff.pinyin import local_search_stock
from wyckoff.storage import load_watchlist, save_watchlist
from wyckoff.utils import normalize_symbol

from . import theme
from .threads import WatchRTThread

if TYPE_CHECKING:
    from ui.main_window import MainWindow


class WatchlistManager:
    """自选股管理器: 管理自选股列表的加载/显示/实时更新/右键菜单。
    
    用法:
        mgr = WatchlistManager(main_window, settings)
        mgr.reload()  # 加载自选股列表
    """
    
    def __init__(self, main_window: MainWindow, settings: dict) -> None:
        self._mw = main_window
        self._settings = settings
        self._watchlist: list[str] = []
        self._watch_names: dict[str, str] = {}
        self._rt_threads: dict[str, WatchRTThread] = {}
        self._last_rt: dict[str, dict] = {}
    
    @property
    def watchlist(self) -> list[str]:
        return list(self._watchlist)
    
    def reload(self) -> None:
        """加载自选股列表并刷新显示。"""
        raw = load_watchlist()
        normalized = []
        for c in raw:
            try:
                normalized.append(normalize_symbol(c))
            except ValueError:
                normalized.append(c)
        self._watchlist = normalized
        self._watch_names = {}
        self._mw.watch_list.clear()
        for code in self._watchlist:
            name = ""
            try:
                bare = code[2:] if len(code) == 8 and code[:2] in ("sh", "sz", "bj") else code
                r = local_search_stock(bare, limit=1)
                if r:
                    name = r[0].get("name", "")
            except Exception as e:
                log_exc("_reload_watchlist 解析股票名失败", e)
                name = ""
            self._watch_names[code] = name
            self._add_item(code, name)
        if self._mw._current_code:
            self.select(self._mw._current_code)
        self.refresh_rt()
    
    def _add_item(self, code, name):
        """添加一个自选股列表项。"""
        from .watch_card import ROLE_NAME, ROLE_PCT, ROLE_PRICE, ROLE_TAG, ROLE_TAG_COLOR
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, code)
        item.setData(ROLE_NAME, name or "")
        item.setData(ROLE_TAG, "")
        item.setData(ROLE_TAG_COLOR, theme.C_MUTED)
        item.setData(ROLE_PRICE, None)
        item.setData(ROLE_PCT, None)
        item.setSizeHint(QSize(120, 40))
        # Tooltip: 显示代码 + 名称
        item.setToolTip(f"{code} {name}" if name else code)
        self._mw.watch_list.addItem(item)
        return item
    
    def select(self, code):
        """选中指定代码的自选股。"""
        for i in range(self._mw.watch_list.count()):
            it = self._mw.watch_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == code:
                self._mw.watch_list.setCurrentItem(it)
                return
    
    def add(self, code):
        """添加股票到自选股。"""
        try:
            full = normalize_symbol(code)
        except ValueError:
            full = code
        if full not in self._watchlist:
            self._watchlist.append(full)
            save_watchlist(self._watchlist)
            self.reload()
            self.select(full)
        else:
            self.select(full)
    
    def remove(self, code):
        """从自选股删除股票。"""
        if code in self._watchlist:
            self._watchlist.remove(code)
            save_watchlist(self._watchlist)
            self.reload()
    
    def move(self, code, direction):
        """移动自选股顺序 (direction: -1 上移, 1 下移)。"""
        i = self._watchlist.index(code) if code in self._watchlist else -1
        if i < 0:
            return
        j = i + direction
        if 0 <= j < len(self._watchlist):
            self._watchlist[i], self._watchlist[j] = self._watchlist[j], self._watchlist[i]
            save_watchlist(self._watchlist)
            self.reload()
            self.select(code)
    
    def refresh_rt(self):
        """后台拉取自选股实时行情 + 阶段分类。"""
        codes = list(self._watchlist)
        if not codes:
            return
        th = WatchRTThread(codes, self._mw)
        th.result.connect(self._on_rt)
        self._rt_threads[th] = th
        th.start()
    
    def _on_rt(self, rt, phases):
        """实时行情回调: 更新卡片数据。"""
        from .watch_card import ROLE_NAME, ROLE_PCT, ROLE_PRICE, ROLE_TAG, ROLE_TAG_COLOR, tag_for
        self._last_rt = rt
        for i in range(self._mw.watch_list.count()):
            item = self._mw.watch_list.item(i)
            if item is None:
                continue
            code = item.data(Qt.ItemDataRole.UserRole)
            try:
                sym = normalize_symbol(code)
            except ValueError:
                sym = ""
            info = rt.get(sym) or rt.get(code) if sym else rt.get(code)
            if info:
                if info.get("name"):
                    item.setData(ROLE_NAME, info["name"])
                    self._watch_names[code] = info["name"]
                    item.setToolTip(f"{code} {info['name']}")
                item.setData(ROLE_PRICE, info.get("price"))
                item.setData(ROLE_PCT, info.get("pct"))
            p = phases.get(code)
            if p:
                tag, color = tag_for(p.get("base"))
                conf = p.get("conf")
                if conf == "high":
                    tag = "✓" + tag
                elif conf == "caution":
                    tag = "!" + tag
                item.setData(ROLE_TAG, tag)
                item.setData(ROLE_TAG_COLOR, color)
        self._mw.watch_list.viewport().update()
        for th in list(self._rt_threads):
            self._rt_threads.pop(th, None)
