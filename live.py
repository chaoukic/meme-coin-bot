"""REAL-MONEY test mode (small, capped). Mirrors paper trades with tiny real swaps via Jupiter.

Safety:
- Off unless [live] enabled = true in config.toml AND no file logs/live_kill exists.
- Hard lifetime budget: total USD spent on real buys never exceeds [live] budget_usd.
- Fixed small size per trade ([live] trade_usd), max [live] max_open real positions.
- Uses only the dedicated test wallet in /home/box/agent-data/memebot_wallet.json.
- Paper trading is unchanged; real trades follow paper decisions (buy on paper buy, sell same fraction on paper sell).
- Runs in background threads so a slow transaction never blocks the paper bot.
"""
import base64, json, os, threading, time
import requests
from common import db, log, now_ts
import notify

WALLET_PATH = "/home/box/agent-data/memebot_wallet.json"
SOL = "So11111111111111111111111111111111111111112"
JUP = "https://lite-api.jup.ag"
KILL = "logs/live_kill"
_MINT_LOCKS = {}
_GLOBAL = threading.Lock()

SQL = """CREATE TABLE IF NOT EXISTS live_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pos_id INTEGER, mint TEXT, symbol TEXT, side TEXT,
  frac REAL, usd REAL, sol_lamports INTEGER, token_raw TEXT, sig TEXT, status TEXT, error TEXT, reason TEXT)"""

_TRIG_COLS = [("trig_id", "TEXT"), ("trig_price", "REAL"), ("trig_state", "TEXT"), ("trig_ts", "REAL"),
              ("trig_raw", "TEXT"), ("trig_check_ts", "REAL"), ("trig_note", "TEXT")]
_ENSURED = {"ok": False}

def ensure(con):
    con.execute(SQL)
    if not _ENSURED["ok"]:
        cols = [r[1] for r in con.execute("PRAGMA table_info(live_trades)")]
        for c, t in _TRIG_COLS:  # Jupiter trigger stop bookkeeping on the BUY row (added Oct 2)
            if c not in cols:
                try:
                    con.execute(f"ALTER TABLE live_trades ADD COLUMN {c} {t}")
                except Exception as e:
                    if "duplicate column" not in str(e):
                        raise
        _ENSURED["ok"] = True

def L(cfg):
    d = {"enabled": False, "trade_usd": 5.0, "budget_usd": 50.0, "max_open": 3, "buy_slippage_bps": 300,
         "sell_slippage_bps": [300, 800, 1500], "min_sol_reserve": 0.02, "rpc": "https://api.mainnet-beta.solana.com",
         "max_priority_lamports": 200000}
    d.update(cfg.get("live", {}))
    return d

def active(cfg):
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    return bool(L(cfg)["enabled"]) and not os.path.exists(os.path.join(base, KILL))

# ---------- chain helpers ----------
def keypair():
    from solders.keypair import Keypair
    with open(WALLET_PATH) as f:
        return Keypair.from_bytes(bytes(json.load(f)["secret"]))

def pubkey():
    with open(WALLET_PATH) as f:
        return json.load(f)["pubkey"]

