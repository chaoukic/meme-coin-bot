"""Paper trader. FAKE MONEY ONLY: simulates fills against DexScreener prices.
No wallets, no keys, no orders are ever sent anywhere."""
import json, threading
from common import (get_state, set_state, decision, now_ts, toronto_date, fnum, log)
from scanner import ds_pairs
import scanner
import jupcheck
import trigstop
import notify
import priceguard as pg
import live
import rugcheck
import fills

LOCK = threading.Lock()
# Positions of the separate PAPER-only established-coin strategy (established.py, strategy='established') have
# their own cash, caps and exits; everything in this file works on the new-coin strategy's rows only.
NEW = "COALESCE(strategy,'new')='new'"
# Side-by-side scoring test (Oct 3, SIDE_BY_SIDE_SPEC.md): two paper accounts on the new-coin strategy.
#   'current' = the existing account (its state keys stay the old global ones; the only one that can trade real money)
#   'new'     = scoring_v1 account: same candidates, filters, RugCheck, guard, sizing, exits and fills; only the score
#               gate and ranking differ. Its state keys are prefixed "new:"; equity rows carry scoring='new'.
ACCOUNTS = ("current", "new")
CUR = NEW + " AND scoring='current'"

def W(acct):
    """SQL filter for one account's new-coin positions."""
    assert acct in ACCOUNTS
    return f"{NEW} AND scoring='{acct}'"

def K(acct, key):
    return key if acct == "current" else f"new:{key}"

def _capmin(P, acct):
    """Minimum score while the daily soft cap is on: current = cap_min_score (75); new = model's top-10% score (55.5)."""
    return P.get("cap_min_score", 75) if acct == "current" else __import__("newscore").thresholds()[1]

def TAGP(acct):
    return "" if acct == "current" else "[new scoring] "

def cash(con, cfg, acct="current"):
    c = get_state(con, K(acct, "cash"))
    if c is None:
        c = float(cfg["paper"]["starting_balance_usd"]) if acct == "current" else float(cfg.get("scoring", {}).get("starting_balance_usd", 1000))
        set_state(con, K(acct, "cash"), c)
        set_state(con, K(acct, "starting_balance"), c)
    return c

def liq_value(qty, price, P, liq=None, cfg=None):
    """Exit value after slippage+fee; with a known pool liquidity also capped by constant-product price impact."""
    if liq is None:
        return qty * price * (1 - P["slippage_pct"] / 100) * (1 - P["fee_pct"] / 100)
    return pg.sell_value(qty, price, P, pg.cfg_guard(cfg or {}), liq)

def equity(con, cfg, acct="current"):
    P = cfg["paper"]
    eq = cash(con, cfg, acct)
    for p in con.execute("SELECT remaining_qty,last_price,last_liq FROM positions WHERE status='open' AND " + W(acct)):
        eq += liq_value(p["remaining_qty"], p["last_price"] or 0, P, p["last_liq"], cfg)
    return eq

def day_mode(pnl, start, P):
    """'normal' | 'soft' | 'hard' from today's P&L vs day-start equity."""
    hard = start * P.get("daily_hard_stop_pct", 10) / 100
    soft = start * P["daily_loss_cap_pct"] / 100
    if pnl <= -hard:
        return "hard"
    if pnl <= -soft:
        return "soft"
    return "normal"

def day_status(con, cfg, eq=None, acct="current"):
    """Read-only view for dashboard / Telegram / status.sh: dict with mode, day_pnl, pct, soft/hard cap in $."""
    P = cfg["paper"]
    eq = equity(con, cfg, acct) if eq is None else eq
    start = get_state(con, K(acct, "day_start_equity"), eq) or eq
    pnl = eq - start
    mode = "hard" if get_state(con, K(acct, "paused_today"), False) else day_mode(pnl, start, P)
    return {"mode": mode, "day_pnl": pnl, "day_pnl_pct": pnl / start * 100 if start else 0,
            "soft_cap_usd": start * P["daily_loss_cap_pct"] / 100, "hard_cap_usd": start * P.get("daily_hard_stop_pct", 10) / 100,
            "label": MODE_LABEL[mode].format(score=_capmin(P, acct), pct=P.get("cap_size_factor", 0.5) * 100)}

