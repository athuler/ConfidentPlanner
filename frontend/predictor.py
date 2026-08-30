"""ML mode: score a *hypothetical new application* at map locations with the user's current settings.

Stored applications are never scored (their outcomes are known). Instead the model is asked: "an application
submitted here, of this type, in this month / on this weekday, with this description - how likely is approval?"
Location features (borough, conservation flag) come from the polygon layers; ward name, ward/OA density and
park distance come from the nearest stored application (they are attributes of the place, not the outcome).
"""
from __future__ import annotations

import datetime
import logging
import math
import time

import numpy as np
import pandas as pd

log = logging.getLogger("frontend.predictor")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DENSITY_TYPICAL = {"low": 3000.0, "medium": 7500.0, "high": 15000.0}  # persons/km2 used for the density sensitivity
APP_TYPES = ["Householder", "Prior Approval", "All Other"]
# A hypothetical application gets the fields a real one of its type has (most common values in the data);
# leaving them blank puts the model out of distribution and drags predictions down by ~20 pp.
FULL_TYPE = {"Householder": "Householder planning permission",
             "Prior Approval": "Prior Approval: Larger Home Extension",
             "All Other": "Full planning permission"}
DEFAULT_TYPE = "Householder"
TYPE_DEFAULTS = {"dh_decision_process": "Delegated", "dh_cil_liability": "false"}
MAX_DESCRIPTION = 500


def _to_m(lon: float, lat: float) -> tuple[float, float]:
    return lon * 111_320 * math.cos(math.radians(51.5)), lat * 110_574


