"""模拟盘 (Paper Trading) 定时任务: Windows 计划任务 / Linux cron / 常驻 daemon。

让模拟盘的"自动周期 / 全市场扫描"不依赖 UI 进程常驻 —— UI 内定时器是
相对间隔且进程一关就失效; 本模块提供跨平台的定点定时执行:

  python -m wyckoff.paper_cron --scan                  # 立即全市场扫描 (run_scan)
  python -m wyckoff.paper_cron --cycle                 # 立即执行一个周期 (run_cycle)
  python -m wyckoff.paper_cron --cycle --force-scan    # 立即强制重扫 + 周期
  python -m wyckoff.paper_cron --install [HH:MM] [--interval N]   # 每日一次, 或每隔 N 分钟
  python -m wyckoff.paper_cron --install-scan [HH:MM] [--interval N]
  python -m wyckoff.paper_cron --uninstall             # 卸载定时任务
  python -m wyckoff.paper_cron --daemon [分钟]         # 常驻: 每 N 分钟执行一次周期 (默认 30)

平台自适应: Windows → schtasks 计划任务 (写 bat); Linux → crontab。
注意: 定时任务与 UI 内"自动执行周期"共用同一份 wx_paper.json, 启用定时任务时
建议把 UI 的自动执行设为关闭, 避免两处并发写账户状态。
"""
from __future__ import annotations

import os
import subprocess
import sys

from .paths import DATA_DIR

TASK_NAME = "WyckoffPaper"
DEFAULT_AT = "08:59"


def _sched_command(extra=""):
    """生成供 cron / 计划任务执行的命令 (源码运营用 `python -m`, 打包后用 exe)。"""
    extra = extra.strip()
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}" paper_cron'
    else:
        proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Windows 计划任务默认工作目录是 System32, 跨盘必须 `cd /d`,
        # 否则 `cd "E:\..."` 不切盘 → python 从 System32 启动, -m 找不到 wyckoff 包。
        cd = "cd /d" if os.name == "nt" else "cd"
        cmd = f'{cd} "{proj}" && "{sys.executable}" -m wyckoff.paper_cron'
    return (cmd + (" " + extra) if extra else cmd)


def _parse_hhmm(arg):
    """解析 "HH:MM"; 无冒号则视为小时 (分钟=0)。失败回退默认 08:59。"""
    if ":" in str(arg):
        hh, mm = str(arg).split(":", 1)
    else:
        hh, mm = str(arg), "0"
    try:
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except ValueError:
        return 8, 59


def _settings():
    try:
        from .storage import load_settings
        s = load_settings()
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def cycle_task(force_scan=False, progress=None):
    """立即执行一个模拟盘周期; 返回 stats (统计 dict)。"""
    from .paper import apply_paper_params, run_cycle
    settings = _settings()
    apply_paper_params(settings)
    return run_cycle(settings=settings, force_scan=force_scan, progress=progress)


def scan_task(scan_type="", n_codes=6000, progress=None):
    """立即执行一次全市场扫描 (三策略并线); 返回 (result_str, n_candidates)。"""
    from .paper import apply_paper_params, load_state, run_scan
    apply_paper_params(_settings())
    st = load_state()
    result = run_scan(st, scan_type=scan_type, n_codes=n_codes, progress=progress)
    return result, len(st.get("candidates") or [])


def _progress_cb():
    """简单进度回调: 打印大进度跳变, 便于定时执行日志可读。"""
    _prev = {"pct": -1}

    def _cb(done, total, code):
        if total > 0:
            pct = int(round(100.0 * done / total))
            if pct != _prev["pct"]:
                _prev["pct"] = pct
                print(f"[paper] 扫描进度 {pct}% ({done}/{total})", flush=True)

    return _cb


def _print_cycle_result(st, force_scan):
    if st is None:
        print("[paper] 另一实例 (界面或另一定时进程) 正在执行, 本次周期已跳过",
              flush=True)
        return
    try:
        eq = st.get("equity", 0)
        cash = st.get("cash", 0)
        n_pos = st.get("n_positions", 0)
        n_closed = st.get("n_closed", 0)
    except Exception:
        eq = cash = n_pos = n_closed = 0
    mode = "强制重扫" if force_scan else "冷却复用"
    print(f"[paper] 周期完成 ({mode}): 权益 {eq:,.0f} · 现金 {cash:,.0f} · "
          f"持仓 {n_pos} · 已平仓 {n_closed}", flush=True)


