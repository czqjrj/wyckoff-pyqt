"""模拟盘选股层: 弱市过滤 / 策略候选 / 全市场扫描 (纪律硬门禁引用)。"""

import os

import wyckoff.paper as paper

from ..strategies.constants import (
    STRATEGY_DISCIPLINE,
    STRATEGY_LONG_LEFT,
    STRATEGY_VALUE_ACC,
)



# ── 选股: 全市场自动筛选并自动生成条件单 ─────────────────────
# ── 三重共振纪律硬门禁 (统一数据源见 discipline.py) ──
# paper 是实际撮合的交关口; 门禁阈值与实现收敛到 discipline.py 单一源。
# 此处保留模块级名字 (供本模块内部与测试 monkeypatch 引用), 语义完全一致。
from ..discipline import (  # noqa: E402
    flow_net5 as _flow_net5,
)
from ..discipline import (
    market_trend_ok as _market_trend_ok,
)
from ..discipline import (
    sector_strength_ok as _sector_strength_ok,
)


# ── 弱市过滤 (改进: 指数未站上MA20 → 降仓 + 停用价值吸筹) ──
def _weak_market_flag():
    """按 paper._CUR 弱市过滤设置评估当前市场强弱, 返回是否弱市。
    弱市过滤关闭或数据异常 → 视为不强 (不受限)。调用方负责把结果落 st["weak"]。"""
    if not paper._CUR.get("weak_filter"):
        return False
    try:
        ok, _reason = paper._market_trend_ok()
        return not ok
    except Exception:
        return False


def _strategy_manager():
    """策略管理器单例 (延迟实例化, 数据目录落在 DATA_DIR 下避免污染运行目录)。

    供模拟盘候选生成复用: 策略4 (模拟盘纪律, conf≥90) 与 综合选股·价值吸筹
    (底部整固 + 20根内吸筹事件)。导入失败或数据不可用时返回 None (纯离线降级)。
    """
    if paper._SMGR is None:
        try:
            from wyckoff_strategies_manager import WyckoffStrategyManager

            from ..paths import DATA_DIR as _DD
            paper._SMGR = WyckoffStrategyManager(
                data_dir=os.path.join(_DD, "strategy_manager_data"))
        except Exception:
            paper._SMGR = False
    return paper._SMGR if paper._SMGR else None


def _value_accum_candidate(code, df, piv, evs):
    """(兼容垫片) 基于策略管理器「价值吸筹」信号构造候选。

    正式扫描统一走 manager.scan_individual; 此处保留旧逻辑供单测/外部引用。
    命中条件 (与回测口径一致): 阶段=底部整固 且 近20根内出现
    {Spring,Shakeout,SC,ST,LPS} 事件, conf≥VA_MIN_CONF 并通过低质过滤。
    管理器不可用 / 评估异常时返回 None。
    """
    m = paper._strategy_manager()
    if m is None:
        return None
    try:
        sig = m.evaluate_strategy_value_accumulation(df, len(df) - 1, evs, piv)
    except Exception:
        return None
    if not sig:
        return None
    ev = sig["event"]
    if int(ev.get("conf", 0) or 0) < VA_MIN_CONF:
        return None
    if _is_low_quality(sig["event"].get("code") or code):
        return None
    return {"strategy": STRATEGY_VALUE_ACC,
            "type": ev["type"], "idx": int(ev.get("idx") or 0),
            "conf": int(ev.get("conf", 0) or 0)}


# 兼容别名 (策略常量已随选股逻辑迁移入策略管理器; 保留引用供单测/外部导入)
VA_EXCLUDE_BJ = True
VA_EXCLUDE_ST = True
VA_MIN_PRICE = 3.0
VA_MIN_CONF = 80
LONG_STRATEGY = STRATEGY_LONG_LEFT
LONG_MIN_CONF = 60
LB_ENTRY_MARGIN = 0.0
LB_MAX = 8
try:
    from ..strategies.manager import (  # noqa: E402 仅取值
        LONG_MIN_CONF as _M_LONG_MIN_CONF,
    )
    from ..strategies.manager import (
        VA_MIN_CONF as _M_VA_MIN_CONF,
    )
    from ..strategies.manager import (
        VA_MIN_PRICE as _M_VA_MIN_PRICE,
    )
    LONG_MIN_CONF, VA_MIN_PRICE, VA_MIN_CONF = (
        _M_LONG_MIN_CONF, _M_VA_MIN_PRICE, _M_VA_MIN_CONF)
