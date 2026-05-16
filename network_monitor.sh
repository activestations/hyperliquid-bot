#!/bin/bash
# 网络连通性监控 - 仅 2026-05-13 运行
# 每10分钟 curl 多个目标，区分本地网络问题 vs 测试网问题
set -u

LOGDIR="/home/nerv/.openclaw/workspace/hyperliquid_bot/network_logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/network_monitor_$(date +%Y%m%d).log"
TIMESTAMP=$(date '+%Y-%m-%dT%H:%M:%S%z')

# 目标清单（带超时，并行执行）
# 策略：国内站点全通=本地网络正常；仅hyperliquid不通=测试网问题；国内站点也不通=本地网络问题
check_url() {
  local name="$1" url="$2"
  local start_ms end_ms elapsed rc http_code err_detail

  start_ms=$(date +%s%N)
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 8 --max-time 12 "$url" 2>/dev/null)
  rc=$?
  end_ms=$(date +%s%N)
  elapsed=$(( (end_ms - start_ms) / 1000000 ))

  if [ "$rc" -eq 0 ] && [ "${http_code:-0}" != "000" ]; then
    echo "OK|$name|$http_code|${elapsed}ms"
  else
    err_detail=$(curl -v --connect-timeout 8 --max-time 12 "$url" 2>&1 | tail -3 | tr '\n' ' ' | sed 's/  */ /g')
    echo "FAIL|$name|rc=$rc|http=${http_code:-000}|${elapsed}ms|$err_detail"
  fi
}

# 并行跑所有检测（最多等30秒）
TMPDIR=$(mktemp -d)

{
  check_url "hyperliquid"   "https://api.hyperliquid-testnet.xyz/info"      > "$TMPDIR/hyperliquid" &
  check_url "hyperliquid-v2" "https://api.hyperliquid-testnet.xyz/exchange"   > "$TMPDIR/hyperliquid-v2" &
  check_url "baidu"         "https://www.baidu.com"                           > "$TMPDIR/baidu" &
  check_url "qq"            "https://www.qq.com"                              > "$TMPDIR/qq" &
  check_url "deepseek"      "https://api.deepseek.com"                           > "$TMPDIR/deepseek" &
  check_url "163"           "https://www.163.com"                             > "$TMPDIR/163" &
  wait
} 2>/dev/null

# 汇总结果
{
  overall_ok=0
  overall_fail=0
  results=""
  
  for f in "$TMPDIR"/*; do
    name=$(basename "$f")
    line=$(cat "$f")
    results="$results  $line"$'\n'
    case "$line" in
      OK*) overall_ok=$(( overall_ok + 1 )) ;;
      FAIL*) overall_fail=$(( overall_fail + 1 )) ;;
    esac
  done

  echo "[$TIMESTAMP] ok=$overall_ok fail=$overall_fail"
  echo -n "$results"
} >> "$LOG"

# 有失败则写 alert
if [ "$overall_fail" -gt 0 ]; then
  {
    echo "[$TIMESTAMP] FAILURES ($overall_fail)"
    echo -n "$results"
  } >> "$LOGDIR/network_monitor_alerts_$(date +%Y%m%d).log"
fi

rm -rf "$TMPDIR"
find "$LOGDIR" -name 'network_monitor_*.log' -mtime +14 -delete 2>/dev/null
