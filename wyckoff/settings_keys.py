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
    # K线复权口径: "qfq"=前复权(默认) / "hfq"=后复权 / "none"=不复权
    DATA_ADJUST = "data_adjust"


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
    # ── 交易成本拆分 (A股: 佣金/印花税/过户费, 替代扁平单边成本) ──
    # 佣金率 (双边, 万2.5=0.00025) 与单笔最低佣金 (5元)
    COMMISSION_RATE = "paper_commission_rate"
    MIN_COMMISSION = "paper_min_commission"
    # 印花税: 卖出单边 0.05% (2023-08-28 起)
    STAMP_TAX_RATE = "paper_stamp_tax_rate"
    # 过户费: 双向 0.001%
    TRANSFER_FEE_RATE = "paper_transfer_fee_rate"
    # 涨跌停成交约束: True=涨停封板买不进 / 跌停封板卖不出 (顺延), False=不约束
    LIMIT_FILL = "paper_limit_fill"
    # 板块权限: 未开通创业板/科创板时, 扫描/选股应排除对应代码。
    ENABLE_CHINEXT = "paper_enable_chinext"
    ENABLE_STAR = "paper_enable_star"
    # 追踪止损: 从持仓期内最高价回撤 trailing 幅度时平仓。
    # 改进(2026-09 回测): 移动止盈默认开启 - 浮盈达 TRAIL_ACTIVATE_PCT 后才启动
    # 峰值回落 TRAIL_BACK_PCT 平仓; 未激活前由固定 -STOP_LOSS 兜底。
    TRAILING_STOP = "paper_trailing_stop"
    # 移动止盈回落幅度 (激活后从峰值回撤该比例平仓)
    TRAIL_BACK_PCT = "paper_trail_back_pct"
    # 移动止盈激活浮盈比例 (<=0 表示复用 TAKE_PROFIT)
    TRAIL_ACTIVATE_PCT = "paper_trail_activate_pct"
    # 追踪止损 ATR 缓冲: stop = 杆位*(1-止损) - atr_mult*ATR (仅追踪开启时生效)
    TRAIL_ATR_MULT = "paper_trail_atr_mult"
    # QLib 卖出否决 (试点, 默认关): 主动性卖出 (止盈/移动止盈/到期) 前查 qlib 模型
    # prob_buy, ≥ PAPER_QLIB_VETO_HI 则否决一次防踏空 (与回测 --qlib-veto 同口径)
    QLIB_VETO = "paper_qlib_veto"
    QLIB_VETO_HI = "paper_qlib_veto_hi"
    # 弱市过滤: 指数(上证)收盘<MA20 → 新开仓上限 WEAK_MAX_POS 且停用价值吸筹
    WEAK_FILTER = "paper_weak_filter"
    WEAK_MAX_POS = "paper_weak_max_pos"
    WEAK_INDEX_CODE = "paper_weak_index_code"
    # 止损后再入冷却 (交易日根数, 0=关闭): 同标止损平仓后 N 个交易日内禁止再开仓。
    # 实证 (200只全A, conf=100): 冷却20根消除同标反复止损 (2024-06 sh605338 三连损),
    # maxpos=5+冷却 → +551%/-11.9% (优于 maxpos=4 的 +494%/-13.1%)。
    STOP_COOLDOWN = "paper_stop_cooldown"
    # ── 风控键 (8键配置化, S5; 引擎真源 _params.py, 默认引用同名常量) ──────
    # 最大账户回撤 (净值从峰值回落阈值)
    MAX_DRAWDOWN = "paper_max_drawdown"
    # 单笔最大风险预算 (账户净值百分比, Kelly 计算上限)
    MAX_RISK_PCT = "paper_max_risk_pct"
    # 仓位方法论: equal_weight / kelly / vol_adjusted / risk_parity / fixed_fractional
    SIZING_METHOD = "paper_sizing_method"
    # 最大行业集中度 (单行业持仓市值占总市值上限)
    MAX_SECTOR_CONC = "paper_max_sector_conc"
    # 最大单股集中度 (单股持仓市值占总市值上限)
    MAX_SINGLE_CONC = "paper_max_single_conc"
    # 相关性阈值 (拒绝开仓高相关标的, 需外部相关性矩阵)
    CORRELATION_THRESHOLD = "paper_correlation_threshold"
    # 波动率调整总开关: 高波动降仓 / 低波动升仓 (ATR 百分位)
    VOL_ADJUST_ENABLED = "paper_vol_adjust_enabled"
    # 资金利用率上限 (防止满仓无现金应对机会)
    MAX_CAPITAL_USAGE = "paper_max_capital_usage"
    # 价值吸筹单仓资金权重
    VA_WEIGHT = "paper_va_weight"
    # 价值吸筹策略总开关: False = 模拟盘完全停用该策略 (不再扫描/生成入场条件单)
    ENABLE_VA = "paper_enable_va"
    # 威科夫左侧买点策略总开关: False = 模拟盘完全停用 (不再扫描/生成入场条件单)
    ENABLE_LONG_LEFT = "paper_enable_long_left"
    # 事件+高价值VSA双因策略总开关: False = 模拟盘完全停用 (不再扫描/生成入场条件单)
    ENABLE_EVENT_VSA = "paper_enable_event_vsa"
    # ST 事件确认门槛: True = 仅已 confirmed 且确认后首根已到的 ST 事件可入候选
    # (Spring/其他强多头事件仍事件即买; 回测验证 +212%→+307% 全组合收益)
    ST_CONFIRM = "paper_st_confirm"
    # 周期级等权再平衡: 满仓且现金富余时补足低权重持仓到 总权益/max_pos
    REBALANCE = "paper_rebalance"
    # ── 微信推送 (交易发生时通知) ─────────────────────────────
    # 总开关; 渠道: "server_chan" (Server酱) / "wechat_work" (企业微信) / "wxpusher"
    PUSH = "paper_push_enabled"
    PUSH_METHOD = "paper_push_method"
    SERVER_CHAN_KEY = "paper_server_chan_key"
    WECHAT_CORP_ID = "paper_wechat_corp_id"
    WECHAT_CORP_SECRET = "paper_wechat_corp_secret"
    WECHAT_AGENT_ID = "paper_wechat_agent_id"
    WECHAT_TO_USER = "paper_wechat_to_user"
    # WxPusher: APP_TOKEN 仅创建时展示一次; 主题ID 与 用户UID 至少提供一个
    WXPUSHER_APP_TOKEN = "paper_wxpusher_app_token"
    WXPUSHER_TOPIC_IDS = "paper_wxpusher_topic_ids"
    WXPUSHER_UIDS = "paper_wxpusher_uids"


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
    General.DATA_ADJUST: "qfq",
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
    Paper.MAX_POS: 5,
    Paper.HOLD_BARS: 20,
    Paper.STOP_LOSS: 0.04,
    Paper.TAKE_PROFIT: 0.15,
    Paper.COST: 0.004,
    Paper.MIN_CONF: 100,
    Paper.SCAN_INTERVAL: 1800,
    Paper.SIZING_METHOD: "equal_weight",
    # 止损后再入冷却 (交易日根数): 降回撤实证最优, 见 _params.STOP_COOLDOWN
    Paper.STOP_COOLDOWN: 20,
    Paper.ENABLE_CHINEXT: False,
    Paper.ENABLE_STAR: False,
    # 交易成本拆分 (A股真实费率, 供模拟盘撮合按明细计费)
    Paper.COMMISSION_RATE: 0.00025,
    Paper.MIN_COMMISSION: 5.0,
    Paper.STAMP_TAX_RATE: 0.0005,
    Paper.TRANSFER_FEE_RATE: 0.00001,
    # 涨跌停成交约束默认开启
    Paper.LIMIT_FILL: True,
    # 价值吸筹回退默认关闭 (实测为负贡献): 需用户显式 paper_enable_va=true 开启
    Paper.ENABLE_VA: False,
    # 威科夫左侧买点默认关闭 (命中率与右侧纪律叠加度低): 需显式 paper_enable_long_left=true
    Paper.ENABLE_LONG_LEFT: False,
    # 事件+高价值VSA双因默认关闭 (独立兜底赛道): 需显式 paper_enable_event_vsa=true
    Paper.ENABLE_EVENT_VSA: False,
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
    # ── 补齐 Paper 域其余缺省键 (与 config.DEFAULT_SETTINGS 对账的完整登记) ──
    Paper.WEAK_FILTER: True,
    Paper.WEAK_MAX_POS: 1,
    Paper.WEAK_INDEX_CODE: "sh000001",
    Paper.TRAILING_STOP: True,
    Paper.TRAIL_BACK_PCT: 0.06,
    Paper.TRAIL_ACTIVATE_PCT: 0.0,
    Paper.TRAIL_ATR_MULT: 0.0,
    Paper.QLIB_VETO: False,
    Paper.QLIB_VETO_HI: 0.60,
    Paper.ST_CONFIRM: True,
    Paper.REBALANCE: True,
    Paper.VA_WEIGHT: 0.6,
    Paper.MAX_DRAWDOWN: 0.15,
    Paper.MAX_RISK_PCT: 0.02,
    Paper.MAX_SECTOR_CONC: 0.40,
    Paper.MAX_SINGLE_CONC: 0.25,
    Paper.CORRELATION_THRESHOLD: 0.70,
    Paper.VOL_ADJUST_ENABLED: True,
    Paper.MAX_CAPITAL_USAGE: 0.95,
    Paper.PUSH: False,
    Paper.PUSH_METHOD: "server_chan",
    Paper.SERVER_CHAN_KEY: "",
    Paper.WECHAT_CORP_ID: "",
    Paper.WECHAT_CORP_SECRET: "",
    Paper.WECHAT_AGENT_ID: "",
    Paper.WECHAT_TO_USER: "",
    Paper.WXPUSHER_APP_TOKEN: "",
    Paper.WXPUSHER_TOPIC_IDS: "",
    Paper.WXPUSHER_UIDS: "",
}


