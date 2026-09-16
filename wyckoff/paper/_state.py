"""模拟盘状态层: 文件落地 / 加载 / 默认新账户结构.

save_state 会被测试 monkeypatch (paper.save_state), 内核调用方统一经
`paper.save_state` 解析; 本模块内 load_state 调用的 _new_state 直接用。
可变配置 paper._CUR 随时可能被 apply_paper_params 重建, 一律经 paper._CUR 读取。
"""

import json
import logging
import os
import time

import wyckoff.paper as paper

from ..paths import PAPER_FILE

logger = logging.getLogger(__name__)


def _backup_corrupt_file() -> str | None:
    """把损坏的账户文件改名留存, 返回备份路径 (文件不存在时返回 None)。"""
    if not os.path.exists(PAPER_FILE):
        return None
    bak = f"{PAPER_FILE}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(PAPER_FILE, bak)
        return bak
    except Exception:
        logger.exception("账户损坏文件备份失败: %s", PAPER_FILE)
        return None


def load_state():
    """读取模拟盘状态; 文件不存在或损坏 → 全新默认账户。

    文件存在但解析失败视为损坏: 备份原文件 (.corrupt-<ts>) + 记 error 日志,
    并在返回的新账户 meta 里打 `rebuilt_after_corruption` 标记, 供 UI 提示。
    """
    try:
        with open(PAPER_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            st.setdefault("cash", float(paper._CUR["init_cash"]))
            st.setdefault("positions", [])   # 持仓: {symbol, name, qty, cost,
            st.setdefault("orders", [])      #       entry_ts, entry_bars, type, conf}
            st.setdefault("closed", [])      # 已平仓: {symbol, ..., buy_px, sell_px,
            st.setdefault("equity_hist", []) #        qty, ret, reason, type, close_ts}
            st.setdefault("candidates", [])  # 最新候选快照
            st.setdefault("pending", [])     # 等待下一根开盘买入的委托
            st.setdefault("conditions", [])  # 条件单: 价格触发/止盈止损/追踪止损
            st.setdefault("meta", {})
            # equity_hist 日期归一化 (兼容旧数据混用 "YYYY-MM-DD" 与 datetime 串),
            # 同日期只保留当天最后一条, 保证净值曲线/回撤按交易日对齐。
            _hist = st.get("equity_hist") or []
            if _hist:
                _pool = []
                _idx = {}
                for h in _hist:
                    d = str(h.get("ts", ""))[:10]
                    h["ts"] = d
                    if d in _idx:
                        _pool[_idx[d]] = h
                    else:
                        _idx[d] = len(_pool)
                        _pool.append(h)
                st["equity_hist"] = _pool
            return st
    except FileNotFoundError:
        return _new_state()
    except Exception as e:
        logger.error("模拟盘账户文件解析失败, 已备份并重建: %s", e)
        bak = _backup_corrupt_file()
        logger.warning("损坏账户已备份至: %s", bak)
        st = _new_state()
        st.setdefault("meta", {})["rebuilt_after_corruption"] = True
        return st
    return _new_state()


def _new_state():
    return {
        "cash": float(paper._CUR["init_cash"]),
        "positions": [],
        "orders": [],
        "closed": [],
        "equity_hist": [],
        "candidates": [],
        "pending": [],
        "conditions": [],
        "advanced_orders": [],  # 高级订单
        "risk_metrics": {},     # 风险指标缓存
        "meta": {},
        "scan_count": 0,        # 今日扫描次数
        "last_scan_time": "",   # 上次扫描时间
        "next_scan_time": "",   # 下次扫描时间
        "last_scan_result": "", # 最后扫描结果
    }


def save_state(st):
    """原子写盘。失败返回 False 并记 error 日志 (调用方需检查返回值)。"""
    try:
        os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
        tmp = PAPER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, PAPER_FILE)
        return True
    except Exception as e:
        logger.error("模拟盘落盘失败: %s", e)
        return False