def rpc(cfg, method, params):
    for i in range(5):
        r = requests.post(L(cfg)["rpc"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        if r.status_code != 429 or i == 4:
            break
        time.sleep(2 + 2 * i)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"RPC {method}: {j['error']}")
    return j["result"]

def sol_balance(cfg):
    return rpc(cfg, "getBalance", [pubkey(), {"commitment": "confirmed"}])["value"] / 1e9

def token_raw(cfg, mint):
    res = rpc(cfg, "getTokenAccountsByOwner", [pubkey(), {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in res["value"])

_PX = {"v": None, "ts": 0.0}

def _px_jup():
    return float(_json("GET", f"{JUP}/price/v3", tries=2, params={"ids": SOL})[SOL]["usdPrice"])

def _px_coinbase():
    return float(_json("GET", "https://api.coinbase.com/v2/prices/SOL-USD/spot", tries=2)["data"]["amount"])

def _px_kraken():
    r = _json("GET", "https://api.kraken.com/0/public/Ticker", tries=2, params={"pair": "SOLUSD"})["result"]
    return float(next(iter(r.values()))["c"][0])

def sol_price():
    """SOL/USD. Cached 30s; tries Jupiter, then Coinbase, then Kraken; falls back to a price under 10 min old."""
    if _PX["v"] and time.time() - _PX["ts"] < 30:
        return _PX["v"]
    errs = []
    for f in (_px_jup, _px_coinbase, _px_kraken):
        try:
            v = f()
            if 1 < v < 100000:
                _PX.update(v=v, ts=time.time())
                return v
            errs.append(f"{f.__name__}: odd value {v}")
        except Exception as e:
            errs.append(f"{f.__name__}: {e}")
            log.warning("SOL price source %s failed: %s", f.__name__, e)
    if _PX["v"] and time.time() - _PX["ts"] < 600:
        return _PX["v"]
    raise RuntimeError("SOL price unavailable from all sources: " + "; ".join(errs)[:300])

def jup_key():
    """User's own Jupiter API key (never logged). From env, else the box secrets file."""
    k = os.environ.get("JUPITER_API_KEY")
    if not k:
        try:
            with open("/home/box/sand-data/box-secrets.json") as f:
                k = json.load(f).get("card", {}).get("JUPITER_API_KEY")
        except Exception:
            k = None
    return (k or "").strip() or None

def _json(method, url, tries=4, **kw):
    """HTTP call returning JSON, retrying on rate limits / non-JSON replies."""
    last = None
    for i in range(tries):
        try:
            r = requests.request(method, url, timeout=20, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                raise RuntimeError(f"HTTP {r.status_code}" + (f" retry-after {ra}" if ra else ""))
            return r.json()
        except Exception as e:
            last = e
            if i == tries - 1:
                break
            wait = min(3 + 4 * i, 20)
            try:
                wait = max(wait, min(float(str(e).split("retry-after ")[1]), 30))
            except Exception:
                pass
            log.info("retrying %s in %.0fs (%s)", url.split("?")[0], wait, e)
            time.sleep(wait)
    raise RuntimeError(f"{url.split('?')[0]} unavailable: {last}")

def swap(cfg, in_mint, out_mint, amount_raw, slippage_bps):
    """Quote + build + sign + send + confirm. Returns (sig, quote)."""
    from solders.transaction import VersionedTransaction
    body_extra = {"userPublicKey": pubkey(), "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                  "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {"maxLamports": int(L(cfg)["max_priority_lamports"]),
                                                                                 "priorityLevel": "high"}}}
    params = {"inputMint": in_mint, "outputMint": out_mint, "amount": int(amount_raw), "slippageBps": int(slippage_bps)}
    # Jupiter's free lite-api is often rate-limited from this machine; fall back to the free public
    # Jupiter endpoint (same routes, same format). Quote and swap always come from the same provider.
    providers = []
    hk = {"x-api-key": jup_key()} if jup_key() else None
    if hk:
        providers.append(("jup-key", "https://api.jup.ag/swap/v1/quote", "https://api.jup.ag/swap/v1/swap", hk))
    providers += [("lite-api", f"{JUP}/swap/v1/quote", f"{JUP}/swap/v1/swap", None),
                 ("public.jupiterapi", "https://public.jupiterapi.com/quote", "https://public.jupiterapi.com/swap", None)]
    errs, s, q = [], None, None
    for rnd in range(3):
        for name, qurl, surl, hdr in providers:
            try:
                q = _json("GET", qurl, tries=2, params=params, headers=hdr)
                if "outAmount" not in q:
                    raise RuntimeError(f"no route: {str(q)[:200]}")
                s = _json("POST", surl, tries=3, json=dict(body_extra, quoteResponse=q), headers=hdr)
                if "swapTransaction" not in s:
                    raise RuntimeError(f"swap build failed: {str(s)[:200]}")
                if name != providers[0][0]:
                    log.info("swap built via backup provider %s", name)
                break
            except Exception as e:
                errs.append(f"{name}: {e}")
                log.warning("swap provider %s failed: %s", name, e)
                s = None
        if s:
            break
        time.sleep(5 + 5 * rnd)
    if not s:
        raise RuntimeError("all swap providers failed: " + "; ".join(errs[-2:])[:300])
    tx = VersionedTransaction.from_bytes(base64.b64decode(s["swapTransaction"]))
    signed = VersionedTransaction(tx.message, [keypair()])
    sig = rpc(cfg, "sendTransaction", [base64.b64encode(bytes(signed)).decode(),
                                        {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
    deadline = time.time() + 75
    while time.time() < deadline:
        time.sleep(2)
        st = rpc(cfg, "getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])["value"][0]
        if st:
            if st.get("err"):
                raise RuntimeError(f"transaction failed on-chain: {st['err']} (sig {sig})")
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig, q
    raise RuntimeError(f"not confirmed in time (sig {sig})")

# ---------- budget ----------
def spent_usd(con):
    ensure(con)
    return con.execute("SELECT COALESCE(SUM(usd),0) FROM live_trades WHERE side='buy' AND status IN ('ok','pending')").fetchone()[0]

def open_live(con):
    ensure(con)
    return con.execute("""SELECT COUNT(*) FROM live_trades b WHERE side='buy' AND status='ok' AND NOT EXISTS
        (SELECT 1 FROM live_trades s WHERE s.pos_id=b.pos_id AND s.side='sell' AND s.frac>=1 AND s.status='ok')""").fetchone()[0]

def summary(con, cfg):
    ensure(con)
    lc = L(cfg)
    got = con.execute("SELECT COALESCE(SUM(usd),0) FROM live_trades WHERE side='sell' AND status='ok'").fetchone()[0]
    return {"active": active(cfg), "budget_usd": lc["budget_usd"], "spent_usd": spent_usd(con), "returned_usd": got,
            "open": open_live(con), "trade_usd": lc["trade_usd"], "wallet": pubkey()}

def _lock(mint):
    with _GLOBAL:
        return _MINT_LOCKS.setdefault(mint, threading.Lock())

# ---------- real wallet's own daily loss limit ----------
def wallet_total(con, cfg):
    """Real wallet value now (SOL + estimated coins), from the ~60s cache. None if unknown."""
    try:
        return dashboard(con, cfg).get("total_est_usd")
    except Exception as e:
        log.warning("live wallet_total failed: %s", e)
        return None

def day_check(con, cfg):
    """(ok, pct, msg). The REAL wallet has its own daily stop: no new real buys once its value is
    [live] daily_stop_pct % below its value at the start of the day (Toronto). Independent of the paper caps."""
    from common import get_state, set_state, toronto_date
    lc = L(cfg)
    stop = float(lc.get("daily_stop_pct", 10))
    today = toronto_date()
    d = get_state(con, "live_day") or {}
    tot = wallet_total(con, cfg)
    if d.get("date") != today:
        if tot is None:
            return False, None, "real wallet value unknown (RPC), waiting"
        d = {"date": today, "start_usd": tot, "stopped": False}
        set_state(con, "live_day", d); con.commit()
    if d.get("stopped"):
        return False, d.get("pct"), f"real wallet daily stop (-{stop:g}%) hit today"
    if tot is None:
        return False, None, "real wallet value unknown (RPC), waiting"
    pct = (tot / d["start_usd"] - 1) * 100 if d.get("start_usd") else 0.0
    if pct <= -stop:
        d.update(stopped=True, pct=pct); set_state(con, "live_day", d); con.commit()
        _note(con, f"live-daystop:{today}", f"⛔ REAL wallet daily stop: down {pct:.1f}% today (limit -{stop:g}%). "
              "No new real buys until midnight Toronto. Real sells still run.")
        return False, pct, f"real wallet daily stop (-{stop:g}%) hit today"
    return True, pct, "ok"

def can_buy(con, cfg):
    """True if a real buy would actually go through right now (on, budget left, slot free, own daily stop not hit)."""
    try:
        if not active(cfg):
            return False
        ensure(con)
        lc = L(cfg)
        if float(lc["budget_usd"]) > 0 and spent_usd(con) + float(lc["trade_usd"]) > float(lc["budget_usd"]) + 1e-9:
            return False
        if open_live(con) >= int(lc["max_open"]):
            return False
        return day_check(con, cfg)[0]
    except Exception as e:
        log.warning("live can_buy failed: %s", e)
        return False

# ---------- hooks called by trader.py ----------
PAPER_ONLY_STRATEGIES = {"established"}   # never copied to the real wallet, whatever the config says (Oct 2)

def paper_only_strategy(con, pos_id):
    """The position's strategy if it must never trade real money, else None. Unknown/missing position = blocked."""
    try:
        r = con.execute("SELECT COALESCE(strategy,'new') s FROM positions WHERE id=?", (pos_id,)).fetchone()
    except Exception:
        return "unknown"
    if r is None:
        return "unknown"
    return r["s"] if r["s"] != "new" else None

def not_current_scoring(con, pos_id):
    """Side-by-side scoring test (Oct 3): real money ONLY for scoring='current' positions. Returns a reason to refuse,
    else None. Any doubt (unknown position, missing column, NULL, error) = refuse."""
    try:
        r = con.execute("SELECT scoring FROM positions WHERE id=?", (pos_id,)).fetchone()
    except Exception as e:
        return f"scoring unknown ({str(e)[:60]})"
    if r is None:
        return "scoring unknown (no position)"
    return None if r["scoring"] == "current" else f"scoring={r['scoring']!r} (paper-only scoring test)"

def on_buy(con, cfg, pos_id, mint, symbol):
    """Called right after a PAPER buy. Returns True if a real buy was started, else a short reason string. Never raises."""
    try:
        ps = paper_only_strategy(con, pos_id)
        if ps:
            log.info("LIVE BUY %s refused: position #%s is strategy '%s' (paper only)", symbol, pos_id, ps)
            return f"paper-only strategy ({ps})"
        ns = not_current_scoring(con, pos_id)
        if ns:
            log.info("LIVE BUY %s refused: position #%s %s", symbol, pos_id, ns)
            return f"paper-only: {ns}"
        if not active(cfg):
            return "real-money mode is off"
        ensure(con)
        lc = L(cfg)
        usd = float(lc["trade_usd"])
        if float(lc["budget_usd"]) > 0 and spent_usd(con) + usd > float(lc["budget_usd"]) + 1e-9:
            return f"the ${lc['budget_usd']:.0f} real budget is used up"
        if open_live(con) >= int(lc["max_open"]):
            return f"already {lc['max_open']} real positions open"
        ok, pct, msg = day_check(con, cfg)
        if not ok:
            log.info("LIVE BUY %s skipped: %s", symbol, msg)
            return msg
        con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,'buy',1,?,'pending')",
                    (now_ts(), pos_id, mint, symbol, usd))
        tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()
        threading.Thread(target=_do_buy, args=(cfg, tid, pos_id, mint, symbol, usd), daemon=True).start()
        return True
    except Exception as e:
        log.exception("live on_buy failed: %s", e)
        return "error starting the real buy"

def on_sell(con, cfg, pos_id, mint, symbol, frac, reason):
    """Called right after a PAPER sell of `frac` of what was left (1 = everything). Runs even if live was
    switched off since, so real positions are never stranded. Never raises."""
    try:
        if paper_only_strategy(con, pos_id) in PAPER_ONLY_STRATEGIES:
            return
        sr = con.execute("SELECT scoring FROM positions WHERE id=?", (pos_id,)).fetchone()
        if sr is not None and sr["scoring"] != "current":  # new-scoring paper account: never real (never bought real either)
            return
        ensure(con)
        b = con.execute("SELECT id FROM live_trades WHERE pos_id=? AND side='buy' AND status IN ('ok','pending')", (pos_id,)).fetchone()
        if not b:
            return
        if con.execute("SELECT 1 FROM live_trades WHERE pos_id=? AND side='sell' AND frac>=0.999 AND status='ok'", (pos_id,)).fetchone():
            log.info("LIVE SELL %s skipped: real position already closed (e.g. Jupiter stop filled)", symbol)
            return
        con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,status,reason) VALUES(?,?,?,?,'sell',?,'pending',?)",
                    (now_ts(), pos_id, mint, symbol, frac, reason))
        tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.commit()
        threading.Thread(target=_do_sell, args=(cfg, tid, pos_id, mint, symbol, frac, reason), daemon=True).start()
    except Exception as e:
        log.exception("live on_sell failed: %s", e)

def _note(con, key, text):
    notify.enqueue(con, key, text)
    con.commit()

def _do_buy(cfg, tid, pos_id, mint, symbol, usd):
    con = db()
    with _lock(mint):
        try:
            ps = paper_only_strategy(con, pos_id)
            if ps:  # second, independent check right before any money moves
                raise RuntimeError(f"refused: position #{pos_id} is strategy '{ps}' (paper only)")
            ns = not_current_scoring(con, pos_id)
            if ns:  # scoring test: second, independent check right before any money moves
                raise RuntimeError(f"refused: position #{pos_id} {ns}")
            lc = L(cfg)
            px = sol_price()
            lamports = int(usd / px * 1e9)
            bal = sol_balance(cfg)
            if bal - lamports / 1e9 < float(lc["min_sol_reserve"]):
                raise RuntimeError(f"wallet too low: {bal:.4f} SOL, need {lamports/1e9:.4f} + {lc['min_sol_reserve']} reserve for fees")
            slips = lc["buy_slippage_bps"]
            slips = slips if isinstance(slips, list) else [slips]
            for i, slip in enumerate(slips):
                try:
                    sig, q = swap(cfg, SOL, mint, lamports, slip)
                    break
                except Exception as e:
                    # A failed simulation (e.g. price moved past slippage, Jupiter error 6001/0x1771) means the
                    # transaction was never sent, so no money left the wallet; retrying with more slippage is safe.
                    msg = str(e)
                    retryable = ("simulation failed" in msg.lower() or "6001" in msg or "0x1771" in msg
                                 or "all swap providers failed" in msg)
                    if not retryable or i == len(slips) - 1:
                        raise
                    log.warning("LIVE BUY %s at %sbps failed (%s), retrying with %sbps", symbol, slip, msg[:120], slips[i + 1])
                    time.sleep(1.5)
            raw = token_raw(cfg, mint)
            con.execute("UPDATE live_trades SET status='ok', sig=?, sol_lamports=?, token_raw=? WHERE id=?", (sig, lamports, str(raw), tid))
            log.info("LIVE BUY %s $%.2f sig %s", symbol, usd, sig)
            _note(con, f"live-buy:{tid}", "🏷 OLD scoring trade\n" + f"💵 REAL BUY {symbol}: ${usd:.2f} ({lamports/1e9:.4f} SOL)\nhttps://solscan.io/tx/{sig}")
            try:  # recording only (fills.py): actual fill from the confirmed tx, in the background
                import fills
                pp = con.execute("SELECT entry_price FROM positions WHERE id=?", (pos_id,)).fetchone()
                fills.record_async(cfg, tid, quote=q, slip_bps=slip, paper_price=pp["entry_price"] if pp else None)
            except Exception as e:
                log.warning("fill recorder (buy) not started: %s", e)
            try:
                import trigstop
                trigstop.after_buy(con, cfg, tid, pos_id, mint, symbol, raw, usd)
            except Exception as e:
                log.exception("trigger stop after buy failed: %s", e)
        except Exception as e:
            con.execute("UPDATE live_trades SET status='failed', error=? WHERE id=?", (str(e)[:500], tid))
            log.warning("LIVE BUY %s failed: %s", symbol, e)
            _note(con, f"live-buyfail:{tid}", f"⚠️ REAL buy of {symbol} failed, no money spent: {str(e)[:200]}")
        finally:
            con.commit(); con.close()

def _do_sell(cfg, tid, pos_id, mint, symbol, frac, reason):
    con = db()
    with _lock(mint):  # waits for a still-running buy of the same coin
        try:
            lc = L(cfg)
            b = con.execute("SELECT status FROM live_trades WHERE pos_id=? AND side='buy' ORDER BY id DESC LIMIT 1", (pos_id,)).fetchone()
            if not b or b["status"] != "ok":
                con.execute("UPDATE live_trades SET status='skipped', error='real buy did not go through' WHERE id=?", (tid,))
                return
            import trigstop
            rel = trigstop.release_before_sell(con, cfg, pos_id, mint, symbol)  # tokens in the Jupiter vault come back first
            if rel == "filled":
                con.execute("UPDATE live_trades SET status='skipped', error='Jupiter stop already sold the coins' WHERE id=?", (tid,))
                return
            try:  # paper price at the moment of the sell decision (recording only, for real-vs-paper slippage)
                _pp = con.execute("SELECT last_price FROM positions WHERE id=?", (pos_id,)).fetchone()
                paper_px = _pp["last_price"] if _pp else None
            except Exception:
                paper_px = None
            raw = token_raw(cfg, mint)
            amt = raw if frac >= 0.999 else int(raw * frac)
            if amt <= 0:
                raise RuntimeError("no tokens left in wallet for this coin")
            before = sol_balance(cfg)
            last = None
            for n, slip in enumerate(lc["sell_slippage_bps"]):
                if n:
                    time.sleep(8)  # let the chain/RPC catch up before retrying
                try:
                    sig, q = swap(cfg, mint, SOL, amt, slip)
                    break
                except Exception as e:
                    last = e
                    log.warning("LIVE SELL %s at %dbps failed: %s", symbol, slip, e)
            else:
                raise last
            after = sol_balance(cfg)
            got = max(after - before, 0) * sol_price()
            con.execute("UPDATE live_trades SET status='ok', sig=?, usd=?, token_raw=? WHERE id=?", (sig, got, str(amt), tid))
            log.info("LIVE SELL %s %.0f%% got $%.2f sig %s", symbol, frac * 100, got, sig)
            _note(con, f"live-sell:{tid}", "🏷 OLD scoring trade\n" + f"💵 REAL SELL {symbol} ({frac*100:.0f}%): got back ${got:.2f}. Reason: {reason}\nhttps://solscan.io/tx/{sig}")
            try:  # recording only (fills.py)
                import fills
                fills.record_async(cfg, tid, quote=q, slip_bps=slip, paper_price=paper_px)
            except Exception as e:
                log.warning("fill recorder (sell) not started: %s", e)
            if rel == "released" and frac < 0.999:
                trigstop.replace_after_partial(con, cfg, pos_id, mint, symbol)
        except Exception as e:
            con.execute("UPDATE live_trades SET status='failed', error=? WHERE id=?", (str(e)[:500], tid))
            log.warning("LIVE SELL %s failed: %s", symbol, e)
            vault = "trigstop.VaultBusy" in repr(type(e)) or type(e).__name__ == "VaultBusy"
            _note(con, f"live-sellfail:{tid}", (f"🚨 REAL sell of {symbol} waiting: the coins are still in the Jupiter stop-order vault "
                                                f"and could not be withdrawn yet ({str(e)[:150]}). The bot keeps retrying every minute."
                                                if vault else f"🚨 REAL sell of {symbol} FAILED, the coins are still in the test wallet: {str(e)[:200]}"))
        finally:
            con.commit(); con.close()

# ---------- dashboard ----------
def dashboard(con, cfg):
    """Block for data.json: real wallet balance (cached ~60s), budget, open real positions (estimated), recent real trades."""
    from common import get_state, set_state
    ensure(con)
    lc = L(cfg)
    w = get_state(con, "live_wallet") or {}
    if now_ts() - (w.get("ts") or 0) > 60:
        try:
            bal = sol_balance(cfg); px = sol_price()
            w = {"ts": now_ts(), "sol": bal, "sol_price": px, "usd": bal * px}
            set_state(con, "live_wallet", w); con.commit()
        except Exception as e:
            log.warning("live wallet refresh failed: %s", e)
    opens = []
    for b in con.execute("""SELECT b.*, p.entry_price, p.last_price, p.status AS paper_status FROM live_trades b
                            LEFT JOIN positions p ON p.id=b.pos_id WHERE b.side='buy' AND b.status='ok'""").fetchall():
        sells = con.execute("SELECT frac FROM live_trades WHERE pos_id=? AND side='sell' AND status='ok'", (b["pos_id"],)).fetchall()
        left = 1.0
        for s in sells:
            left = 0.0 if s["frac"] >= 0.999 else left * (1 - s["frac"])
        if left <= 0:
            continue
        est = b["usd"] * left * ((b["last_price"] or 0) / b["entry_price"]) if b["entry_price"] else None
        opens.append({"pos_id": b["pos_id"], "symbol": b["symbol"], "mint": b["mint"], "cost_usd": b["usd"], "left_frac": left,
                      "est_value_usd": est, "opened_at": b["ts"], "sig": b["sig"]})
    import fills
    fills.ensure(con)  # actual fills (recording only, Oct 3): a few columns per trade, cheap
    trades = [dict(r) for r in con.execute("""SELECT t.id,t.ts,t.pos_id,t.symbol,t.mint,t.side,t.frac,t.usd,t.sig,t.status,t.error,t.reason,
                                              f.fill_price_usd, f.quoted_price_usd, f.slippage_pct, f.paper_price, f.vs_paper_pct,
                                              f.tokens_in, f.tokens_out, f.sol_in, f.sol_out, f.usd_in, f.usd_out, f.fee_sol,
                                              f.source AS fill_source
                                              FROM live_trades t LEFT JOIN live_fills f ON f.trade_id=t.id ORDER BY t.id DESC LIMIT 50""")]
    got = con.execute("SELECT COALESCE(SUM(usd),0) FROM live_trades WHERE side='sell' AND status='ok'").fetchone()[0]
    spent = spent_usd(con)
    hold = sum(o["est_value_usd"] or 0 for o in opens)
    # P&L per real position (one successful buy, then its sells)
    open_ids = {o["pos_id"] for o in opens}
    closed, pos_pnl = [], {}
    for b in con.execute("SELECT * FROM live_trades WHERE side='buy' AND status='ok' ORDER BY id").fetchall():
        sl = con.execute("SELECT ts,usd,reason FROM live_trades WHERE pos_id=? AND side='sell' AND status='ok' ORDER BY id",
                         (b["pos_id"],)).fetchall()
        back = sum(x["usd"] or 0 for x in sl)
        est_left = next((o["est_value_usd"] or 0 for o in opens if o["pos_id"] == b["pos_id"]), 0)
        pnl = back + est_left - b["usd"]
        pos_pnl[b["pos_id"]] = {"cost_usd": b["usd"], "returned_usd": back, "pnl_usd": pnl, "pnl_pct": pnl / b["usd"] * 100 if b["usd"] else None,
                                "is_open": b["pos_id"] in open_ids}
        if b["pos_id"] not in open_ids:
            closed.append({"pos_id": b["pos_id"], "symbol": b["symbol"], "mint": b["mint"], "opened_at": b["ts"],
                           "closed_at": sl[-1]["ts"] if sl else None, "cost_usd": b["usd"], "returned_usd": back,
                           "pnl_usd": pnl, "pnl_pct": pos_pnl[b["pos_id"]]["pnl_pct"],
                           "exit_reason": sl[-1]["reason"] if sl else None, "buy_sig": b["sig"]})
    closed.reverse()
    for o in opens:
        pp = pos_pnl.get(o["pos_id"], {})
        o["returned_usd"] = pp.get("returned_usd", 0); o["pnl_usd"] = pp.get("pnl_usd"); o["pnl_pct"] = pp.get("pnl_pct")
    for t in trades:
        pp = pos_pnl.get(t["pos_id"])
        t["position_pnl_usd"] = pp["pnl_usd"] if pp and t["status"] == "ok" else None
        t["position_pnl_pct"] = pp["pnl_pct"] if pp and t["status"] == "ok" else None
        t["position_open"] = pp["is_open"] if pp else None
    realized = sum(c["pnl_usd"] for c in closed)
    unreal = sum(o["pnl_usd"] or 0 for o in opens)
    funded = get_state(con, "live_funded_usd")
    return {"active": active(cfg), "wallet": pubkey(), "wallet_url": f"https://solscan.io/account/{pubkey()}",
            "wallet_sol": w.get("sol"), "wallet_usd": w.get("usd"), "sol_price": w.get("sol_price"), "wallet_ts": w.get("ts"),
            "coins_est_usd": hold, "total_est_usd": (w.get("usd") or 0) + hold if w.get("usd") is not None else None,
            "budget_usd": lc["budget_usd"], "spent_usd": spent, "returned_usd": got, "budget_left_usd": (max(lc["budget_usd"] - spent, 0) if lc["budget_usd"] > 0 else None),
            "budget_unlimited": not lc["budget_usd"] > 0,
            "trade_usd": lc["trade_usd"], "max_open": lc["max_open"], "open": opens, "trades": trades,
            "funded_usd": funded, "day": get_state(con, "live_day"),
            "closed": closed, "realized_pnl_usd": realized, "unrealized_pnl_usd": unreal,
            "wins": sum(1 for c in closed if c["pnl_usd"] > 0), "losses": sum(1 for c in closed if c["pnl_usd"] <= 0),
            "total_pnl_usd": ((w.get("usd") or 0) + hold - funded) if (funded and w.get("usd") is not None) else None,
            "total_pnl_pct": (((w.get("usd") or 0) + hold - funded) / funded * 100) if (funded and w.get("usd") is not None) else None,
            "daily_stop_pct": float(lc.get("daily_stop_pct", 10))}
