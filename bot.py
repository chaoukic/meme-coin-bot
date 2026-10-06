"""Main background process: scanner loop + paper-position updater.
PAPER TRADING ONLY."""
import json, logging, os, signal, sys, threading, time, traceback
from common import (init_db, db, load_config, apply_limits, now_ts, set_state, get_state, CALLS, BASE, log)
import scanner, trader, newscore, outage
import common

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
STOP = threading.Event()
LAST_CYCLE = {"ts": time.time()}  # last COMPLETED scan cycle (watchdog); starts at process start


def _log_scores(con, cfg, token, sym, sc, sn, g_cur, g_new):
    """Decision log: every coin that reached the scoring step gets one SCORE line per account (passed or turned down),
    at most once per coin per 30 min per account, so each tab shows what its method thought even when nothing is bought."""
    try:
        since = now_ts() - 1800
        thr_cur = cfg["filters"]["min_score_to_buy"]
        rows = [("current", None, sc, thr_cur, g_cur, "COALESCE(scoring,'current')='current'")]
        if g_new in ("pass", "reject"):
            rows.append(("new", "new", sn, newscore.thresholds()[0], g_new, "scoring='new'"))
        for acct, tagv, val, thr, gv, w in rows:
            if con.execute("SELECT 1 FROM decisions WHERE kind='SCORE' AND token=? AND ts>? AND " + w, (token, since)).fetchone():
                continue
            name = "old" if acct == "current" else "new"
            other = (f"new score {sn}" if acct == "current" else f"old score {sc}")
            verdict = f"passed ({val} >= {thr:g})" if gv == "pass" else f"turned down ({val} < {thr:g})"
            common.decision(con, "SCORE", token, sym, f"{name} scoring {verdict}; {other}", scoring=tagv,
                            score_new=sn, score_old=sc)
    except Exception as e:
        log.warning("score log failed: %s", e)

def fetch_market(cfg, toks):
    """Market data for the watchlist. Returns ({address: (kind, data)}, source counts).
    kind 'ds'  : DexScreener pairs list -> normal filters/score (same as before)
    kind 'jup' : (Jupiter metrics, prescreen decision) - settled on Jupiter data alone (too young / too old / liquidity far too low)
    kind 'none': DexScreener answered and the coin is not listed there
    kind 'busy': every source failed for this coin this cycle (never counted as 'not listed')
    [apis] scan_source = "jupiter" uses Jupiter Tokens API v2 first; a Jupiter batch that fails falls back to DexScreener.
    Coins Jupiter can't settle are re-checked on DexScreener pool data, which the filters' thresholds were set on and
    which gives the pool address that paper/real trading uses."""
    addrs = [t["address"] for t in toks]
    counts = {"jupiter": 0, "dexscreener": 0, "busy": 0, "not_listed": 0, "jup_batches_failed": 0}
    out, need_ds = {}, list(addrs)
    if cfg.get("apis", {}).get("scan_source", "dexscreener") == "jupiter":
        jp, failed = scanner.jup_tokens(addrs)
        counts["jup_batches_failed"] = (len(failed) + 99) // 100
        need_ds = []
        F = cfg["filters"]
        for t in toks:
            j = jp.get(t["address"])
            if j:
                m = scanner.jup_metrics(j, t)
                pre = scanner.jup_prescreen(m, F)
                if pre:
                    out[t["address"]] = ("jup", (m, pre))
                    counts["jupiter"] += 1
                    continue
            need_ds.append(t["address"])
    if need_ds:
        # in Jupiter mode a busy DexScreener is asked once per cycle (coins wait as 'pending', retried next cycle)
        jmode = cfg.get("apis", {}).get("scan_source") == "jupiter"
        if jmode and common.ds_paused():
            counts["ds_paused"] = True
        pairs, failed = scanner.ds_tokens_ex(need_ds, retries=1 if jmode else 3, honour_pause=jmode)
        failed = set(failed)
        for a in need_ds:
            if a in pairs:
                out[a] = ("ds", pairs[a]); counts["dexscreener"] += 1
            elif a in failed:
                out[a] = ("busy", None); counts["busy"] += 1
            else:
                out[a] = ("none", None); counts["not_listed"] += 1
    return out, counts

