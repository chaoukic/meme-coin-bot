"""Telegram companion process: sends queued alerts and answers Sammy's commands.
PAPER TRADING ONLY. Only the saved chat id gets answers; everyone else is ignored.
The bot token is read from env/host secrets into memory only and is never printed or logged."""
import json, logging, os, sys, time
from datetime import datetime
import requests
from common import BASE, db, init_db, load_config, get_state, set_state, now_ts, TZ
import notify, trader

CHAT_FILE = os.path.join(BASE, "telegram_chat.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("telegram")
HELP = ("Memebot commands (paper trading only, fake money):\n"
        "/status - fake balance, P&L, open positions, scanner\n"
        "/positions - open paper positions with live-ish P&L\n"
        "/pause - stop opening new paper positions (open ones are still managed)\n"
        "/resume - allow new paper positions again\n"
        "/dashboard - link to the dashboard\n"
        "/live - REAL-money test: on/off, budget used, wallet balance\n"
        "/livestop - emergency stop for REAL buys (sells still run)\n"
        "/help - this list")

# ---------------------------------------------------------------- Telegram API (token never logged)
class TG:
    def __init__(self):
        self.s = requests.Session()
    def call(self, method, http_timeout=30, **params):
        tok = notify.telegram_token()
        if not tok:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not available")
        try:
            r = self.s.post(f"https://api.telegram.org/bot{tok}/{method}", json=params, timeout=http_timeout)
            j = r.json()
        except Exception as e:  # exception text can contain the URL -> redact
            raise RuntimeError(notify.redact(f"{type(e).__name__}: {e}")) from None
        return j

# ---------------------------------------------------------------- chat id
def load_chat():
    try:
        with open(CHAT_FILE) as f:
            return json.load(f).get("chat_id")
    except Exception:
        return None

def save_chat(chat):
    with open(CHAT_FILE, "w") as f:
        json.dump({"chat_id": chat["id"], "username": chat.get("username"), "first_name": chat.get("first_name"),
                   "saved_at": datetime.now(TZ).isoformat(timespec="seconds")}, f, indent=2)

# ---------------------------------------------------------------- command handlers (pure-ish, testable)
def t_local(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%b %d %H:%M ET") if ts else "never"

def positions_text(con, cfg):
    P = cfg["paper"]
    rows = con.execute("SELECT * FROM positions WHERE status='open' AND COALESCE(strategy,'new')='new' AND scoring='current' ORDER BY opened_at").fetchall()
    est = _estab_positions(con, cfg)
    if not rows:
        return "No open paper positions." + est
    out = []
    for p in rows:
        val = (p["proceeds_usd"] or 0) + trader.liq_value(p["remaining_qty"], p["last_price"] or 0, P, p["last_liq"], cfg)
        pnl = val - p["cost_usd"]
        age = (now_ts() - p["opened_at"]) / 60
        out.append(f"• {p['symbol']}: {notify.fmt_usd(pnl)} ({pnl / p['cost_usd'] * 100:+.1f}%), "
                   f"entry {notify.fmt_price(p['entry_price'])} → {notify.fmt_price(p['last_price'] or 0)}, "
                   f"size {notify.fmt_usd(p['cost_usd'])}, {age:.0f}m old" + (" (TP1 taken)" if p["tp1_done"] else "")
                   + (f"\n  {p['url']}" if p["url"] else ""))
    return "Open paper positions (prices from the bot's last update, ≤1 min old):\n" + "\n".join(out) + est

def _estab_positions(con, cfg):
    """Older-coin strategy (PAPER only) positions, listed separately."""
    try:
        import established
        e = established.E(cfg)
        rows = con.execute("SELECT * FROM positions WHERE status='open' AND strategy='established' ORDER BY opened_at").fetchall()
        if not rows:
            return ""
        out = []
        for p in rows:
            val = (p["proceeds_usd"] or 0) + established.value(p["remaining_qty"], p["last_price"] or 0, e, p["last_liq"], cfg)
            pnl = val - p["cost_usd"]
            out.append(f"• {p['symbol']}: {notify.fmt_usd(pnl)} ({pnl / p['cost_usd'] * 100:+.1f}%), "
                       f"entry {notify.fmt_price(p['entry_price'])} → {notify.fmt_price(p['last_price'] or 0)}, "
                       f"size {notify.fmt_usd(p['cost_usd'])}, {(now_ts() - p['opened_at']) / 3600:.1f}h old"
                       + (" (TP1 taken)" if p["tp1_done"] else ""))
        return "\n\nPAPER (older coin) positions:\n" + "\n".join(out)
    except Exception:
        return ""

def _newscore_status(con, cfg):
    """Side-by-side scoring test: one line for the second (new-scoring, PAPER-only) account."""
    try:
        if get_state(con, "new:cash") is None:
            return ""
        eq = trader.equity(con, cfg, "new")
        start = get_state(con, "new:starting_balance", 1000) or 1000
        c = con.execute("SELECT COUNT(*) n, SUM(pnl_usd>0) w FROM positions WHERE status='closed' AND " + trader.W("new")).fetchone()
        n_open = con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND " + trader.W("new")).fetchone()[0]
        ds = trader.day_status(con, cfg, eq, "new")
        return (f"New scoring test (PAPER only, separate $1,000): {notify.fmt_usd(eq)} · P&L {notify.fmt_usd(eq - start)} "
                f"({(eq / start - 1) * 100:+.2f}%) · {n_open} open · {c['n'] or 0} closed ({c['w'] or 0} wins)"
                + (" · ⚠️ soft cap" if ds["mode"] == "soft" else " · ⛔ daily stop hit" if ds["mode"] == "hard" else ""))
    except Exception:
        return ""

def _estab_status(con, cfg):
    try:
        import established
        if not established.enabled(cfg) and not con.execute("SELECT 1 FROM positions WHERE strategy='established' LIMIT 1").fetchone():
            return ""
        k = established.dashboard(con, cfg)["kpi"]
        return (f"Older coins (PAPER only, separate pot): {notify.fmt_usd(k['equity'])} · P&L {notify.fmt_usd(k['pnl'])} "
                f"({k['pnl_pct']:+.2f}%) · {k['open']} open · {k['closed']} closed ({k['wins']} wins)"
                + (" · ⛔ daily stop hit" if k["day_stopped"] else ""))
    except Exception:
        return ""

def status_text(con, cfg):
    eq = trader.equity(con, cfg)
    start = get_state(con, "starting_balance", cfg["paper"]["starting_balance_usd"])
    pnl = eq - start
    closed = con.execute("SELECT COUNT(*) n, SUM(pnl_usd>0) w FROM positions WHERE status='closed' AND COALESCE(strategy,'new')='new' AND scoring='current'").fetchone()
    st = get_state(con, "scanner_status", {}) or {}
    mp = get_state(con, "manual_pause") or {}
    day_start = get_state(con, "day_start_equity", eq)
    lines = [
        "📊 Memebot status (PAPER, fake money)",
        f"Fake balance {notify.fmt_usd(eq)} · total P&L {notify.fmt_usd(pnl)} ({pnl / start * 100:+.2f}%)",
        f"Today {notify.fmt_usd(eq - day_start)} · closed trades {closed['n'] or 0} ({closed['w'] or 0} wins)",
        f"Scanner: {st.get('state', 'unknown')}, last run {t_local(st.get('last_run'))}",
        "Daily loss mode: " + {"normal": "✅ ", "soft": "⚠️ ", "hard": "⛔ "}[(ds := trader.day_status(con, cfg))["mode"]]
            + ds["label"] + f" (today {ds['day_pnl_pct']:+.2f}%; soft cap -{cfg['paper']['daily_loss_cap_pct']}%, "
            f"hard stop -{cfg['paper'].get('daily_hard_stop_pct', 10)}%)",
        "New entries: " + ("⏸ PAUSED via /pause" if mp.get("on") else
                           "⛔ none today (daily hard stop)" if ds["mode"] == "hard" else
                           "⚠️ high-score only, reduced size (daily soft cap)" if ds["mode"] == "soft" else "✅ allowed"),
        _newscore_status(con, cfg),
        _estab_status(con, cfg),
        "",
        positions_text(con, cfg),
        "",
        f"Dashboard: {notify.DASHBOARD_URL}",
    ]
    return "\n".join(lines)

def live_text(con, cfg):
    import live
    try:
        sm = live.summary(con, cfg)
        try:
            bal = live.sol_balance(cfg); px = live.sol_price()
            wb = f"{bal:.4f} SOL (~{notify.fmt_usd(bal * px)})"
        except Exception:
            wb = "unavailable right now"
        return ("💵 REAL-money test: " + ("🟢 ON" if sm["active"] else "⚪ OFF") + "\n"
                f"Budget: {notify.fmt_usd(sm['spent_usd'])} of {notify.fmt_usd(sm['budget_usd'])} spent on buys, "
                f"{notify.fmt_usd(sm['returned_usd'])} back from sells\n"
                f"Open real positions: {sm['open']} · {notify.fmt_usd(sm['trade_usd'])} per trade\n"
                f"Test wallet: {wb}\nhttps://solscan.io/account/{sm['wallet']}")
    except Exception as e:
        return f"Could not read live status: {e}"

def handle(con, cfg, text):
    """Returns the reply text for a command from Sammy (None = ignore)."""
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if (text or "").strip() else ""
    if cmd in ("/start", "/help"):
        return HELP + f"\n\nDashboard: {notify.DASHBOARD_URL}"
    if cmd == "/status":
        return status_text(con, cfg)
    if cmd == "/positions":
        return positions_text(con, cfg)
    if cmd == "/dashboard":
        return f"Dashboard (updates every 5 min, live prices for open positions): {notify.DASHBOARD_URL}"
    if cmd == "/pause":
        if (get_state(con, "manual_pause") or {}).get("on"):
            return "Already paused. Use /resume to allow new paper positions."
        set_state(con, "manual_pause", {"on": True, "since": now_ts(), "by": "telegram"})
        con.commit()
        return "⏸ Paused: no new paper positions will be opened. Open positions are still managed (stops, take-profits). /resume to undo."
    if cmd == "/resume":
        if not (get_state(con, "manual_pause") or {}).get("on"):
            return "Not paused - new paper positions are allowed."
        set_state(con, "manual_pause", {"on": False, "since": now_ts(), "by": "telegram"})
        con.commit()
        m = trader.day_status(con, load_config())["mode"]
        extra = (" (Note: today's daily hard stop is still active - no new entries until midnight.)" if m == "hard" else
                 " (Note: daily soft cap active - only high-score entries at reduced size.)" if m == "soft" else "")
        return "▶️ Resumed: new paper positions allowed again." + extra
    if cmd == "/live":
        return live_text(con, cfg)
    if cmd == "/livestop":
        open(os.path.join(BASE, "logs", "live_kill"), "w").write(str(now_ts()))
        return "🛑 REAL buys stopped (emergency stop). Open real positions will still be sold when the bot exits them. Ask Trading Bot to turn it back on."
    if cmd.startswith("/"):
        return "Unknown command.\n\n" + HELP
    return None

# ---------------------------------------------------------------- main loop
def send(tg, chat_id, text):
    j = tg.call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)
    return j

def drain_outbox(tg, con, chat_id):
    notify.ensure(con)
    rows = con.execute("SELECT * FROM outbox WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 10", (now_ts(),)).fetchall()
    for r in rows:
        try:
            j = send(tg, chat_id, r["text"])
        except Exception as e:
            j = {"ok": False, "description": str(e)}
        if j.get("ok"):
            con.execute("UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?", (now_ts(), r["id"]))
            log.info("sent alert %s", r["key"])
        else:
            att = r["attempts"] + 1
            retry_after = ((j.get("parameters") or {}).get("retry_after") or 0)
            wait = max(retry_after, min(600, 5 * 2 ** att))
            status = "failed" if att >= 8 else "pending"
            con.execute("UPDATE outbox SET attempts=?, next_try=?, status=?, error=? WHERE id=?",
                        (att, now_ts() + wait, status, notify.redact(j.get("description"))[:300], r["id"]))
            log.warning("alert %s failed (attempt %s): %s", r["key"], att, notify.redact(j.get("description")))
            con.commit()
            break
        con.commit()
        time.sleep(1.1)  # stay well under Telegram's per-chat limit

def main():
    init_db()
    tg = TG()
    con = db()
    notify.ensure(con); con.commit()
    offset = get_state(con, "tg_offset")
    backoff = 1
    while True:
        try:
            cfg = load_config()
            chat_id = load_chat()
            params = {"timeout": 10, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            j = tg.call("getUpdates", 40, **params)
            if not j.get("ok"):
                raise RuntimeError(notify.redact(j.get("description")))
            for u in j.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = m.get("chat") or {}
                text = m.get("text") or ""
                if not chat_id and chat.get("type") == "private" and text.strip().lower().startswith("/start"):
                    save_chat(chat); chat_id = chat["id"]
                    log.info("saved chat id from first /start")
                    # don't flood Sammy with events that happened before he connected
                    con.execute("UPDATE outbox SET status='skipped' WHERE status='pending'")
                    notify.enqueue(con, f"connected:{chat_id}",
                                   f"✅ Memebot alerts connected. Paper trading only, fake money.\n"
                                   f"Fake balance: {notify.fmt_usd(trader.equity(con, cfg))}\n\n" + HELP)
                    con.commit()
                    continue
                if chat_id and chat.get("id") == chat_id:
                    reply = handle(con, cfg, text)
                    if reply:
                        send(tg, chat_id, reply)
                        log.info("answered command %s", text.split()[0][:20] if text else "")
                # anything else: ignored silently
            set_state(con, "tg_offset", offset); set_state(con, "tg_heartbeat", now_ts()); con.commit()
            if chat_id:
                drain_outbox(tg, con, chat_id)
            backoff = 1
        except Exception as e:
            log.error("telegram loop error: %s (retry in %ss)", notify.redact(e), backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

if __name__ == "__main__":
    main()