MODE_LABEL = {"normal": "all entries allowed",
              "soft": "soft cap (only score >= {score} at {pct:.0f}% size)",
              "hard": "hard stop (no new entries until midnight Toronto)"}

def day_guard(con, cfg, acct="current"):
    """Returns (mode, day_pnl, soft_cap_usd, hard_cap_usd). mode: 'normal', 'soft' (soft daily cap: only high-score
    entries at reduced size) or 'hard' (hard floor: no new entries until Toronto midnight; latched for the day).
    Resets the day-start equity at Toronto midnight. State: day_mode, paused_today (= hard floor hit)."""
    P = cfg["paper"]
    today = toronto_date()
    eq = equity(con, cfg, acct)
    if get_state(con, K(acct, "day")) != today:
        if get_state(con, K(acct, "paused_today"), False) or get_state(con, K(acct, "day_mode")) == "soft":
            notify.enqueue(con, K(acct, f"resume:{today}"), TAGP(acct) + f"▶️ New day ({today}): daily loss limits reset, normal paper entries resumed."
                           + (" (Still manually paused via /pause.)" if (get_state(con, "manual_pause") or {}).get("on") else ""))
        set_state(con, K(acct, "day"), today)
        set_state(con, K(acct, "day_start_equity"), eq)
        set_state(con, K(acct, "paused_today"), False)
        set_state(con, K(acct, "day_mode"), "normal")
    start = get_state(con, K(acct, "day_start_equity"), eq)
    soft = start * P["daily_loss_cap_pct"] / 100
    hard = start * P.get("daily_hard_stop_pct", 10) / 100
    pnl = eq - start
    mode = "hard" if get_state(con, K(acct, "paused_today"), False) else day_mode(pnl, start, P)
    prev = get_state(con, K(acct, "day_mode"), "normal")
    if mode == "hard" and not get_state(con, K(acct, "paused_today"), False):
        set_state(con, K(acct, "paused_today"), True)
        decision(con, "PAUSE", None, None, TAGP(acct) + f"Daily hard floor hit: day P&L ${pnl:,.2f} <= -${hard:,.2f} "
                 f"(-{P.get('daily_hard_stop_pct', 10)}%). No new entries until tomorrow (Toronto).", scoring=None if acct == "current" else "new")
        notify.enqueue(con, K(acct, f"hardstop:{today}"), TAGP(acct) + f"⛔ Daily hard floor hit: today's P&L {notify.fmt_usd(pnl)} "
                       f"(floor -{notify.fmt_usd(hard)}, -{P.get('daily_hard_stop_pct', 10)}%).\n"
                       f"No new paper entries at all until midnight Toronto time. Open positions are still managed.")
    elif mode == "soft" and prev != "soft":
        decision(con, "PAUSE", None, None, TAGP(acct) + f"Daily soft cap active: day P&L ${pnl:,.2f} <= -${soft:,.2f} "
                 f"(-{P['daily_loss_cap_pct']}%). Only {'score' if acct == 'current' else 'new score'} >= {_capmin(P, acct):g} entries, at "
                 f"{P.get('cap_size_factor', 0.5) * 100:.0f}% size.", scoring=None if acct == "current" else "new")
        notify.enqueue(con, K(acct, f"pause:{today}"), TAGP(acct) + f"⚠️ Daily loss cap hit: today's P&L {notify.fmt_usd(pnl)} (cap -{notify.fmt_usd(soft)}).\n"
                       f"Only high-score trades ({'score' if acct == 'current' else 'new score'} ≥ {_capmin(P, acct):g}) are allowed now, at "
                       f"{P.get('cap_size_factor', 0.5) * 100:.0f}% of normal size. All new entries stop if today's loss reaches "
                       f"-{P.get('daily_hard_stop_pct', 10)}% ({notify.fmt_usd(-hard)}). Open positions are still managed.")
    elif mode == "normal" and prev == "soft":
        decision(con, "RESUME", None, None, TAGP(acct) + f"Daily soft cap lifted: day P&L ${pnl:,.2f} back above -${soft:,.2f}. Normal entries.", scoring=None if acct == "current" else "new")
    if mode != prev:
        set_state(con, K(acct, "day_mode"), mode)
    return mode, pnl, soft, hard

