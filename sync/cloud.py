"""校准数据云传输层 (SQLPub MySQL), 作为 Git 仓的替代。

把共享校准 bundle (signals/feedback/model/meta 四份 canonical 数据) 存进
wyckoff.cloud_db.calib_bundle 单行。云端可用时优先, 否则调用方回退到 Git。

语义与原 Git 传输一致 (见 docs/plan_multiuser_sync.md §2):
    cloud_read_canonical()  → 读远端 canonical 四文件
    cloud_write_canonical() → 写 canonical 四文件
    cloud_remote_meta()     → 读共享 meta (status 用)

与 Git 不同的是: 单行原子 upsert (last-writer-wins at storage), 无
push 冲突重试。但调用方总是"先合并远端+本地再写回", 因此不丢数据。
"""
from wyckoff import cloud_db

CANONICAL_FILES = ("signals.json", "feedback.json", "model.json", "meta.json")


def _file_key(name):
    return {"signals.json": "signals",
            "feedback.json": "feedback",
            "model.json": "model",
            "meta.json": "meta"}[name]


def _bundle_key(name):
    return {"signals": "signals.json",
            "feedback": "feedback.json",
            "model": "model.json",
            "meta": "meta.json"}[name]


def enabled():
    """云传输是否可用 (在线 + 连通)。"""
    try:
        return cloud_db.enabled()
    except Exception:
        return False


def read_canonical():
    """读共享 canonical 数据。返回 {name: obj|None} (缺失文件=None)。"""
    b = cloud_db.read_calib_bundle()
    out = {}
    for name in CANONICAL_FILES:
        out[name] = b.get(_file_key(name))
    return out


def write_canonical(files):
    """写 canonical 数据。files: {name: obj|None}, None 表示跳过该文件。

    返回写入的文件名列表。
    """
    mapped = {_file_key(name): obj for name, obj in files.items() if obj is not None}
    cloud_db.write_calib_bundle(mapped)
    return [name for name in files if files.get(name) is not None]


def remote_meta():
    """读共享 meta.json (status 用); 无则 None。"""
    b = cloud_db.read_calib_bundle()
    return b.get("meta")


def make_meta(contributors, n_signals, n_feedback):
    """构建 meta.json 内容 (与 transport.make_meta 一致)。"""
    from .merge import SCHEMA_VERSION
    import time
    return {
        "schema": SCHEMA_VERSION,
        "updated_ts": time.time(),
        "contributors": contributors,
        "counts": {"signals": n_signals, "feedback": n_feedback},
    }
