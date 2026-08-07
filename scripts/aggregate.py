#!/usr/bin/env python3
"""
Turn the cleaned CSVs into small JSON files the app fetches at runtime.

Why: gam_prices_clean.csv is ~243,000 rows and around 25 MB. A phone will not
parse that, and the charts only ever need weekly aggregates. This reduces it
to one small index plus one file per item, so opening the Prices tab downloads
a few KB rather than the whole history. The CSV stays as the audit trail.

The item-to-crop mapping is OPTIONAL. Without it you get raw bulletin names,
which is enough to explore the data. With it, rows sharing a crop_id are
merged into one series and the app can attach prices to a plot.

Weekly medians, not means: the source contains occasional typos that survive
cleaning, and a median ignores them.

Usage:
    python aggregate.py
    python aggregate.py --mapping data/item_mapping.csv
    python aggregate.py --min-days 150

Output:
    data/prices_index.json      list of series, with coverage stats
    data/prices/<id>.json       one per series
    data/weather.json           daily + precomputed climatology
    data/meta.json              generation time, for the staleness banner
"""

import argparse
import csv
import json
import statistics as stats
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

EPOCH = date(1970, 1, 1)


def median(v):
    return stats.median(v) if v else None


def pct(v, q):
    if not v:
        return None
    s = sorted(v)
    i = (len(s) - 1) * q
    lo = int(i)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)


def r(x, n=1):
    return None if x is None else round(x, n)


def iso_week_key(d):
    """Days since epoch floored to a week. Matches the app's bucketing."""
    return (d - EPOCH).days // 7


