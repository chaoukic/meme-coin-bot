"""One-off correction (2026-09-26): re-price exits booked on rug/zero-volume price prints.
Writes every change into the `corrections` table (before/after) and a CORRECTION decision row."""
import json, sys
sys.path.insert(0, "/workspace/memebot")
import common
from common import get_state, set_state, now_ts, decision
import priceguard as pg

common.init_db()
cfg = common.load_config(); P = cfg["paper"]; G = pg.cfg_guard(cfg)
con = common.db()

FIX = [
  # id, sold-part qty computed below, realistic price, pool liquidity after the pull, note
  (34, 2.935e-09, 0.0035, "corrected: bad price data - DDOS $0.03061 was a zero-volume print after liquidity was pulled "
       "(no trades 06:54-07:05 ET, 07:06 candle volume $0). Remaining half re-priced at the post-pull pool price "
       "$0.000000002935 with pool liquidity ~$0 (fill-capped), i.e. worth ~$0."),
  (22, 9.22e-06, 0.0056, "corrected: bad price data - UPTOBER $0.02232 was a rug print (20:58 ET candle: open $0.000531, "
       "low $0.00000922, volume only $604; pool reserve now ~$0). Position re-priced at the pull low with ~$0 pool "
       "liquidity (fill-capped), i.e. worth ~$0."),
]
audit = lambda pid, field, b, a, note: con.execute(
    "INSERT INTO corrections(ts,position_id,field,before,after,note) VALUES(?,?,?,?,?,?)",
    (now_ts(), pid, field, json.dumps(b), json.dumps(a), note))

cash0 = get_state(con, "cash"); dse0 = get_state(con, "day_start_equity")
total_delta = 0.0; summary = []
for pid, px, liq, note in FIX:
    p = con.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    assert p["status"] == "closed"
    if con.execute("SELECT 1 FROM corrections WHERE position_id=? AND field='exit_reason'", (pid,)).fetchone():
        print("already corrected", pid); continue
    sold_qty = p["qty"] * (1 - P["take_profit_sell_fraction"]) if p["tp1_done"] else p["qty"]
    kept = p["proceeds_usd"] - (p["qty"] - sold_qty) * 0 if not p["tp1_done"] else None
    # proceeds booked before the bad exit (TP1 partial, if any)
    prior = 0.0
    if p["tp1_done"]:
        tp1_px = con.execute("SELECT message FROM decisions WHERE kind='SELL' AND token=? AND message LIKE '%PARTIAL%' "
                             "ORDER BY ts DESC LIMIT 1", (p["token"],)).fetchone()["message"]
        prior = float(tp1_px.split("(+$")[1].split(")")[0].replace(",", ""))
    new_exit = pg.sell_value(sold_qty, px, P, G, liq)
    new_proceeds = prior + new_exit
    new_pnl = new_proceeds - p["cost_usd"]
    new_pct = new_pnl / p["cost_usd"] * 100
    delta = new_pnl - p["pnl_usd"]
    before = {k: p[k] for k in ("last_price", "proceeds_usd", "pnl_usd", "pnl_pct", "exit_reason")}
    after = {"last_price": px, "proceeds_usd": new_proceeds, "pnl_usd": new_pnl, "pnl_pct": new_pct,
             "exit_reason": "corrected: bad price data (was: " + p["exit_reason"] + ")"}
    con.execute("UPDATE positions SET last_price=?, proceeds_usd=?, pnl_usd=?, pnl_pct=?, exit_reason=? WHERE id=?",
                (after["last_price"], new_proceeds, new_pnl, new_pct, after["exit_reason"], pid))
    for k in before:
        audit(pid, k, before[k], after[k], note)
    n = con.execute("UPDATE equity SET equity=equity+?, cash=cash+? WHERE ts>=?", (delta, delta, p["closed_at"] - 1)).rowcount
    audit(pid, "equity_history", f"{n} rows from ts {p['closed_at']-1:.0f}", f"shifted by {delta:+.2f}", note)
    total_delta += delta
    summary.append(f"#{pid} {p['symbol']}: P&L {p['pnl_usd']:+,.2f} -> {new_pnl:+,.2f} ({delta:+,.2f})")
    # daily baseline recorded after this exit also carried the inflated value
    day_start_ts = common.toronto_midnight_ts() if hasattr(common, "toronto_midnight_ts") else None
    if pid == 22:  # closed 2026-09-25 20:58 ET, before today's (2026-09-26) day_start_equity was recorded
        dse1 = get_state(con, "day_start_equity") + delta
        audit(None, "state.day_start_equity", get_state(con, "day_start_equity"), dse1, note)
        set_state(con, "day_start_equity", dse1)

cash1 = cash0 + total_delta
audit(None, "state.cash", cash0, cash1, "sum of corrections")
set_state(con, "cash", cash1)
chk = 1000 + con.execute("SELECT SUM(pnl_usd) FROM positions WHERE status='closed'").fetchone()[0] \
      + con.execute("SELECT COALESCE(SUM(COALESCE(proceeds_usd,0)-cost_usd),0) FROM positions WHERE status='open'").fetchone()[0]
assert abs(chk - cash1) < 0.01, (chk, cash1)
decision(con, "CORRECTION", None, None, "Records corrected for rug/zero-volume price prints: " + "; ".join(summary)
         + f". Fake cash ${cash0:,.2f} -> ${cash1:,.2f}. Day-start equity ${dse0:,.2f} -> ${get_state(con,'day_start_equity'):,.2f}. "
         "Details in the corrections table.")
con.commit()
print("\n".join(summary)); print(f"cash {cash0:.2f} -> {cash1:.2f}; day_start_equity {dse0:.2f} -> {get_state(con,'day_start_equity'):.2f}; check {chk:.2f}")
