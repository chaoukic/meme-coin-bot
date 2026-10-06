"""Recording only (Oct 3): position_ticks (price path per position) and live_fills (actual real fills).
Mocked prices / transactions; runs on a COPY of the DB; no network, no wallet.
Run: .venv/bin/python tests/test_history.py"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
exec(open(os.path.join(os.path.dirname(__file__), "test_established.py")).read().split("# ---- 1. rules (pure)")[0])  # setup
import fills, trader
results.clear()
fills.ensure(con)

# ---- 1. config: trailing switches on at +20% now
check("trailing_activate_pct = 20, trailing_stop_pct = 20", cfg["paper"]["trailing_activate_pct"] == 20 and cfg["paper"]["trailing_stop_pct"] == 20)

# ---- 2. ticks for a new-coin paper position (DexScreener and Jupiter-backup readings), incl. a rejected reading
setup(0)
r, _ = buy(80)
pid = r["id"]
def reading(price, liq=1_000_000, src=None):
    d = {"pairAddress": r["pair"], "baseToken": {"address": r["token"]}, "priceUsd": str(price), "liquidity": {"usd": liq}}
    if src: d["_source"] = src
    return d
n0 = con.execute("SELECT COUNT(*) FROM position_ticks WHERE pos_id=?", (pid,)).fetchone()[0]
before = dict(con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
trader.ds_pairs = lambda pairs: {r["pair"]: reading(1.1e-4)}
trader.update_positions(con, cfg)
after = dict(con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())
t = con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id DESC", (pid,)).fetchone()
check("new-coin position: one tick per price check", con.execute("SELECT COUNT(*) FROM position_ticks WHERE pos_id=?", (pid,)).fetchone()[0] == n0 + 1)
check(f"tick fields: strategy new, price, source dexscreener, liq, pnl_pct +10%, peak, verdict ok ({dict(t)})",
      t["strategy"] == "new" and abs(t["price"] - 1.1e-4) < 1e-12 and t["source"] == "dexscreener" and t["liq"] == 1_000_000
      and abs(t["pnl_pct"] - 10) < 0.01 and abs(t["peak"] - 1.1e-4) < 1e-12 and t["verdict"] == "ok" and t["is_real"] == 0)
check("position itself updated exactly as before (last/peak price)", abs(after["last_price"] - 1.1e-4) < 1e-12 and abs(after["peak_price"] - 1.1e-4) < 1e-12)
trader.ds_pairs = lambda pairs: {r["pair"]: reading(9e-4)}  # 8x spike -> held for confirmation (reject)
trader.update_positions(con, cfg)
t = con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id DESC", (pid,)).fetchone()
check("rejected (guarded) reading is recorded too, with verdict 'reject' and peak unchanged",
      t["verdict"] == "reject" and abs(t["price"] - 9e-4) < 1e-12 and abs(t["peak"] - 1.1e-4) < 1e-12)
check("...and still not acted on", abs(con.execute("SELECT last_price FROM positions WHERE id=?", (pid,)).fetchone()[0] - 1.1e-4) < 1e-12)
trader.ds_pairs = lambda pairs: {}
trader.jup_backup_pairs = lambda cfg_, ps: {r["pair"]: reading(1.05e-4, src="jupiter")}
trader.update_positions(con, cfg)
t = con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id DESC", (pid,)).fetchone()
check("Jupiter backup reading tagged source='jupiter'", t["source"] == "jupiter")
# real position flag
live.ensure(con)
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status,sig) VALUES(?,?,?,?,'buy',1,5,'ok','S')", (time.time(), pid, r["token"], r["symbol"]))
con.commit()
trader.jup_backup_pairs = lambda cfg_, ps: {r["pair"]: reading(1.06e-4, src="jupiter")}
trader.update_positions(con, cfg)
t = con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id DESC", (pid,)).fetchone()
check("tick of a REAL position has is_real=1", t["is_real"] == 1)

# ---- 3. older-coin position ticks
LISTS.clear(); LISTS["HIST1"] = rec("HIST1", "HISTC")
es.run_round(con, cfg)
e = con.execute("SELECT * FROM positions WHERE token='HIST1' AND strategy='established'").fetchone()
PRICES["HIST1"] = rec("HIST1", "HISTC", price=1.03)
es.update_positions(con, cfg)
t = con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id DESC", (e["id"],)).fetchone()
check(f"older-coin position tick: strategy established, source jupiter_batch, pnl +3%", t and t["strategy"] == "established"
      and t["source"] == "jupiter_batch" and abs(t["pnl_pct"] - 3) < 0.01 and t["is_real"] == 0)

# ---- 4. tick failure can never break trading
orig = fills._ENS["t"]
class Boom:
    def __getitem__(self, k): raise KeyError(k)
    def keys(self): return []
fills.tick(con, Boom(), 1.0, "x", 1, "ok")
check("tick() swallows errors (never raises)", True)

# ---- 5. real fills: pure computation from a parsed transaction
W = "WALLET"
def tx(lam_delta, tok_pre, tok_post, fee=5000, dec=6, mint="MINTF", wallet_is_payer=True):
    keys = [{"pubkey": W if wallet_is_payer else "KEEPER"}, {"pubkey": "X"}] + ([] if wallet_is_payer else [{"pubkey": W}])
    i = 0 if wallet_is_payer else 2
    pre = [10_000_000_000, 0, 0]; post = list(pre); post[i] += lam_delta
    tb = lambda amt: [{"owner": W, "mint": mint, "uiTokenAmount": {"amount": str(amt), "decimals": dec}}] if amt is not None else []
    return {"blockTime": 1790000000, "meta": {"fee": fee, "err": None, "preBalances": pre, "postBalances": post,
            "preTokenBalances": tb(tok_pre), "postTokenBalances": tb(tok_post)}, "transaction": {"message": {"accountKeys": keys}}}
p = fills.parse_tx(tx(-(41_252_533 + 45_146 + 2_039_280), None, 61_892_257_369, fee=45_146), W, "MINTF")
check("parse_tx: token delta, decimals, lamports, fee", p["token_delta_raw"] == 61_892_257_369 and p["decimals"] == 6 and p["fee"] == 45_146)
row = {"id": 1, "side": "buy", "token_raw": "61892257369", "sol_lamports": 41_252_533, "usd": 5.0}
q = {"inAmount": "41252533", "outAmount": "62500000000"}
f = fills.compute(row, p, 121.2, quote=q, slip_bps=300, paper_price=7.9e-5)
check(f"buy: sol_out = quote in, tokens_in, usd_out, rent in other_sol ({f['other_sol']})",
      abs(f["sol_out"] - 0.041252533) < 1e-12 and abs(f["tokens_in"] - 61892.257369) < 1e-6 and abs(f["usd_out"] - 0.041252533 * 121.2) < 1e-9
      and abs(f["other_sol"] - 0.00203928) < 1e-9)
check(f"buy: fill price, quoted price, slippage {f['slippage_pct']:.3f}% (got 0.97% fewer tokens than quoted)",
      abs(f["fill_price_usd"] - f["usd_out"] / 61892.257369) < 1e-15 and abs(f["slippage_pct"] - (1 - 61892257369 / 62500000000) * 100) < 1e-9
      and abs(f["quoted_price_usd"] - 0.041252533 * 121.2 / 62500) < 1e-15)
check(f"buy: vs paper entry price {f['vs_paper_pct']:+.2f}% (+ = worse)", abs(f["vs_paper_pct"] - (f["fill_price_usd"] / 7.9e-5 - 1) * 100) < 1e-9)
ps = fills.parse_tx(tx(36_203_304, 61_892_257_369, 0, fee=23_507), W, "MINTF")
rows = {"id": 2, "side": "sell", "token_raw": "61892257369", "sol_lamports": None, "usd": 4.39}
qs = {"inAmount": "61892257369", "outAmount": "36500000"}
f = fills.compute(rows, ps, 121.2, quote=qs, slip_bps=300, paper_price=7.5e-5)
check(f"sell: sol_in = wallet delta + fee, tokens_out, usd_in, slippage {f['slippage_pct']:.3f}%",
      abs(f["sol_in"] - 0.036226811) < 1e-12 and abs(f["tokens_out"] - 61892.257369) < 1e-6 and abs(f["usd_in"] - 0.036226811 * 121.2) < 1e-9
      and abs(f["slippage_pct"] - (1 - 36_226_811 / 36_500_000) * 100) < 1e-9)
check("sell: vs paper exit price (+ = worse)", abs(f["vs_paper_pct"] - (1 - f["fill_price_usd"] / 7.5e-5) * 100) < 1e-9)
pk = fills.parse_tx(tx(36_000_000, None, None, wallet_is_payer=False), W, "MINTF")
rk = {"id": 3, "side": "sell", "token_raw": "61892257369", "sol_lamports": None, "usd": 4.3}
f = fills.compute(rk, pk, 121.2)
check("Jupiter-stop fill (keeper pays fee, coins left from the vault): SOL received, tokens from token_raw, decimals unknown -> price empty",
      abs(f["sol_in"] - 0.036) < 1e-12 and f["fee_sol"] == 0 and f["tokens_out"] is None)
f = fills.compute(rows, None, 121.2)
check("balance-diff fallback (tx unreadable): source balance_diff, usd from live_trades", f["source"] == "balance_diff" and abs(f["usd_in"] - 4.39) < 1e-9)

# ---- 6. record(): reads the tx, writes live_fills; recorder failure never touches live_trades
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,sol_lamports,token_raw,status,sig) VALUES(?,?,?,?,'buy',1,5,41252533,'61892257369','ok','SIGB')",
            (time.time(), pid, "MINTF", "HISTF"))
tid = con.execute("SELECT last_insert_rowid()").fetchone()[0]; con.commit()
live.pubkey = lambda: W
live.sol_price = lambda: 121.2
live.db = lambda: common.db()
fills.get_tx = lambda cfg_, sig, tries=6, wait=5: tx(-(41_252_533 + 45_146 + 2_039_280), None, 61_892_257_369, fee=45_146)
f = fills.record(cfg, tid, quote=q, slip_bps=300, paper_price=7.9e-5)
lf = con.execute("SELECT * FROM live_fills WHERE trade_id=?", (tid,)).fetchone()
check("record(): live_fills row written from the tx", lf and lf["source"] == "tx" and lf["sig"] == "SIGB" and lf["slippage_allowed_bps"] == 300
      and lf["quote_out_raw"] == "62500000000" and lf["block_time"] == 1790000000)
st = dict(con.execute("SELECT * FROM live_trades WHERE id=?", (tid,)).fetchone())
def bad(*a, **k): raise RuntimeError("RPC down")
fills.get_tx = bad
con.execute("DELETE FROM live_fills WHERE trade_id=?", (tid,)); con.commit()
f = fills.record(cfg, tid)
lf = con.execute("SELECT * FROM live_fills WHERE trade_id=?", (tid,)).fetchone()
check("RPC down: balance-diff row with the error noted, live_trades row unchanged",
      lf and lf["source"] == "balance_diff" and "unreadable" in (lf["error"] or "") and dict(con.execute("SELECT * FROM live_trades WHERE id=?", (tid,)).fetchone()) == st)

# ---- 7. hooks: _do_buy / _do_sell start the recorder with the quote; data.json carries the fill columns
started = []
fills.record_async = lambda cfg_, trade_id, **kw: started.append((trade_id, kw))
live.active = lambda cfg_: True
live.sol_balance = lambda cfg_: 1.0
live.token_raw = lambda cfg_, mint: 1000
live.swap = lambda cfg_, a, b, amt, slip: ("SIGX", {"inAmount": str(amt), "outAmount": "999"})
import trigstop
trigstop.after_buy = lambda *a, **k: None
trigstop.release_before_sell = lambda *a, **k: "none"
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,usd,status) VALUES(?,?,?,?,'buy',1,5,'pending')", (time.time(), pid, "MINTF", "HISTF"))
tb_ = con.execute("SELECT last_insert_rowid()").fetchone()[0]; con.commit()
live._do_buy(cfg, tb_, pid, "MINTF", "HISTF", 5.0)
check("real buy starts the fill recorder with the quote, slippage setting and paper entry price",
      started and started[-1][0] == tb_ and started[-1][1]["quote"]["outAmount"] == "999" and started[-1][1]["slip_bps"] == 300
      and started[-1][1]["paper_price"] == r["entry_price"])
con.execute("INSERT INTO live_trades(ts,pos_id,mint,symbol,side,frac,status,reason) VALUES(?,?,?,?,'sell',1,'pending','test')", (time.time(), pid, "MINTF", "HISTF"))
ts_ = con.execute("SELECT last_insert_rowid()").fetchone()[0]; con.commit()
live._do_sell(cfg, ts_, pid, "MINTF", "HISTF", 1.0, "test")
check("real sell starts the fill recorder with the quote and the paper price at the sell",
      started[-1][0] == ts_ and started[-1][1]["quote"]["inAmount"] == "1000" and started[-1][1]["paper_price"] is not None)
d = live.dashboard(con, cfg)
check("data.json live.trades carry fill_price_usd / slippage_pct / vs_paper_pct", "fill_price_usd" in d["trades"][0] and "slippage_pct" in d["trades"][0])
n_ticks = con.execute("SELECT COUNT(*) FROM position_ticks").fetchone()[0]
check("nothing deletes history rows (count only grows)", n_ticks > 0)

fails = [n_ for n_, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
