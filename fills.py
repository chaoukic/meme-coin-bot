"""Recording only (added Oct 3 2026, Analysis Bot item, Sammy approved): no trading decision reads these tables.

1. position_ticks - one row per price check of every open paper position (new-coin and older-coin strategies; is_real=1
   when the position is also a REAL wallet position), so exit rules can be replayed later.
2. live_fills - the actual fill of every REAL buy/sell, read from the confirmed transaction (getTransaction), with a
   balance-diff fallback from what live_trades already stored. Quote-based slippage when the quote is known.
Rows are never deleted."""
import json, threading, time
from common import log, now_ts, fnum

SOL = "So11111111111111111111111111111111111111112"
TICKS_SQL = """CREATE TABLE IF NOT EXISTS position_ticks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pos_id INTEGER, strategy TEXT, is_real INTEGER, ts REAL, price REAL, source TEXT,
  liq REAL, pnl_pct REAL, peak REAL, verdict TEXT, scoring TEXT)"""
FILLS_SQL = """CREATE TABLE IF NOT EXISTS live_fills(
  trade_id INTEGER PRIMARY KEY, pos_id INTEGER, side TEXT, mint TEXT, symbol TEXT, sig TEXT, trade_ts REAL, block_time REAL,
  source TEXT, status TEXT, token_decimals INTEGER, tokens_in REAL, tokens_out REAL, sol_in REAL, sol_out REAL,
  sol_wallet_delta REAL, fee_sol REAL, other_sol REAL, sol_price REAL, sol_price_source TEXT, usd_in REAL, usd_out REAL,
  fill_price_usd REAL, quoted_price_usd REAL, quote_in_raw TEXT, quote_out_raw TEXT, slippage_allowed_bps INTEGER,
  slippage_pct REAL, paper_price REAL, vs_paper_pct REAL, error TEXT, recorded_at REAL)"""
_ENS = {"t": False, "f": False}

def ensure(con):
    con.execute(TICKS_SQL)
    con.execute("CREATE INDEX IF NOT EXISTS ix_ticks_pos ON position_ticks(pos_id, ts)")
    con.execute(FILLS_SQL)

# ------------------------------------------------------------------ 1. price ticks
def _is_real(con, pos_id):
    try:
        return 1 if con.execute("""SELECT 1 FROM live_trades b WHERE b.pos_id=? AND b.side='buy' AND b.status='ok' AND NOT EXISTS
            (SELECT 1 FROM live_trades s WHERE s.pos_id=b.pos_id AND s.side='sell' AND s.frac>=0.999 AND s.status='ok')""",
                                (pos_id,)).fetchone() else 0
    except Exception:
        return 0

def tick(con, pos, price, source, liq, verdict):
    """Record one price check. Never raises, never changes anything else."""
    try:
        if not _ENS["t"]:
            ensure(con); _ENS["t"] = True
        price = fnum(price)
        entry = pos["entry_price"]
        pnl = (price / entry - 1) * 100 if price and entry else None
        peak = max(pos["peak_price"] or 0, price or 0) if verdict == "ok" else pos["peak_price"]
        strat = pos["strategy"] if "strategy" in pos.keys() and pos["strategy"] else "new"
        sco = pos["scoring"] if "scoring" in pos.keys() and pos["scoring"] else "current"
        con.execute("INSERT INTO position_ticks(pos_id,strategy,is_real,ts,price,source,liq,pnl_pct,peak,verdict,scoring) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pos["id"], strat, _is_real(con, pos["id"]) if (strat == "new" and sco == "current") else 0, now_ts(), price, source,
                     fnum(liq), pnl, peak, verdict, sco))
    except Exception as e:
        try:
            who = pos["id"]
        except Exception:
            who = "?"
        log.warning("price tick not recorded for position %s: %s", who, e)

