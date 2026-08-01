#!/usr/bin/env bash
# Embodit service manager — requires Python >= 3.10 and uv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/service.pid"
URL_FILE="${SCRIPT_DIR}/service.url"
LOG_FILE="${SCRIPT_DIR}/service.log"

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
  BOLD="$(tput bold)"
  DIM="$(tput dim)"
  GREEN="$(tput setaf 2)"
  RED="$(tput setaf 1)"
  RESET="$(tput sgr0)"
else
  BOLD=""
  DIM=""
  GREEN=""
  RED=""
  RESET=""
fi

info() { printf '%s\n' "$*"; }
ok() { printf '%s\n' "${GREEN}$*${RESET}"; }
fail() { printf '%s\n' "${RED}$*${RESET}" >&2; }

usage() {
  cat <<'EOF'
Usage: bash embodit.sh <command> [options]

Commands:
  start [DATA_ROOT]    Start Embodit (default data root: current directory)
  stop                 Stop Embodit and clean up stray instances
  restart [DATA_ROOT]  Restart Embodit
  status               Show whether Embodit is running
  logs [LINES]         Show recent logs (default: 50 lines)
  logs -f              Follow the service log
  clean [MODE]         Clean managed files: expired (default), --cache, or --all
  help                 Show this help

Examples:
  bash embodit.sh start ~/datasets
  EMBODY_PORT=9000 bash embodit.sh start /data/lerobot
  bash embodit.sh status
  bash embodit.sh logs -f
  bash embodit.sh stop
  bash embodit.sh clean --dry-run
  bash embodit.sh clean --cache

Optional env vars: EMBODY_ROOT, EMBODY_HOST, EMBODY_PORT,
  EMBODY_PUBLIC_HOST, EMBODY_TOKEN, EMBODY_PROXY, EMBODIT_SANDBOX,
  EMBODIT_CACHE_DIR, AUGMENT_PYTHON, AUGMENT_SAM3_CHECKPOINT
EOF
}

