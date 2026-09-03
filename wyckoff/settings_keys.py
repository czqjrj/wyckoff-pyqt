"""集中化设置键常量 — 消除字符串字面量硬编码，便于重构与类型检查。

所有键名集中定义，按功能域分组。使用时：from wyckoff.settings_keys import S
然后 S.Theme.DEFAULT_LOAD 等。

(原位于 ui/settings_keys.py —— 设置键属于领域层契约, ui 反向 import wyckoff
才是正确方向; 此前 wyckoff.analysis/tts 依赖 ui 包属分层违规, 已下沉归位。)
"""

from enum import Enum


class _Base(str, Enum):
    """基类：既可当字符串用，又有 IDE 补全。"""
    def __str__(self):
        return self.value


# ── 基本/启动 ──────────────────────────────────────────────
class General(_Base):
    DEFAULT_LOAD = "default_load"
    DEFAULT_SCALE = "default_scale"
    DEFAULT_PERIOD = "default_period"
    START_MAXIMIZED = "start_maximized"
    AUTO_SHOW_SCREENER = "auto_show_screener"
    AUTO_SHOW_CALIB = "auto_show_calib"
    AUTO_SHOW_ENTRIES = "auto_show_entries"
    START_NO_ANALYSIS = "startup_no_analysis"
    THEME = "theme"


# ── 界面尺寸/字体 ──────────────────────────────────────────
class UI(_Base):
    FONT_FAMILY = "font_family"
    FONT_SIZE = "font_size"
    WATCH_FONT_SIZE = "watch_font_size"
    TEXT_FONT_SIZE = "text_font_size"
    CHART_FONT_SIZE = "chart_font_size"
    WATCH_WIDTH = "watch_width"
    RIGHT_WIDTH = "right_width"
    LEFT_PANEL_VISIBLE = "left_panel_visible"
    RIGHT_PANEL_VISIBLE = "right_panel_visible"
    PANEL_WIDTHS = "panel_widths"
    DOCK_STATE = "dock_state"


# ── 图表绘制 ──────────────────────────────────────────────
class Chart(_Base):
    DRAW_WAVES = "draw_waves"
    DRAW_LOCKS = "draw_locks"
    PNF_BOX_MODE = "pnf_box_mode"
    PNF_ATR_FACTOR = "pnf_atr_factor"
    PIVOT_SENSITIVITY = "pivot_sensitivity"
    KLINE_LAYERS = "kline_layers"
    IND_DEFAULT_BARS = "ind_default_bars"
    MKT_DEFAULT_BARS = "mkt_default_bars"


# ── 自动刷新/扫描/同步 ─────────────────────────────────────
class Auto(_Base):
    AUTO_REFRESH = "auto_refresh"
    REFRESH_INTERVAL = "refresh_interval"
    AUTO_SCAN = "auto_scan"
    SCAN_INTERVAL = "scan_interval"
    AUTO_SYNC = "auto_sync"
    SYNC_DEBOUNCE = "sync_debounce"
    CONFIRM_ENABLED = "confirm_enabled"


# ── 回测/风控 ──────────────────────────────────────────────
class Backtest(_Base):
    BT_HORIZON = "bt_horizon"
    BT_MIN_N = "bt_min_n"
    BT_COST = "bt_cost"
    PORTFOLIO_VALUE = "portfolio_value"
    RISK_PCT = "risk_pct"
    RISK_MIN_RR = "risk_min_rr"


# ── 模拟盘 (自动筛选/下单/卖出/统计) ────────────────────────
class Paper(_Base):
    INIT_CASH = "paper_init_cash"
    MAX_POS = "paper_max_pos"
    HOLD_BARS = "paper_hold_bars"
    STOP_LOSS = "paper_stop_loss"
    TAKE_PROFIT = "paper_take_profit"
    COST = "paper_cost"
    MIN_CONF = "paper_min_conf"
    SCAN_INTERVAL = "paper_scan_interval"
    # 板块权限: 未开通创业板/科创板时, 扫描/选股应排除对应代码。
    ENABLE_CHINEXT = "paper_enable_chinext"
    ENABLE_STAR = "paper_enable_star"
    # 追踪止损: 从持仓期内最高价回撤 trailing 幅度时平仓 (替代固定止损)
    TRAILING_STOP = "paper_trailing_stop"
    # 追踪止损 ATR 缓冲: stop = 杆位*(1-止损) - atr_mult*ATR
    TRAIL_ATR_MULT = "paper_trail_atr_mult"


class Watch(_Base):
    MOVE_THRESHOLD = "watch_move_threshold"
    MOVE_NOTIFY = "watch_move_notify"
    MOVE_REFRESH = "watch_move_refresh"


# ── AI 相关 ────────────────────────────────────────────────
class AI(_Base):
    FALSIFY_ENABLED = "ai_falsify_enabled"
    INTERPRET_ENABLED = "ai_interpret_enabled"
    API_KEY = "ai_api_key"
    API_BASE = "ai_api_base"
    MODEL = "ai_model"


# ── 语音播报 (TTS) ─────────────────────────────────────────
class TTS(_Base):
    ENABLED = "tts_enabled"
    AUTO = "tts_auto"
    ENGINE = "tts_engine"
    VOICE = "tts_voice"
    RATE = "tts_rate"
    MAX_CHARS = "tts_max_chars"


