"""The slimmed per-year files and the pre-parsed geo bundle must be drop-in equivalents of the full assets."""
from __future__ import annotations

import glob
import random
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Processing"))

from frontend.data import COLUMNS  # noqa: E402
from frontend.geo import GeoLookups  # noqa: E402
from processing import APP_COLUMNS, GEO_BUNDLE_NAME, MODEL_COLUMNS, PER_YEAR_COLUMNS  # noqa: E402

FILES = sorted(glob.glob(str(ROOT / "Data" / "processed" / "applications_enriched_*.parquet")))
BUNDLE = ROOT / "Data" / "reference" / GEO_BUNDLE_NAME


def test_app_columns_match_pipeline():
    """frontend/data.py COLUMNS and the pipeline's APP_COLUMNS must stay in sync (one decides reads, the other writes)."""
    assert set(COLUMNS) == set(APP_COLUMNS)
    assert set(PER_YEAR_COLUMNS) == set(APP_COLUMNS) | set(MODEL_COLUMNS)


@pytest.mark.skipif(not FILES, reason="no processed files")
def test_per_year_files_have_every_needed_column():
    for f in FILES:
        names = set(pq.read_schema(f).names)
        missing = [c for c in PER_YEAR_COLUMNS if c not in names]
        assert not missing, f"{Path(f).name} lacks {missing}"


@pytest.mark.skipif(not BUNDLE.exists(), reason="no geo bundle")
def test_bundle_matches_geojson_lookups():
    """Point lookups from the WKB bundle equal the ones from parsing the raw GeoJSON, on random London points."""
    fast = GeoLookups()
    assert fast.source == "bundle"
    slow = GeoLookups(bundle_path=ROOT / "does-not-exist.pkl")
    assert slow.source == "geojson"
    rng = random.Random(42)
    pts = [(rng.uniform(51.30, 51.68), rng.uniform(-0.48, 0.28)) for _ in range(200)]
    pts += [(51.54, -0.15), (51.5074, -0.1278), (51.4816, -0.0076), (51.6, 0.1)]  # Primrose Hill, Trafalgar Sq, Greenwich, Ilford-ish
    diffs = [(lat, lon, fast.at(lat, lon), slow.at(lat, lon)) for lat, lon in pts if fast.at(lat, lon) != slow.at(lat, lon)]
    assert not diffs, diffs[:3]
    hits = sum(1 for lat, lon in pts if fast.at(lat, lon)["conservation_area"])
    assert hits > 5, "expected some points inside conservation areas"
    assert fast.at(51.54, -0.15)["conservation_area_name"] == "Primrose Hill"
    assert fast.mask["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert len(fast.boroughs_geojson["features"]) == 33
