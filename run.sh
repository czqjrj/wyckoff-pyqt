#!/usr/bin/env bash
# Wyckoff 客户端启动脚本 (PyQt6 版)。
# Deepin 桌面会注入 QT_QPA_PLATFORM=dxcb;xcb, 而 dxcb 是 Qt5 专用插件,
# PyQt6 无法加载导致 "Could not find the Qt platform plugin 'dxcb'" /
# 界面渲染失败 (黑屏/控件失真)。这里强制使用 xcb。
# 注意: 不能用 `conda run` (它会用父 shell 的 dxcb;xcb 覆盖前缀变量),
# 直接调用 conda 环境内的 python 解释器。
set -e
cd "$(dirname "$0")"

find_python() {
    # 1) 项目专用的 conda env 目录 (常见位置)
    for base in "$HOME/.unioncode/miniforge/envs/wyckoff-pyqt" \
                "$HOME/miniforge3/envs/wyckoff-pyqt" \
                "$HOME/miniconda3/envs/wyckoff-pyqt" \
                "$HOME/anaconda3/envs/wyckoff-pyqt"; do
        if [ -x "$base/bin/python" ]; then
            echo "$base/bin/python"
            return 0
        fi
    done
    # 2) 通过 conda 定位环境路径 (不覆盖前缀变量, 只取解释器路径)
    if command -v conda >/dev/null 2>&1; then
        env_py="$(conda env list | awk '/^wyckoff-pyqt[[:space:]]/{print $NF"/bin/python"; exit}')"
        if [ -n "$env_py" ] && [ -x "$env_py" ]; then
            echo "$env_py"
            return 0
        fi
    fi
    # 3) 兜底: 系统 python (环境已全局安装依赖时可用)
    if command -v python3 >/dev/null 2>&1; then
        echo "$(command -v python3)"
        return 0
    fi
    return 1
}

PY="$(find_python)"
if [ -z "$PY" ]; then
    echo "错误: 找不到 Python。请先创建 conda 环境 wyckoff-pyqt (见 README)," \
         "或用环境变量 PYTHON_BIN 指定解释器路径。" >&2
    exit 1
fi

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM_XCB:-xcb}"

exec "$PY" wyckoff_ui.py "$@"
