# Confident Planner

Tool to find the likelihood of a London planning application being approved.

Built as part of the [House London #1 data hackathon](https://house-london.uk/) hosted at [Newspeak House](https://newspeak.house/). Won the 2nd place jury prize and the 2nd place people's choice prize.

Explore it at [confidentplanner.andreithuler.com](https://confidentplanner.andreithuler.com).

## Team Members

- Andrei Thüler ([GitHub](https://github.com/athuler) / [LinkedIn](https://www.linkedin.com/in/andreithuler/) / [Website](https://andreithuler.com))
- Amy Li ([GitHub](https://github.com/amyli06) / [LinkedIn](https://www.linkedin.com/in/amy-li-0a28192b8/))
- Anson Kong ([GitHub](https://github.com/Hotstopper) / [LinkedIn](https://www.linkedin.com/in/anson-kongtszhin/))
- Mark Fothergill ([GitHub](https://github.com/markfoth) / [LinkedIn](https://www.linkedin.com/in/mark-fothergill/))
- Steven Li ([GitHub](https://github.com/InForsaken) / [LinkedIn](https://www.linkedin.com/in/stevenli02))

## Repository layout

- `Processing/` — data pipeline (`processing.py`)
- `Model/` — model training scripts and the standalone predictor + pickles
- `frontend/` + `run.py` — Flask web app
- `Data/` — git-ignored inputs, caches and processed outputs
- `tests/` — `python -m pytest tests -q`

## Installation

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Put the conservation-area GeoJSON (`Data/Conservation_Areas_*.geojson`) and the ward-density workbook
(`Data/TS006-Population-Density-2021-wd-ONS.xlsx`) in `Data/` — both are git-ignored. Everything else is downloaded
and cached on first run. The web app's ML toggle needs `scikit-learn>=1.9` (in `requirements.txt`).

Quick start: process one year with `python Processing/processing.py --years 2026 --per-year`, then `python run.py`
and open http://localhost:5000.

## Processing

`Processing/processing.py` builds the modelling dataset. By default it bulk-downloads every planning
application from the **London Planning Datahub** (Elasticsearch, `https://planningdata.london.gov.uk/api-guest/`)
for a configurable year range, then enriches each application with:

| Column | Source |
|---|---|
| `Lat`, `Lon` (+ `latlon_source`) | Datahub site centroid (sanity-checked against the London bbox), else postcode centroid from postcodes.io |
| `Borough` (+ `borough_source`) | postcodes.io admin district → Datahub `lpa_name` → normalised raw column |
| `Month`, `Day of the Week` | from `Valid date` |
| `Conservation Area?`, `conservation_area_name` | point-in-polygon against `Data/Conservation_Areas_*.geojson` (Historic England) |
| `Flood risk?`, `flood_zone` | point-in-polygon against planning.data.gov.uk `flood-risk-zone` polygons (downloaded once for London) |
| `Population Density` (+ `(Ward)`, `(OA)`, `(LSOA)`, `(Borough)`, `population_density_level`) | ONS Census 2021 TS006 — ward level from `Data/TS006-Population-Density-2021-wd-ONS.xlsx`; OA/LSOA/borough from the Nomis bulk zip |
| `Distance to Park (m)` | nearest OpenStreetMap `leisure=park` (Overpass), optional |
| `dh_*` (~190 columns) | every other Datahub field, flattened (`dh_ward`, `dh_uprn`, `dh_application_type_full`, `dh_application_details.site_area`, residential unit / parking / infrastructure details, …) |

### Run

```bash
python Processing/processing.py                          # current year only (default), all steps
python Processing/processing.py --years 2016-2026 --per-year   # full history (~940k rows), one file per year, newest first (what the web app reads)
python Processing/processing.py --years 2016-2026        # same, as a single file
python Processing/processing.py --years 2024 --skip-parks
python Processing/processing.py --years 2026 --limit 15 --show-sample 5 -v   # quick check on a few rows
python Processing/processing.py --columns slim           # fewer Datahub columns = faster download
python Processing/processing.py --source csv             # use the export CSVs in Data/ instead
python Processing/processing.py --refresh-cache datahub  # force a re-download
```

Flags: `--years 2016-2026|2024|2019,2021`, `--per-year`, `--columns default|full|slim|<comma list>`, `--steps geocode,conservation,flood,density,parks`,
`--limit N`, `--lpa-numbers a,b`, `--show-sample K`, `--max-null-frac F`, `--refresh-cache [all|datahub|postcodes|parks|flood]`, `-v`.

Output: `Data/processed/applications_enriched[_<year>].csv` + `.parquet` (`--out` to change); log in
`Data/processed/processing.log`. `--max-null-frac 0.6` drops columns that are more than 60 % missing/empty (NaN, `""`
and whitespace-only all count) — key columns (`Approved?`, descriptions, dates, coordinates, ids) are always kept; the
default `1.0` keeps every column, which the web app relies on.

### Caching

- `Data/datahub/applications_<year>.<columns>.jsonl.gz` + `.meta.json` + `.parquet` — raw and flattened Datahub
  pages per year; complete years whose server count is unchanged are skipped; interrupted years resume.
- `Data/cache/postcodes.json`, `parks_osm.json`, `datahub_ids.json` — API responses (negative results too).
- `Data/reference/` — TS006 zip, London flood-zone GeoJSON.

A repeat run makes no API calls except one cheap count per year. Set `LONDON_DATAHUB_KEY` to override the guest key.

### Data sources

| Data | Source | Licence |
|---|---|---|
| Applications | [London Planning Datahub](https://planninglondondatahub.london.gov.uk/) | GLA / OGL v3 |
| Postcode → coordinates, borough, ward, OA/LSOA | [postcodes.io](https://postcodes.io) | OGL v3 / OS OpenData |
| Conservation areas | Historic England GeoJSON (in `Data/`) | OGL v3 |
| Flood risk zones | [planning.data.gov.uk](https://www.planning.data.gov.uk/docs) `flood-risk-zone` | OGL v3 |
| Population density | ONS Census 2021 [TS006](https://www.nomisweb.co.uk/datasets/c2021ts006) | OGL v3 |
| Parks | OpenStreetMap via Overpass | ODbL |

## Web app (`frontend/`, `run.py`)

One page: a London map with each borough coloured by the share of decided applications that were approved.
Click a borough to zoom in and see a ~500 m grid heatmap of approval rates; the overlay shows the borough's stats (by conservation/flood/density/day/month/application type);
click anywhere inside it to assess that point — a box shows the likelihood (nearest decided applications within 250 m–2 km), the rate among
neighbours with the same conservation/flood status, the borough average, and the point's features
(conservation area, flood zone, ward population density, distance to park), with day-of-week and month toggles.
Sidebar toggles (flood zone, conservation area, month, day of the week, application type, year range) re-query
every view.

### Running the local server

```bash
source venv/bin/activate
python Processing/processing.py --years 2026 --per-year          # at least one processed year is needed (~a few minutes)
# python Processing/processing.py --years 2016-2026 --per-year   # everything, newest year first (can keep running in the background)
python run.py                                                    # http://localhost:5000
```

Flags: `--port 5000` (change the port), `--host 0.0.0.0` (default: reachable from other machines on your network; use
`--host 127.0.0.1` for local only), `--debug` (Flask auto-reload; slower, reloads the data on every code edit).
Stop it with `Ctrl+C`.

Startup takes ~10–30 s while it loads every `Data/processed/applications_enriched_<year>.parquet`, downloads the
borough boundaries once (cached in `Data/reference/`), and loads the ML model from `Model/` if present — watch for
`Running on http://…` in the terminal. If `Data/processed/` is empty the map has no colours: run the pipeline first.
You can keep the pipeline running while the server is up; the app picks up each newly finished year automatically.
If the ML toggle stays greyed out, the error is shown under the model buttons (usually an old `scikit-learn`).
On WSL2 open the URL from a Windows browser as usual.

### Prediction models

The sidebar's bottom toggle switches every view between **Historical** (share of decided applications approved,
2016–26) and the **ML model** in `Model/` (logistic regression over location, type, borough/ward, site metrics,
density, park distance, conservation flag and a TF-IDF description embedding; trained 2022–26, AUC ≈ 0.73).
Stored applications are never scored (their outcomes are known). In ML mode the app scores a **hypothetical new
application** with your current settings (type, month, weekday, and the description you type in the sidebar box —
the model relies heavily on the text) at each borough centre, at each heatmap cell centre, and at the point you
click; the borough/point panels show one-feature-at-a-time sensitivities (day, month, conservation, density, type).
A hypothetical application is given the fields a real one of its type has (full type e.g. "Householder planning
permission", decision process "Delegated", no CIL liability) — leaving them blank puts the model out of distribution
and depresses predictions by ~20 pp. When no single application type is selected, Householder is assumed.
Pass `model=ml&description=…` on any API call. Tests: `python -m pytest tests -q`.

What the ML model actually reacts to (measured at a fixed point, Householder with a description):

| Input | Where the app gets it | Effect |
|---|---|---|
| Application type (+ full type) | sidebar, single selection | large — Householder ≈ 90 %, All Other ≈ 84 %, Prior Approval ≈ 45 % |
| Description text | sidebar box | large — several pp between texts, −3 pp when empty |
| Borough | polygon lookup | medium — up to ±7 pp between boroughs |
| Ward, month, weekday, conservation area | nearest application / sidebar / polygon | small — ≤ 1 pp each |
| Population density, distance to park, lat/lon | nearest application / sidebar band | negligible (< 0.5 pp) |
| Flood zone, year range | — | not model inputs; greyed out in ML mode |

API: `/api/rates`, `/api/heatmap/<borough>`, `/api/point?lat=&lon=`, `/api/options`, `/api/boroughs.geojson` —
all accept the filter query params `flood=any|yes|no`, `conservation=any|yes|no`, `months=1,2`, `days=Monday,…`,
`app_types=…`, `density=low,medium,high`, `year_min`, `year_max`.

## Model training (`Model/`)

`Model/main.py` trains an approval-probability model on `Data/processed/data.csv` — produce it with
`python Processing/processing.py --years 2016-2026 --out Data/processed/data.csv --max-null-frac 0.6`
(the web app's per-year files keep every column; the training set prunes mostly-empty ones):

- **Recency-weighted 80/20 split** — both train and test sets skew recent: every row
  gets a weight (`WEIGHT_FOR_YEAR` in `Model/main.py`: 1.0 up to 2021, ramping to x10 for 2026),
  the same weights are passed to the fit as `sample_weight`, and the test set is drawn with
  probabilities proportional to the weights. `--no-weights` runs the unweighted ablation.
- **Model** — sparse one-hot + scaled numerics into `LogisticRegression` (calibrated
  probabilities, fast on ~1M sparse rows). Metrics: ROC-AUC, PR-AUC, Brier, log loss.
- `--embeddings Data/processed/description_vectors.csv` joins description vectors
  (32 SVD components, fit on train only) as extra features.
- `--save-model` pickles the fitted pipeline to `Data/processed/approval_model.pkl`.

`Model/embed_descriptions.py` embeds every distinct application description into a fixed-size
vector and writes `Data/processed/description_vectors.csv` (`description,vector` — the vector is
a JSON array string). Default embedder is TF-IDF over hashed 1-2 grams (scikit-learn only,
offline, 512 dims); pass `--model sentence-transformers/all-MiniLM-L6-v2` for semantic vectors.

```bash
python Model/embed_descriptions.py                      # needs data.csv first
python Model/main.py --embeddings Data/processed/description_vectors.csv --save-model
```

The standalone predictor (`Model/predict.py` + the two pickles) is what the web app's ML toggle loads; see `Model/AGENTS.md`.
