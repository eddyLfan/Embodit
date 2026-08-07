#!/usr/bin/env bash
# Embodit service manager — requires Python >= 3.10 and uv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${EMBODIT_STATE_DIR:-${SCRIPT_DIR}/.embodit}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

migrate_state_file() {
  local legacy_path="$1"
  local state_path="$2"
  if [[ ! -e "$state_path" && -e "$legacy_path" ]]; then
    mv "$legacy_path" "$state_path"
  fi
}

# Keep upgrades compatible with the former root/config runtime layout.
migrate_state_file "${SCRIPT_DIR}/service.pid" "${STATE_DIR}/service.pid"
migrate_state_file "${SCRIPT_DIR}/service.url" "${STATE_DIR}/service.url"
migrate_state_file "${SCRIPT_DIR}/service.log" "${STATE_DIR}/service.log"
migrate_state_file "${SCRIPT_DIR}/config/token" "${STATE_DIR}/token"

PID_FILE="${STATE_DIR}/service.pid"
URL_FILE="${STATE_DIR}/service.url"
LOG_FILE="${STATE_DIR}/service.log"
TOKEN_FILE="${STATE_DIR}/token"
ENV_STAMP_FILE="${STATE_DIR}/environment.sha256"

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
  setup                Install all dependencies without starting the service
  stop                 Stop Embodit and clean up stray instances
  restart [DATA_ROOT]  Restart Embodit
  status               Show whether Embodit is running
  logs [LINES]         Show recent logs (default: 50 lines)
  logs -f              Follow the service log
  clean [MODE]         Clean managed files: expired (default), --cache, or --all
  recipe-compose ROBOT MODEL  Compose robot/model configs into a Recipe
  recipe-validate FILE Validate a deployment Recipe
  recipe-run FILE      Start and monitor a deployment Recipe
  recipe-stop FILE     Stop services declared by a deployment Recipe
  help                 Show this help

Examples:
  bash embodit.sh start ~/datasets
  bash embodit.sh setup
  EMBODIT_PYPI_MIRROR=tsinghua bash embodit.sh setup
  EMBODY_PORT=9000 bash embodit.sh start /data/lerobot
  bash embodit.sh status
  bash embodit.sh logs -f
  bash embodit.sh stop
  bash embodit.sh clean --dry-run
  bash embodit.sh clean --cache
  bash embodit.sh recipe-compose config/deployment/robot.example.json config/deployment/models/python.example.json --output /tmp/my-deployment.json
  bash embodit.sh recipe-validate config/deployment/recipe.example.json
  bash embodit.sh recipe-run config/deployment/recipe.example.json --mode dry_run
  bash embodit.sh recipe-stop config/deployment/recipe.example.json

Optional env vars: EMBODY_ROOT, EMBODY_HOST, EMBODY_PORT,
  EMBODY_PUBLIC_HOST, EMBODY_TOKEN, EMBODY_PROXY, EMBODIT_SANDBOX,
  EMBODIT_PYPI_MIRROR, EMBODIT_CACHE_DIR, EMBODIT_STATE_DIR,
  EMBODIT_REVIEW_CONFIG,
  AUGMENT_PYTHON, AUGMENT_SAM3_CHECKPOINT
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

environment_fingerprint() {
  python3 - "${SCRIPT_DIR}/pyproject.toml" "${SCRIPT_DIR}/uv.lock" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
for name in sys.argv[1:]:
    with open(name, "rb") as handle:
        digest.update(handle.read())
print(digest.hexdigest())
PY
}

configure_uv_network() {
  local mirror="${EMBODIT_PYPI_MIRROR:-}" proxy="${EMBODY_PROXY:-${LEROBOT_PROXY:-}}"
  if [[ -n "$mirror" ]]; then
    case "$mirror" in
      tsinghua|tuna)
        export UV_DEFAULT_INDEX="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
        ;;
      official|pypi)
        export UV_DEFAULT_INDEX="https://pypi.org/simple"
        ;;
      http://*|https://*)
        export UV_DEFAULT_INDEX="$mirror"
        ;;
      *)
        fail "EMBODIT_PYPI_MIRROR must be tsinghua, official, or an HTTP(S) Simple Index URL."
        return 2
        ;;
    esac
    # The project-specific setting is explicit and therefore takes precedence
    # over a legacy UV_INDEX_URL inherited from the shell.
    unset UV_INDEX_URL
  fi
  if [[ -n "$proxy" ]]; then
    export http_proxy="$proxy" https_proxy="$proxy"
    export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
  fi
}

environment_ready() {
  local fingerprint stamp_fingerprint
  [[ -x "${SCRIPT_DIR}/.venv/bin/python" && -s "$ENV_STAMP_FILE" ]] || return 1
  fingerprint="$(environment_fingerprint)"
  read -r stamp_fingerprint <"$ENV_STAMP_FILE" || return 1
  [[ "$stamp_fingerprint" == "$fingerprint" ]]
}

