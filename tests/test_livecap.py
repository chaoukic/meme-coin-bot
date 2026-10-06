"""Real wallet's own daily stop, and paper caps not blocking the real test. Never touches the chain."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_daycap.py")).read().split("\nsetup(")[0])  # reuse setup/buy/check helpers
import live, notify
from common import get_state, set_state, toronto_date
res = []
live.active = lambda cfg: True
live.spent_usd = lambda con: 0.0
live.open_live = lambda con: 0
started = []
live.threading.Thread = lambda target, args, daemon: type("T", (), {"start": lambda self: started.append(args)})()
W = {"v": 100.0}
live.wallet_total = lambda con, cfg: W["v"]
setup(11)
set_state(con, "live_day", {"date": toronto_date(), "start_usd": 100.0, "stopped": False}); con.commit()
r, skip = buy(95)
check("paper hard stop on, real wallet flat: entry taken and real buy started", r is not None and len(started) == 1)
check("telegram buy alert says REAL MONEY: yes", con.execute("SELECT 1 FROM outbox WHERE key LIKE 'buy:%' AND text LIKE '%REAL MONEY: yes%'").fetchone())
W["v"] = 89.0
ok, pct, msg = live.day_check(con, cfg)
check("real wallet -11%: real daily stop hits, alert queued", not ok and con.execute("SELECT 1 FROM outbox WHERE key LIKE 'live-daystop:%'").fetchone())
W["v"] = 100.0
check("real daily stop stays on for the day after recovery", not live.day_check(con, cfg)[0])
setup(11)
r, skip = buy(95)
check("paper hard stop + real stop both on: no entry", r is None and "hard floor" in (skip or ""))
