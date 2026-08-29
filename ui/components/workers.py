"""通用后台线程基类 (此前全项目 26 个 QThread 子类中三种家族模式高度重复)。

- FnThread: "传 work 闭包一次性执行" 家族 —— 替换 AutoSyncThread /
  LabelAiThread / _RetrainThread / _AiVerifyThread / _AskThread 等。
- ScanThreadBase: "stop标志 + progress/result 信号 + 循环扫描" 家族基类
  —— WatchScanThread / EntriesScanThread / _AdvScanThread 等继承用。
"""
from PyQt6.QtCore import QThread, pyqtSignal


class FnThread(QThread):
    """在后台线程执行一次 work() -> object; 成功发 ok, 异常发 err。"""

    ok = pyqtSignal(object)
    err = pyqtSignal(str)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work

    def run(self):
        try:
            self.ok.emit(self._work())
        except Exception as e:
            self.err.emit(str(e))


class ScanThreadBase(QThread):
    """可停止的批量扫描线程基类。

    子类覆盖 do_scan(), 在循环里自行检查 self._stopped;
    标准信号: result(object) 最终结果 / progress(int,int,str) 进度 /
    error(str) 失败。stop() 置位停止标志 (幂等)。
    """
    result = pyqtSignal(object)
    progress = pyqtSignal(int, int, str)
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stopped = False

    def stop(self):
        self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped

    def do_scan(self):
        """子类实现: 执行扫描并 return 结果对象 (由基类 emit)。"""
        raise NotImplementedError

    def run(self):
        try:
            self.result.emit(self.do_scan())
        except Exception as e:
            self.error.emit(str(e))
