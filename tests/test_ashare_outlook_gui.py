"""独立 A股 GUI 离线构建测试 (不触网)。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")


def _fake_result():
    return {
        "as_of": "2026-11-06",
        "generated_at": "2026-11-06 21:00:00",
        "primary": "sh000001",
        "indices": {
            "sh000001": {
                "name": "上证指数",
                "close": 3649.18,
                "day_pct": -1.1,
                "score": 23.6,
                "state": "谨慎",
                "phase": "区间整理",
                "trend_score": 0,
                "momentum_score": 30,
                "vol_score": 23,
                "drawdown_score": 71,
                "ret20": -4.32,
                "dd250": -14.32,
                "atr_pct": 1.63,
            }
        },
        "market": {
            "env": "熊市环境",
            "breadth": {
                "up": 716,
                "down": 4431,
                "flat": 73,
                "up_ratio": 0.137,
                "limit_up": 39,
                "limit_down": 22,
            },
            "amount": {"total_yi": 16578, "vol_pct": 18},
            "fund": {"total_yi": -686},
            "emotion": {"phase": "退潮"},
        },
        "analogs": {
            "ok": True,
            "n_analogs": 60,
            "avg_distance": 0.32,
            "horizons": {
                "5": {"n": 60, "up_prob": 0.62, "median": 0.35, "p10": -1.42, "p90": 2.37},
                "20": {"n": 60, "up_prob": 0.5, "median": -0.01, "p10": -4.34, "p90": 5.15},
                "60": {"n": 60, "up_prob": 0.6, "median": 4.7, "p10": -5.08, "p90": 17.89},
            },
        },
        "backtest": {"ok": False, "reason": "略"},
        "primary_levels": {
            "close": 3649.18,
            "ma20": 3693.3,
            "ma50": 3821.37,
            "ma200": 3984.34,
            "tr_bottom": 3612.84,
            "tr_top": 3941.28,
            "near_up": 3795.14,
            "near_down": 3503.21,
            "support_levels": [3612.84, 3585.1, 3574.27],
            "resistance_levels": [3693.3, 3795.14, 3941.28],
            "up_targets": [4214.98, 4269.72, 4543.42],
            "up_prob": [0.61, 0.6, 0.5],
            "down_targets": [3339.14, 3229.66, 2955.96],
            "down_prob": [0.84, 0.77, 0.61],
            "atr_pct": 1.63,
        },
        "scenarios": [
            {"name": "基准: 区间震荡", "trigger": "守住 3613", "path": "3613-3693"},
            {"name": "风险: 破位", "trigger": "跌破 3613", "path": "3503-3339"},
            {"name": "反转: 右侧", "trigger": "站上 3941", "path": "4215-4270"},
        ],
        "etf": {
            "ok": False,
            "label": "ETF资金缺失",
            "verdict": "资金数据缺失",
            "score": None,
            "net_y1": None,
            "net_y20": None,
            "vs_market": "ETF资金数据缺失，无法确认",
            "warnings": [],
        },
        "liquidity": {
            "ok": True,
            "label": "中性",
            "score": 49.9,
            "m1_m2": -4.0,
            "credit_yoy": -20.9,
            "shibor3m": 1.4,
            "bond10": 1.59,
            "as_of": "2026-09",
            "rate_as_of": "2026-11-06",
        },
        "sectors": {
            "n": 90,
            "top": [
                {
                    "name": "银行",
                    "pct": 0.99,
                    "flow20_yi": 27.39,
                    "strength": 100,
                    "tone": "bullish",
                    "amount_yi": 50,
                },
                {
                    "name": "电力",
                    "pct": 0.76,
                    "flow20_yi": 24.77,
                    "strength": 95,
                    "tone": "bullish",
                    "amount_yi": 40,
                },
            ],
            "bottom": [
                {
                    "name": "医疗服务",
                    "pct": -3.38,
                    "flow20_yi": -30.14,
                    "strength": 5,
                    "tone": "bearish",
                    "amount_yi": 30,
                }
            ],
            "all": [
                {
                    "name": "银行",
                    "pct": 0.99,
                    "flow20_yi": 27.39,
                    "strength": 100,
                    "tone": "bullish",
                    "amount_yi": 50,
                },
                {
                    "name": "电力",
                    "pct": 0.76,
                    "flow20_yi": 24.77,
                    "strength": 95,
                    "tone": "bullish",
                    "amount_yi": 40,
                },
                {
                    "name": "医疗服务",
                    "pct": -3.38,
                    "flow20_yi": -30.14,
                    "strength": 5,
                    "tone": "bearish",
                    "amount_yi": 30,
                },
            ],
            "style_gap": 21.22,
        },
        "risk_budget": {"low": 0, "high": 15, "note": "机械降档"},
        "changes": None,
        "backtest_strategy": {
            "ok": True,
            "start": "2021-11-24",
            "end": "2026-11-06",
            "cost_bps": 5.0,
            "strategy": {
                "total_return": -6.03,
                "cagr": -1.3,
                "max_drawdown": -16.34,
                "sharpe": -0.1,
                "avg_exposure": 53.2,
            },
            "ma200": {
                "total_return": 6.9,
                "cagr": 1.41,
                "max_drawdown": -17.09,
                "sharpe": 0.18,
                "avg_exposure": 50.3,
            },
            "buy_hold": {
                "total_return": 1.57,
                "cagr": 0.33,
                "max_drawdown": -26.59,
                "sharpe": 0.1,
                "avg_exposure": 99.9,
            },
            "equity": {
                "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
                "strategy": [1.0, 1.01, 1.02],
                "ma200": [1.0, 1.0, 1.01],
                "buy_hold": [1.0, 0.99, 1.0],
            },
        },
        "score_history": {
            "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "score": [50.0, 48.0, 47.0],
            "close": [3000.0, 2990.0, 2980.0],
        },
        "data_quality": {
            "indices": {
                "ok": True,
                "stale": False,
                "as_of": "2026-11-06",
                "missing": [],
                "error": "",
            },
            "sectors": {"ok": True, "stale": False, "as_of": "2026-11-06", "error": ""},
            "etf": {"ok": False, "stale": False, "as_of": "2026-11-06", "error": "缺失"},
            "liquidity": {"ok": True, "stale": False, "as_of": "2026-09", "error": ""},
            "market": {
                "env": True,
                "breadth": True,
                "amount": True,
                "fund": True,
                "emotion": True,
                "resonance": True,
            },
        },
        "disclaimer": "不构成投资建议",
    }


def test_window_builds_and_renders_offline():
    from PyQt6.QtWidgets import QApplication

    from ui.ashare_outlook_window import AshareOutlookWindow

    app = QApplication.instance() or QApplication([])
    w = AshareOutlookWindow(auto_load=False)
    try:
        assert w.tabs.count() == 7
        w._on_outlook(_fake_result())
        assert "大白话结论" in w.report_view.toPlainText()
        assert "谨慎" in w.concl_state.text()
        assert w.concl_one.text()
        assert "仓位参考" in w.concl_action.text()
        assert "关键位" in w.concl_levels.text()
        assert "预警" in w.concl_alert.text()
        assert "上证指数" in w.lbl_head.text()
        assert "3649.18" in w.lbl_head.text()
        assert "数据" in w.lbl_quality.text()
        assert "数据质量" in w.report_view.toPlainText()
        assert w.sector_table.rowCount() == 3
        assert w.stock_sector.count() >= 3
        assert w.lbl_alerts.text() == "预警: 无"

        # 预警标签
        w._update_alerts(
            [
                {
                    "level": "warn",
                    "title": "P&F 破位",
                    "detail": "收盘低于 P&F 下沿。",
                }
            ]
        )
        assert w.lbl_alerts.text().startswith("预警:")
        assert "P&F 破位" in w.lbl_alerts.text()
        assert "P&F 破位" in w.lbl_alerts.toolTip()

        # 成分股表（不触网，直接喂数据）
        w._cons_name = "银行"
        w._on_constituents(
            {
                "name": "银行",
                "rows": [
                    {
                        "code": "600000",
                        "name": "浦发银行",
                        "pct": 1.0,
                        "amount_yi": 2.0,
                        "turnover": 0.5,
                        "pe": 5.0,
                        "source": "sina",
                    },
                    {
                        "code": "000001",
                        "name": "平安银行",
                        "pct": -0.5,
                        "amount_yi": 1.0,
                        "turnover": 0.6,
                        "pe": 5.1,
                        "source": "sina",
                    },
                ],
            }
        )
        assert w.cons_table.rowCount() == 2
        assert "来源新浪" in w.lbl_cons.text()

        # 双击板块 → 切到个股页
        w._sector_to_stock(0, 0)
        assert w.tabs.currentWidget() is w.stock_tab
        idx = w.stock_sector.findData("银行")
        if idx >= 0:
            w.stock_sector.setCurrentIndex(idx)

        w._on_stock(
            {
                "code": "600519",
                "text": "个股测试报告",
                "confirm": False,
                "summary": [
                    {"label": "综合", "value": "偏多 +10", "tone": "bullish"},
                    {"label": "阶段", "value": "吸筹", "tone": "neutral"},
                ],
                "precomputed": {"phase": "吸筹", "fusion": {"score": 10, "confidence": "中"}},
            }
        )
        assert w.stock_summary.rowCount() == 2
        assert "个股测试报告" in w.stock_view.toPlainText()
        assert "快速模式" in w.stock_view.toPlainText()
        assert "大盘" in w.stock_banner.text()
        assert w.stock_check.rowCount() == 4
        assert "逆风" in w.stock_check.item(3, 1).text()
        assert "个股日志" in w.stock_journal_info.text()

        # 自选/批量
        from wyckoff.ashare_watchlist import clear_watchlist

        clear_watchlist()
        w._load_watchlist()
        w.watch_codes.setText("600519, 000001")
        w._add_watchlist()
        assert w.watch_table.rowCount() == 2
        assert w.watch_table.item(0, 0).text() == "600519"

        w._on_batch_row(
            {
                "code": "600519",
                "name": "贵州茅台",
                "phase": "吸筹",
                "score": 10,
                "direction": "买入",
                "stop": 1600,
                "target1": 1800,
                "record_status": "未记录",
            }
        )
        assert w.batch_table.rowCount() == 1
        assert w.batch_table.item(0, 1).text() == "贵州茅台"

        w._batch_data["600519"] = {
            "symbol": "600519",
            "name": "贵州茅台",
            "as_of": "2026-11-08",
            "close": 1700,
            "precomputed": {"phase": "吸筹", "fusion": {"score": 10}},
            "summary": [],
        }
        w._record_batch_code("600519")
        assert w.batch_table.item(0, 7).text() == "已记录"
        clear_watchlist()

        # 自动识别行业：未手动选择时自动填入
        w._sector_manual = False
        w._auto_select_sector({"industry": "白酒", "candidates": ["白酒"], "source": "cninfo"})
        assert w.stock_sector.currentData() == "白酒"

        # 手动选择后，不覆盖
        idx_bank = w.stock_sector.findData("银行")
        if idx_bank >= 0:
            w.stock_sector.setCurrentIndex(idx_bank)
            w._sector_manual = True
            w._auto_select_sector({"industry": "白酒", "candidates": ["白酒"], "source": "cninfo"})
            assert w.stock_sector.currentData() == "银行"
    finally:
        w.close()
        app.processEvents()


def test_outlook_worker_progressive(monkeypatch):
    from PyQt6.QtWidgets import QApplication

    import ui.ashare_outlook_window as win
    import wyckoff.ashare_outlook as ao

    app = QApplication.instance() or QApplication([])
    assert app is not None

    calls = {"build": [], "extras": None}

    def fake_build(primary, **opts):
        calls["build"].append(dict(opts))
        return {
            "primary": primary,
            "indices": {primary: {"score": 20, "atr_pct": 1.5}},
            "market": {},
            "data_quality": {},
            "alerts": [],
            "risk_budget": {},
        }

    def fake_extras(result, **opts):
        calls["extras"] = dict(opts)
        result["sectors"] = {"n": 1}
        return result

    monkeypatch.setattr(win, "build_outlook", fake_build)
    monkeypatch.setattr(ao, "load_extras", fake_extras)

    worker = win._OutlookWorker(
        "sh000001",
        {
            "datalen": 100,
            "include_sectors": True,
            "include_etf": True,
            "include_liquidity": False,
            "include_valuation": False,
            "progressive": True,
        },
    )
    partials, dones, stages = [], [], []
    worker.partial.connect(partials.append)
    worker.done.connect(dones.append)
    worker.stage.connect(stages.append)
    worker.run()

    # 第一阶段只拿指数核心
    assert calls["build"][0]["include_sectors"] is False
    assert calls["build"][0]["include_etf"] is False
    assert calls["build"][0]["include_liquidity"] is False
    # 第二阶段补 extras
    assert calls["extras"] == {
        "include_sectors": True,
        "include_etf": True,
        "include_liquidity": False,
        "include_valuation": False,
    }
    assert partials and dones
    assert dones[0]["sectors"] == {"n": 1}
    assert stages and "核心已出" in stages[0]

    # 未勾选 extras 时走单阶段，不调用 load_extras
    calls["extras"] = None
    worker2 = win._OutlookWorker(
        "sh000001",
        {
            "datalen": 100,
            "include_sectors": False,
            "include_etf": False,
            "include_liquidity": False,
            "include_valuation": False,
            "progressive": True,
        },
    )
    worker2.run()
    assert calls["extras"] is None


def test_chart_kline_analogs_heatmap():
    from PyQt6.QtWidgets import QApplication

    from ui.ashare_outlook_window import AshareOutlookWindow

    app = QApplication.instance() or QApplication([])
    w = AshareOutlookWindow(auto_load=False)
    try:
        r = _fake_result()
        n = 40
        r["price_history"] = {
            "dates": [f"2026-09-{i % 28 + 1:02d}" for i in range(n)],
            "open": [100 + i * 0.1 for i in range(n)],
            "high": [101 + i * 0.1 for i in range(n)],
            "low": [99 + i * 0.1 for i in range(n)],
            "close": [100.5 + i * 0.1 for i in range(n)],
            "ma20": [100.2 + i * 0.1 for i in range(n)],
            "ma50": [None] * n,
            "ma200": [None] * n,
        }
        for h, vals in (
            ("5", [1.2, -0.5, 0.3, 2.0, -1.1, 0.8]),
            ("20", [0.5, -2.0, 1.3, 3.2, -0.7]),
            ("60", [4.0, -1.5, 2.2]),
        ):
            r["analogs"]["horizons"][h]["samples"] = vals

        w._on_outlook(r)

        assert w.kline_plot.plotItem.listDataItems()
        assert w.analog_plot.plotItem.listDataItems()
        assert w.sector_heat.layout().count() == len(r["sectors"]["all"])
    finally:
        w.close()
        app.processEvents()
