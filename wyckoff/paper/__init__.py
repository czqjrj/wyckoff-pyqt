"""模拟盘 (Paper Trading) 核心引擎。

自动筛选 → 自动下单 → 自动卖出 → 收益统计 全自动闭环, 无真实资金风险。

设计要点 (对齐项目已验证的实证参数, 见 docs/profitability_bt.md):
  - 选股: 全市场 universe 逐股 detect_all, 只取强多头事件 (Spring/
    ST/LPS) 且 conf 达标 (默认 ≥100) 的标的, 按 conf 排序。
  - 撮合: 候选按 conf 填充, 同持上限内用"最近收盘价 + 滑点"买入
    (无未来函数: 同一周期内新成交仓位不参与当期卖出评估)。
  - 卖出触发: 持有满 HOLD_BARS 根到期 / 结构位-4%止损 / 破位 /
     +15%移动止盈 (浮盈达止盈线后从峰值回落8%平仓)。
  - 统计: 分类型/总账户 胜率、盈亏比、净值曲线、最大回撤。
  - 存储: 单 JSON (wx_paper.json), 与项目其他 wx_* 数据文件同目录同风格。
数据目录用 paths.DATA_DIR, 测试用 WYCKOFF_DATA_DIR 隔离。

增强功能:
  - 风控: 最大回撤限制、相关性限制、Kelly 资金管理、波动率调整仓位
  - 高级订单: OCO、括号单、分批建仓/平仓、冰山单
  - 实时监控: WebSocket 行情推送、价格触发条件单
  - 绩效分析: 夏普/索提诺/卡尔马比率、归因分析、回撤分析
"""
from __future__ import annotations

import itertools
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

from .. import paper_log, paper_strategy_accuracy
from ..paths import PAPER_FILE
from ..settings_keys import S
from ..strategies.constants import (
    STRATEGY_DISCIPLINE,
    STRATEGY_LONG_LEFT,
    STRATEGY_VALUE_ACC,
)
from ..trading_time import gate_reason

from ._params import *  # noqa: F401,F403  常量/枚举/数据类 (可配置+风控+订单)
from ._params import _POS_WEIGHT  # noqa: F401
from ._state import file_path, load_state, _new_state, save_state
from ._push import _notify_trade, _push_dispatch, _split_list
from ._risk import (
    check_drawdown_limit,
    check_risk_budget,
    check_sector_concentration,
    check_single_concentration,
    check_capital_usage,
    _risk_blocks_entry,
    calculate_kelly_fraction,
    calculate_position_size,
    calculate_var,
    calculate_position_risk,
    update_portfolio_risk,
)
from ._orders import (
    create_oco_order,
    create_bracket_order,
    create_scale_in_order,
    create_scale_out_order,
    create_trailing_stop_order,
    check_advanced_orders,
    _cancel_sibling_orders,
    cancel_advanced_order,
)
from ._stats import (
    net_cost_rate,
    float_ret,
    equity,
    _record_equity,
    stats,
    advanced_stats,
    signal_stats_text,
)


from ..discipline import flow_net5 as _flow_net5  # noqa: E402  测试会 monkeypatch paper._flow_net5
from ..discipline import market_trend_ok as _market_trend_ok
from ..discipline import sector_strength_ok as _sector_strength_ok

from ._selection import (
    _weak_market_flag,
    _strategy_manager,
    _value_accum_candidate,
    _long_buy_candidate,
    _is_low_quality,
    _probe_workers,
    _mainboard_universe,
    pick_candidates,
    _stock_name,
)
from ._conditions import (
    _cond,
    add_condition,
    _apply_auto_conditions,
    place_condition,
    _create_position_conditions,
    _backfill_position_protection,
    cancel_condition,
    _match_trigger,
    _check_conditions,
    _find_pos,
    _t1_blocked,
    _judge_condition_correct,
    _fire_condition,
)
from ._trading import (
    _next_open,
    execute_date,
    has_position,
    _make_order,
    place_buy_order,
    _enqueue_buy,
    fill_buy,
    step,
    _rebalance_portfolio,
    close_position,
    force_close_position,
)

_LOCK = threading.RLock()

# 条件单 cid 递增序号: 时间戳微秒在同一轮回/并发下会重复 (曾出现 trailing 与
# stop_loss 紧邻创建同 cid 的撞车), 追加序号保证全进程内唯一。
_CID_SEQ = itertools.count(1)

