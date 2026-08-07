#!/usr/bin/env python3
"""
Climate profile for the Mafraq farm, derived from weather_daily.csv.

Computes the things that actually constrain planting dates, none of which are
visible in a raw daily series:

  1. Last spring frost and first autumn frost, per year, at several
     thresholds, with percentiles. This is what sets the safe window for
     frost-sensitive crops.
  2. Frost-free season length by year.
  3. Monthly climatology: temperature, rainfall, reference ET.
  4. Seasonal rainfall on a hydrological year (Oct to May), which is the
     correct boundary for Jordan, not the calendar year.
  5. Heat stress day counts by month, at several thresholds.
  6. Growing degree day accumulation from any start date, so days-to-maturity
     can later be derived from heat rather than assumed from a catalogue.

Thresholds are parameters, not built-in truths. The 0 C / 2 C / 4 C set is
offered because screen-height reanalysis temperature understates ground frost;
which one is right for your field is an empirical question, not one this
script can answer.

No agronomic judgement is encoded here. It reports what the weather did.

Usage:
    python climate_profile.py
    python climate_profile.py --in weather_daily.csv --out climate_profile.json
    python climate_profile.py --gdd-from 2026-03-15 --gdd-base 10
"""

import argparse
import csv
import json
import statistics as stats
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

FROST_THRESHOLDS = [0.0, 2.0, 4.0]
HEAT_THRESHOLDS = [35.0, 38.0, 40.0]

# Spring frost is searched Jan-Jun, autumn frost Jul-Dec, so a single winter
# is not split across the calendar boundary.
SPRING_END_MONTH = 6
AUTUMN_START_MONTH = 7


