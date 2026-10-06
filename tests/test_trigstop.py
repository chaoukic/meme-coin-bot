"""Jupiter trigger stop (trigstop.py) with mocked Jupiter / chain. Runs on a COPY of the DB; no network, no wallet use.
Run: .venv/bin/python tests/test_trigstop.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_daycap.py")).read().split("\nsetup(")[0])  # DB copy + helpers
import live, trigstop, jtrigger as jt
import fills; fills.record_async = lambda *a, **k: None  # Oct 3 fill recorder: no network in tests
from common import get_state, set_state

cfg["live"].update({"trigger_stops": True, "trigger_stops_trial_max": 1, "trigger_trail_min_interval_sec": 300,
                    "trigger_trail_min_move_pct": 5, "trigger_check_interval_sec": 0, "min_sol_reserve": 0.02})
SOLPX = 150.0
W = {"raw": 0, "sol": 0.3}
CALLS_ = []
live.sol_price = lambda: SOLPX
live.sol_balance = lambda cfg_: W["sol"]
live.token_raw = lambda cfg_, mint: W["raw"]
def fake_swap(cfg_, a, b, amt, slip):
    CALLS_.append(("swap", amt)); W["raw"] -= amt; W["sol"] += 0.05; return "SELLSIG", {}
live.swap = fake_swap
ORD = {}
def place(mint, out, raw, trig, slip, exp):
    CALLS_.append(("place", trig, raw, slip, exp)); W["raw"] -= raw
    ORD["ORD1"] = {"id": "ORD1", "orderState": "open", "rawState": "open", "events": []}
    return {"id": "ORD1", "txSignature": "DEP", "depositConfirmed": True}
jt.place_stop = place
jt.update_stop = lambda oid, p, slip=None: CALLS_.append(("patch", oid, p)) or {"id": oid}
CANCEL = {"fail": 0}
def cancel(oid):
    if CANCEL["fail"] > 0:
        CANCEL["fail"] -= 1; raise jt.TriggerError("confirm-cancel HTTP 500", 500)
    CALLS_.append(("cancel", oid)); W["raw"] += int(get_state(con, "_dep", 0)); ORD[oid]["orderState"] = "cancelled"
    return {"id": oid, "txSignature": "WD"}
jt.cancel_withdraw = cancel
jt.order = lambda oid, mint=None: ORD.get(oid)
set_state(con, "trigger_trials_used", 0); con.commit()

def new_real(usd=12.0, raw=1_000_000):
    setup(1)
    r, _ = buy(80)
    con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,token_raw,status,sig) VALUES(?,?,?,?,'buy',1,?,?, 'ok','BUYSIG')",
                (time.time(), r["id"], r["token"], r["symbol"], usd, str(raw)))
    con.commit()
    W["raw"] = raw; set_state(con, "_dep", raw); con.commit()
    return r, con.execute("SELECT last_insert_rowid()").fetchone()[0]
def brow(bid):
    return con.execute("SELECT * FROM live_trades WHERE id=?", (bid,)).fetchone()
def outbox(like):
    return con.execute("SELECT text FROM outbox WHERE text LIKE ? ORDER BY id DESC", (like,)).fetchone()
live.ensure(con)

# 1) $5 position: below Jupiter's $10 minimum -> not placed, trial not used
p, bid = new_real(usd=5.0)
trigstop.after_buy(con, cfg, bid, p["id"], p["token"], p["symbol"], 1_000_000, 5.0)
check("$5 real position: no order (Jupiter $10 minimum), trial kept, Telegram note",
      not any(c[0] == "place" for c in CALLS_) and brow(bid)["trig_state"] == "too_small"
      and trigstop.trials_left(con, cfg) == 1 and outbox("%below Jupiter's $10 minimum%"))

# 2) place
p, bid = new_real()
trigstop.after_buy(con, cfg, bid, p["id"], p["token"], p["symbol"], 1_000_000, 12.0)
b = brow(bid); pl = [c for c in CALLS_ if c[0] == "place"][-1]
check(f"stop placed for the full amount at entry x 0.75 (${pl[1]:.6g})",
      b["trig_state"] == "open" and abs(pl[1] - p["entry_price"] * 0.75) < 1e-12 and pl[2] == 1_000_000 and b["trig_id"] == "ORD1")
check("expiry a bit beyond the 24h max hold", 24 * 3600e3 < pl[4] - time.time() * 1000 <= 26.1 * 3600e3)
check("Telegram: 'Stop set on Jupiter at $X (-25%)'", outbox("%Stop set on Jupiter%(-25%)%"))
check("trial used up: next buy gets no order", trigstop.trials_left(con, cfg) == 0)
p2, bid2 = new_real()
k0 = len(CALLS_); trigstop.after_buy(con, cfg, bid2, p2["id"], p2["token"], p2["symbol"], 1_000_000, 12.0)
check("second real buy after the trial: nothing placed", len(CALLS_) == k0 and brow(bid2)["trig_state"] is None)

# 3) trailing: peak +60% -> stop to peak x 0.8, never down, rate-limited, only >=5% moves
e = p["entry_price"]
con.execute("UPDATE positions SET peak_price=?, last_price=? WHERE id=?", (e * 1.6, e * 1.55, p["id"]))
con.execute("UPDATE live_trades SET trig_ts=? WHERE id=?", (time.time() - 600, bid)); con.commit()
trigstop.on_trail(con, cfg, con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()); time.sleep(0.5)
pa = [c for c in CALLS_ if c[0] == "patch"]
check("trailing active: Jupiter stop PATCHed up to peak x 0.8", pa and abs(pa[-1][2] - e * 1.28) < 1e-12 and abs(brow(bid)["trig_price"] - e * 1.28) < 1e-12)
check("Telegram: 'Stop moved up on Jupiter'", outbox("%Stop moved up on Jupiter%"))
con.execute("UPDATE positions SET peak_price=? WHERE id=?", (e * 1.7, p["id"])); con.commit()
trigstop.on_trail(con, cfg, con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()); time.sleep(0.3)
check("no second move within 5 minutes", len([c for c in CALLS_ if c[0] == "patch"]) == len(pa))
con.execute("UPDATE live_trades SET trig_ts=? WHERE id=?", (time.time() - 600, bid))
con.execute("UPDATE positions SET peak_price=? WHERE id=?", (e * 1.62, p["id"])); con.commit()
trigstop.on_trail(con, cfg, con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()); time.sleep(0.3)
check("no move for a <5% step", len([c for c in CALLS_ if c[0] == "patch"]) == len(pa))
con.execute("UPDATE positions SET peak_price=? WHERE id=?", (e * 1.2, p["id"])); con.commit()
trigstop.on_trail(con, cfg, con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()); time.sleep(0.3)
check("never moved down", len([c for c in CALLS_ if c[0] == "patch"]) == len(pa) and abs(brow(bid)["trig_price"] - e * 1.28) < 1e-12)

# 4) bot exit while coins are in the vault: cancel + withdraw first, then sell
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,status,reason) VALUES(?,?,?,?,'sell',1,'pending','take profit 2')",
            (time.time(), p["id"], p["token"], p["symbol"])); con.commit()
sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
i0 = len(CALLS_)
live._do_sell(cfg, sid, p["id"], p["token"], p["symbol"], 1.0, "take profit 2")
seq = [c[0] for c in CALLS_[i0:]]
check(f"bot sell: cancel+withdraw BEFORE the swap {seq}", seq[:2] == ["cancel", "swap"] and brow(sid)["status"] == "ok" and brow(bid)["trig_state"] == "released")

# 5) Jupiter filled the order: detected, booked as real sell with the right reason, bot sell skipped
set_state(con, "trigger_trials_used", 0); con.commit()
p, bid = new_real()
trigstop.after_buy(con, cfg, bid, p["id"], p["token"], p["symbol"], 1_000_000, 12.0)
ORD["ORD1"].update(orderState="filled", rawState="fill_success", fillPercent=1.0,
                   events=[{"type": "fill", "state": "success", "outputAmount": str(int(0.06 * 1e9)), "txSignature": "FILLSIG"}])
trigstop._CHECK.update(running=False, last=0)
trigstop.check(con, cfg, background=False)
s = con.execute("SELECT * FROM live_trades WHERE pos_id=? AND side='sell'", (p["id"],)).fetchone()
check("Jupiter fill detected: real sell booked, exit_reason 'stop loss (Jupiter trigger)', P&L from the fill",
      s and s["reason"] == "stop loss (Jupiter trigger)" and s["frac"] == 1 and abs(s["usd"] - 9.0) < 1e-6 and s["sig"] == "FILLSIG"
      and brow(bid)["trig_state"] == "filled")
check("Telegram: Jupiter stop SOLD with real P&L", outbox("%Jupiter stop SOLD%P&L%"))
live.dashboard = live.dashboard  # real function; mocks above cover balance/price
d = live.dashboard(con, cfg)
cl = [c for c in d["closed"] if c["pos_id"] == p["id"]]
check("live.closed[] shows it with real P&L -$3.00", cl and cl[0]["exit_reason"] == "stop loss (Jupiter trigger)" and abs(cl[0]["pnl_usd"] + 3.0) < 1e-6)
nsell = con.execute("SELECT COUNT(*) FROM live_trades WHERE pos_id=? AND side='sell'", (p["id"],)).fetchone()[0]
live.on_sell(con, cfg, p["id"], p["token"], p["symbol"], 1.0, "stop loss (-26%)")
check("later paper stop: no second real sell is attempted", con.execute("SELECT COUNT(*) FROM live_trades WHERE pos_id=? AND side='sell'", (p["id"],)).fetchone()[0] == nsell)

# 6) placement failure: fallback to the bot's stop + Telegram, trial not used
set_state(con, "trigger_trials_used", 0); con.commit()
def boom(*a): raise jt.TriggerError("POST /orders/price -> HTTP 400: token not supported", 400)
jt.place_stop = boom
p, bid = new_real()
trigstop.after_buy(con, cfg, bid, p["id"], p["token"], p["symbol"], 1_000_000, 12.0)
check("place failure: state place_failed, trial kept, Telegram says the bot's own stop is watching",
      brow(bid)["trig_state"] == "place_failed" and trigstop.trials_left(con, cfg) == 1 and outbox("%could not be placed%own -25% stop%"))
jt.place_stop = place

# 7) vault withdrawal keeps failing -> sell waits (VaultBusy), retried by check() once Jupiter answers
p, bid = new_real()
trigstop.after_buy(con, cfg, bid, p["id"], p["token"], p["symbol"], 1_000_000, 12.0)
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,status,reason) VALUES(?,?,?,?,'sell',1,'pending','max hold time 24h')",
            (time.time(), p["id"], p["token"], p["symbol"])); con.commit()
sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
CANCEL["fail"] = 99
orig_rel = trigstop.release_before_sell
trigstop.release_before_sell = lambda *a, **k: orig_rel(*a, wait_s=1, **k)
live._do_sell(cfg, sid, p["id"], p["token"], p["symbol"], 1.0, "max hold time 24h")
check("vault stuck: sell marked failed, coins NOT sold from an empty wallet, Telegram explains",
      brow(sid)["status"] == "failed" and brow(bid)["trig_state"] == "release_failed" and outbox("%Jupiter stop-order vault%"))
CANCEL["fail"] = 0
trigstop._CHECK.update(running=False, last=0)
trigstop.check(con, cfg, background=False)
check("next check: withdrawal works, the waiting sell goes through", brow(sid)["status"] == "ok" and brow(bid)["trig_state"] == "released")
trigstop.release_before_sell = orig_rel

con.close(); shutil.rmtree(tmp)
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
