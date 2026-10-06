"""Shared helpers: config, SQLite, rate-limited public API clients.
PAPER TRADING ONLY - this code never touches wallets, keys or real trades."""
import json, logging, os, sqlite3, threading, time, tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "memebot.db")
CONFIG_PATH = os.path.join(BASE, "config.toml")
TZ = ZoneInfo("America/Toronto")
log = logging.getLogger("memebot")

def load_config():
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)

def now_ts():
    return time.time()

def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec="seconds")

def toronto_date(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), TZ).strftime("%Y-%m-%d")

def toronto_midnight_ts(ts=None):
    d = datetime.fromtimestamp(ts if ts is not None else time.time(), TZ)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
  address TEXT PRIMARY KEY, pool_address TEXT, symbol TEXT, name TEXT, dex TEXT,
  pool_created_ts REAL, first_seen REAL, last_eval REAL, status TEXT DEFAULT 'watch',
  last_stage TEXT, last_reasons TEXT, last_score REAL, source TEXT,
  contract_json TEXT, contract_ts REAL, holders_json TEXT, holders_ts REAL);
CREATE INDEX IF NOT EXISTS ix_tokens_status ON tokens(status);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL, discovered INTEGER,
  new_tokens INTEGER, evaluated INTEGER, passed INTEGER, funnel TEXT, errors TEXT, api_calls TEXT);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, ts REAL, token TEXT, symbol TEXT,
  pair TEXT, url TEXT, result TEXT, stage TEXT, reasons TEXT, score REAL, metrics TEXT);
CREATE INDEX IF NOT EXISTS ix_eval_ts ON evaluations(ts);
CREATE INDEX IF NOT EXISTS ix_eval_cycle ON evaluations(cycle_id);
CREATE TABLE IF NOT EXISTS holder_snaps(token TEXT, ts REAL, holders INTEGER, price REAL);
CREATE INDEX IF NOT EXISTS ix_hs ON holder_snaps(token, ts);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, pair TEXT, symbol TEXT, url TEXT,
  opened_at REAL, entry_price REAL, entry_eff REAL, qty REAL, remaining_qty REAL,
  cost_usd REAL, proceeds_usd REAL DEFAULT 0, peak_price REAL, last_price REAL,
  last_update REAL, status TEXT, closed_at REAL, exit_reason TEXT, tp1_done INTEGER DEFAULT 0,
  score REAL, entry_liq REAL, pnl_usd REAL, pnl_pct REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, token TEXT, symbol TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
