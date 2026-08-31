"""测试夹具: 隔离全部用户数据 (设置/自选/缓存/拼音索引) 与内存缓存,
避免污染项目根目录的正式数据文件。

此前只隔离了 SQLite db, MainWindow 测试会在关闭时通过 _remember_panel_state
把测试状态写进真实的 wyckoff_settings.json, 还会改写自选股/拼音索引。
这里在导入任何 wyckoff 模块前把整个数据目录重定向到临时目录。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 必须在导入 wyckoff.* 之前设置: paths.py 在 import 时读取该环境变量
_TMP_DATA = tempfile.mkdtemp(prefix="wyckoff_test_data_")
os.environ["WYCKOFF_DATA_DIR"] = _TMP_DATA
# 语境特征 (context.enrich) 的指数拉取在测试环境一律离线降级, 防止慢网络拖垮全量回归
os.environ["WYCKOFF_NO_NET"] = "1"

import pytest

import wyckoff.datasource as datasource
import wyckoff.sqldb as sqldb


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path):
    """每个测试用独立的临时 db 文件, 且清空内存缓存。"""
    sqldb.set_db_path(str(tmp_path / "test_cache.db"))
    datasource._KLINE_CACHE.clear()
    datasource._FACTOR_CACHE.clear()
    datasource._SOURCE_LOG.clear()
    yield
    datasource._KLINE_CACHE.clear()
    datasource._FACTOR_CACHE.clear()
    datasource._SOURCE_LOG.clear()


@pytest.fixture(autouse=True)
def no_pinyin_network(monkeypatch):
    """禁用启动时的全市场索引下载/自选股名称抓取, 并重定向拼音索引文件。

    MainWindow.__init__ 会后台启动 ensure_full_market_index 与
    load_watchlist_stocks; 测试环境不应触发网络下载, 也不应改写项目根目录下
    的 wyckoff_stock_names.json (bundle 资源文件)。
    """
    try:
        import ui.main_window as mw
        import wyckoff.pinyin as pinyin
    except Exception:
        return
    monkeypatch.setattr(mw, "ensure_full_market_index", lambda *a, **k: False)
    monkeypatch.setattr(mw, "load_watchlist_stocks", lambda *a, **k: None)
    monkeypatch.setattr(pinyin, "STOCK_NAMES_FILE",
                        os.path.join(_TMP_DATA, "wyckoff_stock_names.json"))


def pytest_sessionfinish(session, exitstatus):
    """清理会话级临时数据目录。"""
    shutil.rmtree(_TMP_DATA, ignore_errors=True)
