# -*- coding: utf-8 -*-
"""校准基线管理: 记录"上次校准"时各数据集的已评估样本量, 与当前对比。

超过提醒阈值 (新增评估样本量) 即提示重新校准阈值, 避免用陈旧的检测/打分
阈值继续做新判断。校准动作本身不修改任何规则, 只记录基线供提醒使用。

存储: ~/.wyckoff/wx_calibration.json
"""
import json
import os
import time

from ._shared import atomic_write_json
from .paths import DATA_DIR

CALIB_FILE = os.path.join(DATA_DIR, "wx_calibration.json")

# 提醒阈值: 距上次校准新增的已评估样本量超过即提醒
ACC_EVAL_THRESHOLD = 30        # 分析准确度 (整份分析)
SIGNAL_EVAL_THRESHOLD = 200    # 信号准确度 (逐信号)


def _load():
    try:
        with open(CALIB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        atomic_write_json(CALIB_FILE, d)
    except Exception:
        pass


def record_calibration(acc_evaluated, signal_evaluated, feedback_count):
    """记录当前样本量为校准基线。返回新基线 dict。"""
    d = {
        "calibrated_at": time.time(),
        "baseline": {
            "acc_evaluated": int(acc_evaluated or 0),
            "signal_evaluated": int(signal_evaluated or 0),
            "feedback": int(feedback_count or 0),
        },
    }
    _save(d)
    return d


def calibration_status(acc_evaluated, signal_evaluated, feedback_count):
    """返回 (due, msg): due=True 表示建议重新校准; msg 为一行说明。"""
    d = _load()
    base = d.get("baseline") or {}
    b_acc = int(base.get("acc_evaluated", 0))
    b_sig = int(base.get("signal_evaluated", 0))
    acc_delta = max(0, int(acc_evaluated or 0) - b_acc)
    sig_delta = max(0, int(signal_evaluated or 0) - b_sig)
    if not d.get("calibrated_at"):
        return True, ("尚未设置校准基线 — 点「校准基线」记录当前样本, 之后每次分析/"
                      "扫描都会自动累积评估并在此提醒")
    if acc_delta >= ACC_EVAL_THRESHOLD or sig_delta >= SIGNAL_EVAL_THRESHOLD:
        return True, (f"距上次校准已新增 分析{acc_delta}/{ACC_EVAL_THRESHOLD} · "
                      f"信号{sig_delta}/{SIGNAL_EVAL_THRESHOLD} 条评估样本 — 建议重新校准阈值")
    return False, (f"距上次校准新增 分析{acc_delta} / 信号{sig_delta} 条评估样本, "
                   f"阈值仍有效 (基线 分析{b_acc} / 信号{b_sig})")
