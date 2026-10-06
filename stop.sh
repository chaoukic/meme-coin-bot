#!/usr/bin/env bash
# Stop all memebot background processes (uses the pid files written by supervise.sh).
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${QUIET_STOP:-0}" != "1" ]; then
  "$DIR/.venv/bin/python" "$DIR/notify.py" event "stop:$(date +%s)" "⏹ Memebot stopped at $(TZ=America/Toronto date '+%H:%M ET') (stop.sh). No scanning or paper trading until it is started again." >/dev/null 2>&1
  # give the Telegram process a moment to deliver the stop message
  sleep 12
fi
for n in bot web publish telegram; do
  sp="$DIR/logs/$n.supervisor.pid"; cp="$DIR/logs/$n.pid"
  [ -f "$sp" ] && kill "$(cat "$sp")" 2>/dev/null
  [ -f "$cp" ] && kill "$(cat "$cp")" 2>/dev/null
  rm -f "$sp" "$cp"
done
sleep 1
echo "Stopped."
