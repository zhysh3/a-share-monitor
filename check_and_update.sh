#!/bin/bash
# check_and_update.sh — 每小时被 launchd 调用，判断 arisk_data.json 是否需要重新抓取
# （macOS 版；路径改为脚本所在目录，python 用 venv）
#
# 判据是「数据里的最新交易日」而不是「文件修改时间」：
#   1. JSON 不存在                              → 更新
#   2. turnover.date 落后于"应有的最新交易日"    → 更新
#   3. 数据日期已到位                            → 跳过（收盘数据不会再变）
#
# "应有的最新交易日" = 最近一个工作日；当天 18:00 之前算前一个工作日。
# 若更新后数据日期仍落后且 $EXP 已过去一天仍抓不到，判定为非交易日记入
# .arisk_nontrading_dates，之后自动跳过；若 $EXP 就是今天则不拉黑，留给下小时重试。

DIR="$(cd "$(dirname "$0")" && pwd)"
JSON="$DIR/arisk_data.json"
LOG="$DIR/arisk_update.log"
NONTRADING="$DIR/.arisk_nontrading_dates"
UPDATER="$DIR/run_arisk_update.sh"
PY="$DIR/venv/bin/python"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

expected_date() {
    "$PY" - "$NONTRADING" <<'PY'
import sys, datetime
try:
    skip = {l.strip() for l in open(sys.argv[1]) if l.strip()}
except FileNotFoundError:
    skip = set()
now = datetime.datetime.now()
d = now.date()
if now.hour < 18:                              # 收盘+发布缓冲之前，今天还不该有数据
    d -= datetime.timedelta(days=1)
for _ in range(30):
    if d.weekday() < 5 and d.isoformat() not in skip:
        break
    d -= datetime.timedelta(days=1)
print(d.isoformat())
PY
}

data_date() {
    "$PY" - "$JSON" <<'PY'
import sys, json
try:
    print((json.load(open(sys.argv[1])).get('turnover') or {}).get('date') or '')
except Exception:
    print('')
PY
}

if [ ! -f "$JSON" ]; then
    log "check: json 不存在 → 更新"
    exec "$UPDATER"
fi

EXP=$(expected_date)
CUR=$(data_date)

if [ -n "$CUR" ] && [[ ! "$CUR" < "$EXP" ]]; then
    log "check: skip — 数据日期 $CUR 已覆盖应有交易日 $EXP"
    exit 0
fi

log "check: 数据日期 ${CUR:-<缺失>} 落后于应有交易日 $EXP → 更新"
"$UPDATER"
RC=$?

if [ "$RC" -ne 0 ]; then
    log "check: 更新脚本失败 (exit $RC)，下小时重试"
    exit "$RC"
fi

NEW=$(data_date)
TODAY=$(date +%F)
if [ -n "$NEW" ] && [[ "$NEW" < "$EXP" ]]; then
    if [[ "$EXP" < "$TODAY" ]]; then
        grep -qxF "$EXP" "$NONTRADING" 2>/dev/null || echo "$EXP" >> "$NONTRADING"
        log "check: 更新后数据日期仍为 $NEW，且 $EXP 已过去一天仍抓不到，判定为非交易日，已记入 $(basename $NONTRADING)"
    else
        log "check: 更新后数据日期仍为 $NEW，$EXP 就是今天，判定为数据源尚未发布，下小时重试（不拉黑）"
    fi
else
    log "check: 更新完成，数据日期 ${NEW:-<缺失>}"
fi