def _engine_paper_defaults():
    """Paper 域校准默认值 (单一真源 _params.py)。

    延迟 import: paper/__init__ 反引 settings_keys (S 类), 若在本模块顶部直接
    import 引擎参数会循环; 放到 S 类/DEFAULTS 之后执行, 任一侧先加载时符号均已
    就位 (import 包时仅触发一次, 子模块导入在父包部分初始化下仍可完成)。
    返回 {Paper 键: 引擎常量}, 供 DEFAULTS 覆盖并在程序内作为统一对照。
    """
    from .paper import _params as _p

    return {
        Paper.INIT_CASH: _p.INIT_CASH,
        Paper.MAX_POS: _p.MAX_POSITIONS,
        Paper.HOLD_BARS: _p.HOLD_BARS,
        Paper.STOP_LOSS: _p.STOP_LOSS,
        Paper.TAKE_PROFIT: _p.TAKE_PROFIT,
        Paper.COST: _p.COST,
        Paper.MIN_CONF: _p.MIN_CONF,
        Paper.STOP_COOLDOWN: _p.STOP_COOLDOWN,
        Paper.COMMISSION_RATE: _p.COMMISSION_RATE,
        Paper.MIN_COMMISSION: _p.MIN_COMMISSION,
        Paper.STAMP_TAX_RATE: _p.STAMP_TAX_RATE,
        Paper.TRANSFER_FEE_RATE: _p.TRANSFER_FEE_RATE,
        Paper.LIMIT_FILL: _p.LIMIT_FILL,
        Paper.ENABLE_VA: _p.ENABLE_VA,
        Paper.ENABLE_LONG_LEFT: _p.ENABLE_LONG_LEFT,
        Paper.ENABLE_EVENT_VSA: _p.ENABLE_EVENT_VSA,
        Paper.WEAK_FILTER: _p.WEAK_FILTER,
        Paper.WEAK_MAX_POS: _p.WEAK_MAX_POS,
        Paper.WEAK_INDEX_CODE: _p.WEAK_INDEX_CODE,
        Paper.ST_CONFIRM: _p.ST_CONFIRM,
        Paper.REBALANCE: _p.REBALANCE,
        Paper.VA_WEIGHT: _p.VA_WEIGHT,
        Paper.TRAILING_STOP: _p.TRAILING_STOP,
        Paper.TRAIL_BACK_PCT: _p.TRAIL_BACK_PCT,
        Paper.TRAIL_ACTIVATE_PCT: _p.TRAIL_ACTIVATE_PCT,
        Paper.TRAIL_ATR_MULT: _p.TRAIL_ATR_MULT,
        Paper.QLIB_VETO: _p.QLIB_VETO_ENABLED,
        Paper.QLIB_VETO_HI: _p.QLIB_VETO_HI,
        Paper.MAX_DRAWDOWN: _p.MAX_DRAWDOWN_PCT,
        Paper.MAX_RISK_PCT: _p.MAX_RISK_PCT,
        Paper.MAX_SECTOR_CONC: _p.MAX_SECTOR_CONCENTRATION,
        Paper.MAX_SINGLE_CONC: _p.MAX_SINGLE_CONCENTRATION,
        Paper.CORRELATION_THRESHOLD: _p.CORRELATION_THRESHOLD,
        Paper.VOL_ADJUST_ENABLED: _p.VOL_ADJUST_ENABLED,
        Paper.MAX_CAPITAL_USAGE: _p.MAX_CAPITAL_USAGE,
        Paper.PUSH: _p.PUSH_ENABLED,
        Paper.PUSH_METHOD: _p.PUSH_METHOD,
        Paper.SERVER_CHAN_KEY: _p.PUSH_SERVER_CHAN_KEY,
        Paper.WECHAT_CORP_ID: _p.PUSH_WECHAT_CORP_ID,
        Paper.WECHAT_CORP_SECRET: _p.PUSH_WECHAT_CORP_SECRET,
        Paper.WECHAT_AGENT_ID: _p.PUSH_WECHAT_AGENT_ID,
        Paper.WECHAT_TO_USER: _p.PUSH_WECHAT_TO_USER,
        Paper.WXPUSHER_APP_TOKEN: _p.PUSH_WXPUSHER_APP_TOKEN,
        Paper.WXPUSHER_TOPIC_IDS: _p.PUSH_WXPUSHER_TOPIC_IDS,
        Paper.WXPUSHER_UIDS: _p.PUSH_WXPUSHER_UIDS,
    }


DEFAULTS.update(_engine_paper_defaults())
