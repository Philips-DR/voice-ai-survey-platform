#!/usr/bin/env bash
# server.sh — Manage the AgriCo SeedX IVR server
#
# Usage:
#   ./scripts/server.sh setup                       one-time HTTPS + nginx setup
#   ./scripts/server.sh start   [--port N] [--workers N]
#   ./scripts/server.sh stop
#   ./scripts/server.sh restart [--port N] [--workers N]
#   ./scripts/server.sh status
#   ./scripts/server.sh logs    [access|error]      (default: error)
#   ./scripts/server.sh dev     [--port N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/run/server.pid"
ACCESS_LOG="$PROJECT_DIR/logs/access.log"
ERROR_LOG="$PROJECT_DIR/logs/error.log"
NGINX_CONF_SRC="$SCRIPT_DIR/nginx-ivr-survey.conf"
NGINX_CONF_DST="/etc/nginx/conf.d/ivr-survey-demo.conf"
CERT="$PROJECT_DIR/run/selfsigned.crt"
KEY="$PROJECT_DIR/run/selfsigned.key"

PORT="${PORT:-8000}"
PUBLIC_IP="${PUBLIC_IP:-your.server.ip.here}"
HOST="${HOST:-0.0.0.0}"
WORKERS="${WORKERS:-$(( $(nproc 2>/dev/null || echo 2) * 2 + 1 ))}"

# ── Helpers ───────────────────────────────────────────────────────────────────
die()  { echo "ERROR: $*" >&2; exit 1; }
pid()  { [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""; }
live() { local p; p="$(pid)"; [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null; }

load_env() {
  local env="$PROJECT_DIR/.env"
  [[ -f "$env" ]] && { set -a; source "$env"; set +a; }
}

# ── setup: one-time HTTPS + nginx config ─────────────────────────────────────
cmd_setup() {
  mkdir -p "$PROJECT_DIR/run" "$PROJECT_DIR/logs" "$PROJECT_DIR/sessions"

  # Generate self-signed cert if missing
  if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
    echo "Generating self-signed TLS certificate for $PUBLIC_IP..."
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
      -keyout "$KEY" -out "$CERT" \
      -subj "/CN=$PUBLIC_IP" 2>/dev/null
    echo "  Certificate: $CERT"
  else
    echo "Certificate already exists — skipping generation."
  fi

  # Install nginx config
  echo "Installing nginx config → $NGINX_CONF_DST"
  sudo cp "$NGINX_CONF_SRC" "$NGINX_CONF_DST"

  # Test and reload nginx
  sudo nginx -t 2>/dev/null || die "nginx config test failed"
  sudo nginx -s reload 2>/dev/null || sudo systemctl start nginx
  echo "nginx reloaded."

  echo ""
  echo "Setup complete."
  echo "  Demo URL: https://$PUBLIC_IP"
  echo "  Note: browser will warn about the self-signed cert."
  echo "  Click Advanced → Proceed to $PUBLIC_IP to accept it."
  echo "  After accepting, microphone access will work."
}

# ── start ─────────────────────────────────────────────────────────────────────
cmd_start() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --port)    PORT="$2";    shift 2 ;;
      --workers) WORKERS="$2"; shift 2 ;;
      *) die "Unknown flag: $1" ;;
    esac
  done

  command -v gunicorn &>/dev/null || die "gunicorn not found — run: pip install -r requirements.txt"

  if live; then
    echo "Already running (PID $(pid)). Use restart to reload."
    exit 1
  fi
  [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

  mkdir -p "$PROJECT_DIR/run" "$PROJECT_DIR/logs" "$PROJECT_DIR/sessions"
  load_env
  cd "$PROJECT_DIR"

  echo "Starting...  https://$PUBLIC_IP  (backend on $HOST:$PORT, $WORKERS workers)"

  gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind "$HOST:$PORT" \
    --pid "$PID_FILE" \
    --access-logfile "$ACCESS_LOG" \
    --error-logfile  "$ERROR_LOG" \
    --log-level info \
    --timeout 120 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --daemon

  sleep 1
  live || die "Server failed to start — check $ERROR_LOG"
  echo "Started. PID $(pid)  →  https://$PUBLIC_IP"
}