except Exception:
    pass


def _long_buy_candidate(code, df, piv, evs):
    """(兼容垫片) 委托策略管理器·左侧买点候选生成。

    等价于 manager._left_buy_candidate, 保留仅供既有脚本/回测引用。
    """
    m = paper._strategy_manager()
    if m is None:
        return None
    try:
        res = m._left_buy_candidate(code, df, evs, piv, name=_stock_name(code))
    except Exception:
        return None
    return dict(res) if res else None


def _is_low_quality(code, price=None, name=None) -> bool:
    """(兼容垫片) 低质池过滤 (北交所/ST·退市/低价), 优先委托策略管理器。"""
    if name is None:
        try:
            name = _stock_name(code)
        except Exception:
            name = ""
    m = paper._strategy_manager()
    if m is not None and hasattr(m, "_is_low_quality"):
        try:
            return m._is_low_quality(code, price=price, name=name)
        except Exception:
            pass
    c = str(code).lower()
    if VA_EXCLUDE_BJ and c.startswith("bj"):
        return True
    if VA_EXCLUDE_ST:
        nm = name or ""
        if any(x in nm for x in ("ST", "退", "N ", "C ")):
            return True
    if price is not None and VA_MIN_PRICE > 0 and float(price) < VA_MIN_PRICE:
        return True
    return False


def _probe_workers():
    """自动选择扫描并行度: 利用机内逻辑核, 上限 12 (网络 I/O 也受连接池/带宽约束)。

    numpy/pandas 指标计算在执行时会释放 GIL, 多线程在 8 核 (兆芯/Intel/AMD)
    上可真实并行; 网络等待更是天然并发。旧实现死锁在 6 是低估了整机算力。
    """
    try:
        import os
        n = int(os.cpu_count() or 4)
    except Exception:
        n = 4
    return max(2, min(n, 12))


def _mainboard_universe(max_codes=6000):
    """解析全市场 universe (本地全A名单, 缺失降级东财Top-100) 并收敛到沪深主板。

    与 pick_candidates 的门槛逻辑同口径 (沪 600/601/603/605 + 深 000/001/002/003),
    供 run_scan 统计"实际扫描代码数"而非全程 6000。
    """
    from ..utils import normalize_symbol
    try:
        from ..fundamental import local_universe
        universe = local_universe(max_codes)
    except Exception:
        universe = []
    if not universe:
        try:
            from ..fundamental import universe as market_universe
            universe = market_universe(100)[0] or []
        except Exception:
            universe = []
    universe = [normalize_symbol(c) for c in universe]
    from ..fundamental import is_main_board
    return [c for c in universe if is_main_board(c)]


