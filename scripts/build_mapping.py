#!/usr/bin/env python3
"""
Build the item-to-crop mapping template from the scraped GAM price data.

The bulletin lists produce by local trade name, not by crop. Eggplant alone
appears as several variety listings that trade at different prices. Nothing on
the price side works until each raw string is assigned to a crop, and that
assignment needs local knowledge, so this script does not attempt it. It
produces a template with a blank crop column for you to fill.

What it does do is order the work sensibly: items are ranked by how many days
they appear, with their price level and seasonal pattern shown, so you can
map the twenty strings that matter and ignore the long tail of one-offs.

Usage:
    python build_mapping.py
    python build_mapping.py --min-days 30
    python build_mapping.py --merge item_mapping.csv    # keep existing work

Output:
    item_mapping.csv   two columns to fill: crop_id, notes
    a report to the terminal
"""

import argparse
import csv
import statistics as stats
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Crop ids must match crop_parameters.json. 'ignore' parks a string you have
# looked at and decided is not relevant, so it stops appearing as unmapped.
KNOWN_IDS = ["eggplant", "sweet_pepper", "hot_pepper", "cauliflower", "broccoli",
             "garlic", "watermelon", "melon", "squash", "green_onion",
             "sweet_corn", "okra", "ignore"]

FIELDS = ["item", "crop_id", "days_seen", "first_seen", "last_seen",
          "median_mode_price", "peak_month", "trough_month", "median_qty_t", "notes"]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_existing(path):
    """Preserve crop_id and notes already filled in."""
    if not path or not Path(path).exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            item = r.get("item", "").strip()
            if item:
                out[item] = (r.get("crop_id", "").strip(), r.get("notes", "").strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="gam_prices_local.csv")
    ap.add_argument("--out", default="item_mapping.csv")
    ap.add_argument("--merge", default=None,
                    help="existing mapping file to carry forward")
    ap.add_argument("--min-days", type=int, default=1, dest="min_days")
    args = ap.parse_args()

    if not Path(args.prices).exists():
        sys.exit(f"{args.prices} not found. Run gam_parse.py first.")

    existing = load_existing(args.merge or args.out)

    by_item = defaultdict(lambda: {"dates": [], "mode": [], "qty": [],
                                   "by_month": defaultdict(list),
                                   "flagged": 0})

    total_rows = 0
    with open(args.prices, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            total_rows += 1
            item = r["item"].strip()
            rec = by_item[item]
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            rec["dates"].append(d)
            if r.get("order_ok") == "0":
                rec["flagged"] += 1
                continue                      # exclude dirty rows from stats
            try:
                mode = float(r["price_mode"])
                rec["mode"].append(mode)
                rec["by_month"][d.month].append(mode)
            except (TypeError, ValueError):
                pass
            try:
                rec["qty"].append(float(r["quantity"]))
            except (TypeError, ValueError):
                pass

    rows = []
    for item, rec in by_item.items():
        n = len(rec["dates"])
        if n < args.min_days:
            continue
        monthly = {m: stats.median(v) for m, v in rec["by_month"].items() if len(v) >= 5}
        peak = trough = ""
        if len(monthly) >= 6:
            peak = MONTHS[max(monthly, key=monthly.get) - 1]
            trough = MONTHS[min(monthly, key=monthly.get) - 1]
        crop_id, notes = existing.get(item, ("", ""))
        rows.append({
            "item": item,
            "crop_id": crop_id,
            "days_seen": n,
            "first_seen": min(rec["dates"]).isoformat(),
            "last_seen": max(rec["dates"]).isoformat(),
            "median_mode_price": round(stats.median(rec["mode"]), 1) if rec["mode"] else "",
            "peak_month": peak,
            "trough_month": trough,
            "median_qty_t": round(stats.median(rec["qty"]), 2) if rec["qty"] else "",
            "notes": notes,
        })

    rows.sort(key=lambda r: -r["days_seen"])

    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    # ------------------------------------------------------------- report
    mapped = sum(1 for r in rows if r["crop_id"])
    print(f"Rows read       : {total_rows}")
    print(f"Distinct items  : {len(by_item)}  ({len(rows)} above --min-days {args.min_days})")
    print(f"Already mapped  : {mapped}")
    print(f"Written to      : {args.out}\n")

    print("Valid crop_id values:")
    print("  " + ", ".join(KNOWN_IDS) + "\n")

    print("Top 30 items by days seen. These are the ones worth your time.")
    print(f"{'item':<26}{'days':>6}{'price':>8}{'qty t':>8}  {'peak':<5}{'trough':<7}{'span'}")
    for r in rows[:30]:
        span = f"{r['first_seen'][:4]}-{r['last_seen'][:4]}"
        print(f"{r['item']:<26}{r['days_seen']:>6}{str(r['median_mode_price']):>8}"
              f"{str(r['median_qty_t']):>8}  {r['peak_month']:<5}{r['trough_month']:<7}{span}")

    tail = [r for r in rows if r["days_seen"] < 100]
    print(f"\n{len(tail)} items appear on fewer than 100 days. Most are spelling")
    print("variants or renames of items already in the list above. Mapping them")
    print("to the same crop_id merges the series, which is usually what you want.")

    print("\nFill the crop_id column. Leave blank for anything you have not")
    print("decided on, or write 'ignore' to park it. Re-run with --merge to")
    print("regenerate the statistics without losing your entries.")


if __name__ == "__main__":
    main()