# 全市场重扫的冷却窗口 (分钟): run_cycle 在多周期场景下复用候选, 避免每 30 分钟
# 前一次全市场扫描的候选被下一次周期无条件重扫覆盖 (与 run_scan 的 next_scan_time 一致)。
_SCAN_COOLDOWN_MIN = 30

# 策略管理器单例占位 (延迟实例化, 见 _strategy_manager)
_SMGR = None



def apply_paper_params(settings=None):
    """从用户设置 dict 解析并覆盖模拟盘策略参数, 返回当前生效参数字典 `_CUR`.

    不传/键缺失时回退到模块常量默认值, 保证与旧行为完全一致 (测试兼容)。
    支持键 (见 settings_keys.Paper): INIT_CASH/MAX_POS/HOLD_BARS/STOP_LOSS/
    TAKE_PROFIT/COST/MIN_CONF。调用方 (run_cycle/UI) 在周期执行前调用即可生效。

    新增风控参数: MAX_DRAWDOWN/MAX_RISK_PCT/MAX_SECTOR_CONC/MAX_SINGLE_CONC/
    CORRELATION_THRESHOLD/VOL_ADJUST_ENABLED/MAX_CAPITAL_USAGE
    """
    settings = settings or {}

    def _get(key, default):
        v = settings.get(key)
        return default if v is None or v == "" else v

    global _CUR
    _CUR = {
        "init_cash": float(_get(S.Paper.INIT_CASH, INIT_CASH)),
        "max_pos": max(1, int(_get(S.Paper.MAX_POS, MAX_POSITIONS))),
        "hold_bars": max(1, int(_get(S.Paper.HOLD_BARS, HOLD_BARS))),
        "stop_loss": float(_get(S.Paper.STOP_LOSS, STOP_LOSS)),
        "take_profit": float(_get(S.Paper.TAKE_PROFIT, TAKE_PROFIT)),
        "cost": float(_get(S.Paper.COST, COST)),
        "min_conf": int(_get(S.Paper.MIN_CONF, MIN_CONF)),
        # 风控参数
        "max_drawdown": float(_get("paper_max_drawdown", MAX_DRAWDOWN_PCT)),
        "max_risk_pct": float(_get("paper_max_risk_pct", MAX_RISK_PCT)),
        "max_sector_conc": float(_get("paper_max_sector_conc", MAX_SECTOR_CONCENTRATION)),
        "max_single_conc": float(_get("paper_max_single_conc", MAX_SINGLE_CONCENTRATION)),
        "correlation_threshold": float(_get("paper_correlation_threshold", CORRELATION_THRESHOLD)),
        "vol_adjust_enabled": bool(_get("paper_vol_adjust_enabled", VOL_ADJUST_ENABLED)),
        "max_capital_usage": float(_get("paper_max_capital_usage", MAX_CAPITAL_USAGE)),
        # 资金管理方式
        "sizing_method": _get("paper_sizing_method", PositionSizingMethod.EQUAL_WEIGHT.value),
        # 板块权限: 未开通创业板/科创板 → 扫描/选股排除对应代码
        "enable_chinext": bool(_get(S.Paper.ENABLE_CHINEXT, False)),
        "enable_star": bool(_get(S.Paper.ENABLE_STAR, False)),
        # 移动止盈/追踪止损: 峰值回撤平仓; 开启 trail_back_pct 时由条件单先"激活后回落"
        "trailing_stop": bool(_get(S.Paper.TRAILING_STOP,
                                   _get("paper_trailing_stop", TRAILING_STOP))),
        "trail_atr_mult": float(_get(S.Paper.TRAIL_ATR_MULT,
                                     _get("paper_trail_atr_mult", TRAIL_ATR_MULT))),
        "trail_back_pct": float(_get(S.Paper.TRAIL_BACK_PCT, TRAIL_BACK_PCT)),
        "trail_activate_pct": float(_get(S.Paper.TRAIL_ACTIVATE_PCT,
                                         TRAIL_ACTIVATE_PCT)),
        # 弱市降仓过滤
        "weak_filter": bool(_get(S.Paper.WEAK_FILTER, WEAK_FILTER)),
        "weak_max_pos": max(1, int(_get(S.Paper.WEAK_MAX_POS, WEAK_MAX_POS))),
        "weak_index_code": _get(S.Paper.WEAK_INDEX_CODE, WEAK_INDEX_CODE),
        # 价值吸筹资金降权
        "va_weight": float(_get(S.Paper.VA_WEIGHT, VA_WEIGHT)),
        # 价值吸筹总开关 (False=彻底停用)
        "enable_va": bool(_get(S.Paper.ENABLE_VA, ENABLE_VA)),
        # 周期级等权再平衡
        "rebalance": bool(_get(S.Paper.REBALANCE, _get("paper_rebalance", REBALANCE))),
        # 微信推送配置
        "push_enabled": bool(_get(S.Paper.PUSH, PUSH_ENABLED)),
        "push_method": _get(S.Paper.PUSH_METHOD, PUSH_METHOD),
        "server_chan_key": _get(S.Paper.SERVER_CHAN_KEY, PUSH_SERVER_CHAN_KEY),
        "wechat_corp_id": _get(S.Paper.WECHAT_CORP_ID, PUSH_WECHAT_CORP_ID),
        "wechat_corp_secret": _get(S.Paper.WECHAT_CORP_SECRET, PUSH_WECHAT_CORP_SECRET),
        "wechat_agent_id": _get(S.Paper.WECHAT_AGENT_ID, PUSH_WECHAT_AGENT_ID),
        "wechat_to_user": _get(S.Paper.WECHAT_TO_USER, PUSH_WECHAT_TO_USER),
        "wxpusher_app_token": _get(S.Paper.WXPUSHER_APP_TOKEN, PUSH_WXPUSHER_APP_TOKEN),
        "wxpusher_topic_ids": _get(S.Paper.WXPUSHER_TOPIC_IDS, PUSH_WXPUSHER_TOPIC_IDS),
        "wxpusher_uids": _get(S.Paper.WXPUSHER_UIDS, PUSH_WXPUSHER_UIDS),
    }
    return _CUR


