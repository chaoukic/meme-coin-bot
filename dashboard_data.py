"""Builds the read-only JSON the dashboard shows. Shared by web.py and export_snapshot.py."""
import json
from common import db, load_config, get_state, now_ts, toronto_midnight_ts
import trader
import live
import established
import newscore

def _targets(p, P):
    e = p["entry_price"]
    return {"tp1": e * (1 + P["take_profit_pct"] / 100), "tp2": e * (1 + P["take_profit2_pct"] / 100),
            "sl": e * (1 - P["stop_loss_pct"] / 100),
            "trail": (p["peak_price"] * (1 - P["trailing_stop_pct"] / 100))
                     if p["peak_price"] and p["peak_price"] >= e * (1 + P["trailing_activate_pct"] / 100) else None,
            "max_hold_until": p["opened_at"] + P["max_hold_hours"] * 3600}

def _max_dd(series):
    """Largest peak-to-trough fall in $ and % over an equity series."""
    peak, dd, ddp = None, 0.0, 0.0
    for v in series:
        peak = v if peak is None or v > peak else peak
        if peak and peak - v > dd:
            dd, ddp = peak - v, (peak - v) / peak * 100
    return dd, ddp

def scoring_block(con, cfg):
    """Side-by-side scoring test (SIDE_BY_SIDE_SPEC.md): one block per paper account + a comparison strip.
    current = the existing account (also the only one that can trade real money); new = scoring_v1 paper account."""
    P = cfg["paper"]
    started = get_state(con, "scoring_test_started")
    st = get_state(con, "scoring_new_status") or {}
    try:
        thr = list(newscore.thresholds()); ver = newscore.version()
    except Exception:
        thr, ver = None, None
    midnight = toronto_midnight_ts()
    out = {"model_version": ver, "selftest": {"ok": bool(st.get("ok")), "rows": st.get("rows"), "worst": st.get("worst"),
                                               "error": st.get("error")},
           "enabled": bool(cfg.get("scoring", {}).get("enabled")) and bool(st.get("ok")),
           "started": started, "thresholds": {"current_min_score": cfg["filters"]["min_score_to_buy"],
                                               "current_soft_cap_min": P.get("cap_min_score", 75),
                                               "new_buy": thr[0] if thr else None, "new_soft_cap_min": thr[1] if thr else None},
           "real_money_account": "current"}
    since = started or 0
    for acct in ("current", "new"):
        if acct == "new" and get_state(con, "new:cash") is None:
            out[acct] = None
            continue
        w = trader.W(acct)
        eq = trader.equity(con, cfg, acct); cs = trader.cash(con, cfg, acct)
        start = get_state(con, trader.K(acct, "starting_balance"), float(P["starting_balance_usd"]))
        ds = trader.day_status(con, cfg, eq, acct)
        opens = [dict(r) for r in con.execute(f"SELECT * FROM positions WHERE status='open' AND {w} ORDER BY opened_at DESC")]
        for p in opens:
            p["targets"] = _targets(p, P)
        closed = [dict(r) for r in con.execute(f"SELECT * FROM positions WHERE status='closed' AND {w} ORDER BY closed_at DESC LIMIT 200")]
        a = con.execute(f"""SELECT COUNT(*) n, SUM(pnl_usd>0) w, COALESCE(SUM(pnl_usd),0) pnl,
                            AVG(CASE WHEN pnl_usd>0 THEN pnl_usd END) aw, AVG(CASE WHEN pnl_usd<=0 THEN pnl_usd END) al
                            FROM positions WHERE status='closed' AND {w}""").fetchone()
        t = con.execute(f"""SELECT COUNT(*) n, SUM(pnl_usd>0) w, COALESCE(SUM(pnl_usd),0) pnl FROM positions
                            WHERE status='closed' AND opened_at>=? AND {w}""", (since,)).fetchone()
        eqs = [dict(r) for r in con.execute("SELECT ts,equity FROM equity WHERE scoring=? AND ts>? ORDER BY ts",
                                            (acct, now_ts() - 7 * 86400))]
        dd, ddp = _max_dd([r["equity"] for r in con.execute("SELECT equity FROM equity WHERE scoring=? AND ts>=? ORDER BY ts",
                                                            (acct, since))] + [eq])
        if len(eqs) > 1500:
            step = len(eqs) // 1500 + 1
            eqs = eqs[::step] + [eqs[-1]]
        dec = [dict(r) for r in con.execute(
            "SELECT * FROM decisions WHERE " + ("COALESCE(scoring,'current')='current'" if acct == "current" else "scoring='new'")
            + " ORDER BY id DESC LIMIT 80")]
        _dec_scores(dec)
        n = a["n"] or 0
        out[acct] = {
            "kpi": {"equity": eq, "cash": cs, "start": start, "pnl": eq - start, "pnl_pct": (eq / start - 1) * 100 if start else 0,
                    "day_pnl": ds["day_pnl"], "day_mode": ds["mode"], "day_mode_label": ds["label"],
                    "closed": n, "wins": a["w"] or 0, "win_rate": ((a["w"] or 0) / n * 100) if n else None,
                    "realized_pnl": a["pnl"], "expectancy": (a["pnl"] / n) if n else None,
                    "avg_win": a["aw"], "avg_loss": a["al"], "open": len(opens), "max_open": P["max_open_positions"],
                    "trades_today": con.execute(f"SELECT COUNT(*) FROM positions WHERE opened_at>=? AND {w}", (midnight,)).fetchone()[0],
                    "since_start": {"trades": t["n"] or 0, "win_rate": ((t["w"] or 0) / t["n"] * 100) if t["n"] else None,
                                    "pnl": t["pnl"], "max_drawdown_usd": dd, "max_drawdown_pct": ddp},
                    "real_money": acct == "current"},
            "open": opens, "closed": closed, "equity": eqs, "decisions": dec}
    if out.get("current") and out.get("new"):
        def coins(acct):
            return {r[0] for r in con.execute(f"SELECT DISTINCT token FROM positions WHERE opened_at>=? AND {trader.W(acct)}", (since,))}
        cc, nc = coins("current"), coins("new")
        out["comparison"] = {
            "since": started,
            "current": out["current"]["kpi"]["since_start"], "new": out["new"]["kpi"]["since_start"],
            "both_bought": len(cc & nc), "only_current_coins": len(cc - nc), "only_new_coins": len(nc - cc)}
    return out

