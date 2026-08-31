"""wyckoff — 威科夫 (Wyckoff) 桌面分析工具核心包。

重新导出顶层 API, 使 `import wyckoff as w; w.fetch_kline(...)` 与旧单文件用法兼容。
"""
from .analysis import build_trade_plan, cached_kline, run_analysis
from .backtest import (
    backtest_events,
    robustness_check,
    scan_sector_stocks,
    scan_sectors,
    scan_stock_signals,
    signal_score,
)
from .chart import plot_chart, plot_indicators
from .conclusion import build_conclusion, build_signal_summary, sections_to_text
from .config import (
    C_DOWN,
    C_DOWN_DARK,
    C_GRID,
    C_HEADER,
    C_SEL,
    C_UP,
    C_UP_DARK,
    C_ZEBRA,
    EVENT_CN,
    EVENT_COLORS,
    PERIOD_OPTIONS,
    SCALE_OPTIONS,
    THEME,
    VERSION,
    VERSION_TAG,
    _fs,
    _set_chart_font,
)
from .config import VERSION as __version__
from .datasource import data_source_of, fetch_kline, fetch_name, fetch_realtime
from .events import detect_all
from .fundamental import (
    build_confirm_section,
    fetch_all_board_stats,
    fetch_board_constituents,
    fetch_fundamental,
    fetch_main_flow,
    fetch_market_universe,
    fetch_sector,
    fetch_sector_flow,
)
from .fusion import fuse_signals
from .indicators import add_indicators, find_pivots
from .market import (
    build_market_labels,
    compute_chip_concentration,
    estimate_fund_flow,
    fetch_holder_history,
    fetch_market_env,
    fetch_market_series,
    find_trading_range,
    relative_strength,
    supply_demand,
    volume_profile,
)
from .multitime import multi_tf_analysis
from .nteam import NTEAM_ETFS, nteam_summary, track_nteam
from .paths import DATA_DIR
from .phases import judge_phase, phase_segments
from .pinyin import search_stock
from .pnf import build_pnf, plot_pnf, pnf_history_targets, pnf_targets
from .sqldb import cache_stats, clear_cache
from .storage import (
    build_feedback_record,
    feedback_key,
    load_feedback,
    load_settings,
    load_watchlist,
    save_feedback,
    save_settings,
    save_watchlist,
)
from .structure import structure_progress
from .utils import normalize_symbol
from .vsa import vsa_classify
from .waves import calc_targets, elliott_wave, extract_wave_points

__all__ = [
    "build_trade_plan",
    "cached_kline",
    "run_analysis",
    "backtest_events",
    "robustness_check",
    "scan_sector_stocks",
    "scan_sectors",
    "scan_stock_signals",
    "signal_score",
    "plot_chart",
    "plot_indicators",
    "build_conclusion",
    "build_signal_summary",
    "sections_to_text",
    "C_DOWN",
    "C_DOWN_DARK",
    "C_GRID",
    "C_HEADER",
    "C_SEL",
    "C_UP",
    "C_UP_DARK",
    "C_ZEBRA",
    "EVENT_CN",
    "EVENT_COLORS",
    "PERIOD_OPTIONS",
    "SCALE_OPTIONS",
    "THEME",
    "VERSION",
    "VERSION_TAG",
    "_fs",
    "_set_chart_font",
    "__version__",
    "data_source_of",
    "fetch_kline",
    "fetch_name",
    "fetch_realtime",
    "detect_all",
    "build_confirm_section",
    "fetch_all_board_stats",
    "fetch_board_constituents",
    "fetch_fundamental",
    "fetch_main_flow",
    "fetch_market_universe",
    "fetch_sector",
    "fetch_sector_flow",
    "fuse_signals",
    "add_indicators",
    "find_pivots",
    "build_market_labels",
    "compute_chip_concentration",
    "estimate_fund_flow",
    "fetch_holder_history",
    "fetch_market_env",
    "fetch_market_series",
    "find_trading_range",
    "relative_strength",
    "supply_demand",
    "volume_profile",
    "multi_tf_analysis",
    "NTEAM_ETFS",
    "nteam_summary",
    "track_nteam",
    "DATA_DIR",
    "judge_phase",
    "phase_segments",
    "search_stock",
    "build_pnf",
    "plot_pnf",
    "pnf_history_targets",
    "pnf_targets",
    "cache_stats",
    "clear_cache",
    "build_feedback_record",
    "feedback_key",
    "load_feedback",
    "load_settings",
    "load_watchlist",
    "save_feedback",
    "save_settings",
    "save_watchlist",
    "structure_progress",
    "normalize_symbol",
    "vsa_classify",
    "calc_targets",
    "elliott_wave",
    "extract_wave_points",
]