# 当前生效参数 (默认=模块常量; 由 apply_paper_params 按用户设置覆盖)。
_CUR = {
    "init_cash": INIT_CASH,
    "max_pos": MAX_POSITIONS,
    "hold_bars": HOLD_BARS,
    "stop_loss": STOP_LOSS,
    "take_profit": TAKE_PROFIT,
    "cost": COST,
    "min_conf": MIN_CONF,
    "max_drawdown": MAX_DRAWDOWN_PCT,
    "max_risk_pct": MAX_RISK_PCT,
    "max_sector_conc": MAX_SECTOR_CONCENTRATION,
    "max_single_conc": MAX_SINGLE_CONCENTRATION,
    "correlation_threshold": CORRELATION_THRESHOLD,
    "vol_adjust_enabled": VOL_ADJUST_ENABLED,
    "max_capital_usage": MAX_CAPITAL_USAGE,
    "sizing_method": PositionSizingMethod.EQUAL_WEIGHT.value,
    "enable_chinext": False,
    "enable_star": False,
    "trailing_stop": TRAILING_STOP,
    "trail_atr_mult": TRAIL_ATR_MULT,
    "trail_back_pct": TRAIL_BACK_PCT,
    "trail_activate_pct": TRAIL_ACTIVATE_PCT,
    "weak_filter": WEAK_FILTER,
    "weak_max_pos": WEAK_MAX_POS,
    "weak_index_code": WEAK_INDEX_CODE,
    "va_weight": VA_WEIGHT,
    "enable_va": ENABLE_VA,
    "rebalance": REBALANCE,
    "push_enabled": PUSH_ENABLED,
    "push_method": PUSH_METHOD,
    "server_chan_key": PUSH_SERVER_CHAN_KEY,
    "wechat_corp_id": PUSH_WECHAT_CORP_ID,
    "wechat_corp_secret": PUSH_WECHAT_CORP_SECRET,
    "wechat_agent_id": PUSH_WECHAT_AGENT_ID,
    "wechat_to_user": PUSH_WECHAT_TO_USER,
    "wxpusher_app_token": PUSH_WXPUSHER_APP_TOKEN,
    "wxpusher_topic_ids": PUSH_WXPUSHER_TOPIC_IDS,
    "wxpusher_uids": PUSH_WXPUSHER_UIDS,
}

