"""Exchange-side stop-loss for REAL positions via Jupiter Trigger V2 (TRIAL, added Oct 2 2026, Sammy approved).

[live] trigger_stops = true and trigger_stops_trial_max = N: the next N confirmed real buys get a sell-below order on
Jupiter for the full token amount at entry x (1 - [paper] stop_loss_pct/100). Jupiter watches the price continuously.
 - Trailing: when the bot's trailing rule is active (peak >= +trailing_activate_pct) the trigger is PATCHed up to
   peak x (1 - trailing_stop_pct/100): never down, at most every trigger_trail_min_interval_sec, only on moves of
   >= trigger_trail_min_move_pct.
 - Bot exits (TP, trailing, volume decay, max hold, rug, sells after /livestop): live._do_sell calls
   release_before_sell() first = cancel the order + sign the withdrawal so the coins are back in the wallet, then sells.
   If Jupiter already sold them, the bot's sell is skipped. If the vault can't be emptied, the sell is retried every
   check interval and Telegram is told.
 - Fill detection: check() polls order history (every trigger_check_interval_sec) and books a filled order as a real
   sell with exit_reason 'stop loss (Jupiter trigger)', so live.closed[] shows real P&L.
 - Placement failure (or a position below Jupiter's 10 USD minimum): nothing changes, the bot's own stop runs, Telegram.
Paper trading is unchanged."""
import json, threading, time
from common import db, log, now_ts, get_state, set_state
import notify
import jtrigger as jt

DEFAULTS = {"trigger_stops": False, "trigger_stops_trial_max": 0, "trigger_slippage_bps": 1500,
            "trigger_trail_min_move_pct": 5.0, "trigger_trail_min_interval_sec": 300, "trigger_check_interval_sec": 60,
            "trigger_min_order_usd": 10.5, "trigger_expiry_extra_hours": 2, "trigger_sol_cost": 0.004}
_TLOCKS, _G = {}, threading.Lock()
_CHECK = {"running": False, "last": 0.0}

class VaultBusy(RuntimeError):
    pass

def T(cfg):
    d = dict(DEFAULTS); d.update({k: v for k, v in cfg.get("live", {}).items() if k.startswith("trigger_")}); return d

def _tlock(mint):
    with _G:
        return _TLOCKS.setdefault(mint, threading.Lock())

def _note(con, key, text):
    notify.enqueue(con, key, text); con.commit()

def _buy_row(con, pos_id):
    return con.execute("SELECT * FROM live_trades WHERE pos_id=? AND side='buy' AND status='ok' ORDER BY id DESC LIMIT 1",
                       (pos_id,)).fetchone()

def trials_left(con, cfg):
    t = T(cfg)
    if not t["trigger_stops"]:
        return 0
    return max(int(t["trigger_stops_trial_max"]) - int(get_state(con, "trigger_trials_used", 0) or 0), 0)

