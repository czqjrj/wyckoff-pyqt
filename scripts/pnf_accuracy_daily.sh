#!/usr/bin/env bash
# ── PNF 三档目标准确率 · 每日评估 cron 包装脚本 ──
# 用法:
#   chmod +x scripts/pnf_accuracy_daily.sh
#   ./scripts/pnf_accuracy_daily.sh               # 手动执行一次 (打印+JSON落盘)
#   ./scripts/pnf_accuracy_daily.sh cron          # cron模式: 只输出JSON单行+stderr
#
# 环境: 自动定位 conda 环境 (优先用 /home/RuanJun/.unioncode/miniforge/envs/wyckoff-pyqt)
#       失败则回退到 `which python` 的当前环境。
set -euo pipefail

# ── 路径配置 ──
PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJ_DIR/pnf_accuracy"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/cron.log"

PYTHON_BIN=""
# 1) 优先用本项目指定的 conda 环境 python
CANDIDATE="/home/RuanJun/.unioncode/miniforge/envs/wyckoff-pyqt/bin/python"
if [ -x "$CANDIDATE" ]; then
    PYTHON_BIN="$CANDIDATE"
else
    # 2) 回退: 取当前激活的 python
    PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    echo "[$(date '+%F %T')] [ERROR] 找不到可用 python (尝试 $CANDIDATE)" >&2
    exit 1
fi

EVAL_SCRIPT="$PROJ_DIR/scripts/eval_pnf_tier_accuracy.py"
cd "$PROJ_DIR"

MODE="${1:-manual}"
TS="$(date '+%F %T')"

# 日志头
if [ "$MODE" = "cron" ]; then
    exec >>"$LOG_FILE" 2>&1
    echo "[$TS] ===== PNF 准确率 cron 开始 ====="
    # cron 模式: quiet-json 输出单行 JSON (末尾也会落盘 JSON)
    PYTHONUNBUFFERED=1 TZ=Asia/Shanghai \
        "$PYTHON_BIN" "$EVAL_SCRIPT" --run --quiet-json
    RC=$?
    echo "[$TS] ===== PNF 准确率 cron 结束 exit=$RC ====="
    exit $RC
else
    # 手动模式: 完整打印 + JSON落盘
    echo "[$TS] ===== PNF 准确率 手动执行 ====="
    echo "  PROJ_DIR  = $PROJ_DIR"
    echo "  PYTHON    = $PYTHON_BIN"
    echo "  LOG_DIR   = $LOG_DIR"
    echo
    PYTHONUNBUFFERED=1 TZ=Asia/Shanghai \
        "$PYTHON_BIN" "$EVAL_SCRIPT" --run
    RC=$?
    TS2="$(date '+%F %T')"
    echo
    echo "[$TS2] ===== 执行完成 exit=$RC ====="
    exit $RC
fi
