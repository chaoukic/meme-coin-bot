"""One-off (Oct 3): store the actual fill of every past REAL trade that has no live_fills row yet.
Reads each confirmed transaction (read-only RPC). SOL/USD: buys use the price the bot used then (usd / SOL spent);
sells use Kraken's hourly SOL/USD close at the block time. No quote was stored before Oct 3, so quote slippage is empty.
Run: .venv/bin/python tools/backfill_fills.py"""
import os, sys, time, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common, live, fills

_K = {}
def kraken_close(ts):
    if not _K:
        r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": "SOLUSD", "interval": 60,
                         "since": int(time.time()) - 29 * 86400}, timeout=20).json()["result"]
        for row in next(v for k, v in r.items() if k != "last"):
            _K[int(row[0])] = float(row[4])
    h = int(ts) // 3600 * 3600
    return _K.get(h) or _K.get(h - 3600)

cfg = common.load_config(); common.init_db()
con = common.db(); live.ensure(con); fills.ensure(con)
rows = con.execute("""SELECT t.*, p.entry_price, p.last_price, p.status pstatus FROM live_trades t LEFT JOIN positions p ON p.id=t.pos_id
                      WHERE t.status='ok' AND t.sig IS NOT NULL AND NOT EXISTS (SELECT 1 FROM live_fills f WHERE f.trade_id=t.id)
                      ORDER BY t.id""").fetchall()
con.close()
print(len(rows), "trades to backfill")
for r in rows:
    if r["side"] == "buy" and r["sol_lamports"]:
        px, src = r["usd"] / (r["sol_lamports"] / 1e9), "buy_row"
        paper = r["entry_price"]
    else:
        px, src = kraken_close(r["ts"]), "kraken_1h_close"
        paper = r["last_price"] if (r["frac"] >= 0.999 and r["pstatus"] == "closed") else None
    f = fills.record(cfg, r["id"], sol_price=px, sol_price_source=src, paper_price=paper)
    print(r["id"], r["side"], r["symbol"], "ok" if f else "FAILED", f and f.get("source"), f and f.get("fill_price_usd"))
    time.sleep(1.5)
