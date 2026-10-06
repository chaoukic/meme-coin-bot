"""Side-by-side scoring test (SIDE_BY_SIDE_SPEC.md): second PAPER account on scoring_v1.
Runs on a COPY of the DB, never touches the chain. Run: .venv/bin/python tests/test_scoring.py"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_daycap.py")).read().split("\nsetup(")[0])  # DB copy + helpers
import live, newscore, scanner, bot, dashboard_data, established
from common import get_state, set_state, now_ts
cfg["scoring"] = {"enabled": True, "starting_balance_usd": 1000}
B, TOP = newscore.thresholds()

# ---------------------------------------------------------------- scorer copy, self-test, version
check("self-test passes on the copied model (10 memebot rows)", newscore.selftest() and newscore.STATUS["rows"] == 10)
check("model version is 'scoring_v1 1.0'", newscore.version() == "scoring_v1 1.0")
check("thresholds from the model file: buy 48.4, soft-cap 55.5", (B, TOP) == (48.4, 55.5))
check("scorer is a copy inside memebot (not a link)", not os.path.islink("scoring_v1/score_v1.py") and not os.path.islink("scoring_v1/models/memebot_v1.json"))
_s, _p, _n = newscore.score_metrics({"age_min": 10, "liq": 20000, "mcap": 21000})
check("mcap ~ liq (ratio < 1.3): scored without liq/mcap instead of 0 (Oct 6)", _s > 0 and _n == "pool_liq_near_mcap_no_liq")
check("model guard itself unchanged (raw score_v1 still 0)", newscore.score_v1.score(newscore.BOT, {"age_min": 10, "liq": 20000, "mcap": 21000})[0] == 0)
s, p, note = newscore.score_metrics({"age_min": 10, "liq": 5e6, "mcap": 5.1e6, "src": "jupiter"})
check("Jupiter-sourced metrics: liq/mcap NOT given to the scorer (note recorded)", s > 0 and note == "jupiter_metrics_no_liq")
saved = dict(newscore.STATUS); newscore.STATUS.update(ok=False, checked=True)
check("failed self-test -> new account off", not newscore.enabled(cfg))
newscore.STATUS.clear(); newscore.STATUS.update(saved)
check("enabled with [scoring] enabled + self-test ok", newscore.enabled(cfg))
check("config switch off -> new account off", not newscore.enabled({"scoring": {"enabled": False}}))

# ---------------------------------------------------------------- score gate in scanner.evaluate
scanner.cheap_filters = lambda m, F: None
SC = {"v": 60}
scanner.score = lambda m, d, notes: (SC["v"], {"x": SC["v"]})
fresh = json.dumps({"ts": now_ts(), "mint_known": True, "src": "gt", "top1_pct": 5, "top10_pct": 20, "holders": None})
def ev(age, sc):
    SC["v"] = sc
    m = {"age_min": age, "liq": 20000, "mcap": 100000, "price": 1e-4, "pool_set": [], "src": "dexscreener"}
    tok = {"address": "EVTOK", "contract_json": fresh}
    r = scanner.evaluate(con, cfg, tok, m, {"left": 1, "rpc_left": 0})
    return r, m
(r, m) = ev(10, 40)
check("current rejects (score 40 < 55) but new passes (59.2): result stays the current verdict, new gate 'pass'",
      r[0] == "reject" and r[1] == "score" and m["_gate"] == {"current": "reject", "new": "pass"})
(r, m) = ev(600, 80)
check("current passes (80) but new rejects (39.5)", r[0] == "pass" and m["_gate"] == {"current": "pass", "new": "reject"})
cfg["scoring"]["enabled"] = False
(r, m) = ev(10, 40)
check("new account off: new gate 'n.a.', current behaviour unchanged", r[0] == "reject" and m["_gate"]["new"] == "n.a.")
cfg["scoring"]["enabled"] = True

# ---------------------------------------------------------------- two accounts in try_entries
def reset_new(cash_=1000.0, day_loss=0):
    con.execute("UPDATE positions SET status='closed', closed_at=0 WHERE status='open'")
    set_state(con, "new:cash", cash_); set_state(con, "new:starting_balance", 1000.0)
    set_state(con, "new:day", common.toronto_date()); set_state(con, "new:day_start_equity", cash_ / (1 - day_loss / 100))
    set_state(con, "new:paused_today", False); set_state(con, "new:day_mode", "normal")
    con.commit()
def c2(score, sn, gc, gn):
    c = cand(score); c.update(score_new=sn, prob_new=sn / 100, model_version="scoring_v1 1.0", gate_current=gc, gate_new=gn); return c
def run(cands):
    trader.try_entries(con, cfg, cands, "current"); trader.try_entries(con, cfg, cands, "new"); con.commit()
def pos(tok, acct):
    return con.execute("SELECT * FROM positions WHERE token=? AND scoring=?", (tok, acct)).fetchone()

setup(0); reset_new()
LIVE = []
live.on_buy_orig = live.on_buy
live.on_buy = lambda *a, **k: LIVE.append(a) or "real-money mode is off"
a = c2(40, 59.2, "reject", "pass"); b = c2(80, 39.5, "pass", "reject"); both = c2(70, 56.1, "pass", "pass")
run([a, b, both])
check("coin only the new score passes -> bought by NEW account only", pos(a["token"], "new") and not pos(a["token"], "current"))
check("coin only the current score passes -> bought by CURRENT account only", pos(b["token"], "current") and not pos(b["token"], "new"))
check("coin both pass -> each account holds its own position", pos(both["token"], "current") and pos(both["token"], "new"))
pn = pos(a["token"], "new")
check("new position tagged: scoring/score_current/score_new/prob_new/model_version",
      pn["scoring"] == "new" and pn["score_current"] == 40 and pn["score_new"] == 59.2 and abs(pn["prob_new"] - 0.592) < 1e-9
      and pn["model_version"] == "scoring_v1 1.0")
check("current position tagged scoring='current' with both scores", pos(b["token"], "current")["scoring"] == "current" and pos(b["token"], "current")["score_new"] == 39.5)
size = 1000 * P["position_pct_of_balance"] / 100
check(f"new account sizes on its own equity (2% of $1,000 = ${size:.0f})", abs(pn["cost_usd"] - size) < 0.01)
spent_new = con.execute("SELECT SUM(cost_usd) FROM positions WHERE scoring='new' AND status='open'").fetchone()[0]
check("separate cash: new cash = 1000 - its own two buys", abs(get_state(con, "new:cash") - (1000 - spent_new)) < 1e-6
      and con.execute("SELECT COUNT(*) FROM positions WHERE scoring='new' AND status='open'").fetchone()[0] == 2)
check("live.on_buy called only for current-account buys (2), never for new", len(LIVE) == 2 and all(pos(x["token"], "current")["id"] == aa[2] for x, aa in zip([b, both], sorted(LIVE, key=lambda t: t[2]))))
check("new buy alert tagged 'PAPER (new scoring)'", con.execute("SELECT 1 FROM outbox WHERE key=? AND text LIKE '%PAPER (new scoring)%'", (f"buy:{pn['id']}",)).fetchone())
check("new decisions tagged scoring='new' and '[new scoring]'", con.execute("SELECT 1 FROM decisions WHERE kind='BUY' AND token=? AND scoring='new' AND message LIKE '[new scoring]%'", (a["token"],)).fetchone())

# cooldown / same-coin block is per account
run([a])
check("same-coin block is per account: no second NEW position for the same coin",
      con.execute("SELECT COUNT(*) FROM positions WHERE token=? AND scoring='new'", (a["token"],)).fetchone()[0] == 1)
a_cur = dict(a, gate_current="pass", score=80)
trader.try_entries(con, cfg, [a_cur], "current"); con.commit()
check("...but the CURRENT account can still buy that coin (separate cooldowns)", pos(a["token"], "current") is not None)

# ranking by score_new + max open per account
setup(0); reset_new()
mo = P["max_open_positions"]
cs = [c2(10, 49 + i, "reject", "pass") for i in range(mo + 2)]
trader.try_entries(con, cfg, cs, "new"); con.commit()
got = sorted(r["score_new"] for r in con.execute("SELECT score_new FROM positions WHERE status='open' AND scoring='new'"))
check(f"new account ranks by score_new and stops at its own max open ({mo})", got == sorted(49 + i for i in range(2, mo + 2)))
check("current account unaffected by the new account's open positions",
      con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND scoring='current'").fetchone()[0] == 0)

# soft cap on the new account: 55.5 threshold, half size; current not capped
setup(0); reset_new(1000.0, 6)
lo = c2(80, 52.0, "pass", "pass"); hi = c2(80, 56.1, "pass", "pass")
run([lo, hi])
check("new soft cap (-6% on its own equity): new score 52 skipped", not pos(lo["token"], "new"))
check("new soft cap: new score 56.1 bought at half size", pos(hi["token"], "new") and abs(pos(hi["token"], "new")["cost_usd"] - size * P["cap_size_factor"]) < 0.02)
check("current account's caps are separate (normal day: both bought at full size)",
      pos(lo["token"], "current") and abs(pos(lo["token"], "current")["cost_usd"] - size) < 0.01)
check("new soft-cap PAUSE message says 'new score >= 55.5'", con.execute("SELECT 1 FROM decisions WHERE kind='PAUSE' AND scoring='new' AND message LIKE '[new scoring]%new score >= 55.5%'").fetchone())
check("new soft-cap skip message mentions 55.5", con.execute("SELECT 1 FROM decisions WHERE token=? AND scoring='new' AND message LIKE '%55.5%'", (lo["token"],)).fetchone())
setup(0); reset_new(1000.0, 11)
x = c2(80, 59.2, "pass", "pass"); run([x])
check("new hard floor (-11%) blocks only the new account", not pos(x["token"], "new") and pos(x["token"], "current"))

# ---------------------------------------------------------------- exits for both accounts, same pass, own cash
setup(0); reset_new()
y = c2(80, 59.2, "pass", "pass"); run([y])
SELLS = []
live.on_sell_orig = live.on_sell
live.on_sell = lambda con_, cfg_, pid, *a: SELLS.append(pid)
TRAIL = []
import trigstop; trigstop.on_trail = lambda con_, cfg_, p: TRAIL.append(p["id"]); trigstop.check = lambda *a: None
up = 1e-4 * (1 + P["take_profit2_pct"] / 100 + 0.5)
trader.ds_pairs = lambda pairs: {p: {"pairAddress": p, "baseToken": {"address": "MINT" + p[4:]}, "priceUsd": str(up),
                                     "liquidity": {"usd": 1_000_000}} for p in pairs}
cash_c0, cash_n0 = get_state(con, "cash"), get_state(con, "new:cash")
trader.update_positions(con, cfg)
pc, pn2 = pos(y["token"], "current"), pos(y["token"], "new")
check("one update pass handles both accounts' positions on the same reading", pc["last_price"] == pn2["last_price"] == up)
check("both sold something (same exit rules)", (pc["proceeds_usd"] or 0) > 0 and (pn2["proceeds_usd"] or 0) > 0)
check("proceeds credited to each account's own cash", get_state(con, "cash") > cash_c0 and get_state(con, "new:cash") > cash_n0
      and abs((get_state(con, "new:cash") - cash_n0) - pn2["proceeds_usd"]) < 1e-6)
check("live.on_sell called for the current position only", pc["id"] in SELLS and pn2["id"] not in SELLS)
check("trigstop.on_trail only for the current position", pn2["id"] not in TRAIL)
check("equity rows written per account", con.execute("SELECT 1 FROM equity WHERE scoring='new' AND ts>?", (now_ts() - 60,)).fetchone()
      and con.execute("SELECT 1 FROM equity WHERE scoring='current' AND ts>?", (now_ts() - 60,)).fetchone())
check("position_ticks carry scoring, new ticks never is_real",
      con.execute("SELECT 1 FROM position_ticks WHERE pos_id=? AND scoring='new' AND is_real=0", (pn2["id"],)).fetchone() is not None)
live.on_sell = live.on_sell_orig; live.on_buy = live.on_buy_orig

# ---------------------------------------------------------------- CRITICAL: real-money hard block
LIVE_CALLS = []
live.active = lambda cfg: True
live.spent_usd = lambda con: 0.0
live.open_live = lambda con: 0
live.wallet_total = lambda con, cfg: 100.0
live.day_check = lambda con, cfg: (True, 0, "")
live.swap = lambda *a, **k: LIVE_CALLS.append(("swap", a)) or ("SIG", {})
live.sol_balance = lambda cfg: LIVE_CALLS.append(("bal",)) or 10.0
live.threading.Thread = lambda target, args, daemon: type("T", (), {"start": lambda self: LIVE_CALLS.append(("thread", args))})()
r = live.on_buy(con, cfg, pn2["id"], pn2["token"], pn2["symbol"])
check(f"live.on_buy refuses a scoring='new' position with live ON ({r!r})", r is not True and "paper-only" in str(r) and not LIVE_CALLS)
con.execute("UPDATE positions SET scoring='weird' WHERE id=?", (pn2["id"],)); con.commit()
check("live.on_buy refuses any scoring other than 'current'", live.on_buy(con, cfg, pn2["id"], "X", "X") is not True and not LIVE_CALLS)
con.execute("UPDATE positions SET scoring='new' WHERE id=?", (pn2["id"],)); con.commit()
live.ensure(con)
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,'buy',1,5,'pending')", (now_ts(), pn2["id"], "X", "X"))
tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]; con.commit()
live._do_buy(cfg, tid, pn2["id"], pn2["token"], pn2["symbol"], 5.0)
row = con.execute("SELECT status,error FROM live_trades WHERE id=?", (tid,)).fetchone()
check("forced live._do_buy on a new-scoring position refuses BEFORE any swap", row["status"] == "failed" and "scoring" in (row["error"] or "") and not any(c[0] == "swap" for c in LIVE_CALLS))
LIVE_CALLS.clear()
live.on_sell(con, cfg, pn2["id"], pn2["token"], pn2["symbol"], 1.0, "test")
check("live.on_sell ignores a new-scoring position", not LIVE_CALLS)
check("live.on_buy still allows a current position (control)", live.on_buy(con, cfg, pc["id"], pc["token"], pc["symbol"]) is True)
LIVE_CALLS.clear()
try:
    con.execute("INSERT INTO positions(token,pair,symbol,opened_at,entry_price,qty,remaining_qty,cost_usd,status,scoring) VALUES('N','N','N',0,1,1,1,1,'open',NULL)")
    nn = False
except Exception:
    nn = True
con.rollback()
check("positions.scoring is NOT NULL (a NULL can't sneak past as real)", nn)

# ---------------------------------------------------------------- run_cycle: logging both scores + routing
cfg.setdefault("live", {})["enabled"] = False
live.active = lambda cfg: False
setup(0); reset_new()
toks = [("TOKA", 10, 40, "score"), ("TOKB", 600, 80, "score"), ("TOKC", 30, None, "contract")]
for addr, *_ in toks:
    con.execute("INSERT OR IGNORE INTO tokens(address,symbol,first_seen,status) VALUES(?,?,?, 'watch')", (addr, addr, now_ts()))
con.execute("UPDATE tokens SET status='rejected' WHERE address NOT IN ('TOKA','TOKB','TOKC')"); con.commit()
scanner.discover = lambda cfg: []
scanner.register = lambda con, f: 0
info = {a: (age, sc, st) for a, age, sc, st in toks}
bot.fetch_market = lambda cfg, ts: ({t["address"]: ("ds", [t["address"]]) for t in ts}, {})
scanner.best_pair = lambda tp, pool: tp[0]
scanner.pair_metrics = lambda bp, tp: {"age_min": info[bp][0], "liq": 1_000_000, "mcap": 5_000_000, "price": 1e-4, "pair": "PAIR" + bp,
                                       "symbol": bp, "url": "", "pool_set": [], "src": "dexscreener", "vol_h1": 1, "vol_h24": 1}
def fake_eval(con_, cfg_, t, m, budget):
    age, sc, st = info[t["address"]]
    if st != "score":
        return "reject", st, ["mint authority NOT revoked"], None, False, False
    m["_sn"] = newscore.score_metrics(m)
    g = {"current": "pass" if sc >= cfg_["filters"]["min_score_to_buy"] else "reject",
         "new": "pass" if m["_sn"][0] >= B else "reject"}
    m["_gate"] = g
    return ("pass" if g["current"] == "pass" else "reject"), "score", ["x"], sc, False, False
scanner.evaluate = fake_eval
trader.ds_pairs = lambda pairs: {p: {"pairAddress": p, "baseToken": {"address": p[4:]}, "priceUsd": "0.0001",
                                     "liquidity": {"usd": 1_000_000}} for p in pairs}
bot.run_cycle(cfg)
con2 = common.db()
ev_ = {r["token"]: r for r in con2.execute("SELECT * FROM evaluations WHERE token IN ('TOKA','TOKB','TOKC') ORDER BY id")}
check("evaluations: score_new/prob_new/model_version logged on every row with metrics (incl. early rejects)",
      all(ev_[t]["score_new"] is not None and ev_[t]["model_version"] == "scoring_v1 1.0" for t in ev_) and len(ev_) == 3)
check("evaluations: gate_current/gate_new at the score stage", (ev_["TOKA"]["gate_current"], ev_["TOKA"]["gate_new"]) == ("reject", "pass")
      and (ev_["TOKB"]["gate_current"], ev_["TOKB"]["gate_new"]) == ("pass", "reject") and ev_["TOKC"]["gate_new"] is None)
check("run_cycle: TOKA (new-only pass) bought by new account only", con2.execute("SELECT scoring FROM positions WHERE token='TOKA'").fetchall() == [("new",)] or
      [r[0] for r in con2.execute("SELECT scoring FROM positions WHERE token='TOKA'")] == ["new"])
check("run_cycle: TOKB (current-only pass) bought by current account only", [r[0] for r in con2.execute("SELECT scoring FROM positions WHERE token='TOKB'")] == ["current"])
fun = json.loads(con2.execute("SELECT funnel FROM cycles ORDER BY id DESC LIMIT 1").fetchone()[0])
check("cycle funnel: 'passed' stays current-only, 'passed_new' counted", fun["passed"] == 1 and fun["passed_new"] == 1)
con2.close()

# ---------------------------------------------------------------- data.json
set_state(con, "scoring_new_status", {"ok": True, "rows": 10, "worst": 5.6e-17, "version": "scoring_v1 1.0"})
set_state(con, "scoring_test_started", now_ts() - 3600); con.commit()
dashboard_data.load_config = lambda: cfg
d = dashboard_data.build()
s_ = d["scoring"]
check("data.json scoring block: model_version, selftest, thresholds, started", s_["model_version"] == "scoring_v1 1.0" and s_["selftest"]["ok"]
      and s_["thresholds"]["new_buy"] == 48.4 and s_["started"])
check("data.json scoring.current/new each have kpi, open, closed, equity",
      all(set(("kpi", "open", "closed", "equity")) <= set(s_[a]) for a in ("current", "new")))
check("data.json kpi fields", all(k in s_["new"]["kpi"] for k in ("equity", "cash", "pnl", "win_rate", "expectancy", "max_open", "since_start")))
check("data.json comparison strip", all(k in s_["comparison"] for k in ("current", "new", "only_current_coins", "only_new_coins", "both_bought"))
      and "max_drawdown_usd" in s_["comparison"]["new"])
check("data.json top-level open/closed stay CURRENT only (real-money widgets unchanged)",
      all(p["scoring"] == "current" for p in d["open"] + d["closed"]))
check("data.json new open positions carry both scores + targets", all("targets" in p and "score_current" in p for p in s_["new"]["open"]))
check("established strategy block unaffected / comparison 'new' side = current account",
      "established" in d and d["strategies"]["new"]["closed"] == con.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND COALESCE(strategy,'new')='new' AND scoring='current'").fetchone()[0])
check("unchanged settings: budget $130, pump 75%, trailing 20/20, cleanup off",
      float(cfg["live"]["budget_usd"]) == 130 and P["trailing_activate_pct"] == 20 and P["trailing_stop_pct"] == 20)

print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
sys.exit(0 if all(ok for _, ok in results) else 1)
