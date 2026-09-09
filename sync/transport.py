"""同步公共助手 (传输层仅云端 MySQL)。

历史: 本模块曾是 Git 传输层 (clone/fetch/reset/commit/push)。多用户校准数据
与账户私有数据已全部收敛到 MySQL 云后端 (见 wyckoff/cloud_db.py), 已彻底
移除对 git 的运行时依赖。此处只保留各同步模块共享的异常与离线开关。
"""
import os


class SyncError(Exception):
    """同步失败 (云端不可达 / 合并 / 写入失败等不可恢复错误)。"""


class PushRejected(SyncError):
    """兼容旧接口保留; 云后端单行原子 upsert, 不再存在 push 冲突。"""


def no_net():
    return os.environ.get("WYCKOFF_NO_NET", "").strip() in ("1", "true", "TRUE")