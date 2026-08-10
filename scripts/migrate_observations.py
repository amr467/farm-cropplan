#!/usr/bin/env python3
"""
One-time: split the scraped price history into monthly compressed files.

Why: the daily job needs the full history to judge outliers against a rolling
window and to compute seasonal percentiles, but a single 25 MB CSV rewritten
every day would add 25 MB to the repo daily. Split by month, only the current
month changes, and the repo grows by roughly 25 KB a day instead.

Run this ONCE, locally, then commit data/observations/. After that the daily
workflow appends to it and you never touch it again.

Usage:
    python migrate_observations.py
    python migrate_observations.py --prices gam_prices_local.csv --outdir farm-cropplan/data/observations

Output:
    data/observations/YYYY-MM.csv.gz     one per month, ~25 KB each
    data/observations/_state.json        dates confirmed to have no bulletin
"""

import argparse
import csv
import gzip
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

COLS = ["date", "item", "unit", "price_low", "price_mode", "price_high", "quantity"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="gam_prices_local.csv")
    ap.add_argument("--checkpoint", default="state/local_checkpoint.json",
                    help="scraper checkpoint, used to record confirmed no-data days")
    ap.add_argument("--outdir", default="data/observations")
    args = ap.parse_args()

    src = Path(args.prices)
    if not src.exists():
        sys.exit(f"{src} not found.")

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    months = defaultdict(list)
    seen = set()
    with src.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            d = row.get("date", "")
            if len(d) != 10:
                continue
            key = (d, row["item"].strip())
            if key in seen:                       # drop exact repeats at source
                continue
            seen.add(key)
            months[d[:7]].append({c: row.get(c, "") for c in COLS})

    total = 0
    for month, rows in sorted(months.items()):
        rows.sort(key=lambda r: (r["date"], r["item"]))
        path = out / f"{month}.csv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=9) as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        total += path.stat().st_size
        print(f"  {month}  {len(rows):>6} rows  {path.stat().st_size/1024:>6.0f} KB")

    # carry forward which dates genuinely had no bulletin, so the daily job
    # does not re-request them forever
    empty = []
    cp = Path(args.checkpoint)
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            empty = sorted(d for d, v in data.items()
                           if v.get("status") == "empty_confirmed")
        except json.JSONDecodeError:
            print(f"  warning: could not read {cp}")

    dates = sorted({r["date"] for rows in months.values() for r in rows})
    (out / "_state.json").write_text(json.dumps({
        "first": dates[0], "last": dates[-1],
        "days_with_data": len(dates),
        "no_bulletin": empty,
        "migrated": datetime.now().isoformat(timespec="seconds"),
    }, indent=1), encoding="utf-8")

    print(f"\n{len(months)} monthly files, {total/1024/1024:.1f} MB total")
    print(f"{len(dates)} trading days, {dates[0]} to {dates[-1]}")
    print(f"{len(empty)} dates recorded as having no bulletin")
    print(f"\nCommit {out}. The daily workflow appends to it from here on.")


if __name__ == "__main__":
    main()
