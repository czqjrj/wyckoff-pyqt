# -*- coding: utf-8 -*-
"""全局常量、图表字号状态与 matplotlib 初始化。

集中存放颜色表、事件字典、周期/周期刻度选项、字体候选与默认设置,
并维护 K 线图表基准字号 (`_CHART_FONT`) 及其访问/修改函数。
"""
import matplotlib

# GUI 后端由入口显式指定 (wxPython 版 wyckoff_desktop.py 设 WxAgg, --test 设 Agg),
# 此处不强制。
import matplotlib.dates as mdates  # noqa: F401  (注册日期 locator/formatter)

matplotlib.rcParams["font.sans-serif"] = [
    "Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "SimHei",
]
matplotlib.rcParams["axes.unicode_minus"] = False

# ── 界面主题 (浅色细化) ──
# 统一桌面端/图表的配色入口; 涨/多头用红, 跌/空头用绿 (A股习惯),
# 状态语义 (成功/错误) 与方向语义 (涨/跌) 分离, 避免混淆。
_THEME_LIGHT = {
    "bg":          "#f4f6fb",   # 窗口背景
    "panel":       "#ffffff",   # 面板/表格背景
    "border":      "#dce3f0",   # 分隔线
    "text":        "#1f2937",   # 正文
    "muted":       "#8a94a6",   # 次要文本
    "accent":      "#2563eb",   # 主强调 (操作/链接)
    "accent_dark": "#1d4fd7",
    # 方向语义 (A股: 红涨绿跌)
    "up":          "#e03131",   # 涨 / 多头
    "up_dark":     "#c92a2a",
    "down":        "#2f9e44",   # 跌 / 空头
    "down_dark":   "#15803d",
    "amber":       "#d97706",   # 中性警示
    # 控件
    "btn":         "#eef2f9",
    "btn_hover":   "#e2e8f3",
    "zebra":       "#f7f9fc",   # 表格斑马纹
    "header":      "#eef2f9",   # 表头底
    "sel":         "#dbeafe",   # 选中行高亮 (淡蓝)
    # 图表
    "grid":        "#dfe6f2",   # 图表网格线
    "axis":        "#cbd5e1",   # 坐标轴线
    "mkt":         "#f08c00",   # 大盘对比线
    "poc":         "#d97706",   # 成交重心线
    # P&F/K线 区间底色带 (浅色主题下的半透明浅色)
    "zone_acc":    "#ffe4e6",   # 吸筹区淡红
    "zone_dist":   "#d1fae5",   # 派发区淡绿
    "zone_neut":   "#f1f3f5",   # 中性区淡灰
}

# 深色护眼主题: 低亮度高对比, 蓝紫基调, 减少刺眼光线。
_THEME_DARK = {
    "bg":          "#12141a",   # 窗口背景 (近黑)
    "panel":       "#1b1e27",   # 面板/表格背景
    "border":      "#2c3038",   # 分隔线
    "text":        "#e6e9f0",   # 正文 (亮灰白)
    "muted":       "#8b93a3",   # 次要文本
    "accent":      "#6ea8fe",   # 主强调 (柔亮蓝)
    "accent_dark": "#4d7fd1",
    # 方向语义 (A股: 红涨绿跌, 深色下略微提亮保证可读性)
    "up":          "#e0534a",   # 涨 / 多头
    "up_dark":     "#c73e36",
    "down":        "#4cb26a",   # 跌 / 空头
    "down_dark":   "#3b9254",
    "amber":       "#e8a33d",   # 中性警示
    # 控件
    "btn":         "#242833",
    "btn_hover":   "#2e3342",
    "zebra":       "#20242e",   # 表格斑马纹
    "header":      "#262b38",   # 表头底
    "sel":         "#2f4a7a",   # 选中行高亮
    # 图表
    "grid":        "#2c3038",   # 图表网格线
    "axis":        "#41464f",   # 坐标轴线
    "mkt":         "#e8a33d",   # 大盘对比线
    "poc":         "#e8a33d",   # 成交重心线
    # P&F/K线 区间底色带 (深色主题下低饱和暗色)
    "zone_acc":    "#3a1f24",   # 吸筹区暗红
    "zone_dist":   "#1b3327",   # 派发区暗绿
    "zone_neut":   "#262b33",   # 中性区暗灰
}

THEMES = {
    "light": _THEME_LIGHT,
    "dark": _THEME_DARK,
}
THEME = _THEME_LIGHT

VERSION = "1.0.0"
VERSION_TAG = "v1.0"

# 界面字体候选 (按优先级, 取本机已安装者)
FONT_CANDIDATES = [
    "Noto Sans CJK SC",
    "Noto Sans Mono CJK SC",
    "Noto Serif CJK SC",
    "Source Han Sans CN",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "PingFang SC",
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
    "SimSun",
    "DejaVu Sans",
]

MONO_FONT = "Noto Sans Mono CJK SC"

# 图表基准字号, 与界面 font_size 设置同步; 绘图函数用 _fs(偏移) 相对缩放
_CHART_FONT = 11


