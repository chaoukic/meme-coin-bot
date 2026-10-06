"""Pre-buy fallback (added Oct 2 2026, approved by Sammy): when DexScreener's fresh pool re-read right before a buy
fails (429 / error), check the coin with Jupiter instead. Read-only: quotes are never executed here.

Checks, all must pass:
 1. fresh Jupiter Tokens API v2 reading (price, 1h change, buys/sells, volume, market cap, age) through the SAME cheap
    filters as the scanner (scanner.cheap_filters), incl. the 1h pump limit. Liquidity: Jupiter's own figure must be
    >= fallback_min_jup_liq_factor x min_liquidity_usd (Jupiter reports about half of DexScreener's pool figure).
 2. holders (Jupiter holderCount, when known) >= min_holders.
 3. trade-size quote (SOL -> coin for [live] trade_usd): cost vs Jupiter's price AND Jupiter's priceImpactPct both
    <= fallback_max_trade_impact_pct.
 4. depth quote (fallback_depth_usd, sent at the same moment as the trade-size quote): the extra cost of the bigger
    trade over the small one is a fee-free price-impact reading. For a constant-product pool holding L dollars in total
    (both sides, DexScreener's convention), buying D dollars costs about D / (L/2 + D), so
        L_est = 2 * D * (1 - i) / i.
    L_est must be >= min_liquidity_usd (for D = $30 and $30k that means i <= 0.2%), and Jupiter's own priceImpactPct
    at that size must also be <= fallback_max_trade_impact_pct.
Returns a dict: ok, reason, price, liq (liquidity to size the paper fill with), details (stored on the position)."""
import threading, time
from common import http_get, fnum, log, LIMITS
import scanner

SOL = "So11111111111111111111111111111111111111112"
QUOTE = "https://api.jup.ag/swap/v1/quote"
DEFAULTS = {"prebuy_fallback": "jupiter_quote", "fallback_depth_usd": 30.0, "fallback_max_trade_impact_pct": 2.0,
            "fallback_min_jup_liq_factor": 0.5}

def cfg_entry(cfg):
    d = dict(DEFAULTS); d.update(cfg.get("entry", {})); return d

def enabled(cfg):
    return cfg_entry(cfg)["prebuy_fallback"] == "jupiter_quote"

def depth_liq(usd, impact):
    """Constant-product total liquidity implied by a fee-free impact fraction `impact` for a `usd` buy."""
    if impact <= 0:
        return float("inf")
    return 2 * usd * (1 - impact) / impact

def max_depth_impact(usd, min_liq):
    """Largest impact fraction at `usd` that is still consistent with liquidity >= min_liq."""
    return 2 * usd / (min_liq + 2 * usd)

def _key():
    import live
    return live.jup_key()

def _search(mint, key):
    j = http_get("jup", f"{scanner.JUPT}/search", {"query": mint}, retries=2, headers={"x-api-key": key})
    if isinstance(j, list):
        for x in j:
            if x.get("id") == mint:
                return x
    return None

def _quote_pair(mint, lamports_a, lamports_b, key):
    """Two SOL->coin quotes sent at the same moment (so a fast-moving price barely differs between them)."""
    LIMITS["jup"].wait()
    LIMITS["jup"].wait()  # reserve two slots in the scanning budget
    res = [None, None]
    def go(i, amt):
        res[i] = http_get("jup", QUOTE, {"inputMint": SOL, "outputMint": mint, "amount": int(amt), "slippageBps": 300},
                          retries=2, headers={"x-api-key": key}, skip_spacing=True)
    th = [threading.Thread(target=go, args=(0, lamports_a)), threading.Thread(target=go, args=(1, lamports_b))]
    for t in th: t.start()
    for t in th: t.join(30)
    return res

