#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/service.pid"

if [[ -t 1 ]]; then
  GREEN="$(tput setaf 2)" DIM="$(tput dim)" RESET="$(tput sgr0)"
else
  GREEN="" DIM="" RESET=""
fi

STOPPED=0

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    printf 'Stopping Embodit (pid %s) ... ' "$PID"
    kill "$PID"
    for _ in $(seq 1 50); do
      if ! kill -0 "$PID" 2>/dev/null; then break; fi
      sleep 0.1
    done
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
      printf '%s\n' "${GREEN}done${RESET} ${DIM}(forced)${RESET}"
    else
      printf '%s\n' "${GREEN}done${RESET}"
    fi
    STOPPED=1
  fi
  rm -f "$PID_FILE" "${SCRIPT_DIR}/service.url"
fi

# Also reap stray instances not tracked by the pid file (e.g. started twice).
if pkill -f "${SCRIPT_DIR}/backend/app.py" 2>/dev/null; then
  printf 'Cleaning up stray instances ... '
  sleep 1
  # Some workers ignore SIGTERM while blocked in native code; escalate.
  pkill -9 -f "${SCRIPT_DIR}/backend/app.py" 2>/dev/null || true
  printf '%s\n' "${GREEN}done${RESET}"
  STOPPED=1
fi

if [[ "$STOPPED" == "1" ]]; then
  printf '%s\n' "${GREEN}Embodit stopped.${RESET}"
else
  printf '%s\n' "Embodit is not running."
fi
