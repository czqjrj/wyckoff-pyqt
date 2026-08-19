# -*- coding: utf-8 -*-
"""路径与用户数据目录。

打包后 `__file__` 指向临时解压目录, 不能用于持久写入。
用户数据(自选/设置/反馈/导出)统一放用户目录; 资源文件(拼音索引)走 bundle 内路径。
"""
import os
import sys


def _resource_dir():
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return base


def _data_dir():
    # 测试隔离: WYCKOFF_DATA_DIR 环境变量可把全部用户数据重定向到临时目录,
    # 避免测试改写项目根目录下的真实设置/自选股/缓存。
    override = os.environ.get("WYCKOFF_DATA_DIR", "").strip()
    if override:
        base = override
    elif getattr(sys, "frozen", False):
        base = os.path.join(os.path.expanduser("~"), ".wyckoff")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(base, exist_ok=True)
    return base


DATA_DIR = _data_dir()
WATCHLIST_FILE = os.path.join(DATA_DIR, "wyckoff_watchlist.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "wyckoff_settings.json")
FEEDBACK_FILE = os.path.join(DATA_DIR, "wx_feedback.json")
ACCURACY_FILE = os.path.join(DATA_DIR, "wx_accuracy.json")
SIGNAL_ACCURACY_FILE = os.path.join(DATA_DIR, "wx_signal_accuracy.json")
ONLINE_MODEL_FILE = os.path.join(DATA_DIR, "wx_online_model.json")
CANDIDATES_FILE = os.path.join(DATA_DIR, "wyckoff_candidates.json")
BOARD_MAP_FILE = os.path.join(DATA_DIR, "wyckoff_board_map.json")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "wx_portfolio.json")
NOTES_FILE = os.path.join(DATA_DIR, "wx_notes.json")

# SQLite 行情持久缓存 (K线 / 复权因子), 跨会话复用
CACHE_DB = os.path.join(DATA_DIR, "wyckoff_cache.db")

# 拼音索引资源文件: 源码运行时位于包外项目根, 打包后位于 bundle 内
STOCK_NAMES_FILE = os.path.join(_resource_dir(), "wyckoff_stock_names.json")

# 全市场 A 股拼音索引 (用户目录, 首次启动后台下载构建, 周期性刷新)
ALL_STOCKS_FILE = os.path.join(DATA_DIR, "wyckoff_all_stocks.json")

# 帮助文档 (使用说明/技巧): 源码运行时位于项目根 docs/, 打包后位于 bundle 内
HELP_FILE = os.path.join(_resource_dir(), "docs", "help.html")
