"""One-off comparison: DexScreener vs Jupiter Tokens API v2 for watchlist tokens (read-only, no DB writes).
Never prints the API key."""
import json, sqlite3, sys, time, math, statistics as st
sys.path.insert(0, "/workspace/memebot")
import requests
from common import load_config, fnum, BASE
import scanner, live

cfg = load_config()
KEY = live.jup_key()
con = sqlite3.connect(f"file:{BASE}/memebot.db?mode=ro", uri=True, timeout=30)
con.row_factory = sqlite3.Row
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
newest = con.execute("SELECT * FROM tokens WHERE status='watch' ORDER BY first_seen DESC LIMIT ?", (N,)).fetchall()
# second set: tokens that recently got past the liquidity stage (harder cases)
deep = con.execute("""SELECT t.* FROM tokens t WHERE t.status='watch' AND t.last_eval > ? AND t.last_stage NOT IN ('liquidity','age','data')
                      AND t.address NOT IN (SELECT address FROM tokens WHERE status='watch' ORDER BY first_seen DESC LIMIT ?)
                      ORDER BY t.last_eval DESC LIMIT ?""", (time.time() - 86400, N, N)).fetchall()
sets = {"newest": newest, "past_liquidity": deep}
allt = newest + deep
addrs = [t["address"] for t in allt]

def ds_fetch(addrs):
    out = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        for attempt in range(6):
            r = requests.get(f"{scanner.DS}/tokens/v1/solana/{','.join(chunk)}", timeout=20)
            if r.status_code == 200:
                for p in r.json():
                    b = (p.get("baseToken") or {}).get("address")
                    if b: out.setdefault(b, []).append(p)
                break
            time.sleep(10 * (attempt + 1))
        else:
            print("DS chunk failed", i, file=sys.stderr)
        time.sleep(2)
    return out

def jup_fetch(addrs):
    out = {}
    for i in range(0, len(addrs), 100):
        r = requests.get("https://api.jup.ag/tokens/v2/search", params={"query": ",".join(addrs[i:i + 100])},
                         headers={"x-api-key": KEY}, timeout=20)
        r.raise_for_status()
        for x in r.json():
            out[x["id"]] = x
        time.sleep(1.5)
    return out

t0 = time.time(); ds = ds_fetch(addrs); t_ds = time.time() - t0
t0 = time.time(); jp = jup_fetch(addrs); t_jp = time.time() - t0
json.dump({"ds": ds, "jup": jp}, open("/tmp/cmp_raw.json", "w"))

import scanner as S
def pct(a, b):
    if a is None or b is None: return None
    if a == 0 and b == 0: return 0.0
    return abs(a - b) / max(abs(a), abs(b)) * 100

class RO:  # evaluate() with budget 0 never writes; guard anyway
    def __init__(s, c): s.c = c
    def execute(s, q, *a):
        if q.strip().upper().startswith(("UPDATE", "INSERT", "DELETE")): raise RuntimeError("write blocked")
        return s.c.execute(q, *a)

def ev(t, m):
    if m is None:
        return ("pending", "data", None)
    b = {"left": 0, "rpc_left": 0}
    r = S.evaluate(RO(con), cfg, t, dict(m), b)
    return (r[0], r[1], r[3])

def hypo_score(t, m):
    if not m or not m.get("liq"): return None
    d = json.loads(t["contract_json"]) if t["contract_json"] else {}
    mm = dict(m); mm["_minliq"] = cfg["filters"]["min_liquidity_usd"]; mm["_maxtop1"] = cfg["filters"]["max_top_holder_pct"]
    if mm["liq"] < mm["_minliq"]: mm["liq"] = mm["liq"]  # score formula clamps at 0 below min
    return S.score(mm, d, [])[0]

report = {"timing": {"ds_s": round(t_ds, 1), "jup_s": round(t_jp, 1), "ds_calls": math.ceil(len(addrs) / 30), "jup_calls": math.ceil(len(addrs) / 100)}}
for age_mode in ("firstPool", "discovery_pool"):
    for name, toks in sets.items():
        rows = []
        for t in toks:
            a = t["address"]
            dm = S.pair_metrics(S.best_pair(ds[a], t["pool_address"]), ds[a]) if a in ds else None
            jm = S.jup_metrics(jp[a], t, age_mode=age_mode) if a in jp else None
            rows.append((t, dm, jm, ev(t, dm), ev(t, jm)))
        n = len(rows)
        both = [r for r in rows if r[1] and r[2]]
        def pf(e): return "pass" if e[0] == "pass" else ("pending" if e[0] == "pending" else "reject")
        same_res = sum(1 for r in rows if pf(r[3]) == pf(r[4]))
        same_stage = sum(1 for r in rows if r[3][:2] == r[4][:2])
        diffs = {}
        for f in ("price", "liq", "chg_h1", "vol_h1", "vol_h24", "buys_h1", "sells_h1", "mcap", "age_min"):
            ds_ = [pct(fnum(r[1][f]), fnum(r[2][f])) for r in both if r[1][f] is not None and r[2][f] is not None]
            ds_ = [x for x in ds_ if x is not None]
            if f == "chg_h1":
                ab = [abs(r[1][f] - r[2][f]) for r in both]
                diffs[f] = {"median_abs_pts": round(st.median(ab), 1) if ab else None,
                            "within_5pts": f"{sum(1 for x in ab if x <= 5)}/{len(ab)}"}
            elif f == "age_min":
                ab = [abs(r[1][f] - r[2][f]) for r in both if r[1][f] is not None and r[2][f] is not None]
                diffs[f] = {"median_abs_min": round(st.median(ab), 1) if ab else None,
                            "within_10min": f"{sum(1 for x in ab if x <= 10)}/{len(ab)}"}
            else:
                diffs[f] = {"median_pct_diff": round(st.median(ds_), 1) if ds_ else None,
                            "within_10pct": f"{sum(1 for x in ds_ if x <= 10)}/{len(ds_)}",
                            "within_25pct": f"{sum(1 for x in ds_ if x <= 25)}/{len(ds_)}"}
        sc = [(hypo_score(r[0], r[1]), hypo_score(r[0], r[2])) for r in both]
        sc = [(a, b) for a, b in sc if a is not None and b is not None]
        mism = [(r[0]["symbol"], f"{r[3][0]}/{r[3][1]}", f"{r[4][0]}/{r[4][1]}") for r in rows if pf(r[3]) != pf(r[4])]
        report[f"{name}|age={age_mode}"] = {
            "tokens": n, "ds_found": sum(1 for r in rows if r[1]), "jup_found": sum(1 for r in rows if r[2]),
            "pass_reject_pending_match": f"{same_res}/{n} ({same_res / n * 100:.1f}%)",
            "same_stage_match": f"{same_stage}/{n} ({same_stage / n * 100:.1f}%)",
            "ds_results": {k: sum(1 for r in rows if pf(r[3]) == k) for k in ("pass", "reject", "pending")},
            "jup_results": {k: sum(1 for r in rows if pf(r[4]) == k) for k in ("pass", "reject", "pending")},
            "field_diffs": diffs,
            "score_(hypothetical, liq>0)": {"n": len(sc), "median_abs_diff": round(st.median([abs(a - b) for a, b in sc]), 1) if sc else None,
                                            "within_5pts": f"{sum(1 for a, b in sc if abs(a - b) <= 5)}/{len(sc)}",
                                            "same_side_of_55": f"{sum(1 for a, b in sc if (a >= 55) == (b >= 55))}/{len(sc)}"},
            "mismatches": mism[:25],
        }
print(json.dumps(report, indent=1))
