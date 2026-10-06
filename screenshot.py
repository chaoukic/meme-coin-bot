"""Full-page screenshot of the local dashboard (1400px wide) -> dashboard.png"""
import os
from playwright.sync_api import sync_playwright
BASE = os.path.dirname(os.path.abspath(__file__))
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.goto("http://localhost:8787/", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    pg.screenshot(path=os.path.join(BASE, "dashboard.png"), full_page=True)
    b.close()
print("saved dashboard.png")
