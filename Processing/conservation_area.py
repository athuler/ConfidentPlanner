"""Convert postcodes into a London conservation-area membership dummy."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd


BRITISH_NATIONAL_GRID = "EPSG:27700"
GLA_CONSERVATION_AREAS_URL = (
    "https://gis.london.gov.uk/arcgis/rest/services/apps/"
    "planning_data_map_02/MapServer/205/query"
    "?where=1%3D1"
    "&outFields=objectid%2Csitename%2Cborough"
    "&returnGeometry=true"
    "&outSR=27700"
    "&f=geojson"
)


def normalize_postcode(value: object) -> str | None:
    """Normalize a postcode for matching, returning null for missing input."""

    if value is None or pd.isna(value):
        return None
    normalized = "".join(str(value).upper().split())
    return normalized or None


def download_gla_conservation_areas(
    destination: str | Path,
    *,
    url: str = GLA_CONSERVATION_AREAS_URL,
    timeout: int = 120,
) -> Path:
    """Download the official GLA conservation-area GeoJSON atomically."""

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ConfidentPlanner/1.0 conservation-area-pipeline"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()

    parsed = json.loads(payload)
    if parsed.get("type") != "FeatureCollection" or "features" not in parsed:
        raise ValueError("GLA response was not a GeoJSON FeatureCollection")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def load_conservation_areas(path: str | Path) -> gpd.GeoDataFrame:
    """Load GLA-style conservation polygons and standardize their fields."""

    areas = gpd.read_file(path)
    if areas.empty:
        raise ValueError("Conservation-area layer contains no features")
    if areas.crs is None:
        raise ValueError("Conservation-area layer has no CRS")

    name_column = _first_present(areas.columns, ("sitename", "name", "NAME"))
    areas = areas.to_crs(BRITISH_NATIONAL_GRID).copy()
    areas["_conservation_area_name"] = (
        areas[name_column].astype("string") if name_column else pd.NA
    )
    return areas[["_conservation_area_name", "geometry"]]


def load_postcode_lookup(path: str | Path) -> pd.DataFrame:
    """Load the postcode and British National Grid columns from an ONSPD CSV."""

    header = pd.read_csv(path, nrows=0)
    original_columns = list(header.columns)
    lower_to_original = {column.strip().lower(): column for column in original_columns}

    postcode_column = _first_present(lower_to_original, ("pcds", "pcd", "postcode"))
    easting_column = _first_present(lower_to_original, ("oseast1m", "easting"))
    northing_column = _first_present(lower_to_original, ("osnrth1m", "northing"))
    if postcode_column is None or easting_column is None or northing_column is None:
        raise ValueError(
            "Postcode lookup must contain pcds/pcd, oseast1m, and osnrth1m"
        )

    selected = [
        lower_to_original[postcode_column],
        lower_to_original[easting_column],
        lower_to_original[northing_column],
    ]
    raw = pd.read_csv(path, usecols=selected, dtype="string", low_memory=False)
    lookup = pd.DataFrame(
        {
            "_postcode_key": raw[selected[0]].map(normalize_postcode),
            "_easting": pd.to_numeric(raw[selected[1]], errors="coerce"),
            "_northing": pd.to_numeric(raw[selected[2]], errors="coerce"),
        }
    )
    return (
        lookup.dropna(subset=["_postcode_key", "_easting", "_northing"])
        .drop_duplicates("_postcode_key", keep="first")
        .reset_index(drop=True)
    )


def classify_postcodes(
    postcodes: pd.Series,
    conservation_areas: gpd.GeoDataFrame,
    postcode_lookup: pd.DataFrame,
) -> pd.DataFrame:
    """Return nullable conservation membership and area name per postcode.

    A resolved postcode receives 1 when its ONSPD point intersects at least one
    conservation polygon and 0 otherwise. An unresolved postcode receives null.
    """

    input_rows = pd.DataFrame(
        {
            "_row_id": range(len(postcodes)),
            "_postcode_key": postcodes.reset_index(drop=True).map(normalize_postcode),
        }
    )
    resolved = input_rows.merge(
        postcode_lookup,
        on="_postcode_key",
        how="left",
        validate="many_to_one",
    )
    has_point = resolved["_easting"].notna() & resolved["_northing"].notna()

    result = pd.DataFrame(index=range(len(postcodes)))
    result["in_conservation_area"] = pd.Series(pd.NA, dtype="Int8")
    result["conservation_area_name"] = pd.Series(pd.NA, dtype="string")
    if not has_point.any():
        return result

    points = gpd.GeoDataFrame(
        resolved.loc[has_point, ["_row_id"]],
        geometry=gpd.points_from_xy(
            resolved.loc[has_point, "_easting"],
            resolved.loc[has_point, "_northing"],
        ),
        crs=BRITISH_NATIONAL_GRID,
    )
    areas = conservation_areas.to_crs(BRITISH_NATIONAL_GRID).copy()
    if "_conservation_area_name" not in areas.columns:
        name_column = _first_present(areas.columns, ("sitename", "name", "NAME"))
        areas["_conservation_area_name"] = (
            areas[name_column].astype("string") if name_column else pd.NA
        )

    matches = gpd.sjoin(
        points,
        areas[["_conservation_area_name", "geometry"]],
        how="left",
        predicate="intersects",
    )
    resolved_ids = points["_row_id"].tolist()
    matched = matches[matches["index_right"].notna()]
    matched_ids = matched["_row_id"].unique().tolist()
    result.loc[resolved_ids, "in_conservation_area"] = 0
    result.loc[matched_ids, "in_conservation_area"] = 1

    names = matched.groupby("_row_id")["_conservation_area_name"].agg(
        _join_unique_names
    )
    for row_id, name in names.items():
        if name:
            result.at[row_id, "conservation_area_name"] = name
    return result


def enrich_dataframe(
    applications: pd.DataFrame,
    conservation_areas: gpd.GeoDataFrame,
    postcode_lookup: pd.DataFrame,
    *,
    postcode_column: str = "postcode",
) -> pd.DataFrame:
    """Append conservation-area outputs to a dataframe containing postcodes."""

    if postcode_column not in applications.columns:
        raise ValueError(f"Input does not contain postcode column: {postcode_column}")
    output = applications.copy()
    classification = classify_postcodes(
        output[postcode_column], conservation_areas, postcode_lookup
    )
    output["in_conservation_area"] = classification["in_conservation_area"].array
    output["conservation_area_name"] = classification["conservation_area_name"].array
    return output


def _first_present(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    column_set = set(columns)
    return next((candidate for candidate in candidates if candidate in column_set), None)


def _join_unique_names(values: pd.Series) -> str | None:
    names = sorted({str(value) for value in values.dropna() if str(value).strip()})
    return "; ".join(names) if names else None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert postcodes to a London conservation-area dummy."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--postcode-lookup", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--postcode-column", default="postcode")
    parser.add_argument(
        "--conservation-areas",
        type=Path,
        default=Path("data/reference/london_conservation_areas.geojson"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.conservation_areas.exists():
        download_gla_conservation_areas(args.conservation_areas)

    applications = pd.read_csv(args.input, low_memory=False)
    areas = load_conservation_areas(args.conservation_areas)
    postcode_lookup = load_postcode_lookup(args.postcode_lookup)
    output = enrich_dataframe(
        applications,
        areas,
        postcode_lookup,
        postcode_column=args.postcode_column,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)


if __name__ == "__main__":
    main()
