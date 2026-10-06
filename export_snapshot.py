"""Static exports of the dashboard (read-only, no server needed).

  python export_snapshot.py          -> snapshot.html  (ONE self-contained file, data inlined; good for sending)
  python export_snapshot.py --site   -> site/          (index.html + style.css + app.js + chart.js + data.json;
                                                        the page re-reads data.json every 60s. Published to Netlify.)
"""
import hashlib, json, os, shutil, sys
import dashboard_data
BASE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(BASE, "static")
SITE = os.path.join(BASE, "site")
read = lambda n: open(os.path.join(S, n), encoding="utf-8").read()
safe = lambda s: s.replace("</script", "<\\/script")

def snapshot(data=None):
    data = data or dashboard_data.build()
    html = read("index.html")
    html = html.replace('<link rel="stylesheet" href="static/style.css">', "<style>\n" + read("style.css") + "\n</style>")
    html = html.replace('<link rel="stylesheet" href="static/live.css">', "<style>\n" + read("live.css") + "\n</style>")
    html = html.replace('<script src="static/chart.umd.min.js"></script>', "<script>" + safe(read("chart.umd.min.js")) + "</script>")
    html = html.replace('<script src="static/app.js"></script>',
                        "<script>window.__SNAPSHOT__ = " + safe(json.dumps(data, default=str)) + ";</script>\n<script>" + safe(read("app.js")) + "</script>")
    html = html.replace("<title>Memebot · Paper Trading Dashboard</title>", "<title>Memebot snapshot · Paper Trading</title>")
    out = os.path.join(BASE, "snapshot.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out

def data_fingerprint(data):
    """Hash of the data ignoring fields that change on every build."""
    d = dict(data); d.pop("generated", None); d.pop("trader_heartbeat", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()

def site(data=None):
    data = data or dashboard_data.build()
    tmp = SITE + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    html = read("index.html").replace('<script src="static/app.js"></script>',
                                      '<script>window.__DATA_URL__ = "data.json";</script>\n<script src="static/app.js"></script>')
    os.makedirs(os.path.join(tmp, "static"))
    with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    for n in ("style.css", "live.css", "app.js", "chart.umd.min.js"):
        shutil.copy(os.path.join(S, n), os.path.join(tmp, "static", n))
    with open(os.path.join(tmp, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, default=str, separators=(",", ":"))
    with open(os.path.join(tmp, "robots.txt"), "w") as f:
        f.write("User-agent: *\nDisallow: /\n")
    with open(os.path.join(tmp, "_headers"), "w") as f:
        f.write("/*\n  X-Robots-Tag: noindex, nofollow\n  Cache-Control: no-cache\n")
    shutil.rmtree(SITE, ignore_errors=True)
    os.replace(tmp, SITE)
    # fingerprint = data + page code, so design/code changes also get published
    h = hashlib.sha256(data_fingerprint(data).encode())
    for n in ("index.html", "style.css", "live.css", "app.js"):
        h.update(open(os.path.join(S, n), "rb").read())
    return SITE, h.hexdigest()

if __name__ == "__main__":
    d = dashboard_data.build()
    if "--site" in sys.argv:
        print("built", site(d)[0])
    else:
        out = snapshot(d)
        print(f"wrote {out} ({os.path.getsize(out)//1024} KB)")
