"""校准数据变更通知: 落盘时置脏, GUI 调度器按去抖窗口触发自动同步。

纯 Python / 线程安全 / 不依赖 Qt 与 wyckoff:
  - wyckoff.signal_accuracy.save_signals / wyckoff.storage.save_feedback 每次
    写库时调用 notify_change(), 标记"本地有新数据待同步";
  - desktop 主窗口的调度器轮询 pending()/last_change_ts(), 满足去抖窗口后
    在后台线程执行 sync.service.sync(), 完成后 reset()。

不做账号/排队/重试: 只负责"有没有新数据"这一件事, 其余交给 service 层。
"""
import threading
import time

_LOCK = threading.Lock()
_DIRTY = False
_LAST_CHANGE = 0.0


def notify_change(kind="signals"):
    """校准数据有变更 (信号/反馈落盘时调用)。线程安全, 可被任意线程调用。"""
    global _DIRTY, _LAST_CHANGE
    with _LOCK:
        _DIRTY = True
        _LAST_CHANGE = time.time()


def pending():
    """是否有待同步的本地变更。"""
    with _LOCK:
        return _DIRTY


def last_change_ts():
    """最近一次变更时间戳 (epoch 秒), 供去抖窗口计算。"""
    with _LOCK:
        return _LAST_CHANGE


def reset():
    """同步完成后清空待同步标记。"""
    global _DIRTY
    with _LOCK:
        _DIRTY = False
