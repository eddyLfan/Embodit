#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT="${1:-${EMBODY_ROOT:-${LEROBOT_ROOT:-/media/DATA}}}"
HOST="${EMBODY_HOST:-${LEROBOT_HOST:-127.0.0.1}}"
PORT="${EMBODY_PORT:-${LEROBOT_PORT:-8765}}"
PROXY="${EMBODY_PROXY:-${LEROBOT_PROXY:-http://192.168.32.28:18000}}"
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
printf 'Starting server (first run may take a while to resolve dependencies) ... '

START_TS=$SECONDS
nohup uv run \
  --with pyarrow \
  --with numpy \
  --with h5py \
  --with mcap \
  --with imageio-ffmpeg \
  --with fastapi \
  --with uvicorn \
  --with pydantic \
  --with av \
  --with opencv-python-headless \
  --with pillow \
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
