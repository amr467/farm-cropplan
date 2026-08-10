#!/usr/bin/env python3
"""
Daily: fetch any bulletin days we do not already have, append to the monthly
observation files. Fetches and parses in one pass, keeping nothing on disk but
the parsed rows.

Designed to run in GitHub Actions. It looks back a configurable window rather
than assuming yesterday, so a few failed runs heal themselves without anyone
noticing.

Conservative by design: sequential, minimum six seconds apart, session
re-primed on failure. The site soft-fails to an empty table under burst load,
so an empty response is retried once on a clean session before being recorded
as a genuine no-bulletin day.

Usage:
    python daily_update.py
    python daily_update.py --lookback 30
    python daily_update.py --dry-run

Exit codes:
    0  new rows appended, or nothing to do
    1  could not reach the site at all
"""

import argparse
import csv
import gzip
import json
import random
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install requests beautifulsoup4")

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
COLS = ["date", "item", "unit", "price_low", "price_mode", "price_high", "quantity"]

# Rendered column order is high, mode, low. The page's own header row says
# high, low, mode; verified empirically at 99.4% across 217,660 rows.
I_ITEM, I_UNIT, I_HIGH, I_MODE, I_LOW, I_QTY, I_DATE = range(7)
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def clean(t):
    t = t.translate(ARABIC_DIGITS)
    return re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", t).strip()


def hidden_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for n in HIDDEN:
        tag = soup.find("input", {"name": n})
        if tag is not None:
            out[n] = tag.get("value", "")
    return out


def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(URL, timeout=60)
    r.raise_for_status()
    f = hidden_fields(r.text)
    if "__VIEWSTATE" not in f:
        raise RuntimeError("No __VIEWSTATE; the page layout may have changed.")
    return s, f


def fetch_day(session, fields, day):
    d = day.strftime("%d-%m-%Y")
    payload = dict(fields)
    payload.update({"__EVENTTARGET": P + "btnSearch", "__EVENTARGUMENT": "",
                    P + "txtfromdate": d, P + "txttodate": d,
                    P + "FruitType": "rbLocal"})
    r = session.post(URL, data=payload, timeout=90)
    r.raise_for_status()
    if r.encoding is None or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text, hidden_fields(r.text)


def parse(html, expect):
    """Rows for one date, or None if the table is absent or dated wrongly."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-bordered")
    if table is None:
        return None
    trs = table.find_all("tr")[1:]
    if not trs:
        return None
    rows = []
    for tr in trs:
        c = [clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
        if len(c) < 7 or not c[I_ITEM]:
            continue
        served = c[I_DATE]
        if served and served != expect.strftime("%d-%m-%Y"):
            return None                       # server ignored the request
        rows.append({"date": expect.isoformat(), "item": c[I_ITEM],
                     "unit": c[I_UNIT], "price_low": c[I_LOW],
                     "price_mode": c[I_MODE], "price_high": c[I_HIGH],
                     "quantity": c[I_QTY]})
    return rows or None


def load_month(path):
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_month(path, rows):
    rows.sort(key=lambda r: (r["date"], r["item"]))
    with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=9) as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows([{c: r.get(c, "") for c in COLS} for r in rows])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default="data/observations")
    ap.add_argument("--lookback", type=int, default=21,
                    help="days back to check for holes, so failed runs heal")
    ap.add_argument("--delay", type=float, default=6.0)
    ap.add_argument("--max-fetch", type=int, default=40, dest="max_fetch",
                    help="ceiling per run, so a long outage does not turn into "
                         "a burst the site will throttle")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    args = ap.parse_args()

    obs = Path(args.obs)
    obs.mkdir(parents=True, exist_ok=True)
    state_path = obs / "_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() \
        else {"no_bulletin": []}
    no_bulletin = set(state.get("no_bulletin", []))

    have = set()
    for p in obs.glob("*.csv.gz"):
        for r in load_month(p):
            have.add(r["date"])

    today = date.today()
    wanted = []
    for k in range(1, args.lookback + 1):
        d = today - timedelta(days=k)
        if d.weekday() == 4:                   # Friday, market closed
            continue
        if d.isoformat() in have or d.isoformat() in no_bulletin:
            continue
        wanted.append(d)
    wanted.sort(reverse=True)
    wanted = wanted[:args.max_fetch]

    print(f"Have {len(have)} days on file. {len(wanted)} to fetch.")
    if not wanted:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("Would fetch:", ", ".join(d.isoformat() for d in wanted))
        return

    try:
        session, fields = new_session()
    except Exception as exc:
        print(f"Cannot reach the site: {exc}")
        sys.exit(1)

    added = defaultdict(list)
    empties = []
    for d in wanted:
        try:
            html, nf = fetch_day(session, fields, d)
            if nf.get("__VIEWSTATE"):
                fields = nf
            rows = parse(html, d)
        except Exception as exc:
            print(f"  {d}  error: {type(exc).__name__}")
            time.sleep(15)
            try:
                session, fields = new_session()
            except Exception:
                pass
            continue

        if rows is None:
            # empty is provisional: the site soft-fails under load
            time.sleep(30)
            try:
                session, fields = new_session()
                html, nf = fetch_day(session, fields, d)
                if nf.get("__VIEWSTATE"):
                    fields = nf
                rows = parse(html, d)
            except Exception:
                rows = None
            if rows is None:
                empties.append(d.isoformat())
                print(f"  {d}  no bulletin (confirmed on a clean session)")
                time.sleep(args.delay)
                continue

        added[d.isoformat()[:7]].extend(rows)
        print(f"  {d}  {len(rows)} rows")
        time.sleep(args.delay + random.uniform(0, args.delay * 0.4))

    if not added and not empties:
        print("\nNothing appended.")
        return

    for month, rows in added.items():
        path = obs / f"{month}.csv.gz"
        existing = load_month(path)
        keys = {(r["date"], r["item"]) for r in existing}
        fresh = [r for r in rows if (r["date"], r["item"]) not in keys]
        write_month(path, existing + fresh)
        print(f"  {month}.csv.gz  +{len(fresh)} rows  "
              f"({path.stat().st_size/1024:.0f} KB)")

    all_dates = sorted(have | set(added and
                       [r["date"] for rows in added.values() for r in rows]))
    state["no_bulletin"] = sorted(no_bulletin | set(empties))
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    if all_dates:
        state["first"], state["last"] = all_dates[0], all_dates[-1]
        state["days_with_data"] = len(all_dates)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")

    print(f"\nAppended {sum(len(v) for v in added.values())} rows across "
          f"{len(added)} month file(s).")


if __name__ == "__main__":
    main()