# ── 运行时记忆 (分析历史/校准仓库/档案同步) ──────────────────
class Runtime(_Base):
    LAST_ANALYZED_CODE = "last_analyzed_code"
    LAST_ANALYZED_SCALE = "last_analyzed_scale"
    LAST_ANALYZED_PERIOD = "last_analyzed_period"
    CALIB_REPO_URL = "calib_repo_url"
    PROFILE_REPO_URL = "profile_repo_url"
    PROFILE_SYNC = "profile_sync"


# ── 便捷聚合：S.<域>.<键> ───────────────────────────────────
class S:
    """单入口访问所有设置键。用法：S.General.DEFAULT_LOAD"""
    General = General
    UI = UI
    Chart = Chart
    Auto = Auto
    Backtest = Backtest
    Paper = Paper
    AI = AI
    TTS = TTS
    Watch = Watch
    Runtime = Runtime


# ── 兼容：扁平字典映射，供旧代码逐步迁移 ────────────────────
# 逐步替换 settings.get("key") → settings.get(S.Domain.KEY)
ALL_KEYS = {
    **{f"General.{k.name}": k.value for k in General},
    **{f"UI.{k.name}": k.value for k in UI},
    **{f"Chart.{k.name}": k.value for k in Chart},
    **{f"Auto.{k.name}": k.value for k in Auto},
    **{f"Backtest.{k.name}": k.value for k in Backtest},
    **{f"Paper.{k.name}": k.value for k in Paper},
    **{f"AI.{k.name}": k.value for k in AI},
    **{f"TTS.{k.name}": k.value for k in TTS},
    **{f"Watch.{k.name}": k.value for k in Watch},
    **{f"Runtime.{k.name}": k.value for k in Runtime},
}


def get(key_enum: _Base) -> str:
    """类型安全获取键值字符串。"""
    return key_enum.value


# ── 默认值字典（供首次初始化/测试用） ──────────────────────
DEFAULTS = {
    General.DEFAULT_LOAD: "",
    General.DEFAULT_SCALE: "日线",
    General.DEFAULT_PERIOD: "近3年",
    General.START_MAXIMIZED: True,
    General.AUTO_SHOW_SCREENER: False,
    General.AUTO_SHOW_CALIB: False,
    General.AUTO_SHOW_ENTRIES: False,
    General.START_NO_ANALYSIS: True,
    General.THEME: "light",
    UI.FONT_FAMILY: "",
    UI.FONT_SIZE: 12,
    UI.WATCH_FONT_SIZE: 12,
    UI.TEXT_FONT_SIZE: 11,
    UI.CHART_FONT_SIZE: 11,
    UI.WATCH_WIDTH: 190,
    UI.RIGHT_WIDTH: 560,
    UI.LEFT_PANEL_VISIBLE: True,
    UI.RIGHT_PANEL_VISIBLE: True,
    UI.PANEL_WIDTHS: {},
    UI.DOCK_STATE: None,
    Chart.DRAW_WAVES: True,
    Chart.DRAW_LOCKS: True,
    Chart.PNF_BOX_MODE: "pct",
    Chart.PNF_ATR_FACTOR: 0.5,
    Chart.PIVOT_SENSITIVITY: "normal",
    Chart.KLINE_LAYERS: {},
    Chart.IND_DEFAULT_BARS: 250,
    Chart.MKT_DEFAULT_BARS: 120,
    Auto.AUTO_REFRESH: False,
    Auto.REFRESH_INTERVAL: 30,
    Auto.AUTO_SCAN: False,
    Auto.SCAN_INTERVAL: 3600,
    Auto.AUTO_SYNC: False,
    Auto.SYNC_DEBOUNCE: 60,
    Auto.CONFIRM_ENABLED: True,
    Backtest.BT_HORIZON: 20,
    Backtest.BT_MIN_N: 3,
    Backtest.BT_COST: 0.004,
    Backtest.PORTFOLIO_VALUE: 0,
    Backtest.RISK_PCT: 0.02,
    Backtest.RISK_MIN_RR: 3.0,
    Paper.INIT_CASH: 1_000_000,
    Paper.MAX_POS: 3,
    Paper.HOLD_BARS: 20,
    Paper.STOP_LOSS: 0.03,
    Paper.TAKE_PROFIT: 0.15,
    Paper.COST: 0.004,
    Paper.MIN_CONF: 90,
    Paper.SCAN_INTERVAL: 1800,
    Paper.ENABLE_CHINEXT: False,
    Paper.ENABLE_STAR: False,
    AI.FALSIFY_ENABLED: False,
    AI.INTERPRET_ENABLED: False,
    AI.API_KEY: "",
    AI.API_BASE: "https://api.deepseek.com",
    AI.MODEL: "deepseek-chat",
    TTS.ENABLED: False,
    TTS.AUTO: False,
    TTS.ENGINE: "auto",
    TTS.VOICE: "zh-CN-XiaoxiaoNeural",
    TTS.RATE: 0,
    TTS.MAX_CHARS: 3000,
    Watch.MOVE_THRESHOLD: 2.0,
    Watch.MOVE_NOTIFY: True,
    Watch.MOVE_REFRESH: True,
    Runtime.LAST_ANALYZED_CODE: "",
    Runtime.LAST_ANALYZED_SCALE: "日线",
    Runtime.LAST_ANALYZED_PERIOD: "近3年",
    Runtime.CALIB_REPO_URL: "",
    Runtime.PROFILE_REPO_URL: "",
    Runtime.PROFILE_SYNC: False,
}
