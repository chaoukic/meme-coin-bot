"""Unit-style checks for the price guard, duplicate-entry block and fill cap. Runs on a COPY of the DB.
Run: .venv/bin/python tests/test_guard.py"""
import os, sqlite3, sys, shutil, tempfile
sys.path.insert(0, "/workspace/memebot")
import common
tmp = tempfile.mkdtemp(); TEST_DB = os.path.join(tmp, "test.db")
src = sqlite3.connect(common.DB_PATH); dst = sqlite3.connect(TEST_DB); src.backup(dst); dst.close(); src.close()
common.DB_PATH = TEST_DB
import notify; notify.DB_PATH = TEST_DB
import trader, priceguard as pg

cfg = common.load_config(); P = cfg["paper"]
cfg.setdefault("live", {})["enabled"] = False  # tests must never touch the real wallet
cfg.setdefault("rugcheck", {})["enabled"] = False  # no network in tests
con = common.db(); common.init_db()
T = [1_800_000_000.0]
clock = lambda: T[0]
trader.now_ts = clock; pg.now_ts = clock
GT = {"v": None}
pg.gt_pool = lambda pair, token: GT["v"]
results = []
def check(name, cond):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)

def mkpos(sym, entry, last, liq, tp1=0, token=None, pair=None):
    token = token or f"TOK{sym}{T[0]}"; pair = pair or f"PAIR{sym}{T[0]}"
    qty = 40 / entry
    cur = con.execute("""INSERT INTO positions(token,pair,symbol,url,opened_at,entry_price,entry_eff,qty,remaining_qty,
        cost_usd,peak_price,last_price,last_update,status,score,entry_liq,pnl_usd,pnl_pct,proceeds_usd,tp1_done,last_liq,guard_last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',70,?,0,0,0,?,?,?)""",
        (token, pair, sym, "", T[0] - 3600, entry, entry, qty, qty / 2 if tp1 else qty, 40, last, last, T[0], liq, tp1, liq, T[0]))
    con.commit(); return cur.lastrowid
def reading(pid, price, liq, mcap_supply=1e9, vol5=5000, tx5=40):
    p = con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    return {"pairAddress": p["pair"], "baseToken": {"address": p["token"]}, "priceUsd": str(price),
            "liquidity": {"usd": liq}, "marketCap": price * mcap_supply, "volume": {"m5": vol5, "h1": 50000, "h6": 300000, "h24": 900000},
            "txns": {"m5": {"buys": tx5, "sells": tx5}}, "pairCreatedAt": (T[0] - 5 * 3600) * 1000}
pos = lambda pid: con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
def step(pid, r, dt=45):
    T[0] += dt
    trader.check_exits(con, cfg, pos(pid), r); con.commit(); return pos(pid)

# 1) DDOS replay: last good $0.000199, entry liq $30k; reading $0.03061
pid = mkpos("DDOSX", 9.42e-05, 0.000199, 30000, tp1=1)
step(pid, reading(pid, 0.000199, 30000))
p = step(pid, reading(pid, 0.03061, 0))
check("DDOS $0.03061 with liquidity $0 rejected (no sell, price not updated)", p["status"] == "open" and p["last_price"] == 0.000199)
GT["v"] = {"price": 0.0306, "liq": 0.0035}
p = step(pid, reading(pid, 0.03061, 30000))
check("DDOS $0.03061 with stale DS liquidity but GT pool drained rejected", p["status"] == "open" and p["last_price"] == 0.000199)
GT["v"] = None
p = step(pid, reading(pid, 0.03061, 30000, vol5=0, tx5=0))
p = step(pid, reading(pid, 0.03061, 30000, vol5=0, tx5=0))
check("DDOS $0.03061 repeated with zero trades (no 2nd source) rejected", p["status"] == "open")
p = step(pid, reading(pid, 0.03061, 30000, mcap_supply=1e7))
check("reading with inconsistent market cap rejected", p["status"] == "open" and p["last_price"] == 0.000199)
r = reading(pid, 0.03061, 30000); r["pairAddress"] = "OTHERPAIR"
p = step(pid, r)
check("reading from a different pair rejected", p["status"] == "open" and p["last_price"] == 0.000199)
# pool really drained -> force-close at a liquidity-capped fill (~$0), confirmed on 2 checks
step(pid, reading(pid, 2.9e-09, 0.0035))
p = step(pid, reading(pid, 2.9e-09, 0.0035))
check("drained pool closes after 2 checks as rug with ~$0 fill", p["status"] == "closed" and "rug" in p["exit_reason"] and p["proceeds_usd"] < 0.01)

