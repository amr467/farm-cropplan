#!/usr/bin/env python3
"""
GAM central market price bulletin: offline parser.

Reads the raw HTML saved by gam_fetch.py and writes one tidy CSV. Touches the
network never. Re-run it as often as you like; if the parsing logic turns out
to be wrong, nothing needs re-fetching.

Column handling: the page's header row reads
    الصنف | العبوة | السعر الأعلى | السعر الأدنى | السعر الأغلب | الكمية | تاريخ السعر
but the rendered values are actually ordered high, mode, low. The header is
wrong, so this parses by POSITION and then checks low <= mode <= high on every
row. Violations are flagged rather than silently dropped, and summarised at the
end. If violations cluster in one period, the column order changed there and
this assumption needs revisiting.

Usage:
    python gam_parse.py
    python gam_parse.py --kind imported
    python gam_parse.py --out prices_local.csv

Output:
    gam_prices_<kind>.csv
    a report to the terminal: coverage, units seen, anomalies
"""

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run: pip install beautifulsoup4")


# Rendered order, which is not the header order. See docstring.
IDX_ITEM, IDX_UNIT, IDX_HIGH, IDX_MODE, IDX_LOW, IDX_QTY, IDX_DATE = range(7)

FIELDS = ["date", "item", "unit", "price_low", "price_mode", "price_high",
          "quantity", "kind", "order_ok", "source_file"]

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def clean(text):
    text = text.translate(ARABIC_DIGITS)
    # strip bidi and zero-width marks that break float()
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    return text.strip()


def to_number(text):
    text = clean(text).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_iso(text):
    text = clean(text)
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_file(path, kind):
    """Yield row dicts from one saved page."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    table = soup.find("table", class_="table-bordered")
    if table is None:
        return

    for tr in table.find_all("tr")[1:]:
        cells = [clean(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        if len(cells) < 7:
            continue

        item = cells[IDX_ITEM]
        if not item:
            continue

        high = to_number(cells[IDX_HIGH])
        mode = to_number(cells[IDX_MODE])
        low = to_number(cells[IDX_LOW])
        qty = to_number(cells[IDX_QTY])
        day = to_iso(cells[IDX_DATE]) or path.stem

        order_ok = None
        if None not in (low, mode, high):
            order_ok = low <= mode <= high

        yield {
            "date": day,
            "item": item,
            "unit": cells[IDX_UNIT],
            "price_low": low,
            "price_mode": mode,
            "price_high": high,
            "quantity": qty,
            "kind": kind,
            "order_ok": "" if order_ok is None else int(order_ok),
            "source_file": path.name,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="local")
    ap.add_argument("--rawdir", default="raw")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    raw_root = Path(args.rawdir) / args.kind
    if not raw_root.exists():
        sys.exit(f"No such directory: {raw_root}. Run gam_fetch.py first.")

    files = sorted(raw_root.rglob("*.html"))
    if not files:
        sys.exit(f"No saved pages under {raw_root}.")

    out_path = Path(args.out or f"gam_prices_{args.kind}.csv")

    rows = []
    empty_files = 0
    for path in files:
        got = list(parse_file(path, args.kind))
        if not got:
            empty_files += 1
        rows.extend(got)

    rows.sort(key=lambda r: (r["date"], r["item"]))

    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    # ------------------------------------------------------------- report
    print(f"Files read        : {len(files)}  ({empty_files} with no rows)")
    print(f"Rows written      : {len(rows)}")
    print(f"Output            : {out_path}")

    if not rows:
        return

    dates = sorted({r["date"] for r in rows})
    print(f"Date span         : {dates[0]} to {dates[-1]}  ({len(dates)} distinct days)")

    units = Counter(r["unit"] for r in rows)
    print(f"\nPackaging units seen ({len(units)}):")
    for unit, n in units.most_common():
        print(f"  {unit or '(blank)':<20} {n}")
    if len(units) > 1:
        print("  NOTE: more than one unit. Prices are not comparable across")
        print("  units and must not be pooled without conversion.")

    bad = [r for r in rows if r["order_ok"] == 0]
    missing = [r for r in rows if r["order_ok"] == ""]
    print(f"\nRows where low <= mode <= high fails : {len(bad)} "
          f"({100 * len(bad) / len(rows):.2f}%)")
    print(f"Rows with a missing price            : {len(missing)}")
    if bad:
        by_year = Counter(r["date"][:4] for r in bad)
        total_by_year = Counter(r["date"][:4] for r in rows)
        print("  by year (failures / total):")
        for year in sorted(total_by_year):
            print(f"    {year}  {by_year.get(year, 0)} / {total_by_year[year]}")
        print("  If failures concentrate in particular years, the column")
        print("  order changed there and the parser needs a per-era rule.")
        print("\n  First few failing rows:")
        for r in bad[:5]:
            print(f"    {r['date']}  {r['item']}  "
                  f"low={r['price_low']} mode={r['price_mode']} high={r['price_high']}")

    items = Counter(r["item"] for r in rows)
    print(f"\nDistinct items    : {len(items)}")
    print("Most frequent:")
    for item, n in items.most_common(15):
        print(f"  {item:<28} {n} days")

    coverage = defaultdict(set)
    for r in rows:
        coverage[r["date"][:4]].add(r["date"])
    print("\nDays with data per year:")
    for year in sorted(coverage):
        print(f"  {year}  {len(coverage[year])}")


if __name__ == "__main__":
    main()
