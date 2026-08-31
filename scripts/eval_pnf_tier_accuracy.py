#!/usr/bin/env python3
"""PNF 三档目标准确率评估 CLI (薄封装: 库逻辑在 wyckoff.pnf_accuracy)。

用法:
  python scripts/eval_pnf_tier_accuracy.py            # 交互式评估+打印
  python scripts/eval_pnf_tier_accuracy.py --run           # 标准评估 (打印+落盘)
  python scripts/eval_pnf_tier_accuracy.py --run --quiet-json  # cron模式 (只输出单行JSON)
  python scripts/eval_pnf_tier_accuracy.py --export        # 静默评估+仅打印落盘位置
  python scripts/eval_pnf_tier_accuracy.py --install-cron [HH:MM]
  python scripts/eval_pnf_tier_accuracy.py --uninstall-cron
  python scripts/eval_pnf_tier_accuracy.py --install-task [HH:MM]   # Windows
  python scripts/eval_pnf_tier_accuracy.py --uninstall-task         # Windows
"""
import json
import os
import sys

# 让脚本被直接执行时能 import wyckoff
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wyckoff.pnf_accuracy import (
    PNF_ACC_DIR,
    PNF_ACC_LATEST,
    install_cron,
    install_task,
    run_eval,
)


def main():
    args = list(sys.argv)
    if "--run" in args:
        quiet = "--quiet-json" in args
        rep = run_eval(print_stdout=not quiet, export_json=True)
        if quiet:
            print(json.dumps(rep, ensure_ascii=False, default=str))
    elif "--export" in args:
        rep = run_eval(print_stdout=False, export_json=True)
        print(f"已导出报告到 {PNF_ACC_DIR}, 最新: {PNF_ACC_LATEST}")
    elif "--install-cron" in args:
        i = args.index("--install-cron")
        arg = args[i + 1] if len(args) > i + 1 else "02:10"
        if ":" in arg:
            hh, mm = arg.split(":", 1)
        else:
            hh, mm = arg, "10"
        try:
            hh, mm = int(hh), int(mm)
        except ValueError:
            hh, mm = 2, 10
        install_cron(hh, mm)
        print(f"已安装每日 {hh:02d}:{mm:02d} 的 PNF 准确率自动评估 cron 任务 (默认凌晨02:10)")
        print(f"  日志目录: {PNF_ACC_DIR}/cron.log")
        print(f"  JSON报告: {PNF_ACC_LATEST}")
    elif "--uninstall-cron" in args:
        install_cron(None)
        print("已移除 PNF 准确率自动评估 cron 任务")
    elif "--install-task" in args:
        i = args.index("--install-task")
        arg = args[i + 1] if len(args) > i + 1 else "02:10"
        install_task(arg)
        print(f"已创建 Windows 每日 {arg} 计划任务 WyckoffPnfAccuracy")
    elif "--uninstall-task" in args:
        install_task(remove=True)
        print("已删除 Windows WyckoffPnfAccuracy 计划任务")
    else:
        # 默认: 直接交互式评估 + 打印
        run_eval(print_stdout=True, export_json=True)


if __name__ == "__main__":
    main()