# 2) real 60% crash with healthy pool: waits one check, then stop loss fires
pid = mkpos("CRASH", 1e-4, 1e-4, 40000)
p = step(pid, reading(pid, 4e-5, 30000))
check("60% crash: first reading held for confirmation", p["status"] == "open")
p = step(pid, reading(pid, 3.9e-5, 29000))
check("60% crash: exits via stop loss after confirmation on next check", p["status"] == "closed" and "stop loss" in p["exit_reason"])
pid = mkpos("CRASH2", 1e-4, 1e-4, 40000)
GT["v"] = {"price": 4.05e-5, "liq": 30000}
p = step(pid, reading(pid, 4e-5, 30000))
check("60% crash confirmed by GeckoTerminal exits immediately", p["status"] == "closed" and "stop loss" in p["exit_reason"])
GT["v"] = None
pid = mkpos("DROP30", 1e-4, 1e-4, 40000)
p = step(pid, reading(pid, 7e-5, 38000))
check("normal 30% drop exits immediately via stop loss", p["status"] == "closed" and "stop loss" in p["exit_reason"])
pid = mkpos("RUGSELL", 1e-4, 1e-4, 40000)
step(pid, reading(pid, 1e-6, 50)); p = step(pid, reading(pid, 1e-6, 50))
check("real rug (99% crash, liquidity pulled) still exits", p["status"] == "closed")
pid = mkpos("STALE", 1e-4, 1e-4, 40000)
p = step(pid, reading(pid, 1.2e-4, 40000), dt=73 * 60)
check("first reading after a 73-min freeze is not acted on", p["last_price"] == 1e-4)
p = step(pid, reading(pid, 1.2e-4, 40000))
check("second reading after the freeze is used", p["last_price"] == 1.2e-4)
pid = mkpos("VD", 1e-4, 1.05e-4, 40000)
r = reading(pid, 1.05e-4, 40000); r["volume"]["h1"] = 0
p = step(pid, r)
check("volume-decay exit waits for a second check", p["status"] == "open")
p = step(pid, r)
check("volume-decay exit fires on the second agreeing check", p["status"] == "closed" and "volume decay" in p["exit_reason"])

# 3) duplicates
common.set_state(con, 'paused_today', False); common.set_state(con, 'day', common.toronto_date()); common.set_state(con, 'day_start_equity', trader.equity(con, cfg))  # real-DB day cap must not interfere
# older-coin (strategy='established') positions are a separate strategy and deliberately don't block new-coin buys: use a new-coin row
con.execute("UPDATE positions SET status='closed', closed_at=? WHERE status='open' AND id NOT IN (SELECT MIN(id) FROM positions WHERE status='open' AND COALESCE(strategy,'new')='new')", (T[0] - 10 * 86400,))
con.commit()
openp = con.execute("SELECT * FROM positions WHERE status='open' AND COALESCE(strategy,'new')='new' ORDER BY id LIMIT 1").fetchone()
bought = []
trader.ds_pairs = lambda pairs: {}  # a buy would need a fresh re-read; count attempts that get past the duplicate check
orig_reject = pg.reject
pg.reject = lambda con_, tok, sym, msg, log_decision=True: bought.append((sym, msg))
n0 = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
base = {"score": 90, "price": 1e-4, "liq": 50000, "url": "", "symbol": "NEWCOIN"}
trader.try_entries(con, cfg, [dict(base, token=openp["token"], pair="SOMEOTHERPAIR")]); con.commit()
check("duplicate buy by same mint blocked", con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == n0 and not bought)
trader.try_entries(con, cfg, [dict(base, token="SOMEOTHERMINT", pair=openp["pair"])])
check("duplicate buy by same pair blocked", not bought)
trader.try_entries(con, cfg, [dict(base, token="MINTZ", pair="PAIRZ", symbol=openp["symbol"].lower())])
check("buy of a same-ticker copycat blocked", not bought)
cl = con.execute("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 1").fetchone()
con.execute("UPDATE positions SET closed_at=? WHERE id=?", (T[0] - 3600, cl["id"])); con.commit()
trader.try_entries(con, cfg, [dict(base, token=cl["token"], pair="P2", symbol="ZZZ")])
check("re-entry within 24h cooldown (same mint) blocked", not bought)
trader.try_entries(con, cfg, [dict(base, token="FRESHMINT", pair="FRESHPAIR", symbol="FRESH")])
check("a genuinely new coin gets past the duplicate check (to the fresh re-read)", bought and "re-read" in bought[-1][1])
pg.reject = orig_reject

# 4) fill cap
G = pg.cfg_guard(cfg)
qty, price = 217516, 0.03061
check("fill cap: $6,658 sell into a $0.0035 pool books ~$0", pg.sell_value(qty, price, P, G, 0.0035) < 0.01)
v = pg.sell_value(qty, price, P, G, 20000); plain = qty * price * 0.99 * 0.995
check(f"fill cap: $6,658 sell into a $20k pool capped at ${v:,.0f} (< ${plain:,.0f})", v < plain and abs(v - 10000 * 6658.2 / 16658.2 * 0.995) < 5)
check("fill cap: small sell in a deep pool ~ normal slippage+fee", abs(pg.sell_value(1000, 0.01, P, G, 1e6) - 10 * 0.99 * 0.995) < 0.01)
check("buy price impact: $170 into $38k pool -> 1.0% slip floor", abs(pg.buy_price_multiplier(170, 38000, P, G) - 1.01) < 1e-9)
check("buy price impact: $1000 into $20k pool -> 10% impact", abs(pg.buy_price_multiplier(1000, 20000, P, G) - 1.1) < 1e-9)

con.close(); shutil.rmtree(tmp)
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
