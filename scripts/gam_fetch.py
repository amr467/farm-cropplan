#!/usr/bin/env python3
"""
GAM central market price bulletin: backfill fetcher (v2).

Change from v1: an empty response is NO LONGER treated as final. Probing
showed the site soft-fails to an empty table under burst load and recovers
on its own, so recording empties as "no data" would silently punch holes in
the dataset that look identical to market holidays.

Now, on an empty response the fetcher cools down, opens a fresh session, and
retries the same date. Only if it comes back empty twice from clean sessions
is it recorded as a confirmed no-data day. Delay is a floor plus jitter
rather than a Gaussian, so requests never land closer than --delay apart.

Usage:
    pip install requests beautifulsoup4
    python gam_fetch.py                 # local produce, 2015-08-01 to today
    python gam_fetch.py --delay 8
    python gam_fetch.py --limit 40      # trial
    python gam_fetch.py --kind imported

Resumable. Ctrl+C any time, rerun to continue. Skips only confirmed days.

Output:
    raw/<kind>/YYYY/YYYY-MM-DD.html
    state/<kind>_checkpoint.json
"""

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests beautifulsoup4")


URL = "https://www.ammancity.gov.jo/ar/market/prices.aspx"

PREFIX = "ctl00$ContentPlaceHolder1$"
F_FROM = PREFIX + "txtfromdate"
F_TO = PREFIX + "txttodate"
F_KIND = PREFIX + "FruitType"
BTN = PREFIX + "btnSearch"

KINDS = {"local": "rbLocal", "imported": "rbImported"}

HIDDEN = ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
          "__VIEWSTATEENCRYPTED", "__LASTFOCUS"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Origin": "https://www.ammancity.gov.jo",
    "Referer": URL,
}

# Statuses we never revisit. Everything else is retried on the next run.
FINAL = {"ok", "empty_confirmed"}

MAX_CONSECUTIVE_FAILURES = 8
REPRIME_EVERY = 40          # fresh session periodically, cheap insurance
EMPTY_COOLDOWN = 45         # seconds to wait before retrying an empty date


class Checkpoint:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"Warning: {self.path} unreadable, starting fresh.")
        # v1 recorded bare "empty" as final. Demote those so they get retried.
        for key, entry in self.data.items():
            if entry.get("status") == "empty":
                entry["status"] = "empty_unverified"
        self._dirty = 0

    def status(self, day):
        return self.data.get(day, {}).get("status")

    def record(self, day, status, rows=None, note=None):
        entry = {"status": status, "at": datetime.now().isoformat(timespec="seconds")}
        if rows is not None:
            entry["rows"] = rows
        if note:
            entry["note"] = note
        self.data[day] = entry
        self._dirty += 1
        if self._dirty >= 10:
            self.flush()

    def flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = 0

    def counts(self):
        out = {}
        for entry in self.data.values():
            out[entry["status"]] = out.get(entry["status"], 0) + 1
        return out


def hidden_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for name in HIDDEN:
        tag = soup.find("input", {"name": name})
        if tag is not None:
            fields[name] = tag.get("value", "")
    return fields


def new_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    resp = session.get(URL, timeout=45)
    resp.raise_for_status()
    fields = hidden_fields(resp.text)
    if "__VIEWSTATE" not in fields:
        raise RuntimeError("No __VIEWSTATE on the primed page. Site may have changed.")
    return session, fields


def post_date(session, fields, day, kind):
    payload = dict(fields)
    payload["__EVENTTARGET"] = BTN
    payload["__EVENTARGUMENT"] = ""
    payload[F_FROM] = day.strftime("%d-%m-%Y")
    payload[F_TO] = day.strftime("%d-%m-%Y")
    payload[F_KIND] = KINDS[kind]

    resp = session.post(URL, data=payload, timeout=60)
    resp.raise_for_status()
    if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text, hidden_fields(resp.text)