def _skip(con, c, msg, acct="current"):
    # avoid repeating the same skip message every cycle (per account)
    if acct == "current":
        q = "SELECT 1 FROM decisions WHERE kind='SKIP' AND token=? AND ts>? AND COALESCE(scoring,'current')='current'"
    else:
        q = "SELECT 1 FROM decisions WHERE kind='SKIP' AND token=? AND ts>? AND scoring='new'"
    if not con.execute(q, (c["token"], now_ts() - 1800)).fetchone():
        decision(con, "SKIP", c["token"], c["symbol"], TAGP(acct) + msg, scoring=None if acct == "current" else "new",
                 **_cscores(c))

def _cscores(c):
    """score_new / score_old of a scan candidate, for the decision log."""
    return {"score_new": c.get("score_new"), "score_old": c.get("score")}

def _pscores(pos):
    """score_new / score_old of a position row (score_current is the old score; very old rows only have 'score')."""
    k = pos.keys()
    old = pos["score_current"] if "score_current" in k else None
    if old is None and (pos["scoring"] if "scoring" in k else "current") in (None, "current") and \
            ("strategy" not in k or (pos["strategy"] or "new") == "new"):
        old = pos["score"] if "score" in k else None
    return {"score_new": pos["score_new"] if "score_new" in k else None, "score_old": old}

def gate(c, acct):
    """Did this candidate pass this account's score gate? (candidates without gate info = old callers = current pass)"""
    if acct == "current":
        return c.get("gate_current", "pass") == "pass"
    return c.get("gate_new") == "pass"

