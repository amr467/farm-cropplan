#!/usr/bin/env python3
"""
Build the deployable site into build/.

Nothing this produces is ever committed. GitHub Pages deploys build/ as an
artifact, which is what keeps the repo small: data/prices/ alone is 1.7 MB and
every series changes daily, so committing it would add roughly 600 MB of git
history a year for files that are entirely reproducible.

Pipeline: monthly observations -> one CSV -> clean -> aggregate -> build/.
It shells out to the existing scripts rather than reimplementing them, so
there is one copy of the outlier rules and one copy of the aggregation.

Usage:
    python scripts/build_site.py
    python scripts/build_site.py --skip-weather      # reuse a cached pull
"""

import argparse
import csv
import gzip
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

COLS = ["date", "item", "unit", "price_low", "price_mode", "price_high", "quantity"]


def run(cmd):
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run([sys.executable] + [str(c) for c in cmd],
                       capture_output=True, text=True)
    if r.stdout:
        print("  " + r.stdout.strip().replace("\n", "\n  "))
    if r.returncode != 0:
        print(r.stderr)
        sys.exit(f"Failed: {cmd[0]}")
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--build", default="build")
    ap.add_argument("--skip-weather", action="store_true", dest="skip_weather")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    scripts = root / "scripts"
    obs = root / "data" / "observations"
    build = root / Path(args.build)
    work = root / ".work"
    work.mkdir(exist_ok=True)

    if not obs.exists():
        sys.exit(f"{obs} not found. Run migrate_observations.py once, locally.")

    # ---- 1. stitch the monthly files back together
    merged = work / "prices_raw.csv"
    n = 0
    with merged.open("w", encoding="utf-8", newline="") as out:
        w = csv.DictWriter(out, fieldnames=COLS + ["order_ok"])
        w.writeheader()
        for path in sorted(obs.glob("*.csv.gz")):
            with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    # clean_prices.py expects the order_ok flag the parser sets
                    try:
                        lo = float(row["price_low"]); md = float(row["price_mode"])
                        hi = float(row["price_high"])
                        row["order_ok"] = int(lo <= md <= hi)
                    except (TypeError, ValueError):
                        row["order_ok"] = ""
                    w.writerow({k: row.get(k, "") for k in COLS + ["order_ok"]})
                    n += 1
    print(f"Stitched {n:,} rows from {len(list(obs.glob('*.csv.gz')))} monthly files")

    # ---- 2. clean
    cleaned = work / "prices_clean.csv"
    run([scripts / "clean_prices.py", "--prices", merged, "--out", cleaned])

    # ---- 3. weather
    weather = work / "weather_daily.csv"
    if not (args.skip_weather and weather.exists()):
        run([scripts / "weather_fetch.py", "--out", weather,
             "--meta", work / "weather_meta.json"])

    # ---- 4. aggregate straight into the build
    data = build / "data"
    data.mkdir(parents=True, exist_ok=True)
    run([scripts / "aggregate.py", "--prices", cleaned, "--weather", weather,
         "--mapping", root / "data" / "item_mapping.csv", "--outdir", data])

    # ---- 5. static files
    shutil.copy(root / "index.html", build / "index.html")
    for name in ("crop_yields.json",):
        src = root / name
        if src.exists():
            shutil.copy(src, build / name)
        else:
            print(f"  note: {name} absent, yield defaults will be blank")
    for name in ("item_mapping.csv", "crop_parameters.json"):
        src = root / "data" / name
        if src.exists():
            shutil.copy(src, data / name)

    # The Data tab fetches these directly and gunzips them in the browser, so
    # they have to be in the deployed artifact, not just in the repo. Roughly
    # 3 MB total and nothing downloads until a month is actually requested.
    obs_out = data / "observations"
    if obs_out.exists():
        shutil.rmtree(obs_out)
    shutil.copytree(obs, obs_out)
    n_obs = len(list(obs_out.glob("*.csv.gz")))
    print(f"  copied {n_obs} monthly observation files "
          f"({sum(f.stat().st_size for f in obs_out.iterdir())/1024/1024:.1f} MB)")

    (build / ".nojekyll").touch()      # stop Pages ignoring files
    state = root / "data" / "observations" / "_state.json"
    meta = {"built": datetime.now().isoformat(timespec="seconds")}
    if state.exists():
        meta.update(json.loads(state.read_text(encoding="utf-8")))
    existing = data / "meta.json"
    if existing.exists():
        meta.update(json.loads(existing.read_text(encoding="utf-8")))
    meta["generated"] = datetime.now().isoformat(timespec="seconds")
    existing.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    total = sum(f.stat().st_size for f in build.rglob("*") if f.is_file())
    print(f"\nBuilt {build} — {total/1024/1024:.2f} MB")


if __name__ == "__main__":
    main()
