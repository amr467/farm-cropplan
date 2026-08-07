#!/usr/bin/env python3
"""
Discovery script for the Greater Amman Municipality central market price page.

Fetches https://www.ammancity.gov.jo/ar/market/prices.aspx once, saves the raw
HTML, and prints the structure of the form so the scraper can be written
against reality instead of guesses.

Makes exactly ONE request. Safe to run repeatedly.

Usage:
    pip install requests beautifulsoup4
    python gam_discover.py

Output:
    - prints a structure report to the terminal
    - writes gam_prices_raw.html next to the script
"""

import sys
import re

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")


URL = "https://www.ammancity.gov.jo/ar/market/prices.aspx"
RAW_PATH = "gam_prices_raw.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
}

BIG_FIELDS = {"__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
              "__PREVIOUSPAGE", "__VIEWSTATEENCRYPTED"}

LINE = "=" * 70


def section(title):
    print(f"\n{LINE}\n{title}\n{LINE}")


def shorten(value, limit=60):
    if value is None:
        return "(none)"
    value = value.replace("\n", " ").replace("\r", " ")
    if len(value) > limit:
        return f"{value[:limit]}... [{len(value)} chars total]"
    return value


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"Fetching {URL}")
    try:
        resp = session.get(URL, timeout=45)
    except requests.RequestException as exc:
        sys.exit(f"Request failed: {exc}")

    section("RESPONSE")
    print(f"Status          : {resp.status_code}")
    print(f"Final URL       : {resp.url}")
    print(f"Declared enc    : {resp.encoding}")
    print(f"Apparent enc    : {resp.apparent_encoding}")
    print(f"Content length  : {len(resp.content)} bytes")
    print(f"Server          : {resp.headers.get('Server', '(not sent)')}")
    print(f"Cookies set     : {list(session.cookies.keys()) or '(none)'}")

    # ASP.NET pages frequently mislabel Arabic encoding. Trust the sniffer
    # unless the declared encoding actually decodes cleanly.
    if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1",):
        resp.encoding = resp.apparent_encoding
    html = resp.text

    with open(RAW_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\nRaw HTML written to {RAW_PATH}")

    soup = BeautifulSoup(html, "html.parser")

    # ---------------------------------------------------------------- forms
    section("FORMS")
    forms = soup.find_all("form")
    if not forms:
        print("No <form> found. The page may load its data from a separate")
        print("endpoint via JavaScript. Check the raw HTML for fetch/ajax URLs.")
    for i, form in enumerate(forms):
        print(f"\nForm #{i}")
        print(f"  id     : {form.get('id')}")
        print(f"  name   : {form.get('name')}")
        print(f"  action : {form.get('action')}")
        print(f"  method : {(form.get('method') or 'GET').upper()}")

    # --------------------------------------------------------------- inputs
    section("INPUT FIELDS")
    for inp in soup.find_all("input"):
        name = inp.get("name") or inp.get("id") or "(unnamed)"
        itype = inp.get("type", "text")
        value = inp.get("value")
        if name in BIG_FIELDS:
            size = len(value) if value else 0
            print(f"  {name:<40} type={itype:<10} [{size} chars, omitted]")
        else:
            print(f"  {name:<40} type={itype:<10} value={shorten(value, 40)}")

    # -------------------------------------------------------------- selects
    section("DROPDOWNS")
    selects = soup.find_all("select")
    if not selects:
        print("None. The date is probably a textbox or a calendar control.")
    for sel in selects:
        name = sel.get("name") or sel.get("id") or "(unnamed)"
        options = sel.find_all("option")
        print(f"\n  {name}  ({len(options)} options)")
        preview = options[:6]
        tail = options[-3:] if len(options) > 9 else []
        for opt in preview:
            print(f"      value={opt.get('value')!r:<12} text={opt.get_text(strip=True)!r}")
        if tail:
            print("      ...")
            for opt in tail:
                print(f"      value={opt.get('value')!r:<12} text={opt.get_text(strip=True)!r}")

    # ------------------------------------------------------------ postbacks
    section("POSTBACK TARGETS")
    targets = sorted(set(re.findall(r"__doPostBack\(\s*['\"]([^'\"]+)['\"]", html)))
    if targets:
        for t in targets[:40]:
            print(f"  {t}")
        if len(targets) > 40:
            print(f"  ... and {len(targets) - 40} more")
    else:
        print("None found. Buttons likely submit the form directly.")

    # --------------------------------------------------------- ajax markers
    section("AJAX / PARTIAL POSTBACK")
    markers = {
        "ScriptManager": "ScriptManager" in html,
        "UpdatePanel": "UpdatePanel" in html or "sm_HiddenField" in html,
        "Sys.WebForms": "Sys.WebForms" in html,
        "MicrosoftAjax": "MicrosoftAjax" in html,
    }
    for k, v in markers.items():
        print(f"  {k:<16} {'yes' if v else 'no'}")
    if any(markers.values()):
        print("\n  Partial postback in use. The scraper will need")
        print("  __ASYNCPOST=true and an X-MicrosoftAjax header.")

    # --------------------------------------------------------------- tables
    section("TABLES")
    tables = soup.find_all("table")
    print(f"{len(tables)} table(s) on the page.\n")
    for i, tbl in enumerate(tables):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True) for c in cells]
        print(f"  Table #{i}: {len(rows)} rows, {len(cells)} columns")
        print(f"    id/class : {tbl.get('id')} / {tbl.get('class')}")
        print(f"    header   : {headers}")
        if len(rows) > 1:
            first = [c.get_text(strip=True) for c in rows[1].find_all(["th", "td"])]
            print(f"    row 1    : {first}")
        print()

    section("DONE")
    print("Paste the output above, and if the tables section looks empty or")
    print("garbled, send gam_prices_raw.html as well.")


if __name__ == "__main__":
    main()
