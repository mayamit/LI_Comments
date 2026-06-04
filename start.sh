#!/usr/bin/env bash
#
# start.sh — start the LI_Comments server if it isn't already running.
#
# Checks whether something is already listening on the app's port. If so it
# warns and exits without touching it — unless --force/--restart is given, in
# which case it stops the existing process first. Otherwise it starts uvicorn
# (preferring the project's .venv) in the background, logging to logs/server.out.
#
# Usage:
#   ./start.sh                  # start on 127.0.0.1:8000 (warn if already up)
#   ./start.sh --force          # stop any running server, then start
#   ./start.sh --restart        # alias for --force
#   PORT=9000 ./start.sh        # override port
#   HOST=0.0.0.0 ./start.sh
#
set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
LOG_DIR="${LOG_DIR:-./logs}"
SERVER_OUT="$LOG_DIR/server.out"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -f|--force|--restart) FORCE=1 ;;
        -h|--help)
            # Print the leading comment block (skip shebang), stop at first
            # non-comment line so code never leaks into the help text.
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument '$arg' (try --help)." >&2
            exit 2
            ;;
    esac
done

# Pick the venv uvicorn if present, else fall back to PATH.
if [[ -x ".venv/bin/uvicorn" ]]; then
    UVICORN=".venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN="uvicorn"
else
    echo "ERROR: uvicorn not found (.venv/bin/uvicorn or on PATH)." >&2
    exit 1
fi

# Already running?
existing="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$existing" ]]; then
    if [[ "$FORCE" -eq 1 ]]; then
        echo "Stopping existing server on port $PORT (PID $existing)..."
        # shellcheck disable=SC2086
        kill $existing 2>/dev/null || true
        # Wait up to ~10s for the port to free, escalating to SIGKILL.
        for _ in $(seq 1 20); do
            sleep 0.5
            lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
        done
        if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "Process didn't exit gracefully — sending SIGKILL."
            # shellcheck disable=SC2046
            kill -9 $(lsof -ti "tcp:$PORT" -sTCP:LISTEN) 2>/dev/null || true
            sleep 1
        fi
    else
        echo "WARNING: server already running on port $PORT (PID $existing) — not starting." >&2
        echo "         Restart it with:  ./start.sh --force" >&2
        exit 1
    fi
fi

mkdir -p "$LOG_DIR"
echo "Starting server on http://$HOST:$PORT  (using $UVICORN)"
nohup "$UVICORN" main:app --host "$HOST" --port "$PORT" >>"$SERVER_OUT" 2>&1 &
pid=$!

# Give it a moment, then confirm it actually came up.
sleep 2
if kill -0 "$pid" 2>/dev/null; then
    echo "Started (PID $pid). Console output -> $SERVER_OUT ; app logs -> $LOG_DIR/app.log"
else
    echo "ERROR: server exited immediately. Last lines of $SERVER_OUT:" >&2
    tail -n 20 "$SERVER_OUT" >&2 || true
    exit 1
fi
