"""Staged filter pipeline: cheap checks first, expensive safety checks last.
Data: GeckoTerminal + DexScreener (public, keyless) + Jupiter Tokens API v2 (Sammy's key, read-only)
+ Solana public RPC (read-only)."""
import json, math, time
from common import (http_get, rpc, fnum, now_ts, log, CALLS, ds_paused, ds_note)

GT = "https://api.geckoterminal.com/api/v2"
JUPT = "https://api.jup.ag/tokens/v2"
DS = "https://api.dexscreener.com"
SKIP_BASE = {"So11111111111111111111111111111111111111112",
             "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
             "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
# Token-account owners that belong to AMMs / launchpads (pool reserves, not holders)
KNOWN_POOL_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",   # Raydium LaunchLab authority
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2 pool authority
    "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",  # Meteora DBC pool authority
}
BURN_OWNERS = {"1nc1nerator11111111111111111111111111111111"}
STAGES = ["data", "age", "liquidity", "volume", "market_cap", "txns",
          "contract", "holders", "holder_trend", "score"]

# ------------------------------------------------------------------ discovery
def _parse_pools(j, source):
    out = []
    for p in (j or {}).get("data", []) or []:
        a = p.get("attributes", {})
        rel = p.get("relationships", {})
        base = (rel.get("base_token", {}).get("data") or {}).get("id", "")
        base = base.split("_", 1)[1] if "_" in base else base
        if not base or base in SKIP_BASE:
            continue
        created = a.get("pool_created_at")
        ts = None
        if created:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
        out.append({"token": base, "pool": a.get("address"),
                    "symbol": (a.get("name") or "?").split(" / ")[0][:24],
                    "dex": (rel.get("dex", {}).get("data") or {}).get("id"),
                    "created": ts, "source": source})
    return out

def discover(cfg):
    sc = cfg["scanner"]
    found = []
    for page in range(1, int(sc.get("new_pool_pages", 3)) + 1):
        found += _parse_pools(http_get("gt", f"{GT}/networks/solana/new_pools", {"page": page}), "gt_new")
    if sc.get("include_trending", True):
        found += _parse_pools(http_get("gt", f"{GT}/networks/solana/trending_pools", {"page": 1, "duration": "6h"}), "gt_trending")
    if sc.get("include_top_volume", True):
        found += _parse_pools(http_get("gt", f"{GT}/networks/solana/pools", {"page": 1, "sort": "h24_volume_usd_desc"}), "gt_topvol")
    if sc.get("include_jupiter_trending", False):
        found += jup_discover()
    return found

def register(con, found):
    new = 0
    t = now_ts()
    for f in found:
        cur = con.execute("INSERT OR IGNORE INTO tokens(address,pool_address,symbol,dex,pool_created_ts,first_seen,source,status) "
                          "VALUES(?,?,?,?,?,?,?, 'watch')",
                          (f["token"], f["pool"], f["symbol"], f["dex"], f["created"], t, f["source"]))
        new += cur.rowcount
    return new

# ------------------------------------------------------------------ market data
def _ds_get(url, retries):
    """One DexScreener GET that feeds the 429 pause (common.DS_PAUSE). Returns (json_or_None, got_429)."""
    n0 = CALLS.get("ds_429", 0)
    j = http_get("ds", url, retries=retries)
    hit = CALLS.get("ds_429", 0) > n0
    ds_note(hit and j is None)
    return j, hit and j is None

def ds_tokens_ex(addrs, retries=3, honour_pause=True):
    """DexScreener pairs for up to 30 token addresses per call. Returns (pairs, failed_addrs): failed_addrs are the
    mints of calls that failed (429 / error / DexScreener paused after a 429), so they are not mistaken for
    'not listed'. After a 429 the remaining chunks of this call are not sent."""
    pairs, failed = {}, []
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        if honour_pause and ds_paused():
            failed += chunk
            continue
        j, _ = _ds_get(f"{DS}/tokens/v1/solana/{','.join(chunk)}", retries)
        if isinstance(j, list):
            for p in j:
                b = (p.get("baseToken") or {}).get("address")
                if b:
                    pairs.setdefault(b, []).append(p)
        else:
            failed += chunk
    return pairs, failed