def _dec_scores(rows):
    """Every decision carries numeric score_new / score_old (None if unknown)."""
    for d in rows:
        for k in ("score_new", "score_old"):
            v = d.get(k)
            try:
                d[k] = None if v is None else float(v)
            except (TypeError, ValueError):
                d[k] = None
    return rows

def build():
    cfg = load_config()
    con = db()
    try:
        start_bal = get_state(con, "starting_balance", cfg["paper"]["starting_balance_usd"])
        eq = trader.equity(con, cfg)
        cash = trader.cash(con, cfg)
        midnight = toronto_midnight_ts()
        closed = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='closed' AND COALESCE(strategy,'new')='new' AND scoring='current' ORDER BY closed_at DESC LIMIT 200")]
        allclosed = con.execute("SELECT COUNT(*) n, SUM(pnl_usd>0) w FROM positions WHERE status='closed' AND COALESCE(strategy,'new')='new' AND scoring='current'").fetchone()
        opens = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open' AND COALESCE(strategy,'new')='new' AND scoring='current' ORDER BY opened_at DESC")]
        P = cfg["paper"]
        for p in opens:
            p["targets"] = _targets(p, P)
        trades_today = con.execute("SELECT COUNT(*) FROM positions WHERE opened_at>=? AND COALESCE(strategy,'new')='new' AND scoring='current'", (midnight,)).fetchone()[0]
        scanned_today = con.execute("SELECT COUNT(DISTINCT token) FROM evaluations WHERE ts>=?", (midnight,)).fetchone()[0]
        evals_today = con.execute("SELECT COUNT(*) FROM evaluations WHERE ts>=?", (midnight,)).fetchone()[0]
        cycles = [dict(r) for r in con.execute("SELECT * FROM cycles WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 20")]
        for c in cycles:
            for k in ("funnel", "errors", "api_calls"):
                c[k] = json.loads(c[k]) if c[k] else None
        last_cycle = cycles[0] if cycles else None
        feed = []
        if last_cycle:
            for r in con.execute("""SELECT ts,token,symbol,url,result,stage,reasons,score,metrics,score_new,gate_current,gate_new FROM evaluations
                                    WHERE cycle_id=? ORDER BY CASE WHEN result='pass' THEN 0 WHEN stage IN ('score','holder_trend','holders','contract') THEN 1
                                    WHEN result LIKE 'reject%' THEN 2 ELSE 3 END, ts DESC LIMIT 300""", (last_cycle["id"],)):
                d = dict(r); d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
                feed.append(d)
        deep = [dict(r) for r in con.execute("""SELECT ts,token,symbol,url,result,stage,reasons,score,score_new,gate_current,gate_new FROM evaluations
                  WHERE stage IN ('contract','holders','holder_trend','score') AND ts>? ORDER BY ts DESC LIMIT 60""", (now_ts() - 6 * 3600,))]
        decisions = _dec_scores([dict(r) for r in con.execute("SELECT * FROM decisions WHERE COALESCE(scoring,'current')='current' ORDER BY id DESC LIMIT 80")])
        eqc = [dict(r) for r in con.execute("SELECT ts,equity FROM equity WHERE scoring='current' AND ts>? ORDER BY ts", (now_ts() - 7 * 86400,))]
        if len(eqc) > 1500:
            step = len(eqc) // 1500 + 1
            eqc = eqc[::step] + [eqc[-1]]
        watch = con.execute("SELECT COUNT(*) FROM tokens WHERE status='watch'").fetchone()[0]
        status = get_state(con, "scanner_status", {})
        paused = get_state(con, "paused_today", False)
        ds = trader.day_status(con, cfg, eq)
        mp = get_state(con, "manual_pause") or {}
        day_start = get_state(con, "day_start_equity", eq)
        try:
            live_block = live.dashboard(con, cfg)
        except Exception as e:
            live_block = {"active": False, "error": str(e)[:200]}
        # older-coin strategy (paper only): its own block; the fields above stay new-coin strategy only
        try:
            est_block = established.dashboard(con, cfg)
        except Exception as e:
            est_block = {"enabled": False, "error": str(e)[:200]}
        try:
            sc_block = scoring_block(con, cfg)
        except Exception as e:
            sc_block = {"enabled": False, "error": str(e)[:200]}
        strategies = {"new": established.stats(con, "new", start_bal, eq),
                      "established": (est_block.get("kpi") if "kpi" in est_block else established.stats(con, "established"))}
        gen = now_ts()
        # offline banner support (Oct 6): the site should warn when generated/last_cycle_ts/heartbeat_ts are older
        # than offline_after_sec compared with the viewer's clock (all unix seconds)
        health = {"last_cycle_ts": last_cycle["finished"] if last_cycle else None,
                  "heartbeat_ts": get_state(con, "bot_heartbeat"), "generated": gen, "offline_after_sec": 600}
        return {
            "generated": gen, "health": health, "live": live_block, "scoring": sc_block, "established": est_block, "strategies": strategies,
            "kpi": {"equity": eq, "cash": cash, "start": start_bal, "pnl": eq - start_bal,
                    "pnl_pct": (eq / start_bal - 1) * 100 if start_bal else 0,
                    "closed": allclosed["n"] or 0, "wins": allclosed["w"] or 0,
                    "win_rate": ((allclosed["w"] or 0) / allclosed["n"] * 100) if allclosed["n"] else None,
                    "open": len(opens), "max_open": P["max_open_positions"], "trades_today": trades_today,
                    "scanned_today": scanned_today, "evals_today": evals_today, "watchlist": watch,
                    "day_pnl": eq - day_start, "day_cap": day_start * P["daily_loss_cap_pct"] / 100, "paused": paused,
                    "day_mode": ds["mode"], "day_mode_label": ds["label"], "day_hard_cap": ds["hard_cap_usd"],
                    "cap_min_score": P.get("cap_min_score", 75), "cap_size_factor": P.get("cap_size_factor", 0.5),
                    "manual_pause": bool(mp.get("on")), "manual_pause_since": mp.get("since") if mp.get("on") else None},
            "status": status, "trader_heartbeat": get_state(con, "trader_heartbeat"),
            "bot_started": get_state(con, "bot_started"),
            "open": opens, "closed": closed, "cycles": cycles, "feed": feed, "deep": deep,
            "decisions": decisions, "equity": eqc, "config": cfg,
        }
    finally:
        con.close()
