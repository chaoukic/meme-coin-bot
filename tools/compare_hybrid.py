"""Re-uses /tmp/cmp_raw.json from compare_jup_ds.py: hybrid (Jupiter prescreen + DexScreener for the rest) vs DexScreener-only."""
import json, sqlite3, sys
sys.path.insert(0, "/workspace/memebot")
import scanner as S
from common import load_config, BASE
sys.argv = sys.argv[:1]
cfg = load_config(); F = cfg["filters"]
raw = json.load(open("/tmp/cmp_raw.json"))
con = sqlite3.connect(f"file:{BASE}/memebot.db?mode=ro", uri=True); con.row_factory = sqlite3.Row
class RO:
    def __init__(s, c): s.c = c
    def execute(s, q, *a):
        if q.strip().upper().startswith(("UPDATE", "INSERT", "DELETE")): raise RuntimeError("write blocked")
        return s.c.execute(q, *a)
def ev(t, ps):
    if not ps: return ("pending", "data", False)
    m = S.pair_metrics(S.best_pair(ps, t["pool_address"]), ps)
    r = S.evaluate(RO(con), cfg, t, m, {"left": 0, "rpc_left": 0}); return (r[0], r[1], r[4])
n = same = same_stage = same_final = by_jup = 0; mism = []
for a, j in raw["jup"].items():
    t = con.execute("SELECT * FROM tokens WHERE address=?", (a,)).fetchone()
    d = ev(t, raw["ds"].get(a))
    pre = S.jup_prescreen(S.jup_metrics(j, t), F)
    h = (pre[0], pre[1], pre[3]) if pre else d
    by_jup += bool(pre); n += 1
    same += d[0] == h[0]; same_stage += d[:2] == h[:2]; same_final += d[2] == h[2]
    if d[:2] != h[:2] or d[2] != h[2]: mism.append((t["symbol"], d, h))
print(f"tokens {n}, settled by Jupiter alone {by_jup}, pass/reject/pending match {same}/{n}, same stage {same_stage}/{n}, same final flag {same_final}/{n}")
for x in mism: print(x)
