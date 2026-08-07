#!/usr/bin/env python3
"""
GAM bulletin: diagnose why results go empty.

Three hypotheses for the empty responses after ~14 fetches:
  A. session/IP throttle that soft-fails to an empty table
  B. the page only retains a short window of history
  C. a genuine gap in the data for those dates

This runs three tests to tell them apart. Roughly 30 requests total, spaced
out. Makes no assumptions and writes nothing except a report.

Usage:
    python gam_probe.py
    python gam_probe.py --delay 10        # slower, if you suspect throttling
"""

import argparse
import re
import sys
import time
from datetime import date, timedelta

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")

URL = "https://www.ammancity.gov.jo/ar/market/prices.aspx"
P = "ctl00$ContentPlaceHolder1$"
HIDDEN = ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
          "__VIEWSTATEENCRYPTED", "__LASTFOCUS"]
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Origin": "https://www.ammancity.gov.jo",
    "Referer": URL,
}


def hidden(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for n in HIDDEN:
        t = soup.find("input", {"name": n})
        if t is not None:
            out[n] = t.get("value", "")
    return out


def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(URL, timeout=45)
    r.raise_for_status()
    return s, hidden(r.text)


def fetch(session, fields, day):
    """Returns (n_rows, first_row_date, status_code, new_fields)."""
    d = day.strftime("%d-%m-%Y")
    payload = dict(fields)
    payload.update({
        "__EVENTTARGET": P + "btnSearch",
        "__EVENTARGUMENT": "",
        P + "txtfromdate": d,
        P + "txttodate": d,
        P + "FruitType": "rbLocal",
    })
    r = session.post(URL, data=payload, timeout=60)
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="table-bordered")
    n = 0
    first_date = None
    if table is not None:
        trs = table.find_all("tr")
        n = max(0, len(trs) - 1)
        if n:
            cells = [c.get_text(strip=True) for c in trs[1].find_all("td")]
            if len(cells) >= 7:
                first_date = cells[6]
    # look for any visible "no results" message
    text = soup.get_text(" ", strip=True)
    no_data = bool(re.search(r"لا توجد|لا يوجد|no records|not found", text, re.I))
    return n, first_date, r.status_code, no_data, hidden(r.text)


def banner(t):
    print(f"\n{'=' * 66}\n{t}\n{'=' * 66}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=6.0)
    args = ap.parse_args()
    D = args.delay

    # ---------------------------------------------------------- TEST 1
    banner("TEST 1  Fresh session per date, spread across years")
    print("If old dates return rows on a clean session, history exists and")
    print("the empties are a throttle. If they are empty even fresh, the")
    print("page does not serve old data at all.\n")

    spot_dates = [
        date(2026, 7, 20),   # first empty from the trial run
        date(2026, 7, 15),
        date(2026, 6, 15),
        date(2026, 3, 10),
        date(2025, 11, 12),
        date(2024, 5, 14),
        date(2022, 9, 20),
        date(2020, 1, 15),
        date(2018, 4, 17),
        date(2015, 8, 4),    # the date you checked in the browser
    ]
    for day in spot_dates:
        try:
            s, f = new_session()
            time.sleep(1.5)
            n, first, code, no_data, _ = fetch(s, f, day)
            note = "  <-- 'no data' message" if no_data else ""
            print(f"  {day}  http={code}  rows={n:<4} row_date={first}{note}")
            s.close()
        except Exception as exc:
            print(f"  {day}  ERROR {exc}")
        time.sleep(D)

    # ---------------------------------------------------------- TEST 2
    banner("TEST 2  One session, walk backwards until it goes empty")
    print("Counts how many requests a single session survives.\n")

    try:
        s, f = new_session()
        day = date.today() - timedelta(days=1)
        n_ok = 0
        first_empty = None
        for i in range(25):
            if day.weekday() == 4:
                day -= timedelta(days=1)
                continue
            n, first, code, no_data, nf = fetch(s, f, day)
            if nf.get("__VIEWSTATE"):
                f = nf
            print(f"  req {i + 1:<3} {day}  rows={n:<4} row_date={first}")
            if n == 0 and first_empty is None:
                first_empty = i + 1
            if n:
                n_ok += 1
            day -= timedelta(days=1)
            time.sleep(D)
        s.close()
        print(f"\n  populated responses: {n_ok}, first empty at request "
              f"{first_empty if first_empty else 'never'}")
    except Exception as exc:
        print(f"  ERROR {exc}")

    # ---------------------------------------------------------- TEST 3
    banner("TEST 3  Retry the first empty date on a brand new session")
    print("Same date, clean session. Rows here means the limit is per")
    print("session and re-priming is the whole fix.\n")

    target = date(2026, 7, 20)
    for attempt in range(2):
        try:
            time.sleep(D)
            s, f = new_session()
            time.sleep(1.5)
            n, first, code, no_data, _ = fetch(s, f, target)
            print(f"  attempt {attempt + 1}: {target}  rows={n}  row_date={first}")
            s.close()
        except Exception as exc:
            print(f"  attempt {attempt + 1}: ERROR {exc}")

    banner("DONE")
    print("Paste the whole output.")


if __name__ == "__main__":
    main()
