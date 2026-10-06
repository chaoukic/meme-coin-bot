"""Pre-buy Jupiter fallback (jupcheck.py) with mocked DexScreener / Jupiter / RugCheck. Runs on a COPY of the DB,
never touches the network or the real wallet. Run: .venv/bin/python tests/test_jupfallback.py"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_daycap.py")).read().split("\nsetup(")[0])  # DB copy + helpers
import scanner, jupcheck, live, rugcheck
from common import get_state

cfg["entry"] = {"prebuy_fallback": "jupiter_quote", "fallback_depth_usd": 30, "fallback_max_trade_impact_pct": 2.0,
                "fallback_min_jup_liq_factor": 0.5}
cfg.setdefault("live", {})["trade_usd"] = 5
SOLPX = 150.0
live.sol_price = lambda: SOLPX
jupcheck._key = lambda: "TESTKEY"
MARKET = {}      # mint -> dict(price, jup_liq, pool_liq, chg_h1, holders, quote_ok, impact_bias)
DS_MODE = {"m": "busy"}
def fake_ds_pairs(pairs):
    scanner.DS_FAILED.clear()
    if DS_MODE["m"] == "busy":
        scanner.DS_FAILED.update(pairs)
    return {}
trader.ds_pairs = fake_ds_pairs
RC = {"ok": True, "calls": []}
def fake_rc(mint, cfg_, pair=None):
    RC["calls"].append(cfg_.get("rugcheck", {}))
    return (True, "rug check ok") if RC["ok"] else (False, "rug check unavailable (RuntimeError: RugCheck unavailable)")
rugcheck.check = fake_rc

def fake_http_get(kind, url, params=None, retries=3, headers=None, skip_spacing=False):
    assert headers and headers.get("x-api-key") == "TESTKEY"
    if url.endswith("/search"):
        mk = MARKET.get(params["query"])
        if not mk:
            return []
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 3600))
        st = {"priceChange": mk.get("chg_h1", 10), "buyVolume": 20000, "sellVolume": 15000, "numBuys": 300, "numSells": 250}
        return [{"id": params["query"], "symbol": "X", "decimals": 6, "usdPrice": mk["price"], "liquidity": mk["jup_liq"],
                 "mcap": 500000, "holderCount": mk.get("holders", 800), "firstPool": {"id": "P", "createdAt": ts},
                 "stats1h": st, "stats6h": st, "stats24h": dict(st, numBuys=3000, numSells=2500, buyVolume=200000, sellVolume=150000)}]
    if url == jupcheck.QUOTE:
        mk = MARKET[params["outputMint"]]
        if not mk.get("quote_ok", True):
            return None
        usd = params["amount"] / 1e9 * SOLPX
        R = mk["pool_liq"] / 2                         # constant-product pool, 0.25% fee
        got_usd = usd * (1 - 0.0025) * R / (R + usd)
        out = got_usd / mk["price"] * 1e6
        imp = usd / (R + usd) + mk.get("impact_bias", 0)
        return {"inAmount": str(params["amount"]), "outAmount": str(int(out)), "priceImpactPct": str(imp),
                "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}, "percent": 100}]}
    raise AssertionError(url)
jupcheck.http_get = fake_http_get

def fb_buy(**mk):
    c = cand(80)
    MARKET[c["token"]] = dict({"price": 1e-4, "jup_liq": 100000, "pool_liq": 200000}, **mk)
    c["liq"] = 200000
    rejected.clear()
    trader.try_entries(con, cfg, [c]); con.commit()
    r = con.execute("SELECT * FROM positions WHERE token=?", (c["token"],)).fetchone()
    return r, (rejected[-1] if rejected else ""), c

rejected = []
orig_reject = pg.reject
pg.reject = lambda con_, tok, sym, msg, log_decision=True: rejected.append(msg)

# pure math
check("depth rule: $30 into a $30k pool is the limit at ~0.1996% impact",
      abs(jupcheck.max_depth_impact(30, 30000) * 100 - 0.1996) < 0.001 and abs(jupcheck.depth_liq(30, jupcheck.max_depth_impact(30, 30000)) - 30000) < 1)

setup(1)
r, why, c = fb_buy()
check("deep pool ($200k), DexScreener busy: fallback buy goes through", r is not None)
check("position tagged entry_source='jup_fallback' with the quote numbers stored",
      r and r["entry_source"] == "jup_fallback" and json.loads(r["entry_check"])["liq_est"] and "depth_impact_pct" in r["entry_check"])
check("decision log carries the fallback tag", con.execute("SELECT 1 FROM decisions WHERE kind='BUY' AND token=? AND message LIKE '%entry_source=jup_fallback%'", (c["token"],)).fetchone())
check("Telegram buy alert says '(DexScreener busy, checked via Jupiter quote)'",
      con.execute("SELECT 1 FROM outbox WHERE key=? AND text LIKE '%(DexScreener busy, checked via Jupiter quote)%'", (f"buy:{r['id']}",)).fetchone() if r else False)
check("RugCheck forced on + fail-closed on the fallback path", RC["calls"] and RC["calls"][-1].get("enabled") and RC["calls"][-1].get("fail_closed"))
check("paper fill sized on min(scan liquidity, quote-implied liquidity)", r and r["entry_liq"] <= 200000 + 1)
check("data.json row would include entry_source", r and "entry_source" in dict(r).keys())

setup(1); r, why, _ = fb_buy(pool_liq=20000, jup_liq=60000)
check(f"thin pool ($20k by quotes): rejected as too thin [{why[-90:]}]", r is None and "too thin" in why)
setup(1); r, why, _ = fb_buy(pool_liq=300, jup_liq=60000)
check(f"tiny pool: $5 trade impact > 2% rejected [{why[-80:]}]", r is None and "too costly" in why)
setup(1); r, why, _ = fb_buy(impact_bias=0.03)
check("Jupiter's own price impact > 2% rejected", r is None and ("too costly" in why or "price impact" in why))
setup(1); r, why, _ = fb_buy(jup_liq=10000)
check(f"Jupiter liquidity $10k < 0.5 x $30k rejected [{why[-70:]}]", r is None and "liquidity" in why)
setup(1); r, why, _ = fb_buy(chg_h1=120)
check("1h pump 120% (> 75%) rejected by the same filter", r is None and "pumped" in why)
setup(1); r, why, _ = fb_buy(holders=40)
check("40 holders < 100 rejected", r is None and "holders" in why)
setup(1); r, why, _ = fb_buy(price=1.5e-4)
check("Jupiter price 50% above scan price rejected (price-spike guard)", r is None and "differs" in why)
setup(1); r, why, _ = fb_buy(quote_ok=False)
check("quote unavailable -> no buy", r is None and "quote unavailable" in why)
RC["ok"] = False
setup(1); r, why, c = fb_buy()
skip = con.execute("SELECT message FROM decisions WHERE token=? AND kind='SKIP'", (c["token"],)).fetchone()
check("RugCheck unreachable -> fallback buy blocked", r is None and skip and "rug check" in skip["message"])
RC["ok"] = True
cfg["entry"]["prebuy_fallback"] = "off"
setup(1); r, why, _ = fb_buy()
check("fallback switched off -> old behaviour (no buy, 'could not re-read')", r is None and "re-read" in why)
cfg["entry"]["prebuy_fallback"] = "jupiter_quote"
DS_MODE["m"] = "notfound"
setup(1); r, why, _ = fb_buy()
check("DexScreener answered but pool not found -> no fallback, no buy", r is None and "re-read" in why)
pg.reject = orig_reject

# DexScreener pause after a 429: no DexScreener call at all while paused; callers see the pairs as 'failed' (-> fallback)
import common as C
orig_get = scanner.http_get
sent = []
def get429(kind, url, params=None, retries=3, headers=None, skip_spacing=False):
    sent.append(url); C.CALLS["ds_429"] = C.CALLS.get("ds_429", 0) + 1; return None
scanner.http_get = get429
C.DS_PAUSE.update(until=0, streak=0, base=180, max=900)
out = scanner.ds_pairs(["PAIRA"])
check("a DexScreener 429 starts a 3-min pause and marks the pair as failed", C.ds_paused() and "PAIRA" in scanner.DS_FAILED and len(sent) == 1)
out = scanner.ds_pairs(["PAIRB"]); p2, f2 = scanner.ds_tokens_ex(["M1", "M2"])
check("while paused: no DexScreener calls, pairs/mints reported as failed", len(sent) == 1 and "PAIRB" in scanner.DS_FAILED and f2 == ["M1", "M2"])
C.DS_PAUSE["until"] = 0; scanner.ds_pairs(["PAIRC"])
check("next 429 doubles the pause (6 min)", 350 < C.DS_PAUSE["until"] - time.time() <= 361 and C.DS_PAUSE["streak"] == 2)
scanner.http_get = lambda *a, **k: {"pairs": []}
C.DS_PAUSE["until"] = 0; scanner.ds_pairs(["PAIRD"])
check("a successful call resets the pause streak", C.DS_PAUSE["streak"] == 0 and not C.ds_paused())
scanner.http_get = orig_get

con.close(); shutil.rmtree(tmp)
bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed")
sys.exit(1 if bad else 0)
