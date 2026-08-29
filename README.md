# Confident Planner

Directories
- Processing: Data processing from start to end
- Model: Trains, tunes, and runs model
- FrontEnd

## Postcode to conservation-area dummy

`Processing/conservation_area.py` converts a postcode into a nullable
`in_conservation_area` dummy. It uses an offline ONS Postcode Directory
(ONSPD) CSV for the postcode coordinate, then intersects that point with the
official GLA conservation-area polygons.

Install the dependencies:

```bash
python -m pip install -r requirements-conservation.txt
```

Run the CSV pipeline:

```bash
python -m Processing.conservation_area \
  --input applications.csv \
  --postcode-lookup ONSPD.csv \
  --output applications_with_conservation.csv
```

The input CSV must contain `postcode` (override with `--postcode-column`). The
ONSPD file must contain `pcds` or `pcd`, plus `oseast1m` and `osnrth1m`.

The output adds:

- `in_conservation_area`: `1`, `0`, or blank if the postcode is unresolved;
- `conservation_area_name`: the matching conservation area, when available.

The GLA layer is downloaded and cached under `data/reference/` on first use.
An existing GeoJSON, GeoPackage, or Shapefile can instead be supplied with
`--conservation-areas`.
