"""Two-tier daily loss cap: normal / soft cap (score >= cap_min_score, size x cap_size_factor) / hard floor.
Runs on a COPY of the DB. Run: .venv/bin/python tests/test_daycap.py"""
import os, sqlite3, sys, shutil, tempfile
sys.path.insert(0, "/workspace/memebot")
import common
tmp = tempfile.mkdtemp(); TEST_DB = os.path.join(tmp, "test.db")
src = sqlite3.connect(common.DB_PATH); dst = sqlite3.connect(TEST_DB); src.backup(dst); dst.close(); src.close()
common.DB_PATH = TEST_DB
import notify; notify.DB_PATH = TEST_DB
import trader, priceguard as pg
from common import get_state, set_state

cfg = common.load_config(); P = cfg["paper"]
cfg.setdefault("live", {})["enabled"] = False  # tests must never touch the real wallet
cfg.setdefault("rugcheck", {})["enabled"] = False  # no network in tests
cfg["guard"]["use_second_source"] = False
con = common.db(); common.init_db()
# clean slate: no open positions, no cooldowns, plenty of cash, fixed day
con.execute("UPDATE positions SET status='closed', closed_at=0 WHERE status='open'")
con.execute("UPDATE positions SET closed_at=0")
set_state(con, "manual_pause", {"on": False}); set_state(con, "day", common.toronto_date())
con.commit()
results = []
def check(name, cond):
    results.append((name, bool(cond))); print(("PASS " if cond else "FAIL ") + name)

n = [0]
def cand(score):
    n[0] += 1
    return {"token": f"MINT{n[0]}", "pair": f"PAIR{n[0]}", "symbol": f"COIN{n[0]}", "score": score,
            "price": 1e-4, "liq": 1_000_000, "url": ""}
trader.ds_pairs = lambda pairs: {p: {"pairAddress": p, "baseToken": {"address": "MINT" + p[4:]}, "priceUsd": "0.0001",
                                     "liquidity": {"usd": 1_000_000}} for p in pairs}
def setup(day_loss_pct):
    """Close test positions, set cash so equity = 1000, and a day start so today's P&L = -day_loss_pct %."""
    con.execute("UPDATE positions SET status='closed', closed_at=0 WHERE status='open'")
    set_state(con, "cash", 1000.0 - sum(0 for _ in []))
    # cash() may be derived from state 'cash'; equity = cash when nothing is open
    eq = trader.equity(con, cfg)
    set_state(con, "cash", get_state(con, "cash") + (1000.0 - eq))
    set_state(con, "day_start_equity", 1000.0 / (1 - day_loss_pct / 100))
    set_state(con, "paused_today", False); set_state(con, "day_mode", "normal")
    con.commit()
def buy(score):
    c = cand(score)
    before = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    trader.try_entries(con, cfg, [c]); con.commit()
    r = con.execute("SELECT * FROM positions WHERE token=?", (c["token"],)).fetchone()
    skip = con.execute("SELECT message FROM decisions WHERE token=? AND kind='SKIP' ORDER BY id DESC", (c["token"],)).fetchone()
    return r, (skip["message"] if skip else None)

normal_size = 1000 * P["position_pct_of_balance"] / 100
setup(1)
r, _ = buy(62)
check(f"normal (-1%): score 62 buys at full size ${normal_size:.0f}", r and abs(r["cost_usd"] - normal_size) < 0.01)
check("normal: mode reported 'normal'", trader.day_status(con, cfg)["mode"] == "normal")

setup(6)
r, _ = buy(80)
check(f"soft cap (-6%): score 80 buys at {P['cap_size_factor']*100:.0f}% size (${normal_size*P['cap_size_factor']:.0f})",
      r and abs(r["cost_usd"] - normal_size * P["cap_size_factor"]) < 0.01)
check("soft cap: mode 'soft', alert queued, buy decision says reduced size",
      get_state(con, "day_mode") == "soft" and con.execute("SELECT 1 FROM outbox WHERE key LIKE 'pause:%' AND text LIKE '%high-score%'").fetchone()
      and con.execute("SELECT 1 FROM decisions WHERE kind='BUY' AND token=? AND message LIKE '%reduced size 50%% (daily soft cap)%'", (r["token"],)).fetchone())
r, skip = buy(70)
check("soft cap: score 70 skipped with '< 75 (daily soft cap active)'", r is None and skip and "70 < 75 (daily soft cap active)" in skip)
r, skip = buy(75)
check("soft cap: score exactly 75 allowed", r is not None)

setup(11)
r, skip = buy(95)
check("hard floor (-11%): even score 95 skipped", r is None and skip and "hard floor" in skip)
check("hard floor: mode 'hard', paused_today set, hard-floor alert queued",
      trader.day_status(con, cfg)["mode"] == "hard" and get_state(con, "paused_today")
      and con.execute("SELECT 1 FROM outbox WHERE key LIKE 'hardstop:%'").fetchone())
# hard floor is latched for the day even if P&L recovers
set_state(con, "day_start_equity", 1000.0); con.commit()
r, skip = buy(95)
check("hard floor stays on for the rest of the day after recovery", r is None)
# exactly at -5% and -10% boundaries
check("boundary: exactly -5% is soft", trader.day_mode(-50, 1000, P) == "soft")
check("boundary: exactly -10% is hard", trader.day_mode(-100, 1000, P) == "hard")
check("boundary: -4.99% is normal", trader.day_mode(-49.9, 1000, P) == "normal")

con.close(); shutil.rmtree(tmp)
bad = [x for x, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} passed"); sys.exit(1 if bad else 0)
