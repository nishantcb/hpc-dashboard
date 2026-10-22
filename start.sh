#!/usr/bin/env bash
# Starts Cluster Watch in the background so it keeps running after you close
# this terminal, and prints the URL to use from this machine and from other
# devices on your local network. Stop it with ./stop.sh.
#
# Override defaults if you want:
#   HOST=127.0.0.1 ./start.sh   # this device only, no LAN access
#   PORT=9000 ./start.sh
set -e
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
PIDFILE=".cluster-watch.pid"
LOGFILE="cluster-watch.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PIDFILE"))."
  exit 0
fi

if [ "$HOST" = "0.0.0.0" ] && [ -z "$DASHBOARD_PASSWORD" ]; then
  echo "Warning: binding to 0.0.0.0 (your LAN) with no DASHBOARD_PASSWORD set."
  echo "Anyone on your network could open this dashboard. Set one with:"
  echo "  DASHBOARD_PASSWORD=some-secret ./start.sh"
  echo ""
fi

nohup python3 -m uvicorn main:app --host "$HOST" --port "$PORT" > "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"

LAN_IP=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except Exception:
    print('<your-lan-ip>')
finally:
    s.close()
")

sleep 1
echo "Started (PID $(cat "$PIDFILE"))."
echo "This device:  http://127.0.0.1:${PORT}"
if [ "$HOST" = "0.0.0.0" ]; then
  echo "On your LAN:  http://${LAN_IP}:${PORT}"
fi
echo "Logs:         tail -f ${LOGFILE}"
echo "Stop with:    ./stop.sh"
