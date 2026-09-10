"""模拟盘状态层: 文件落地 / 加载 / 默认新账户结构.

save_state 会被测试 monkeypatch (paper.save_state), 内核调用方统一经
`paper.save_state` 解析; 本模块内 load_state 调用的 _new_state 直接用。
可变配置 paper._CUR 随时可能被 apply_paper_params 重建, 一律经 paper._CUR 读取。
"""

import json
import os

import wyckoff.paper as paper

from ..paths import PAPER_FILE


def file_path() -> str:
    return PAPER_FILE


def load_state():
    """读取模拟盘状态; 文件不存在或损坏 → 全新默认账户。"""
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
    except Exception:
        pass
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
    """原子写盘。"""
    try:
        os.makedirs(os.path.dirname(PAPER_FILE), exist_ok=True)
        tmp = PAPER_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, PAPER_FILE)
        return True
    except Exception:
        return False