def try_entries(con, cfg, candidates, acct="current"):
    """Entries for one paper account. acct='current' (score >= min_score_to_buy; may also start a REAL buy) or
    acct='new' (scoring_v1 gate; PAPER ONLY - never calls live.on_buy). Same filters, RugCheck, guard, sizing, fills."""
    P = cfg["paper"]
    SN = cfg.get("scoring", {})
    candidates = [c for c in candidates if gate(c, acct)]
    if not candidates:
        return 0
    with LOCK:
        mode, pnl, cap, hard = day_guard(con, cfg, acct)
        # The paper daily caps do not block the REAL-money test: it has its own daily stop (live.day_check).
        # While paper is capped, an entry is still taken (normal rules and size) only if the real buy would go through.
        live_ok = acct == "current" and mode != "normal" and live.can_buy(con, cfg)
        opened = 0
        G = pg.cfg_guard(cfg)
        skey = "score" if acct == "current" else "score_new"
        soft_min = _capmin(P, acct)
        for c in sorted(candidates, key=lambda x: -(x.get(skey) or 0)):
            # one position per coin: same mint or same pool (and, by default, same ticker) is blocked while
            # open and for the re-entry cooldown after it closed
            since = now_ts() - P["reentry_cooldown_hours"] * 3600
            q = "SELECT id, symbol, status FROM positions WHERE (token=? OR pair=?"
            args = [c["token"], c["pair"]]
            if G["block_same_symbol"] and c.get("symbol"):
                q += " OR UPPER(symbol)=UPPER(?)"
                args.append(c["symbol"])
            q += ") AND (status='open' OR closed_at>?) AND " + W(acct)
            recent = con.execute(q, (*args, since)).fetchone()
            if recent:
                if recent["status"] == "open":
                    _skip(con, c, f"already holding {recent['symbol']} (position #{recent['id']}) - one position per coin", acct)
                continue  # cooling down (silent: happens every cycle)
            sc_txt = f"score {c['score']}" if acct == "current" else f"new score {c.get('score_new')}"
            n_open = con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND " + W(acct)).fetchone()[0]
            if n_open >= P["max_open_positions"]:
                _skip(con, c, f"passed ({sc_txt}) but max open positions ({P['max_open_positions']}) reached", acct)
                continue
            emode = "normal" if live_ok else mode
            if emode == "hard":
                _skip(con, c, f"passed ({sc_txt}) but daily hard floor (-{P.get('daily_hard_stop_pct', 10)}%) hit - no new entries today", acct)
                continue
            if emode == "soft" and (c.get(skey) or 0) < soft_min:
                _skip(con, c, f"{'score' if acct == 'current' else 'new score'} {c.get(skey) or 0:g} < {soft_min:g} (daily soft cap active)", acct)
                continue
            if (get_state(con, "manual_pause") or {}).get("on"):
                _skip(con, c, f"passed ({sc_txt}) but new entries are paused (Telegram /pause)", acct)
                continue
            rc_ok, rc_msg = rugcheck.check(c["token"], cfg, c["pair"])
            if not rc_ok:
                _skip(con, c, f"passed ({sc_txt}) but rug check failed: {rc_msg}", acct)
                continue
            eq = equity(con, cfg, acct)
            cs = cash(con, cfg, acct)
            base = eq * P["position_pct_of_balance"] / 100
            if emode == "soft":
                base *= P.get("cap_size_factor", 0.5)
            size = min(base, c["liq"] * P["max_pct_of_pool_liquidity"] / 100, cs)
            if size < 1:
                decision(con, "SKIP", c["token"], c["symbol"], TAGP(acct) + f"size ${size:.2f} too small (cash ${cs:.2f})",
                         scoring=None if acct == "current" else "new", **_cscores(c))
                continue
            # fresh re-read of the same pool right before buying; never buy on a stale or odd reading
            price, liq = c["price"], c["liq"]
            entry_source, entry_check = "dexscreener", None
            if G["enabled"]:
                fresh = ds_pairs([c["pair"]]).get(c["pair"])
                ds_busy = not fresh and c["pair"] in scanner.DS_FAILED
                if ds_busy and jupcheck.enabled(cfg):
                    # DexScreener's re-read failed (429 / error): Jupiter reading + live quotes instead (jupcheck.py)
                    rc = rugcheck.cfg_rc(cfg)
                    if not (rc["enabled"] and rc["fail_closed"]):  # RugCheck is mandatory on this path, fail-closed
                        strict = dict(cfg, rugcheck=dict(cfg.get("rugcheck", {}), enabled=True, fail_closed=True))
                        rc_ok, rc_msg = rugcheck.check(c["token"], strict, c["pair"])
                        if not rc_ok:
                            _skip(con, c, f"passed ({sc_txt}) but rug check failed: {rc_msg}", acct)
                            continue
                    try:
                        jc = jupcheck.prebuy(cfg, c)
                    except Exception as e:
                        jc = {"ok": False, "reason": f"Jupiter check error: {e}", "details": {}}
                    log.info("pre-buy fallback %s: %s", c["symbol"], jc["reason"])
                    if not jc["ok"]:
                        pg.reject(con, c["token"], c["symbol"], TAGP(acct) + f"buy skipped: DexScreener busy, Jupiter check failed: {jc['reason']}")
                        continue
                    fp, fl = jc["price"], jc["liq"]
                    entry_source = "jup_fallback"
                    entry_check = json.dumps(dict(jc["details"], reason=jc["reason"]))
                else:
                    fp = fnum((fresh or {}).get("priceUsd"))
                    fl = fnum(((fresh or {}).get("liquidity") or {}).get("usd"))
                    if not fresh or (fresh.get("baseToken") or {}).get("address") != c["token"] or not fp:
                        pg.reject(con, c["token"], c["symbol"], TAGP(acct) + "buy skipped: could not re-read the same pool/coin right before buying")
                        continue
                    if abs(fp / price - 1) * 100 > G["entry_price_tolerance_pct"]:
                        pg.reject(con, c["token"], c["symbol"], TAGP(acct) + f"buy skipped: fresh price ${fp:.4g} differs {abs(fp / price - 1) * 100:.0f}% from scan price ${price:.4g}")
                        continue
                    if not fl or fl < cfg["filters"]["min_liquidity_usd"]:
                        pg.reject(con, c["token"], c["symbol"], TAGP(acct) + f"buy skipped: fresh liquidity ${fl or 0:,.0f} below the minimum")
                        continue
                src_name = "Jupiter" if entry_source == "jup_fallback" else "DexScreener"
                if G["use_second_source"]:
                    gt = pg.gt_pool(c["pair"], c["token"])
                    if gt and gt["price"] and not pg.agree(fp, gt["price"], G["confirm_tolerance_pct"]):
                        pg.reject(con, c["token"], c["symbol"], TAGP(acct) + f"buy skipped: {src_name} ${fp:.4g} vs GeckoTerminal ${gt['price']:.4g} disagree")
                        continue
                price, liq = fp, fl
                size = min(size, liq * P["max_pct_of_pool_liquidity"] / 100)
            eff = price * pg.buy_price_multiplier(size, liq, P, G)
            qty = size * (1 - P["fee_pct"] / 100) / eff
            con.execute("""INSERT INTO positions(token,pair,symbol,url,opened_at,entry_price,entry_eff,qty,remaining_qty,
                cost_usd,peak_price,last_price,last_update,status,score,entry_liq,pnl_usd,pnl_pct,last_liq,guard_last_seen,
                entry_source,entry_check,scoring,score_current,score_new,prob_new,model_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,0,0,?,?,?,?,?,?,?,?,?)""",
                        (c["token"], c["pair"], c["symbol"], c["url"], now_ts(), price, eff, qty, qty, size,
                         price, price, now_ts(), c["score"] if acct == "current" else c.get("score_new"), liq, liq, now_ts(),
                         entry_source, entry_check, acct, c.get("score"), c.get("score_new"), c.get("prob_new"), c.get("model_version")))
            pos_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            set_state(con, K(acct, "cash"), cs - size)
            soft_note = f" - reduced size {P.get('cap_size_factor', 0.5) * 100:.0f}% (daily soft cap)" if emode == "soft" else ""
            fb_note = " [entry_source=jup_fallback: DexScreener busy, checked via Jupiter quote]" if entry_source == "jup_fallback" else ""
            if emode != mode:
                soft_note += f" - paper daily {mode} cap is on, but taken so the real-money test can trade (real wallet has its own daily stop)"
            decision(con, "BUY", c["token"], c["symbol"], TAGP(acct) +
                     f"PAPER BUY ${size:,.2f} @ ${price:.10g} (eff ${eff:.10g} after slippage/price impact + {P['fee_pct']}% fee), "
                     f"score {c['score']} / new score {c.get('score_new')}, liq ${liq:,.0f}" + soft_note + fb_note,
                     scoring=None if acct == "current" else "new", **_cscores(c))
            if acct == "current":  # REAL money only ever follows the current-scoring account (live.py re-checks scoring too)
                real = live.on_buy(con, cfg, pos_id, c["token"], c["symbol"])
                real_line = (f"\n💵 REAL MONEY: yes, buying ${float(live.L(cfg)['trade_usd']):.2f} of {c['symbol']} with real money too "
                             "(confirmation follows)" if real is True else f"\n📝 Paper only, no real money ({real})")
            else:
                real_line = f"\n📝 Paper only - new-scoring test account (new score {c.get('score_new')}, current score {c.get('score')})"
            notify.enqueue(con, f"buy:{pos_id}", notify.fmt_buy(c["symbol"], price, size, c["score"] if acct == "current" else c.get("score_new"),
                                                             c["url"], equity(con, cfg, acct), via_jupiter=entry_source == "jup_fallback",
                                                             tag="PAPER" if acct == "current" else "PAPER (new scoring)",
                                                             balance_label="Fake balance" if acct == "current" else "New-scoring fake balance",
                                                             label=notify.scoring_label(acct),
                                                             scores=(c.get("score"), c.get("score_new")) if c.get("score") is not None and c.get("score_new") is not None else None)
                           + (f"\n⚠️ Reduced size {P.get('cap_size_factor', 0.5) * 100:.0f}% (daily soft cap active)" if soft_note and emode == "soft" else "")
                           + real_line)
            opened += 1
            if live_ok and not live.can_buy(con, cfg):
                live_ok = False
        con.commit()
        return opened

