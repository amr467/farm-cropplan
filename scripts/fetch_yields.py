#!/usr/bin/env python3
"""
Pull Jordan crop yields from FAOSTAT, for use as editable defaults.

Why this exists: the app needs a starting number in the yield field, and I am
not willing to type one from memory. FAOSTAT publishes national yield by crop
and year, sourced from Jordan's own reporting, so the default comes with a
citation and a year range rather than an air of authority.

WHAT THESE NUMBERS ARE NOT: they are national averages. Jordan's vegetable
production is dominated by the Jordan Valley, which is warmer, lower and far
more intensive than Mafraq, and the figures pool open field with plastic house
and greenhouse production. A highland open-field plot at 680 m should expect
LESS than the national average, probably substantially. The app labels them as
national averages and asks you to overwrite them.

The script reports the last ten years individually as well as the median, so
the spread is visible instead of collapsing into one confident figure.

Usage:
    pip install requests
    python fetch_yields.py
    python fetch_yields.py --years 15

Output:
    crop_yields.json   consumed by cropplan.html
"""

import argparse
import csv
import io
import json
import statistics as stats
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests")

BASE = "https://faostatservices.fao.org/api/v1/en/data/QCL"
JORDAN = "112"      # FAOSTAT area code
YIELD = "5412"      # element: yield

# FAOSTAT item codes. Several of the app's crops share an aggregate item,
# which is recorded here rather than hidden.
ITEMS = {
    "eggplant":     ("399", "Eggplants (aubergines)", None),
    "sweet_pepper": ("401", "Chillies and peppers, green",
                     "FAOSTAT does not separate sweet from hot pepper. This "
                     "aggregate covers both."),
    "hot_pepper":   ("401", "Chillies and peppers, green",
                     "Same aggregate as sweet pepper. FAOSTAT does not split them."),
    "cauliflower":  ("393", "Cauliflowers and broccoli",
                     "FAOSTAT reports cauliflower and broccoli as one item."),
    "broccoli":     ("393", "Cauliflowers and broccoli",
                     "Same aggregate as cauliflower. Broccoli typically yields "
                     "less than cauliflower, so this figure likely overstates it."),
    "garlic":       ("406", "Garlic", None),
    "watermelon":   ("567", "Watermelons", None),
    "melon":        ("568", "Cantaloupes and other melons", None),
    "squash":       ("394", "Pumpkins, squash and gourds",
                     "Includes winter squash and pumpkin, which yield more per "
                     "hectare than courgette."),
    "green_onion":  ("402", "Onions and shallots, green", None),
    "sweet_corn":   ("446", "Maize (corn), green", None),
    "okra":         ("430", "Okra", None),
}


def fetch(item_code, start_year):
    params = {
        "area": JORDAN, "element": YIELD, "item": item_code,
        "year": ",".join(str(y) for y in range(start_year, datetime.now().year + 1)),
        "output_type": "csv", "show_codes": "true", "show_unit": "true",
        "show_flags": "false", "null_values": "false",
    }
    r = requests.get(BASE, params=params, timeout=90)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    out = []
    for row in rows:
        try:
            year = int(row.get("Year") or row.get("year"))
            val = float(row.get("Value") or row.get("value"))
        except (TypeError, ValueError):
            continue
        unit = (row.get("Unit") or row.get("unit") or "").strip()
        out.append((year, val, unit))
    return sorted(out)


def to_kg_per_dunum(value, unit):
    """
    FAOSTAT has published yield in several units over the years. Convert what
    we are given rather than assuming. 1 dunum = 0.1 ha.
    """
    u = unit.lower().replace(" ", "")
    if "100g/ha" in u or "hg/ha" in u:
        return value * 0.1 / 10          # hg/ha -> kg/ha -> kg/dunum
    if "kg/ha" in u:
        return value / 10
    if "t/ha" in u or "tonnes/ha" in u:
        return value * 100
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--out", default="crop_yields.json")
    args = ap.parse_args()
    start = datetime.now().year - args.years

    print(f"FAOSTAT QCL, Jordan, yield element, {start} onward\n")
    result = {}
    for crop, (code, label, caveat) in ITEMS.items():
        try:
            data = fetch(code, start)
        except Exception as exc:
            print(f"{crop:<14} FAILED: {exc}")
            continue
        if not data:
            print(f"{crop:<14} no data returned")
            result[crop] = {"kg_per_dunum": None, "faostat_item": label,
                            "note": "FAOSTAT returned no yield rows.",
                            "caveat": caveat}
            continue

        converted = [(y, to_kg_per_dunum(v, u)) for y, v, u in data]
        good = [(y, v) for y, v in converted if v is not None]
        if not good:
            unit = data[0][2]
            print(f"{crop:<14} unrecognised unit '{unit}', not converting")
            result[crop] = {"kg_per_dunum": None, "faostat_item": label,
                            "note": f"Unrecognised FAOSTAT unit '{unit}'.",
                            "caveat": caveat}
            continue

        vals = [v for _, v in good]
        med = stats.median(vals)
        result[crop] = {
            "kg_per_dunum": round(med, 1),
            "min": round(min(vals), 1), "max": round(max(vals), 1),
            "years": [good[0][0], good[-1][0]], "n": len(good),
            "faostat_item": label,
            "unit_reported": data[0][2],
            "caveat": caveat,
            "source": "FAOSTAT, Crops and livestock products (QCL), "
                      "yield element 5412, area Jordan (112). National average.",
        }
        print(f"{crop:<14} median {med:>7.0f} kg/dunum   "
              f"range {min(vals):.0f}-{max(vals):.0f}   "
              f"{good[0][0]}-{good[-1][0]}   [{label}]")

    payload = {
        "_meta": {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "source": "FAOSTAT (fao.org/faostat), QCL domain, Jordan.",
            "warning": "NATIONAL AVERAGES. Jordan's vegetable output is "
                       "dominated by the Jordan Valley, which is warmer, lower "
                       "and more intensive than Mafraq, and these figures pool "
                       "open field with protected cultivation. A highland "
                       "open-field plot should expect less. Treat as a "
                       "starting point to overwrite, not a projection.",
        },
        "crops": result,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"\nWritten to {args.out}")
    print("Load it in the app's Plan tab, or place it beside cropplan.html")
    print("and it will be picked up automatically.")


if __name__ == "__main__":
    main()