def run_cycle(cfg):
    con = db()
    started = now_ts()
    calls0 = dict(CALLS)
    set_state(con, "scanner_status", {"state": "scanning", "since": started}); con.commit()
    errors = []
    found = scanner.discover(cfg)
    if not found:
        errors.append("discovery returned nothing (GeckoTerminal down or rate limited?)")
    new = scanner.register(con, found)
    con.commit()
    toks = con.execute("SELECT * FROM tokens WHERE status='watch' ORDER BY first_seen DESC LIMIT ?",
                       (cfg["scanner"]["max_watchlist_eval"],)).fetchall()
    market, src_counts = fetch_market(cfg, toks)
    cur = con.execute("INSERT INTO cycles(started) VALUES(?)", (started,))
    cid = cur.lastrowid
    con.commit()
    funnel = {"evaluated": 0, "passed": 0, "rejected": {}, "pending": {}, "data_source": src_counts}
    budget = {"left": int(cfg["scanner"]["contract_checks_per_cycle"]), "rpc_left": int(cfg["scanner"].get("holder_retries_per_cycle", 6))}
    candidates = []
    sn_on = newscore.enabled(cfg)   # side-by-side scoring test: new-score paper account (self-test passed + [scoring] enabled)
    mver = newscore.version() if sn_on else None
    funnel["passed_new"] = 0
    # evaluate tokens that already survived cheap stages first, so they get the dossier budget
    toks = sorted(toks, key=lambda t: 0 if t["last_stage"] in ("contract", "holders", "holder_trend", "score") else 1)
    for t in toks:
        kind, tp = market.get(t["address"], ("none", None))
        funnel["evaluated"] += 1
        if kind == "busy":
            res, stage, reasons, sc, m, final = "pending", "data", ["market data unavailable this cycle (DexScreener busy or paused after a 429), retry next cycle"], None, None, False
            url, sym, pair = None, t["symbol"], None
        elif kind == "jup":
            m, (res, stage, reasons, final) = tp
            sc = None
            url, sym, pair = m["url"], m["symbol"] or t["symbol"], m["pair"]
        elif not tp:
            final = now_ts() - t["first_seen"] > 1800
            res, stage, reasons, sc, m = "reject" if final else "pending", "data", ["not listed on DexScreener" + (" after 30 min" if final else " yet")], None, None
            url, sym, pair = None, t["symbol"], None
        else:
            bp = scanner.best_pair(tp, t["pool_address"])
            m = scanner.pair_metrics(bp, tp)
            try:
                res, stage, reasons, sc, final, _ = scanner.evaluate(con, cfg, t, m, budget)
            except Exception as e:
                res, stage, reasons, sc, final = "pending", "error", [f"evaluation error: {e}"], None, False
                errors.append(f"{t['symbol']}: {e}")
            url, sym, pair = m["url"], m["symbol"] or t["symbol"], m["pair"]
        # both scores on every evaluation that has metrics (spec: log both, even for early-stage rejects)
        sn = pn = note = None
        g = (m or {}).get("_gate") or {}
        g_cur = g.get("current") or ("pass" if res == "pass" else None)
        g_new = g.get("new")
        if m and sn_on:
            try:
                sn, pn, note = m.get("_sn") or newscore.score_metrics(m)
            except Exception as e:
                note = f"score_new error: {str(e)[:80]}"
        if g and m:
            _log_scores(con, cfg, t["address"], (m or {}).get("symbol") or t["symbol"], sc, sn, g_cur, g_new)
        if res == "pass":
            funnel["passed"] += 1
        else:
            funnel["rejected" if res == "reject" else "pending"][stage] = funnel["rejected" if res == "reject" else "pending"].get(stage, 0) + 1
        if g_new == "pass":
            funnel["passed_new"] += 1
        if g_cur == "pass" or g_new == "pass":
            candidates.append({"token": t["address"], "symbol": sym, "pair": pair, "url": url,
                               "price": m["price"], "liq": m["liq"], "score": sc,
                               "score_new": sn, "prob_new": pn, "model_version": mver,
                               "gate_current": g_cur or "reject", "gate_new": g_new or "n.a."})
        metrics = None
        if m:
            metrics = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()
                       if k in ("price", "liq", "vol_h1", "vol_h24", "mcap", "age_min", "buys_h1", "sells_h1", "chg_h1", "src")}
            if note:
                metrics["score_new_note"] = note
        status = "rejected" if (res == "reject" and final) else "watch"
        con.execute("UPDATE tokens SET last_eval=?, status=?, last_stage=?, last_reasons=?, last_score=?, symbol=COALESCE(?,symbol) WHERE address=?",
                    (now_ts(), status, stage, "; ".join(reasons), sc, sym, t["address"]))
        con.execute("""INSERT INTO evaluations(cycle_id,ts,token,symbol,pair,url,result,stage,reasons,score,metrics,
                       score_new,prob_new,model_version,gate_current,gate_new) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, now_ts(), t["address"], sym, pair, url, res + (" (final)" if status == "rejected" else ""), stage,
                     "; ".join(reasons), sc, json.dumps(metrics) if metrics else None,
                     sn, pn, mver if (m and sn_on) else None, g_cur if stage == "score" else None, g_new if stage == "score" else None))
        con.commit()  # keep write transactions short so the position updater never waits
    con.commit()
    opened = trader.try_entries(con, cfg, candidates, "current") if candidates else 0
    opened_new = trader.try_entries(con, cfg, candidates, "new") if (candidates and sn_on) else 0
    funnel["opened_new"] = opened_new
    calls = {k: CALLS[k] - calls0.get(k, 0) for k in CALLS}
    con.execute("UPDATE cycles SET finished=?, discovered=?, new_tokens=?, evaluated=?, passed=?, funnel=?, errors=?, api_calls=? WHERE id=?",
                (now_ts(), len(found), new, funnel["evaluated"], funnel["passed"], json.dumps(funnel), json.dumps(errors), json.dumps(calls), cid))
    set_state(con, "scanner_status", {"state": "idle", "last_run": now_ts(), "last_cycle": cid,
                                      "duration": round(now_ts() - started, 1)})
    outage.heartbeat(con)  # persisted every cycle: lets the next start detect an outage (outage.startup_check)
    # housekeeping: nothing is deleted any more (Sammy, Oct 2) - evaluations, holder snapshots, tokens and cycles are kept permanently
    con.commit()
    con.close()
    LAST_CYCLE["ts"] = time.time()
    log.info("cycle %s: discovered %s (new %s), evaluated %s, passed %s, opened %s, funnel %s, calls %s",
             cid, len(found), new, funnel["evaluated"], funnel["passed"], opened, json.dumps(funnel), calls)

def scan_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            cfg = load_config(); apply_limits(cfg)
            run_cycle(cfg)
        except Exception:
            log.error("scan cycle failed:\n%s", traceback.format_exc())
            try:
                con = db(); set_state(con, "scanner_status", {"state": "error", "last_error": traceback.format_exc()[-500:], "last_run": now_ts()}); con.commit(); con.close()
            except Exception:
                pass
            cfg = {"scanner": {"scan_interval_sec": 90}}
        STOP.wait(max(5, cfg["scanner"].get("scan_interval_sec", 90) - (time.time() - t0)))

def position_loop():
    while not STOP.is_set():
        interval = 45
        try:
            cfg = load_config(); interval = cfg["paper"].get("update_interval_sec", 45)
            con = db()
            trader.update_positions(con, cfg)
            set_state(con, "trader_heartbeat", now_ts()); con.commit(); con.close()
        except Exception:
            log.error("position update failed:\n%s", traceback.format_exc())
        STOP.wait(interval)

def main():
    init_db()
    con = db(); cfg = load_config(); trader.cash(con, cfg)
    # outage alert: must run before the first heartbeat of this process is written
    try:
        outage.startup_check(con); con.commit()
    except Exception as e:
        log.warning("outage startup check failed: %s", e)
    # side-by-side scoring test: self-test the copied scorer at startup; on failure the new account stays OFF
    try:
        ok = newscore.selftest()
        st = dict(newscore.STATUS); st["version"] = newscore.version(); st["thresholds"] = list(newscore.thresholds())
    except Exception as e:
        ok, st = False, {"ok": False, "error": str(e)[:200]}
    st["enabled_in_config"] = bool(cfg.get("scoring", {}).get("enabled", False))
    set_state(con, "scoring_new_status", st)
    if ok:
        log.info("scoring self-test PASS: model %s, %s rows, worst diff %.2g, thresholds buy>=%s / soft-cap>=%s, new account %s",
                 st["version"], st.get("rows"), st.get("worst") or 0, st["thresholds"][0], st["thresholds"][1],
                 "ON" if st["enabled_in_config"] else "off in config")
        if st["enabled_in_config"]:
            trader.cash(con, cfg, "new")
            if get_state(con, "scoring_test_started") is None:
                set_state(con, "scoring_test_started", now_ts())
    else:
        log.error("scoring self-test FAILED (%s) - new-scoring account stays OFF", st.get("error") or f"worst diff {st.get('worst')}")
    set_state(con, "bot_started", now_ts()); set_state(con, "bot_pid", os.getpid()); con.commit(); con.close()
    signal.signal(signal.SIGTERM, lambda *a: STOP.set())
    import established  # PAPER-ONLY older-coin strategy: own thread, own cash/caps; never touches the real wallet
    threads = [threading.Thread(target=scan_loop, daemon=True), threading.Thread(target=position_loop, daemon=True),
               threading.Thread(target=established.loop, args=(STOP, load_config), daemon=True)]
    for th in threads: th.start()
    wd, next_hb = outage.Watchdog(), 0.0
    while not STOP.is_set():
        if time.time() >= next_hb:  # heartbeat (process alive) + scanner watchdog, once a minute
            next_hb = time.time() + 60
            try:
                con = db(); outage.heartbeat(con); wd.check(con, LAST_CYCLE["ts"]); con.commit(); con.close()
            except Exception as e:
                log.warning("heartbeat/watchdog failed: %s", e)
        STOP.wait(5)
    log.info("bot stopping")

if __name__ == "__main__":
    main()
