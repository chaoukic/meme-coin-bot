"""Older-coin strategy: Jupiter pre-buy fallback when DexScreener is busy/paused (Oct 3). Mocked Jupiter / GeckoTerminal /
DexScreener; runs on a COPY of the DB; PAPER ONLY. Run: .venv/bin/python tests/test_estab_fallback.py"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_established.py")).read().split("# ---- 1. rules (pure)")[0])  # setup + rec()
import jupcheck
results.clear()
SOLPX = 150.0
live.sol_price = lambda: SOLPX
DS = {"st": "busy"}
es.ds_pool = lambda mint: (DS["st"], {"pair": "DSPAIR" + mint, "url": "u", "price": (LISTS.get(mint) or {}).get("usdPrice", 1.0), "liq": 900_000}
                           if DS["st"] == "ok" else None)
FRESH = {}
jupcheck._search = lambda mint, key: FRESH.get(mint)
QUOTES = {"ca": 0.004, "liq": None, "imp": 0.0, "fail": False}
QCALLS = []
def q(mint, usd, cost, dec=6):
    lam = int(usd / SOLPX * 1e9)
    price = FRESH[mint]["usdPrice"]
    return {"inAmount": str(lam), "outAmount": str(int(usd * (1 - cost) / price * 10 ** dec)), "priceImpactPct": str(QUOTES["imp"]),
            "routePlan": [{"percent": 100, "swapInfo": {"label": "Meteora DLMM", "ammKey": "ROUTEPOOL" + mint}}]}
def fake_pair(mint, la, lb, key):
    QCALLS.append(mint)
    if QUOTES["fail"]:
        return None, None
    T, D = la / 1e9 * SOLPX, lb / 1e9 * SOLPX
    ca = QUOTES["ca"]
    if QUOTES["liq"]:
        i = 2 * D / (QUOTES["liq"] + 2 * D)        # constant-product impact at D
        marg = i * (D - T) / D
        cb = 1 - (1 - ca) * (1 - marg)
    else:
        cb = ca
    return q(mint, T, ca), q(mint, D, cb)
jupcheck._quote_pair = fake_pair
GT_ = {"v": None}
es.gt_token = lambda mint: GT_["v"]
n = [0]
def attempt(fresh_kw=None, scan_kw=None, ds="busy", gt="agree", liq=None, ca=0.004, imp=0.0, qfail=False, fb="jupiter_quote"):
    n[0] += 1
    mint = f"FB{n[0]}"
    LISTS.clear(); LISTS[mint] = rec(mint, f"FBC{n[0]}", **(scan_kw or {}))
    FRESH.clear(); FRESH[mint] = rec(mint, f"FBC{n[0]}", **(fresh_kw or {})); FRESH[mint]["decimals"] = 6
    DS["st"] = ds
    p = FRESH[mint]["usdPrice"]
    GT_["v"] = {"price": p, "pool": "GTPOOL" + mint} if gt == "agree" else ({"price": p * 1.10, "pool": "X"} if gt == "disagree" else None)
    QUOTES.update(ca=ca, liq=liq, imp=imp, fail=qfail)
    cfg["established"]["prebuy_fallback"] = fb
    con.execute("UPDATE positions SET status='closed', closed_at=0 WHERE status='open' AND strategy='established'")
    set_state(con, "estab_day", None); con.commit()
    del QCALLS[:]
    es.run_round(con, cfg)
    r = con.execute("SELECT * FROM positions WHERE token=? AND strategy='established'", (mint,)).fetchone()
    g = con.execute("SELECT message FROM decisions WHERE token=? ORDER BY id DESC", (mint,)).fetchone()
    return r, (g["message"] if g else ""), mint

r, msg, mint = attempt(liq=2_000_000)
check("DexScreener busy + good Jupiter quotes + GeckoTerminal agrees -> paper buy", r is not None)
check("tagged entry_source='jup_fallback', strategy='established'", r and r["entry_source"] == "jup_fallback" and r["strategy"] == "established")
check("pool = GeckoTerminal top pool", r and r["pair"] == "GTPOOL" + mint)
check("Telegram alert says '(checked via Jupiter quote)' and 'PAPER (older coin)'",
      con.execute("SELECT 1 FROM outbox WHERE key=? AND text LIKE '%PAPER (older coin) BUY FBC% (checked via Jupiter quote)%'", (f"buy:{r['id']}",)).fetchone())
import json as _json
det = _json.loads(r["entry_check"])
check(f"quote details stored (depth ${det.get('depth_usd')}, implied liq ~${det.get('liq_est'):,})", det.get("depth_usd") == 150 and det.get("trade_usd") == 15 and det.get("liq_est"))
check("real wallet: no live trade for the fallback entry, live.on_buy refuses it",
      live.on_buy(con, cfg, r["id"], r["token"], r["symbol"]) != True and not LIVE_CALLS)

r, msg, mint = attempt(liq=60_000)
check(f"depth quote implies ~$60k < $100k -> no buy ({msg[:90]})", r is None and "too thin" in msg)
r, msg, mint = attempt(liq=130_000)
check("depth quote implies ~$130k -> buy", r is not None)
r, msg, mint = attempt(ca=0.02, liq=5_000_000)
check(f"$15 quote costs 2% > 1.5% -> no buy", r is None and "too costly" in msg)
r, msg, mint = attempt(imp=0.02)
check("Jupiter price impact 2% -> no buy", r is None and ("too costly" in msg or "impact" in msg))
r, msg, mint = attempt(gt="disagree")
check(f"GeckoTerminal 10% off -> no buy", r is None and "GeckoTerminal" in msg)
r, msg, mint = attempt(gt="none")
check("GeckoTerminal unavailable -> still buys (cross-check only when available), pool from Jupiter route",
      r is not None and r["pair"] == "ROUTEPOOL" + mint and "GeckoTerminal unavailable" in r["entry_check"])
r, msg, mint = attempt(fresh_kw={"price": 1.07})
check("fresh Jupiter price 7% above scan price -> no buy", r is None and "differs" in msg)
r, msg, mint = attempt(fresh_kw={"c1": 12})
check("fresh reading now pumping +12% 1h -> no buy (same rules)", r is None and "rules" in msg)
r, msg, mint = attempt(qfail=True)
check("Jupiter quote unavailable -> no buy", r is None and "quote unavailable" in msg)
r, msg, mint = attempt(ds="none")
check("DexScreener answered 'not listed' -> no fallback, no buy", r is None and not QCALLS)
r, msg, mint = attempt(fb="off")
check("prebuy_fallback='off' -> old behaviour (skip while busy)", r is None and not QCALLS and "busy" in msg)
r, msg, mint = attempt(ds="ok")
check("DexScreener ok -> normal check, entry_source='jupiter', no quotes", r is not None and r["entry_source"] == "jupiter" and not QCALLS)
saved = es.rug_ok
es.rug_ok = lambda mint, cfg_, pair: (False, "RugCheck danger: test")
r, msg, mint = attempt(liq=2_000_000)
check("RugCheck still runs first: rug fail -> no quotes, no buy", r is None and not QCALLS and "rug check failed" in msg)
es.rug_ok = saved
check("no real-money activity at any point", not LIVE_CALLS)

fails = [n_ for n_, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