def _set(con, buy_id, **kw):
    con.execute("UPDATE live_trades SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), buy_id))
    con.commit()

# ---------------------------------------------------------------- placement
def _place(con, cfg, buy_id, pos_id, mint, symbol, raw, trig, label):
    import live
    t = T(cfg)
    hold_h = float(cfg["paper"].get("max_hold_hours", 24)) + float(t["trigger_expiry_extra_hours"])
    exp_ms = int((time.time() + hold_h * 3600) * 1000)
    try:
        r = jt.place_stop(mint, live.SOL, raw, trig, int(t["trigger_slippage_bps"]), exp_ms)
    except Exception as e:
        # did the deposit leave the wallet anyway? then find the order so the coins are not forgotten in the vault
        left = None
        try:
            left = live.token_raw(cfg, mint)
        except Exception:
            pass
        if left is not None and left < raw * 0.99:
            _set(con, buy_id, trig_state="release_failed", trig_note=f"place error after deposit: {str(e)[:200]}")
            _note(con, f"trig-orphan:{buy_id}", f"🚨 {symbol}: Jupiter stop order failed but the coins left the wallet. "
                  "The bot will try to withdraw them from the Jupiter vault before any sell.")
        else:
            _set(con, buy_id, trig_state="place_failed", trig_note=str(e)[:300])
            _note(con, f"trig-fail:{buy_id}", f"⚠️ Jupiter stop for {symbol} could not be placed ({str(e)[:160]}). "
                  f"The bot's own -{cfg['paper']['stop_loss_pct']:g}% stop is still watching it.")
        log.warning("trigger stop %s for %s failed: %s", label, symbol, e)
        return False
    _set(con, buy_id, trig_id=r.get("id"), trig_price=float(trig), trig_state="open" if r.get("depositConfirmed", True) else "pending",
         trig_ts=now_ts(), trig_raw=str(int(raw)), trig_note=json.dumps({"deposit_tx": r.get("txSignature"), "expires_ms": exp_ms}))
    log.info("trigger stop %s for %s: order %s at $%.6g", label, symbol, r.get("id"), trig)
    return True

def after_buy(con, cfg, buy_id, pos_id, mint, symbol, raw, usd):
    """Called by live._do_buy after a confirmed real buy. Never raises."""
    import live
    try:
        live.ensure(con)
        if trials_left(con, cfg) <= 0 or not raw:
            return
        t = T(cfg)
        pos = con.execute("SELECT entry_price FROM positions WHERE id=?", (pos_id,)).fetchone()
        if not pos or not pos["entry_price"]:
            return
        sl = float(cfg["paper"]["stop_loss_pct"])
        trig = pos["entry_price"] * (1 - sl / 100)
        if usd < float(t["trigger_min_order_usd"]):
            # Jupiter rejects price orders under 10 USD; the trial is NOT used up
            _set(con, buy_id, trig_state="too_small", trig_note=f"${usd:.2f} < Jupiter minimum")
            _note(con, f"trig-small:{buy_id}", f"ℹ️ Jupiter stop not set for {symbol}: the ${usd:.2f} position is below Jupiter's "
                  f"$10 minimum order. The bot's own -{sl:g}% stop is watching it.")
            return
        bal = live.sol_balance(cfg)
        need = float(live.L(cfg)["min_sol_reserve"]) + float(t["trigger_sol_cost"])
        if bal < need:
            _set(con, buy_id, trig_state="place_failed", trig_note=f"SOL {bal:.4f} < {need:.4f} needed for the order")
            _note(con, f"trig-fail:{buy_id}", f"⚠️ Jupiter stop for {symbol} not placed: wallet SOL {bal:.4f} too low for the "
                  f"vault deposit fees (needs {need:.4f}). The bot's own -{sl:g}% stop is watching it.")
            return
        if _place(con, cfg, buy_id, pos_id, mint, symbol, raw, trig, "place"):
            set_state(con, "trigger_trials_used", int(get_state(con, "trigger_trials_used", 0) or 0) + 1); con.commit()
            _note(con, f"trig-set:{buy_id}", f"🛡 Stop set on Jupiter for {symbol} at {notify.fmt_price(trig)} (-{sl:g}%). "
                  "Jupiter watches the price continuously.")
    except Exception as e:
        log.exception("trigger after_buy failed: %s", e)

def replace_after_partial(con, cfg, pos_id, mint, symbol):
    """After a partial bot sell (TP1) the order was withdrawn; re-place it for what is left if still >= the minimum."""
    import live
    try:
        b = _buy_row(con, pos_id)
        if not b or not b["trig_price"]:
            return
        t = T(cfg)
        raw = live.token_raw(cfg, mint)
        p = con.execute("SELECT last_price, entry_price FROM positions WHERE id=?", (pos_id,)).fetchone()
        val = b["usd"] * (raw / int(b["token_raw"] or raw or 1)) * ((p["last_price"] or 0) / p["entry_price"]) if p and p["entry_price"] else 0
        if raw <= 0 or val < float(t["trigger_min_order_usd"]):
            _set(con, b["id"], trig_state="released", trig_note=f"rest ${val:.2f} below Jupiter minimum, bot stop only")
            _note(con, f"trig-rest:{b['id']}", f"ℹ️ {symbol}: rest of the real position (${val:.2f}) is below Jupiter's $10 minimum, "
                  "so the bot's own stop handles it from here.")
            return
        if _place(con, cfg, b["id"], pos_id, mint, symbol, raw, b["trig_price"], "re-place"):
            _note(con, f"trig-replace:{b['id']}:{int(now_ts())}", f"🛡 Stop re-set on Jupiter for the rest of {symbol} at {notify.fmt_price(b['trig_price'])}.")
    except Exception as e:
        log.exception("trigger re-place failed: %s", e)

# ---------------------------------------------------------------- fills
def _record_fill(con, cfg, b, o):
    import live
    frac, out_raw, sig = jt.fill_summary(o)
    if frac <= 0 and o.get("orderState") == "filled":
        frac = 1.0
    if frac <= 0:
        return False
    usd = out_raw / 1e9 * live.sol_price()
    full = frac >= 0.999
    try:
        sold_raw = str(int(int(b["trig_raw"] or 0) * (1.0 if full else frac))) if b["trig_raw"] else None
    except Exception:
        sold_raw = None
    con.execute("""INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,sig,status,reason,token_raw)
                   VALUES(?,?,?,?,'sell',?,?,?,'ok',?,?)""",
                (now_ts(), b["pos_id"], b["mint"], b["symbol"], 1.0 if full else frac, usd, sig,
                 "stop loss (Jupiter trigger)" if full else "stop loss (Jupiter trigger, partial fill)", sold_raw))
    sell_tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    _set(con, b["id"], trig_state="filled" if full else "partial")
    pnl = usd - b["usd"] if full else None
    _note(con, f"trig-fill:{b['id']}:{int(frac * 100)}",
          f"🛡 Jupiter stop SOLD {b['symbol']} ({frac * 100:.0f}%): got back ${usd:.2f}"
          + (f", real P&L {notify.fmt_usd(pnl)} ({pnl / b['usd'] * 100:+.1f}%)" if pnl is not None else "")
          + (f"\nhttps://solscan.io/tx/{sig}" if sig else ""))
    log.info("Jupiter stop filled for %s: %.0f%% -> $%.2f", b["symbol"], frac * 100, usd)
    if sig:
        try:  # recording only (fills.py): actual fill of the Jupiter keeper's transaction
            import fills
            pp = con.execute("SELECT last_price FROM positions WHERE id=?", (b["pos_id"],)).fetchone()
            con.commit()
            fills.record_async(cfg, sell_tid, paper_price=pp["last_price"] if pp else None)
        except Exception as e:
            log.warning("fill recorder (Jupiter stop) not started: %s", e)
    return True

# ---------------------------------------------------------------- bot exits
def release_before_sell(con, cfg, pos_id, mint, symbol, wait_s=180):
    """Make sure the coins are in the wallet before the bot sells. Returns 'none' (no order), 'released'
    (order cancelled + withdrawn) or 'filled' (Jupiter already sold them: the bot must not sell). Raises VaultBusy."""
    import live
    live.ensure(con)
    b = _buy_row(con, pos_id)
    if not b or not b["trig_id"] or b["trig_state"] not in ("open", "pending", "release_failed", "releasing", "partial"):
        return "none"
    with _tlock(mint):
        _set(con, b["id"], trig_state="releasing")
        before = live.token_raw(cfg, mint)
        deadline, last = time.time() + wait_s, None
        while time.time() < deadline:
            try:
                o = jt.order(b["trig_id"], mint)
            except Exception as e:
                o, last = None, e
            st = (o or {}).get("orderState")
            if st == "filled":
                _record_fill(con, cfg, b, o)
                return "filled"
            if st == "executing":          # Jupiter is selling right now: wait for the outcome
                time.sleep(8); continue
            if st == "cancelled" or (st in ("expired", "failed") and (o or {}).get("rawState") == "deposit_failed"):
                break                      # nothing left in the vault for this order
            try:
                if o and jt.fill_summary(o)[0] > 0:
                    _record_fill(con, cfg, b, o)  # partly sold by Jupiter: book that part, withdraw the rest
                jt.cancel_withdraw(b["trig_id"])
                break
            except Exception as e:
                last = e
                log.warning("trigger release %s: %s", symbol, e)
                time.sleep(8)
        else:
            _set(con, b["id"], trig_state="release_failed", trig_note=f"release failed: {str(last)[:250]}")
            raise VaultBusy(f"Jupiter vault withdrawal failed: {str(last)[:200]}")
        for _ in range(20):   # wait until the withdrawn coins are visible in the wallet
            if live.token_raw(cfg, mint) > before:
                break
            time.sleep(3)
        _set(con, b["id"], trig_state="released")
        log.info("trigger stop for %s cancelled and withdrawn before the bot's sell", symbol)
        return "released"

# ---------------------------------------------------------------- trailing
def on_trail(con, cfg, pos):
    """Called by the paper trader after each good price reading. Cheap unless the stop should move."""
    try:
        import live
        live.ensure(con)
        b = _buy_row(con, pos["id"])
        if not b or b["trig_state"] != "open" or not b["trig_price"]:
            return
        t, P = T(cfg), cfg["paper"]
        entry, peak, last = pos["entry_price"], pos["peak_price"], pos["last_price"]
        if not entry or not peak or (peak / entry - 1) * 100 < P["trailing_activate_pct"]:
            return
        want = peak * (1 - P["trailing_stop_pct"] / 100)
        if want < b["trig_price"] * (1 + t["trigger_trail_min_move_pct"] / 100):
            return
        if now_ts() - (b["trig_ts"] or 0) < t["trigger_trail_min_interval_sec"] or (last and want >= last * 0.99):
            return
        lk = _tlock(b["mint"])
        if not lk.acquire(blocking=False):
            return
        def go():
            c2 = db()
            try:
                jt.update_stop(b["trig_id"], want)
                _set(c2, b["id"], trig_price=want, trig_ts=now_ts())
                _note(c2, f"trig-move:{b['id']}:{int(want * 1e12)}",
                      f"🛡 Stop moved up on Jupiter for {b['symbol']} to {notify.fmt_price(want)} "
                      f"(-{P['trailing_stop_pct']:g}% from the peak {notify.fmt_price(peak)}, {(want / entry - 1) * 100:+.0f}% vs entry).")
            except Exception as e:
                _set(c2, b["id"], trig_ts=now_ts())  # don't hammer the API; bot's own trailing exit still runs
                log.warning("trigger trail update %s failed: %s", b["symbol"], e)
            finally:
                lk.release(); c2.close()
        threading.Thread(target=go, daemon=True).start()
    except Exception as e:
        log.warning("trigger on_trail failed: %s", e)

# ---------------------------------------------------------------- monitoring
def check(con, cfg, background=True):
    """Called from the position loop. Polls active orders (rate-limited) in a background thread."""
    import live
    live.ensure(con)
    if not con.execute("SELECT 1 FROM live_trades WHERE side='buy' AND trig_state IN ('open','pending','release_failed','partial') LIMIT 1").fetchone():
        return
    if _CHECK["running"] or time.time() - _CHECK["last"] < T(cfg)["trigger_check_interval_sec"]:
        return
    _CHECK.update(running=True, last=time.time())
    if background:
        threading.Thread(target=_check_all, args=(cfg,), daemon=True).start()
    else:
        _check_all(cfg)

def _check_all(cfg):
    import live
    con = db()
    try:
        for b in con.execute("SELECT * FROM live_trades WHERE side='buy' AND trig_state IN ('open','pending','release_failed','partial')").fetchall():
            if b["trig_state"] == "release_failed":
                s = con.execute("SELECT * FROM live_trades WHERE pos_id=? AND side='sell' AND status='failed' ORDER BY id DESC LIMIT 1",
                                (b["pos_id"],)).fetchone()
                if s:   # a bot sell is waiting for the vault: retry it (it withdraws first)
                    con.execute("UPDATE live_trades SET status='pending' WHERE id=?", (s["id"],)); con.commit()
                    live._do_sell(cfg, s["id"], s["pos_id"], s["mint"], s["symbol"], s["frac"], s["reason"])
                continue
            lk = _tlock(b["mint"])
            if not lk.acquire(blocking=False):
                continue
            try:
                o = jt.order(b["trig_id"], b["mint"])
                st = (o or {}).get("orderState")
                con.execute("UPDATE live_trades SET trig_check_ts=? WHERE id=?", (now_ts(), b["id"])); con.commit()
                if st == "filled" or (o and jt.fill_summary(o)[0] >= 0.999):
                    _record_fill(con, cfg, b, o)
                elif st == "open" and b["trig_state"] == "pending":
                    _set(con, b["id"], trig_state="open")
                elif st == "expired":
                    try:
                        jt.cancel_withdraw(b["trig_id"]); _set(con, b["id"], trig_state="expired_withdrawn")
                        _note(con, f"trig-exp:{b['id']}", f"ℹ️ Jupiter stop for {b['symbol']} expired; coins withdrawn to the wallet. Bot's own stop continues.")
                    except Exception as e:
                        _set(con, b["id"], trig_state="release_failed", trig_note=f"expired, withdraw failed: {str(e)[:200]}")
                elif st in ("failed", "cancelled"):
                    _set(con, b["id"], trig_state=st, trig_note=f"order {st} ({(o or {}).get('rawState')})")
                    _note(con, f"trig-gone:{b['id']}", f"⚠️ Jupiter stop for {b['symbol']} is {st} ({(o or {}).get('rawState')}). "
                          "The bot's own stop is watching it.")
            except Exception as e:
                log.warning("trigger check %s: %s", b["symbol"], e)
            finally:
                lk.release()
    finally:
        _CHECK["running"] = False
        con.close()
