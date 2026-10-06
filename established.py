"""Established-coin strategy - PAPER ONLY (added Oct 2 2026, approved by Sammy).

A second, separate fake-money strategy for Solana coins whose first pool is older than 24h (days to months old),
next to the main new-coin strategy (pools 10 min - 24 h old). It has its own fake allocation, its own daily stop,
its own entry and exit rules ([established] in config.toml) and its rows are tagged strategy='established'.

REAL MONEY: never. This module never calls live.py, and live.py itself refuses any position whose strategy is not
'new' (live.paper_only_strategy) - so even a config mistake cannot make the real wallet copy these trades.

Data: Jupiter Tokens API v2 lists (toporganicscore / toptrending, 1h/6h/24h) + one batch lookup (100 coins per call),
all through the shared 'jup' rate limiter; one DexScreener call per entry (best pool + a second price), honouring the
DexScreener 429 pause; RugCheck before every buy; the price guard (priceguard.py) on every reading of an open position.
"""
import json, re, threading, time
from common import (get_state, set_state, decision, now_ts, toronto_date, fnum, log, http_get, ds_paused, CALLS)
import scanner
import notify
import priceguard as pg
import rugcheck

STRATEGY = "established"
TAG = "PAPER (older coin)"
LOCK = threading.Lock()
EST = "strategy='established'"

DEFAULTS = {
    "enabled": False, "paper_only": True,
    "starting_balance_usd": 300, "position_usd": 15, "max_open": 3, "max_entries_per_round": 1,
    "max_pct_of_pool_liquidity": 1, "slippage_pct": 1.0, "fee_pct": 0.5,
    "discovery_interval_min": 10, "update_interval_sec": 60, "watch_hours": 24, "list_limit": 100,
    "lists": ["toporganicscore/1h", "toporganicscore/6h", "toporganicscore/24h",
              "toptrending/1h", "toptrending/6h", "toptrending/24h"],
    "min_pool_age_hours": 24, "max_pool_age_days": 1095,
    "min_liquidity_usd": 100000, "min_volume_h24_usd": 250000, "min_volume_h1_usd": 5000,
    "min_market_cap_usd": 1000000, "max_market_cap_usd": 500000000,
    "min_holders": 1000, "min_organic_score": 50, "max_top_holders_pct": 50,
    "min_buys_h1": 20, "min_sells_h1": 10,
    "require_mint_revoked": True, "require_freeze_revoked": True,
    "exclude_tags": ["major", "stable", "lst", "original-lst", "yield", "yb", "jup-lend-earn",
                     "rwa", "stocks", "xstocks", "prestocks", "equities"],
    "pullback_min_pct": 3, "pullback_max_pct": 25, "bounce_min_h1_pct": 0.5, "max_h1_pump_pct": 8,
    "min_m5_pct": -1.0, "min_h24_change_pct": -35, "max_h24_change_pct": 100, "min_net_buyers_h1": 1,
    "rugcheck_lp_rule": "any", "entry_price_agree_pct": 5,
    "prebuy_fallback": "jupiter_quote", "fallback_depth_usd": 150, "fallback_max_trade_impact_pct": 1.5,
    "fallback_min_liq_usd": 100000, "fallback_price_tolerance_pct": 5, "fallback_gt_check": True,
    "take_profit_pct": 12, "take_profit_sell_fraction": 0.5, "take_profit2_pct": 25, "stop_loss_pct": 8,
    "trailing_activate_pct": 8, "trailing_stop_pct": 5, "max_hold_hours": 168, "volume_decay_ratio": 0,
    "daily_stop_pct": 5, "reentry_cooldown_hours": 24,
}

# never traded by this strategy: SOL / wrapped majors / stablecoins / liquid-staking SOL
EXCLUDE_MINTS = set(scanner.SKIP_BASE) | {
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So", "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1", "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm", "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA", "DEkqHyPN7GMRJ5cArtQFAWefqbZb33Hyf6s5iCwjEonT",
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",
}   # plus Jupiter tags (major/stable/lst/...), the symbol pattern and the $1-peg check below
_SYM_RE = re.compile(r"^(W|CB|Z|X|T|S|J|M|B|H|INF)?(BTC|ETH|SOL|XRP|SUI)$|USD|^INF$|SOL$", re.I)

def E(cfg):
    d = dict(DEFAULTS)
    d.update(cfg.get("established", {}))
    return d

def enabled(cfg):
    return bool(E(cfg)["enabled"])

# ------------------------------------------------------------------ money (separate fake allocation)
def cash(con, cfg):
    c = get_state(con, "estab_cash")
    if c is None:
        c = float(E(cfg)["starting_balance_usd"])
        set_state(con, "estab_cash", c)
        set_state(con, "estab_starting_balance", c)
    return c

def value(qty, price, e, liq, cfg):
    return pg.sell_value(qty, price, e, pg.cfg_guard(cfg), liq)

def equity(con, cfg):
    e = E(cfg)
    eq = cash(con, cfg)
    for p in con.execute(f"SELECT remaining_qty,last_price,last_liq FROM positions WHERE status='open' AND {EST}"):
        eq += value(p["remaining_qty"], p["last_price"] or 0, e, p["last_liq"], cfg)
    return eq