# ------------------------------------------------------------------ 2. real fills
def parse_tx(tx, wallet, mint):
    """Wallet-side deltas of one confirmed transaction: dict(token_delta_raw, decimals, lamport_delta, wsol_delta_raw, fee)."""
    m = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    lam = 0
    if wallet in keys:
        i = keys.index(wallet)
        lam = m["postBalances"][i] - m["preBalances"][i]
    def bal(lst, mt):
        tot, dec = 0, None
        for b in lst or []:
            if b.get("owner") == wallet and b.get("mint") == mt:
                tot += int(b["uiTokenAmount"]["amount"]); dec = b["uiTokenAmount"]["decimals"]
        return tot, dec
    pre, d1 = bal(m.get("preTokenBalances"), mint)
    post, d2 = bal(m.get("postTokenBalances"), mint)
    wpre, _ = bal(m.get("preTokenBalances"), SOL)
    wpost, _ = bal(m.get("postTokenBalances"), SOL)
    fee_payer = keys[0] if keys else None
    return {"token_delta_raw": post - pre, "decimals": d2 if d2 is not None else d1, "lamport_delta": lam,
            "wsol_delta_raw": wpost - wpre, "fee": m.get("fee", 0) if fee_payer == wallet else 0,
            "block_time": tx.get("blockTime"), "err": m.get("err")}

def compute(row, p, sol_price, quote=None, slip_bps=None, paper_price=None):
    """Fill fields from a live_trades row + parsed tx (p may be None = balance-diff fallback). Pure, testable."""
    side = row["side"]
    out = {"side": side, "sol_price": sol_price, "paper_price": paper_price, "slippage_allowed_bps": slip_bps}
    dec = (p or {}).get("decimals")
    if p:
        out["source"] = "tx"
        out["fee_sol"] = p["fee"] / 1e9
        out["sol_wallet_delta"] = (p["lamport_delta"] + p["wsol_delta_raw"]) / 1e9
        tok = abs(p["token_delta_raw"])
        if side == "sell" and tok == 0 and row["token_raw"]:   # e.g. Jupiter stop: coins left from the vault, not the wallet
            tok = int(row["token_raw"])
    else:
        out["source"] = "balance_diff"
        tok = int(row["token_raw"]) if row["token_raw"] else 0
    if dec is None and quote and quote.get("_decimals") is not None:
        dec = quote["_decimals"]
    out["token_decimals"] = dec
    ui = tok / 10 ** dec if dec is not None else None
    if side == "buy":
        sol_paid = int(quote["inAmount"]) / 1e9 if quote else ((row["sol_lamports"] or 0) / 1e9 or None)
        out.update(sol_out=sol_paid, sol_in=0.0, tokens_in=ui, tokens_out=0.0)
        if p and sol_paid is not None:
            out["other_sol"] = round(-out["sol_wallet_delta"] - sol_paid - out["fee_sol"], 9)  # token-account rent etc.
        usd = sol_paid * sol_price if (sol_paid and sol_price) else row["usd"]
        out.update(usd_out=usd, usd_in=0.0)
        if p is None and row["usd"]:
            out["usd_out"] = row["usd"]
        out["fill_price_usd"] = out["usd_out"] / ui if (ui and out["usd_out"]) else None
        if quote and dec is not None and sol_price:
            qtok = int(quote["outAmount"]) / 10 ** dec
            out["quoted_price_usd"] = int(quote["inAmount"]) / 1e9 * sol_price / qtok if qtok else None
            out["slippage_pct"] = (1 - tok / int(quote["outAmount"])) * 100 if int(quote["outAmount"]) else None
    else:
        got = (out["sol_wallet_delta"] + out["fee_sol"]) if p else None
        if got is None and row["usd"] and sol_price:
            got = row["usd"] / sol_price
        out.update(sol_in=got, sol_out=0.0, tokens_out=ui, tokens_in=0.0)
        out["usd_in"] = got * sol_price if (got is not None and sol_price) else row["usd"]
        out["usd_out"] = 0.0
        out["fill_price_usd"] = out["usd_in"] / ui if (ui and out["usd_in"] is not None) else None
        if quote and dec is not None and sol_price and int(quote.get("inAmount") or 0):
            qsol = int(quote["outAmount"]) / 1e9
            out["quoted_price_usd"] = qsol * sol_price / (int(quote["inAmount"]) / 10 ** dec)
            out["slippage_pct"] = (1 - got * 1e9 / int(quote["outAmount"])) * 100 if got is not None else None
    if quote:
        out["quote_in_raw"], out["quote_out_raw"] = str(quote.get("inAmount")), str(quote.get("outAmount"))
    fp = out.get("fill_price_usd")
    if fp and paper_price:
        out["vs_paper_pct"] = (fp / paper_price - 1) * 100 if side == "buy" else (1 - fp / paper_price) * 100  # + = worse than paper
    return out