def _fs(delta=0):
    return max(6, _CHART_FONT + delta)


def _set_chart_font(size):
    global _CHART_FONT
    _CHART_FONT = size


SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

EVENT_COLORS = {
    "PSY": "#8c564b",   # 初步支撑
    "SC": "#d62728",    # 卖出高潮
    "BC": "#ff7f0e",    # 买入高潮
    "AR": "#2ca02c",    # 自动反弹
    "ST": "#1f77b4",    # 二次测试
    "Spring": "#7b2fbe",  # 弹簧
    "UTAD": "#e07b00",  # 上冲派发
    "SOS": "#17becf",   # 强势信号
    "JOC": "#006400",   # 跨越小溪
    "LPS": "#4c78a8",   # 最后支撑点
    "BU": "#9a6b0a",    # 回撤
}

EVENT_CN = {
    "PSY": "初步支撑", "SC": "卖出高潮", "BC": "买入高潮", "AR": "自动反弹",
    "ST": "二次测试", "Spring": "弹簧", "UTAD": "上冲派发", "SOS": "强势信号",
    "JOC": "跨越小溪", "LPS": "最后支撑点", "BU": "回撤",
}

BULL_EVENTS = ("SOS", "JOC", "Spring", "LPS", "ST", "BU")
BEAR_EVENTS = ("UTAD",)
NEUTRAL_EVENTS = ("SC", "BC", "AR")


def event_dir(typ):
    """事件方向: 1=多头, -1=空头, 0=中性
    实证: SC/BC/AR 在不同阶段有不同含义, 统一标中性;
    SC 在吸筹末期为反转信号, BC 在派发末期为反转信号,
    AR 是 SC 后第一次反弹(常被卖出, 为结构确认而非方向信号)。"""
    if typ in BULL_EVENTS:
        return 1
    if typ in BEAR_EVENTS:
        return -1
    return 0


_PHASE_STYLE = {
    "markdown":     ("Markdown 下跌", "#c92a2a", 0.10, "下跌"),
    "accumulation": ("Accumulation 吸筹", "#2b8a3e", 0.13, "吸筹"),
    "markup":       ("Markup 拉升", "#1971c2", 0.13, "拉升"),
    "distribution": ("Distribution 派发", "#f08c00", 0.15, "派发"),
}

VSA_CN = {
    "ND": "无需求(无量上冲)", "NS": "无供给(无量下探)",
    "SC": "卖出高潮", "BC": "买入高潮",
    "SV": "停止量(供方枯竭)", "UT": "上冲量(诱多)",
    "SPR": "弹簧量(震仓)", "ER": "努力无结果(上涨)",
    "EF": "努力无结果(下跌)", "N": "中性",
    # ── 整合来源: FibAlgo VSA / VSA Advanced / Wyckoff-Pro (见 docs/vsa_sources) ──
    "DEM": "强势需求(高量宽幅阳收近高)", "SUP": "强势供给(高量宽幅阴收近低)",
    "ABS": "吸收(高量窄幅·努力无结果)",
    "CHOC": "性质变化(最宽幅+超高量+逆势, 阶段转换)",
    "EVR": "努力/结果背离(低努力高结果)",
    "UPT": "上冲量(高量宽幅突破前高后收低端·诱多)", "TEST": "二次测试(低量阴收高端)",
    "ETR": "努力上涨(高量宽幅阳收最高)", "ETF": "努力下跌(高量宽幅阴收最低)",
    "TRU": "诱多陷阱(突破前高后收弱)", "TRD": "诱空陷阱(跌破前低后收强)",
}

VSA_COLOR = {
    "ND": "#868e96", "NS": "#868e96", "SC": "#d62728", "BC": "#ff7f0e",
    "SV": "#17becf", "UT": "#e07b00", "SPR": "#7b2fbe", "ER": "#f08c00",
    "EF": "#4c78a8", "N": "#ced4da",
    # ── 新增: 按语义区分 (红系=空头/供给, 绿系=多头/需求, 橙紫=中性/警示) ──
    "DEM": "#2f9e44", "SUP": "#e03131",
    "ABS": "#f08c00", "CHOC": "#9c36b5", "EVR": "#868e96",
    "UPT": "#e03131", "TEST": "#2b8a3e",
    "ETR": "#2f9e44", "ETF": "#e03131",
    "TRU": "#d9480f", "TRD": "#2b8a3e",
}

# 主题语义色快捷常量 (方向: A股红涨绿跌)
C_UP = THEME["up"]
C_UP_DARK = THEME["up_dark"]
C_DOWN = THEME["down"]
C_DOWN_DARK = THEME["down_dark"]
C_ZEBRA = THEME["zebra"]
C_HEADER = THEME["header"]
C_SEL = THEME["sel"]
C_GRID = THEME["grid"]

