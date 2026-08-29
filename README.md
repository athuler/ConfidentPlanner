# Confident Planner

Directories
- Processing: Data processing from start to end
- Model: Trains, tunes, and runs model
- FrontEnd
- Data: raw data sources

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

### Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Put the conservation-area GeoJSON and the ward-density workbook in `Data/` (both are git-ignored / large).

### Run

```bash
python Processing/processing.py                          # current year only (default), all steps
python Processing/processing.py --years 2016-2026        # full history (~940k rows, ~10 min download)
python Processing/processing.py --years 2024 --skip-parks
python Processing/processing.py --years 2026 --limit 15 --show-sample 5 -v   # quick check on a few rows
python Processing/processing.py --columns slim           # fewer Datahub columns = faster download
python Processing/processing.py --source csv             # use the export CSVs in Data/ instead
python Processing/processing.py --refresh-cache datahub  # force a re-download
```

Flags: `--years 2016-2026|2024|2019,2021`, `--columns default|full|slim|<comma list>`, `--steps geocode,conservation,flood,density,parks`,
`--limit N`, `--lpa-numbers a,b`, `--show-sample K`, `--refresh-cache [all|datahub|postcodes|parks|flood]`, `-v`.

Output: `Data/processed/applications_enriched.csv` + `.parquet`; log in `Data/processed/processing.log`.

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