def _sell(con, cfg, pos, qty, price, reason, liq=None):
    P = cfg["paper"]
    acct = pos["scoring"] if "scoring" in pos.keys() and pos["scoring"] else "current"
    if liq is None:
        liq = pos["last_liq"]
    proceeds = liq_value(qty, price, P, liq, cfg)
    if acct == "current":  # real money only ever follows the current-scoring account
        live.on_sell(con, cfg, pos["id"], pos["token"], pos["symbol"],
                     1.0 if qty >= pos["remaining_qty"] * (1 - 1e-9) else qty / pos["remaining_qty"], reason)
    remaining = pos["remaining_qty"] - qty
    total_proceeds = (pos["proceeds_usd"] or 0) + proceeds
    set_state(con, K(acct, "cash"), cash(con, cfg, acct) + proceeds)
    tagkw = {"tag": "PAPER", "label": notify.scoring_label(acct)} if acct == "current" else {"tag": "PAPER (new scoring)", "balance_label": "New-scoring fake balance", "label": notify.scoring_label(acct)}
    if remaining <= pos["qty"] * 1e-9:
        pnl = total_proceeds - pos["cost_usd"]
        con.execute("""UPDATE positions SET remaining_qty=0, proceeds_usd=?, status='closed', closed_at=?, exit_reason=?,
                       pnl_usd=?, pnl_pct=?, last_price=? WHERE id=?""",
                    (total_proceeds, now_ts(), reason, pnl, pnl / pos["cost_usd"] * 100, price, pos["id"]))
        decision(con, "SELL", pos["token"], pos["symbol"], TAGP(acct) +
                 f"PAPER SELL ALL @ ${price:.10g}: {reason}. Trade P&L ${pnl:+,.2f} ({pnl / pos['cost_usd'] * 100:+.1f}%)",
                 scoring=None if acct == "current" else "new", **_pscores(pos))
        notify.enqueue(con, f"sell:{pos['id']}:full", notify.fmt_sell(pos["symbol"], reason, pnl, pnl / pos["cost_usd"] * 100,
                                                                     equity(con, cfg, acct), url=pos["url"], **tagkw))
    else:
        con.execute("UPDATE positions SET remaining_qty=?, proceeds_usd=?, tp1_done=1 WHERE id=?",
                    (remaining, total_proceeds, pos["id"]))
        decision(con, "SELL", pos["token"], pos["symbol"], TAGP(acct) +
                 f"PAPER PARTIAL SELL {qty / pos['qty'] * 100:.0f}% @ ${price:.10g}: {reason} (+${proceeds:,.2f})",
                 scoring=None if acct == "current" else "new", **_pscores(pos))
        sold_cost = pos["cost_usd"] * qty / pos["qty"]
        part_pnl = proceeds - sold_cost
        notify.enqueue(con, f"sell:{pos['id']}:partial:{int(pos['tp1_done'] or 0)}",
                       notify.fmt_sell(pos["symbol"], f"{reason} - sold {qty / pos['qty'] * 100:.0f}% of the position",
                                       part_pnl, part_pnl / sold_cost * 100, equity(con, cfg, acct), partial=True, url=pos["url"], **tagkw))

