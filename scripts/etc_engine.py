#!/usr/bin/env python3
"""
Crop water demand engine v3, FAO-56 single crop coefficient method.

Changes from v2, fixing what v2's own broccoli sweep exposed:

  1. THERMAL WINDOW GATE. v2 recommended planting broccoli on 30 June because
     GDD accumulated without an upper bound, so hotter always meant faster and
     faster always meant cheaper. The season is now checked against the
     temperature range the crop is actually grown in, derived the same way the
     GDD requirement is: run every FAO-56 Table 11 row for this crop from its
     stated planting month against Mafraq weather, record the mean temperature
     each growth stage experienced, and take the envelope across all rows and
     all years. Candidate plantings whose stages fall outside that envelope
     are rejected.

     Weakness, stated plainly: this assumes the FAO reference regions are
     thermally representative of what the crop tolerates. The California
     Desert in autumn is not Mafraq in autumn. It will be roughly right and
     occasionally wrong, and both the envelope and each candidate's position
     within it are printed so the error is visible rather than buried.

  2. GDD UPPER CAP. Optional --gdd-cap applies the upper cutoff method:
     Tmax above the cap is clipped before averaging, so extreme heat stops
     accelerating development without limit. Applied identically during
     calibration and application, or the requirements would not be comparable.

  3. MANUAL HEAT OVERRIDE. --heat-threshold plus --max-heat-days rejects
     seasons exceeding a real damage threshold, for when you have one. Also
     read from an optional heat_threshold_c field per crop in the JSON.
     Nothing is defaulted; absent means not applied.

  4. --gdd-sensitivity no longer demands --gdd-base, which was the whole
     point of that flag.

Frost gating and GDD-driven stage lengths carry over from v2 unchanged.

1 mm over 1 dunum (1000 m2) is 1 cubic metre.

Usage:
    python etc_engine.py --list
    python etc_engine.py --crop broccoli --fw 0.4 --eff 0.9 --gdd-sensitivity
    python etc_engine.py --crop broccoli --sweep --fw 0.4 --eff 0.9 --gdd-base 5
    python etc_engine.py --crop watermelon --sweep --fw 0.4 --eff 0.9 --gdd-base 10 --gdd-cap 32
    python etc_engine.py --crop broccoli --plant 09-15 --fw 0.4 --eff 0.9 --gdd-base 5 --envelope
"""

import argparse
import csv
import json
import math
import statistics as stats
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

MAX_SEASON_DAYS = 400
STAGE_NAMES = ["initial", "development", "mid-season", "late"]


# --------------------------------------------------------------- utilities

