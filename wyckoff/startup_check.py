"""启动体检: 汇总数据源/模型/校准/自选股/拼音索引健康状态。

纯逻辑、无 GUI 依赖、无网络请求 (只用磁盘文件与进程内已聚合的健康度),
供状态栏阶段进度与体检汇总复用, 也便于独立回归测试。
"""
from __future__ import annotations

import os
import time

from .calibration import _load
from .datasource import source_health
from .online_model import ONLINE_MODEL_FILE, model_status
from .paths import ALL_STOCKS_FILE


def _age(path: str) -> float | None:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def check_data_source() -> dict:
    """数据源体检: 进程内健康度快照; 尚无请求记录时返回 warn。"""
    h = source_health()
    if not h:
        return {"ok": False, "level": "warn", "msg": "尚无请求记录 (首次分析后生效)",
                "detail": {}}
    total = sum(v["ok"] + (v["fail"] or 0)
                for v in h.values() if isinstance(v, dict))
    fails = sum((v["fail"] or 0) for v in h.values() if isinstance(v, dict))
    ok_srcs = ", ".join(sorted(k for k, v in h.items()
                               if isinstance(v, dict) and (v["fail"] or 0) == 0)) or "无"
    return {
        "ok": fails == 0 and total > 0,
        "level": "ok" if fails == 0 else "warn",
        "msg": f"{total} 次请求 / {fails} 失败 (源: {ok_srcs})",
        "detail": h,
    }


def check_model() -> dict:
    """模型体检: 在线模型文件存在且状态 ready。"""
    st = model_status()
    if not st:
        return {"ok": False, "level": "warn",
                "msg": "未训练 (跑一次校准/重训后生效)", "detail": {}}
    age = _age(ONLINE_MODEL_FILE)
    ready = bool(st.get("ready"))
    return {
        "ok": ready,
        "level": "ok" if ready else "warn",
        "msg": ("就绪" if ready else "未就绪")
               + f" · 样本 {st.get('n_train', 0)} · AUC {st.get('auc_oos')}"
               + (f" · {age / 86400:.1f} 天前更新" if age is not None else ""),
        "detail": st,
    }


def check_calibration() -> dict:
    """校准体检: 校准基线文件存在。"""
    d = _load()
    base = d.get("baseline") or {}
    n = int(base.get("acc_evaluated", 0) or 0) + int(base.get("signal_evaluated", 0) or 0)
    if not d.get("calibrated_at"):
        return {"ok": False, "level": "warn",
                "msg": "未设校准基线 (校准中心 → 记录基线)", "detail": d}
    return {"ok": True, "level": "ok",
            "msg": f"基线样本 {n} 已记录", "detail": d}


def check_watchlist(n_watch: int, rt_filled: bool, scan_done: bool) -> dict:
    """自选股体检: 数量 + 首扫是否已拿到行情。"""
    if n_watch <= 0:
        return {"ok": True, "level": "ok", "msg": "无自选股 (无需扫描)",
                "detail": {"n": 0}}
    if scan_done and rt_filled:
        return {"ok": True, "level": "ok", "msg": f"{n_watch} 只, 首扫已拿到行情",
                "detail": {"n": n_watch, "rt_filled": True}}
    if scan_done:
        return {"ok": False, "level": "warn",
                "msg": f"{n_watch} 只, 首扫进行中/未拿到行情",
                "detail": {"n": n_watch, "rt_filled": False}}
    return {"ok": False, "level": "warn",
            "msg": f"{n_watch} 只, 等待首扫定时器",
            "detail": {"n": n_watch, "rt_filled": False}}


def check_pinyin() -> dict:
    """拼音索引体检: 全市场索引文件存在且非空。"""
    try:
        size = os.path.getsize(ALL_STOCKS_FILE)
    except OSError:
        return {"ok": False, "level": "warn",
                "msg": "索引未就绪 (后台构建中/首次启动)", "detail": {}}
    if size <= 4:
        return {"ok": False, "level": "warn", "msg": "索引为空 (后台构建中)",
                "detail": {"bytes": size}}
    return {"ok": True, "level": "ok", "msg": f"索引 {size // 1024} KB 已就绪",
            "detail": {"bytes": size}}


# ── 汇总 ──
def run_startup_check(n_watch: int = 0, rt_filled: bool = False,
                      scan_done: bool = False) -> dict:
    """执行全部体检项 (无网络、无阻塞), 返回 {items, ok_all, summary}。"""
    items = {
        "数据源": check_data_source(),
        "模型": check_model(),
        "校准": check_calibration(),
        "自选股": check_watchlist(n_watch, rt_filled, scan_done),
        "拼音索引": check_pinyin(),
    }
    bad = [k for k, v in items.items() if not v["ok"]]
    return {
        "items": items,
        "ok_all": not bad,
        "summary": "全部通过" if not bad else "待完善: " + " / ".join(bad),
    }


def format_check_summary(report: dict) -> str:
    """一行摘要 (状态栏用): ✓/! 标记。"""
    if not report:
        return ""
    marks = {k: ("✓" if v["ok"] else "!") for k, v in report["items"].items()}
    return report["summary"] + " [" + " ".join(f"{k}{m}" for k, m in marks.items()) + "]"