def day_check(con, cfg, eq=None):
    """Own daily stop (separate from the new-coin strategy's caps): (ok, day_pnl, start)."""
    e = E(cfg)
    eq = equity(con, cfg) if eq is None else eq
    d = get_state(con, "estab_day") or {}
    today = toronto_date()
    if d.get("date") != today:
        d = {"date": today, "start": eq, "stopped": False}
        set_state(con, "estab_day", d)
    pnl = eq - d["start"]
    if not d["stopped"] and d["start"] and pnl <= -d["start"] * float(e["daily_stop_pct"]) / 100:
        d["stopped"] = True
        set_state(con, "estab_day", d)
        decision(con, "PAUSE", None, None, f"[older coins] daily stop hit: day P&L ${pnl:,.2f} "
                 f"(-{e['daily_stop_pct']}%). No new older-coin entries until midnight Toronto.")
        notify.enqueue(con, f"estab-daystop:{today}", f"⛔ {TAG}: daily stop hit (today {notify.fmt_usd(pnl)}, "
                       f"-{e['daily_stop_pct']}%). No new older-coin paper entries until midnight Toronto. "
                       "Open older-coin positions are still managed. The new-coin strategy is not affected.")
    return (not d["stopped"]), pnl, d["start"]

# ------------------------------------------------------------------ data
def jkey():
    import live
    return live.jup_key()

def fetch_lists(cfg):
    """Union of the configured Jupiter lists (one call each, shared 'jup' limiter). Returns ({mint: record}, calls, failed)."""
    e = E(cfg)
    key = jkey()
    if not key:
        return {}, 0, len(e["lists"])
    out, n, failed = {}, 0, 0
    for name in e["lists"]:
        n += 1
        j = http_get("jup", f"{scanner.JUPT}/{name}", {"limit": int(e["list_limit"])}, retries=1, headers={"x-api-key": key})
        if not isinstance(j, list):
            failed += 1
            continue
        for x in j:
            if x.get("id"):
                out.setdefault(x["id"], x)
    return out, n, failed

def batch(mints):
    """Jupiter records for up to 100 mints per call (same batch lookup as the main scanner)."""
    return scanner.jup_tokens(list(mints))[0] if mints else {}

def metrics(x):
    s5, s1, s6, s24 = (x.get(k) or {} for k in ("stats5m", "stats1h", "stats6h", "stats24h"))
    vol = lambda s: (fnum(s.get("buyVolume"), 0) or 0) + (fnum(s.get("sellVolume"), 0) or 0)
    created = scanner._iso_ts((x.get("firstPool") or {}).get("createdAt")) or scanner._iso_ts(x.get("createdAt"))
    a = x.get("audit") or {}
    return {
        "mint": x.get("id"), "symbol": (x.get("symbol") or "?")[:24], "name": x.get("name"),
        "price": fnum(x.get("usdPrice")), "liq": fnum(x.get("liquidity"), 0) or 0,
        "mcap": fnum(x.get("mcap")) or fnum(x.get("fdv")) or 0, "holders": int(fnum(x.get("holderCount"), 0) or 0),
        "organic": fnum(x.get("organicScore"), 0) or 0, "top_pct": fnum(a.get("topHoldersPercentage")),
        "mint_off": bool(a.get("mintAuthorityDisabled")) and not x.get("mintAuthority"),
        "freeze_off": bool(a.get("freezeAuthorityDisabled")) and not x.get("freezeAuthority"),
        "tags": [str(t).lower() for t in (x.get("tags") or [])],
        "age_h": (time.time() - created) / 3600 if created else None,
        "vol_h1": vol(s1), "vol_h24": vol(s24),
        "buys_h1": int(fnum(s1.get("numBuys"), 0) or 0), "sells_h1": int(fnum(s1.get("numSells"), 0) or 0),
        "buys_m5": int(fnum(s5.get("numBuys"), 0) or 0), "sells_m5": int(fnum(s5.get("numSells"), 0) or 0),
        "vol_m5": vol(s5), "net_buyers_h1": int(fnum(s1.get("numNetBuyers"), 0) or 0),
        "chg_m5": fnum(s5.get("priceChange"), 0) or 0, "chg_h1": fnum(s1.get("priceChange"), 0) or 0,
        "chg_h6": fnum(s6.get("priceChange"), 0) or 0, "chg_h24": fnum(s24.get("priceChange"), 0) or 0,
        "pool": (x.get("firstPool") or {}).get("id"),
    }

# ------------------------------------------------------------------ rules (pure, testable)
def excluded(m, e):
    """Reason the coin is not a candidate at all (SOL, wrapped majors, stablecoins, LSTs, stocks...), else None."""
    if m["mint"] in EXCLUDE_MINTS:
        return "SOL / stablecoin / wrapped major / LST (fixed list)"
    bad = sorted(set(m["tags"]) & {t.lower() for t in e["exclude_tags"]})
    if bad:
        return "excluded type (Jupiter tag: " + ", ".join(bad) + ")"
    if _SYM_RE.search(m["symbol"] or ""):
        return f"symbol {m['symbol']} looks like SOL / a wrapped major / a stablecoin / an LST"
    if m["price"] and 0.95 <= m["price"] <= 1.05 and abs(m["chg_h24"]) < 2:
        return "price pinned near $1 (stablecoin-like)"
    return None