def svp(t):
    """Saturation vapour pressure, kPa. FAO-56 Eq. 11."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def parse_months(text):
    out = []
    for chunk in text.replace(";", "/").replace(",", "/").split("/"):
        key = chunk.strip().lower()[:3]
        if key in MONTHS and MONTHS[key] not in out:
            out.append(MONTHS[key])
    return out


# ------------------------------------------------------------------ inputs

def load_weather(path):
    rows, cols = {}, []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        for r in reader:
            def num(k):
                v = r.get(k)
                if v in (None, "", "None"):
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            rows[datetime.strptime(r["date"], "%Y-%m-%d").date()] = {
                "tmin": num("temperature_2m_min"),
                "tmax": num("temperature_2m_max"),
                "et0": num("et0_fao_evapotranspiration"),
                "precip": num("precipitation_sum"),
                "wind_mean": num("wind_speed_10m_mean"),
                "wind_max": num("wind_speed_10m_max"),
                "source": r.get("source", ""),
            }
    return rows, cols


def load_crops(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["crops"]}, data["_meta"]


def complete_years(weather):
    counts = defaultdict(int)
    for d, r in weather.items():
        if r["et0"] is not None and r["source"] == "era5_archive":
            counts[d.year] += 1
    return sorted(y for y, n in counts.items() if n >= 360)


# ------------------------------------------------------- FAO-56 core maths

def wind_at_2m(kmh):
    """km/h at 10 m to m/s at 2 m. FAO-56 Eq. 47."""
    if kmh is None:
        return None
    return (kmh / 3.6) * 4.87 / math.log(67.8 * 10 - 5.42)


def rhmin_from_temps(tmin, tmax, arid_correction=2.0):
    """FAO-56 Eq. 64 with the Annex 6 arid-climate correction."""
    if tmin is None or tmax is None:
        return None
    return 100.0 * svp(tmin - arid_correction) / svp(tmax)


def adjust_kc(kc_tab, u2, rhmin, h):
    """FAO-56 Eq. 62 / Eq. 65."""
    if u2 is None or rhmin is None:
        return kc_tab
    return kc_tab + (0.04 * (clamp(u2, 1, 6) - 2.0)
                     - 0.004 * (clamp(rhmin, 20, 80) - 45.0)) * (clamp(h, 0.1, 10) / 3.0) ** 0.3


def kc_on_day(i, lengths, kc_ini, kc_mid, kc_end):
    """FAO-56 Eq. 66."""
    l_ini, l_dev, l_mid, l_late = lengths
    if i <= l_ini:
        return kc_ini
    if i <= l_ini + l_dev:
        return kc_ini + (i - l_ini) / max(l_dev, 1) * (kc_mid - kc_ini)
    if i <= l_ini + l_dev + l_mid:
        return kc_mid
    prev = l_ini + l_dev + l_mid
    return kc_mid + (i - prev) / max(l_late, 1) * (kc_end - kc_mid)


# ------------------------------------------------------------------- heat

def gdd_day(rec, base, cap=None):
    """
    Simple average method with optional upper cutoff.
    Clipping Tmax at the cap before averaging stops extreme heat from
    accelerating development without limit.
    """
    if rec is None or rec["tmin"] is None or rec["tmax"] is None:
        return None
    tmax = rec["tmax"] if cap is None else min(rec["tmax"], cap)
    tmin = rec["tmin"] if cap is None else min(rec["tmin"], cap)
    return max(0.0, (tmax + tmin) / 2.0 - base)


def tmean_day(rec):
    if rec is None or rec["tmin"] is None or rec["tmax"] is None:
        return None
    return (rec["tmax"] + rec["tmin"]) / 2.0


def run_fao_reference(weather, years, lengths, month, base, cap):
    """
    Run one FAO-56 row's day counts from mid the stated planting month.
    Returns (per-stage GDD lists, per-stage mean-temperature lists).
    """
    gdd = [[], [], [], []]
    temps = [[], [], [], []]
    for y in years:
        try:
            start = date(y, month, 15)
        except ValueError:
            continue
        acc_g, acc_t, ok, offset = [0.0] * 4, [[] for _ in range(4)], True, 0
        for si, ln in enumerate(lengths):
            for _ in range(ln):
                rec = weather.get(start + timedelta(days=offset))
                g, t = gdd_day(rec, base, cap), tmean_day(rec)
                if g is None or t is None:
                    ok = False
                    break
                acc_g[si] += g
                acc_t[si].append(t)
                offset += 1
            if not ok:
                break
        if ok:
            for si in range(4):
                gdd[si].append(acc_g[si])
                temps[si].append(stats.mean(acc_t[si]))
    return gdd, temps


def build_reference(weather, years, crop, variant, base, cap, pool):
    """
    GDD requirement from the chosen variant. Thermal envelope pooled across
    every variant, since FAO-56 listing several planting months is evidence
    of the range the crop is grown in.
    """
    v = crop["stage_lengths_days"][variant]
    months = parse_months(v["plant_date"])
    if not months:
        return None, None, 0
    lengths = (v["ini"], v["dev"], v["mid"], v["late"])
    gdd, temps = run_fao_reference(weather, years, lengths, months[0], base, cap)
    if not gdd[0]:
        return None, None, 0
    requirement = [stats.mean(g) for g in gdd]
    n_used = len(gdd[0])

    envelope = [[min(t), max(t)] for t in temps]
    if pool:
        for i, other in enumerate(crop["stage_lengths_days"]):
            if i == variant:
                continue
            oms = parse_months(other["plant_date"])
            if not oms:
                continue
            ol = (other["ini"], other["dev"], other["mid"], other["late"])
            for m in oms:
                _, ot = run_fao_reference(weather, years, ol, m, base, cap)
                for si in range(4):
                    if ot[si]:
                        envelope[si][0] = min(envelope[si][0], min(ot[si]))
                        envelope[si][1] = max(envelope[si][1], max(ot[si]))
    return requirement, envelope, n_used


def stages_from_gdd(weather, start, requirements, base, cap):
    lengths, offset = [], 0
    for need in requirements:
        acc, days = 0.0, 0
        while acc < need:
            g = gdd_day(weather.get(start + timedelta(days=offset)), base, cap)
            if g is None:
                return None
            acc += g
            days += 1
            offset += 1
            if offset > MAX_SEASON_DAYS:
                return None
        lengths.append(days)
    return tuple(lengths)


# -------------------------------------------------------------- season run

def run_season(weather, crop, lengths, start, fw, eff, rain_frac,
               frost_threshold, heat_threshold):
    total = sum(lengths)
    recs = [weather.get(start + timedelta(days=k)) for k in range(total)]
    if any(r is None or r["et0"] is None for r in recs):
        return None

    l_ini, l_dev, l_mid, l_late = lengths
    bounds = [l_ini, l_ini + l_dev, l_ini + l_dev + l_mid, total]
    mid = recs[l_ini + l_dev: l_ini + l_dev + l_mid]
    late = recs[l_ini + l_dev + l_mid:] or mid

    def conditions(sl):
        us, rs = [], []
        for r in sl:
            u = wind_at_2m(r["wind_mean"] if r["wind_mean"] is not None else r["wind_max"])
            if u is not None:
                us.append(u)
            rh = rhmin_from_temps(r["tmin"], r["tmax"])
            if rh is not None:
                rs.append(rh)
        return (stats.mean(us) if us else None, stats.mean(rs) if rs else None)

    u2_mid, rh_mid = conditions(mid)
    u2_late, rh_late = conditions(late)

    h = crop["height_m"]["value"]
    kc_ini = fw * crop["kc"]["ini"]["value"]
    kc_mid = adjust_kc(crop["kc"]["mid"]["value"], u2_mid, rh_mid, h)
    kc_end_tab = crop["kc"]["end"]["value"]
    kc_end = (adjust_kc(kc_end_tab, u2_late, rh_late, h)
              if kc_end_tab > 0.45 else kc_end_tab)

    etc_stage = [0.0] * 4
    stage_temps = [[] for _ in range(4)]
    et0_t = etc_t = rain_t = 0.0
    frost_days = heat_days = 0
    coldest = hottest = None

    for k, r in enumerate(recs, start=1):
        etc = kc_on_day(k, lengths, kc_ini, kc_mid, kc_end) * r["et0"]
        etc_t += etc
        et0_t += r["et0"]
        rain_t += r["precip"] or 0.0
        if r["tmin"] is not None:
            if r["tmin"] <= frost_threshold:
                frost_days += 1
            coldest = r["tmin"] if coldest is None else min(coldest, r["tmin"])
        if r["tmax"] is not None:
            if heat_threshold is not None and r["tmax"] >= heat_threshold:
                heat_days += 1
            hottest = r["tmax"] if hottest is None else max(hottest, r["tmax"])
        for si, b in enumerate(bounds):
            if k <= b:
                etc_stage[si] += etc
                t = tmean_day(r)
                if t is not None:
                    stage_temps[si].append(t)
                break

    net = max(0.0, etc_t - rain_frac * rain_t)
    return {
        "start": start, "end": start + timedelta(days=total - 1),
        "lengths": lengths, "days": total,
        "et0_mm": et0_t, "etc_mm": etc_t, "rain_mm": rain_t,
        "gross_mm": net / eff, "etc_by_stage": etc_stage,
        "kc_ini": kc_ini, "kc_mid": kc_mid, "kc_end": kc_end,
        "u2_mid": u2_mid, "rhmin_mid": rh_mid,
        "frost_days": frost_days, "coldest": coldest,
        "heat_days": heat_days, "hottest": hottest,
        "stage_temp": [stats.mean(t) if t else None for t in stage_temps],
    }


def thermal_fit(result, envelope, tol):
    """
    True if every growth stage's mean temperature falls inside the envelope
    derived from the FAO-56 reference seasons, widened by tol degrees.
    Returns (fits, worst_stage_index, deviation_c).
    """
    worst, dev = None, 0.0
    for si, t in enumerate(result["stage_temp"]):
        if t is None or envelope[si][0] is None:
            continue
        lo, hi = envelope[si][0] - tol, envelope[si][1] + tol
        d = (t - hi) if t > hi else ((lo - t) if t < lo else 0.0)
        if d > dev:
            worst, dev = si, d
    return dev == 0.0, worst, dev


def evaluate(weather, years, crop, mmdd, args, requirement, envelope):
    month, dom = (int(x) for x in mmdd.split("-"))
    heat_thr = args.heat_threshold
    if heat_thr is None:
        heat_thr = crop.get("heat_threshold_c")

    runs = []
    for y in years:
        try:
            start = date(y, month, dom)
        except ValueError:
            continue
        if args.fixed_days:
            v = crop["stage_lengths_days"][args.variant]
            lengths = (v["ini"], v["dev"], v["mid"], v["late"])
        else:
            lengths = stages_from_gdd(weather, start, requirement,
                                      args.gdd_base, args.gdd_cap)
            if lengths is None:
                continue
        r = run_season(weather, crop, lengths, start, args.fw, args.eff,
                       args.rain_frac, args.frost_threshold, heat_thr)
        if r:
            fits, worst, dev = thermal_fit(r, envelope, args.thermal_tol)
            r["thermal_ok"], r["thermal_worst"], r["thermal_dev"] = fits, worst, dev
            runs.append(r)
    return runs


# --------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weather", default="weather_daily.csv")
    ap.add_argument("--crops", default="crop_parameters.json")
    ap.add_argument("--crop")
    ap.add_argument("--plant", help="planting date, MM-DD")
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--fw", type=float)
    ap.add_argument("--eff", type=float)
    ap.add_argument("--gdd-base", type=float, dest="gdd_base")
    ap.add_argument("--gdd-cap", type=float, default=None, dest="gdd_cap",
                    help="upper cutoff temperature. Tmax above this is clipped "
                         "before averaging. Crop specific, unsourced, no default.")
    ap.add_argument("--rain-frac", type=float, default=0.0, dest="rain_frac")
    ap.add_argument("--frost-threshold", type=float, default=0.0, dest="frost_threshold")
    ap.add_argument("--max-frost-risk", type=float, default=0.10, dest="max_frost_risk")
    ap.add_argument("--thermal-tol", type=float, default=1.5, dest="thermal_tol",
                    help="degrees C the envelope is widened by. Judgement, not "
                         "a sourced figure. 0 makes the gate strict.")
    ap.add_argument("--max-thermal-risk", type=float, default=0.20,
                    dest="max_thermal_risk",
                    help="sweep rejects dates outside the envelope in more "
                         "than this fraction of years")
    ap.add_argument("--no-pool", action="store_true", dest="no_pool",
                    help="build the envelope from the chosen variant only")
    ap.add_argument("--heat-threshold", type=float, default=None, dest="heat_threshold",
                    help="manual override. Tmax at or above this counts as a "
                         "heat event. Overrides heat_threshold_c in the JSON.")
    ap.add_argument("--max-heat-days", type=int, default=None, dest="max_heat_days",
                    help="reject seasons with more than this many heat days")
    ap.add_argument("--fixed-days", action="store_true", dest="fixed_days")
    ap.add_argument("--gdd-sensitivity", action="store_true", dest="gdd_sensitivity")
    ap.add_argument("--envelope", action="store_true",
                    help="print the derived thermal envelope and stop")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--sweep-step", type=int, default=15, dest="sweep_step")
    ap.add_argument("--show-all", action="store_true", dest="show_all")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    crops, meta = load_crops(args.crops)

    if args.list:
        print(f"{'id':<14}{'crop':<22}{'var':>4}  {'frost':<10}status")
        for cid, c in crops.items():
            n = len(c["stage_lengths_days"])
            ready = c["kc"]["mid"]["value"] is not None and n > 0
            ft = "tolerant" if c.get("frost_tolerant") else "sensitive"
            print(f"{cid:<14}{c['name_en']:<22}{n:>4}  {ft:<10}"
                  f"{'ready' if ready else 'INCOMPLETE: ' + (c.get('GAP') or '')[:38]}")
        return

    if not args.crop or args.crop not in crops:
        sys.exit("Specify a valid --crop, or --list.")
    crop = crops[args.crop]
    if crop["kc"]["mid"]["value"] is None or not crop["stage_lengths_days"]:
        sys.exit(f"{crop['name_en']} has no sourced parameters.\n{crop.get('GAP', '')}")
    if not Path(args.weather).exists():
        sys.exit(f"{args.weather} not found. Run weather_fetch.py first.")

    weather, cols = load_weather(args.weather)
    years = complete_years(weather)
    v = crop["stage_lengths_days"][args.variant]
    fao_lengths = (v["ini"], v["dev"], v["mid"], v["late"])
    plant_months = parse_months(v["plant_date"])

    # ------------------------------- sensitivity runs before any validation
    if args.gdd_sensitivity:
        print("=" * 72)
        print(f"{crop['name_en']}: GDD BASE SENSITIVITY")
        print("=" * 72)
        print("Derived heat requirement and the season length it produces, at")
        print(f"the FAO-56 reference planting ({v['plant_date']}, {v['region']}).\n")
        print(f"{'base C':>7}{'cap C':>8}{'GDD total':>12}{'season days':>14}")
        for b in (5.0, 8.0, 10.0, 12.0, 15.0):
            req, env, n = build_reference(weather, years, crop, args.variant,
                                          b, args.gdd_cap, not args.no_pool)
            if req is None:
                continue
            start = date(years[len(years) // 2], plant_months[0], 15)
            lg = stages_from_gdd(weather, start, req, b, args.gdd_cap)
            cap_s = f"{args.gdd_cap:g}" if args.gdd_cap else "none"
            print(f"{b:>7.0f}{cap_s:>8}{sum(req):>12.0f}"
                  f"{(sum(lg) if lg else 0):>14}")
        print("\nSeason length at the reference date should land near "
              f"{sum(fao_lengths)} days,")
        print("which is what FAO-56 states. Large departures mean the base")
        print("temperature is fighting the calibration rather than fitting it.")
        return

    if args.fw is None or args.eff is None:
        sys.exit("--fw and --eff are required and have no defaults.")
    if args.gdd_base is None and not args.fixed_days:
        sys.exit("--gdd-base is required. Use --gdd-sensitivity to explore it.")

    requirement, envelope, n_used = build_reference(
        weather, years, crop, args.variant, args.gdd_base or 10.0,
        args.gdd_cap, not args.no_pool)
    if requirement is None:
        sys.exit("Could not build the FAO-56 reference. Check the planting month.")

    # ------------------------------------------------------------- header
    print("=" * 72)
    print(f"{crop['name_en']}  /  {crop['name_ar']}")
    print("=" * 72)
    print(f"FAO-56 variant {args.variant}: {'/'.join(str(x) for x in fao_lengths)}"
          f" days, planted {v['plant_date']}, {v['region']}")
    print(f"Kc: ini {crop['kc']['ini']['value']}, mid {crop['kc']['mid']['value']}, "
          f"end {crop['kc']['end']['value']}, h {crop['height_m']['value']} m")
    print(f"Frost {'sensitive' if not crop.get('frost_tolerant') else 'tolerant'} "
          f"at {args.frost_threshold:g} C   |   fw {args.fw}, eff {args.eff}, "
          f"rain {args.rain_frac:.0%}")
    if args.gdd_cap:
        print(f"GDD upper cutoff: {args.gdd_cap:g} C")
    if "wind_speed_10m_mean" not in cols:
        print("NOTE: no wind_speed_10m_mean column, daily max used. "
              "Kc_mid biased up ~0.03.")

    print(f"\n{'DERIVED GDD REQUIREMENT':<40}base {args.gdd_base:g} C, "
          f"{n_used} reference years")
    for i, nm in enumerate(STAGE_NAMES):
        print(f"  {nm:<14}{requirement[i]:>8.0f} GDD   "
              f"({fao_lengths[i]} d in the FAO reference)")
    print(f"  {'TOTAL':<14}{sum(requirement):>8.0f} GDD")

    src = "all variants pooled" if not args.no_pool else "this variant only"
    print(f"\n{'DERIVED THERMAL ENVELOPE':<40}{src}, +/- {args.thermal_tol:g} C")
    print("  Mean temperature each stage saw in the FAO-56 reference seasons,")
    print("  run against Mafraq weather. Candidates outside this are rejected.")
    for i, nm in enumerate(STAGE_NAMES):
        print(f"  {nm:<14}{envelope[i][0]:>7.1f} to {envelope[i][1]:>5.1f} C")

    if args.envelope:
        return

    # ------------------------------------------------------------- sweep
    if args.sweep:
        sensitive = not crop.get("frost_tolerant")
        print("\n" + "=" * 72)
        print("PLANTING DATE SWEEP   gross irrigation, m3/dunum")
        print("=" * 72)
        print(f"* = FAO-56 planting month ({v['plant_date']}).  "
              f"F = frost reject, T = thermal reject.\n")
        print(f"{'':2}{'plant':<7}{'harvest':<8}{'days':>5}{'med':>7}"
              f"{'min':>6}{'max':>6}{'frost':>8}{'therm':>8}  flag")

        day, shown = date(2001, 1, 1), 0
        for _ in range(400):
            if day.year != 2001:
                break
            runs = evaluate(weather, years, crop, day.strftime("%m-%d"),
                            args, requirement, envelope)
            if runs:
                n = len(runs)
                frost_yrs = sum(1 for r in runs if r["frost_days"] > 0)
                therm_bad = sum(1 for r in runs if not r["thermal_ok"])
                if args.max_heat_days is not None:
                    therm_bad = max(therm_bad, sum(
                        1 for r in runs if r["heat_days"] > args.max_heat_days))
                flags = ""
                if sensitive and frost_yrs / n > args.max_frost_risk:
                    flags += "F"
                if therm_bad / n > args.max_thermal_risk:
                    flags += "T"
                if not flags or args.show_all:
                    g = sorted(r["gross_mm"] for r in runs)
                    md = int(stats.median([r["days"] for r in runs]))
                    mark = "*" if day.month in plant_months else " "
                    print(f"{mark:2}{day.strftime('%d %b'):<7}"
                          f"{(day + timedelta(days=md - 1)).strftime('%d %b'):<8}"
                          f"{md:>5}{stats.median(g):>7.0f}{g[0]:>6.0f}{g[-1]:>6.0f}"
                          f"{frost_yrs:>5}/{n:<2}{therm_bad:>5}/{n:<2}  {flags}")
                    shown += 1
            day += timedelta(days=args.sweep_step)

        if shown == 0:
            print("  No viable planting dates under these gates.")
            print("  Try --show-all to see what was rejected and why.")
        elif not args.show_all:
            print("\nRejected dates hidden. --show-all shows them flagged.")
        return

    # -------------------------------------------------- single planting
    if not args.plant:
        sys.exit("Specify --plant MM-DD, or --sweep.")
    runs = evaluate(weather, years, crop, args.plant, args, requirement, envelope)
    if not runs:
        sys.exit("No complete seasons for that planting date.")

    n = len(runs)
    lens = [r["days"] for r in runs]
    print("\n" + "=" * 72)
    print(f"SEASON  planted {runs[0]['start'].strftime('%d %b')}")
    print("=" * 72)
    print(f"  length   median {int(stats.median(lens))} d, "
          f"range {min(lens)} to {max(lens)}")

    frost_yrs = sum(1 for r in runs if r["frost_days"] > 0)
    print(f"  frost    {frost_yrs}/{n} years saw Tmin <= {args.frost_threshold:g} C"
          f"   coldest {min(r['coldest'] for r in runs):.1f} C")
    if frost_yrs and not crop.get("frost_tolerant"):
        print("           FROST SENSITIVE CROP, this planting is exposed.")

    bad = [r for r in runs if not r["thermal_ok"]]
    print(f"  thermal  {n - len(bad)}/{n} years inside the envelope")
    if bad:
        worst = max(bad, key=lambda r: r["thermal_dev"])
        si = worst["thermal_worst"]
        print(f"           worst departure {worst['thermal_dev']:.1f} C at the "
              f"{STAGE_NAMES[si]} stage")
        print(f"           that stage averaged {worst['stage_temp'][si]:.1f} C "
              f"against an envelope of {envelope[si][0]:.1f} to {envelope[si][1]:.1f}")

    print(f"\n  Kc  ini {stats.mean(r['kc_ini'] for r in runs):.3f}   "
          f"mid {stats.mean(r['kc_mid'] for r in runs):.3f}   "
          f"end {stats.mean(r['kc_end'] for r in runs):.3f}")

    print("\n" + "=" * 72)
    print("WATER, mm over the season  (= m3 per dunum)")
    print("=" * 72)
    for key, label in [("et0_mm", "Reference ET0"), ("etc_mm", "Crop ETc"),
                       ("rain_mm", "Rainfall"), ("gross_mm", "GROSS IRRIGATION")]:
        vals = sorted(r[key] for r in runs)
        print(f"  {label:<18} median {stats.median(vals):>6.0f}   "
              f"range {vals[0]:>5.0f} to {vals[-1]:>5.0f}")

    med = stats.median([r["gross_mm"] for r in runs])
    print(f"\n  15 dunum {med * 15:>8.0f} m3     20 dunum {med * 20:>8.0f} m3")
    print("\nWater only. No yield, no price, no margin.")


if __name__ == "__main__":
    main()