def check_exits(con, cfg, pos, pair):
    P = cfg["paper"]
    G = pg.cfg_guard(cfg)
    verdict, price, liq = pg.check_reading(con, cfg, pos, pair)
    fills.tick(con, pos, price if price else pair.get("priceUsd"), pair.get("_source") or "dexscreener",
               liq if price else (pair.get("liquidity") or {}).get("usd"), verdict)  # recording only (position_ticks)
    if verdict == "reject" or not price:
        return  # bad/unconfirmed reading: no price update, no TP/SL/trailing decision
    if verdict == "drained":
        con.execute("UPDATE positions SET last_price=?, last_liq=?, last_update=? WHERE id=?", (price, liq, now_ts(), pos["id"]))
        pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        return _sell(con, cfg, pos, pos["remaining_qty"], price,
                     f"rug: liquidity pulled (pool liquidity ${liq:,.0f}, confirmed on 2 checks; fill capped by pool)", liq)
    peak = max(pos["peak_price"] or price, price)
    con.execute("UPDATE positions SET last_price=?, peak_price=?, last_liq=?, last_update=? WHERE id=?",
                (price, peak, liq, now_ts(), pos["id"]))
    pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
    if (pos["scoring"] if "scoring" in pos.keys() else "current") == "current":
        trigstop.on_trail(con, cfg, pos)  # REAL position with a Jupiter stop: move it up with the trailing rule (never down)
    gain = (price / pos["entry_price"] - 1) * 100
    held_min = (now_ts() - pos["opened_at"]) / 60
    if gain <= -P["stop_loss_pct"]:
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"stop loss ({gain:.1f}% <= -{P['stop_loss_pct']}%)")
    if gain >= P["take_profit2_pct"]:
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"take profit 2 (+{gain:.1f}%)")
    if gain >= P["take_profit_pct"] and not pos["tp1_done"]:
        frac = P["take_profit_sell_fraction"]
        _sell(con, cfg, pos, pos["remaining_qty"] * frac if frac < 1 else pos["remaining_qty"], price,
              f"take profit 1 (+{gain:.1f}% >= +{P['take_profit_pct']}%)")
        return
    peak_gain = (peak / pos["entry_price"] - 1) * 100
    drop = (1 - price / peak) * 100
    if peak_gain >= P["trailing_activate_pct"] and drop >= P["trailing_stop_pct"]:
        return _sell(con, cfg, pos, pos["remaining_qty"], price,
                     f"trailing stop ({drop:.1f}% off peak, peak was +{peak_gain:.1f}%)")
    # volume decay: recent volume vs its average pace, scaled for young tokens
    if held_min >= P["volume_decay_min_hold_min"]:
        created = pair.get("pairCreatedAt")
        age_h = (now_ts() - created / 1000) / 3600 if created else 24
        vol = pair.get("volume") or {}
        v1, v6, v24 = fnum(vol.get("h1"), 0), fnum(vol.get("h6"), 0), fnum(vol.get("h24"), 0)
        ratio = P["volume_decay_ratio"]
        vd = None
        if age_h >= 12:
            base = v24 * 6 / min(age_h, 24)
            if base > 0 and v6 < ratio * base:
                vd = f"volume decay (6h vol ${v6:,.0f} < {ratio*100:.0f}% of avg 6h pace ${base:,.0f})"
        elif age_h >= 2:
            base = v6 / min(age_h, 6)
            if base > 0 and v1 < ratio * base:
                vd = f"volume decay (1h vol ${v1:,.0f} < {ratio*100:.0f}% of avg hourly pace ${base:,.0f})"
        if vd:
            prev = pos["guard_vd_ts"]
            if not G["volume_decay_confirm"] or (prev and now_ts() - prev <= G["confirm_window_sec"]):
                con.execute("UPDATE positions SET guard_vd_ts=NULL WHERE id=?", (pos["id"],))
                return _sell(con, cfg, pos, pos["remaining_qty"], price, vd + (", confirmed on 2 checks" if G["volume_decay_confirm"] else ""))
            con.execute("UPDATE positions SET guard_vd_ts=? WHERE id=?", (now_ts(), pos["id"]))
            log.info("%s: %s - waiting for next check to confirm", pos["symbol"], vd)
        elif pos["guard_vd_ts"]:
            con.execute("UPDATE positions SET guard_vd_ts=NULL WHERE id=?", (pos["id"],))
    if held_min >= P["max_hold_hours"] * 60:
        return _sell(con, cfg, pos, pos["remaining_qty"], price, f"max hold time {P['max_hold_hours']}h")