def quality(m, e):
    """Established-coin quality filters. Returns (stage, reason) of the first failure, or None."""
    if m["age_h"] is None:
        return "age", "pool age unknown"
    if m["age_h"] < float(e["min_pool_age_hours"]):
        return "age", f"pool age {m['age_h']:.1f}h < {e['min_pool_age_hours']}h (new-coin strategy's range)"
    if m["age_h"] > float(e["max_pool_age_days"]) * 24:
        return "age", f"pool age {m['age_h'] / 24:.0f}d > {e['max_pool_age_days']}d"
    if e["require_mint_revoked"] and not m["mint_off"]:
        return "contract", "mint authority not revoked"
    if e["require_freeze_revoked"] and not m["freeze_off"]:
        return "contract", "freeze authority not revoked"
    if m["liq"] < float(e["min_liquidity_usd"]):
        return "liquidity", f"liquidity ${m['liq']:,.0f} < ${float(e['min_liquidity_usd']):,.0f} (Jupiter, all pools)"
    if m["vol_h24"] < float(e["min_volume_h24_usd"]):
        return "volume", f"24h volume ${m['vol_h24']:,.0f} < ${float(e['min_volume_h24_usd']):,.0f}"
    if m["vol_h1"] < float(e["min_volume_h1_usd"]):
        return "volume", f"1h volume ${m['vol_h1']:,.0f} < ${float(e['min_volume_h1_usd']):,.0f}"
    if not (float(e["min_market_cap_usd"]) <= m["mcap"] <= float(e["max_market_cap_usd"])):
        return "market_cap", f"market cap ${m['mcap']:,.0f} outside ${float(e['min_market_cap_usd']):,.0f}-${float(e['max_market_cap_usd']):,.0f}"
    if m["holders"] < int(e["min_holders"]):
        return "holders", f"{m['holders']:,} holders < {int(e['min_holders']):,}"
    if m["organic"] < float(e["min_organic_score"]):
        return "organic", f"organic score {m['organic']:.0f} < {e['min_organic_score']} (much of the trading may be bots)"
    if m["top_pct"] is not None and m["top_pct"] > float(e["max_top_holders_pct"]):
        return "holders", f"top holders own {m['top_pct']:.0f}% > {e['max_top_holders_pct']}%"
    if m["buys_h1"] < int(e["min_buys_h1"]) or m["sells_h1"] < int(e["min_sells_h1"]):
        return "txns", f"1h trades {m['buys_h1']} buys / {m['sells_h1']} sells (need {e['min_buys_h1']}/{e['min_sells_h1']})"
    return None

def timing(m, e):
    """Pullback-and-bounce entry (never chase a pump). Returns None if the entry is good, else (stage, reason)."""
    if m["chg_h24"] > float(e["max_h24_change_pct"]):
        return "timing", f"up {m['chg_h24']:.0f}% in 24h > {e['max_h24_change_pct']}% (still pumping, not established)"
    if m["chg_h24"] < float(e["min_h24_change_pct"]):
        return "timing", f"down {m['chg_h24']:.0f}% in 24h (collapse, not a pullback)"
    if m["chg_h1"] > float(e["max_h1_pump_pct"]):
        return "timing", f"up {m['chg_h1']:.1f}% in 1h > {e['max_h1_pump_pct']}% (don't chase)"
    if m["chg_h6"] > -float(e["pullback_min_pct"]):
        return "timing", f"no pullback: 6h change {m['chg_h6']:+.1f}% (need -{e['pullback_min_pct']}% or lower)"
    if m["chg_h6"] < -float(e["pullback_max_pct"]):
        return "timing", f"6h drop {m['chg_h6']:.1f}% deeper than -{e['pullback_max_pct']}% (falling knife)"
    if m["chg_h1"] < float(e["bounce_min_h1_pct"]):
        return "timing", f"no bounce yet: 1h change {m['chg_h1']:+.1f}% (need +{e['bounce_min_h1_pct']}%)"
    if m["chg_m5"] < float(e["min_m5_pct"]):
        return "timing", f"falling right now: 5m change {m['chg_m5']:+.1f}%"
    if m["net_buyers_h1"] < int(e["min_net_buyers_h1"]):
        return "timing", f"net buyers in the last hour {m['net_buyers_h1']} (need {e['min_net_buyers_h1']}+)"
    return None

def rug_ok(mint, cfg, pair):
    """RugCheck for older coins. Not rugged, no 'danger' risk, no transfer fee; LP lock rule 'any' = at least one of
    the coin's pools has >= min_lp_locked_pct locked/burned (concentrated-liquidity pools - Orca, Raydium CLMM,
    Meteora DLMM - are market-maker positions that cannot be locked and always show 0%). 'pool' = the main rule."""
    e, R = E(cfg), rugcheck.cfg_rc(cfg)
    if not R["enabled"]:
        return True, "rug check off"
    if e["rugcheck_lp_rule"] == "pool":
        return rugcheck.check(mint, cfg, pair)
    try:
        j = rugcheck.report(mint)
    except Exception as ex:
        return (not R["fail_closed"]), f"rug check unavailable ({ex})"
    if j.get("rugged"):
        return False, "RugCheck marks this coin as already rugged"
    if fnum((j.get("transferFee") or {}).get("pct"), 0):
        return False, f"token has a transfer fee ({(j.get('transferFee') or {}).get('pct')}%)"
    if R["reject_danger"]:
        dangers = sorted({x.get("name", "?") for x in j.get("risks") or [] if x.get("level") == "danger"})
        if dangers:
            return False, "RugCheck danger: " + ", ".join(dangers)
    lps = [fnum((mk.get("lp") or {}).get("lpLockedPct"), 0) or 0 for mk in j.get("markets") or []]
    best = max(lps) if lps else 0
    if best < float(R["min_lp_locked_pct"]):
        return False, f"no pool with locked LP (best {best:.0f}% < {R['min_lp_locked_pct']}%)"
    return True, f"rug check ok (a pool has {best:.0f}% LP locked, no danger flags)"

