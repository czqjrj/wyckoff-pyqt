"""Git 传输层: 私有仓库 clone/fetch/reset/commit/push。

canonical 仓库布局 (见 docs/plan_multiuser_sync.md §2):
    signals.json / feedback.json / model.json / meta.json

WYCKOFF_NO_NET=1 时所有网络操作静默跳过 (返回 skipped), 测试离线安全。
"""
import os
import subprocess

from .merge import SCHEMA_VERSION

REPO_DIRNAME = "calib_repo"
CANONICAL_FILES = ("signals.json", "feedback.json", "model.json", "meta.json")


class SyncError(Exception):
    """同步失败 (clone/push 等不可恢复错误)。"""


class PushRejected(SyncError):
    """push 被拒 (他人先推), 调用方应重拉合并后重试。"""


def no_net():
    return os.environ.get("WYCKOFF_NO_NET") == "1"


def repo_dir(data_dir=None):
    if data_dir is None:
        from wyckoff.paths import DATA_DIR

        data_dir = DATA_DIR
    return os.path.join(data_dir, REPO_DIRNAME)


def _run(args, cwd=None):
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd,
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError as e:
        raise SyncError("未找到 git 可执行文件") from e
    except subprocess.TimeoutExpired as e:
        raise SyncError(f"git {' '.join(args[:2])} 超时") from e
    return p


def _ok(p):
    return p.returncode == 0


def default_branch(repo):
    """探测远端默认分支名; 探测不到回退 main。"""
    p = _run(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if _ok(p) and p.stdout.strip():
        return p.stdout.strip().removeprefix("origin/")
    for cand in ("main", "master"):
        p = _run(["show-ref", "--verify", f"refs/remotes/origin/{cand}"], cwd=repo)
        if _ok(p):
            return cand
    return "main"


def fetch_repo(rdir):
    """fetch 远端引用; 失败抛 SyncError。"""
    p = _run(["fetch", "origin", "--prune"], cwd=rdir)
    if not _ok(p):
        raise SyncError(f"git fetch 失败: {p.stderr.strip()}")


def ensure_repo(url, rdir=None):
    """clone (首次) 或 fetch (已有)。返回 (repo_path, branch)。"""
    if not url:
        raise SyncError("未配置校准仓库 URL")
    if rdir is None:
        rdir = repo_dir()
    if no_net():
        return rdir, None
    if os.path.isdir(os.path.join(rdir, ".git")):
        fetch_repo(rdir)
    else:
        parent = os.path.dirname(rdir)
        os.makedirs(parent, exist_ok=True)
        p = _run(["clone", url, rdir])
        # 空仓 clone 会以 "空仓库" 提示退出码非零但目录已初始化, 需容忍
        if not os.path.isdir(os.path.join(rdir, ".git")):
            raise SyncError(f"git clone 失败: {(p.stderr or p.stdout).strip()}")
        if not _ok(p):
            _run(["remote", "set-url", "origin", url], cwd=rdir)
        # 空仓场景 HEAD 未born且分支名取决于本机 git 配置; 统一钉到
        # 探测到的默认分支名 (无远端分支时回退 main), 保证多端一致
        br = default_branch(rdir)
        _run(["checkout", "-B", br], cwd=rdir)
    return rdir, default_branch(rdir)


def reset_to_remote(repo, branch):
    """丢弃本地克隆的任何中间状态, 硬复位到 origin/<branch>。"""
    if branch is None:
        return
    p = _run(["checkout", "-B", branch, f"origin/{branch}"], cwd=repo)
    if not _ok(p):
        # 远端分支尚不存在 (空仓首推场景): 本地工作树保持原样即可
        return


def read_canonical(repo):
    """读 canonical 文件; 缺失/损坏按空处理。"""
    data = {}
    for name in CANONICAL_FILES:
        path = os.path.join(repo, name)
        entry = None
        if os.path.exists(path):
            import json

            try:
                with open(path, encoding="utf-8") as f:
                    entry = json.load(f)
            except Exception:
                entry = None
        data[name] = entry
    return data


def write_canonical(repo, files):
    """写 canonical 文件。files: {name: obj|None}, None 表示跳过该文件。"""

    from wyckoff._shared import atomic_write_json

    written = []
    for name, obj in files.items():
        if obj is None:
            continue
        atomic_write_json(os.path.join(repo, name), obj, indent=1)
        written.append(name)
    return written


def commit_all(repo, message):
    """提交全部变更。有新提交返回 True, 无变更返回 False。"""
    _run(["add", "-A"], cwd=repo)
    p = _run(["commit", "-m", message], cwd=repo)
    if _ok(p):
        return True
    if "nothing to commit" in (p.stdout or ""):
        return False
    raise SyncError(f"git commit 失败: {(p.stderr or '').strip()}")


def push(repo, branch):
    """推送; 非快进被拒抛 PushRejected, 其余失败抛 SyncError。"""
    if branch is None:
        raise SyncError("远端分支未知, 无法推送")
    p = _run(["push", "origin", f"HEAD:{branch}"], cwd=repo)
    if _ok(p):
        return
    err = (p.stderr or "") + (p.stdout or "")
    if "rejected" in err or "fetch first" in err or "non-fast-forward" in err:
        raise PushRejected(err.strip())
    raise SyncError(f"git push 失败: {err.strip()}")


def remote_head_meta(repo, branch):
    """不切工作树直接读远端最新提交里的 meta.json (status 用)。"""
    if branch is None:
        return None
    p = _run(["show", f"origin/{branch}:meta.json"], cwd=repo)
    if not _ok(p):
        return None
    import json

    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def make_meta(contributors, n_signals, n_feedback):
    """构建 meta.json 内容。contributors: {machine_id: last_seen_ts}。"""
    import time

    return {
        "schema": SCHEMA_VERSION,
        "updated_ts": time.time(),
        "contributors": contributors,
        "counts": {"signals": n_signals, "feedback": n_feedback},
    }
