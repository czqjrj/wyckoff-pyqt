"""自选股预警: 价格阈值 + 信号触发提醒。

规则存储: ~/.wyckoff/wx_alerts.json
  [{code, name, kind: "price_up"/"price_down"/"signal", target(价或信号类型),
    enabled, created_ts, triggered_ts, note}]

触发逻辑:
  - check_price_alerts(rt): 实时行情 {code: {price, pct}} 与价格阈值比较,
    越界 → 标记触发 (一次性, 触发后该规则停用, 用户可重新启用)。
  - check_signal_alerts(signals): 检测到的新信号类型与"signal"规则比较。
  - 触发返回 [(code, name, text)] 供界面弹窗 + TTS。
"""
import json
import os
import threading
import time

from ._shared import atomic_write_json
from .paths import DATA_DIR

ALERTS_FILE = os.path.join(DATA_DIR, "wx_alerts.json")

_LOCK = threading.Lock()


def load_alerts():
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_alerts(records):
    try:
        atomic_write_json(ALERTS_FILE, records)
    except Exception:
        pass


def add_alert(code, kind, target, name="", note=""):
    """添加预警规则。同 code+kind+target 去重, 返回是否新增。"""
    kind = kind if kind in ("price_up", "price_down", "signal") else "price_up"
    with _LOCK:
        records = load_alerts()
        for r in records:
            if r.get("code") == code and r.get("kind") == kind \
                    and str(r.get("target")) == str(target):
                return False
        records.append({
            "code": code, "name": name or "", "kind": kind,
            "target": target, "note": note or "", "enabled": True,
            "created_ts": time.time(), "triggered_ts": None,
        })
        save_alerts(records)
        return True


def remove_alert(code, kind, target):
    with _LOCK:
        records = [r for r in load_alerts()
                   if not (r.get("code") == code and r.get("kind") == kind
                           and str(r.get("target")) == str(target))]
        save_alerts(records)
        return len(records)


def enable_alert(code, kind, target, enabled):
    with _LOCK:
        records = load_alerts()
        for r in records:
            if r.get("code") == code and r.get("kind") == kind \
                    and str(r.get("target")) == str(target):
                r["enabled"] = bool(enabled)
                if enabled:
                    r["triggered_ts"] = None
        save_alerts(records)


def _mark_triggered(code, kind, target):
    with _LOCK:
        records = load_alerts()
        for r in records:
            if r.get("code") == code and r.get("kind") == kind \
                    and str(r.get("target")) == str(target):
                r["triggered_ts"] = time.time()
                r["enabled"] = False  # 一次性, 触发后停用
        save_alerts(records)


def check_price_alerts(rt):
    """检查实时行情价格阈值, 返回触发列表 [(code, name, text)]。

    rt: {code: {name, price, pct}} (code 为 6 位数字, 与规则一致)。
    """
    out = []
    if not rt:
        return out
    rules = load_alerts()
    for r in rules:
        if not r.get("enabled"):
            continue
        if r.get("kind") not in ("price_up", "price_down"):
            continue
        code = r.get("code")
        info = rt.get(code)
        if not info or not info.get("price"):
            continue
        try:
            price = float(info["price"])
            target = float(r["target"])
        except (TypeError, ValueError):
            continue
        hit = price >= target if r["kind"] == "price_up" else price <= target
        if hit:
            name = r.get("name") or info.get("name") or code
            verb = "突破" if r["kind"] == "price_up" else "跌破"
            out.append((code, name,
                        f"预警: {name}({code}) {verb} {target:.2f}, 现价 {price:.2f}"))
            _mark_triggered(code, r["kind"], target)
    return out


def check_signal_alerts(signal_types_by_code):
    """检查新信号类型, 返回触发列表 [(code, name, text)]。

    signal_types_by_code: {code: [signal_type, ...]} 本次检测到的信号。
    """
    out = []
    if not signal_types_by_code:
        return out
    rules = load_alerts()
    for r in rules:
        if not r.get("enabled"):
            continue
        if r.get("kind") != "signal":
            continue
        code = r.get("code")
        want = str(r.get("target", ""))
        got = signal_types_by_code.get(code) or []
        if want in got:
            name = r.get("name") or code
            out.append((code, name,
                        f"信号预警: {name}({code}) 出现 {want} 信号, 注意阶段转换"))
            _mark_triggered(code, "signal", want)
    return out


def clear_all():
    with _LOCK:
        save_alerts([])