class Predictor:
    def __init__(self, model, store, geo, boroughs_geojson: dict):
        from shapely.geometry import shape

        self.model, self.store, self.geo = model, store, geo
        self.centroids = {f["properties"]["name"]: shape(f["geometry"]).representative_point() for f in boroughs_geojson["features"]}
        self._tree, self._tree_rows, self._tree_key = None, None, None

    # -- nearest stored application (place attributes) -------------------------
    def _ensure_tree(self) -> None:
        if self._tree is not None and self._tree_key == self.store.loaded_at:
            return
        from shapely import points
        from shapely.strtree import STRtree

        df = self.store.df
        ok = df["x_m"].notna() & df["y_m"].notna()
        cols = [c for c in ["ward_name", "density", "park_m", "Population Density (OA)"] if c in df.columns]
        rows = df.loc[ok, cols].reset_index(drop=True)
        if "Population Density (OA)" not in rows:
            rows["Population Density (OA)"] = np.nan
        t0 = time.perf_counter()
        self._tree = STRtree(points(df.loc[ok, "x_m"].to_numpy(), df.loc[ok, "y_m"].to_numpy()))
        self._tree_rows, self._tree_key = rows, self.store.loaded_at
        log.info("predictor: spatial index over %d stored applications built in %.1fs", len(rows), time.perf_counter() - t0)

    def place_features(self, lats: list[float], lons: list[float]) -> list[dict]:
        self._ensure_tree()
        from shapely import points

        pts = points([_to_m(lo, la)[0] for la, lo in zip(lats, lons)], [_to_m(lo, la)[1] for la, lo in zip(lats, lons)])
        idx = self._tree.nearest(pts)
        near = self._tree_rows.iloc[idx]
        boro = self.geo.boroughs.lookup(lats, lons)
        cons = self.geo.conservation.lookup(lats, lons) if self.geo.conservation else [[] for _ in lats]
        out = []
        for i in range(len(lats)):
            r = near.iloc[i]
            oa = pd.to_numeric(r["Population Density (OA)"], errors="coerce")
            out.append({
                "Borough": boro[i][0].get("name") if boro[i] else None,
                "Conservation Area?": bool(cons[i]),
                "conservation_area_name": cons[i][0].get("NAME") if cons[i] else None,
                "ward_name": r["ward_name"] if pd.notna(r["ward_name"]) else None,
                "Population Density": float(r["density"]) if pd.notna(r["density"]) else None,
                "Population Density (OA)": float(oa) if pd.notna(oa) else None,
                "Distance to Park (m)": float(r["park_m"]) if pd.notna(r["park_m"]) else None,
            })
        return out

    # -- user settings ------------------------------------------------------------
    @staticmethod
    def settings(args) -> dict:
        today = datetime.date.today()
        types = [t for t in (args.get("app_types") or "").split(",") if t]
        months = [m for m in (args.get("months") or "").split(",") if m.strip().isdigit()]
        days = [d for d in (args.get("days") or "").split(",") if d]
        desc = (args.get("description") or "").strip()[:MAX_DESCRIPTION]
        bands = [b for b in (args.get("density") or "").split(",") if b in DENSITY_TYPICAL]
        cons = {"yes": True, "no": False}.get(args.get("conservation"))
        return {
            "conservation_override": cons,
            "density_override": DENSITY_TYPICAL[bands[0]] if len(bands) == 1 else None,
            "density_band": bands[0] if len(bands) == 1 else None,
            "Application type": types[0] if len(types) == 1 else DEFAULT_TYPE,
            "type_defaulted": len(types) != 1,
            "Month": int(months[0]) if len(months) == 1 else today.month,
            "Day of the Week": days[0] if len(days) == 1 else today.strftime("%A"),
            "description": desc or None,
            "_month_from_filter": len(months) == 1, "_day_from_filter": len(days) == 1, "_type_from_filter": len(types) == 1,
        }

    def predict(self, lats, lons, args, overrides: list[dict] | None = None, places: list[dict] | None = None) -> np.ndarray:
        """Batch prediction; overrides[i] (optional) replaces individual features of row i."""
        st = self.settings(args)
        places = places or self.place_features(list(lats), list(lons))
        rows = []
        for i, (la, lo) in enumerate(zip(lats, lons)):
            row = {"Lat": la, "Lon": lo, **{k: v for k, v in places[i].items() if k != "conservation_area_name"},
                   "Application type": st["Application type"], "Month": st["Month"], "Day of the Week": st["Day of the Week"],
                   **TYPE_DEFAULTS}
            if st["conservation_override"] is not None:  # sidebar inside/outside overrides the polygon lookup
                row["Conservation Area?"] = st["conservation_override"]
            if st["density_override"] is not None:  # a single selected density band overrides the location's density
                row["Population Density"] = st["density_override"]
                row["Population Density (OA)"] = st["density_override"]
            if overrides and overrides[i]:
                row.update(overrides[i])
            row["dh_application_type_full"] = FULL_TYPE.get(row["Application type"])
            rows.append(row)
        X = self.model.build_frame(pd.DataFrame(rows), descriptions=[st["description"]] * len(rows))
        return self.model.predict_frame(X)

    @staticmethod
    def _r(p) -> dict:
        return {"rate": float(p), "n": None, "approved": None}

    # -- map views ----------------------------------------------------------------
    def boroughs(self, args) -> dict:
        t0 = time.perf_counter()
        names = list(self.centroids)
        lats = [self.centroids[n].y for n in names]
        lons = [self.centroids[n].x for n in names]
        p = self.predict(lats, lons, args)
        out = {n: self._r(v) for n, v in zip(names, p)}
        log.info("predictor: %d borough predictions in %.0f ms (mean %.3f)", len(out), (time.perf_counter() - t0) * 1000, float(p.mean()))
        return {"overall": {"rate": float(p.mean()), "n": len(out), "approved": None}, "boroughs": out}

    def grid(self, hist_grid: dict, args) -> dict:
        feats = hist_grid["features"]
        if not feats:
            return hist_grid
        lats, lons = [], []
        for f in feats:
            ring = f["geometry"]["coordinates"][0]
            lons.append((ring[0][0] + ring[2][0]) / 2)
            lats.append((ring[0][1] + ring[2][1]) / 2)
        p = self.predict(lats, lons, args)
        for f, v in zip(feats, p):
            f["properties"]["rate"] = float(v)
            f["properties"]["approved"] = None
        return {**hist_grid, "stats": {"rate": float(p.mean()), "n": len(feats), "approved": None}}

    def sensitivities(self, lat: float, lon: float, args) -> dict:
        """Base prediction at a point plus one-feature-at-a-time variations (each row changes one thing)."""
        place = self.place_features([lat], [lon])[0]
        variants: list[tuple[str, str, dict]] = [("base", "base", {})]
        variants += [("by_day", d, {"Day of the Week": d}) for d in DAYS]
        variants += [("by_month", str(m), {"Month": m}) for m in range(1, 13)]
        variants += [("conservation", "inside", {"Conservation Area?": True}), ("conservation", "outside", {"Conservation Area?": False})]
        variants += [("density", b, {"Population Density": v}) for b, v in DENSITY_TYPICAL.items()]
        variants += [("app_types", t, {"Application type": t}) for t in APP_TYPES]
        n = len(variants)
        p = self.predict([lat] * n, [lon] * n, args, overrides=[v[2] for v in variants], places=[place] * n)
        out: dict = {"base": self._r(p[0]), "by_day": {}, "by_month": {}, "conservation": {}, "density": {}, "app_types": []}
        for (group, key, _), v in zip(variants[1:], p[1:]):
            if group == "app_types":
                out["app_types"].append({"value": key, "rate": float(v), "n": None})
            elif group == "by_month":
                out["by_month"][int(key)] = self._r(v)
            else:
                out[group][key] = self._r(v)
        out["place"] = place
        out["settings"] = {k: v for k, v in self.settings(args).items() if not k.startswith("_")}
        return out