# 强多头事件: 方向命中显著优于随机且可裸多落地 (见 docs/winrate_improve_eval.md §五)
# 采用完整强梯队 {Spring,Shakeout,UTAD,LPSY,ST,LPS,SC} (沿 config.STRONG_TIER_TYPES),
# 命中 76.8% vs 弱梯队 49.8% (差 27pt)。该强梯队为"必选"改进, 弱事件
# (AR/BC/SOS/JOC/PSY/SOW_INVALID 等) 不单独触发入场, 避免稀释组合质量。
# 注: UTAD/LPSY 命中 78.5%/78.3%, 高于旧含 SOW_INVALID 的口径, 一并纳入。
try:
    # 仅保留明确的多头事件（Spring, ST, LPS），
    # 移除 UTAD/LPSY (空头方向 event_dir=-1)、SC (中性)、Shakeout (胜率最差41.7%)
    # 预期：移除 Shakeout 可提升胜率 ~5个百分点
    LONG_EVENT_TYPES = frozenset({"Spring", "ST", "LPS"})
    # SC/SOW_INVALID 方向为中性 (event_dir==0), 但属底部反转/空头失效。
    # 其中 SC 命中 60.7% 在强梯队内, 保留; SOW_INVALID (71.1%) 非强梯队, 剔除.
except Exception:  # pragma: no cover - 防御首启缺失
    LONG_EVENT_TYPES = frozenset(
        {"Spring", "ST", "LPS"})


# ── 微信推送 (交易提醒) ─────────────────────────────────────
_STRATEGY_LABELS = {
    STRATEGY_DISCIPLINE: "纪律",
    STRATEGY_VALUE_ACC: "价值吸筹",
    STRATEGY_LONG_LEFT: "左侧买点",
}


def _reset_logs():
    """清空模拟盘当日全部日志 (paper_logs), 供账户重置时同步清理。"""
    try:
        paper_log.clear_today()
    except Exception:
        pass


def _scan_due(st):
    """距上次全市场扫描是否已过冷却窗口 (next_scan_time 文本即 isoformat 比较)。"""
    nxt = st.get("next_scan_time") or ""
    if not nxt:
        return True
    try:
        return datetime.now().isoformat() >= nxt
    except Exception:
        return True


PAPER_BUSY_MSG = ("另一实例 (界面或系统定时任务) 正在执行中, 本次已跳过")


def _acquire_paper_lock():
    """跨进程互斥锁 (Windows msvcrt / POSIX fcntl)。

    UI 内的自动周期线程与系统定时任务 (schtasks/cron) 会并发触发 run_scan /
    run_cycle, 两者争用同一份 wx_paper.json。这里保证同一时刻只允许一个执行者
    进入; 拿不到锁 (已被占用) 返回 None。进程崩溃/被杀时 OS 自动释放锁,
    不会残留卡死后续调度。
    """
    fh = None
    try:
        from ..paths import DATA_DIR as _DD
        lock_path = os.path.join(_DD, "wx_paper.lock")
        os.makedirs(_DD, exist_ok=True)
        fh = open(lock_path, "a+b")
        fh.seek(0)
        fh.truncate(1)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _release_paper_lock(fh):
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


def _paper_locked(fail=None):
    """装饰器: 只在实际拿到跨进程锁时才执行 fn; 锁被占用时返回 fail。"""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            fh = _acquire_paper_lock()
            if fh is None:
                return fail() if callable(fail) else fail
            try:
                return fn(*args, **kwargs)
            finally:
                _release_paper_lock(fh)
        return wrapper

    return deco