def load(path):
    rows = []
    with open(path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            def num(key):
                v = r.get(key)
                if v in (None, "", "None"):
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            rows.append({
                "date": datetime.strptime(r["date"], "%Y-%m-%d").date(),
                "tmin": num("temperature_2m_min"),
                "tmax": num("temperature_2m_max"),
                "tmean": num("temperature_2m_mean"),
                "precip": num("precipitation_sum"),
                "et0": num("et0_fao_evapotranspiration"),
                "source": r.get("source", ""),
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def doy_to_label(doy, year=2001):
    """Day-of-year back to a readable month/day, using a non-leap reference."""
    if doy is None:
        return "n/a"
    return (date(year, 1, 1) + timedelta(days=int(round(doy)) - 1)).strftime("%d %b")


def frost_dates(rows, threshold):
    """Per year: last spring frost DOY, first autumn frost DOY."""
    last_spring, first_autumn = {}, {}
    for r in rows:
        if r["tmin"] is None or r["tmin"] > threshold:
            continue
        y, m = r["date"].year, r["date"].month
        doy = r["date"].timetuple().tm_yday
        if m <= SPRING_END_MONTH:
            last_spring[y] = max(last_spring.get(y, 0), doy)
        elif m >= AUTUMN_START_MONTH:
            first_autumn[y] = min(first_autumn.get(y, 400), doy)
    return last_spring, first_autumn


def complete_years(rows):
    """Years with near-full coverage, so partial years do not skew stats."""
    counts = defaultdict(int)
    for r in rows:
        counts[r["date"].year] += 1
    return {y for y, n in counts.items() if n >= 360}


def hydro_year(d):
    """Oct-Sep. Labelled by the starting calendar year."""
    return d.year if d.month >= 10 else d.year - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="weather_daily.csv")
    ap.add_argument("--out", default="climate_profile.json")
    ap.add_argument("--gdd-from", default=None,
                    help="date to accumulate growing degree days from, YYYY-MM-DD")
    ap.add_argument("--gdd-base", type=float, default=10.0,
                    help="base temperature. Crop specific, 10 C is a common "
                         "default for warm-season vegetables and is NOT a "
                         "verified value for any particular crop.")
    args = ap.parse_args()

    if not Path(args.infile).exists():
        sys.exit(f"{args.infile} not found. Run weather_fetch.py first.")

    rows = load(args.infile)
    settled = [r for r in rows if r["source"] == "era5_archive"]
    full = complete_years(settled)
    usable = [r for r in settled if r["date"].year in full]

    print(f"Loaded {len(rows)} rows, {len(settled)} settled, "
          f"{len(full)} complete years ({min(full)}-{max(full)})\n")

    profile = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": args.infile,
        "complete_years": sorted(full),
    }

    # ------------------------------------------------------------ frost
    print("=" * 68)
    print("FROST WINDOWS")
    print("=" * 68)
    print("Percentiles across complete years. The 90th percentile of last")
    print("spring frost is the conservative planting date: in 9 years out of")
    print("10, frost was over by then.\n")

    profile["frost"] = {}
    for thr in FROST_THRESHOLDS:
        spring, autumn = frost_dates(usable, thr)
        s_vals = [v for y, v in spring.items() if y in full]
        a_vals = [v for y, v in autumn.items() if y in full]

        print(f"Threshold {thr:.0f} C   "
              f"(spring frost in {len(s_vals)}/{len(full)} years, "
              f"autumn in {len(a_vals)}/{len(full)})")

        if s_vals:
            print(f"  last spring frost   p50 {doy_to_label(pct(s_vals, 50))}   "
                  f"p90 {doy_to_label(pct(s_vals, 90))}   "
                  f"latest {doy_to_label(max(s_vals))}")
        if a_vals:
            print(f"  first autumn frost  p50 {doy_to_label(pct(a_vals, 50))}   "
                  f"p10 {doy_to_label(pct(a_vals, 10))}   "
                  f"earliest {doy_to_label(min(a_vals))}")

        lengths = [autumn[y] - spring[y] for y in full
                   if y in spring and y in autumn]
        if lengths:
            print(f"  frost-free days     median {int(stats.median(lengths))}   "
                  f"range {min(lengths)} to {max(lengths)}")
        print()

        profile["frost"][f"{thr:g}C"] = {
            "last_spring_doy": {"p50": pct(s_vals, 50), "p90": pct(s_vals, 90),
                                "max": max(s_vals) if s_vals else None},
            "first_autumn_doy": {"p50": pct(a_vals, 50), "p10": pct(a_vals, 10),
                                 "min": min(a_vals) if a_vals else None},
            "years_with_spring_frost": len(s_vals),
            "years_with_autumn_frost": len(a_vals),
            "frost_free_days_median": stats.median(lengths) if lengths else None,
        }

    # ------------------------------------------------------- climatology
    print("=" * 68)
    print("MONTHLY CLIMATOLOGY")
    print("=" * 68)

    by_month = defaultdict(lambda: defaultdict(list))
    for r in usable:
        m = r["date"].month
        for key in ("tmax", "tmin", "et0"):
            if r[key] is not None:
                by_month[m][key].append(r[key])
    monthly_rain = defaultdict(lambda: defaultdict(float))
    monthly_et0 = defaultdict(lambda: defaultdict(float))
    for r in usable:
        if r["precip"] is not None:
            monthly_rain[r["date"].year][r["date"].month] += r["precip"]
        if r["et0"] is not None:
            monthly_et0[r["date"].year][r["date"].month] += r["et0"]

    print(f"{'':<6}{'Tmax':>8}{'Tmin':>8}{'Rain':>9}{'ET0':>9}   "
          f"{'heat days >35/38/40':>22}")
    print(f"{'':<6}{'C':>8}{'C':>8}{'mm':>9}{'mm':>9}")

    profile["monthly"] = {}
    for m in range(1, 13):
        label = date(2001, m, 1).strftime("%b")
        tmax = stats.mean(by_month[m]["tmax"]) if by_month[m]["tmax"] else None
        tmin = stats.mean(by_month[m]["tmin"]) if by_month[m]["tmin"] else None
        rain = stats.mean([monthly_rain[y][m] for y in full]) if full else None
        et0 = stats.mean([monthly_et0[y][m] for y in full]) if full else None

        heat = []
        for thr in HEAT_THRESHOLDS:
            n = sum(1 for t in by_month[m]["tmax"] if t >= thr)
            heat.append(n / len(full) if full else 0)

        print(f"{label:<6}{tmax:>8.1f}{tmin:>8.1f}{rain:>9.1f}{et0:>9.1f}   "
              f"{heat[0]:>6.1f}{heat[1]:>8.1f}{heat[2]:>8.1f}")

        profile["monthly"][label] = {
            "tmax_mean_c": tmax, "tmin_mean_c": tmin,
            "rain_mean_mm": rain, "et0_mean_mm": et0,
            "heat_days_per_year": dict(zip([f">{t:g}C" for t in HEAT_THRESHOLDS], heat)),
        }

    print("\nET0 is FAO-56 reference evapotranspiration: what a short grass")
    print("reference would use. Crop water need is ET0 x Kc, and Kc is")
    print("crop and growth-stage specific. Not applied here.")

    # ------------------------------------------------ hydrological rainfall
    print("\n" + "=" * 68)
    print("SEASONAL RAINFALL (Oct to Sep, labelled by starting year)")
    print("=" * 68)

    seasonal = defaultdict(float)
    season_days = defaultdict(int)
    for r in settled:
        if r["precip"] is None:
            continue
        hy = hydro_year(r["date"])
        seasonal[hy] += r["precip"]
        season_days[hy] += 1

    complete_seasons = {y: v for y, v in seasonal.items() if season_days[y] >= 360}
    if complete_seasons:
        values = sorted(complete_seasons.values())
        print(f"Complete seasons: {len(values)}")
        print(f"  driest  {values[0]:.0f} mm")
        print(f"  p25     {pct(values, 25):.0f} mm")
        print(f"  median  {pct(values, 50):.0f} mm")
        print(f"  p75     {pct(values, 75):.0f} mm")
        print(f"  wettest {values[-1]:.0f} mm")
        print("\nDriest and wettest five seasons:")
        ordered = sorted(complete_seasons.items(), key=lambda kv: kv[1])
        for y, v in ordered[:5]:
            print(f"  {y}/{str(y + 1)[2:]}  {v:6.0f} mm")
        print("  ...")
        for y, v in ordered[-5:]:
            print(f"  {y}/{str(y + 1)[2:]}  {v:6.0f} mm")

        profile["seasonal_rainfall_mm"] = {
            str(y): round(v, 1) for y, v in sorted(complete_seasons.items())
        }

    # ---------------------------------------------------------------- GDD
    if args.gdd_from:
        start = datetime.strptime(args.gdd_from, "%Y-%m-%d").date()
        print("\n" + "=" * 68)
        print(f"GROWING DEGREE DAYS from {start}, base {args.gdd_base} C")
        print("=" * 68)
        print("Across all complete years, how many days to reach each total.")
        print("Uses the simple average method: max(0, (Tmax+Tmin)/2 - base).\n")

        targets = [400, 800, 1200, 1600, 2000]
        results = defaultdict(list)
        for year in sorted(full):
            acc, hit = 0.0, {}
            day = date(year, start.month, start.day)
            index = {r["date"]: r for r in usable}
            for offset in range(365):
                r = index.get(day + timedelta(days=offset))
                if not r or r["tmin"] is None or r["tmax"] is None:
                    continue
                acc += max(0.0, (r["tmax"] + r["tmin"]) / 2 - args.gdd_base)
                for t in targets:
                    if t not in hit and acc >= t:
                        hit[t] = offset + 1
            for t in targets:
                if t in hit:
                    results[t].append(hit[t])

        print(f"{'GDD':>6}{'median days':>14}{'fastest':>10}{'slowest':>10}"
              f"{'years':>8}")
        for t in targets:
            v = results[t]
            if v:
                print(f"{t:>6}{int(stats.median(v)):>14}{min(v):>10}"
                      f"{max(v):>10}{len(v):>8}")
        print("\nBase temperature is crop specific. The default of 10 C is a")
        print("common value for warm-season vegetables and has NOT been")
        print("verified for any crop on your list. Set --gdd-base per crop.")

        profile["gdd"] = {
            "start": args.gdd_from, "base_c": args.gdd_base,
            "days_to_target": {str(t): {"median": stats.median(v),
                                        "min": min(v), "max": max(v)}
                               for t, v in results.items() if v},
        }

    Path(args.out).write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"\nWritten to {args.out}")


if __name__ == "__main__":
    main()
