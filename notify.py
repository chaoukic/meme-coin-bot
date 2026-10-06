"""Telegram alert outbox. PAPER TRADING ONLY.

Trading code calls notify.enqueue(...), which only inserts a row into SQLite (fast, never raises),
so Telegram problems can never block or crash trading. The separate telegram_bot.py process
drains the outbox, retries with backoff and respects Telegram rate limits.
Each event has a unique key, so restarts never resend old events.

CLI (used by the shell scripts):  python notify.py event <key> <text>
"""
import json, os, re, sqlite3, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "memebot.db")
DASHBOARD_URL = "https://memedash-ukjfmaj3ptw1.netlify.app"

OUTBOX_SQL = """CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, text TEXT, created REAL,
  sent_at REAL, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT)"""

def ensure(con):
    con.execute(OUTBOX_SQL)

def enqueue(con, key, text):
    """Queue a message. Never raises; duplicate keys are ignored (dedupe)."""
    try:
        ensure(con)
        con.execute("INSERT OR IGNORE INTO outbox(key,text,created) VALUES(?,?,?)", (key, text, time.time()))
    except Exception:
        pass

def enqueue_standalone(key, text):
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        enqueue(con, key, text)
        con.commit()
        con.close()
    except Exception:
        pass

# ---------------------------------------------------------------- formatting (pure, testable)
def fmt_usd(v):
    return ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"

def fmt_price(v):
    return "$" + (f"{v:.4f}" if v >= 1 else f"{v:.4g}")

def scoring_label(acct):
    return "🏷 NEW scoring trade" if acct == "new" else "🏷 OLD scoring trade"

def fmt_buy(symbol, price, size, score, url, balance, via_jupiter=False, tag="PAPER", balance_label="Fake balance",
            label=None, scores=None):
    return ((label + "\n" if label else "") + f"🟢 {tag} BUY {symbol}" + (" (DexScreener busy, checked via Jupiter quote)" if via_jupiter else "") + "\n"
            f"Price {fmt_price(price)} · size {fmt_usd(size)}" + ("" if scores else f" · score {score:g}") + "\n"
            + (f"Scores: old {scores[0]:g} · new {scores[1]:g}\n" if scores else "") +
            f"{balance_label} {fmt_usd(balance)}\n{url or ''}").strip()

def fmt_sell(symbol, reason, pnl_usd, pnl_pct, balance, partial=False, url=None, tag="PAPER", balance_label="Fake balance", label=None):
    icon = "💰" if pnl_usd >= 0 else "🔻"
    what = "PARTIAL SELL" if partial else "SELL"
    return ((label + "\n" if label else "") + f"{icon} {tag} {what} {symbol}\n{reason}\n"
            f"P&L {fmt_usd(pnl_usd)} ({pnl_pct:+.1f}%)\n"
            f"{balance_label} {fmt_usd(balance)}" + (f"\n{url}" if url else ""))

# ---------------------------------------------------------------- token safety
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

def redact(s):
    return _TOKEN_RE.sub("bot<redacted>", str(s))

def telegram_token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        for path in ("/home/box/agent-data/box-secrets.json", "/home/box/sand-data/box-secrets.json"):
            try:
                with open(path) as f:
                    t = (json.load(f).get("card") or {}).get("TELEGRAM_BOT_TOKEN")
                if t:
                    break
            except Exception:
                continue
    return t

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "event":
        enqueue_standalone(sys.argv[2], " ".join(sys.argv[3:]))
