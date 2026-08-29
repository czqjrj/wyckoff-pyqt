"""技术指标面板声明式注册表 — 让指标面板可扩展/可配置。

每个面板通过 IndPanelDef 声明:
  - key: 面板唯一标识
  - title: 初始标题
  - row/col/colspan/stretch: GraphicsLayout 布局
  - fmt_y: 十字光标 Y 读数格式化
  - is_volume_x: X 轴是否为成交量 (非日期) 单位

绘制函数由 IndWidget 通过注册的 draw 方法调用 (见 draw_fns 注册表)。
"""



class IndPanelDef:
    """单个指标面板的声明式定义。"""

    __slots__ = ("key", "title", "row", "col", "colspan", "stretch",
                 "fmt_y", "is_volume_x", "yrange")

    def __init__(self, key, title, row, col, colspan=1, stretch=13,
                 fmt_y=None, is_volume_x=False, yrange=None):
        self.key = key
        self.title = title
        self.row = row
        self.col = col
        self.colspan = colspan
        self.stretch = stretch
        self.fmt_y = fmt_y or (lambda v: f"{v:.2f}")
        self.is_volume_x = bool(is_volume_x)
        self.yrange = yrange  # 可选的 yrange 计算函数 (输入完整数据, 返回 (lo, hi))


# ── 面板注册表: 按 key 组织 ──
_PANELS = {}


def register_panel(panel_def):
    """注册一个指标面板定义。"""
    _PANELS[panel_def.key] = panel_def
    return panel_def


# ── 内置面板定义 ──
def _build_default_panels():
    return [
        register_panel(IndPanelDef(
            "macd", "MACD (12,26,9)", 0, 0, colspan=2, stretch=13,
            fmt_y=lambda v: f"{v:.3f}")),
        register_panel(IndPanelDef(
            "volume", "量能 (万手)", 2, 0, colspan=1, stretch=13,
            fmt_y=lambda v: f"{v:.1f}万手")),
        register_panel(IndPanelDef(
            "price", "价格 · 布林带 (20,2) · 大盘对比", 2, 1, colspan=1, stretch=13,
            fmt_y=lambda v: f"{v:.2f}")),
        register_panel(IndPanelDef(
            "kdj", "KDJ (9,3,3)", 4, 0, colspan=1, stretch=13,
            fmt_y=lambda v: f"{v:.1f}")),
        register_panel(IndPanelDef(
            "rsi", "RSI (6,12,24)", 4, 1, colspan=1, stretch=13,
            fmt_y=lambda v: f"{v:.1f}")),
        register_panel(IndPanelDef(
            "obv", "OBV 能量潮", 6, 0, colspan=1, stretch=15,
            fmt_y=lambda v: f"{v:.0f}")),
        register_panel(IndPanelDef(
            "vp", "量价分布 (Volume Profile)", 6, 1, colspan=1, stretch=15,
            fmt_y=lambda v: f"{v:.2f}", is_volume_x=True)),
        register_panel(IndPanelDef(
            "rs", "相对强度 RS (20日) vs 上证指数", 8, 0, colspan=2, stretch=11,
            fmt_y=lambda v: f"{v:+.1f}%")),
    ]


def get_panels():
    """获取全部已注册面板定义 (按注册顺序)。"""
    return list(_PANELS.values())


def get_panel(key):
    """按 key 获取面板定义, 不存在返回 None。"""
    return _PANELS.get(key)


# 模块导入时构建默认面板
_build_default_panels()


def _fmt_cn_vol(v):
    """成交量缩写 (万/亿), 供列信息卡与 HUD。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"