def inspect(html):
    """Returns (n_rows, date_string_of_first_row)."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-bordered")
    if table is None:
        return 0, None
    trs = table.find_all("tr")
    n = max(0, len(trs) - 1)
    if not n:
        return 0, None
    cells = [c.get_text(strip=True) for c in trs[1].find_all("td")]
    return n, (cells[6] if len(cells) >= 7 else None)


def daterange(start, end):
    """Newest first, skipping Fridays."""
    day = end
    while day >= start:
        if day.weekday() != 4:
            yield day
        day -= timedelta(days=1)


def sleep_for(delay):
    time.sleep(delay + random.uniform(0, delay * 0.4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-08-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--kind", default="local", choices=sorted(KINDS))
    ap.add_argument("--delay", type=float, default=6.0,
                    help="minimum seconds between requests")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
    if start > end:
        sys.exit("Start date is after end date.")

    root = Path(args.outdir)
    raw_root = root / "raw" / args.kind
    ckpt = Checkpoint(root / "state" / f"{args.kind}_checkpoint.json")

    print(f"Range   : {start} to {end}, Fridays skipped, newest first")
    print(f"Kind    : {args.kind}")
    print(f"Delay   : >= {args.delay}s between requests")
    print(f"Output  : {raw_root}")
    if ckpt.data:
        print(f"Resuming: {ckpt.counts()}")

    try:
        session, fields = new_session()
    except Exception as exc:
        sys.exit(f"Could not prime session: {exc}")

    todo = [d for d in daterange(start, end) if ckpt.status(d.isoformat()) not in FINAL]
    print(f"{len(todo)} dates to fetch.\n")

    fetched = 0
    since_prime = 0
    consecutive_failures = 0
    consecutive_empties = 0

    def save(day, html):
        path = raw_root / f"{day.year}" / f"{day.isoformat()}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    try:
        for day in todo:
            if args.limit is not None and fetched >= args.limit:
                print(f"\nReached --limit {args.limit}, stopping.")
                break

            key = day.isoformat()
            expected = day.strftime("%d-%m-%Y")

            if since_prime >= REPRIME_EVERY:
                try:
                    session.close()
                    session, fields = new_session()
                    since_prime = 0
                except Exception:
                    pass

            try:
                html, new_fields = post_date(session, fields, day, args.kind)
                since_prime += 1
            except Exception as exc:
                consecutive_failures += 1
                ckpt.record(key, "failed", note=str(exc)[:200])
                wait = min(300, 10 * 2 ** (consecutive_failures - 1))
                print(f"{key}  FAILED ({type(exc).__name__}). Backing off {wait}s.")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print("\nToo many consecutive failures. Stopping. Progress saved.")
                    break
                time.sleep(wait + random.uniform(0, 5))
                try:
                    session, fields = new_session()
                    since_prime = 0
                except Exception:
                    pass
                continue

            consecutive_failures = 0
            if new_fields.get("__VIEWSTATE"):
                fields = new_fields

            rows, row_date = inspect(html)

            # ---- empty: cool down, fresh session, retry the same date once
            if rows == 0:
                consecutive_empties += 1
                cooldown = EMPTY_COOLDOWN * min(consecutive_empties, 4)
                print(f"{key}  empty, cooling down {cooldown}s and retrying")
                time.sleep(cooldown)
                try:
                    session.close()
                    session, fields = new_session()
                    since_prime = 0
                    html, new_fields = post_date(session, fields, day, args.kind)
                    since_prime += 1
                    if new_fields.get("__VIEWSTATE"):
                        fields = new_fields
                    rows, row_date = inspect(html)
                except Exception as exc:
                    ckpt.record(key, "failed", note=f"retry: {str(exc)[:180]}")
                    print(f"{key}  retry failed ({type(exc).__name__})")
                    sleep_for(args.delay)
                    continue

                if rows == 0:
                    ckpt.record(key, "empty_confirmed", rows=0)
                    print(f"{key}  confirmed no data")
                    sleep_for(args.delay)
                    continue
                print(f"{key}  recovered on retry")

            consecutive_empties = 0

            if row_date and row_date != expected:
                ckpt.record(key, "mismatch", note=f"rows dated {row_date}")
                print(f"{key}  MISMATCH: rows dated {row_date}. Not saved.")
                sleep_for(args.delay)
                continue

            save(day, html)
            ckpt.record(key, "ok", rows=rows)
            print(f"{key}  {rows} rows")
            fetched += 1
            sleep_for(args.delay)

    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved, rerun to resume.")

    ckpt.flush()
    print(f"\nFetched {fetched} this run. Totals: {ckpt.counts()}")


if __name__ == "__main__":
    main()
