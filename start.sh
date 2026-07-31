#!/usr/bin/env bash
# Embodit launcher — requires: Python >= 3.10 and uv (https://github.com/astral-sh/uv)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT="${1:-${EMBODY_ROOT:-${LEROBOT_ROOT:-}}}"
HOST="${EMBODY_HOST:-${LEROBOT_HOST:-127.0.0.1}}"
PORT="${EMBODY_PORT:-${LEROBOT_PORT:-8765}}"
# Empty by default (open-source friendly). Set EMBODY_PROXY if you need a proxy.
PROXY="${EMBODY_PROXY:-${LEROBOT_PROXY:-}}"
PUBLIC_HOST="${EMBODY_PUBLIC_HOST:-${LEROBOT_PUBLIC_HOST:-localhost}}"
PID_FILE="${SCRIPT_DIR}/service.pid"
URL_FILE="${SCRIPT_DIR}/service.url"
LOG_FILE="${SCRIPT_DIR}/service.log"

# ---- terminal helpers -------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD="$(tput bold)" DIM="$(tput dim)" GREEN="$(tput setaf 2)" RED="$(tput setaf 1)" RESET="$(tput sgr0)"
else
  BOLD="" DIM="" GREEN="" RED="" RESET=""
fi
info()  { printf '%s\n' "$*"; }
ok()    { printf '%s\n' "${GREEN}$*${RESET}"; }
fail()  { printf '%s\n' "${RED}$*${RESET}" >&2; }

usage() {
  cat <<EOF
Usage: bash start.sh [DATA_ROOT]

  DATA_ROOT   Directory the browser starts in (default: current directory)

Examples:
  bash start.sh ~/datasets
  EMBODY_PORT=9000 bash start.sh /data/lerobot

Optional env vars: EMBODY_HOST, EMBODY_PORT, EMBODY_PUBLIC_HOST,
  EMBODY_TOKEN, EMBODY_PROXY, EMBODIT_SANDBOX, AUGMENT_SAM3_CHECKPOINT
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "$DATA_ROOT" ]]; then
  DATA_ROOT="$(pwd)"
  info "${DIM}No data root given; browsing from current directory: ${DATA_ROOT}${RESET}"
fi
DATA_ROOT="$(cd "$DATA_ROOT" 2>/dev/null && pwd || echo "$DATA_ROOT")"

# ---- prerequisites ----------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  fail "uv is not installed (required to manage the Python environment)."
  fail "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  fail "Docs:    https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required on PATH."
  exit 1
fi

# ---- already running? -------------------------------------------------------
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  ok "Embodit is already running (pid $(cat "$PID_FILE"))."
  info "  URL: $(cat "$URL_FILE" 2>/dev/null || echo "unknown")"
  exit 0
fi

rm -f "$PID_FILE" "$URL_FILE"

# Refuse to start when the port is already occupied by a stray instance,
# otherwise the freshly generated token will not match the old listener.
if python3 - "$HOST" "$PORT" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
connect_host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
try:
    with socket.create_connection((connect_host, port), timeout=0.3):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
then
  fail "Port ${PORT} is already in use (a stray Embodit instance may still be running)."
  fail "Run ${SCRIPT_DIR}/stop.sh first, or: pkill -f '${SCRIPT_DIR}/backend/app.py'"
  exit 1
fi

# Persist the token across restarts so old URLs / browser cookies stay valid.
TOKEN_FILE="${SCRIPT_DIR}/config/token"
TOKEN="${EMBODY_TOKEN:-${LEROBOT_TOKEN:-}}"
if [[ -z "$TOKEN" && -s "$TOKEN_FILE" ]]; then
  TOKEN="$(head -n1 "$TOKEN_FILE" | tr -d '[:space:]')"
fi
if [[ -z "$TOKEN" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
mkdir -p "${SCRIPT_DIR}/config"
printf '%s\n' "$TOKEN" >"$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
# The token must ride on the first visit: the server exchanges it for an
# HttpOnly cookie and the frontend scrubs it from the address bar.
URL="http://${PUBLIC_HOST}:${PORT}/?token=${TOKEN}"

if [[ -n "$PROXY" ]]; then
  export http_proxy="$PROXY"
  export https_proxy="$PROXY"
  export HTTP_PROXY="$PROXY"
  export HTTPS_PROXY="$PROXY"
fi
export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost"

info "${BOLD}Embodit · Embodied Intelligence Toolkit${RESET}"
info "${DIM}----------------------------------------${RESET}"
info "  Data root : ${DATA_ROOT}"
info "  Listen on : http://${HOST}:${PORT}"
info "  Log file  : ${LOG_FILE}"
info ""

# Sync project deps from pyproject.toml / uv.lock into .venv (fast when up to date).
printf 'Syncing environment ... '
if (cd "$SCRIPT_DIR" && uv sync --quiet); then
  printf '%s\n' "${GREEN}done${RESET}"
else
  printf '%s\n' "${RED}failed${RESET}"
  fail "uv sync failed. Check network / proxy (EMBODY_PROXY) and retry."
  exit 1
fi

printf 'Starting server ... '
START_TS=$SECONDS
nohup uv run --project "$SCRIPT_DIR" \
  python "${SCRIPT_DIR}/backend/app.py" \
  --host "$HOST" \
  --port "$PORT" \
  --browse-root "$DATA_ROOT" \
  --token="$TOKEN" \
  >"$LOG_FILE" 2>&1 &

PID=$!
echo "$PID" >"$PID_FILE"
echo "$URL" >"$URL_FILE"

if ! python3 - "$HOST" "$PORT" "$PID" <<'PY'
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
  kill "$PID" 2>/dev/null || true
  rm -f "$PID_FILE" "$URL_FILE"
  exit 1
fi

printf '%s\n' "${GREEN}done${RESET} ${DIM}(pid ${PID}, $((SECONDS - START_TS))s)${RESET}"
info ""
ok "Embodit is up. Open this URL in your browser:"
info ""
info "  ${BOLD}${URL}${RESET}"
info ""
info "${DIM}The token is exchanged for a browser cookie on first visit;${RESET}"
info "${DIM}afterwards http://${PUBLIC_HOST}:${PORT}/ works without it.${RESET}"
info ""
info "  Stop : ${SCRIPT_DIR}/stop.sh"
info "  Logs : tail -f ${LOG_FILE}"
