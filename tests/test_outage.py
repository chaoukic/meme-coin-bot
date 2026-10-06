"""Outage alert (startup gap + scanner watchdog), dashboard health block and decision score fields.
Runs on a fresh EMPTY temp DB (no network, never touches the real DB). Run: .venv/bin/python tests/test_outage.py"""
import os, sys, tempfile, time
sys.path.insert(0, "/workspace/memebot"); sys.path.insert(0, "/workspace/memebot/tools")
import common
TEST_DB = os.path.join(tempfile.mkdtemp(), "test.db")
common.DB_PATH = TEST_DB
import notify; notify.DB_PATH = TEST_DB
import live, outage, trader, dashboard_data
import backfill_decision_scores as bf

results = []
def check(name, cond):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)

common.init_db()
con = common.db(); live.ensure(con); notify.ensure(con)
outbox = lambda pat: con.execute("SELECT key, text FROM outbox WHERE key LIKE ? ORDER BY id", (pat,)).fetchall()
now = time.time()

# --- startup outage alert
check("no history -> no offline alert", outage.startup_check(con, now) is None and not outbox("offline:%"))
con.execute("INSERT INTO cycles(started, finished) VALUES(?, ?)", (now - 300, now - 290)); con.commit()
check("last cycle 5 min ago -> no alert", outage.startup_check(con, now) is None)
t0 = now - 26 * 3600
con.execute("DELETE FROM cycles"); con.execute("INSERT INTO cycles(started, finished) VALUES(?, ?)", (t0 - 30, t0))
outage.heartbeat(con, t0 - 60); con.commit()
txt = outage.startup_check(con, now); con.commit()
check("gap > 10 min -> alert queued", txt and len(outbox("offline:%")) == 1)
check("alert wording: times, duration, no trades, no em-dash",
      txt.startswith("⚠️ Memebot was offline from " + outage.fmt_when(t0) + " to " + outage.fmt_when(now))
      and "(about 26h)" in txt and "No trades happened while it was down." in txt and "—" not in txt)
outage.startup_check(con, now + 5); con.commit()
check("same outage never queued twice", len(outbox("offline:%")) == 1)
check("heartbeat newer than last cycle is used", (outage.heartbeat(con, t0 + 100), outage.last_activity(con))[1] == t0 + 100)
outage.heartbeat(con, t0); con.commit()
# real-money position open during the gap (bought before, not sold before the gap)
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,?,?,?,?)", (t0 - 999, 7, "M7", "WIF", "buy", 1, 5, "ok"))
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,?,?,?,?)", (t0 - 999, 8, "M8", "OLD", "buy", 1, 5, "ok"))
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,?,?,?,?)", (t0 - 500, 8, "M8", "OLD", "sell", 1, 6, "ok"))
check("real positions open during gap found (closed one ignored)", outage.real_open_during(con, t0, now) == ["WIF"])
con.execute("DELETE FROM outbox"); con.commit()
txt = outage.startup_check(con, now); con.commit()
check("alert mentions open real position instead of 'no trades'", "1 real-money position was open while it was down (WIF)" in txt
      and "No trades" not in txt)
check("fmt_when looks like 'Mon 12:06 PM'", outage.fmt_when(1791216382) == "Mon 12:06 PM")

# --- watchdog: one stuck alert, no spam, one recovery alert
wd = outage.Watchdog(); last_ok = now - 100
check("watchdog quiet while cycles are recent", wd.check(con, last_ok, now) is None)
r1 = wd.check(con, last_ok, last_ok + 660); r2 = wd.check(con, last_ok, last_ok + 1500); con.commit()
check("watchdog: one stuck alert, no repeats", r1 == "stuck" and r2 is None and len(outbox("stuck:%")) == 1
      and outbox("stuck:%")[0][1] == "⚠️ Memebot's scanner is stuck: no scan for 11 min.")
r3 = wd.check(con, last_ok + 1600, last_ok + 1600); r4 = wd.check(con, last_ok + 1600, last_ok + 1700); con.commit()
check("watchdog: one recovery alert when cycles resume", r3 == "recovered" and r4 is None and len(outbox("unstuck:%")) == 1)

# --- decision score fields
cols = [r[1] for r in con.execute("PRAGMA table_info(decisions)")]
check("decisions has score_new/score_old columns", "score_new" in cols and "score_old" in cols)
common.decision(con, "SCORE", "TOK1", "AAA", "new scoring passed (51.0 >= 48.4); old score 62.5", scoring="new", score_new=51.0, score_old=62.5)
trader._skip(con, {"token": "TOK2", "symbol": "BBB", "score": 70.1, "score_new": 49.9}, "passed (new score 49.9) but test", "new")
common.decision(con, "PAUSE", None, None, "no scores here")
rows = {r["token"] or "none": r for r in con.execute("SELECT * FROM decisions")}
check("decision() stores score_new/score_old", rows["TOK1"]["score_new"] == 51.0 and rows["TOK1"]["score_old"] == 62.5)
check("trader._skip passes candidate scores", rows["TOK2"]["score_new"] == 49.9 and rows["TOK2"]["score_old"] == 70.1 and rows["TOK2"]["scoring"] == "new")
check("unknown scores stay NULL", rows["none"]["score_new"] is None and rows["none"]["score_old"] is None)
pos = {"score": 60.0, "score_current": None, "score_new": 52.0, "scoring": "current", "strategy": "new"}
check("_pscores uses position scores", trader._pscores(pos) == {"score_new": 52.0, "score_old": 60.0})
dec = dashboard_data._dec_scores([dict(r) for r in con.execute("SELECT * FROM decisions ORDER BY id")])
check("dashboard decisions carry numeric score_new/score_old keys", all("score_new" in d and "score_old" in d for d in dec)
      and isinstance(dec[0]["score_new"], float))
check("backfill parser reads SCORE message", bf.parse("new scoring turned down (0.0 < 48.4); old score 72.0") == (0.0, 72.0))
check("backfill parser: 'None' means unknown", bf.parse("old scoring passed (62.1 >= 55); new score None") == (None, 62.1))

# --- dashboard health block
con.commit()
try:
    d = dashboard_data.build()
    h = d.get("health") or {}
    check("data.json health block shape", set(h) == {"last_cycle_ts", "heartbeat_ts", "generated", "offline_after_sec"}
          and h["offline_after_sec"] == 600 and h["generated"] == d["generated"] and h["heartbeat_ts"] == t0)
except Exception as e:
    check(f"dashboard build on empty DB ({e})", False)

con.close()
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
