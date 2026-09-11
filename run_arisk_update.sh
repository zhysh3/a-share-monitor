#!/bin/bash
# run_arisk_update.sh — 运行一次数据更新（macOS 版）
# 由 check_and_update.sh 调用，或手动执行：bash run_arisk_update.sh
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/arisk_update.log"
PY="$DIR/venv/bin/python"

exec >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') 开始更新 ==="

# 读取 MX_APIKEY（可选）；没有也能跑，社融走央行官网直连 + AKShare 回退
[ -f "$DIR/.env" ] && source "$DIR/.env"
if [ -z "$MX_APIKEY" ]; then
  echo "  (MX_APIKEY 未设置 → 央行直连 / AKShare 回退，功能不受影响)"
fi

cd "$DIR"
"$PY" update_arisk_data.py

echo "=== 完成 ==="
