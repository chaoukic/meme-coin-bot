"""One-off (idempotent) backfill of decisions.score_new / score_old (added Oct 6).
Order per row: 1) numbers stated in the message ('new score X', 'old score X', 'score X'; 'None' = known unknown),
2) BUY/SELL: the position's score_current / score_new, 3) the most recent evaluations row for the token with
ts <= decision ts that has a score. Older-coin (established) decisions have no scores and stay null.
Only rows where both columns are still NULL are touched.  Run: .venv/bin/python tools/backfill_decision_scores.py"""
import bisect, re, sys
sys.path.insert(0, "/workspace/memebot")
import common

NUM = r"(-?\d+(?:\.\d+)?|None)"
RE_SCORE = re.compile(r"^(new|old) scoring (?:passed|turned down) \(" + NUM + r" [<>]=? [\d.]+\); (new|old) score " + NUM)
RE_NEW = re.compile(r"\bnew score " + NUM)
RE_OLD = re.compile(r"\bold score " + NUM)
RE_PLAIN = re.compile(r"(?<!new )(?<!old )\bscore " + NUM)
UNSET = object()

def _v(x):
    return None if x == "None" else float(x)

def parse(msg):
    """-> (new, old) each a float, None (stated as unknown) or UNSET (not in the message)."""
    m = RE_SCORE.match(msg or "")
    if m:
        a, b = (_v(m.group(2)), _v(m.group(4)))
        return (a, b) if m.group(1) == "new" else (b, a)
    new = old = UNSET
    if (m := RE_NEW.search(msg or "")):
        new = _v(m.group(1))
    if (m := RE_OLD.search(msg or "")) or (m := RE_PLAIN.search(msg or "")):
        old = _v(m.group(1))
    return new, old

def is_estab(msg):
    return "[older coins]" in (msg or "") or "(older coin)" in (msg or "")

def main(dry=False):
    common.init_db()
    con = common.db()
    rows = [dict(r) for r in con.execute("SELECT id, ts, kind, token, scoring, message FROM decisions "
                                         "WHERE score_new IS NULL AND score_old IS NULL ORDER BY id")]
    toks = sorted({r["token"] for r in rows if r["token"] and not is_estab(r["message"])})
    ev = {}
    if toks:
        q = ",".join("?" * len(toks))
        for t, ts, sc, sn in con.execute(f"SELECT token, ts, score, score_new FROM evaluations WHERE token IN ({q}) "
                                         "AND (score IS NOT NULL OR score_new IS NOT NULL) ORDER BY ts", toks):
            ev.setdefault(t, []).append((ts, sc, sn))
    stats = {"rows": len(rows), "updated": 0, "from_message": 0, "from_position": 0, "from_evaluation": 0}
    for r in rows:
        if is_estab(r["message"]):
            continue
        new, old = parse(r["message"])
        src = set()
        if new is not UNSET or old is not UNSET:
            src.add("from_message")
        if r["kind"] in ("BUY", "SELL") and r["token"] and (new is UNSET or old is UNSET):
            p = con.execute("""SELECT score, score_current, score_new, scoring FROM positions WHERE token=? AND
                               COALESCE(scoring,'current')=? AND COALESCE(strategy,'new')='new' AND opened_at<=?
                               ORDER BY opened_at DESC LIMIT 1""", (r["token"], r["scoring"] or "current", r["ts"] + 5)).fetchone()
            if p:
                if new is UNSET and p["score_new"] is not None:
                    new = p["score_new"]; src.add("from_position")
                po = p["score_current"] if p["score_current"] is not None else (p["score"] if (p["scoring"] or "current") == "current" else None)
                if old is UNSET and po is not None:
                    old = po; src.add("from_position")
        if r["token"] and (new is UNSET or old is UNSET) and r["token"] in ev:
            lst = ev[r["token"]]
            i = bisect.bisect_right([e[0] for e in lst], r["ts"]) - 1
            if i >= 0:
                _, sc, sn = lst[i]
                if new is UNSET and sn is not None:
                    new = sn; src.add("from_evaluation")
                if old is UNSET and sc is not None:
                    old = sc; src.add("from_evaluation")
        new = None if new is UNSET else new
        old = None if old is UNSET else old
        if new is None and old is None:
            continue
        stats["updated"] += 1
        for s in src:
            stats[s] += 1
        if not dry:
            con.execute("UPDATE decisions SET score_new=?, score_old=? WHERE id=? AND score_new IS NULL AND score_old IS NULL",
                        (new, old, r["id"]))
    con.commit()
    con.close()
    print(stats)

if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
