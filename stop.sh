#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PIDFILE=".cluster-watch.pid"

if [ ! -f "$PIDFILE" ]; then
  echo "Not running (no PID file)."
  exit 0
fi

PID=$(cat "$PIDFILE")
if kill "$PID" 2>/dev/null; then
  echo "Stopped (PID $PID)."
else
  echo "Process $PID not found; removing stale PID file."
fi
rm -f "$PIDFILE"
