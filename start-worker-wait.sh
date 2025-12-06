#!/bin/sh
# start-worker-wait.sh
# POSIX sh compatible: wait until Redis (or host:port from CELERY_BROKER_URL) is reachable,
# ensure REPORT_PATH exists, then exec the provided command.
set -eu

: "${CELERY_BROKER_URL:=redis://redis:6379/0}"
: "${MAX_WAIT_SECONDS:=60}"
: "${SLEEP:=0.5}"
: "${VERBOSE:=0}"

log() {
  printf "%s %s\n" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$*"
}

# parse host and port from CELERY_BROKER_URL (basic)
proto_and_rest=${CELERY_BROKER_URL#*://}
hostport=${proto_and_rest%%/*}

# handle IPv6 bracketed addresses like [::1]:6379 and plain host:port
if printf '%s' "$hostport" | grep -q '^



\[' 2>/dev/null; then
  # hostport looks like [addr]:port
  # use safe field-splitting: split on [ and ] to get the address between them
  host=$(printf '%s' "$hostport" | awk -F'[][]' '{print $2}')
  # for port, take last colon-separated field
  port=$(printf '%s' "$hostport" | awk -F: '{print $NF}')
else
  # if hostport ends with :digits, strip them to get host and use digits as port
  if printf '%s' "$hostport" | grep -q ':[0-9][0-9]*$' 2>/dev/null; then
    port=$(printf '%s' "$hostport" | awk -F: '{print $NF}')
    host=$(printf '%s' "$hostport" | awk -F: '{$NF=""; sub(/:$/,""); print}')
  else
    host=$hostport
    port=''
  fi
fi

# normalize: if host ends with :<port> remove that suffix
if [ -n "$port" ]; then
  host=$(printf '%s' "$host" | sed -e "s/:$port\$//")
fi

# fallback defaults
if [ -z "$host" ] || [ "$host" = "$port" ]; then
  host=${host:-redis}
fi
if ! printf '%s' "$port" | grep -q '^[0-9]\+$' >/dev/null 2>&1; then
  port=6379
fi

log "startup-wait: probing ${host}:${port} (from CELERY_BROKER_URL=${CELERY_BROKER_URL})"

# Ensure REPORT_PATH exists
REPORT_PATH=${REPORT_PATH:-/app/instance/reports}
mkdir -p "$REPORT_PATH" 2>/dev/null || true
log "startup-wait: ensured REPORT_PATH $REPORT_PATH"

deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
attempt=0

while [ "$(date +%s)" -le "$deadline" ]; do
  attempt=$((attempt+1))

  probe_host=${host}
  if [ -z "${port:-}" ]; then
    probe_port=6379
  else
    probe_port=${port}
  fi

  case "$probe_port" in
    ''|*[!0-9]*)
      probe_port=6379
      ;;
  esac

  suffix=$probe_port

  if [ -z "$probe_host" ]; then
    probe_host=redis
  fi

  printf 'startup-wait probe: host=%s port=%s (from CELERY_BROKER_URL=%s)\n' "$probe_host" "$probe_port" "${CELERY_BROKER_URL:-}"

  if python3 - <<PY 2> /tmp/probe.err
import socket,sys
host = "$probe_host"
port = $suffix
try:
    s = socket.create_connection((host, port), timeout=3)
    s.close()
    print("PY_OK")
except Exception as e:
    print("PY_FAIL", type(e).__name__, str(e))
    sys.exit(1)
PY
  then
    log "startup-wait: ${probe_host}:${probe_port} reachable after ${attempt} attempts"
    if [ "${VERBOSE}" = "1" ]; then
      log "startup-wait: exec'ing: $*"
    fi

    # DIAGNOSTIC: dump how the script sees arguments and the container cmdline
    log "DEBUG: ARGN: $#"
    i=1
    for a in "$@"; do
      log "DEBUG: ARG $i: <$a>"
      i=$((i+1))
    done
    # show what PID 1 actually sees (NUL-separated)
    if command -v tr >/dev/null 2>&1; then
      tr '\0' '\n' < /proc/1/cmdline | while read -r l; do log "DEBUG: /proc/1/cmdline: <$l>"; done
    else
      # fallback
      cat /proc/1/cmdline | sed 's/\x0/ /g' | while read -r l; do log "DEBUG: /proc/1/cmdline (flat): <$l>"; done
    fi

# --- begin: force prefork if an eventlet token sneaks in ---
# Rewrite any incoming --pool=eventlet or two-token form --pool eventlet
rewrite_args=""
expecting_pool_value=0
for a in "$@"; do
  if [ "${expecting_pool_value:-0}" = "1" ]; then
    if [ "$a" = "eventlet" ]; then
      rewrite_args="$rewrite_args prefork"
    else
      rewrite_args="$rewrite_args $a"
    fi
    expecting_pool_value=0
    continue
  fi

  case "$a" in
    --pool=eventlet)
      rewrite_args="$rewrite_args --pool=prefork"
      ;;
    --pool)
      # preserve --pool but rewrite the following token if it equals eventlet
      rewrite_args="$rewrite_args --pool"
      expecting_pool_value=1
      ;;
    *)
      rewrite_args="$rewrite_args $a"
      ;;
  esac
done

# Trim leading space and reset positional args
set -- $rewrite_args
# --- end: force prefork if an eventlet token sneaks in ---

if [ "${1:-}" = -- ]; then shift; fi
exec "$@"
  else
    if [ -s /tmp/probe.err ]; then
      printf 'startup-wait: probe stderr (short):\n'
      if command -v head >/dev/null 2>&1; then
        head -n 20 /tmp/probe.err || true
      else
        sed -n '1,20p' /tmp/probe.err || true
      fi
      printf '--- end probe.err ---\n'
    fi
    log "startup-wait: waiting for Redis at ${probe_host}:${probe_port}"
    sleep "$SLEEP"
  fi
done

log "startup-wait: timed out after ${MAX_WAIT_SECONDS}s waiting for ${probe_host}:${probe_port}"
exit 1



