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


def test_breadth_falls_back_to_legu(monkeypatch):
    """东财源失败时自动回退乐咕源。"""
    from wyckoff import market_dashboard as md
    monkeypatch.setattr(md, "_breadth_from_em", lambda: None)
    monkeypatch.setattr(
        md, "_breadth_from_legu",
        lambda: {"up": 3257, "down": 1859, "flat": 91,
                 "limit_up": 74, "limit_down": 1, "total": 5207, "src": "legu"})
    md.clear_cache()
    val = md.fetch_market_breadth()
    assert val is not None and val["up"] == 3257 and val["src"] == "legu"


def test_fetch_sector_ranking_offline_list():
    from wyckoff.market_dashboard import fetch_sector_ranking, clear_cache
    clear_cache()
    val = fetch_sector_ranking()
    assert isinstance(val, list)
    for s in val:
        assert "amount_yi" in s


def test_build_sector_heatmap_sorted(monkeypatch):
    """热力图按成交额降序取 top_n，成交额缺失时按强度兜底。"""
    from wyckoff import market_dashboard as md
    fake = [
        {"name": f"B{i}", "pct": i - 5, "tone": "neutral",
         "amount_yi": i, "score": i}
        for i in range(1, 40)
    ]
    monkeypatch.setattr(md, "fetch_sector_ranking", lambda: fake)
    md.clear_cache()
    rows = md.build_sector_heatmap(top_n=24)
    assert len(rows) == 24
    assert rows[0]["amount_yi"] == 39 and rows[-1]["amount_yi"] == 16
    assert rows[0]["name"] == "B39"
    for r in rows:
        assert set(r) >= {"name", "pct", "tone", "amount_yi"}

    # amount 全缺 (东财源) → 按 score 兜底排序, 仍返回合法列表
    fake2 = [{"name": f"A{i}", "pct": 1.0, "tone": "neutral",
              "amount_yi": 0, "score": i} for i in range(1, 30)]
    monkeypatch.setattr(md, "fetch_sector_ranking", lambda: fake2)
    md.clear_cache()
    rows2 = md.build_sector_heatmap(top_n=10)
    assert len(rows2) == 10 and rows2[0]["name"] == "A29"


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
    """聚合结果包含全部图表数据键与主力资金键。"""
    from wyckoff.market_dashboard import build_dashboard_data, clear_cache
    clear_cache()
    data = build_dashboard_data()
    for key in ("sse_chart", "index_compare", "sector_flow", "fund_flow",
                "sector_heatmap"):
        assert key in data


def test_fetch_market_fund_flow_offline():
    """离线时主力资金为 None 或合法结构。"""
    from wyckoff.market_dashboard import fetch_market_fund_flow, clear_cache
    clear_cache()
    val = fetch_market_fund_flow()
    if val is None:
        return
    assert "items" in val and "total_yi" in val
    for it in val["items"]:
        assert "name" in it and "net_yi" in it


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
    """全部图表控件可离线构建+渲染 (数据为 None / {} / [] 时不抛异常)。"""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui.dash_charts import (IndexCompareChart, SectorFlowChart,
                                SectorHeatmap, SseKlineChart)
    from wyckoff.market_dashboard import (
        build_index_compare, build_sector_flow_chart, build_sector_heatmap,
        build_sse_chart, clear_cache)
    clear_cache()
    c1 = build_sse_chart()
    c2 = build_index_compare()
    c3 = build_sector_flow_chart()
    c4 = build_sector_heatmap()
    w1, w2 = SseKlineChart(), IndexCompareChart()
    w3, w4 = SectorFlowChart(), SectorHeatmap()
    w1.set_data(c1)
    w2.set_data(c2)
    w3.set_data(c3)
    w4.set_data(c4)
    # 空数据降级路径
    w1.set_data(None)
    w2.set_data({})
    w3.set_data([])
    w4.set_data([])
    # 真实结构渲染分支
    w4.set_data([{"name": "半导体", "pct": 2.1, "tone": "bullish",
                  "amount_yi": 280.5}])
    for w in (w1, w2, w3, w4):
        w.show()
        w.repaint()
        w.deleteLater()


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
        "sector_heatmap": [
            {"name": "半导体", "pct": 2.1, "tone": "bullish", "amount_yi": 280.5},
            {"name": "油气开采", "pct": 4.2, "tone": "bullish", "amount_yi": 86.9},
            {"name": "农化制品", "pct": 4.7, "tone": "bullish", "amount_yi": 281.4},
        ],
        "fund_flow": {
            "items": [
                {"name": "上证指数", "net_yi": 27.2, "net_pct": 30},
                {"name": "深证成指", "net_yi": -109.2, "net_pct": -104},
            ],
            "total_yi": -82.0,
        },
    }
    w = DashboardWidget(font_size=11)
    w.set_data(fake)
    w.apply_theme()
    w.deleteLater()