# ── stop ──────────────────────────────────────────────────────────────────────
cmd_stop() {
  local p; p="$(pid)"
  if [[ -z "$p" ]]; then echo "Not running."; return; fi
  if ! live; then echo "Not running (stale PID). Cleaning up."; rm -f "$PID_FILE"; return; fi

  echo "Stopping (PID $p)..."
  kill -TERM "$p"
  for _ in $(seq 1 15); do
    live || { rm -f "$PID_FILE"; echo "Stopped."; return; }
    sleep 1
  done
  echo "Timeout — sending KILL..."
  kill -KILL "$p" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Killed."
}

# ── restart ───────────────────────────────────────────────────────────────────
cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start "$@"
}

# ── status ────────────────────────────────────────────────────────────────────
cmd_status() {
  echo "══════════════════════════════════════"
  echo "  AgriCo SeedX IVR — Server Status"
  echo "══════════════════════════════════════"
  if live; then
    local p; p="$(pid)"
    local workers; workers=$(pgrep -P "$p" 2>/dev/null | wc -l | tr -d ' ')
    local started; started=$(ps -o lstart= -p "$p" 2>/dev/null | xargs || true)
    echo "  Status:  RUNNING"
    echo "  PID:     $p  ($workers workers)"
    echo "  URL:     https://$PUBLIC_IP"
    [[ -n "$started" ]] && echo "  Since:   $started"
  else
    echo "  Status:  STOPPED"
    [[ -f "$PID_FILE" ]] && { rm -f "$PID_FILE"; echo "  (stale PID file removed)"; }
  fi
  echo ""
  # nginx status
  if [[ -f "$NGINX_CONF_DST" ]]; then
    echo "  nginx:   configured (https on :443 → :$PORT)"
  else
    echo "  nginx:   not configured — run: ./scripts/server.sh setup"
  fi
  echo ""
  echo "  Logs:"
  echo "    Error:  $ERROR_LOG"
  echo "    Access: $ACCESS_LOG"
  if [[ -f "$ERROR_LOG" && -s "$ERROR_LOG" ]]; then
    echo ""
    echo "── Last 10 error lines ────────────────"
    tail -10 "$ERROR_LOG"
  fi
}

# ── logs ──────────────────────────────────────────────────────────────────────
cmd_logs() {
  local which="${1:-error}"
  case "$which" in
    access) exec tail -f "$ACCESS_LOG" ;;
    error)  exec tail -f "$ERROR_LOG"  ;;
    *) die "Usage: logs [access|error]" ;;
  esac
}

# ── dev ───────────────────────────────────────────────────────────────────────
cmd_dev() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --port) PORT="$2"; shift 2 ;;
      *) die "Unknown flag: $1" ;;
    esac
  done
  command -v uvicorn &>/dev/null || die "uvicorn not found — run: pip install -r requirements.txt"
  load_env
  cd "$PROJECT_DIR"
  echo "Dev server → http://localhost:$PORT  (hot reload, no HTTPS)"
  exec uvicorn main:app \
    --host 127.0.0.1 --port "$PORT" \
    --reload \
    --reload-dir "$PROJECT_DIR/survey" \
    --reload-dir "$PROJECT_DIR/routers" \
    --log-level debug
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
CMD="${1:-}"; shift || true
case "$CMD" in
  setup)   cmd_setup           ;;
  start)   cmd_start   "$@"   ;;
  stop)    cmd_stop            ;;
  restart) cmd_restart "$@"   ;;
  status)  cmd_status          ;;
  logs)    cmd_logs    "$@"   ;;
  dev)     cmd_dev     "$@"   ;;
  *)
    echo "Usage: $(basename "$0") <command> [options]"
    echo ""
    echo "  setup                         one-time: TLS cert + nginx HTTPS config"
    echo "  start   [--port N] [--workers N]  start production server (daemonised)"
    echo "  stop                          graceful shutdown"
    echo "  restart [--port N] [--workers N]  stop then start"
    echo "  status                        PID, workers, uptime, nginx state"
    echo "  logs    [access|error]        tail log (default: error)"
    echo "  dev     [--port N]            uvicorn --reload, localhost only"
    exit 1 ;;
esac
