"""Pre-buy rug check using RugCheck (api.rugcheck.xyz, free, no key). Added 2026-09-26 after 9 rug exits.
A coin is only bought if: RugCheck answers (fails closed), the pool's LP is locked/burned >= min_lp_locked_pct,
and RugCheck lists no 'danger' risk (e.g. one wallet holding a big share, top-10 holding too much, low liquidity)."""
import time, requests
from common import log

DEFAULTS = {"enabled": True, "min_lp_locked_pct": 90, "reject_danger": True, "fail_closed": True, "cache_sec": 600}
_CACHE = {}

def cfg_rc(cfg):
    d = dict(DEFAULTS); d.update(cfg.get("rugcheck", {})); return d

def report(mint):
    hit = _CACHE.get(mint)
    if hit and time.time() - hit[0] < DEFAULTS["cache_sec"]:
        return hit[1]
    last = None
    for i in range(3):
        try:
            r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            j = r.json()
            _CACHE[mint] = (time.time(), j)
            return j
        except Exception as e:
            last = e; time.sleep(2 + 2 * i)
    raise RuntimeError(f"RugCheck unavailable: {last}")

def check(mint, cfg, pair=None):
    """(ok, reason). Uses the full report (the summary's LP number is unreliable) and the LP lock of the pool we buy in."""
    R = cfg_rc(cfg)
    if not R["enabled"]:
        return True, "rug check off"
    try:
        j = report(mint)
    except Exception as e:
        log.warning("rugcheck %s: %s", mint, e)
        return (not R["fail_closed"]), f"rug check unavailable ({e})"
    if j.get("rugged"):
        return False, "RugCheck marks this coin as already rugged"
    mk = [m for m in j.get("markets") or [] if pair and m.get("pubkey") == pair]
    if mk:
        lp = (mk[0].get("lp") or {}).get("lpLockedPct")
    else:
        lps = [(m.get("lp") or {}).get("lpLockedPct") for m in j.get("markets") or []]
        lps = [x for x in lps if x is not None]
        lp = min(lps) if lps else None  # pool not found: be strict, every pool must be locked
    if lp is None or float(lp) < float(R["min_lp_locked_pct"]):
        return False, f"pool money not locked (LP locked {float(lp or 0):.0f}% < {R['min_lp_locked_pct']}%) - creator could pull it"
    if R["reject_danger"]:
        dangers = sorted({x.get("name", "?") for x in j.get("risks") or [] if x.get("level") == "danger"})
        if dangers:
            return False, "RugCheck danger: " + ", ".join(dangers)
    return True, f"rug check ok (LP locked {float(lp):.0f}%, no danger flags)"
