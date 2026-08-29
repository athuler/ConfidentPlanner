"""Point-in-polygon lookups for the web app (boroughs, conservation areas, flood zones)."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "Processing"))
from processing import PolygonIndex, find_conservation_geojson, load_geojson_features  # noqa: E402

log = logging.getLogger("frontend.geo")


class GeoLookups:
    def __init__(self, boroughs_geojson: dict):
        self.boroughs = PolygonIndex("boroughs", boroughs_geojson["features"])
        cons = find_conservation_geojson(REPO_ROOT / "Data")
        self.conservation = PolygonIndex("conservation areas", load_geojson_features(cons)) if cons else None
        flood = REPO_ROOT / "Data" / "reference" / "flood_risk_zones_london.geojson"
        self.flood = PolygonIndex("flood risk zones", load_geojson_features(flood)) if flood.exists() else None

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