# ------------------------------------------------------------------ discovery + evaluation round
def _held_or_cooling(con, e, mint, symbol):
    since = now_ts() - float(e["reentry_cooldown_hours"]) * 3600
    return con.execute(f"SELECT id, symbol, status FROM positions WHERE (token=? OR UPPER(symbol)=UPPER(?)) "
                       f"AND (status='open' OR closed_at>?) AND {EST}", (mint, symbol or "", since)).fetchone()

def ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS estab_evals(id INTEGER PRIMARY KEY AUTOINCREMENT, round_ts REAL, token TEXT,
                   symbol TEXT, result TEXT, stage TEXT, reasons TEXT, score REAL, metrics TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_estab_evals_ts ON estab_evals(round_ts)")

def run_round(con, cfg):
    """One discovery/evaluation round. Returns the round summary (also saved as state 'estab_last_round')."""
    e = E(cfg)
    ensure(con)
    t0 = now_ts()
    calls0 = CALLS.get("jup", 0)
    recs, ncalls, failed = fetch_lists(cfg)
    listed = len(recs)
    # coins that passed the quality filters recently but dropped off the lists: one batch lookup (<=100)
    watch = {k: v for k, v in (get_state(con, "estab_watch") or {}).items() if t0 - v < float(e["watch_hours"]) * 3600}
    extra = [m for m in sorted(watch, key=lambda k: -watch[k]) if m not in recs][:100]
    if extra:
        recs.update(batch(extra))
    funnel = {"listed": listed, "rechecked": len(extra), "evaluated": 0, "excluded": 0, "rejected": {}, "passed": 0,
              "list_calls": ncalls, "list_calls_failed": failed}
    passed, rows = [], []
    for mint, x in recs.items():
        m = metrics(x)
        funnel["evaluated"] += 1
        why = excluded(m, e)
        if why:
            funnel["excluded"] += 1
            continue
        q = quality(m, e)
        if q:
            funnel["rejected"][q[0]] = funnel["rejected"].get(q[0], 0) + 1
            if q[0] not in ("age",):
                rows.append((m, "reject", q[0], q[1]))
            continue
        watch[mint] = t0
        funnel["quality_ok"] = funnel.get("quality_ok", 0) + 1
        tm = timing(m, e)
        if tm:
            funnel["rejected"]["timing"] = funnel["rejected"].get("timing", 0) + 1
            rows.append((m, "wait", tm[0], tm[1]))
            continue
        funnel["passed"] += 1
        passed.append(m)
        rows.append((m, "pass", "entry", f"pullback {m['chg_h6']:+.1f}% (6h), bounce {m['chg_h1']:+.1f}% (1h), "
                                          f"organic {m['organic']:.0f}, {m['holders']:,} holders"))
    set_state(con, "estab_watch", watch)
    keep = ("price", "liq", "mcap", "holders", "organic", "age_h", "vol_h1", "vol_h24", "chg_m5", "chg_h1", "chg_h6", "chg_h24")
    for m, res, stage, reason in rows:
        con.execute("INSERT INTO estab_evals(round_ts,token,symbol,result,stage,reasons,score,metrics) VALUES(?,?,?,?,?,?,?,?)",
                    (t0, m["mint"], m["symbol"], res, stage, reason, round(m["organic"], 1),
                     json.dumps({k: (round(m[k], 4) if isinstance(m[k], float) else m[k]) for k in keep})))
    con.commit()
    opened = try_entries(con, cfg, passed) if passed else 0
    funnel["opened"] = opened
    summary = {"ts": t0, "duration": round(now_ts() - t0, 1), "funnel": funnel, "jup_calls": CALLS.get("jup", 0) - calls0,
               "candidates": [{"symbol": m["symbol"], "mint": m["mint"], "chg_h6": m["chg_h6"], "chg_h1": m["chg_h1"],
                               "organic": round(m["organic"]), "liq": round(m["liq"])} for m in passed[:10]],
               "watchlist": len(watch)}
    set_state(con, "estab_last_round", summary)
    con.commit()
    log.info("[older coins] round: listed %s (+%s rechecked), evaluated %s, excluded %s, passed quality %s, "
             "entry signals %s, opened %s, jup calls %s, watchlist %s, rejected %s", listed, len(extra), funnel["evaluated"],
             funnel["excluded"], funnel.get("quality_ok", 0), funnel["passed"], opened, summary["jup_calls"], len(watch),
             json.dumps(funnel["rejected"]))
    return summary

def _skip(con, m, msg):
    if not con.execute("SELECT 1 FROM decisions WHERE kind='SKIP' AND token=? AND ts>? AND message LIKE '[older coins]%'",
                       (m["mint"], now_ts() - 1800)).fetchone():
        decision(con, "SKIP", m["mint"], m["symbol"], "[older coins] " + msg)

def ds_pool(mint):
    """Deepest DexScreener pool for the coin + its price. Returns (status, pool):
    'ok' + pool dict | 'busy' (paused after a 429, or the call failed) | 'none' (DexScreener answered: not listed)."""
    if ds_paused():
        return "busy", None
    pairs, failed = scanner.ds_tokens_ex([mint], retries=1, honour_pause=True)
    if mint in failed:
        return "busy", None
    ps = [p for p in pairs.get(mint, []) if (p.get("baseToken") or {}).get("address") == mint]
    if not ps:
        return "none", None
    bp = scanner.best_pair(ps, None)
    dp = {"pair": bp.get("pairAddress"), "url": bp.get("url"), "price": fnum(bp.get("priceUsd")),
          "liq": fnum((bp.get("liquidity") or {}).get("usd"))}
    return ("ok", dp) if dp["pair"] and dp["price"] else ("none", None)

GT = "https://api.geckoterminal.com/api/v2"

def gt_token(mint):
    """GeckoTerminal price + top pool for the coin, or None (never waits on GeckoTerminal's rate-limit back-off)."""
    from common import LIMITS
    if LIMITS["gt"].blocked_until > time.time() + 5:
        return None
    j = http_get("gt", f"{GT}/networks/solana/tokens/{mint}", retries=1)
    d = (j or {}).get("data") or {}
    a = d.get("attributes") or {}
    price = fnum(a.get("price_usd"))
    if not price:
        return None
    tops = ((d.get("relationships") or {}).get("top_pools") or {}).get("data") or []
    pool = tops[0]["id"].split("_", 1)[-1] if tops and tops[0].get("id") else None
    return {"price": price, "pool": pool}

def jup_fallback(cfg, m):
    """Pre-buy check when DexScreener is busy/paused (Oct 3, Sammy): fresh Jupiter reading through the SAME older-coin
    rules + jupcheck.quote_check ($position_usd trade quote + $fallback_depth_usd depth quote) + GeckoTerminal price
    cross-check when GeckoTerminal answers. Read-only (quotes are never executed). Returns dict ok/reason/price/liq/pair/url/details."""
    import jupcheck
    e = E(cfg)
    out = {"ok": False, "reason": "", "price": None, "liq": None, "pair": None, "url": None, "details": {}}
    det = out["details"]
    key = jkey()
    if not key:
        out["reason"] = "Jupiter key missing"; return out
    x = jupcheck._search(m["mint"], key)
    if not x or not fnum(x.get("usdPrice")):
        out["reason"] = "no fresh Jupiter reading"; return out
    f = metrics(x)
    det.update({k: (round(f[k], 6) if isinstance(f[k], float) else f[k]) for k in ("price", "liq", "chg_h1", "chg_h6", "holders")})
    bad = excluded(f, e) or quality(f, e) or timing(f, e)
    if bad:
        out["reason"] = "fresh Jupiter reading fails the rules: " + (bad if isinstance(bad, str) else bad[1]); return out
    tol = float(e["fallback_price_tolerance_pct"])
    if m.get("price") and abs(f["price"] / m["price"] - 1) * 100 > tol:
        out["reason"] = f"fresh Jupiter price ${f['price']:.6g} differs {abs(f['price'] / m['price'] - 1) * 100:.1f}% from scan price ${m['price']:.6g} (> {tol}%)"
        return out
    import live
    try:
        solpx = live.sol_price()
    except Exception as ex:
        out["reason"] = f"SOL price unavailable ({ex})"; return out
    qc = jupcheck.quote_check(m["mint"], x, f["price"], float(e["position_usd"]), float(e["fallback_depth_usd"]),
                              float(e["fallback_min_liq_usd"]), float(e["fallback_max_trade_impact_pct"]), key, solpx)
    det.update(qc["details"])
    if not qc["ok"]:
        out["reason"] = qc["reason"]; return out
    gt = gt_token(m["mint"]) if e["fallback_gt_check"] else None
    det["gt_price"] = gt["price"] if gt else None
    if gt and not pg.agree(f["price"], gt["price"], tol):
        out["reason"] = f"Jupiter ${f['price']:.6g} vs GeckoTerminal ${gt['price']:.6g} differ more than {tol}%"; return out
    pair = (gt or {}).get("pool") or qc["amm"] or f["pool"]
    liq_est = qc["liq_est"]
    det["pool_from"] = "geckoterminal_top_pool" if (gt or {}).get("pool") else ("jupiter_route" if qc["amm"] else "jupiter_first_pool")
    out.update(ok=True, price=f["price"], liq=f["liq"], fill_liq=min(f["liq"], liq_est) if liq_est != float("inf") else f["liq"],
               pair=pair, url=f"https://dexscreener.com/solana/{pair.lower()}" if pair else None,
               reason=(f"Jupiter check ok: ${float(e['position_usd']):.0f} cost {qc['trade_imp'] * 100:.2f}%, "
                       f"${float(e['fallback_depth_usd']):.0f} impact {qc['i_depth'] * 100:.3f}% "
                       + (f"(~${liq_est:,.0f} liquidity)" if liq_est != float("inf") else "(deep)")
                       + (f", GeckoTerminal ${gt['price']:.6g} agrees" if gt else ", GeckoTerminal unavailable")))
    return out

def try_entries(con, cfg, cands):
    """Fake-money entries for the established strategy. NEVER calls live.py (real wallet)."""
    e = E(cfg)
    G = pg.cfg_guard(cfg)
    opened = 0
    with LOCK:
        ok, dpnl, dstart = day_check(con, cfg)
        for m in sorted(cands, key=lambda m: -m["organic"]):
            if opened >= int(e["max_entries_per_round"]):
                break
            if not ok:
                _skip(con, m, f"entry signal but the older-coin daily stop (-{e['daily_stop_pct']}%) is hit")
                break
            if (get_state(con, "manual_pause") or {}).get("on"):
                _skip(con, m, "entry signal but new entries are paused (Telegram /pause)")
                break
            if _held_or_cooling(con, e, m["mint"], m["symbol"]):
                continue
            n_open = con.execute(f"SELECT COUNT(*) FROM positions WHERE status='open' AND {EST}").fetchone()[0]
            if n_open >= int(e["max_open"]):
                _skip(con, m, f"entry signal but max older-coin positions ({e['max_open']}) reached")
                break
            cs = cash(con, cfg)
            size = min(float(e["position_usd"]), m["liq"] * float(e["max_pct_of_pool_liquidity"]) / 100, cs)
            if size < 1:
                _skip(con, m, f"size ${size:.2f} too small (older-coin cash ${cs:.2f})")
                break
            rc_ok, rc_msg = rug_ok(m["mint"], cfg, None)
            if not rc_ok:
                _skip(con, m, f"entry signal but rug check failed: {rc_msg}")
                continue
            st, dp = ds_pool(m["mint"])
            entry_source, fb = "jupiter", None
            if st == "busy" and e["prebuy_fallback"] == "jupiter_quote":
                try:
                    fb = jup_fallback(cfg, m)
                except Exception as ex:
                    fb = {"ok": False, "reason": f"Jupiter check error: {ex}"}
                log.info("[older coins] pre-buy fallback %s: %s", m["symbol"], fb["reason"])
                if not fb["ok"]:
                    pg.reject(con, m["mint"], m["symbol"], f"[older coins] buy skipped: DexScreener busy, Jupiter check failed: {fb['reason']}")
                    continue
                entry_source = "jup_fallback"
                price, liq, fill_liq = fb["price"], fb["liq"], fb["fill_liq"]
                dp = {"pair": fb["pair"], "url": fb["url"], "price": None, "liq": None}
            elif st != "ok":
                _skip(con, m, "entry signal but DexScreener " + ("busy/paused" if st == "busy" else "has no pool for it")
                      + " - retry next round")
                continue
            else:
                if not pg.agree(m["price"], dp["price"], float(e["entry_price_agree_pct"])):
                    pg.reject(con, m["mint"], m["symbol"], f"[older coins] buy skipped: Jupiter ${m['price']:.6g} vs "
                              f"DexScreener ${dp['price']:.6g} differ more than {e['entry_price_agree_pct']}%")
                    continue
                price, liq = m["price"], m["liq"]
                fill_liq = liq
            eff = price * pg.buy_price_multiplier(size, fill_liq, e, G)
            qty = size * (1 - float(e["fee_pct"]) / 100) / eff
            chk = {"ds_price": dp["price"], "ds_liq": dp["liq"], "chg_h6": m["chg_h6"], "chg_h1": m["chg_h1"],
                   "holders": m["holders"], "age_days": round(m["age_h"] / 24, 1), "rugcheck": rc_msg}
            if fb:
                chk = dict(chk, **fb["details"], reason=fb["reason"])
            con.execute("""INSERT INTO positions(token,pair,symbol,url,opened_at,entry_price,entry_eff,qty,remaining_qty,
                cost_usd,peak_price,last_price,last_update,status,score,entry_liq,pnl_usd,pnl_pct,last_liq,guard_last_seen,
                entry_source,entry_check,strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,0,0,?,?,?,?,?)""",
                        (m["mint"], dp["pair"], m["symbol"], dp["url"], now_ts(), price, eff, qty, qty, size, price, price,
                         now_ts(), round(m["organic"], 1), liq, liq, now_ts(), entry_source, json.dumps(chk), STRATEGY))
            pos_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            set_state(con, "estab_cash", cs - size)
            decision(con, "BUY", m["mint"], m["symbol"],
                     f"{TAG} BUY ${size:,.2f} @ ${price:.10g} (eff ${eff:.10g}), pullback {m['chg_h6']:+.1f}% 6h / bounce "
                     f"{m['chg_h1']:+.1f}% 1h, organic {m['organic']:.0f}, {m['holders']:,} holders, liq ${liq:,.0f}, "
                     f"pool {m['age_h'] / 24:.0f}d old. Paper only - never real money."
                     + (f" [entry_source=jup_fallback: DexScreener busy, {fb['reason']}]" if fb else ""))
            txt = notify.fmt_buy(m["symbol"], price, size, round(m["organic"]), dp["url"], equity(con, cfg), tag=TAG,
                                 balance_label="Older-coin fake balance")
            if fb:
                txt = txt.replace(f"BUY {m['symbol']}", f"BUY {m['symbol']} (checked via Jupiter quote)", 1)
            notify.enqueue(con, f"buy:{pos_id}", txt
                           + f"\nPullback {m['chg_h6']:+.1f}% (6h), bounce {m['chg_h1']:+.1f}% (1h) · pool {m['age_h'] / 24:.0f} days old"
                           + "\n📝 Paper only, never real money (older-coin strategy)")
            opened += 1
        con.commit()
    return opened

# ------------------------------------------------------------------ open positions
def _sell(con, cfg, pos, qty, price, reason, liq=None):
    e = E(cfg)
    liq = pos["last_liq"] if liq is None else liq
    proceeds = value(qty, price, e, liq, cfg)
    remaining = pos["remaining_qty"] - qty
    total = (pos["proceeds_usd"] or 0) + proceeds
    set_state(con, "estab_cash", cash(con, cfg) + proceeds)
    if remaining <= pos["qty"] * 1e-9:
        pnl = total - pos["cost_usd"]
        con.execute("""UPDATE positions SET remaining_qty=0, proceeds_usd=?, status='closed', closed_at=?, exit_reason=?,
                       pnl_usd=?, pnl_pct=?, last_price=? WHERE id=?""",
                    (total, now_ts(), reason, pnl, pnl / pos["cost_usd"] * 100, price, pos["id"]))
        decision(con, "SELL", pos["token"], pos["symbol"], f"{TAG} SELL ALL @ ${price:.10g}: {reason}. "
                 f"Trade P&L ${pnl:+,.2f} ({pnl / pos['cost_usd'] * 100:+.1f}%)")
        notify.enqueue(con, f"sell:{pos['id']}:full", notify.fmt_sell(pos["symbol"], reason, pnl, pnl / pos["cost_usd"] * 100,
                       equity(con, cfg), url=pos["url"], tag=TAG, balance_label="Older-coin fake balance"))
    else:
        con.execute("UPDATE positions SET remaining_qty=?, proceeds_usd=?, tp1_done=1 WHERE id=?", (remaining, total, pos["id"]))
        sold_cost = pos["cost_usd"] * qty / pos["qty"]
        part = proceeds - sold_cost
        decision(con, "SELL", pos["token"], pos["symbol"],
                 f"{TAG} PARTIAL SELL {qty / pos['qty'] * 100:.0f}% @ ${price:.10g}: {reason} (+${proceeds:,.2f})")
        notify.enqueue(con, f"sell:{pos['id']}:partial:{int(pos['tp1_done'] or 0)}",
                       notify.fmt_sell(pos["symbol"], f"{reason} - sold {qty / pos['qty'] * 100:.0f}% of the position",
                                       part, part / sold_cost * 100, equity(con, cfg), partial=True, url=pos["url"],
                                       tag=TAG, balance_label="Older-coin fake balance"))

def reading(pos, x):
    """Jupiter record -> DexScreener-shaped reading for the price guard (coin-wide price/liquidity; same source as at entry)."""
    m = metrics(x)
    return {"pairAddress": pos["pair"], "baseToken": {"address": pos["token"]}, "priceUsd": str(m["price"] or 0),
            "liquidity": {"usd": m["liq"]}, "marketCap": m["mcap"] or None, "volume": {"m5": m["vol_m5"]},
            "txns": {"m5": {"buys": m["buys_m5"], "sells": m["sells_m5"]}}, "_m": m}

def check_exits(con, cfg, pos, rd):
    e = E(cfg)
    verdict, price, liq = pg.check_reading(con, cfg, pos, rd)
    import fills
    fills.tick(con, pos, price if price else rd.get("priceUsd"), "jupiter_batch",
               liq if price else (rd.get("liquidity") or {}).get("usd"), verdict)  # recording only (position_ticks)
    if verdict == "reject" or not price:
        return
    if verdict == "drained":
        con.execute("UPDATE positions SET last_price=?, last_liq=?, last_update=? WHERE id=?", (price, liq, now_ts(), pos["id"]))
        pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"rug: liquidity pulled (${liq:,.0f}, confirmed on 2 checks)", liq)
    peak = max(pos["peak_price"] or price, price)
    con.execute("UPDATE positions SET last_price=?, peak_price=?, last_liq=?, last_update=? WHERE id=?",
                (price, peak, liq, now_ts(), pos["id"]))
    pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
    gain = (price / pos["entry_price"] - 1) * 100
    if gain <= -float(e["stop_loss_pct"]):
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"stop loss ({gain:.1f}% <= -{e['stop_loss_pct']}%)")
    if gain >= float(e["take_profit2_pct"]):
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"take profit 2 (+{gain:.1f}%)")
    if gain >= float(e["take_profit_pct"]) and not pos["tp1_done"]:
        frac = float(e["take_profit_sell_fraction"])
        return _sell(con, cfg, pos, pos["remaining_qty"] * frac if frac < 1 else pos["remaining_qty"], price,
                     f"take profit 1 (+{gain:.1f}% >= +{e['take_profit_pct']}%)")
    peak_gain = (peak / pos["entry_price"] - 1) * 100
    drop = (1 - price / peak) * 100
    if peak_gain >= float(e["trailing_activate_pct"]) and drop >= float(e["trailing_stop_pct"]):
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"trailing stop ({drop:.1f}% off peak, peak was +{peak_gain:.1f}%)")
    r = float(e["volume_decay_ratio"] or 0)
    m = rd.get("_m") or {}
    if r > 0 and m.get("vol_h24") and m.get("vol_h1", 0) < r * m["vol_h24"] / 24:
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"volume decay (1h ${m['vol_h1']:,.0f} < {r * 100:.0f}% of 24h pace)")
    if now_ts() - pos["opened_at"] >= float(e["max_hold_hours"]) * 3600:
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"max hold time {e['max_hold_hours']}h")