@_paper_locked(fail=None)
def run_cycle(settings=None, min_conf=None, universe=None, candidates=None,
              force_scan=False, strategies=None, progress=None, anytime=False):
    """无头自动运行一个周期: 筛选→下单→步进→统计。返回统计。

    供 cron / 调度线程 / 手动触发。每周期持仓 K 数 +1,
    到期/止盈/止损/破位在该周期内平仓。
    settings 传入界面设置 dict (S.Paper.* 键) 覆盖策略参数; min_conf 显式传入
    时优先于 settings (兼容旧调用方)。candidates 传入时直接复用 (跳过选股),
    供"扫描完成→自动买入"避免二次全市场扫描。
    candidates 为 None 时, 若距上次扫描仍在冷却窗口内且已有候选快照, 则复用
    st["candidates"] 而非无条件重扫全市场 (多周期调度每 30 分钟触发一次全市场
    重扫会造成选股覆盖与节流), 用 force_scan=True 强制立即重扫。
    strategies: 可选策略 key 子集 (与 run_scan 单策略模式同语义), 仅在需要
    重扫时透传给 pick_candidates; 复用候选/传入候选时过滤由调用方保证。
    progress: 可选扫描进度回调 (done, total, code), 仅在真正执行全市场重扫时
    上报; 复用候选/传入候选时不触发 (供 UI 区分"正在扫描"与"仅执行周期")。
    anytime: True 时绕开交易时段门禁 (供手动补跑/测试); 默认 False — 非交易
    时段执行时自动降级为"纯快照"模式: 只更新持仓最新价与净值, 不重扫、不生成
    入场条件单、不撮合任何买卖 (防止收盘后按收盘价"成交"的不合理交易)。
    """
    from ..datasource import fetch_kline
    from ..indicators import add_indicators

    apply_paper_params(settings)
    if min_conf is None:
        min_conf = _CUR["min_conf"]

    # 撮合门禁: 非交易时段 → 纯快照 (anytime 可强制放行, 供手动补跑有交易)。
    trading = anytime or gate_reason() is None

    st = load_state()
    # 弱市过滤: 指数未站上 MA20 → 降仓上限与停用价值吸筹 (与回测移动止盈口径一致)
    st["weak"] = _weak_market_flag()
    weak = st["weak"]
    # 1) 持仓防护回填: 对缺失止盈/止损保护条件单的已有持仓自动补齐
    #    (_create_position_conditions 幂等: 已有 active 不重复; 并消费同标的入场单)。
    #    覆盖历史遗留/非 fill_buy 路径建立的仓位, 避免"裸奔"只有 step 兜底。
    _backfill_position_protection(st)
    # 1) 选股: candidates 传入时直接复用扫描结果 (扫描已完成选股)
    if not trading:
        # 非交易时段: 只快照 — 复用现有候选, 不重扫/不更新条件单/不撮合。
        cand = st.get("candidates") or []
    elif candidates is not None:
        cand = candidates
    else:
        reuse = (not force_scan) and st.get("candidates") and not _scan_due(st)
        if reuse:
            cand = st["candidates"]
        else:
            # 与 run_scan 一致地计为一次全市场扫描 (scan_count + scan 日志),
            # 否则"周期内嵌选股"命中的买入在日志里扫描次数恒为 0, 误导核查。
            st["scan_count"] = st.get("scan_count", 0) + 1
            cand = pick_candidates(universe=universe, min_conf=min_conf,
                                   strategies=strategies, progress=progress)
            _now = datetime.now()
            st["last_scan_time"] = _now.isoformat()
            st["next_scan_time"] = (
                _now + timedelta(minutes=_SCAN_COOLDOWN_MIN)).isoformat()
            try:
                scanned = len(_mainboard_universe(universe))
                paper_log.log_scan(
                    scan_count=st["scan_count"], codes_scanned=scanned,
                    candidates_found=len(cand), candidates=cand)
            except Exception:
                pass
    st["candidates"] = cand
    # 自动买入条件单: 与候选一同在最终 save_state 落盘 (不被快照覆盖);
    # 非交易时段 (纯快照) 不更新, 避免收盘后挂出"触发价=收盘价*1.002"的条件单。
    if trading:
        _apply_auto_conditions(st, cand, weak=weak)
    # 2) 下单: 仓位未满时取候选填补 (同持上限内), 进 pending 待本周期撮合。
    #    三大硬门槛已由 pick_candidates 在候选入池时 fail-close 判定
    #    (大盘↑+板块>60分位+资金>50分位), 这里直接消费精筛后的候选。
    #    左侧买点特殊: 挂买点入场价, 价格未回踩到位 (last > entry_price) 时
    #    不直接成交, 交给下方 buy_price 条件单 (below) 等回踩。
    if trading:
        # 冻结交割时段模式: cand 在非交易时段被截留为空/复用, 不执行任何撮合。
        for e in cand:
            eff_max = _CUR["weak_max_pos"] if weak else _CUR["max_pos"]
            if len(st["positions"]) >= eff_max:
                break
            code = e["code"]
            if has_position(st, code) or any(o["symbol"] == code for o in st["pending"]):
                continue
            # 弱市停用价值吸筹 (与回测弱市过滤口径一致)
            if weak and e.get("strategy") == STRATEGY_VALUE_ACC:
                continue
            # 开关停用价值吸筹: 存量候选兜底拦截 (防止历史候选重放入场)
            if not _CUR.get("enable_va", True) \
                    and e.get("strategy") == STRATEGY_VALUE_ACC:
                continue
            px = float(e.get("entry_price") or 0) or float(e.get("last", 0) or 0)
            last = float(e.get("last", 0) or 0)
            if e.get("trigger") == "below":
                # 左侧买点: 现价高于买点入场价 → 等回踩 (条件单 below 触发)
                if last <= 0 or last > px:
                    continue
            if px <= 0:
                continue
            # 风控门禁 (此前为死代码, 现接入入场路径):
            #   回撤上限 / 单笔风险预算 / 行业集中度 / 单股集中度 / 资金利用率
            if _risk_blocks_entry(st, e, px):
                continue
            # 直接按候选现价撮合成交, 不再依赖 step 二次拉行情的待撮合;
            # 避免全市场大扫描后行情接口节流导致 pending 悬空、界面永不显示建仓。
            stop_pct = take_pct = None
            if e.get("strategy") == STRATEGY_LONG_LEFT and px > 0:
                stop_px = float(e.get("stop_price") or 0)
                target_px = float(e.get("target_price") or 0)
                if stop_px:
                    stop_pct = round((px - stop_px) / px, 4)
                if target_px > stop_px:
                    take_pct = round((target_px - px) / px, 4)
            order = _make_order(code, e.get("name", ""), e["type"],
                                e.get("conf", 50), px, 0, st["cash"],
                                sector=e.get("sector", ""),
                                strategy=e.get("strategy", ""), st=st,
                                stop_pct=stop_pct, take_pct=take_pct)
            if order is None:
                continue
            order["day"] = str(e.get("day") or "")
            order["reason"] = ("回踩买入" if e.get("trigger") == "below"
                               else "上破买入")
            _filled, _msg = fill_buy(st, order)
            if _filled is not None and e.get("trigger", "above") == "above":
                # 直接成交 = 上方 buy_price 自动条件单触发 (below 回踩单由 _check_conditions
                # 撮合并已在那边记录 condition 事件); 这里补一条, 让当日日志"买入/条件单"
                # 联动可查 (此前 run_cycle 内嵌选股直接成交只记 buy, 条件单计数恒为 0)。
                try:
                    paper_log.log_condition_fired(
                        code, e.get("name", ""), "buy_price",
                        e.get("auto_cond_price") or float(e.get("last", 0) or 0),
                        float(e.get("last", 0) or 0), action="买入",
                        reason=f"自动:{e.get('strategy', '')}")
                except Exception:
                    pass
    # 3) 步进+平仓判定 (持仓 + 待撮合用最新行情)
    df_by_code = {}
    codes = {p["symbol"] for p in st["positions"]}
    codes |= {o["symbol"] for o in st["pending"]}
    for code in codes:
        try:
            df_by_code[code] = add_indicators(
                fetch_kline(code, datalen=420, scale=240), symbol=code)
        except Exception:
            pass
    step(st, df_by_code, trading=trading)
    # 4) 周期级等权再平衡: 满仓且现金富余时, 把权重过低的持仓补足到等权目标,
    #    消除资金利用率不足(~66%)与单仓过度集中。
    if trading:
        _rebalance_portfolio(st, df_by_code)
    _day = ""
    for _df in df_by_code.values():
        try:
            _day = str(_df["day"].iloc[-1])
            break
        except Exception:
            continue
    _record_equity(st, _day or None)
    save_state(st)
    try:
        paper_log.log_account_snapshot(
            equity_value=equity(st, {}), cash=st["cash"],
            positions_count=len(st["positions"]),
            closed_count=len(st["closed"]))
    except Exception:
        pass
    return stats(st)


