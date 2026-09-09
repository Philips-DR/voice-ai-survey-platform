#!/usr/bin/env bash
# agent.sh — Manage the Demeter LiveKit survey agent
#
# Usage:
#   ./scripts/agent.sh dev                   hot-reload, connects to Agents Playground
#   ./scripts/agent.sh start                 production mode (daemonised)
#   ./scripts/agent.sh stop
#   ./scripts/agent.sh restart
#   ./scripts/agent.sh status
#   ./scripts/agent.sh logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/agent"
PID_FILE="$PROJECT_DIR/run/agent.pid"
LOG_FILE="$PROJECT_DIR/logs/agent.log"
VENV="$AGENT_DIR/.venv"
PYTHON="$VENV/bin/python"

# ── Helpers ───────────────────────────────────────────────────────────────────
die()  { echo "ERROR: $*" >&2; exit 1; }
pid()  { [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""; }
live() { local p; p="$(pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

# Kill a process and all its descendants (forkserver + job subprocesses)
kill_tree() {
  local root=$1 sig=${2:-TERM}
  local children
  children=$(pgrep -P "$root" 2>/dev/null || true)
  for child in $children; do
    kill_tree "$child" "$sig"
  done
  kill "-$sig" "$root" 2>/dev/null || true
}

# Kill all processes that match the agent script path (catches strays)
kill_strays() {
  local sig=${1:-TERM}
  pkill "-$sig" -f "agent/.venv.*agent\.py" 2>/dev/null || true
}

load_env() {
  local env="$PROJECT_DIR/.env"
  [[ -f "$env" ]] && { set -a; source "$env"; set +a; }
}

check_venv() {
  [[ -f "$PYTHON" ]] || die "venv not found — run: cd agent && uv venv && uv pip install livekit-agents livekit-plugins-silero httpx python-dotenv pydantic"
}

# ── dev ───────────────────────────────────────────────────────────────────────
cmd_dev() {
  check_venv
  kill_strays  # clean up any leftover procs before starting
  load_env
  cd "$AGENT_DIR"
  echo "Agent dev mode — connect via LiveKit Agents Playground"
  exec "$PYTHON" agent.py dev
}

# ── start (daemonised) ────────────────────────────────────────────────────────
cmd_start() {
  check_venv

  if live; then
    echo "Already running (PID $(pid)). Use restart to reload."
    exit 1
  fi
  [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

  mkdir -p "$PROJECT_DIR/run" "$PROJECT_DIR/logs"
  load_env
  cd "$AGENT_DIR"

  echo "Starting agent (production)..."
  nohup "$PYTHON" agent.py start >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  sleep 2
  live || die "Agent failed to start — check $LOG_FILE"
  echo "Started. PID $(pid)"
}

# ── stop ──────────────────────────────────────────────────────────────────────
cmd_stop() {
  local p; p="$(pid)"
  local any_running=false

  { [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && any_running=true; } || true
  { pgrep -f "agent/.venv.*agent\.py" > /dev/null 2>&1 && any_running=true; } || true

  if [[ "$any_running" == false ]]; then
    echo "Not running."
    rm -f "$PID_FILE" 2>/dev/null || true
    return 0
  fi

  echo "Stopping agent (PID ${p:-unknown})..."

  # Gracefully kill the process tree (main → forkserver → job subprocesses)
  [[ -n "$p" ]] && kill_tree "$p" TERM
  kill_strays TERM

  # Wait up to 10 s for everything to exit
  for _ in $(seq 1 10); do
    if ! pgrep -f "agent/.venv.*agent\.py" > /dev/null 2>&1; then
      rm -f "$PID_FILE"; echo "Stopped."; return
    fi
    sleep 1
  done

  # Force-kill anything still alive
  echo "Timeout — force-killing..."
  [[ -n "$p" ]] && kill_tree "$p" KILL
  kill_strays KILL
  rm -f "$PID_FILE"
  echo "Killed."
}

# ── restart ───────────────────────────────────────────────────────────────────
cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

# ── status ────────────────────────────────────────────────────────────────────
cmd_status() {
  echo "══════════════════════════════════════"
  echo "  Demeter Survey Agent — Status"
  echo "══════════════════════════════════════"
  if live; then
    local p; p="$(pid)"
    local started; started=$(ps -o lstart= -p "$p" 2>/dev/null | xargs || true)
    echo "  Status:  RUNNING"
    echo "  PID:     $p"
    [[ -n "$started" ]] && echo "  Since:   $started"
  else
    echo "  Status:  STOPPED"
    [[ -f "$PID_FILE" ]] && { rm -f "$PID_FILE"; echo "  (stale PID file removed)"; }
  fi
  echo ""
  echo "  Log: $LOG_FILE"
  if [[ -f "$LOG_FILE" && -s "$LOG_FILE" ]]; then
    echo ""
    echo "── Last 10 lines ──────────────────────"
    tail -10 "$LOG_FILE"
  fi
}

# ── logs ──────────────────────────────────────────────────────────────────────
cmd_logs() {
  [[ -f "$LOG_FILE" ]] || { echo "No log file yet."; exit 0; }
  exec tail -f "$LOG_FILE"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
CMD="${1:-}"; shift || true
case "$CMD" in
  dev)     cmd_dev     ;;
  start)   cmd_start   ;;
  stop)    cmd_stop    ;;
  restart) cmd_restart ;;
  status)  cmd_status  ;;
  logs)    cmd_logs    ;;
  *)
    echo "Usage: $(basename "$0") <command>"
    echo ""
    echo "  dev      development mode — hot reload, Agents Playground"
    echo "  start    production mode (daemonised, logs to logs/agent.log)"
    echo "  stop     graceful shutdown"
    echo "  restart  stop then start"
    echo "  status   PID, uptime, last log lines"
    echo "  logs     tail logs/agent.log"
    exit 1 ;;
esac
