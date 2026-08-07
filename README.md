# CropPlan

Cash crop planning for a farm in Mafraq, Jordan. Roughly 32.5°N 36.2°E, 680 m,
semi-arid, well-fed drip irrigation, 15 to 20 dunums available for vegetables
alongside an established olive orchard.

Live at: `https://<username>.github.io/<repo>/`

## What it does

- Draw a plot on satellite imagery, set row bearing and spacing, get a plant
  count from real row geometry rather than area division.
- Minimum seasonal water for a chosen crop, from the farm's own 31-year ET0
  and FAO-56 crop coefficients.
- Explore eleven years of Amman wholesale prices and quantities.
- Explore thirty years of local weather.

## What it does not do

- It does not know your yields. Defaults are Jordan national averages from
  FAOSTAT, which pool the Jordan Valley and protected cultivation with
  highland open field. Expect less here. Overwrite them.
- It does not forecast prices. The seasonal view shows what a harvest window
  has historically paid, as a distribution.
- Water figures are a floor. ETc only: no wetting losses, no application
  efficiency, no drainage, no rainfall credit.
- Published market prices are **wholesale, not farmgate**. Transport, boxes,
  labour and any intermediary come out before you see the money.
- Hot pepper, garlic and okra have no complete FAO-56 parameters. The app says
  so rather than substituting a related crop.

## Data sources

| What | Source | Notes |
|---|---|---|
| Prices, quantities | Greater Amman Municipality daily bulletin | Aug 2015 to present, ~2,900 trading days, ~228 items |
| Weather | ERA5 reanalysis via Open-Meteo | 1995 to present, elevation-corrected to 680 m |
| Crop coefficients, stage lengths | FAO-56 (Allen et al. 1998) Tables 11 and 12 | |
| Yield defaults | FAOSTAT QCL, Jordan | National averages |

## Data quality

The bulletin's header row lists prices as high, low, mode; the rendered order
is high, mode, low. Verified empirically at 99.4% across 217,660 rows.

Of 243,104 parsed rows, about 98% survive cleaning. Excluded: 129 duplicate
listings, ~1,200 rows more than 5 MAD from a rolling 21-day local median
(source typos, e.g. 28,000 against a local median of 750), rows with missing
or zero prices. Nothing is repaired, only flagged; every row stays in the CSV
with its flags.

542 non-Friday dates returned no bulletin. Fridays are excluded by design.

## Running the pipeline

```bash
pip install -r requirements.txt

# one-time backfill, several hours, resumable
python scripts/gam_fetch.py

# daily thereafter
python scripts/gam_fetch.py --end today
python scripts/weather_fetch.py

# process
python scripts/gam_parse.py
python scripts/clean_prices.py
python scripts/fetch_yields.py
```

The scraper is deliberately slow: one request every six seconds minimum,
sequential, session re-primed periodically. The site soft-fails to an empty
table under burst load, so empty responses are retried on a fresh session
before being recorded as a genuine no-data day.

## Layout

```
index.html          the app
data/               what the app fetches
scripts/            the pipeline
raw/                scraped bulletins (gitignored, keep locally)
state/              scraper checkpoints (gitignored)
```
