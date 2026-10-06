#!/usr/bin/env bash
# Keeps one component alive: restarts it if it exits. Usage: supervise.sh <name> <command...>
NAME="$1"; shift
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/logs/$NAME.log"
PY="$DIR/.venv/bin/python"
echo $$ > "$DIR/logs/$NAME.supervisor.pid"
trap 'kill $CHILD 2>/dev/null; rm -f "$DIR/logs/$NAME.pid"; exit 0' TERM INT
while true; do
  echo "=== $(date '+%F %T %Z') starting $NAME" >> "$LOG"
  "$@" >> "$LOG" 2>&1 &
  CHILD=$!
  echo $CHILD > "$DIR/logs/$NAME.pid"
  wait $CHILD
  CODE=$?
  echo "=== $(date '+%F %T %Z') $NAME exited (code $CODE), restarting in 10s" >> "$LOG"
  "$PY" "$DIR/notify.py" event "restart:$NAME:$(date +%s)" "⚠️ Memebot: the '$NAME' process stopped (exit code $CODE) at $(TZ=America/Toronto date '+%H:%M ET') and is restarting automatically." >/dev/null 2>&1
  sleep 10
done