def load_mapping(path):
    if not path or not Path(path).exists():
        return {}, {}
    out, labels = {}, {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            cid = (row.get("crop_id") or "").strip()
            item = (row.get("item") or "").strip()
            if not cid or cid == "ignore" or not item:
                continue
            out[item] = cid
            labels.setdefault(cid, []).append(item)
    return out, labels


def build_prices(args, outdir):
    src = Path(args.prices)
    if not src.exists():
        sys.exit(f"{src} not found. Run clean_prices.py first.")

    mapping, members = load_mapping(args.mapping)
    if mapping:
        print(f"Mapping: {len(mapping)} item names -> {len(members)} crops")
    else:
        print("No mapping applied. Series will use raw bulletin names.")

    series = defaultdict(list)
    skipped = 0
    with src.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("usable") != "1":
                skipped += 1
                continue
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                mode = float(row["price_mode"])
            except (ValueError, KeyError):
                continue
            item = row["item"].strip()
            key = mapping.get(item, item)
            try:
                lo = float(row["price_low"])
                hi = float(row["price_high"])
            except (TypeError, ValueError):
                lo = hi = None
            try:
                q = float(row["quantity"])
            except (TypeError, ValueError):
                q = None
            series[key].append((d, mode, lo, hi, q))

    print(f"Read {sum(len(v) for v in series.values()):,} usable rows "
          f"({skipped:,} skipped), {len(series)} series")

    pdir = outdir / "prices"
    pdir.mkdir(parents=True, exist_ok=True)
    for old in pdir.glob("*.json"):
        old.unlink()

    index = []
    for n, (key, rows) in enumerate(
            sorted(series.items(), key=lambda kv: -len(kv[1]))):
        days = len({d for d, *_ in rows})
        if days < args.min_days:
            continue
        rows.sort(key=lambda t: t[0])

        # ---- weekly series
        wk = defaultdict(lambda: {"m": [], "l": [], "h": [], "q": []})
        for d, mode, lo, hi, q in rows:
            b = wk[iso_week_key(d)]
            b["m"].append(mode)
            if lo is not None:
                b["l"].append(lo)
            if hi is not None:
                b["h"].append(hi)
            if q:
                b["q"].append(q)
        weeks = sorted(wk)
        weekly = [[w, r(median(wk[w]["m"])), r(median(wk[w]["l"])),
                   r(median(wk[w]["h"])), r(median(wk[w]["q"]), 2),
                   len(wk[w]["m"])] for w in weeks]

        # ---- seasonal profile: week of year across all years
        soy = defaultdict(lambda: {"m": [], "q": []})
        for d, mode, lo, hi, q in rows:
            woy = min(52, (d.timetuple().tm_yday - 1) // 7)
            soy[woy]["m"].append(mode)
            if q:
                soy[woy]["q"].append(q)
        seasonal = []
        for woy in range(53):
            b = soy.get(woy)
            if not b or len(b["m"]) < 4:
                continue
            seasonal.append([woy, r(pct(b["m"], .10)), r(pct(b["m"], .25)),
                             r(median(b["m"])), r(pct(b["m"], .75)),
                             r(pct(b["m"], .90)), r(median(b["q"]), 2),
                             len(b["m"])])

        sid = f"s{n:03d}"
        payload = {
            "id": sid, "name": key,
            "members": members.get(key, [key]) if mapping else [key],
            "first": rows[0][0].isoformat(), "last": rows[-1][0].isoformat(),
            "days": days,
            "weekly_cols": ["week", "mode", "low", "high", "qty_t", "n"],
            "weekly": weekly,
            "seasonal_cols": ["week_of_year", "p10", "p25", "median",
                              "p75", "p90", "qty_t", "n"],
            "seasonal": seasonal,
        }
        (pdir / f"{sid}.json").write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8")

        index.append({
            "id": sid, "name": key, "days": days,
            "first": rows[0][0].isoformat(), "last": rows[-1][0].isoformat(),
            "median_price": r(median([m for _, m, *_ in rows])),
            "median_qty_t": r(median([q for *_, q in rows if q]), 2),
            "mapped": bool(mapping and key in members),
        })

    (outdir / "prices_index.json").write_text(
        json.dumps({"epoch_days_per_week": 7, "unit": "fils per kg",
                    "note": "Wholesale at the Greater Amman central market. "
                            "NOT farmgate.",
                    "series": index}, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8")

    total = sum(f.stat().st_size for f in pdir.glob("*.json"))
    print(f"Wrote {len(index)} series, {total/1024:.0f} KB total, "
          f"largest {max((f.stat().st_size for f in pdir.glob('*.json')), default=0)/1024:.0f} KB")
    return len(index)


def build_weather(args, outdir):
    src = Path(args.weather)
    if not src.exists():
        print(f"{src} not found, skipping weather.")
        return
    rows = []
    with src.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            def num(k):
                try:
                    return float(row[k])
                except (TypeError, ValueError, KeyError):
                    return None
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            rows.append({"d": d, "tmax": num("temperature_2m_max"),
                         "tmin": num("temperature_2m_min"),
                         "rain": num("precipitation_sum") or 0.0,
                         "et0": num("et0_fao_evapotranspiration") or 0.0,
                         "settled": row.get("source") == "era5_archive"})
    rows.sort(key=lambda x: x["d"])
    settled = [x for x in rows if x["settled"] and x["tmax"] is not None]
    years = sorted({x["d"].year for x in settled})
    full = [y for y in years
            if sum(1 for x in settled if x["d"].year == y) >= 360]

    monthly = []
    for m in range(1, 13):
        sel = [x for x in settled if x["d"].month == m and x["d"].year in full]
        n_y = len(full) or 1
        monthly.append({
            "month": m,
            "tmax": r(stats.mean([x["tmax"] for x in sel])),
            "tmin": r(stats.mean([x["tmin"] for x in sel])),
            "rain": r(sum(x["rain"] for x in sel) / n_y),
            "et0": r(sum(x["et0"] for x in sel) / n_y),
            "frost_nights": r(sum(1 for x in sel if x["tmin"] <= 0) / n_y, 2),
        })

    hydro = defaultdict(lambda: {"rain": 0.0, "days": 0})
    for x in settled:
        hy = x["d"].year if x["d"].month >= 10 else x["d"].year - 1
        hydro[hy]["rain"] += x["rain"]
        hydro[hy]["days"] += 1
    seasons = [{"season": y, "rain": r(v["rain"])}
               for y, v in sorted(hydro.items()) if v["days"] >= 360]

    daily = [[(x["d"] - EPOCH).days, r(x["tmax"]), r(x["tmin"]),
              r(x["rain"]), r(x["et0"]), 1 if x["settled"] else 0]
             for x in rows if x["tmax"] is not None]

    payload = {
        "site": {"lat": 32.5, "lon": 36.2, "elevation_m": 680,
                 "source": "ERA5 reanalysis via Open-Meteo"},
        "first": rows[0]["d"].isoformat(), "last": rows[-1]["d"].isoformat(),
        "complete_years": full,
        "daily_cols": ["epoch_day", "tmax", "tmin", "rain_mm", "et0_mm", "settled"],
        "daily": daily,
        "monthly": monthly,
        "seasonal_rain": seasons,
        "note": "The most recent days are preliminary and get revised as ERA5 "
                "catches up. Rainfall seasons run October to September.",
    }
    p = outdir / "weather.json"
    p.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote weather.json, {p.stat().st_size/1024:.0f} KB, "
          f"{len(daily):,} days, {len(full)} complete years")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default="gam_prices_clean.csv")
    ap.add_argument("--weather", default="weather_daily.csv")
    ap.add_argument("--mapping", default="data/item_mapping.csv")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--min-days", type=int, default=150, dest="min_days",
                    help="drop series thinner than this; they cannot support "
                         "a seasonal profile")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    n = build_prices(args, outdir)
    build_weather(args, outdir)

    (outdir / "meta.json").write_text(json.dumps({
        "generated": datetime.now().isoformat(timespec="seconds"),
        "series": n,
    }, indent=1), encoding="utf-8")
    print("\nCommit the data folder and the app will pick it up.")


if __name__ == "__main__":
    main()