def update_positions(con, cfg):
    """Re-price open older-coin positions with ONE Jupiter batch call, then TP/SL/trailing/max-hold. Paper only."""
    e = E(cfg)
    with LOCK:
        opens = con.execute(f"SELECT * FROM positions WHERE status='open' AND {EST}").fetchall()
        if opens:
            recs = batch([p["token"] for p in opens])
            for p in opens:
                x = recs.get(p["token"])
                if x and fnum(x.get("usdPrice")):
                    check_exits(con, cfg, p, reading(p, x))
                else:
                    log.warning("[older coins] no Jupiter price for %s this update", p["symbol"])
        for p in con.execute(f"SELECT * FROM positions WHERE status='open' AND {EST}").fetchall():
            val = (p["proceeds_usd"] or 0) + value(p["remaining_qty"], p["last_price"] or 0, e, p["last_liq"], cfg)
            con.execute("UPDATE positions SET pnl_usd=?, pnl_pct=? WHERE id=?", (val - p["cost_usd"], (val / p["cost_usd"] - 1) * 100, p["id"]))
        day_check(con, cfg)
        eq = equity(con, cfg)
        hist = get_state(con, "estab_equity_last") or 0
        if now_ts() - hist >= 300:
            con.execute("CREATE TABLE IF NOT EXISTS estab_equity(ts REAL, equity REAL, cash REAL)")
            con.execute("INSERT INTO estab_equity(ts,equity,cash) VALUES(?,?,?)", (now_ts(), eq, cash(con, cfg)))
            set_state(con, "estab_equity_last", now_ts())
        con.commit()

