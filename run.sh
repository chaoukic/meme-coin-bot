#!/usr/bin/env bash
# Start everything in the background (survives logout). Safe to run again: restarts cleanly.
# PAPER TRADING ONLY.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs
QUIET_STOP=1 "$DIR/stop.sh" >/dev/null 2>&1
PY="$DIR/.venv/bin/python"
# Self-heal the virtualenv: after the Oct 5 box re-provision .venv was missing entirely and every process
# failed to start. Rebuild it from requirements.txt if python is missing or the core imports fail.
if ! "$PY" -c "import requests, fastapi, uvicorn, solders, base58" >/dev/null 2>&1; then
  echo "=== $(date '+%F %T %Z') .venv missing or broken, rebuilding from requirements.txt" | tee -a "$DIR/logs/run.log"
  rm -rf "$DIR/.venv"
  if ! { python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"; } >> "$DIR/logs/run.log" 2>&1; then
    echo "!!! .venv rebuild FAILED, see logs/run.log" | tee -a "$DIR/logs/run.log"; exit 1
  fi
  echo "=== $(date '+%F %T %Z') .venv rebuilt OK" | tee -a "$DIR/logs/run.log"
fi
setsid nohup "$DIR/supervise.sh" bot "$PY" "$DIR/bot.py" >/dev/null 2>&1 < /dev/null &
setsid nohup "$DIR/supervise.sh" web "$PY" -m uvicorn web:app --host 127.0.0.1 --port 8787 --app-dir "$DIR" --log-level warning >/dev/null 2>&1 < /dev/null &
if [ "${NO_PUBLISH:-0}" != "1" ]; then
  setsid nohup "$DIR/supervise.sh" publish "$PY" "$DIR/publish.py" >/dev/null 2>&1 < /dev/null &
fi
setsid nohup "$DIR/supervise.sh" telegram "$PY" "$DIR/telegram_bot.py" >/dev/null 2>&1 < /dev/null &
"$PY" "$DIR/notify.py" event "start:$(date +%s)" "🟢 Memebot started at $(TZ=America/Toronto date '+%H:%M ET') (run.sh). Paper trading only." >/dev/null 2>&1
sleep 5
"$DIR/status.sh"