write_environment_stamp() {
  local fingerprint="$1" temporary="${ENV_STAMP_FILE}.tmp.$$"
  printf '%s\n' "$fingerprint" >"$temporary"
  mv "$temporary" "$ENV_STAMP_FILE"
}

sync_environment() {
  local fingerprint requirements_file=""
  if environment_ready; then
    return 0
  fi
  configure_uv_network
  fingerprint="$(environment_fingerprint)"
  info "Preparing environment from uv.lock ..."
  if [[ -n "${UV_DEFAULT_INDEX:-}" ]]; then
    info "Package index: ${UV_DEFAULT_INDEX}"
  elif [[ -n "${UV_INDEX_URL:-}" ]]; then
    info "Package index: ${UV_INDEX_URL}"
  fi
  if [[ -n "${UV_DEFAULT_INDEX:-}${UV_INDEX_URL:-}" ]]; then
    requirements_file="$(mktemp "${TMPDIR:-/tmp}/embodit-requirements.XXXXXX")"
    if ! (cd "$SCRIPT_DIR" && uv export --frozen --no-dev \
      --format requirements-txt --no-emit-project >"$requirements_file"); then
      rm -f "$requirements_file"
      return 1
    fi
    if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
      if ! uv venv --python python3 "${SCRIPT_DIR}/.venv"; then
        rm -f "$requirements_file"
        return 1
      fi
    fi
    if ! uv pip sync --python "${SCRIPT_DIR}/.venv/bin/python" "$requirements_file"; then
      rm -f "$requirements_file"
      return 1
    fi
    rm -f "$requirements_file"
  else
    (cd "$SCRIPT_DIR" && uv sync --frozen --no-dev)
  fi
  write_environment_stamp "$fingerprint"
}

setup_environment() {
  if (( $# > 0 )); then
    fail "setup does not accept arguments."
    return 2
  fi
  if ! command -v uv >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    fail "setup requires uv and python3 on PATH."
    return 1
  fi
  sync_environment
  ok "Embodit environment is ready."
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
  local token url pid start_ts sandbox_notice=""

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

  # A non-loopback listener is reachable by other machines. Unless the user
  # explicitly chooses otherwise, confine every client-supplied path to the
  # selected data root so a leaked token cannot expose the whole host.
  if [[ -z "${EMBODIT_SANDBOX+x}" ]]; then
    case "$host" in
      127.0.0.1|localhost|::1) ;;
      *)
        export EMBODIT_SANDBOX=1
        sandbox_notice="enabled automatically for non-loopback access"
        ;;
    esac
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
  if [[ -z "$token" && -s "$TOKEN_FILE" ]]; then
    token="$(head -n1 "$TOKEN_FILE" | tr -d '[:space:]')"
  fi
  if [[ -z "$token" ]]; then
    token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  fi
  printf '%s\n' "$token" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
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
  if [[ -n "$sandbox_notice" ]]; then
    info "  Path guard: ${sandbox_notice} (${data_root})"
  elif [[ "${EMBODIT_SANDBOX:-}" =~ ^(1|true|yes)$ ]]; then
    info "  Path guard: enabled (${data_root})"
  elif [[ "$host" != "127.0.0.1" && "$host" != "localhost" && "$host" != "::1" ]]; then
    fail "  WARNING: Path guard explicitly disabled on non-loopback listener ${host}"
    fail "           Authenticated clients can access paths allowed by this service account."
  else
    info "  Path guard: disabled (local trusted use)"
  fi
  info "  Log file  : ${LOG_FILE}"
  info ""

  if sync_environment; then
    ok "Environment is ready."
  else
    fail "uv sync failed. Check network / proxy (EMBODY_PROXY) and retry."
    return 1
  fi

  printf 'Starting server ... '
  start_ts=$SECONDS
  nohup uv run --no-sync --project "$SCRIPT_DIR" \
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
    if environment_ready; then
      info "  Environment: ready"
    else
      info "  Environment: dependencies changed; restart or run 'bash embodit.sh setup'"
    fi
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

run_recipe_cli() {
  local subcommand="$1"
  shift
  if (( $# < 1 )); then
    fail "recipe-${subcommand} requires a Recipe path."
    return 2
  fi
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv is required to create the Embodit environment."
    return 1
  fi
  sync_environment
  "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/backend/deploy/cli.py" "$subcommand" "$@"
}

command="${1:-help}"
if (( $# > 0 )); then
  shift
fi

case "$command" in
  start) start_service "$@" ;;
  setup) setup_environment "$@" ;;
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
  recipe-compose) run_recipe_cli compose "$@" ;;
  recipe-validate) run_recipe_cli validate "$@" ;;
  recipe-run) run_recipe_cli run "$@" ;;
  recipe-stop) run_recipe_cli stop "$@" ;;
  help|-h|--help) usage ;;
  *)
    fail "Unknown command: ${command}"
    info ""
    usage >&2
    exit 2
    ;;
esac