"""

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con

def init_db():
    con = db()
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(holder_snaps)")]
    if "marker" not in cols:
        con.execute("ALTER TABLE holder_snaps ADD COLUMN marker TEXT")
    # price-guard bookkeeping on positions (added 2026-09-26)
    pcols = [r[1] for r in con.execute("PRAGMA table_info(positions)")]
    for col, typ in [("guard_pending_price", "REAL"), ("guard_pending_ts", "REAL"), ("guard_drain_ts", "REAL"),
                     ("guard_supply", "REAL"), ("guard_last_seen", "REAL"), ("guard_vd_ts", "REAL"), ("last_liq", "REAL"),
                     ("entry_source", "TEXT"), ("entry_check", "TEXT"),   # entry_source/entry_check added Oct 2 (Jupiter pre-buy fallback)
                     ("strategy", "TEXT DEFAULT 'new'")]:  # Oct 2: 'new' = new-coin strategy (all older rows), 'established' = established.py (paper only)
        if col not in pcols:
            try:
                con.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:  # another process (web/telegram) added it at the same moment
                if "duplicate column" not in str(e):
                    raise
    import fills  # recording-only tables: position_ticks, live_fills (Oct 3)
    fills.ensure(con)
    # side-by-side scoring test (Oct 3): 'current' = existing paper account (and real money), 'new' = scoring_v1 paper account
    _add_cols(con, "positions", [("scoring", "TEXT NOT NULL DEFAULT 'current'"), ("score_current", "REAL"), ("score_new", "REAL"),
                                 ("prob_new", "REAL"), ("model_version", "TEXT")])
    _add_cols(con, "evaluations", [("score_new", "REAL"), ("prob_new", "REAL"), ("model_version", "TEXT"),
                                   ("gate_current", "TEXT"), ("gate_new", "TEXT")])
    _add_cols(con, "equity", [("scoring", "TEXT NOT NULL DEFAULT 'current'")])
    _add_cols(con, "decisions", [("scoring", "TEXT"), ("score_new", "REAL"), ("score_old", "REAL")])  # scores added Oct 6
    _add_cols(con, "position_ticks", [("scoring", "TEXT")])
    if get_state(con, "scoring_backfill_done") is None:  # existing new-coin rows: their score IS the current score
        con.execute("UPDATE positions SET score_current=score WHERE score_current IS NULL AND COALESCE(strategy,'new')='new'")
        set_state(con, "scoring_backfill_done", True)
    con.execute("""CREATE TABLE IF NOT EXISTS corrections(id INTEGER PRIMARY KEY, ts REAL, position_id INTEGER,
                   field TEXT, before TEXT, after TEXT, note TEXT)""")
    con.commit()
    con.close()

def _add_cols(con, table, cols):
    have = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    for col, typ in cols:
        if col not in have:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

def get_state(con, key, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default

def set_state(con, key, value):
    con.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None

def decision(con, kind, token, symbol, message, scoring=None, score_new=None, score_old=None):
    """Decision log row. score_new = scoring_v1 (new) score, score_old = current (old) score; None if unknown."""
    row = (now_ts(), kind, token, symbol, message, scoring or None, _num(score_new), _num(score_old))
    sql = "INSERT INTO decisions(ts,kind,token,symbol,message,scoring,score_new,score_old) VALUES(?,?,?,?,?,?,?,?)"
    try:
        con.execute(sql, row)
    except sqlite3.OperationalError as e:
        if "score_" not in str(e) and "scoring" not in str(e):
            raise
        _add_cols(con, "decisions", [("scoring", "TEXT"), ("score_new", "REAL"), ("score_old", "REAL")])
        con.execute(sql, row)
    log.info("[%s] %s %s", kind, symbol or token, message)

# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Simple thread-safe spacing limiter + cool-down after 429s."""
    def __init__(self, per_min):
        self.lock = threading.Lock()
        self.set_rate(per_min)
        self.next_ok = 0.0
        self.blocked_until = 0.0
    def set_rate(self, per_min):
        self.interval = 60.0 / max(per_min, 1)
    def wait(self):
        with self.lock:
            t = time.time()
            start = max(t, self.next_ok, self.blocked_until)
            self.next_ok = start + self.interval
        d = start - time.time()
        if d > 0:
            time.sleep(d)
    def backoff(self, secs):
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + secs)

UA = {"User-Agent": "memebot-paper-scanner/1.0 (paper trading research)", "Accept": "application/json"}
# 'jup' = Jupiter Tokens API v2 scanning (Sammy's key). The key's whole bucket is 60/min (sliding window) and is
# shared with real-money swap quotes and price calls (live.py, trader.jup_backup_pairs), so scanning stays well below it.
LIMITS = {"gt": RateLimiter(25), "ds": RateLimiter(250), "rpc": RateLimiter(120), "jup": RateLimiter(40)}
CALLS = {"gt": 0, "ds": 0, "rpc": 0, "jup": 0, "errors": 0, "gt_429": 0, "ds_429": 0, "jup_429": 0}
_session = requests.Session()
_session.headers.update(UA)

# DexScreener pause (Oct 2): the machine's IP is shared with another bot, so after a DexScreener 429 the bot stops
# asking DexScreener for a while instead of firing every round (doubling 3 -> 6 -> 12 -> 15 min while 429s continue).
# During a pause the watchlist double-checks wait, open positions are priced from Jupiter (trader.jup_backup_pairs)
# and buys use the Jupiter pre-buy fallback (jupcheck.py).
DS_PAUSE = {"until": 0.0, "streak": 0, "base": 180.0, "max": 900.0}

def ds_paused():
    return time.time() < DS_PAUSE["until"]

