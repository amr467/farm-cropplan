#!/usr/bin/env python3
"""
Daily weather history for the Mafraq farm, from Open-Meteo.

Two endpoints, because they cover different periods:

  archive-api  ERA5 reanalysis. Settled, consistent, but lags ~5 days.
  forecast     Preliminary analysis via past_days, covers the recent tail
               and the next week.

Rows are merged with the archive taking precedence, so provisional values are
overwritten by reanalysis once it catches up. Every row is flagged with its
source so you can tell settled data from preliminary at a glance.

Elevation is passed explicitly. The ERA5 grid cell around 32.5N 36.2E is not
at 680 m, and Open-Meteo applies a lapse-rate correction when told the real
site elevation. Without it, temperatures are wrong by a degree or two, which
matters for frost.

No API key. Free for non-commercial use.

Usage:
    pip install requests
    python weather_fetch.py
    python weather_fetch.py --start 1991-01-01     # longer frost baseline
    python weather_fetch.py --out data/weather.csv

Output:
    weather_daily.csv
    weather_meta.json   (fetch time, coords, elevation the model used)
"""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests")


ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"

LAT = 32.5
LON = 36.2
ELEVATION = 680          # metres, per the farm brief
TZ = "Asia/Amman"

# Daily variables. et0_fao_evapotranspiration is the FAO-56 reference ET,
# which is what the crop water calculations key off.
DAILY = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "rain_sum",
    "et0_fao_evapotranspiration",
    "shortwave_radiation_sum",
    "wind_speed_10m_max",
    "relative_humidity_2m_mean",
]

FIELDS = ["date"] + DAILY + ["source"]


def call(url, params):
    resp = requests.get(url, params=params, timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"{url} returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def to_rows(payload, source, wanted):
    """Turn Open-Meteo's column-oriented daily block into row dicts."""
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    rows = {}
    for i, day in enumerate(dates):
        row = {"date": day, "source": source}
        for var in wanted:
            series = daily.get(var)
            row[var] = series[i] if series and i < len(series) else None
        rows[day] = row
    return rows


def fetch_archive(start, end, daily_vars):
    params = {
        "latitude": LAT,
        "longitude": LON,
        "elevation": ELEVATION,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(daily_vars),
        "timezone": TZ,
    }
    return call(ARCHIVE, params)


def fetch_recent(past_days, daily_vars):
    params = {
        "latitude": LAT,
        "longitude": LON,
        "elevation": ELEVATION,
        "daily": ",".join(daily_vars),
        "past_days": min(past_days, 92),
        "forecast_days": 7,
        "timezone": TZ,
    }
    return call(FORECAST, params)


def probe_variables(start, daily_vars):
    """
    Open-Meteo occasionally renames or drops a daily variable. Rather than
    fail the whole run, ask for one day and keep only what comes back.
    """
    try:
        payload = fetch_archive(start, start, daily_vars)
    except RuntimeError as exc:
        # A bad variable name is a 400 with the offending name in the body.
        msg = str(exc)
        surviving = [v for v in daily_vars if v not in msg]
        if surviving and surviving != daily_vars:
            dropped = set(daily_vars) - set(surviving)
            print(f"  dropped unsupported variables: {sorted(dropped)}")
            return probe_variables(start, surviving)
        raise
    available = [v for v in daily_vars if v in (payload.get("daily") or {})]
    missing = set(daily_vars) - set(available)
    if missing:
        print(f"  not returned by the API, skipping: {sorted(missing)}")
    return available


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="1995-01-01",
                    help="earliest date. Longer history gives better frost stats.")
    ap.add_argument("--out", default="weather_daily.csv")
    ap.add_argument("--meta", default="weather_meta.json")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    today = date.today()

    print(f"Location  : {LAT}N {LON}E, elevation {ELEVATION} m, tz {TZ}")
    print(f"Range     : {start} to today\n")

    print("Checking which variables the API supports...")
    daily_vars = probe_variables(start, DAILY)
    print(f"  using {len(daily_vars)} variables\n")

    # Archive lags several days. Ask up to today anyway and let it return
    # what it has, rather than hardcoding a lag that may change.
    print("Fetching ERA5 archive...")
    archive_payload = fetch_archive(start, today, daily_vars)
    archive_rows = to_rows(archive_payload, "era5_archive", daily_vars)
    archive_rows = {d: r for d, r in archive_rows.items()
                    if r.get("temperature_2m_max") is not None}
    print(f"  {len(archive_rows)} days")

    last_archive = max(archive_rows) if archive_rows else None
    if last_archive:
        lag = (today - datetime.strptime(last_archive, "%Y-%m-%d").date()).days
        print(f"  archive ends {last_archive} ({lag} days behind today)")
    else:
        lag = 30

    print("\nFetching recent tail and short forecast...")
    recent_payload = fetch_recent(max(lag + 7, 14), daily_vars)
    recent_rows = to_rows(recent_payload, "preliminary", daily_vars)
    print(f"  {len(recent_rows)} days")

    merged = dict(recent_rows)
    merged.update(archive_rows)          # archive wins where both exist

    rows = [merged[d] for d in sorted(merged)]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date"] + daily_vars + ["source"],
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    meta = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "latitude": LAT,
        "longitude": LON,
        "elevation_requested_m": ELEVATION,
        "elevation_model_m": archive_payload.get("elevation"),
        "timezone": archive_payload.get("timezone"),
        "variables": daily_vars,
        "units": archive_payload.get("daily_units"),
        "archive_last_date": last_archive,
        "row_count": len(rows),
        "source": "Open-Meteo, ERA5 reanalysis",
    }
    Path(args.meta).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- report
    print(f"\nWrote {len(rows)} rows to {out}")
    print(f"Metadata to {args.meta}")
    print(f"Model grid elevation: {archive_payload.get('elevation')} m "
          f"(requested {ELEVATION} m)")

    settled = [r for r in rows if r["source"] == "era5_archive"]
    provisional = [r for r in rows if r["source"] != "era5_archive"]
    print(f"Settled (ERA5): {len(settled)}   Preliminary: {len(provisional)}")

    if settled:
        print(f"Span: {rows[0]['date']} to {rows[-1]['date']}")

        # Sanity checks only. No agronomic conclusions drawn here.
        temps = [r["temperature_2m_min"] for r in settled
                 if r.get("temperature_2m_min") is not None]
        if temps:
            print(f"\nMin temp recorded : {min(temps):.1f} C")
            frost_days = sum(1 for t in temps if t <= 0)
            years = len({r['date'][:4] for r in settled})
            print(f"Days at or below 0 C: {frost_days} across {years} years "
                  f"({frost_days / years:.1f} per year)")

        rain = {}
        for r in settled:
            if r.get("precipitation_sum") is not None:
                rain[r["date"][:4]] = rain.get(r["date"][:4], 0) + r["precipitation_sum"]
        full_years = {y: v for y, v in rain.items() if y not in (rows[0]['date'][:4],)}
        if full_years:
            values = sorted(full_years.values())
            print(f"\nAnnual rainfall, mm: min {values[0]:.0f}, "
                  f"median {values[len(values) // 2]:.0f}, max {values[-1]:.0f}")
            print("  (calendar years, not hydrological years. Jordan's rain")
            print("   season spans the new year, so seasonal totals will be")
            print("   computed properly downstream.)")


if __name__ == "__main__":
    main()
