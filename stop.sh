#!/bin/bash
# stop.sh — 停止本地代理(8899) 和 静态服务器(8788)
for port in 8899 8788; do
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill 2>/dev/null || true
    echo "✓ 已停止端口 $port"
  else
    echo "· 端口 $port 未在运行"
  fi
done