def _save(con, row, f, block_time=None, error=None, sol_price_source=None):
    ensure(con)
    cols = ["trade_id", "pos_id", "side", "mint", "symbol", "sig", "trade_ts", "block_time", "source", "status",
            "token_decimals", "tokens_in", "tokens_out", "sol_in", "sol_out", "sol_wallet_delta", "fee_sol", "other_sol",
            "sol_price", "sol_price_source", "usd_in", "usd_out", "fill_price_usd", "quoted_price_usd", "quote_in_raw",
            "quote_out_raw", "slippage_allowed_bps", "slippage_pct", "paper_price", "vs_paper_pct", "error", "recorded_at"]
    v = dict(f)
    v.update(trade_id=row["id"], pos_id=row["pos_id"], side=row["side"], mint=row["mint"], symbol=row["symbol"], sig=row["sig"],
             trade_ts=row["ts"], block_time=block_time, status="error" if error else "ok", error=error,
             sol_price_source=sol_price_source, recorded_at=now_ts())
    con.execute(f"INSERT OR REPLACE INTO live_fills({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [v.get(c) for c in cols])

def get_tx(cfg, sig, tries=6, wait=5):
    import live
    last = None
    for i in range(tries):
        try:
            t = live.rpc(cfg, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                       "commitment": "confirmed"}])
            if t:
                return t
        except Exception as e:
            last = e
        time.sleep(wait * (i + 1))
    if last:
        raise last
    return None

def record(cfg, trade_id, quote=None, slip_bps=None, paper_price=None, sol_price=None, sol_price_source="live"):
    """Read the confirmed tx of a REAL trade and store its fill. Own DB connection. Never raises."""
    from common import db
    import live
    con = db()
    try:
        live.ensure(con)
        row = None
        for _ in range(10):  # the caller may not have committed yet
            row = con.execute("SELECT * FROM live_trades WHERE id=?", (trade_id,)).fetchone()
            if row and row["status"] == "ok" and row["sig"]:
                break
            time.sleep(2)
        if not row or row["status"] != "ok" or not row["sig"]:
            return None
        if sol_price is None:
            try:
                sol_price = live.sol_price()
            except Exception:
                sol_price = None
        p, err, bt = None, None, None
        try:
            tx = get_tx(cfg, row["sig"])
            if tx:
                p = parse_tx(tx, live.pubkey(), row["mint"])
                bt = p["block_time"]
        except Exception as e:
            err = f"tx unreadable, balance-diff fallback: {e}"[:300]
        if p and p.get("decimals") is None:  # e.g. Jupiter stop fill: the coins left from the vault, not our wallet
            d = con.execute("SELECT token_decimals FROM live_fills WHERE mint=? AND token_decimals IS NOT NULL LIMIT 1",
                            (row["mint"],)).fetchone()
            if d:
                p["decimals"] = d[0]
        f = compute(row, p, sol_price, quote, slip_bps, paper_price)
        _save(con, row, f, bt, None, sol_price_source)
        if err:
            con.execute("UPDATE live_fills SET error=? WHERE trade_id=?", (err, trade_id))
        con.commit()
        log.info("real fill recorded: trade %s %s %s fill $%s slippage %s%%", trade_id, row["side"], row["symbol"],
                 f.get("fill_price_usd"), None if f.get("slippage_pct") is None else round(f["slippage_pct"], 2))
        return f
    except Exception as e:
        log.warning("real fill not recorded for trade %s: %s", trade_id, e)
        return None
    finally:
        con.close()

def record_async(cfg, trade_id, **kw):
    """Background (the tx needs a few seconds to be readable); a recording failure can never affect trading."""
    try:
        threading.Thread(target=record, args=(cfg, trade_id), kwargs=kw, daemon=True).start()
    except Exception as e:
        log.warning("fill recorder not started: %s", e)