# ------------------------------------------------------------------ dashboard / stats
def stats(con, strategy, start=None, eq=None):
    """Closed-trade stats per strategy (shared format for the comparison block in data.json)."""
    w = "COALESCE(strategy,'new')=?"
    if strategy == "new":  # side-by-side scoring test: the new-coin side of this comparison stays the CURRENT account
        w += " AND scoring='current'"
    r = con.execute(f"""SELECT COUNT(*) n, SUM(pnl_usd>0) w, COALESCE(SUM(pnl_usd),0) pnl, AVG(pnl_pct) avg_pct,
                        MAX(pnl_pct) best, MIN(pnl_pct) worst, AVG(closed_at-opened_at) hold
                        FROM positions WHERE status='closed' AND {w}""", (strategy,)).fetchone()
    n_open = con.execute(f"SELECT COUNT(*) FROM positions WHERE status='open' AND {w}", (strategy,)).fetchone()[0]
    out = {"strategy": strategy, "closed": r["n"] or 0, "wins": r["w"] or 0,
           "win_rate": ((r["w"] or 0) / r["n"] * 100) if r["n"] else None, "realized_pnl": r["pnl"] or 0,
           "avg_trade_pct": r["avg_pct"], "best_trade_pct": r["best"], "worst_trade_pct": r["worst"],
           "avg_hold_hours": (r["hold"] / 3600) if r["hold"] else None, "open": n_open}
    if start is not None and eq is not None:
        out.update({"start": start, "equity": eq, "pnl": eq - start, "pnl_pct": (eq / start - 1) * 100 if start else 0})
    return out

