"""Publisher loop: deploys the static dashboard to Netlify within ~5-15s of every paper buy/sell,
plus a fallback deploy every 5 minutes.
- Skips the deploy when the data has not changed.
- Backs off on errors.
- The Netlify token is read from the NETLIFY_AUTH_TOKEN environment variable (or the host's
  secrets file) at deploy time, kept in memory only, and never printed or logged."""
import io, json, logging, os, random, string, sys, time, zipfile
import requests
from common import BASE, init_db
import export_snapshot

API = "https://api.netlify.com/api/v1"
SITE_FILE = os.path.join(BASE, "netlify_site.json")
URL_FILE = os.path.join(BASE, "public_url.txt")
INTERVAL = 300
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("publish")

def token():
    t = os.environ.get("NETLIFY_AUTH_TOKEN")
    if not t:
        try:  # secret provided by the user through the host; read into memory only
            with open("/home/box/sand-data/box-secrets.json") as f:
                t = (json.load(f).get("card") or {}).get("NETLIFY_AUTH_TOKEN")
        except Exception:
            t = None
    if not t:
        raise RuntimeError("NETLIFY_AUTH_TOKEN not available")
    return t

def hdrs(extra=None):
    h = {"Authorization": "Bearer " + token(), "User-Agent": "memebot-publisher"}
    h.update(extra or {})
    return h

def ensure_site():
    if os.path.exists(SITE_FILE):
        with open(SITE_FILE) as f:
            s = json.load(f)
        r = requests.get(f"{API}/sites/{s['site_id']}", headers=hdrs(), timeout=30)
        if r.status_code == 200:
            return s
        log.warning("saved site lookup returned HTTP %s; creating a new site", r.status_code)
    for _ in range(3):
        name = "memedash-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        r = requests.post(f"{API}/sites", headers=hdrs({"Content-Type": "application/json"}),
                          json={"name": name}, timeout=30)
        if r.status_code in (200, 201):
            j = r.json()
            s = {"site_id": j["id"], "name": j["name"], "url": j.get("ssl_url") or j.get("url"),
                 "admin_url": j.get("admin_url"), "created": time.time()}
            with open(SITE_FILE, "w") as f:
                json.dump(s, f, indent=2)
            log.info("created Netlify site %s (%s)", s["name"], s["url"])
            return s
        log.warning("site create failed HTTP %s: %s", r.status_code, r.text[:200])
    raise RuntimeError("could not create Netlify site")

def zip_dir(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(path):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, path))
    return buf.getvalue()

def deploy(site, path):
    r = requests.post(f"{API}/sites/{site['site_id']}/deploys", headers=hdrs({"Content-Type": "application/zip"}),
                      data=zip_dir(path), timeout=120)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"deploy failed HTTP {r.status_code}: {r.text[:200]}")
    dep = r.json()
    for _ in range(40):  # wait until live
        if dep.get("state") in ("ready", "error"):
            break
        time.sleep(1)
        dep = requests.get(f"{API}/deploys/{dep['id']}", headers=hdrs(), timeout=30).json()
    if dep.get("state") != "ready":
        raise RuntimeError(f"deploy state {dep.get('state')}: {dep.get('error_message')}")
    return dep

_SITE = None
def once(last_fp, force=False, reason="periodic"):
    global _SITE
    if _SITE is None:
        _SITE = ensure_site()
        with open(URL_FILE, "w") as f:
            f.write(_SITE["url"] + "\n")
    path, fp = export_snapshot.site()
    export_snapshot.snapshot()
    if fp == last_fp and not force:
        log.info("data unchanged, skipping deploy")
        return fp
    t0 = time.time()
    dep = deploy(_SITE, path)
    log.info("deployed %s -> %s (%s, deploy took %.1fs)", dep.get("id"), _SITE["url"], reason, time.time() - t0)
    return fp

# ---------------------------------------------------------------- instant publish after trades
POLL_SEC = 3          # how often to look for a trade signal (cheap SQLite query + file stat)
DEBOUNCE_SEC = 2      # wait for a burst of trades to settle into one deploy
MIN_GAP_SEC = 10      # never deploy more than once per 10s (Netlify friendliness)
MARKER = os.path.join(BASE, "logs", "publish_now")   # touch this file to force a publish

def trade_signal():
    """Changes whenever a paper position is opened, partially sold or closed."""
    from common import db
    con = db()
    try:
        r = con.execute("SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(status='closed'),0), "
                        "COALESCE(SUM(tp1_done),0), COALESCE(SUM(remaining_qty),0) FROM positions").fetchone()
        sig = tuple(r)
    finally:
        con.close()
    try:
        m = os.path.getmtime(MARKER)
    except OSError:
        m = 0
    return sig, m

def main():
    init_db()
    last_fp, fails = None, 0
    last_deploy = 0.0       # time of last deploy attempt
    next_periodic = 0.0     # run the 5-minute fallback immediately at start
    blocked_until = 0.0     # error back-off
    sig, marker = trade_signal()
    pending_since = None
    while True:
        now = time.time()
        try:
            new_sig, new_marker = trade_signal()
            if new_sig != sig or new_marker != marker:
                why = "trade" if new_sig != sig else "marker"
                if pending_since is None:
                    log.info("change signal seen (%s), publishing soon", why)
                pending_since = now
                pending_reason = why
                sig, marker = new_sig, new_marker
        except Exception as e:
            log.warning("signal check failed: %s", str(e)[:200])
        due_trade = pending_since is not None and now - pending_since >= DEBOUNCE_SEC
        due_periodic = now >= next_periodic
        if (due_trade or due_periodic) and now >= blocked_until and now - last_deploy >= MIN_GAP_SEC:
            last_deploy = now
            try:
                if due_trade:
                    last_fp = once(last_fp, force=True, reason=f"instant after {pending_reason}")
                    pending_since = None
                else:
                    last_fp = once(last_fp)
                next_periodic = time.time() + INTERVAL
                fails = 0
            except Exception as e:
                fails += 1
                wait = min(INTERVAL * 4, 30 * 2 ** min(fails, 4))
                blocked_until = time.time() + wait
                log.error("publish error (%s), retrying in %ss", str(e)[:300], wait)
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    if "--once" in sys.argv:
        once(None, force=True, reason="manual")
    else:
        main()