read_pid() {
  [[ -s "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$pid"
}

running_pid() {
  local pid
  pid="$(read_pid)" || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

start_service() {
  if (( $# > 1 )); then
    fail "start accepts at most one DATA_ROOT argument."
    usage >&2
    return 2
  fi
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    return 0
  fi

  local data_root="${1:-${EMBODY_ROOT:-${LEROBOT_ROOT:-}}}"
  local host="${EMBODY_HOST:-${LEROBOT_HOST:-127.0.0.1}}"
  local port="${EMBODY_PORT:-${LEROBOT_PORT:-8765}}"
  local proxy="${EMBODY_PROXY:-${LEROBOT_PROXY:-}}"
  local public_host="${EMBODY_PUBLIC_HOST:-${LEROBOT_PUBLIC_HOST:-localhost}}"
  local token_file="${SCRIPT_DIR}/config/token"
  local token url pid start_ts

  if [[ -z "$data_root" ]]; then
    data_root="$(pwd)"
    info "${DIM}No data root given; browsing from current directory: ${data_root}${RESET}"
  fi
  data_root="$(cd "$data_root" 2>/dev/null && pwd || printf '%s' "$data_root")"

  if ! command -v uv >/dev/null 2>&1; then
    fail "uv is not installed (required to manage the Python environment)."
    fail "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fail "Docs:    https://docs.astral.sh/uv/getting-started/installation/"
    return 1
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 is required on PATH."
    return 1
  fi

  if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    fail "EMBODY_PORT must be an integer between 1 and 65535 (got: ${port})."
    return 2
  fi

  if pid="$(running_pid)"; then
    ok "Embodit is already running (pid ${pid})."
    info "  URL: $(cat "$URL_FILE" 2>/dev/null || printf 'unknown')"
    return 0
  fi

  rm -f "$PID_FILE" "$URL_FILE"

  # Refuse to generate a new token while another process owns the port.
  if python3 - "$host" "$port" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
connect_host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
try:
    with socket.create_connection((connect_host, port), timeout=0.3):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
  then
    fail "Port ${port} is already in use (a stray Embodit instance may still be running)."
    fail "Run 'bash ${SCRIPT_DIR}/embodit.sh stop' first."
    return 1
  fi

  token="${EMBODY_TOKEN:-${LEROBOT_TOKEN:-}}"
  if [[ -z "$token" && -s "$token_file" ]]; then
    token="$(head -n1 "$token_file" | tr -d '[:space:]')"
  fi
  if [[ -z "$token" ]]; then
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  fi
  mkdir -p "${SCRIPT_DIR}/config"
  printf '%s\n' "$token" >"$token_file"
  chmod 600 "$token_file"
  url="http://${public_host}:${port}/?token=${token}"

  if [[ -n "$proxy" ]]; then
    export http_proxy="$proxy" https_proxy="$proxy"
    export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
  fi
  export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost"
  export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"

  info "${BOLD}Embodit · Embodied Intelligence Toolkit${RESET}"
  info "${DIM}----------------------------------------${RESET}"
  info "  Data root : ${data_root}"
  info "  Listen on : http://${host}:${port}"
  info "  Log file  : ${LOG_FILE}"
  info ""

  printf 'Syncing environment ... '
  if (cd "$SCRIPT_DIR" && uv sync --quiet); then
    printf '%s\n' "${GREEN}done${RESET}"
  else
    printf '%s\n' "${RED}failed${RESET}"
    fail "uv sync failed. Check network / proxy (EMBODY_PROXY) and retry."
    return 1
  fi

  printf 'Starting server ... '
  start_ts=$SECONDS
  nohup uv run --project "$SCRIPT_DIR" \
    python "${SCRIPT_DIR}/backend/app.py" \
    --host "$host" \
    --port "$port" \
    --browse-root "$data_root" \
    --token="$token" \
    >"$LOG_FILE" 2>&1 &

  pid=$!
  printf '%s\n' "$pid" >"$PID_FILE"
  printf '%s\n' "$url" >"$URL_FILE"

  if ! python3 - "$host" "$port" "$pid" <<'PY'
import os
import socket
import sys
import time

host, port, pid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
connect_host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
for _ in range(480):
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit(1)
    try:
        with socket.create_connection((connect_host, port), timeout=0.3):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.25)
raise SystemExit(1)
PY
  then
    printf '%s\n\n' "${RED}failed${RESET}"
    fail "Server did not come up. Last 40 log lines:"
    tail -n 40 "$LOG_FILE" >&2 || true
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE" "$URL_FILE"
    return 1
  fi

  printf '%s\n' "${GREEN}done${RESET} ${DIM}(pid ${pid}, $((SECONDS - start_ts))s)${RESET}"
  info ""
  ok "Embodit is up. Open this URL in your browser:"
  info ""
  info "  ${BOLD}${url}${RESET}"
  info ""
  info "${DIM}The token is exchanged for a browser cookie on first visit;${RESET}"
  info "${DIM}afterwards http://${public_host}:${port}/ works without it.${RESET}"
  info ""
  info "  Stop : bash ${SCRIPT_DIR}/embodit.sh stop"
  info "  Logs : bash ${SCRIPT_DIR}/embodit.sh logs -f"
}

stop_service() {
  if (( $# > 0 )); then
    fail "stop does not accept arguments."
    return 2
  fi

  local stopped=0 pid
  if pid="$(running_pid)"; then
    printf 'Stopping Embodit (pid %s) ... ' "$pid"
    kill "$pid"
    for _ in $(seq 1 50); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
      printf '%s\n' "${GREEN}done${RESET} ${DIM}(forced)${RESET}"
    else
      printf '%s\n' "${GREEN}done${RESET}"
    fi
    stopped=1
  fi
  rm -f "$PID_FILE" "$URL_FILE"

  # Also reap instances not tracked by the PID file (for example after a crash).
  if pkill -f "${SCRIPT_DIR}/backend/app.py" 2>/dev/null; then
    printf 'Cleaning up stray instances ... '
    sleep 1
    pkill -9 -f "${SCRIPT_DIR}/backend/app.py" 2>/dev/null || true
    printf '%s\n' "${GREEN}done${RESET}"
    stopped=1
  fi

  if (( stopped == 1 )); then
    ok "Embodit stopped."
  else
    info "Embodit is not running."
  fi
}

show_status() {
  if (( $# > 0 )); then
    fail "status does not accept arguments."
    return 2
  fi

  local pid
  if pid="$(running_pid)"; then
    ok "Embodit is running (pid ${pid})."
    info "  URL: $(cat "$URL_FILE" 2>/dev/null || printf 'unknown')"
    info "  Log: ${LOG_FILE}"
    return 0
  fi

  info "Embodit is not running."
  return 1
}

show_logs() {
  local lines=50
  if (( $# > 1 )); then
    fail "logs accepts only a line count or -f/--follow."
    return 2
  fi
  if (( $# == 1 )); then
    case "$1" in
      -f|--follow)
        touch "$LOG_FILE"
        tail -n "$lines" -f "$LOG_FILE"
        return
        ;;
      *) lines="$1" ;;
    esac
  fi
  if [[ ! "$lines" =~ ^[0-9]+$ ]]; then
    fail "Log line count must be a non-negative integer (got: ${lines})."
    return 2
  fi
  if [[ ! -f "$LOG_FILE" ]]; then
    info "No service log exists yet: ${LOG_FILE}"
    return 0
  fi
  tail -n "$lines" "$LOG_FILE"
}

clean_cache() {
  local mode="auto" dry_run=0 arg
  for arg in "$@"; do
    case "$arg" in
      --expired|--auto) mode="auto" ;;
      --cache) mode="cache" ;;
      --all) mode="all" ;;
      --dry-run) dry_run=1 ;;
      -h|--help)
        cat <<'EOF'
Usage: bash embodit.sh clean [--expired|--cache|--all] [--dry-run]

  --expired   Apply the configured retention policy (default).
  --cache     Remove all reproducible previews, media, and SAM caches.
  --all       Remove cache, job history, logs, and QC reports under the cache root.
  --dry-run   Print candidates without deleting or migrating anything.

Stop the service before a real cleanup. Dataset outputs, labels, review files,
the Python environment, service token, and service log are never removed.
EOF
        return 0
        ;;
      *)
        fail "Unknown clean option: ${arg}"
        return 2
        ;;
    esac
  done

  if (( dry_run == 0 )) && running_pid >/dev/null 2>&1; then
    fail "Stop Embodit before cleaning managed files: bash ${SCRIPT_DIR}/embodit.sh stop"
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 is required to manage the cache."
    return 1
  fi
  local args=("${SCRIPT_DIR}/backend/cache_manager.py" "$mode")
  if (( dry_run == 1 )); then
    args+=("--dry-run")
  fi
  python3 "${args[@]}"
}

command="${1:-help}"
if (( $# > 0 )); then
  shift
fi

case "$command" in
  start) start_service "$@" ;;
  stop) stop_service "$@" ;;
  restart)
    if (( $# > 1 )); then
      fail "restart accepts at most one DATA_ROOT argument."
      exit 2
    fi
    if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
      usage
      exit 0
    fi
    stop_service
    start_service "$@"
    ;;
  status) show_status "$@" ;;
  logs|log) show_logs "$@" ;;
  clean) clean_cache "$@" ;;
  help|-h|--help) usage ;;
  *)
    fail "Unknown command: ${command}"
    info ""
    usage >&2
    exit 2
    ;;
esac