def dashboard(con, cfg):
    e = E(cfg)
    eq, cs = equity(con, cfg), cash(con, cfg)
    start = get_state(con, "estab_starting_balance", float(e["starting_balance_usd"]))
    d = get_state(con, "estab_day") or {}
    opens = [dict(r) for r in con.execute(f"SELECT * FROM positions WHERE status='open' AND {EST} ORDER BY opened_at DESC")]
    for p in opens:
        en = p["entry_price"]
        p["targets"] = {"tp1": en * (1 + e["take_profit_pct"] / 100), "tp2": en * (1 + e["take_profit2_pct"] / 100),
                        "sl": en * (1 - e["stop_loss_pct"] / 100),
                        "trail": (p["peak_price"] * (1 - e["trailing_stop_pct"] / 100))
                                 if p["peak_price"] and p["peak_price"] >= en * (1 + e["trailing_activate_pct"] / 100) else None,
                        "max_hold_until": p["opened_at"] + e["max_hold_hours"] * 3600}
    closed = [dict(r) for r in con.execute(f"SELECT * FROM positions WHERE status='closed' AND {EST} ORDER BY closed_at DESC LIMIT 100")]
    try:
        eqc = [dict(r) for r in con.execute("SELECT ts,equity FROM estab_equity WHERE ts>? ORDER BY ts", (now_ts() - 7 * 86400,))]
    except Exception:
        eqc = []
    k = stats(con, STRATEGY, start, eq)
    k.update({"cash": cs, "max_open": int(e["max_open"]), "position_usd": float(e["position_usd"]),
              "day_pnl": eq - d["start"] if d.get("start") else 0, "day_stopped": bool(d.get("stopped")),
              "daily_stop_pct": float(e["daily_stop_pct"])})
    return {"enabled": bool(e["enabled"]), "paper_only": True, "label": "Older coins (paper only)",
            "kpi": k, "open": opens, "closed": closed, "equity": eqc, "last_round": get_state(con, "estab_last_round"),
            "rules": {"pool_age": f">{e['min_pool_age_hours']}h", "min_liquidity_usd": e["min_liquidity_usd"],
                      "min_volume_h24_usd": e["min_volume_h24_usd"], "min_holders": e["min_holders"],
                      "min_organic_score": e["min_organic_score"],
                      "entry": f"6h pullback -{e['pullback_min_pct']}% to -{e['pullback_max_pct']}%, then 1h bounce "
                               f"+{e['bounce_min_h1_pct']}% to +{e['max_h1_pump_pct']}%",
                      "take_profit": f"+{e['take_profit_pct']}% (sell {e['take_profit_sell_fraction'] * 100:.0f}%), +{e['take_profit2_pct']}% (rest)",
                      "stop_loss": f"-{e['stop_loss_pct']}%", "trailing": f"{e['trailing_stop_pct']}% after +{e['trailing_activate_pct']}%",
                      "max_hold_hours": e["max_hold_hours"]}}

# ------------------------------------------------------------------ loop (own thread in bot.py)
def loop(stop, load_config):
    last_round = 0.0
    while not stop.is_set():
        interval = 60
        try:
            cfg = load_config()
            e = E(cfg)
            interval = float(e["update_interval_sec"])
            from common import db
            con = db()
            try:
                update_positions(con, cfg)  # runs even if disabled, so open older-coin positions are always managed
                if e["enabled"] and time.time() - last_round >= float(e["discovery_interval_min"]) * 60:
                    last_round = time.time()
                    run_round(con, cfg)
                set_state(con, "estab_heartbeat", now_ts()); con.commit()
            finally:
                con.close()
        except Exception:
            import traceback
            log.error("[older coins] loop failed:\n%s", traceback.format_exc())
        stop.wait(interval)
