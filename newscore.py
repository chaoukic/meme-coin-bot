"""Side-by-side scoring test (Oct 3 2026, spec /home/box/analysis-bot/scoring/SIDE_BY_SIDE_SPEC.md, Sammy approved).
Wraps the COPIED reference scorer scoring_v1/score_v1.py + scoring_v1/models/memebot_v1.json (copied, not linked, so a
re-fit in analysis-bot can never change a running test). The 'new' paper account trades on this score; real money never."""
import json, os, sys, time
from common import log

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scoring_v1")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import score_v1  # noqa: E402

BOT = "memebot"
STATUS = {"ok": False, "checked": False, "worst": None, "rows": 0, "error": None}

def model():
    return score_v1.load(BOT)

def version():
    m = model()
    return f"{m['model']} {m['version']}"          # "scoring_v1 1.0"

def thresholds():
    m = model()
    return float(m["recommended_buy_threshold"]), float(m["top10pct_threshold"])

def selftest():
    """Re-score the model's 10 stored rows (memebot only) exactly like score_v1._selftest. Sets STATUS; never raises."""
    try:
        rows = json.load(open(os.path.join(HERE, "models", f"{BOT}_v1_testrows.json")))
        worst = 0.0
        for r in rows:
            s, p = score_v1.score(BOT, r["metrics"])
            worst = max(worst, abs(p - r["expected_prob"]))
        STATUS.update(ok=worst < 1e-9 and len(rows) > 0, checked=True, worst=worst, rows=len(rows), error=None, ts=time.time())
    except Exception as e:
        STATUS.update(ok=False, checked=True, error=str(e)[:200], ts=time.time())
    return STATUS["ok"]

def enabled(cfg):
    S = cfg.get("scoring", {})
    if not S.get("enabled", False):
        return False
    if not STATUS["checked"]:
        selftest()
    return STATUS["ok"]

def score_metrics(m):
    """(score_new, prob_new, note) from the SAME metrics dict the bot saves in evaluations.metrics.
    Liquidity/market cap are only given to the scorer when they are DexScreener POOL figures (Jupiter's liquidity is
    coin-wide, on a different scale): for Jupiter-sourced metrics they are left out, so the domain guard can't misfire."""
    d = {k: m.get(k) for k in ("liq", "vol_h1", "vol_h24", "mcap", "age_min", "buys_h1", "sells_h1", "chg_h1", "chg_m5", "chg_h24")}
    note = None
    if m.get("src") == "jupiter":
        d["liq"] = d["mcap"] = None
        note = "jupiter_metrics_no_liq"
    elif score_v1.out_of_domain(d):
        # Oct 6 (Sammy): DexScreener pool liquidity on Solana meme pools is usually >= ~77% of market cap, which the
        # model's guard (built on Cross Chain data) treats as bad data and scores 0 - so EVERY coin at the score step got 0.
        # Score these the same way as Jupiter-sourced coins: without liquidity / market cap. Paper (new account) only.
        d["liq"] = d["mcap"] = None
        note = "pool_liq_near_mcap_no_liq"
    s, p = score_v1.score(BOT, d)
    return s, p, note
