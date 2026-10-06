"""Price-sanity safeguards for the PAPER trader.

No buy, sell, take-profit, stop or trailing decision is made on a reading that fails these checks.
Every rejected reading is logged with a [PRICE-GUARD] tag. Thresholds live in config.toml [guard].
"""
import logging, time
from common import http_get, fnum, now_ts, decision, LIMITS

log = logging.getLogger("memebot")
GT = "https://api.geckoterminal.com/api/v2"

DEFAULTS = {
    "enabled": True,
    "max_jump_up_factor": 5.0,       # reading > 5x the last good price = extreme
    "max_drop_pct": 50,              # reading more than 50% below the last good price = extreme
    "confirm_tolerance_pct": 25,     # second source / next check must agree within this
    "confirm_window_sec": 300,       # a pending extreme reading must be confirmed within this
    "stale_gap_min": 10,             # after a gap this long (outage, freeze) the first reading is not acted on
    "liq_drain_pct": 80,             # liquidity down this much from entry (or reported 0) = pool drained
    "min_exit_liquidity_usd": 1000,  # below this the pool is treated as drained
    "max_supply_deviation_pct": 50,  # market cap / price must imply the same supply (+/- this)
    "use_second_source": True,       # ask GeckoTerminal to confirm extreme readings
    "fill_cap": True,                # constant-product cap on paper fills
    "entry_price_tolerance_pct": 20, # fresh re-read before a buy must be within this of the scan price
    "block_same_symbol": True,       # also treat same-ticker coins as the same position (copycats)
    "volume_decay_confirm": True,    # volume-decay exit needs two consecutive agreeing checks
}

def cfg_guard(cfg):
    g = dict(DEFAULTS)
    g.update(cfg.get("guard", {}))
    return g

_last_logged = {}
def reject(con, pos_or_token, symbol, msg, log_decision=True):
    log.warning("[PRICE-GUARD] %s: %s", symbol, msg)
    key = (pos_or_token, msg.split(":")[0][:40])
    if log_decision and time.time() - _last_logged.get(key, 0) > 600:
        _last_logged[key] = time.time()
        decision(con, "GUARD", pos_or_token if isinstance(pos_or_token, str) else None, symbol, "[PRICE-GUARD] " + msg)

# ---------------------------------------------------------------- fills
def cp_sell_usd(qty, price, liq):
    """What a constant-product pool with total liquidity `liq` (USD, both sides) pays for qty tokens."""
    v = qty * price
    if liq is None:
        return v
    r = max(liq, 0) / 2
    return r * v / (r + v) if r > 0 and v > 0 else 0.0

def sell_value(qty, price, P, G, liq=None):
    """Paper sell proceeds after slippage, fee and (optionally) the pool's constant-product limit."""
    gross = qty * price * (1 - P["slippage_pct"] / 100)
    if G.get("fill_cap", True) and liq is not None:
        gross = min(gross, cp_sell_usd(qty, price, liq))
    return gross * (1 - P["fee_pct"] / 100)

def buy_price_multiplier(size_usd, liq, P, G):
    """Effective buy price multiplier: max(assumed slippage, constant-product price impact)."""
    m = 1 + P["slippage_pct"] / 100
    if G.get("fill_cap", True) and liq:
        r = liq / 2
        m = max(m, (r + size_usd) / r)
    return m

# ---------------------------------------------------------------- second source
def gt_pool(pair, token):
    """GeckoTerminal price/reserve for a pool, or None. Never waits on a long rate-limit back-off."""
    if LIMITS["gt"].blocked_until > time.time() + 5:
        return None
    j = http_get("gt", f"{GT}/networks/solana/pools/{pair}", retries=1)
    a = ((j or {}).get("data") or {}).get("attributes") or {}
    rel = ((j or {}).get("data") or {}).get("relationships") or {}
    if not a:
        return None
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    price = fnum(a.get("base_token_price_usd")) if base.endswith(token) else fnum(a.get("quote_token_price_usd"))
    return {"price": price, "liq": fnum(a.get("reserve_in_usd"))}

def agree(a, b, tol_pct):
    return a and b and abs(a / b - 1) * 100 <= tol_pct

