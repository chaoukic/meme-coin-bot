"""Opens the public site at phone width in headless Chrome, checks live prices tick, saves mobile_live.png."""
import os, time
from playwright.sync_api import sync_playwright
BASE = os.path.dirname(os.path.abspath(__file__))
url = open(os.path.join(BASE, "public_url.txt")).read().strip()
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    calls = []
    pg.on("response", lambda r: calls.append((time.strftime("%H:%M:%S"), r.status)) if "api.dexscreener.com" in r.url else None)
    pg.goto(url, wait_until="networkidle")
    pg.wait_for_selector("#livebadge .live-label", timeout=20000)
    read = lambda: pg.eval_on_selector_all("#open tbody tr", "rs => rs.map(r => [...r.children].filter(c => getComputedStyle(c).display != 'none').map(c => c.innerText.replace(/\\n/g,' ')).join(' | '))")
    print("badge:", pg.inner_text("#livebadge"))
    print("rows t0:", read())
    print("kpi equity:", pg.inner_text("#kpi-equity .value"), "| pnl:", pg.inner_text("#kpi-pnl .value"), pg.inner_text("#kpi-pnl .sub"))
    seen = set()
    for _ in range(9):
        pg.wait_for_timeout(5000)
        seen.add(tuple(read()))
    print("distinct row states over 45s:", len(seen))
    print("badge:", pg.inner_text("#livebadge"))
    print("rows end:", read())
    print("dexscreener calls:", calls)
    print("visible header cols:", pg.eval_on_selector_all("#open thead th", "hs => hs.filter(h => getComputedStyle(h).display != 'none').map(h => h.innerText)"))
    print("page width:", pg.evaluate("document.documentElement.scrollWidth"))
    pg.screenshot(path=os.path.join(BASE, "mobile_live.png"), full_page=True)
    # fallback check: block DexScreener and confirm the table falls back to 'snapshot'
    pg2 = b.new_page(viewport={"width": 390, "height": 844})
    pg2.route("**/api.dexscreener.com/**", lambda r: r.abort())
    pg2.goto(url, wait_until="networkidle"); pg2.wait_for_timeout(3000)
    print("fallback badge:", pg2.inner_text("#livebadge").replace("\n", " "))
    b.close()
