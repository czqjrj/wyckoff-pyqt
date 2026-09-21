"""P&F 目标"到达日期"标签的 UI 渲染回归测试。

数据侧 (wyckoff/pnf.py) 为每列附 K 线日期并在目标命中处写到达日期;
本测试验证 pyqtgraph 渲染器 (ui/renderers/pnf_history.py / pnf_targets.py)
把这些日期真正画到图上, 以及两个纯函数辅助 (ui/renderers/_hit_*) 的格式。
"""
import os
import re
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from wyckoff.pnf import build_pnf, pnf_history_targets, pnf_targets

APP = None


def _df_with_trends():
    np.random.seed(7)
    close = []
    p = 80.0
    for _nbar, _base, drift in [(300, 100, 0.15), (120, 135, 0.1),
                                (200, 100, -0.15), (180, 85, 0.1)]:
        for _ in range(_nbar):
            p += drift + np.random.randn() * 1.2
            close.append(p)
    close = np.array(close)
    return pd.DataFrame({
        "day": pd.date_range("2023-01-01", periods=len(close)),
        "open": close * 0.999, "close": close,
        "high": close * 1.008, "low": close * 0.992,
        "volume": np.random.rand(len(close)) * 1e6,
    })


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    global APP
    APP = QApplication.instance() or QApplication([])
    return APP


def _plot():
    import pyqtgraph as pg
    w = pg.GraphicsLayoutWidget()
    w.setWindowTitle("pnf-arrival-test")
    plot = w.addPlot()
    return w, plot


def _texts(w):
    out = []
    scene = w.scene()
    if scene is None:
        return out
    for it in scene.items():
        if hasattr(it, "toPlainText"):
            t = it.toPlainText()
            if t:
                out.append(t)
    return out


def test_history_renderer_shows_arrival_date(app):
    from ui.renderers.pnf_history import PnfHistoryRenderer
    df = _df_with_trends()
    cols, box = build_pnf(df)
    hist = pnf_history_targets(cols, box)
    assert any(h.get("up_hit_date") or h.get("down_hit_date") for h in hist)
    w, plot = _plot()
    PnfHistoryRenderer(hist, cols, box, (0.0, 1.0)).draw(plot)
    labels = "\n".join(_texts(w))
    dated = [s for s in labels.splitlines()
             if re.search(r"已到 \d{4}-\d{2}-\d{2}", s)]
    assert dated, ("历史目标'已到'标注应带到达日期")


def test_targets_renderer_shows_arrival_date(app):
    from ui.renderers.pnf_targets import PnfTargetsRenderer
    df = _df_with_trends()
    cols, box = build_pnf(df)
    t = pnf_targets(df, cols, box)
    assert (t.get("下方hit_近端") or t.get("上方hit_近端")
            or any(t.get(f"上方hit_{n}") or t.get(f"下方hit_{n}")
                   for n in ("保守", "中", "激进")))
    w, plot = _plot()
    PnfTargetsRenderer(t, cols, box, (0.0, 1.0)).draw(plot)
    labels = "\n".join(_texts(w))
    dated = [s for s in labels.splitlines() if "已到" in s]
    assert dated, "当前TR命中目标标签应带到达日期"


def test_hit_date_lbl_helper():
    from ui.renderers.pnf_targets import _hit_date_lbl
    t = {"上方hit_保守": True, "上方hit日期_保守": "2026-01-01"}
    assert _hit_date_lbl(t, "上方", "保守") == " ·已到2026-01-01"
    assert _hit_date_lbl(t, "上方", "中") == ""          # 未命中档
    assert _hit_date_lbl({}, "上方", "保守") == ""        # 无命中信息
    assert _hit_date_lbl({"上方hit_保守": True}, "上方", "保守") == ""  # 无日期


def test_history_hit_suffix_helper():
    from ui.renderers.pnf_history import _hit_suffix
    assert _hit_suffix(True, "2026-01-01") == " 2026-01-01"
    assert _hit_suffix(True, None) == ""
    assert _hit_suffix(False, "2026-01-01") == ""
    assert _hit_suffix(False, "") == ""


def test_reach_status_unreached_shows_space():
    from wyckoff.pnf import pnf_reach_status
    t = {"横向计数上方目标_保守": 12.34, "上方空间_保守%": 5.2,
         "上方概率_保守": 0.45}
    assert pnf_reach_status(t, "上方", "上涨") == "上涨目标12.34 空间+5.2% 概率45%"
    t2 = {"横向计数下方目标_保守": 9.87, "下方空间_保守%": -8.1,
          "下方概率_保守": 0.60}
    assert pnf_reach_status(t2, "下方", "下跌") == "下跌目标9.87 空间-8.1% 概率60%"


def test_reach_status_reached_shows_overshoot_and_date():
    from wyckoff.pnf import pnf_reach_status
    t = {"横向计数上方目标_保守": 12.34, "上方空间_保守%": -1.2,
         "上方概率_保守": 0.45, "上方hit_保守": True,
         "上方hit日期_保守": "2024-08-21"}
    assert pnf_reach_status(t, "上方", "上涨") == \
        "上涨目标12.34 已到2024-08-21(超1.2%) 概率45%"
    # 已到但无日期: 不显示日期也不报错
    t2 = {"横向计数下方目标_保守": 9.87, "下方空间_保守%": 3.0,
          "下方hit_保守": True}
    assert pnf_reach_status(t2, "下方", "下跌") == "下跌目标9.87 已到"


def test_reach_status_missing_target():
    from wyckoff.pnf import pnf_reach_status
    assert pnf_reach_status({}, "上方", "上涨") == "上涨目标待确认"


def test_reach_status_from_real_pnf_targets(app):
    from wyckoff.pnf import pnf_reach_status
    df = _df_with_trends()
    cols, box = build_pnf(df)
    t = pnf_targets(df, cols, box)
    up = pnf_reach_status(t, "上方", "上涨")
    dn = pnf_reach_status(t, "下方", "下跌")
    assert "上涨目标" in up and "下跌目标" in dn
    assert ("空间" in up or "已到" in up) and ("空间" in dn or "已到" in dn)


def test_widget_info_bar_shows_both_directions(app):
    """顶部信息条两个方向都常显, 且到达状态置于行首并左对齐。

    信息条整行远宽于视口, 居中会被两端截断; 到达状态必须在行首 + 左对齐锚点,
    否则用户看不到。"""
    from ui.pnf_widget import PnfWidget
    df = _df_with_trends()
    cols, box = build_pnf(df)
    t = pnf_targets(df, cols, box)
    t["direction"] = "up"          # 即使方向为上涨, 下跌侧也应显示
    w = PnfWidget()
    w.set_data(cols=cols, box=box, title="t", targets=t)
    bar = next(ti for ti, fy in w._pinned
               if hasattr(ti, "toPlainText") and fy == 0.018)
    txt = bar.toPlainText()
    assert txt.startswith("上涨目标"), "到达状态应置于信息条行首"
    assert "下跌目标" in txt
    assert bar._pin_anchor == (0, 0.5), "信息条应左对齐以避开右侧截断"
