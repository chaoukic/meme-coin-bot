"""Older-coin (established) strategy - PAPER ONLY. Mocked Jupiter / DexScreener; runs on a COPY of the DB.
Run: .venv/bin/python tests/test_established.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_daycap.py")).read().split("\nsetup(")[0])  # DB copy + helpers
import established as es, live, notify, trader
from common import get_state, set_state

cfg.setdefault("established", {}).update({"enabled": True, "starting_balance_usd": 300, "position_usd": 15, "max_open": 3,
                                          "max_entries_per_round": 1, "daily_stop_pct": 5})
E = es.E(cfg)
con.execute("UPDATE positions SET status='closed', closed_at=0 WHERE status='open'")
set_state(con, "estab_cash", 300.0); set_state(con, "estab_starting_balance", 300.0)
set_state(con, "estab_day", None); set_state(con, "estab_watch", {}); set_state(con, "manual_pause", {"on": False})
con.commit()
EST_CLOSED0 = es.stats(con, "established")["closed"]
iso_days = lambda d: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - d * 86400))

def rec(mint, sym="OLDC", price=1.0, age_d=30, liq=500_000, v24=1_000_000, holders=20_000, org=80, c5=0.2, c1=2.0, c6=-6.0,
        c24=-5.0, tags=("verified", "meme"), mint_auth=None, supply=10_000_000):
    half = lambda v: v / 2
    return {"id": mint, "symbol": sym, "name": sym, "usdPrice": price, "liquidity": liq, "mcap": price * supply, "holderCount": holders,
            "organicScore": org, "tags": list(tags), "firstPool": {"id": "POOL" + mint, "createdAt": iso_days(age_d)},
            "audit": {"mintAuthorityDisabled": mint_auth is None, "freezeAuthorityDisabled": True, "topHoldersPercentage": 20},
            **({"mintAuthority": mint_auth} if mint_auth else {}),
            "stats5m": {"priceChange": c5, "buyVolume": 500, "sellVolume": 500, "numBuys": 10, "numSells": 8},
            "stats1h": {"priceChange": c1, "buyVolume": 30_000, "sellVolume": 20_000, "numBuys": 300, "numSells": 200, "numNetBuyers": 12},
            "stats6h": {"priceChange": c6}, "stats24h": {"priceChange": c24, "buyVolume": half(v24), "sellVolume": half(v24)}}

LISTS = {}
es.fetch_lists = lambda cfg_: (dict(LISTS), 6, 0)
PRICES = {}
def fake_batch(mints):
    return {m: PRICES[m] for m in mints if m in PRICES}
es.batch = fake_batch
es.ds_pool = lambda mint: ("ok", {"pair": "PAIR" + mint, "url": "https://dexscreener.com/solana/pair" + mint.lower(),
                           "price": (LISTS.get(mint) or PRICES.get(mint))["usdPrice"], "liq": 900_000})
LIVE_CALLS = []
live.active = lambda cfg_: True
live.spent_usd = lambda con_: 0.0
live.open_live = lambda con_: 0
live.day_check = lambda con_, cfg_: (True, 0, "ok")
live.threading.Thread = lambda target, args, daemon: type("T", (), {"start": lambda self: LIVE_CALLS.append(("thread", args))})()
live.swap = lambda *a, **k: LIVE_CALLS.append(("swap", a)) or ("SIG", {})

# ---- 1. rules (pure)
m = es.metrics
chk = lambda r: (es.excluded(m(r), E), es.quality(m(r), E), es.timing(m(r), E))
check("SOL excluded", es.excluded(m(rec("So11111111111111111111111111111111111111112", "SOL", tags=("major",))), E))
check("USDC (mint list) excluded", es.excluded(m(rec("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC")), E))
check("LST tag excluded", es.excluded(m(rec("LSTX", "abcSOL", tags=("lst",))), E))
check("wrapped major by symbol (cbBTC) excluded", es.excluded(m(rec("WB1", "cbBTC")), E))
check("stablecoin-like $1 peg excluded", es.excluded(m(rec("ST1", "DOLLA", price=1.001, c24=0.1)), E))
check("tokenized stock (rwa tag) excluded", es.excluded(m(rec("RW1", "NVDAx", tags=("rwa", "stocks"))), E))
check("ordinary older meme coin not excluded", es.excluded(m(rec("OK1")), E) is None)
check("pool 10h old -> age reject (new-coin strategy's range)", (es.quality(m(rec("Y1", age_d=10 / 24)), E) or [""])[0] == "age")
check("liquidity $60k -> reject", (es.quality(m(rec("L1", liq=60_000)), E) or [""])[0] == "liquidity")
check("500 holders -> reject", (es.quality(m(rec("H1", holders=500)), E) or [""])[0] == "holders")
check("organic score 30 -> reject", (es.quality(m(rec("O1", org=30)), E) or [""])[0] == "organic")
check("mint authority still active -> reject", (es.quality(m(rec("MA1", mint_auth="SomeAuth")), E) or [""])[0] == "contract")
check("good coin passes quality", es.quality(m(rec("Q1")), E) is None)
check("pullback -6% then bounce +2% -> entry", es.timing(m(rec("T1")), E) is None)
check("1h pump +12% -> no entry (don't chase)", "chase" in es.timing(m(rec("T2", c1=12)), E)[1])
check("no pullback (6h +4%) -> wait", "no pullback" in es.timing(m(rec("T3", c6=4)), E)[1])
check("still falling (1h -2%) -> wait", "no bounce" in es.timing(m(rec("T4", c1=-2)), E)[1])
check("6h -30% -> falling knife", "knife" in es.timing(m(rec("T5", c6=-30)), E)[1])
check("+300% in 24h -> still pumping", "pumping" in es.timing(m(rec("T6", c24=300)), E)[1])

# ---- 2. round: evaluates candidates, opens ONE paper position tagged 'established'
LISTS.update({"EST1": rec("EST1", "OLDA", org=90), "EST2": rec("EST2", "OLDB", org=70), "EST3": rec("EST3", "OLDC", c1=15),
              "SOLX": rec("So11111111111111111111111111111111111111112", "SOL", tags=("major",)), "YNG": rec("YNG", age_d=0.2)})
LISTS["SOLX"]["id"] = "So11111111111111111111111111111111111111112"
paper_cash0, paper_eq0 = trader.cash(con, cfg), trader.equity(con, cfg)
n_live0 = con.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0] if con.execute("SELECT name FROM sqlite_master WHERE name='live_trades'").fetchone() else 0
s = es.run_round(con, cfg)
f = s["funnel"]
check(f"round evaluated {f['evaluated']}, quality ok {f.get('quality_ok')}, signals {f['passed']}, opened {f['opened']}",
      f["evaluated"] == 5 and f.get("quality_ok") == 3 and f["passed"] == 2 and f["opened"] == 1)
p = con.execute("SELECT * FROM positions WHERE strategy='established' AND status='open'").fetchone()
check("highest organic score bought first (OLDA), strategy='established'", p and p["symbol"] == "OLDA" and p["strategy"] == "established")
check("size = position_usd $15 from the older-coin pot", p and abs(p["cost_usd"] - 15) < 1e-6 and abs(get_state(con, "estab_cash") - 285) < 1e-6)
check("new-coin strategy cash and equity unchanged", abs(trader.cash(con, cfg) - paper_cash0) < 1e-9 and abs(trader.equity(con, cfg) - paper_eq0) < 1e-6)
check("Telegram buy alert says 'PAPER (older coin)'", con.execute("SELECT 1 FROM outbox WHERE key=? AND text LIKE '%PAPER (older coin) BUY OLDA%'", (f"buy:{p['id']}",)).fetchone())
check("evaluations stored in estab_evals", con.execute("SELECT COUNT(*) FROM estab_evals WHERE round_ts=?", (s["ts"],)).fetchone()[0] >= 3)

# ---- 3. real wallet NEVER copies these trades (enforced in live.py, not config)
n_live1 = con.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0] if con.execute("SELECT name FROM sqlite_master WHERE name='live_trades'").fetchone() else 0
check("no real trade row and no live thread from the older-coin buy", n_live1 == n_live0 and not LIVE_CALLS)
r = live.on_buy(con, cfg, p["id"], p["token"], p["symbol"])
check(f"live.on_buy refuses an 'established' position even with live on ({r!r})", r is not True and "paper-only" in str(r) and not LIVE_CALLS)
live.on_sell(con, cfg, p["id"], p["token"], p["symbol"], 1.0, "test")
check("live.on_sell ignores an 'established' position", not LIVE_CALLS)
live.ensure(con)
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,'buy',1,5,'pending')", (time.time(), p["id"], p["token"], p["symbol"]))
tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]; con.commit()
live.db = lambda: common.db()
live._do_buy(cfg, tid, p["id"], p["token"], p["symbol"], 5.0)
row = con.execute("SELECT status, error FROM live_trades WHERE id=?", (tid,)).fetchone()
check("even a forced live._do_buy refuses before any swap (status failed, no swap)", row["status"] == "failed" and "paper only" in (row["error"] or "") and not LIVE_CALLS)
con.execute("DELETE FROM live_trades WHERE id=?", (tid,)); con.commit()
r2 = live.on_buy(con, cfg, 99999999, "X", "X")
check("live.on_buy refuses an unknown position id", r2 is not True)

# ---- 4. new-coin strategy does not see older-coin positions
seen = []
trader.ds_pairs = lambda pairs: seen.extend(pairs) or {}
trader.jup_backup_pairs = lambda cfg_, ps: {}
trader.update_positions(con, cfg)
check("trader.update_positions never touches older-coin positions", p["pair"] not in seen)
check("new-coin open count excludes older-coin positions",
      con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND " + trader.NEW).fetchone()[0] == 0)

# ---- 5. exits with Jupiter batch prices
def price_to(mint, px):
    PRICES[mint] = rec(mint, "OLDA", price=px, org=90)
price_to("EST1", 1.13); es.update_positions(con, cfg)
p = con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
check("+13% -> TP1 sells half (12% target)", p["tp1_done"] == 1 and abs(p["remaining_qty"] - p["qty"] / 2) < 1e-6)
check("partial-sell alert says 'PAPER (older coin)'", con.execute("SELECT 1 FROM outbox WHERE key LIKE ? AND text LIKE '%PAPER (older coin) PARTIAL SELL%'", (f"sell:{p['id']}:partial%",)).fetchone())
price_to("EST1", 1.20); es.update_positions(con, cfg)
price_to("EST1", 1.13); es.update_positions(con, cfg)
p = con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
check(f"peak +20% then -5.8% off peak -> trailing stop closes ({p['exit_reason']})", p["status"] == "closed" and "trailing" in (p["exit_reason"] or ""))
check("closed P&L positive and booked to the older-coin pot only", p["pnl_usd"] > 0 and get_state(con, "estab_cash") > 300 and abs(trader.cash(con, cfg) - paper_cash0) < 1e-9)
# stop loss on another coin
LISTS.clear(); LISTS["EST4"] = rec("EST4", "OLDD", org=85)
es.run_round(con, cfg)
q = con.execute("SELECT * FROM positions WHERE token='EST4' AND strategy='established'").fetchone()
PRICES["EST4"] = rec("EST4", "OLDD", price=0.91); es.update_positions(con, cfg)
q = con.execute("SELECT * FROM positions WHERE id=?", (q["id"],)).fetchone()
check(f"-9% -> stop loss (-8%) closes ({q['exit_reason']})", q["status"] == "closed" and "stop loss" in q["exit_reason"])
check("sell alert says 'PAPER (older coin) SELL'", con.execute("SELECT 1 FROM outbox WHERE key=? AND text LIKE '%PAPER (older coin) SELL OLDD%'", (f"sell:{q['id']}:full",)).fetchone())
check("re-entry cooldown: same coin not bought again", es.run_round(con, cfg)["funnel"]["opened"] == 0)

# ---- 6. separate daily stop
LISTS.clear(); LISTS["EST5"] = rec("EST5", "OLDE")
before = (get_state(con, "day_mode"), get_state(con, "paused_today"), get_state(con, "day_start_equity"), trader.equity(con, cfg))
set_state(con, "estab_day", {"date": common.toronto_date(), "start": es.equity(con, cfg) / 0.94, "stopped": False}); con.commit()
check("older-coin daily stop (-6% vs -5%) blocks older-coin entries", es.run_round(con, cfg)["funnel"]["opened"] == 0
      and (get_state(con, "estab_day") or {}).get("stopped"))
after = (get_state(con, "day_mode"), get_state(con, "paused_today"), get_state(con, "day_start_equity"), trader.equity(con, cfg))
check("...and the new-coin strategy's day mode / caps / equity are untouched", before == after)
set_state(con, "estab_day", None); con.commit()

# ---- 7. dashboard / data.json fields
d = es.dashboard(con, cfg)
check("dashboard block has kpi/open/closed/last_round/rules", all(k in d for k in ("kpi", "open", "closed", "last_round", "rules")) and d["paper_only"])
check("closed rows carry strategy='established'", d["closed"] and all(r["strategy"] == "established" for r in d["closed"]))
st_new = es.stats(con, "new")
check("separate stats per strategy", es.stats(con, "established")["closed"] == EST_CLOSED0 + 2 and st_new["strategy"] == "new")
import dashboard_data
dashboard_data.load_config = lambda: cfg
dd = dashboard_data.build()
check("data.json: 'established' + 'strategies' blocks; main open/closed lists are new-coin only",
      "established" in dd and set(dd["strategies"]) == {"new", "established"}
      and all((r.get("strategy") or "new") == "new" for r in dd["closed"] + dd["open"]))

fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
