"""多用户校准数据同步包 (顶层, 与 wyckoff/ 平级)。

架构与合并语义见 docs/plan_multiuser_sync.md。

模块划分 (传输层仅云端 MySQL, 无 git 依赖):
    merge      纯合并逻辑 (无 IO)
    bundle     用户数据 ↔ 同步 bundle 打包/落库
    cloud      MySQL 云传输层 (单行原子 upsert)
    service    pull/push/sync/status 编排 + 重训集成

用法:
    python -m sync sync
    python -m sync status
"""
from .bundle import export_bundle, import_bundle, machine_id
from .merge import (
    SCHEMA_VERSION,
    feedback_key,
    merge_feedback,
    merge_model,
    merge_signals,
    signal_key,
)
from .service import pull, push, status, sync
from .transport import SyncError

__all__ = [
    "SCHEMA_VERSION",
    "SyncError",
    "export_bundle",
    "import_bundle",
    "machine_id",
    "merge_signals",
    "merge_feedback",
    "merge_model",
    "signal_key",
    "feedback_key",
    "pull",
    "push",
    "sync",
    "status",
]