# ── 定时任务安装 (Windows / Linux) ────────────────────────
def _cron_existing():
    try:
        return subprocess.check_output(["crontab", "-l"],
                                       stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError:
        return ""


def install_cron(at=DEFAULT_AT, remove=False, scan=False, force_scan=False,
                 interval=0):
    """Linux: 在 crontab 安装/移除每日模拟盘任务。

    interval>0 时改为每隔 interval 分钟执行一次 (`*/N * * * *`);
    否则每日 at (HH:MM) 执行一次。remove=True 时移除。
    """
    lines = [ln for ln in _cron_existing().splitlines()
             if "wyckoff.paper_cron" not in ln]
    if not remove:
        # 默认(不带 scan/force-scan) = 周期任务, 需带 --cycle (Linux cron 同逻辑)。
        extra = " --scan" if scan else (" --force-scan" if force_scan else " --cycle")
        if interval > 0:
            # 无法在 cron 里自定义起始分钟 (*/N 固定从整点对位), 忽略 at。
            line = f"*/{max(1, int(interval))} * * * * " \
                   f"{_sched_command(extra)} >> /dev/null 2>&1"
        else:
            hh, mm = _parse_hhmm(at)
            line = f"{mm} {hh} * * * {_sched_command(extra)} " \
                   f">> /dev/null 2>&1"
        lines.insert(0, line)
    subprocess.run(["crontab", "-"], input="\n".join(lines).strip() + "\n",
                   text=True, check=True)
    return not remove


def install_task(at=DEFAULT_AT, remove=False, scan=False, force_scan=False,
                 interval=0):
    """Windows: 创建/移除"模拟盘"计划任务 (schtasks)。

    interval>0 时用 /SC MINUTE /MO N, 每 N 分钟重复 (带 /ST 起始时刻);
    否则 /SC DAILY 每日 at (HH:MM) 执行一次。
    """
    if remove:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       check=False, stderr=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL)
        try:
            os.remove(os.path.join(DATA_DIR, "wx_paper_daily.bat"))
        except OSError:
            pass
        return
    # 默认(不带 scan/force-scan) = 周期任务: 必须带 --cycle,
    # 否则计划任务启动的 `python -m wyckoff.paper_cron` 无参数只打印帮助即退出。
    extra = " --scan" if scan else (" --force-scan" if force_scan else " --cycle")
    bat = os.path.join(DATA_DIR, "wx_paper_daily.bat")
    log = os.path.join(DATA_DIR, "wx_paper_cron.log")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f"@echo off\n{_sched_command(extra)} >> \"{log}\" 2>&1\n")
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", bat, "/F"]
    if interval > 0:
        args += ["/SC", "MINUTE", "/MO", str(max(1, int(interval))),
                 "/ST", at]
    else:
        args += ["/SC", "DAILY", "/ST", at]
    subprocess.run(args, check=True)


def daemon_cycle(minutes=30, force_scan=False):
    """常驻循环: 每 minutes 分钟执行一次周期 (跨平台, 可手动后台运行)。"""
    minutes = max(5, int(minutes))
    print(f"[paper] 常驻周期已启动, 每 {minutes} 分钟一次 (Ctrl+C 退出)", flush=True)
    while True:
        try:
            _main(["--force-scan"] if force_scan else ["--cycle"])
        except Exception as e:
            print(f"[paper] 周期异常: {e}", flush=True)
        try:
            import time
            time.sleep(minutes * 60)
        except KeyboardInterrupt:
            break


def install_daily(at=DEFAULT_AT, scan=False, force_scan=False, remove=False,
                  interval=0):
    """平台自适应安装/卸载模拟盘定时任务 (Windows schtasks / Linux cron)。

    interval>0: 每隔 interval 分钟重复执行一次 (Windows /SC MINUTE,
    Linux `*/N`), at 作起始时刻; interval=0: 每日 at (HH:MM) 执行一次。
    返回人类可读结果字符串 (供 UI 直接展示)。
    """
    if remove:
        if os.name == "nt":
            install_task(at, remove=True)
        else:
            install_cron(at, remove=True)
        return "已移除模拟盘定时任务"
    kind = "全市场扫描" if scan else ("强制重扫周期" if force_scan else "周期")
    if interval > 0:
        freq = f"每隔 {int(interval)} 分钟 (起始 {at})"
    else:
        freq = f"每日 {at}"
    if os.name == "nt":
        install_task(at, scan=scan, force_scan=force_scan, interval=interval)
        return f"已安装{freq}模拟盘{kind}任务 (Windows 计划任务)"
    install_cron(at, scan=scan, force_scan=force_scan, interval=interval)
    return f"已安装{freq}模拟盘{kind}任务 (crontab)"


def _main(argv=None):
    import time
    args = argv if argv is not None else sys.argv[1:]
    at = DEFAULT_AT
    for kw in ("--install-scan", "--install"):
        if kw in args:
            i = args.index(kw)
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if ":" in nxt or (nxt.isdigit()):
                at = nxt
    interval = 0
    if "--interval" in args:
        i = args.index("--interval")
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if nxt.isdigit():
            interval = max(1, int(nxt))

    if "--scan" in args:
        result, _n = scan_task(progress=_progress_cb())
        print(f"[paper] {result}", flush=True)
    elif "--cycle" in args or "--force-scan" in args:
        force = "--force-scan" in args
        st = cycle_task(force_scan=force, progress=_progress_cb())
        _print_cycle_result(st, force)
    elif "--install-scan" in args:
        if os.name == "nt":
            install_task(at, scan=True, interval=interval)
        else:
            install_cron(at, scan=True, interval=interval)
        freq = (f"每隔 {interval} 分钟 (起始 {at})" if interval > 0
                else f"每日 {at}")
        print(f"[paper] 已安装{freq}全市场扫描任务 (Windows schtasks / Linux cron)")
    elif "--install" in args or "--install-cron" in args or "--install-task" in args:
        scan = "--scan" in args
        force = "--force-scan" in args
        if os.name == "nt":
            install_task(at, scan=scan, force_scan=force, interval=interval)
        else:
            install_cron(at, scan=scan, force_scan=force, interval=interval)
        freq = (f"每隔 {interval} 分钟 (起始 {at})" if interval > 0
                else f"每日 {at}")
        kind = "全市场扫描" if scan else ("强制重扫周期" if force else "周期")
        print(f"[paper] 已安装{freq}模拟盘{kind}任务 (Windows schtasks / Linux cron)")
    elif "--uninstall" in args or "--remove" in args:
        if os.name == "nt":
            install_task(remove=True)
        else:
            install_cron(remove=True)
        print("[paper] 已移除模拟盘定时任务")
    elif "--daemon" in args:
        i = args.index("--daemon")
        minutes = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 30
        daemon_cycle(minutes, force_scan="--force-scan" in args)
    else:
        print(__doc__)
    # 等待 print 缓冲排空, 保证计划任务/cron 日志完整落盘后进程退出
    try:
        time.sleep(0.05)
    except Exception:
        pass


if __name__ == "__main__":
    _main()