def ds_note(got_429):
    if got_429:
        DS_PAUSE["streak"] += 1
        secs = min(DS_PAUSE["base"] * 2 ** (DS_PAUSE["streak"] - 1), DS_PAUSE["max"])
        DS_PAUSE["until"] = time.time() + secs
        log.warning("DexScreener 429: pausing DexScreener calls for %.0fs (streak %d)", secs, DS_PAUSE["streak"])
    else:
        DS_PAUSE["streak"] = 0

def apply_limits(cfg):
    a = cfg.get("apis", {})
    DS_PAUSE["base"] = float(a.get("dexscreener_pause_after_429_sec", 180))
    DS_PAUSE["max"] = float(a.get("dexscreener_pause_max_sec", 900))
    LIMITS["gt"].set_rate(a.get("geckoterminal_per_min", 25))
    LIMITS["ds"].set_rate(a.get("dexscreener_per_min", 250))
    LIMITS["rpc"].set_rate(a.get("rpc_per_sec", 2) * 60)
    LIMITS["jup"].set_rate(min(a.get("jupiter_per_min", 40), 50))  # hard ceiling: never more than 50 of the key's 60/min

def _jup_ratelimit_headers(r, lim):
    """Jupiter sends x-ratelimit-remaining / x-ratelimit-reset (epoch seconds): pause until the reset when nearly empty."""
    try:
        rem = int(r.headers.get("x-ratelimit-remaining", "99"))
        reset = float(r.headers.get("x-ratelimit-reset", "0"))
    except ValueError:
        return
    if rem <= 2 and reset:
        wait = min(max(reset - time.time(), 1), 60)
        lim.backoff(wait)

def http_get(kind, url, params=None, retries=3, headers=None, skip_spacing=False):
    """skip_spacing: don't wait for the per-call spacing (still honours 429 / rate-limit back-offs). Used to send a
    pair of Jupiter quotes at the same moment; the caller has already waited for one slot."""
    lim = LIMITS[kind]
    for attempt in range(retries):
        if skip_spacing and attempt == 0:
            d = lim.blocked_until - time.time()
            if d > 0:
                time.sleep(d)
        else:
            lim.wait()
        CALLS[kind] += 1
        try:
            r = _session.get(url, params=params, timeout=20, headers=headers)
        except requests.RequestException as e:
            CALLS["errors"] += 1
            log.warning("%s GET failed %s: %s", kind, url, e)
            time.sleep(2 * (attempt + 1))
            continue
        if kind == "jup":
            _jup_ratelimit_headers(r, lim)
        if r.status_code == 429:
            CALLS["errors"] += 1
            CALLS[f"{kind}_429"] = CALLS.get(f"{kind}_429", 0) + 1
            wait = 60 if kind == "gt" else 10 * (attempt + 1)
            if kind == "jup":
                try:
                    wait = max(wait, min(float(r.headers.get("x-ratelimit-reset", 0)) - time.time(), 60))
                except ValueError:
                    pass
            log.warning("%s 429 rate limited, backing off %ss", kind, round(wait))
            lim.backoff(wait)
            continue
        if r.status_code >= 500:
            CALLS["errors"] += 1
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            CALLS["errors"] += 1
            log.warning("%s GET %s -> %s", kind, url, r.status_code)
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None

_rpc_bad_until = {}
def rpc(cfg, method, params):
    """JSON-RPC against the configured public endpoints, rotating on errors."""
    eps = cfg.get("apis", {}).get("rpc_endpoints", ["https://api.mainnet-beta.solana.com"])
    for ep in eps:
        if _rpc_bad_until.get((ep, method), 0) > time.time():
            continue
        LIMITS["rpc"].wait()
        CALLS["rpc"] += 1
        try:
            r = _session.post(ep, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            j = r.json()
        except Exception as e:
            CALLS["errors"] += 1
            _rpc_bad_until[(ep, method)] = time.time() + 30
            continue
        if "error" in j or r.status_code != 200:
            CALLS["errors"] += 1
            _rpc_bad_until[(ep, method)] = time.time() + 20
            continue
        return j.get("result")
    return None

def fnum(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default
