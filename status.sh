#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Memebot status ($(date '+%F %T %Z')) — PAPER TRADING ONLY"
for n in bot web publish telegram; do
  sp="$DIR/logs/$n.supervisor.pid"; cp="$DIR/logs/$n.pid"
  if [ -f "$sp" ] && kill -0 "$(cat "$sp")" 2>/dev/null; then
    c=$( [ -f "$cp" ] && cat "$cp" ); echo "  $n: RUNNING (supervisor pid $(cat "$sp"), process pid ${c:-?})"
  else echo "  $n: STOPPED"; fi
done
code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8787/ 2>/dev/null)
echo "  local dashboard: http://localhost:8787  (HTTP $code)"
[ -s "$DIR/public_url.txt" ] && echo "  public dashboard (Netlify, published ~10s after every paper buy/sell, else every 5 min): $(cat "$DIR/public_url.txt")" || echo "  public dashboard: (not published yet)"
[ -f "$DIR/logs/publish.log" ] && echo "  last publish log: $(grep -E 'deployed|skipping|error' "$DIR/logs/publish.log" | tail -1)"
"$DIR/.venv/bin/python" - <<'PY' 2>/dev/null
import sys, json, time; sys.path.insert(0, "/workspace/memebot")
from common import db, get_state
c = db(); st = get_state(c, "scanner_status", {}) or {}
r = c.execute("SELECT id, finished, evaluated, passed, funnel FROM cycles WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
if r: print(f"  last scan cycle #{r['id']} {int(time.time()-r['finished'])}s ago: checked {r['evaluated']}, passed {r['passed']}")
import json as _j, os as _o
mp = get_state(c, "manual_pause") or {}
import trader, common as _c
_cfg = _c.load_config(); ds = trader.day_status(c, _cfg)
print(f"  daily loss mode: {ds['mode'].upper()} - {ds['label']} | today {ds['day_pnl_pct']:+.2f}% (${ds['day_pnl']:+,.2f}); "
      f"soft cap -{_cfg['paper']['daily_loss_cap_pct']}%, hard stop -{_cfg['paper'].get('daily_hard_stop_pct', 10)}%")
print("  new entries:", "PAUSED via Telegram /pause" if mp.get("on") else {"hard": "none today (daily hard stop)",
      "soft": "high-score only at reduced size (daily soft cap)", "normal": "allowed"}[ds["mode"]])
cf = "/workspace/memebot/telegram_chat.json"
print("  telegram:", ("connected (chat saved)" if _o.path.exists(cf) else "waiting for Sammy to press Start in Telegram"),
      "| pending alerts:", c.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0] if c.execute("SELECT name FROM sqlite_master WHERE name='outbox'").fetchone() else 0)
print("  open paper positions:", c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND COALESCE(strategy,'new')='new' AND scoring='current'").fetchone()[0],
      "| closed:", c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND COALESCE(strategy,'new')='new' AND scoring='current'").fetchone()[0],
      "| fake cash: $%.2f" % (get_state(c, "cash", 0) or 0))
try:
    if get_state(c, "new:cash") is not None:
        _w = trader.W("new"); _st = get_state(c, "scoring_new_status") or {}
        print("  new-scoring test (PAPER only):", "self-test PASS" if _st.get("ok") else "self-test FAILED/off", _st.get("version"),
              "| open:", c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND " + _w).fetchone()[0],
              "| closed:", c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND " + _w).fetchone()[0],
              "| equity: $%.2f" % trader.equity(c, _cfg, "new"), "| cash: $%.2f" % (get_state(c, "new:cash", 0) or 0))
except Exception as _e:
    print("  new-scoring test: status error", _e)
try:
    import established as _es
    _r = get_state(c, "estab_last_round") or {}
    _f = _r.get("funnel") or {}
    import time as _t
    print("  older coins (PAPER only):", "on" if _es.enabled(_cfg) else "off",
          "| open:", c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND strategy='established'").fetchone()[0],
          "| closed:", c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND strategy='established'").fetchone()[0],
          "| fake cash: $%.2f" % (get_state(c, "estab_cash", 0) or 0),
          "| last round %s: evaluated %s, quality ok %s, entry signals %s, opened %s" % (
              ("%.0fs ago" % (_t.time() - _r["ts"])) if _r.get("ts") else "never", _f.get("evaluated", 0),
              _f.get("quality_ok", 0), _f.get("passed", 0), _f.get("opened", 0)))
except Exception as _e:
    print("  older coins: status unavailable", _e)
PY
