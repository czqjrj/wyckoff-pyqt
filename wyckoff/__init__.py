# -*- coding: utf-8 -*-
"""wyckoff — 威科夫 (Wyckoff) 桌面分析工具核心包。

重新导出顶层 API, 使 `import wyckoff as w; w.fetch_kline(...)` 与旧单文件用法兼容。
"""
from .config import VERSION as __version__

from .config import (VERSION, VERSION_TAG, EVENT_COLORS, EVENT_CN, PERIOD_OPTIONS,
                     SCALE_OPTIONS, THEME, C_UP, C_UP_DARK, C_DOWN, C_DOWN_DARK,
                     C_ZEBRA, C_HEADER, C_SEL, C_GRID, _fs, _set_chart_font)
from .paths import DATA_DIR
from .utils import normalize_symbol
from .sqldb import clear_cache, cache_stats
from .datasource import (fetch_kline, fetch_realtime, fetch_name,
                         data_source_of)
from .pinyin import search_stock
from .storage import (load_watchlist, save_watchlist, load_settings,
                      save_settings, load_feedback, save_feedback,
                      feedback_key, build_feedback_record)
from .indicators import add_indicators, find_pivots
from .events import detect_all
from .phases import judge_phase, phase_segments
from .waves import elliott_wave, extract_wave_points, calc_targets
from .pnf import build_pnf, pnf_targets, pnf_history_targets, plot_pnf
from .vsa import vsa_classify
from .fusion import fuse_signals
from .structure import structure_progress
from .market import (estimate_fund_flow, compute_chip_concentration,
                     fetch_holder_history, build_market_labels, volume_profile,
                     supply_demand, find_trading_range, fetch_market_env,
                     fetch_market_series, relative_strength)
from .multitime import multi_tf_analysis
from .fundamental import (fetch_fundamental, fetch_main_flow, fetch_sector,
                          fetch_sector_flow, fetch_market_universe,
                          build_confirm_section, fetch_all_board_stats,
                          fetch_board_constituents)
from .backtest import (backtest_events, robustness_check, scan_stock_signals,
                       scan_sectors, scan_sector_stocks, signal_score)
from .conclusion import build_signal_summary, build_conclusion, sections_to_text
from .chart import plot_chart, plot_indicators
from .analysis import run_analysis, build_trade_plan, cached_kline
from .nteam import NTEAM_ETFS, track_nteam, nteam_summary
