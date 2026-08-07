#!/usr/bin/env python3
"""
Data quality audit and cleaning for the scraped GAM price series.

Five checks, in the order they matter:

  1. DUPLICATES. Items appearing more than once in a single bulletin. The
     parse report showed several items on more days than the file has days,
     so this is happening. Left alone it double-weights observations.

  2. COLUMN ORDER, verified empirically instead of assumed. Counts how often
     each of the three price positions holds the largest, middle and smallest
     value across all rows. If position 0 is the maximum in 99% of rows, it is
     the high column and the current assignment is right. Settles by evidence
     what was previously settled by looking at one row.

  3. NEAR-DUPLICATE ITEM NAMES, using co-occurrence rather than name
     similarity. Two strings listed in the same bulletin on the same day are
     necessarily different products. Two similar strings that never co-occur
     and whose date ranges abut are probably a rename. Name similarity alone
     gets this wrong in both directions: اسود عجمي and اسود رفيع look alike
     and are different varieties; بندورة and بندوره are the same word spelled
     two ways. NOTHING IS MERGED AUTOMATICALLY. Suggestions go to a review
     file for you to accept or reject.

  4. OUTLIERS, per item, using median absolute deviation rather than standard
     deviation, since a single 10x typo would inflate an SD enough to hide
     itself.

  5. MISSING PRICES and zero values.

Output is a cleaned CSV plus two review files. The cleaning is conservative:
bad rows are flagged and excluded, never repaired, because there is no way to
know what a typo was meant to say.

Usage:
    python clean_prices.py
    python clean_prices.py --mad-k 8          # looser outlier rule
    python clean_prices.py --apply-merges item_merges.csv
"""