def quote_check(mint, j, price, trade_usd, depth_usd, min_liq, max_imp_pct, key, sol_price):
    """Trade-size + depth quote pair (shared by the new-coin fallback and established.py). Returns a dict:
    ok, reason, details, liq_est, i_depth, trade_imp, depth_usd, amm (pool the depth quote mostly routes through)."""
    out = {"ok": False, "reason": "", "details": {}, "liq_est": None, "i_depth": None, "trade_imp": None,
           "depth_usd": depth_usd, "amm": None}
    det = out["details"]
    qa, qb = _quote_pair(mint, trade_usd / sol_price * 1e9, depth_usd / sol_price * 1e9, key)
    if not qa or not qb or "outAmount" not in qa or "outAmount" not in qb:
        out["reason"] = "Jupiter quote unavailable (no route or API busy)"; return out
    dec = 10 ** int(j.get("decimals") or 0)
    def rate(q):  # coin units per SOL lamport, after AMM fees
        return int(q["outAmount"]) / dec / int(q["inAmount"])
    usd_in_a = int(qa["inAmount"]) / 1e9 * sol_price
    cost_a = 1 - (int(qa["outAmount"]) / dec * price) / usd_in_a
    pia, pib = max(fnum(qa.get("priceImpactPct"), 0), 0), max(fnum(qb.get("priceImpactPct"), 0), 0)
    trade_imp = max(cost_a, pia)
    marg = 1 - rate(qb) / rate(qa)                    # extra cost of the big trade over the small one (fees cancel)
    i_depth = max(marg * depth_usd / (depth_usd - trade_usd), 0.0)
    liq_est = depth_liq(depth_usd, i_depth)
    lim_imp = max_depth_impact(depth_usd, min_liq)
    steps = qb.get("routePlan") or []
    det.update({"trade_usd": trade_usd, "depth_usd": depth_usd, "sol_price": round(sol_price, 4),
                "trade_cost_pct": round(cost_a * 100, 3), "trade_impact_jup_pct": round(pia * 100, 3),
                "depth_impact_jup_pct": round(pib * 100, 3), "depth_impact_pct": round(i_depth * 100, 4),
                "depth_impact_limit_pct": round(lim_imp * 100, 4),
                "liq_est": None if liq_est == float("inf") else round(liq_est),
                "route": [((s.get("swapInfo") or {}).get("label")) for s in steps]})
    if steps:
        best = max(steps, key=lambda s: fnum(s.get("percent"), 0) or 0)
        out["amm"] = (best.get("swapInfo") or {}).get("ammKey")
    out.update(liq_est=liq_est, i_depth=i_depth, trade_imp=trade_imp)
    maxi = max_imp_pct / 100
    if trade_imp > maxi:
        out["reason"] = (f"${trade_usd:.0f} quote too costly: {trade_imp * 100:.2f}% vs Jupiter price "
                         f"(cost {cost_a * 100:.2f}%, Jupiter impact {pia * 100:.2f}%) > {maxi * 100:.1f}%"); return out
    if pib > maxi:
        out["reason"] = f"${depth_usd:.0f} quote: Jupiter price impact {pib * 100:.2f}% > {maxi * 100:.1f}%"; return out
    if liq_est < min_liq:
        out["reason"] = (f"pool too thin: ${depth_usd:.0f} vs ${trade_usd:.0f} quote impact {i_depth * 100:.3f}% "
                         f"(> {lim_imp * 100:.3f}%) = about ${liq_est:,.0f} liquidity < ${min_liq:,.0f}"); return out
    out["ok"] = True
    return out

def prebuy(cfg, c, sol_price=None):
    E = cfg_entry(cfg)
    F = cfg["filters"]
    G = cfg.get("guard", {})
    out = {"ok": False, "reason": "", "price": None, "liq": None, "details": {}}
    det = out["details"]
    key = _key()
    if not key:
        out["reason"] = "Jupiter key missing"; return out
    j = _search(c["token"], key)
    if not j or not fnum(j.get("usdPrice")):
        out["reason"] = "no fresh Jupiter reading"; return out
    tok = {"pool_address": c.get("pair"), "pool_created_ts": None, "dex": None}
    m = scanner.jup_metrics(j, tok)
    price = m["price"]
    det.update({k: (round(v, 8) if isinstance(v, float) else v) for k, v in m.items()
                if k in ("price", "liq", "vol_h1", "vol_h24", "mcap", "age_min", "buys_h1", "sells_h1", "chg_h1")})
    det["holders"] = j.get("holderCount")
    min_liq = float(F["min_liquidity_usd"])
    jl_min = E["fallback_min_jup_liq_factor"] * min_liq
    bad = scanner.cheap_filters(m, F, min_liq=jl_min)
    if bad:
        out["reason"] = f"Jupiter reading fails the {bad[1]} filter: " + "; ".join(bad[2]); return out
    if j.get("holderCount") is not None and int(j["holderCount"]) < int(F["min_holders"]):
        out["reason"] = f"{j['holderCount']} holders < {F['min_holders']}"; return out
    tol = float(G.get("entry_price_tolerance_pct", 20))
    if c.get("price") and abs(price / c["price"] - 1) * 100 > tol:
        out["reason"] = f"Jupiter price ${price:.4g} differs {abs(price / c['price'] - 1) * 100:.0f}% from scan price ${c['price']:.4g}"
        return out
    # ---- quotes
    if sol_price is None:
        import live
        sol_price = live.sol_price()
    trade_usd = float(cfg.get("live", {}).get("trade_usd", 5))
    qc = quote_check(c["token"], j, price, trade_usd, float(E["fallback_depth_usd"]), min_liq,
                     float(E["fallback_max_trade_impact_pct"]), key, sol_price)
    det.update(qc["details"])
    if not qc["ok"]:
        out["reason"] = qc["reason"]; return out
    liq_est, depth_usd, i_depth = qc["liq_est"], qc["depth_usd"], qc["i_depth"]
    trade_imp = qc["trade_imp"]
    liq_use = min(x for x in (c.get("liq"), liq_est) if x)
    out.update(ok=True, price=price, liq=liq_use,
               reason=(f"Jupiter check ok: ${trade_usd:.0f} cost {trade_imp * 100:.2f}%, ${depth_usd:.0f} impact {i_depth * 100:.3f}% "
                       f"(~${liq_est:,.0f} liquidity)" if liq_est != float("inf") else
                       f"Jupiter check ok: ${trade_usd:.0f} cost {trade_imp * 100:.2f}%, ${depth_usd:.0f} impact ~0% (deep)")
               + f", Jupiter liquidity ${m['liq']:,.0f}")
    return out