ACC_PHASES = [
    ("A", "初步支撑 PSY + 卖出高潮 SC + 自动反弹 AR", "恐慌抛售 → 初步止跌"),
    ("B", "二次测试 ST / 区间震荡", "抛压衰减, 区间构筑"),
    ("C", "弹簧 Spring / 震仓", "最后一次探底, 主升前洗盘"),
    ("D", "强势信号 SOS / 最后支撑 LPS / 回撤 BU", "力量转移, 启动信号"),
    ("E", "突破 JOC → 主升浪", "脱离区间, 强势上行"),
]
DIST_PHASES = [
    ("A", "买入高潮 BC + 自动回落 AR", "狂热追涨 → 初步滞涨"),
    ("B", "二次上冲 UT / 区间震荡", "需求衰减, 高位派发"),
    ("C", "上冲派发 UTAD", "最后一次诱多, 出货关键"),
    ("D", "弱势信号 SOS失败 / LPS失守", "力量转移向下, 破位前奏"),
    ("E", "破位下跌 JOC反向下穿 → 主跌浪", "跌破区间, 弱势下行"),
]

PERIOD_OPTIONS = {
    "近1月": 30,
    "近2月": 60,
    "近3月": 90,
    "近6月": 180,
    "近1年": 250,
    "近2年": 500,
    "近3年": 700,
    "近5年": 1200,
    "全部": 1023,
}

SCALE_OPTIONS = {
    "日线": 240,
    "120分钟": 120,
    "60分钟": 60,
    "30分钟": 30,
    "15分钟": 15,
}

# ── 分析窗口常量 (统一入口, 避免各文件魔数漂移) ──
W_RECENT = 120    # 近期事件/信号统计窗口
W_PIVOT_LONG = 200  # 长周期枢轴/结构确认窗口
W_MA_LONG = 200     # 长期均线(年线)采样窗口

# 量价方向判定 (不同度量, 各自阈值集中于此)
ER_BULL = 1.2    # 涨带量/跌缩量 量价比 > 此值判多头
ER_BEAR = 0.8    # < 此值判空头
SD_BULL = 1.1    # 需求/供给强度比 >= 此值判买方占优
SD_BEAR = 0.9    # <= 此值判卖方主导

# 回测默认参数 (可在设置面板中调整)
DEFAULT_BACKTEST = {
    "horizon": 20,     # 触发后持有根数
    "min_n": 3,        # 最少样本数
    "cost": 0.004,     # 单边成本
}

DEFAULT_SETTINGS = {
    "default_load": "600104",
    "default_scale": "日线",
    "default_period": "近3年",
    "watch_width": 190,
    "right_width": 560,
    "font_family": "Noto Sans CJK SC",
    "font_size": 10,
    "chart_font_size": 11,
    "draw_waves": True,
    "draw_locks": True,
    # 启动时最大化窗口
    "start_maximized": True,
    # 实时行情自动刷新 (秒, 0=关闭)
    "auto_refresh": False,
    "refresh_interval": 30,
    # 定时扫描自选股信号 (重算威科夫信号+更新准确度, 秒)
    "auto_scan": False,
    "scan_interval": 3600,
    # 基本面/资金流确认机制 (关闭=离线快速模式, 不抓东财基本面/资金流/板块)
    "confirm_enabled": True,
    # 回测参数
    "bt_horizon": 20,
    "bt_min_n": 3,
    "bt_cost": 0.004,
    # 点数图格值来源: pct=最新价百分比 / atr=动态ATR(0.5×ATR14, 随波动率自适应)
    "pnf_box_mode": "pct",
    "pnf_atr_factor": 0.5,
    # 枢轴灵敏度档位: fast(细枢轴, 信号多) / normal(默认) / safe(粗枢轴, 假信号少)
    "pivot_sensitivity": "normal",
    # 仓位风险管理 (单笔风险预算)
    "portfolio_value": 0,
    "risk_pct": 0.02,
    "risk_min_rr": 3.0,
    # AI 反向证伪 (可选, 需 DeepSeek/OpenAI 兼容 API Key)
    "ai_falsify_enabled": False,
    # AI 报告解读 (可选, 与证伪共用同一 API Key/Base/Model)
    "ai_interpret_enabled": False,
    "ai_api_key": "",
    "ai_api_base": "https://api.deepseek.com",
    "ai_model": "deepseek-chat",
    # 解读语音播报 (TTS, 可选)
    "tts_enabled": False,
    "tts_engine": "auto",
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "tts_rate": 0,
    "tts_auto": False,
    # 单次播报最大字数: 默认 3000, 覆盖 AI 解读 (600~2000 字) 完整朗读;
    # 超过该值的文本由 tts.py 按句子分块连续朗读, 不再中途截断。
    "tts_max_chars": 3000,
    # 界面主题: light=浅色 / dark=深色护眼
    "theme": "light",
    # 分析结论 / AI解读 面板文字字号 (pt)
    "text_font_size": 11,
}

# ── 通用阈值 ──
# K线有效数据最少根数: 少于该数量视为数据不足 (数据源 / 市场数据检查共用)
MIN_KLINE_BARS = 20
# 全市场扫描时区分"成交额Top N (来自接口)"与"内置列表"的规模阈值
SCAN_SRC_FETCH_THRESHOLD = 51
