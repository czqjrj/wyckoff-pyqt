"""CLI 入口: python -m sync <command>。

子命令:
    setup <url>   配置私有仓库 URL 并 clone
    pull          拉取远端合并进本地
    push          本地全量推送远端
    sync          完整同步 (pull→merge→retrain→push)
    status        查看同步状态
"""
import argparse
import json
import sys

from . import service, transport


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m sync",
                                     description="多用户校准数据同步")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_setup = sub.add_parser("setup", help="配置仓库 URL 并 clone (https 可附带凭据自动鉴权)")
    p_setup.add_argument("url", help="私有 git 仓库地址 (ssh/https)")
    p_setup.add_argument("--user", default="", help="https 用户名 (可选)")
    p_setup.add_argument("--password", default="", help="https 密码 / Personal Access Token (可选)")
    sub.add_parser("pull", help="拉取远端合并进本地")
    sub.add_parser("push", help="本地全量推送远端")
    sub.add_parser("sync", help="完整同步 (pull→merge→retrain→push)")
    sub.add_parser("status", help="查看同步状态")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "setup":
            _print(service.setup(args.url, args.user, args.password))
        elif args.cmd == "pull":
            _print(service.pull())
        elif args.cmd == "push":
            _print(service.push())
        elif args.cmd == "sync":
            r = service.sync()
            if r.get("skipped"):
                print(f"[sync] 跳过: {r['skipped']}")
                return 0
            _print(r)
            return 0 if r.get("ok") else 1
        elif args.cmd == "status":
            st = service.status()
            if transport.no_net():
                st["note"] = "WYCKOFF_NO_NET=1, 已跳过 fetch"
            _print(st)
    except transport.SyncError as e:
        print(f"[sync] 失败: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
