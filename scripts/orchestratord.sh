#!/usr/bin/env bash
#
# Control script for the scraper orchestrator daemon.
#
#   scripts/orchestratord.sh start     # launch in background (nohup)
#   scripts/orchestratord.sh stop      # graceful stop (TERM, then KILL)
#   scripts/orchestratord.sh restart   # stop then start
#   scripts/orchestratord.sh status    # is it running + due-check table
#
# State lives under var/ (gitignored):
#   var/orchestrator.pid   - PID of the running daemon
#   var/orchestrator.log   - daemon log (written by the orchestrator)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAR_DIR="$REPO_ROOT/var"
PID_FILE="$VAR_DIR/orchestrator.pid"
# The orchestrator's own file logger owns orchestrator.log. nohup output
# goes to a separate boot log so we still capture crashes that happen
# before logging is configured (e.g. import errors) without duplicating
# every line into orchestrator.log.
LOG_FILE="$VAR_DIR/orchestrator.log"
BOOT_LOG="$VAR_DIR/orchestrator.boot.log"
# Prefer the project venv interpreter; fall back to system python3.
if [ -n "${PYTHON:-}" ]; then
  :
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

mkdir -p "$VAR_DIR"

# Echo the PID of the running daemon, or nothing if it is not running.
running_pid() {
  [ -f "$PID_FILE" ] || return 0
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
  fi
}

cmd_start() {
  local pid
  pid="$(running_pid)"
  if [ -n "$pid" ]; then
    echo "already running (pid $pid)"
    return 0
  fi
  # Stale pidfile (process gone) — clean it up before relaunching.
  [ -f "$PID_FILE" ] && rm -f "$PID_FILE"

  cd "$REPO_ROOT"
  nohup "$PYTHON" -m src.orchestrator "$@" >>"$BOOT_LOG" 2>&1 &
  pid=$!
  echo "$pid" >"$PID_FILE"

  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    echo "started (pid $pid); logging to $LOG_FILE"
  else
    rm -f "$PID_FILE"
    echo "failed to start; last boot-log lines:" >&2
    tail -n 20 "$BOOT_LOG" >&2 2>/dev/null || true
    return 1
  fi
}

cmd_stop() {
  local pid
  pid="$(running_pid)"
  if [ -z "$pid" ]; then
    echo "not running"
    rm -f "$PID_FILE"
    return 0
  fi
  echo "stopping pid $pid ..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "did not stop gracefully; sending KILL"
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "stopped"
}

cmd_status() {
  local pid
  pid="$(running_pid)"
  if [ -n "$pid" ]; then
    local started
    started="$(ps -o lstart= -p "$pid" 2>/dev/null || echo '?')"
    echo "daemon: RUNNING (pid $pid, since$started)"
  elif [ -f "$PID_FILE" ]; then
    echo "daemon: NOT RUNNING (stale pidfile $PID_FILE)"
  else
    echo "daemon: NOT RUNNING"
  fi

  echo
  echo "scraper due-check:"
  cd "$REPO_ROOT"
  "$PYTHON" -m src.orchestrator --list 2>/dev/null || echo "  (could not read state)"

  if [ -f "$LOG_FILE" ]; then
    echo
    echo "recent log ($LOG_FILE):"
    tail -n 12 "$LOG_FILE" | sed 's/^/  /'
  fi
}

action="${1:-}"
[ $# -gt 0 ] && shift || true

case "$action" in
  start)   cmd_start "$@" ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start "$@" ;;
  status)  cmd_status ;;
  *)
    echo "usage: $0 {start|stop|restart|status} [-- orchestrator args]" >&2
    exit 2
    ;;
esac