def pick_candidates(universe=None, max_codes=6000, min_conf=None,
                    cancel_event=None, progress=None, skip_gates=False,
                    strategies=None):
    """扫描 universe 中触发强多头事件的高 conf 标的, 返回候选单 (降序 conf)。

    选股统一由策略管理器 (WyckoffStrategyManager.scan_individual) 产出,
    本函数只负责编排: 拉K线 → 管理器选股 → 门禁 → 低质过滤 → 排序。
    单只股票优先级: 纪律 (受门禁) → 威科夫左侧买点 (独立赛道, 不受门禁)
                    → 价值吸筹 (受门禁, 纪律兜底)。

    strategies: 可选策略 key 子集 (如仅左侧买点); None/空=三策略并线。
    单策略扫描时直接把子集传给 scan_individual, 避免 STRATEGY_ORDER 优序
    掩盖低优先级策略候选。

    纪律硬门禁 (缺一不可, 数据不可用即拦截; skip_gates=True 跳过全部门禁,
    供离线/测试/仅排序场景使用):
      - 大盘20日线向上 (fetch_market_env)
      - 板块强度 > 60 分位 (sector_strength_pct)
      - 资金流近5日主力净流入 > 候选池截面中位 (跨市场截面分位, fetch_main_flow)
    左侧买点不经过上述门禁 (左侧买点诞生于大盘弱市, 属独立赛道)。

    遇到候选时自动添加价格买入条件单 (buy_price):
      - 纪律/价值吸筹: 触发价=最近收盘价×1.002, 条件 "≥ 达上破" (above)。
      - 左侧买点: 触发价=买点入场价, 条件 "回踩买入" (below)。
    """
    if min_conf is None:
        min_conf = paper._CUR["min_conf"]
    # 价值吸筹停用时: 统一在选股源头剔除该策略 (不产生候选/条件单)。
    if not paper._CUR.get("enable_va", True):
        if strategies is None:
            strategies = (STRATEGY_DISCIPLINE, STRATEGY_LONG_LEFT)
        else:
            strategies = tuple(s for s in strategies if s != STRATEGY_VALUE_ACC)
        if not strategies:
            # 显式仅指定价值吸筹 (如 UI 模式扫描) → 直接返回空, 避免回退全策略
            return []
    from ..datasource import fetch_kline
    from ..fundamental import fetch_sector
    from ..indicators import add_indicators
    from ..utils import normalize_symbol

    if universe is None:
        # 全A 市场扫描: 本地全A 名单 (去 ST/退市/新股, ~5900 只) 逐股扫描;
        # 名单缺失时降级东财成交额 Top-100 / 本地抽样兜底。
        universe = _mainboard_universe(max_codes)
    else:
        # 调用方显式传入: 同样归一化 + 主板收敛 (非主板代码忽略)
        universe = [normalize_symbol(c) for c in universe]
        from ..fundamental import is_main_board
        universe = [c for c in universe if is_main_board(c)]
    # 纪律门禁 ①: 大盘20日线向上 (全市场统一, 一次判定; fail-close)。
    # 不再整池短路: 左侧买点属独立赛道, 大盘弱市也可出候选; 纪律/价值吸筹
    # 在 scan_individual 按 market_ok 拦截。
    market_ok = True
    _market_reason = ""
    if not skip_gates:
        market_ok, _market_reason = paper._market_trend_ok()

    def _probe(code):
        """单只股票的重活: K线 → 策略管理器选股 → 门禁/低质/触发价。
        Gate ③ (资金流截面分位) 是跨市场判定, 故 flow 先随候选带回, 由调用方统一过滤。
        返回 (code, candidate_or_None, flow_or_None)。"""
        if cancel_event is not None and cancel_event.is_set():
            return code, None, None
        flow = None
        try:
            df = add_indicators(fetch_kline(code, datalen=400, scale=240),
                                symbol=code)
            if df is None or len(df) < 200:
                return code, None, None
            name = _stock_name(code)
            m = paper._strategy_manager()
            if m is None:
                return code, None, None
            cand = m.scan_individual(
                code, df, min_conf=min_conf,
                gates_ok=(market_ok, _market_reason), name=name,
                event_types=paper.LONG_EVENT_TYPES, strategies=strategies)
            if cand is None:
                return code, None, None
            cand["code"] = code
            cand["name"] = name
            cand["last"] = round(float(df["close"].iloc[-1]), 2)
            cand["day"] = str(df["day"].iloc[-1])
            # 低质池过滤 (北交所/ST/低价) — 各策略统一适用, 防垃圾入池
            if _is_low_quality(code, price=cand["last"], name=name):
                return code, None, None
            cand["sector"] = ""
            try:
                cand["sector"] = fetch_sector(code) or ""
            except Exception:
                pass
            if cand.get("gated"):
                # 纪律/价值吸筹 (受门禁): 板块强度 + 资金流 + 产业链 conf 调整
                if not skip_gates:
                    s_ok, _s_reason = paper._sector_strength_ok(cand["sector"])
                    if not s_ok:
                        return code, None, None
                    # 纪律门禁 ③: 收集主力净流入供截面分位判定 (稍后统一过滤)
                    flow = paper._flow_net5(code)
                else:
                    flow = None
                # A: 产业链加分 — 不改变入池过滤, 只影响排序优先级;
                #    数据不可用/无板块 (离线) 时 adj=0 完全退化为原行为。
                base_conf = int(cand.get("conf", 0) or 0)
                try:
                    if cand["sector"]:
                        from ..chain import chain_conf_adjust
                        adj = chain_conf_adjust(cand["sector"], base_conf)
                        cand["base_conf"] = base_conf
                        cand["conf"] = max(min_conf, min(100, base_conf + adj))
                        cand["chain_adj"] = adj
                except Exception:
                    pass
                # 自动买入条件单: 触发价=现价+0.2% 上破 (above)。
                cand["auto_cond_price"] = round(float(cand["last"]) * 1.002, 3)
                cand["trigger"] = "above"
            else:
                # 独立赛道左侧买点: 直接挂买点入场价, 回踩触发 (below)。
                cand["auto_cond_price"] = round(float(cand["entry_price"]), 3)
                cand["trigger"] = "below"
            # 价值吸筹已停用: 候选层面兜底剔除 (管理器可能绕过 strategies 过滤,
            # 兜底保证 任何路径都不产出 VA 候选/条件单)。
            if not paper._CUR.get("enable_va", True) \
                    and cand.get("strategy") == STRATEGY_VALUE_ACC:
                return code, None, None
            return code, cand, flow
        except Exception:
            return code, None, None

    out = []
    _flow_map = {}  # code -> 近5日主力净流入
    # 主板块收敛已在上方统一完成 (沪 600/601/603/605 + 深 000/001/002/003),
    # 创业板/科创板/北交所等不再进入并行扫描 (_codes 无需再按权限过滤)。
    _codes = universe[:max_codes]
    _total = len(_codes)
    try:
        from .._shared import parallel_map
        for code, cand, flow in parallel_map(_codes, _probe, workers=_probe_workers(),
                                             progress=progress):
            if cand is not None:
                out.append(cand)
                _flow_map[code] = flow
    except Exception:
        # 串行兜底: 同样上报进度, 保证进度条口径一致
        for i, code in enumerate(_codes):
            if progress is not None:
                try:
                    progress(i + 1, _total, code)
                except Exception:
                    pass
            _code, cand, flow = _probe(code)
            if cand is not None:
                out.append(cand)
                _flow_map[_code] = flow
    # 纪律门禁 ③: 资金流"净流入>50 分位" —— 跨市场截面判定。
    # 只对拿到净流入数据的候选计算中位; 有数据但低于中位 → 拦截。
    # 若全部候选都拿不到资金流数据 (接口被拒/离线/新股无数据) → 数据缺失不代表
    # "资金弱", 降级跳过该门禁 (保留 ①大盘 ②板块 fail-close);
    # 部分有部分无数据时, 无数据者仍按不达标拦截。
    # 独立赛道左侧买点 (gated=False) 豁免: 不受资金流门禁管束。
    if not skip_gates:
        flows = [f for f in _flow_map.values() if f is not None]
        if flows:
            med = sorted(flows)[len(flows) // 2]
            out = [e for e in out
                   if not e.get("gated")
                   or (_flow_map.get(e["code"]) is not None
                       and _flow_map[e["code"]] >= med)]
        else:
            # 完全无资金流数据 → 门禁降级跳过 (见注释)
            pass
    out.sort(key=lambda e: -(int(e.get("conf", 0) or 0)))
    return out


def _stock_name(code):
    try:
        from ..screener import _get_stock_name
        return _get_stock_name(str(code)[-6:])
    except Exception:
        return ""