@_paper_locked(fail=PAPER_BUSY_MSG)
def run_scan(st, scan_type='', n_codes=6000, progress=None, anytime=False):
    """运行扫描并更新状态。

    三策略并线扫描 (纪律 + 威科夫左侧买点 + 价值吸筹): 强多头事件 (Spring/
    Shakeout/ST/LPS/SC) + conf≥阈值; 纪律/价值吸筹受大盘门禁, 左侧买点独立观。
    候选写入 st["candidates"] 并落盘 (save_state)。返回描述字符串。

    参数:
        st: 交易状态 dict
        scan_type: 策略过滤。""=三策略并线; 或一个策略 key
                   (STRATEGY_DISCIPLINE/STRATEGY_VALUE_ACC/STRATEGY_LONG_LEFT)
                   单策略模式只产出该策略候选 (旧 "volume_surge"/"pnf_breakout"/
                   "discipline" 等历史值按纪律并线兼容)。
        n_codes: 要扫描的代码数量上限 (pick_candidates 内部会把 universe 收敛为
                 沪深主板 600/601/603/605 + 000/001/002/003, 实际扫描量以主板为准)
        progress: 可选进度回调 (done, total, code), 透传给 pick_candidates
        anytime: True 绕开交易时段门禁 (供手动补跑/测试); 默认 False — 非交易
                 时段直接返回门禁提示, 不执行扫描 (扫描本身不撮合, 但需避免
                 收盘后挂出次日开盘即触发的入场条件单)。

    返回:
        扫描结果字符串描述
    """
    reason = gate_reason(anytime=anytime)
    if reason:
        return f"跳过扫描: {reason}"
    # 弱市过滤: 指数未站上 MA20 → 降仓上限与停用价值吸筹 (与 run_cycle 同口径)。
    # 扫描时点即判定一次, 供下方条件单生成与换手限仓共用 (避免弱市 VA 漏入条件单)。
    st["weak"] = _weak_market_flag()
    st['scan_count'] = st.get('scan_count', 0) + 1
    now = datetime.now().isoformat()
    st['last_scan_time'] = now

    # 策略过滤: 历史兼容值统一按纪律; 未知值视为并线
    _LEGACY_SINGLE = {"discipline", "volume_surge", "pnf_breakout", "sector_driven"}
    if scan_type in _LEGACY_SINGLE:
        strategies = (STRATEGY_DISCIPLINE,)
    elif scan_type in (STRATEGY_DISCIPLINE, STRATEGY_VALUE_ACC, STRATEGY_LONG_LEFT):
        strategies = (scan_type,)
    else:
        strategies = None
    # 纪律扫描: 强多头事件 + conf≥min_conf + 三大硬门禁
    try:
        scanned_universe = _mainboard_universe(n_codes)
    except Exception:
        scanned_universe = []
    scanned = len(scanned_universe)
    try:
        cand = pick_candidates(universe=scanned_universe or None,
                               max_codes=n_codes, min_conf=_CUR["min_conf"],
                               progress=progress, strategies=strategies)
    except Exception:
        cand = []
    cand.sort(key=lambda e: (-int(e.get("conf", 0) or 0), e.get("code", "")))
    st["candidates"] = cand
    try:
        paper_log.log_scan(
            scan_count=st['scan_count'], codes_scanned=scanned,
            candidates_found=len(cand), candidates=cand)
    except Exception:
        pass

    if scan_type in (STRATEGY_DISCIPLINE, STRATEGY_VALUE_ACC, STRATEGY_LONG_LEFT):
        label = f"策略管理器扫描({paper_strategy_accuracy.STRATEGY_CN.get(scan_type, scan_type)})"
        empty_note = (f"{label}: 无满足条件的候选 (该策略的门禁/conf/事件未满足)")
    else:
        label = "策略管理器扫描(纪律+左侧买点+价值吸筹)"
        empty_note = ("策略管理器扫描: 无满足条件的候选 (纪律强多头事件/conf/大盘/板块/资金流门禁拦截, "
                      "左侧买点未现于可执行窗口, 价值吸筹未现于底部整固)")
    result_str = (f"{label}: 扫描{scanned} 码, 命中 {len(cand)} 个候选"
                  if cand else empty_note)
    st['last_scan_result'] = result_str
    st['next_scan_time'] = (datetime.now() + timedelta(minutes=30)).isoformat()

    # 落盘: 合并到扫描期间可能被并发修改的最新状态 (保留持仓/待撮合/条件单),
    # 并统一生成 buy_price 条件单 (由 _probe 带回 auto_cond_price), 避免快照覆盖。
    with _LOCK:
        fresh = load_state()
        fresh['scan_count'] = st['scan_count']
        fresh['last_scan_time'] = now
        fresh['next_scan_time'] = st['next_scan_time']
        fresh['candidates'] = cand
        fresh['last_scan_result'] = result_str
        fresh['weak'] = bool(st.get('weak', False))
        _apply_auto_conditions(fresh, cand, weak=fresh['weak'])
        try:
            save_state(fresh)
        except Exception:
            pass

    return result_str