# ---------------------------------------------------------------- reading check for open positions
def check_reading(con, cfg, pos, pair):
    """Validate a DexScreener reading for an open position.
    Returns (verdict, price, liq):
      'ok'      -> act normally on price/liq
      'drained' -> pool liquidity is gone (confirmed): close at a liquidity-capped fill
      'reject'  -> do not act on this reading
    """
    G = cfg_guard(cfg)
    price = fnum(pair.get("priceUsd"))
    liq = fnum((pair.get("liquidity") or {}).get("usd"))
    if not G["enabled"]:
        return ("ok", price, liq)
    sym, pid, now = pos["symbol"], pos["id"], now_ts()
    upd = lambda **kw: con.execute("UPDATE positions SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), pid))
    last_seen = pos["guard_last_seen"]
    upd(guard_last_seen=now)
    # 1) identity: same pool and same coin as when the position was opened
    if pair.get("pairAddress") != pos["pair"] or (pair.get("baseToken") or {}).get("address") != pos["token"]:
        reject(con, pos["token"], sym, f"reading from a different pair/coin ({pair.get('pairAddress')}) ignored")
        return ("reject", None, None)
    if not price or price <= 0:
        reject(con, pos["token"], sym, "no usable price in reading")
        return ("reject", None, None)
    # 2) market cap must imply the same supply as before
    mcap = fnum(pair.get("marketCap")) or fnum(pair.get("fdv"))
    if mcap:
        supply = mcap / price
        if pos["guard_supply"]:
            dev = abs(supply / pos["guard_supply"] - 1) * 100
            if dev > G["max_supply_deviation_pct"]:
                reject(con, pos["token"], sym, f"market cap ${mcap:,.0f} inconsistent with price ${price:.4g} (implied supply off {dev:.0f}%)")
                return ("reject", None, None)
        else:
            upd(guard_supply=supply)
    last_good = pos["last_price"] or pos["entry_price"]
    ratio = price / last_good
    entry_liq = pos["entry_liq"] or 0
    drained = liq is not None and (liq < G["min_exit_liquidity_usd"] or (entry_liq and liq < entry_liq * (1 - G["liq_drain_pct"] / 100)))
    # 3) drained pool: a spike is fake; any move needs confirmation, then the position is closed at a capped fill
    if drained:
        if ratio >= 1.5:
            reject(con, pos["token"], sym, f"price ${price:.4g} ({ratio:.1f}x last good) on a drained pool (liquidity ${liq:,.0f} vs ${entry_liq:,.0f} at entry) - not a real price")
        if pos["guard_drain_ts"] and now - pos["guard_drain_ts"] <= G["confirm_window_sec"]:
            upd(guard_drain_ts=None)
            return ("drained", min(price, last_good), liq)
        upd(guard_drain_ts=now)
        reject(con, pos["token"], sym, f"liquidity ${liq:,.0f} (entry ${entry_liq:,.0f}) looks drained - waiting for confirmation on next check")
        return ("reject", None, None)
    elif pos["guard_drain_ts"]:
        upd(guard_drain_ts=None)
    # 4) extreme moves and first reading after a long gap need confirmation
    extreme = ratio >= G["max_jump_up_factor"] or ratio <= 1 - G["max_drop_pct"] / 100
    stale = last_seen is not None and now - last_seen > G["stale_gap_min"] * 60
    if extreme or stale:
        why = (f"{ratio:.2f}x vs last good ${last_good:.4g}" if extreme else f"first reading after a {(now - last_seen)/60:.0f}-min data gap")
        spike_up = extreme and ratio > 1
        gt = gt_pool(pos["pair"], pos["token"]) if extreme and G["use_second_source"] else None
        if gt is not None:
            gl = gt.get("liq")
            gt_drained = gl is not None and gl < G["min_exit_liquidity_usd"]
            if agree(price, gt["price"], G["confirm_tolerance_pct"]) and not (spike_up and gt_drained):
                upd(guard_pending_price=None, guard_pending_ts=None)
                log.info("[PRICE-GUARD] %s: %s confirmed by GeckoTerminal ($%.4g)", sym, why, gt["price"])
                return ("ok", price, liq)
            if spike_up:
                # the second source contradicts the spike (different price or drained pool): never act on it
                upd(guard_pending_price=None, guard_pending_ts=None)
                reject(con, pos["token"], sym, f"reading ${price:.4g} ({why}) contradicted by GeckoTerminal "
                       f"(${gt['price'] or 0:.4g}, pool liquidity ${gl or 0:,.0f})")
                return ("reject", None, None)
        pend = pos["guard_pending_price"]
        if pend and now - (pos["guard_pending_ts"] or 0) <= G["confirm_window_sec"] and agree(price, pend, G["confirm_tolerance_pct"]):
            m5 = pair.get("txns", {}).get("m5") or {}
            traded = (fnum((pair.get("volume") or {}).get("m5"), 0) or 0) > 0 and (m5.get("buys", 0) + m5.get("sells", 0)) > 0
            if not spike_up or traded:
                upd(guard_pending_price=None, guard_pending_ts=None)
                log.info("[PRICE-GUARD] %s: %s confirmed on next check, acting on it", sym, why)
                return ("ok", price, liq)
            reject(con, pos["token"], sym, f"spike ${price:.4g} repeated but with no trades in the last 5 min - not acted on")
            return ("reject", None, None)
        upd(guard_pending_price=price, guard_pending_ts=now)
        reject(con, pos["token"], sym, f"reading ${price:.4g} ({why}) held for confirmation")
        return ("reject", None, None)
    if pos["guard_pending_price"]:
        upd(guard_pending_price=None, guard_pending_ts=None)
    return ("ok", price, liq)
