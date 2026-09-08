"""大盘仪表盘数据层测试。

测试环境设 WYCKOFF_NO_NET=1 (见 conftest), 所有网络函数应静默降级,
因此多数断言聚焦在「离线时返回结构正确/不抛异常」上。
"""
import pytest


# ── 结构 ──

def test_dash_indices_structure():
    from wyckoff.market_dashboard import DASH_INDICES
    assert len(DASH_INDICES) >= 6
    for idx in DASH_INDICES:
        assert "code" in idx and "name" in idx
        assert idx["code"].startswith(("sh", "sz"))
        assert len(idx["code"]) == 8


def test_build_dashboard_data_offline():
    """离线 (WYCKOFF_NO_NET=1) 时仍返回完整 dict 结构, 不抛异常。"""
    from wyckoff.market_dashboard import build_dashboard_data, clear_cache
    clear_cache()
    data = build_dashboard_data()
    assert isinstance(data, dict)
    for key in ("indices", "tech", "breadth", "sectors",
                "north", "zt_pool", "market_env"):
        assert key in data
    # 离线时 indices/tech 可能为空 dict, 绝不抛异常
    assert isinstance(data["indices"], dict)
    assert isinstance(data["tech"], dict)


def test_fetch_breadth_offline_none_or_dict():
    """离线时市场广度为 None 或合法结构。"""
    from wyckoff.market_dashboard import fetch_market_breadth, clear_cache
    clear_cache()
    val = fetch_market_breadth()
    assert val is None or isinstance(val, dict)
    if isinstance(val, dict):
        for key in ("up", "down", "flat", "total"):
            assert key in val


def test_fetch_sector_ranking_offline_list():
    from wyckoff.market_dashboard import fetch_sector_ranking, clear_cache
    clear_cache()
    val = fetch_sector_ranking()
    assert isinstance(val, list)


def test_fetch_north_offline_list():
    from wyckoff.market_dashboard import fetch_north_flow, clear_cache
    clear_cache()
    val = fetch_north_flow()
    assert isinstance(val, list)


def test_fetch_zt_pool_offline_list():
    from wyckoff.market_dashboard import fetch_limit_up_pool, clear_cache
    clear_cache()
    val = fetch_limit_up_pool()
    assert isinstance(val, list) or val is None


def test_clear_cache():
    from wyckoff.market_dashboard import fetch_market_breadth, clear_cache
    clear_cache()
    fetch_market_breadth()
    assert clear_cache() is None


# ── 图表数据 ──

def test_build_dashboard_data_has_chart_keys():
    """聚合结果包含 3 组图表数据键。"""
    from wyckoff.market_dashboard import build_dashboard_data, clear_cache
    clear_cache()
    data = build_dashboard_data()
    for key in ("sse_chart", "index_compare", "sector_flow"):
        assert key in data


def test_sse_chart_structure():
    """上证K线图: None 或合法 dict (含 MA20/MA50 序列)。"""
    from wyckoff.market_dashboard import build_sse_chart, clear_cache
    clear_cache()
    c = build_sse_chart()
    if c is None:
        return
    for key in ("days", "x", "open", "high", "low", "close",
                "volume", "ma20", "ma50", "last_close"):
        assert key in c
    assert len(c["open"]) == len(c["close"]) == len(c["x"]) > 0
    assert "env" in c and "tone" in c


def test_index_compare_structure():
    from wyckoff.market_dashboard import build_index_compare, clear_cache
    clear_cache()
    c = build_index_compare()
    if c is None:
        return
    assert c.get("days")
    for s in c["series"]:
        assert "name" in s and len(s["data"]) > 30
        assert abs(s["data"][0] - 100.0) < 0.01  # 基期 100


def test_sector_flow_chart_list():
    from wyckoff.market_dashboard import (
        build_sector_flow_chart, clear_cache)
    clear_cache()
    rows = build_sector_flow_chart()
    assert isinstance(rows, list)
    for r in rows:
        assert {"name", "flow", "pct", "tone"} <= set(r)


# ── UI 控件 ──

def test_dashboard_widget_builds_without_data():
    """仪表盘控件可无数据构建, set_data({}) 不抛异常。"""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.dashboard_widget import DashboardWidget
    w = DashboardWidget(font_size=11)
    w.set_data({})
    # 卡片映射已构建
    assert w._idx_cards
    w.set_placeholder("test")
    w.deleteLater()


def test_dashboard_widget_set_empty_and_theme():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.dashboard_widget import DashboardWidget
    from wyckoff.market_dashboard import build_dashboard_data, clear_cache
    clear_cache()
    data = build_dashboard_data()
    w = DashboardWidget(font_size=11)
    w.set_data(data)
    w.apply_theme()
    w.deleteLater()


def test_chart_widgets_render_offline():
    """三个图表控件可离线构建+渲染 (数据为 None / {} / [] 时不抛异常)。"""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.dash_charts import SseKlineChart, IndexCompareChart, SectorFlowChart
    from wyckoff.market_dashboard import (
        build_sse_chart, build_index_compare, build_sector_flow_chart, clear_cache)
    clear_cache()
    c1 = build_sse_chart()
    c2 = build_index_compare()
    c3 = build_sector_flow_chart()
    w1, w2, w3 = SseKlineChart(), IndexCompareChart(), SectorFlowChart()
    w1.set_data(c1)
    w2.set_data(c2)
    w3.set_data(c3)
    # 空数据降级路径
    w1.set_data(None)
    w2.set_data({})
    w3.set_data([])
    w1.deleteLater()
    w2.deleteLater()
    w3.deleteLater()


def test_dashboard_widget_renders_charts_with_data():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.dashboard_widget import DashboardWidget
    # 伪造真实图表数据, 验证控件分支渲染不抛异常
    fake = {
        "sse_chart": {
            "days": ["2026-01-01", "2026-01-02"],
            "x": [0, 1], "open": [100.0, 101.0],
            "high": [102.0, 103.0], "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1e8, 2e8],
            "ma20": [None, 100.5], "ma50": [None, 99.8],
            "last_close": 102.0, "env": "牛市", "tone": "bullish",
        },
        "index_compare": {
            "days": ["2026-01-01", "2026-01-02"],
            "series": [
                {"name": "上证指数", "data": [100.0, 101.0]},
                {"name": "创业板指", "data": [100.0, 99.0]},
            ],
        },
        "sector_flow": [
            {"name": "半导体", "flow": 12.3, "pct": 1.2, "tone": "bullish"},
            {"name": "银行", "flow": -5.0, "pct": -0.4, "tone": "bearish"},
        ],
    }
    w = DashboardWidget(font_size=11)
    w.set_data(fake)
    w.apply_theme()
    w.deleteLater()