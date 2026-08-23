"""轻量日志工具: 把静默吞掉的异常写入用户数据目录的日志文件, 便于排查。

设计: 只做"防静默吞错"这一件事 —— 在 `except Exception: pass` 处改用
`except Exception as e: log_exc("context", e)`, 不引入第三方依赖。
写文件失败时绝不抛异常 (日志系统本身不破坏主流程)。
"""
import os
import threading
import traceback
from datetime import datetime

from .paths import DATA_DIR

_LOG_FILE = os.path.join(DATA_DIR, "wx_debug.log")
_LOCK = threading.Lock()
_MAX_BYTES = 1 << 20  # 1MB 滚动, 防止日志无限膨胀


def log_exc(context: str, exc: Exception):
    """记录一条异常日志 (含上下文与堆栈)。失败静默。"""
    try:
        with _LOCK:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            line = f"[{ts}] {context}\n{tb}\n"
            _append(line)
    except Exception:
        pass


def log_msg(context: str, detail: str = ""):
    """记录一条纯文本日志 (无异常对象, 用于 traceback 字符串/诊断信息)。"""
    try:
        with _LOCK:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] {context}\n{detail}\n" if detail else f"[{ts}] {context}\n"
            _append(line)
    except Exception:
        pass


def _append(line: str):
    if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _MAX_BYTES:
        try:
            os.replace(_LOG_FILE, _LOG_FILE + ".old")
        except OSError:
            pass
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
