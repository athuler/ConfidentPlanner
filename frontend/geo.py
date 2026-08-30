"""Point-in-polygon lookups for the web app (boroughs, conservation areas, flood zones).

Loads the pre-parsed bundle `Data/reference/app_geo.pkl` (built by `python Processing/processing.py --geo-bundle`)
when present - ~1 s - and falls back to parsing the raw GeoJSON files (~10-25 s) otherwise.
"""
from __future__ import annotations

import logging
import pickle
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "Processing"))
from processing import (  # noqa: E402
    GEO_BUNDLE_NAME, GEO_BUNDLE_VERSION, PolygonIndex, find_conservation_geojson, load_boroughs_geojson,
    load_geojson_features, london_mask,
)

log = logging.getLogger("frontend.geo")
REFERENCE_DIR = REPO_ROOT / "Data" / "reference"


class GeoLookups:
    def __init__(self, boroughs_geojson: dict | None = None, bundle_path: Path | None = None):
        t0 = time.perf_counter()
        bundle = self._read_bundle(bundle_path or REFERENCE_DIR / GEO_BUNDLE_NAME)
        if bundle:
            self.source = "bundle"
            self.boroughs_geojson = boroughs_geojson or bundle["boroughs"]
            self.mask = bundle["mask"]
            self.conservation = PolygonIndex.from_wkb("conservation areas", **bundle["conservation"]) if bundle.get("conservation") else None
            self.flood = PolygonIndex.from_wkb("flood risk zones", **bundle["flood"]) if bundle.get("flood") else None
        else:
            self.source = "geojson"
            self.boroughs_geojson = boroughs_geojson or load_boroughs_geojson(REFERENCE_DIR)
            self.mask = london_mask(self.boroughs_geojson)
            cons = find_conservation_geojson(REPO_ROOT / "Data")
            self.conservation = PolygonIndex("conservation areas", load_geojson_features(cons)) if cons else None
            flood = REFERENCE_DIR / "flood_risk_zones_london.geojson"
            self.flood = PolygonIndex("flood risk zones", load_geojson_features(flood)) if flood.exists() else None
        self.boroughs = PolygonIndex("boroughs", self.boroughs_geojson["features"])
        log.info("geo lookups ready from %s in %.1fs", self.source, time.perf_counter() - t0)

    @staticmethod
    def _read_bundle(path: Path) -> dict | None:
        if not path.exists():
            log.info("no geo bundle at %s - parsing GeoJSON instead (run: python Processing/processing.py --geo-bundle)", path)
            return None
        t0 = time.perf_counter()
        try:
            with open(path, "rb") as fh:
                bundle = pickle.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("geo bundle %s unreadable (%s) - falling back to GeoJSON", path, exc)
            return None
        if bundle.get("version") != GEO_BUNDLE_VERSION:
            log.warning("geo bundle version %s != %s - falling back to GeoJSON; rebuild with --geo-bundle", bundle.get("version"), GEO_BUNDLE_VERSION)
            return None
        log.info("geo bundle %s read in %.1fs (built %s)", path.name, time.perf_counter() - t0, bundle.get("built_at"))
        return bundle

    def at(self, lat: float, lon: float) -> dict:
        b = self.boroughs.lookup([lat], [lon])[0]
        c = self.conservation.lookup([lat], [lon])[0] if self.conservation else []
        f = self.flood.lookup([lat], [lon])[0] if self.flood else []
        levels = [int(p["flood-risk-level"]) for p in f if str(p.get("flood-risk-level", "")).isdigit()]
        return {
            "borough": b[0].get("name") if b else None,
            "conservation_area": bool(c),
            "conservation_area_name": (c[0].get("NAME") if c else None),
            "flood_zone": max(levels) if levels else None,
            "flood_risk_type": sorted({p.get("flood-risk-type") for p in f if p.get("flood-risk-type")}),
        }