def jup_backup_pairs(cfg, positions):
    """Backup price for held coins when DexScreener is rate-limited: Jupiter price API using Sammy's key.
    Returns DexScreener-shaped readings so the price guard still checks them. Jupiter's liquidity is for the
    whole coin, not our pool, so it is only passed on when it shows the coin is clearly dead; otherwise the
    last known pool liquidity is used."""
    out = {}
    try:
        import live, requests
        key = live.jup_key()
        if not key:
            return out
        ids = ",".join(p["token"] for p in positions)
        r = requests.get("https://api.jup.ag/price/v3", params={"ids": ids}, headers={"x-api-key": key}, timeout=15)
        if r.status_code != 200:
            log.warning("Jupiter backup price HTTP %s", r.status_code)
            return out
        j = r.json() or {}
        min_liq = float(cfg.get("guard", {}).get("min_exit_liquidity_usd", 1000))
        for p in positions:
            d = j.get(p["token"])
            if not d or not d.get("usdPrice"):
                continue
            jl = d.get("liquidity")
            liq = jl if (jl is not None and jl < min_liq) else (p["last_liq"] or p["entry_liq"])
            out[p["pair"]] = {"pairAddress": p["pair"], "baseToken": {"address": p["token"]},
                              "priceUsd": str(d["usdPrice"]), "liquidity": {"usd": liq}, "_source": "jupiter"}
            log.info("backup price for %s from Jupiter: $%.6g (DexScreener busy)", p["symbol"], d["usdPrice"])
    except Exception as e:
        log.warning("Jupiter backup price failed: %s", e)
    return out

