#!/usr/bin/env python3
"""Read-only browser inspection for the OECD application worker."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    args = parser.parse_args()
    dest = ROOT / '.local' / 'inspection'
    dest.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        # Inspection must never create an application, including via script.
        page.route('**/*', lambda route: route.abort() if route.request.method not in {'GET', 'HEAD', 'OPTIONS'} else route.continue_())
        page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(5000)
        (dest / 'page.html').write_text(page.content())
        (dest / 'page.txt').write_text(page.locator('body').inner_text())
        page.screenshot(path=str(dest / 'page.png'), full_page=True)
        print(json.dumps({'url': page.url, 'title': page.title(), 'text': page.locator('body').inner_text()[:18000], 'links': page.locator('a').evaluate_all('(els) => els.map(e=>({text:e.innerText,url:e.href}))'), 'frames': [f.url for f in page.frames]}, indent=2))
        browser.close()

if __name__ == '__main__':
    main()