import argparse
import csv
import re
import statistics as stats
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Arabic normalisation. Folds orthographic variation that carries no meaning.
# Deliberately does NOT touch word content, so بندوره معلقه stays distinct
# from بندوره.
TATWEEL = "\u0640"
DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0653-\u0655]")
BIDI = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def normalise_ar(text):
    t = unicodedata.normalize("NFKC", text)
    t = BIDI.sub("", t)
    t = DIACRITICS.sub("", t)
    t = t.replace(TATWEEL, "")
    t = re.sub("[أإآٱ]", "ا", t)
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = t.replace("ؤ", "و").replace("ئ", "ي")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def edit_distance(a, b, cap=3):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def mad(values):
    """Median absolute deviation, scaled to be comparable to an SD."""
    if len(values) < 3:
        return None
    med = stats.median(values)
    d = stats.median([abs(v - med) for v in values])
    return 1.4826 * d, med


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="gam_prices_local.csv")
    ap.add_argument("--out", default="gam_prices_clean.csv")
    ap.add_argument("--mad-k", type=float, default=6.0, dest="mad_k")
    ap.add_argument("--apply-merges", default=None, dest="apply_merges",
                    help="CSV with from_item,to_item to apply after review")
    args = ap.parse_args()

    if not Path(args.prices).exists():
        sys.exit(f"{args.prices} not found. Run gam_parse.py first.")

    rows = []
    with open(args.prices, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            def num(k):
                try:
                    return float(r[k])
                except (TypeError, ValueError, KeyError):
                    return None
            rows.append({
                "date": r["date"], "item": r["item"].strip(), "unit": r["unit"],
                "low": num("price_low"), "mode": num("price_mode"),
                "high": num("price_high"), "qty": num("quantity"),
                "order_ok": r.get("order_ok", ""),
            })
    print(f"Loaded {len(rows)} rows\n")

    # ---------------------------------------------------------- 1 duplicates
    print("=" * 70)
    print("1. DUPLICATE (date, item) PAIRS")
    print("=" * 70)
    seen = defaultdict(list)
    for i, r in enumerate(rows):
        seen[(r["date"], r["item"])].append(i)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    print(f"{len(dupes)} date-item pairs appear more than once "
          f"({sum(len(v) - 1 for v in dupes.values())} extra rows)")

    identical = conflicting = 0
    for k, idxs in dupes.items():
        vals = {(rows[i]["low"], rows[i]["mode"], rows[i]["high"], rows[i]["qty"])
                for i in idxs}
        if len(vals) == 1:
            identical += 1
        else:
            conflicting += 1
    print(f"  {identical} are byte-identical repeats, safe to drop")
    print(f"  {conflicting} disagree on price or quantity")
    if conflicting:
        print("\n  Examples of conflicting duplicates:")
        shown = 0
        for k, idxs in dupes.items():
            vals = {(rows[i]["low"], rows[i]["mode"], rows[i]["high"]) for i in idxs}
            if len(vals) > 1 and shown < 5:
                print(f"    {k[0]}  {k[1]}")
                for i in idxs:
                    print(f"       low={rows[i]['low']} mode={rows[i]['mode']} "
                          f"high={rows[i]['high']} qty={rows[i]['qty']}")
                shown += 1
        print("\n  Conflicting duplicates are kept but flagged, not averaged.")
        print("  Averaging two contradictory records invents a third value.")

    # -------------------------------------------------------- 2 column order
    print("\n" + "=" * 70)
    print("2. COLUMN ORDER, VERIFIED")
    print("=" * 70)
    print("The parser assumes rendered order is high, mode, low. Counting how")
    print("often each position actually holds the largest value:\n")
    pos_rank = [Counter(), Counter(), Counter()]
    n_ranked = 0
    for r in rows:
        trio = [r["high"], r["mode"], r["low"]]   # as currently assigned
        if any(v is None for v in trio) or len(set(trio)) < 3:
            continue
        order = sorted(range(3), key=lambda i: -trio[i])
        for rank, pos in enumerate(order):
            pos_rank[pos]["largest middle smallest".split()[rank]] += 1
        n_ranked += 1

    labels = ["position 0 (parsed as high)", "position 1 (parsed as mode)",
              "position 2 (parsed as low)"]
    print(f"{'':<30}{'largest':>10}{'middle':>10}{'smallest':>10}")
    for i, lab in enumerate(labels):
        c = pos_rank[i]
        print(f"{lab:<30}{c['largest'] / n_ranked:>9.1%}"
              f"{c['middle'] / n_ranked:>10.1%}{c['smallest'] / n_ranked:>10.1%}")
    print(f"\n  based on {n_ranked} rows with three distinct values")
    print("  Expect position 0 largest, 1 middle, 2 smallest, each near 99%.")
    print("  Anything below about 90% means the assignment is wrong.")

    # ------------------------------------------------------ 3 name clustering
    print("\n" + "=" * 70)
    print("3. NEAR-DUPLICATE ITEM NAMES")
    print("=" * 70)
    days_by_item = defaultdict(set)
    for r in rows:
        days_by_item[r["item"]].add(r["date"])
    items = sorted(days_by_item, key=lambda i: -len(days_by_item[i]))
    norm = {i: normalise_ar(i) for i in items}

    exact = defaultdict(list)
    for i in items:
        exact[norm[i]].append(i)
    exact_groups = {k: v for k, v in exact.items() if len(v) > 1}

    print(f"{len(exact_groups)} groups differ ONLY by Arabic orthography")
    print("(alef forms, ta marbuta, ya, diacritics, spacing):\n")
    for k, group in list(exact_groups.items())[:15]:
        co = 0
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                co = max(co, len(days_by_item[group[a]] & days_by_item[group[b]]))
        verdict = "CO-OCCUR, different products" if co > 10 else "likely same, safe to merge"
        print(f"  {' | '.join(group)}")
        print(f"      days each: {[len(days_by_item[g]) for g in group]}"
              f"   shared days: {co}   -> {verdict}")

    suggestions = []
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            ia, ib = items[a], items[b]
            if norm[ia] == norm[ib]:
                continue
            d = edit_distance(norm[ia], norm[ib], cap=2)
            if d > 2:
                continue
            shared = len(days_by_item[ia] & days_by_item[ib])
            smaller = min(len(days_by_item[ia]), len(days_by_item[ib]))
            rate = shared / smaller if smaller else 0
            suggestions.append({
                "item_a": ia, "item_b": ib, "edit_distance": d,
                "days_a": len(days_by_item[ia]), "days_b": len(days_by_item[ib]),
                "shared_days": shared, "cooccurrence_rate": round(rate, 3),
                "verdict": "DIFFERENT (co-occur)" if rate > 0.05 else "possible rename",
                "decision": "",
            })

    with open("item_review.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["item_a", "item_b", "edit_distance",
                                           "days_a", "days_b", "shared_days",
                                           "cooccurrence_rate", "verdict", "decision"])
        w.writeheader()
        w.writerows(sorted(suggestions, key=lambda s: s["cooccurrence_rate"]))

    renames = [s for s in suggestions if s["verdict"] == "possible rename"]
    print(f"\n{len(suggestions)} name pairs within edit distance 2, of which")
    print(f"{len(renames)} never meaningfully co-occur and may be renames.")
    print("Written to item_review.csv, sorted with the likeliest merges first.")
    print("Fill the 'decision' column with 'merge' or 'keep'. Nothing is")
    print("merged until you say so.")

    # ------------------------------------------------------------ 4 outliers
    print("\n" + "=" * 70)
    print("4. OUTLIERS  (median absolute deviation, k = %.1f)" % args.mad_k)
    print("=" * 70)
    by_item = defaultdict(list)
    for r in rows:
        if r["mode"] is not None and r["mode"] > 0:
            by_item[r["item"]].append(r["mode"])
    thresholds = {}
    for item, vals in by_item.items():
        m = mad(vals)
        if m and m[0] > 0:
            thresholds[item] = m

    n_out = 0
    for r in rows:
        r["outlier"] = 0
        t = thresholds.get(r["item"])
        if t and r["mode"] is not None:
            scale, med = t
            if abs(r["mode"] - med) > args.mad_k * scale:
                r["outlier"] = 1
                n_out += 1
    print(f"{n_out} rows ({n_out / len(rows):.2%}) more than {args.mad_k} MAD "
          f"from their item's median")

    # ------------------------------------------------- 5 missing and zeroes
    print("\n" + "=" * 70)
    print("5. MISSING AND ZERO VALUES")
    print("=" * 70)
    missing = sum(1 for r in rows if None in (r["low"], r["mode"], r["high"]))
    zeros = sum(1 for r in rows if 0 in (r["low"], r["mode"], r["high"]))
    no_qty = sum(1 for r in rows if r["qty"] is None or r["qty"] == 0)
    print(f"  rows with a missing price : {missing}")
    print(f"  rows with a zero price    : {zeros}")
    print(f"  rows with no quantity     : {no_qty}")

    # --------------------------------------------------------------- output
    merges = {}
    if args.apply_merges and Path(args.apply_merges).exists():
        with open(args.apply_merges, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                merges[r["from_item"].strip()] = r["to_item"].strip()
        print(f"\nApplying {len(merges)} reviewed merges.")

    dup_flag = set()
    kept = set()
    for k, idxs in seen.items():
        for n, i in enumerate(idxs):
            if n == 0:
                kept.add(i)
            else:
                dup_flag.add(i)

    out_fields = ["date", "item", "item_raw", "unit", "price_low", "price_mode",
                  "price_high", "quantity", "order_ok", "outlier", "duplicate", "usable"]
    written = usable = 0
    with open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=out_fields)
        w.writeheader()
        for i, r in enumerate(rows):
            item = merges.get(r["item"], r["item"])
            is_dup = int(i in dup_flag)
            ok = int(r["order_ok"] == "1" and not r["outlier"] and not is_dup
                     and None not in (r["low"], r["mode"], r["high"])
                     and r["mode"] > 0)
            usable += ok
            written += 1
            w.writerow({
                "date": r["date"], "item": item, "item_raw": r["item"],
                "unit": r["unit"], "price_low": r["low"], "price_mode": r["mode"],
                "price_high": r["high"], "quantity": r["qty"],
                "order_ok": r["order_ok"], "outlier": r["outlier"],
                "duplicate": is_dup, "usable": ok,
            })

    print(f"\nWrote {written} rows to {args.out}")
    print(f"{usable} ({usable / written:.1%}) pass every check and carry usable=1.")
    print("Nothing was deleted. Every row is present with its flags, so you")
    print("can loosen a rule later without re-running the scrape.")


if __name__ == "__main__":
    main()