def update_positions(con, cfg):
    with LOCK:
        # both scoring accounts' positions: ONE price reading per pool, exits for both in the same pass
        opens = con.execute("SELECT * FROM positions WHERE status='open' AND " + NEW).fetchall()
        if opens:
            pairs = ds_pairs(list(dict.fromkeys(p["pair"] for p in opens)))
            missing = [p for p in opens if p["pair"] not in pairs]
            if missing:
                pairs.update(jup_backup_pairs(cfg, list({p["pair"]: p for p in missing}.values())))
            for p in opens:
                if p["pair"] in pairs:
                    check_exits(con, cfg, p, pairs[p["pair"]])
                else:
                    log.warning("no price for %s this update", p["symbol"])
        try:
            trigstop.check(con, cfg)  # Jupiter stop orders on REAL positions: detect fills / expiry (background, rate-limited)
        except Exception as e:
            log.warning("trigger stop check failed: %s", e)
        # refresh P&L on remaining open positions
        P = cfg["paper"]
        for p in con.execute("SELECT * FROM positions WHERE status='open' AND " + NEW).fetchall():
            val = (p["proceeds_usd"] or 0) + liq_value(p["remaining_qty"], p["last_price"] or 0, P, p["last_liq"], cfg)
            con.execute("UPDATE positions SET pnl_usd=?, pnl_pct=? WHERE id=?",
                        (val - p["cost_usd"], (val / p["cost_usd"] - 1) * 100, p["id"]))
        for acct in accounts_live(con, cfg):
            day_guard(con, cfg, acct)
            eq = equity(con, cfg, acct)
            last = con.execute("SELECT ts FROM equity WHERE scoring=? ORDER BY ts DESC LIMIT 1", (acct,)).fetchone()
            if not last or now_ts() - last["ts"] >= 55:
                con.execute("INSERT INTO equity(ts,equity,cash,scoring) VALUES(?,?,?,?)", (now_ts(), eq, cash(con, cfg, acct), acct))
        con.commit()

def accounts_live(con, cfg):
    """'current' always; 'new' while the scoring test is on (self-test passed) or it still has open positions."""
    import newscore
    out = ["current"]
    if newscore.enabled(cfg) or get_state(con, "new:cash") is not None:
        out.append("new")
    return out
