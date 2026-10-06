"""Outage detection + Telegram alerts (added Oct 6 after the Oct 5 silent outage). PAPER TRADING ONLY.

- startup_check(): on bot start, if the last scan cycle / heartbeat is older than OFFLINE_AFTER_SEC, queue one
  "Memebot was offline from X to Y" alert (mentions real-money positions that were open during the gap).
- Watchdog: called from the bot's main loop; queues ONE "scanner is stuck" alert when no scan cycle has completed
  for OFFLINE_AFTER_SEC while the process is alive, and ONE recovery alert when cycles resume.
All alerts go through notify.enqueue (unique keys, so nothing is ever sent twice)."""
import time
from datetime import datetime
import notify
from common import TZ, get_state, set_state, log

OFFLINE_AFTER_SEC = 600
HEARTBEAT_KEY = "bot_heartbeat"


def fmt_when(ts):
    """Toronto time like 'Mon 12:06 PM'."""
    d = datetime.fromtimestamp(ts, TZ)
    return d.strftime("%a ") + str(int(d.strftime("%I"))) + d.strftime(":%M %p")


def fmt_dur(sec):
    sec = max(0, sec)
    if sec < 3600:
        return f"{max(1, round(sec / 60))} min"
    h = sec / 3600
    if h < 10:
        return f"{h:.1f}".rstrip("0").rstrip(".") + "h"
    return f"{round(h)}h"


def heartbeat(con, ts=None):
    set_state(con, HEARTBEAT_KEY, ts if ts is not None else time.time())


def last_cycle_ts(con):
    r = con.execute("SELECT MAX(finished) FROM cycles WHERE finished IS NOT NULL").fetchone()
    return r[0] if r and r[0] else None


def last_activity(con):
    """Most recent sign of life: last finished scan cycle or last heartbeat (unix seconds), or None."""
    vals = [v for v in (last_cycle_ts(con), get_state(con, HEARTBEAT_KEY)) if v]
    return max(vals) if vals else None


def real_open_during(con, t0, t1):
    """Symbols of REAL-money positions (live_trades) that were open at some point between t0 and t1."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_trades'").fetchone():
        return []
    rows = con.execute("""SELECT b.pos_id, b.symbol FROM live_trades b WHERE b.side='buy' AND b.status='ok' AND b.ts < ?
                          AND NOT EXISTS (SELECT 1 FROM live_trades s WHERE s.pos_id=b.pos_id AND s.side='sell'
                                          AND s.status='ok' AND s.frac>=0.999 AND s.ts <= ?)
                          ORDER BY b.id""", (t1, t0)).fetchall()
    return [r[1] or f"#{r[0]}" for r in rows]


def offline_text(t0, t1, real_syms):
    s = (f"⚠️ Memebot was offline from {fmt_when(t0)} to {fmt_when(t1)} (about {fmt_dur(t1 - t0)}). "
         "It's running again now. ")
    if real_syms:
        n = len(real_syms)
        s += (f"{n} real-money position{'s were' if n > 1 else ' was'} open while it was down "
              f"({', '.join(real_syms)}), and the bot was not watching {'them' if n > 1 else 'it'}. Please check.")
    else:
        s += "No trades happened while it was down."
    return s


def startup_check(con, now=None):
    """Call once at bot start, BEFORE the first heartbeat is written. Returns the alert text if one was queued."""
    now = now if now is not None else time.time()
    last = last_activity(con)
    if not last or now - last <= OFFLINE_AFTER_SEC:
        return None
    text = offline_text(last, now, real_open_during(con, last, now))
    notify.enqueue(con, f"offline:{int(last)}", text)
    set_state(con, "last_outage", {"from": last, "to": now})
    log.warning("outage detected: no activity from %s to %s; alert queued", fmt_when(last), fmt_when(now))
    return text


class Watchdog:
    """In-process scanner watchdog. last_ok = time of the last completed scan cycle."""
    def __init__(self, now=None):
        self.stuck_since = None   # last_ok at the moment we alerted (None = not alerted)

    def check(self, con, last_ok, now=None):
        now = now if now is not None else time.time()
        if self.stuck_since is None:
            if now - last_ok > OFFLINE_AFTER_SEC:
                self.stuck_since = last_ok
                notify.enqueue(con, f"stuck:{int(last_ok)}",
                               f"⚠️ Memebot's scanner is stuck: no scan for {round((now - last_ok) / 60)} min.")
                log.warning("watchdog: no completed scan cycle for %.0f min, alert queued", (now - last_ok) / 60)
                return "stuck"
        elif last_ok > self.stuck_since:
            notify.enqueue(con, f"unstuck:{int(self.stuck_since)}",
                           f"✅ Memebot's scanner is working again (it was stuck for about {fmt_dur(last_ok - self.stuck_since)}).")
            log.info("watchdog: scanner recovered")
            self.stuck_since = None
            return "recovered"
        return None
