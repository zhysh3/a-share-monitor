#!/bin/bash
# start.sh — 一键启动 A股风险监视器
# 启动本地代理(8899) + 静态服务器(8788)，然后用默认浏览器打开面板。
# 重复运行安全：已在跑的服务不会重复启动。
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
[ -f .env ] && source .env
PY="$DIR/venv/bin/python"
HTTP_PORT=8788
URL="http://localhost:$HTTP_PORT/arisk_monitor_local.html"

is_up() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 2 "$1" 2>/dev/null)" != "000" ]; }

# 1) 代理 8899（浏览器实时抓数 + 妙想API 走它）
if is_up "http://localhost:8899/"; then
  echo "✓ 代理已在运行 (8899)"
else
  nohup "$PY" proxy.py >> logs/proxy.log 2>&1 &
  sleep 1
  echo "✓ 代理已启动 (8899)"
fi

# 2) 静态服务器 8788（面板必须走 http，不能用 file:// 双击）
if is_up "$URL"; then
  echo "✓ 网页服务已在运行 ($HTTP_PORT)"
else
  nohup "$PY" -m http.server "$HTTP_PORT" >> logs/http.log 2>&1 &
  sleep 1
  echo "✓ 网页服务已启动 ($HTTP_PORT)"
fi

echo ""
echo "面板地址：$URL"
open "$URL"