def ds_tokens(addrs):
    return ds_tokens_ex(addrs)[0]

DS_FAILED = set()   # pair addresses whose last ds_pairs() call failed (429 / error), as opposed to "not found"

def ds_pairs(pair_addrs):
    out = {}
    DS_FAILED.clear()
    for i in range(0, len(pair_addrs), 30):
        chunk = pair_addrs[i:i + 30]
        if ds_paused():   # paused after a 429: don't ask; callers fall back to Jupiter
            DS_FAILED.update(chunk)
            continue
        j, _ = _ds_get(f"{DS}/latest/dex/pairs/solana/{','.join(chunk)}", 1)
        if j is None:
            DS_FAILED.update(chunk)
        for p in (j or {}).get("pairs") or []:
            out[p.get("pairAddress")] = p
    return out

def pair_metrics(p, all_pairs):
    liq = fnum((p.get("liquidity") or {}).get("usd"))
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}
    created = p.get("pairCreatedAt")
    return {
        "pair": p.get("pairAddress"), "url": p.get("url"), "dex": p.get("dexId"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "name": (p.get("baseToken") or {}).get("name"),
        "price": fnum(p.get("priceUsd")), "liq": liq,
        "vol_h1": fnum(vol.get("h1"), 0), "vol_h6": fnum(vol.get("h6"), 0), "vol_h24": fnum(vol.get("h24"), 0),
        "buys_h1": (tx.get("h1") or {}).get("buys", 0), "sells_h1": (tx.get("h1") or {}).get("sells", 0),
        "buys_h24": (tx.get("h24") or {}).get("buys", 0), "sells_h24": (tx.get("h24") or {}).get("sells", 0),
        "chg_h1": fnum((p.get("priceChange") or {}).get("h1"), 0),
        "chg_h24": fnum((p.get("priceChange") or {}).get("h24"), 0),
        "mcap": fnum(p.get("marketCap")) or fnum(p.get("fdv")),
        "age_min": (time.time() - created / 1000) / 60 if created else None,
        "pool_set": [x.get("pairAddress") for x in all_pairs if x.get("pairAddress")],
    }

def best_pair(pairs, known_pool):
    with_liq = [p for p in pairs if fnum((p.get("liquidity") or {}).get("usd"))]
    if with_liq:
        return max(with_liq, key=lambda p: fnum(p["liquidity"]["usd"]))
    for p in pairs:
        if p.get("pairAddress") == known_pool:
            return p
    return pairs[0] if pairs else None

# ---- Jupiter Tokens API v2 (watchlist re-checks, added Oct 2 2026)
def jup_tokens(addrs):
    """Jupiter token records for up to 100 mints per call. Returns (found, failed_addrs): mints in a batch whose
    call failed (error / 429 after retries) are listed in failed_addrs so the caller can fall back for that batch."""
    import live
    key = live.jup_key()
    if not key:
        return {}, list(addrs)
    found, failed = {}, []
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        j = http_get("jup", f"{JUPT}/search", {"query": ",".join(chunk)}, retries=2, headers={"x-api-key": key})
        if not isinstance(j, list):
            failed += chunk
            continue
        for x in j:
            if x.get("id"):
                found[x["id"]] = x
    return found, failed

def _iso_ts(v):
    if not v:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def jup_metrics(j, tok, age_mode="firstPool"):
    """Jupiter token record -> the same metrics dict pair_metrics() builds from a DexScreener pair.
    Jupiter is per COIN (all pools combined), not per pool, so there is no 'pair': the pool found at discovery
    (GeckoTerminal) is kept as the pair / DexScreener link. Gaps vs DexScreener: liquidity and volume are
    coin-wide (all pools); pool age comes from Jupiter's firstPool (or the discovery pool's creation time)."""
    s1, s6, s24 = j.get("stats1h") or {}, j.get("stats6h") or {}, j.get("stats24h") or {}
    vol = lambda s: (fnum(s.get("buyVolume"), 0) or 0) + (fnum(s.get("sellVolume"), 0) or 0)
    fp = j.get("firstPool") or {}
    created = None
    if age_mode == "discovery_pool":
        created = tok["pool_created_ts"]
    if not created:
        created = _iso_ts(fp.get("createdAt")) or _iso_ts(j.get("createdAt"))
    pool = tok["pool_address"] or fp.get("id")
    return {
        "pair": pool, "url": f"https://dexscreener.com/solana/{pool.lower()}" if pool else None, "dex": tok["dex"],
        "symbol": j.get("symbol"), "name": j.get("name"),
        "price": fnum(j.get("usdPrice")), "liq": fnum(j.get("liquidity")),
        "vol_h1": vol(s1), "vol_h6": vol(s6), "vol_h24": vol(s24),
        "buys_h1": int(fnum(s1.get("numBuys"), 0)), "sells_h1": int(fnum(s1.get("numSells"), 0)),
        "buys_h24": int(fnum(s24.get("numBuys"), 0)), "sells_h24": int(fnum(s24.get("numSells"), 0)),
        "chg_h1": fnum(s1.get("priceChange"), 0), "chg_h24": fnum(s24.get("priceChange"), 0),
        "mcap": fnum(j.get("mcap")) or fnum(j.get("fdv")),
        "age_min": (time.time() - created) / 60 if created else None,
        "pool_set": [x for x in {tok["pool_address"], fp.get("id")} if x],
        "src": "jupiter",
    }

# Jupiter's liquidity figure is about HALF of DexScreener's pool liquidity for the same coin (measured Oct 2 on
# 23 coins: pumpswap/raydium ratio 0.46-0.53, small pools down to 0.27). The filters' dollar thresholds were set on
# DexScreener numbers, so Jupiter data only settles a coin when the answer cannot depend on that difference
# (too young, too old, or liquidity far below the minimum even after multiplying by a safety margin:
# x3 for "below the minimum", x4 for "dead" = permanently off the watchlist).
# Everything else is re-checked on DexScreener's pool data before the normal filters/score run.
JUP_LIQ_MARGIN = 3
JUP_DEAD_MARGIN = 4

def jup_prescreen(m, F):
    """Cheap decision from Jupiter metrics, or None = needs the DexScreener pool check.
    Returns (result, stage, reasons, final)."""
    age, liq = m["age_min"], m["liq"]
    if age is None or not liq or not m["price"]:
        return None
    if age < F["min_pool_age_min"]:
        return "pending", "age", [f"pool age {age:.0f}m < {F['min_pool_age_min']}m (waiting)"], False
    if age > F["max_pool_age_hours"] * 60 + 15:
        return "reject", "age", [f"pool age {age/60:.1f}h > {F['max_pool_age_hours']}h"], True
    if liq * JUP_LIQ_MARGIN < F["min_liquidity_usd"]:
        final = age > 60 and liq * JUP_DEAD_MARGIN < F["min_liquidity_usd"] * 0.25
        return "reject", "liquidity", [f"liquidity ${liq:,.0f} on Jupiter (all pools; about half of DexScreener's pool figure) "
                                       f"- far below ${F['min_liquidity_usd']:,.0f}" + (" (dead)" if final else "")], final
    return None

def jup_discover(limit=100):
    """Optional extra discovery: Jupiter top-trending (1h) coins. One Jupiter call."""
    import live
    key = live.jup_key()
    if not key:
        return []
    j = http_get("jup", f"{JUPT}/toptrending/1h", {"limit": limit}, retries=1, headers={"x-api-key": key})
    out = []
    for x in j if isinstance(j, list) else []:
        if not x.get("id") or x["id"] in SKIP_BASE:
            continue
        fp = x.get("firstPool") or {}
        out.append({"token": x["id"], "pool": None, "symbol": (x.get("symbol") or "?")[:24], "dex": x.get("launchpad"),
                    "created": _iso_ts(fp.get("createdAt")) or _iso_ts(x.get("createdAt")), "source": "jup_trending"})
    return out

# ------------------------------------------------------------------ dossier (expensive)
def fetch_dossier(cfg, token, pool_set):
    d = {"ts": now_ts(), "src": []}
    j = http_get("gt", f"{GT}/networks/solana/tokens/{token}/info")
    a = ((j or {}).get("data") or {}).get("attributes") or {}
    if a:
        d["src"].append("geckoterminal")
        d["gt_mint"] = a.get("mint_authority")
        d["gt_freeze"] = a.get("freeze_authority")
        d["honeypot"] = a.get("is_honeypot")
        h = a.get("holders") or {}
        d["holders"] = h.get("count")
        d["holders_updated"] = h.get("last_updated")
        dist = h.get("distribution_percentage") or {}
        d["gt_top10"] = fnum(dist.get("top_10"))
        d["gt_score"] = fnum(a.get("gt_score"))
        d["dev_pct"] = fnum(a.get("developer_holding_percentage"))
    # Authoritative mint / freeze authority + supply from chain
    m = rpc(cfg, "getAccountInfo", [token, {"encoding": "jsonParsed"}])
    info = (((m or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info") if m else None
    if info:
        d["src"].append("rpc_mint")
        d["mint_authority"] = info.get("mintAuthority")
        d["freeze_authority"] = info.get("freezeAuthority")
        d["supply_raw"] = int(info.get("supply", 0))
        d["mint_known"] = True
    elif a:
        d["mint_authority"] = None if a.get("mint_authority") == "no" else ("unknown" if a.get("mint_authority") is None else "set")
        d["freeze_authority"] = None if a.get("freeze_authority") == "no" else ("unknown" if a.get("freeze_authority") is None else "set")
        d["mint_known"] = a.get("mint_authority") is not None
    if d.get("supply_raw"):
        d.update(fetch_holders(cfg, token, pool_set, d["supply_raw"]))
    return d


def fetch_holders(cfg, token, pool_set, supply):
    """Top wallets from the chain, excluding pool/LP/burn accounts. {} if the RPC refused."""
    la = rpc(cfg, "getTokenLargestAccounts", [token])
    if la is None:
        time.sleep(3)
        la = rpc(cfg, "getTokenLargestAccounts", [token])
    accts = (la or {}).get("value") or []
    if not accts:
        return {}
    own = rpc(cfg, "getMultipleAccounts", [[x["address"] for x in accts], {"encoding": "jsonParsed"}])
    owners = []
    for v in ((own or {}).get("value") or []):
        try:
            owners.append(v["data"]["parsed"]["info"]["owner"])
        except Exception:
            owners.append(None)
    if len(owners) != len(accts):
        return {}
    pools = set(pool_set) | KNOWN_POOL_OWNERS
    holders, excluded = [], 0
    for acc, ow in zip(accts, owners):
        amt = int(acc.get("amount", 0))
        if acc["address"] in pools or ow in pools or ow in BURN_OWNERS:
            excluded += amt
            continue
        holders.append((amt, ow))
    holders.sort(reverse=True)
    return {"top1_pct": holders[0][0] / supply * 100 if holders else 0.0,
            "top10_pct": sum(h[0] for h in holders[:10]) / supply * 100,
            "pool_pct": excluded / supply * 100,
            "top_wallet": holders[0][1] if holders else None,
            "holders_rpc_ts": now_ts()}

# ------------------------------------------------------------------ scoring
def score(m, d, notes):
    """Transparent rule-based score, 0-100. Each part is listed in the reasons."""
    f = {}
    minliq = max(m["_minliq"], 1)
    f["liquidity"] = min(20, 20 * math.log10(max(m["liq"] / minliq, 1)) / math.log10(8))
    turnover = m["vol_h1"] / m["liq"] if m["liq"] else 0
    f["volume/liquidity"] = min(15, turnover * 15)
    bs = m["buys_h1"] / max(m["sells_h1"], 1)
    f["buy/sell balance"] = 15 if 1.1 <= bs <= 2.5 else (10 if 0.9 <= bs < 1.1 or 2.5 < bs <= 4 else 3)
    tg = m.get("_trend")
    if tg is not None:
        hg, pc = tg
        f["holder growth vs price"] = max(0, min(20, 10 + (hg - max(pc, 0) / 4) * 2))
    else:
        f["holder growth vs price"] = 5
    t1 = d.get("top1_pct")
    f["concentration"] = (15 * max(0, 1 - t1 / max(m["_maxtop1"], 0.1))) if t1 is not None else 4
    age_h = (m["age_min"] or 0) / 60
    f["age sweet spot"] = 10 if 0.5 <= age_h <= 6 else (6 if age_h <= 12 else 3)
    ch = m["chg_h1"]
    f["momentum"] = 5 if 0 < ch <= 60 else (2 if 60 < ch <= 150 else 0)
    pen = 0
    if d.get("honeypot") not in ("no",):
        pen += 3; notes.append("honeypot flag unknown (-3)")
    if ch > 150:
        pen += 10; notes.append(f"already pumped {ch:.0f}% in 1h (-10)")
    total = max(0, min(100, sum(f.values()) - pen))
    return round(total, 1), {k: round(v, 1) for k, v in f.items()}

# ------------------------------------------------------------------ evaluation
def cheap_filters(m, F, min_liq=None):
    """The cheap filter stages (age, liquidity, volume, market cap, 1h pump, txns). Returns None if all pass,
    else (result, stage, reasons, final). min_liq overrides F['min_liquidity_usd'] (used by the Jupiter pre-buy
    fallback, whose liquidity figure is on a different scale)."""
    R = []
    min_liq = F["min_liquidity_usd"] if min_liq is None else min_liq
    age = m["age_min"]
    if age is None:
        return "pending", "age", ["no pool creation time yet"], False
    if age < F["min_pool_age_min"]:
        return "pending", "age", [f"pool age {age:.0f}m < {F['min_pool_age_min']}m (waiting)"], False
    if age > F["max_pool_age_hours"] * 60:
        return "reject", "age", [f"pool age {age/60:.1f}h > {F['max_pool_age_hours']}h"], True
    liq = m["liq"]
    if not liq:
        final = age > 120
        return "reject", "liquidity", ["no liquidity reported (bonding curve / unlisted)"], final
    if liq < min_liq:
        final = age > 60 and liq < min_liq * 0.25
        return "reject", "liquidity", [f"liquidity ${liq:,.0f} < ${min_liq:,.0f}" + (" (dead)" if final else "")], final
    if m["vol_h1"] < F["min_volume_h1_usd"]:
        R.append(f"1h volume ${m['vol_h1']:,.0f} < ${F['min_volume_h1_usd']:,.0f}")
    if m["vol_h24"] < F["min_volume_h24_usd"]:
        R.append(f"24h volume ${m['vol_h24']:,.0f} < ${F['min_volume_h24_usd']:,.0f}")
    if R:
        return "reject", "volume", R, False
    mc = m["mcap"]
    if mc is None:
        return "reject", "market_cap", ["market cap unknown"], False
    if mc < F["min_market_cap_usd"] or mc > F["max_market_cap_usd"]:
        return "reject", "market_cap", [f"market cap ${mc:,.0f} outside ${F['min_market_cap_usd']:,.0f}-${F['max_market_cap_usd']:,.0f}"], False
    if m["chg_h1"] > F.get("max_price_change_h1_pct", 1e9):
        return "reject", "market_cap", [f"already pumped {m['chg_h1']:.0f}% in 1h (> {F['max_price_change_h1_pct']}%) - late entry"], False
    b, s = m["buys_h1"], m["sells_h1"]
    if b < F["min_buys_h1"]:
        R.append(f"only {b} buys in 1h (< {F['min_buys_h1']})")
    if s < F["min_sells_h1"]:
        R.append(f"only {s} sells in 1h (< {F['min_sells_h1']}) - possible honeypot")
    elif b and s / b < F["min_sell_buy_ratio"]:
        R.append(f"sell/buy ratio {s/b:.2f} < {F['min_sell_buy_ratio']} - possible honeypot")
    if m["buys_h24"] + m["sells_h24"] < F["min_txns_h24"]:
        R.append(f"{m['buys_h24'] + m['sells_h24']} txns/24h < {F['min_txns_h24']}")
    if R:
        return "reject", "txns", R, False
    return None

def evaluate(con, cfg, tok, m, budget):
    """Returns (result, stage, reasons, score, final, dossier_used).
    result: pass / reject / pending."""
    F = cfg["filters"]
    R = []
    cheap = cheap_filters(m, F)
    if cheap:
        res, stage, reasons, final = cheap
        return res, stage, reasons, None, final, False

    # ---- expensive stage: dossier (cached 5 min, limited per cycle)
    d = json.loads(tok["contract_json"]) if tok["contract_json"] else None
    used = False
    if not d or now_ts() - d.get("ts", 0) > 300:
        if budget["left"] <= 0:
            return "pending", "contract", ["safety lookup deferred (API budget for this cycle used)"], None, False, False
        budget["left"] -= 1
        used = True
        d = fetch_dossier(cfg, tok["address"], m["pool_set"])
        con.execute("UPDATE tokens SET contract_json=?, contract_ts=? WHERE address=?",
                    (json.dumps(d), d["ts"], tok["address"]))
        if d.get("holders"):
            # only record a snapshot when GeckoTerminal actually refreshed its holder count
            last = con.execute("SELECT marker FROM holder_snaps WHERE token=? ORDER BY ts DESC LIMIT 1",
                               (tok["address"],)).fetchone()
            mk = d.get("holders_updated") or str(d["holders"])
            if not last or last["marker"] != mk:
                con.execute("INSERT INTO holder_snaps(token,ts,holders,price,marker) VALUES(?,?,?,?,?)",
                            (tok["address"], d["ts"], int(d["holders"]), m["price"], mk))
    if not d.get("mint_known") and not d.get("src"):
        return "pending", "contract", ["contract data unavailable (API error), retry next cycle"], None, False, used
    if F.get("require_mint_revoked", True) and d.get("mint_authority"):
        R.append("mint authority NOT revoked" + (" (unknown)" if d.get("mint_authority") == "unknown" else ""))
    if F.get("require_freeze_revoked", True) and d.get("freeze_authority"):
        R.append("freeze authority NOT revoked" + (" (unknown)" if d.get("freeze_authority") == "unknown" else ""))
    if F.get("reject_honeypot", True) and d.get("honeypot") == "yes":
        return "reject", "contract", ["GeckoTerminal honeypot flag = yes"], None, True, used
    if R:
        return "reject", "contract", R, None, False, used

    notes = []
    if d.get("top1_pct") is None and d.get("supply_raw") and budget.get("rpc_left", 0) > 0:
        # chain top-holder lookup failed earlier: retry just that part (no GeckoTerminal call)
        budget["rpc_left"] -= 1
        h = fetch_holders(cfg, tok["address"], m["pool_set"], d["supply_raw"])
        if h:
            d.update(h)
            con.execute("UPDATE tokens SET contract_json=? WHERE address=?", (json.dumps(d), tok["address"]))
    t1, t10 = d.get("top1_pct"), d.get("top10_pct")
    if t1 is None:
        # GeckoTerminal's top-10 figure includes pool/bonding-curve accounts, so it is not used to decide
        return ("pending", "holders", ["top-holder check waiting: Solana public RPC refused the lookup, retrying next cycle"
                                      + (f" (GeckoTerminal top10 incl. pool: {d['gt_top10']:.0f}%)" if d.get("gt_top10") is not None else "")],
                None, False, used)
    if t1 > F["max_top_holder_pct"]:
        R.append(f"top wallet holds {t1:.1f}% > {F['max_top_holder_pct']}% (pool excluded)")
    if t10 is not None and t10 > F["max_top10_pct"]:
        R.append(f"top 10 wallets hold {t10:.1f}% > {F['max_top10_pct']}% (pool excluded)")
    if d.get("holders") and d["holders"] < F["min_holders"]:
        R.append(f"{d['holders']} holders < {F['min_holders']}")
    if R:
        return "reject", "holders", R, None, False, used

    # ---- holder trend vs price
    m["_trend"] = None
    if d.get("holders"):
        snaps = con.execute("SELECT ts,holders,price FROM holder_snaps WHERE token=? AND ts>? ORDER BY ts",
                            (tok["address"], now_ts() - 3600)).fetchall()
        if len(snaps) >= 2 and snaps[-1]["ts"] - snaps[0]["ts"] >= F["trend_min_interval_min"] * 60:
            o, n = snaps[0], snaps[-1]
            hg = (n["holders"] - o["holders"]) / max(o["holders"], 1) * 100
            pc = ((n["price"] - o["price"]) / o["price"] * 100) if o["price"] else 0
            m["_trend"] = (hg, pc)
            mins = (n["ts"] - o["ts"]) / 60
            if hg < -F["trend_max_holder_drop_pct"]:
                return "reject", "holder_trend", [f"holders fell {hg:.1f}% in {mins:.0f}m"], None, False, used
            if pc > F["trend_price_up_pct"] and hg < F["trend_min_holder_growth_pct"]:
                return "reject", "holder_trend", [f"price +{pc:.0f}% but holders only {hg:+.1f}% in {mins:.0f}m (one-buyer shape)"], None, False, used
            notes.append(f"holders {hg:+.1f}% vs price {pc:+.1f}% over {mins:.0f}m")
        else:
            return "pending", "holder_trend", [f"waiting for GeckoTerminal to refresh holder count ({d['holders']} holders now) to compare with price"], None, False, used
    else:
        notes.append("holder count not indexed yet; trend check skipped")

    m["_minliq"] = F["min_liquidity_usd"]
    m["_maxtop1"] = F["max_top_holder_pct"]
    sc, parts = score(m, d, notes)
    notes.append("score parts: " + ", ".join(f"{k} {v}" for k, v in parts.items()))
    # Side-by-side scoring test (SIDE_BY_SIDE_SPEC.md): BOTH scores are computed here, on the same metrics. The result
    # returned is still the CURRENT account's verdict (unchanged); the new account's verdict travels in m["_gate"] so a
    # coin rejected by one score still reaches the other account (bot.run_cycle builds candidates from both gates).
    import newscore
    g_cur = "pass" if sc >= F["min_score_to_buy"] else "reject"
    g_new = "n.a."
    if newscore.enabled(cfg):
        if "_sn" not in m:
            m["_sn"] = newscore.score_metrics(m)
        sn = m["_sn"][0]
        g_new = "pass" if (sn is not None and sn >= newscore.thresholds()[0]) else "reject"
        notes.append(f"new score {sn} ({'passes' if g_new == 'pass' else 'below'} {newscore.thresholds()[0]:g}, new-scoring paper account)")
    m["_gate"] = {"current": g_cur, "new": g_new}
    if g_cur == "reject":
        return "reject", "score", [f"score {sc} < {F['min_score_to_buy']}"] + notes, sc, False, used
    return "pass", "score", [f"PASSED all filters, score {sc}"] + notes, sc, False, used
