#!/usr/bin/env bash
# Enhanced one-shot network diagnostics for HL/DeepSeek route/TLS failures.
# Intended window: 2026-05-15 03:00-08:30 Asia/Shanghai. Safe to run manually too.
set -u

TZ="Asia/Shanghai"
export TZ

BASE="/home/nerv/.openclaw/workspace/hyperliquid_bot"
LOGDIR="$BASE/network_logs"
mkdir -p "$LOGDIR"
DAY="$(date +%Y%m%d)"
LOG="$LOGDIR/network_diag_once_${DAY}.log"
ALERT="$LOGDIR/network_diag_once_alerts_${DAY}.log"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-120}"
END_AT="${END_AT:-2026-05-15 08:30:00}"
END_EPOCH="$(date -d "$END_AT" +%s)"

TARGETS=(
  "deepseek|api.deepseek.com|https://api.deepseek.com/"
  "hyperliquid-info|api.hyperliquid-testnet.xyz|https://api.hyperliquid-testnet.xyz/info"
  "hyperliquid-exchange|api.hyperliquid-testnet.xyz|https://api.hyperliquid-testnet.xyz/exchange"
  "baidu|www.baidu.com|https://www.baidu.com/"
  "163|www.163.com|https://www.163.com/"
  "qq|www.qq.com|https://www.qq.com/"
)

log() { printf '%s\n' "$*" >> "$LOG"; }

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip(), ensure_ascii=False))'
}

resolve_host() {
  local host="$1"
  getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd ',' -
}

route_to() {
  local ip="$1"
  [ -z "$ip" ] && return 0
  ip route get "$ip" 2>/dev/null | head -1 | sed 's/  */ /g'
}

curl_check() {
  local name="$1" host="$2" url="$3" resolve_ip="${4:-}"
  local extra=() label="$name" out rc verbose http code namelookup connect appconnect starttransfer total remote_ip err_tail

  if [ -n "$resolve_ip" ]; then
    # Force one resolved IP while preserving SNI/Host.
    extra=(--resolve "$host:443:$resolve_ip")
    label="$name@$resolve_ip"
  fi

  out=$(mktemp)
  verbose=$(mktemp)
  code=$(curl -sS -o /dev/null \
    -w 'http=%{http_code} dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total} remote=%{remote_ip}' \
    --connect-timeout 5 --max-time 10 \
    "${extra[@]}" "$url" 2>"$verbose")
  rc=$?
  err_tail=$(tail -4 "$verbose" | tr '\n' ' ' | sed 's/  */ /g')
  rm -f "$out" "$verbose"

  if [ "$rc" -eq 0 ] && ! printf '%s' "$code" | grep -q 'http=000'; then
    printf 'OK|%s|%s\n' "$label" "$code"
  else
    printf 'FAIL|%s|rc=%s|%s|err=%s\n' "$label" "$rc" "${code:-http=000}" "$err_tail"
  fi
}

openssl_check() {
  local name="$1" host="$2"
  local out rc tail_line
  out=$(timeout 8 openssl s_client -connect "$host:443" -servername "$host" -brief </dev/null 2>&1)
  rc=$?
  tail_line=$(printf '%s' "$out" | tail -5 | tr '\n' ' ' | sed 's/  */ /g')
  if [ "$rc" -eq 0 ]; then
    printf 'TLS_OK|%s|%s\n' "$name" "$tail_line"
  else
    printf 'TLS_FAIL|%s|rc=%s|%s\n' "$name" "$rc" "$tail_line"
  fi
}

sample_once() {
  local ts ok=0 fail=0 line name host url ips first_ip route ext_ip
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  log "[$ts] sample_start"
  ext_ip=$(curl -sS --connect-timeout 4 --max-time 6 https://api.ipify.org 2>/dev/null || true)
  log "ENV|default_gw=$(ip route show default 2>/dev/null | head -1 | sed 's/  */ /g')|dns=$(resolvectl dns 2>/dev/null | tr '\n' ';' | sed 's/  */ /g')|egress_ip=${ext_ip:-unknown}"

  for item in "${TARGETS[@]}"; do
    IFS='|' read -r name host url <<< "$item"
    ips="$(resolve_host "$host")"
    first_ip="${ips%%,*}"
    route="$(route_to "$first_ip")"
    log "DNS|$name|host=$host|ips=${ips:-NONE}|route=${route:-NONE}"

    line="$(curl_check "$name" "$host" "$url")"
    log "$line"
    case "$line" in OK*) ok=$((ok+1));; FAIL*) fail=$((fail+1));; esac

    # For suspicious API targets, test each current A record directly via --resolve to catch bad CDN edge/IP/routing.
    case "$name" in
      deepseek|hyperliquid-*)
        if [ -n "$ips" ]; then
          IFS=',' read -r -a arr <<< "$ips"
          for ip in "${arr[@]}"; do
            line="$(curl_check "$name" "$host" "$url" "$ip")"
            log "$line"
            case "$line" in OK*) ok=$((ok+1));; FAIL*) fail=$((fail+1));; esac
          done
        fi
        line="$(openssl_check "$name" "$host")"
        log "$line"
        case "$line" in TLS_OK*) ok=$((ok+1));; TLS_FAIL*) fail=$((fail+1));; esac
        ;;
    esac
  done

  log "[$ts] sample_end ok=$ok fail=$fail"
  if [ "$fail" -gt 0 ]; then
    {
      echo "[$ts] FAILURES ok=$ok fail=$fail"
      tail -n 80 "$LOG"
    } >> "$ALERT"
  fi
}

log "==== network diag once started at $(date '+%Y-%m-%dT%H:%M:%S%z'), end_at=$END_AT, interval=${INTERVAL_SECONDS}s ===="
while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
  sample_once
  now=$(date +%s)
  [ "$now" -ge "$END_EPOCH" ] && break
  sleep_for=$INTERVAL_SECONDS
  remaining=$((END_EPOCH - now))
  [ "$remaining" -lt "$sleep_for" ] && sleep_for="$remaining"
  sleep "$sleep_for"
done
log "==== network diag once finished at $(date '+%Y-%m-%dT%H:%M:%S%z') ===="
