"""In-memory dataset for the web app: loads the enriched per-year parquet files and computes approval rates."""
from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("frontend.data")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = REPO_ROOT / "Data" / "processed"

COLUMNS = ["Borough", "Approved?", "Lat", "Lon", "Month", "Day of the Week", "Flood risk?", "flood_zone",
           "Conservation Area?", "conservation_area_name", "Application type", "Valid date", "Population Density",
           "ward_name", "Distance to Park (m)"]
POINT_MIN_N = 30
POINT_RADII_M = [250, 500, 1000, 2000]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MIN_N_BOROUGH = 5
MIN_N_CELL = 3


class DataStore:
    def __init__(self, processed_dir: Path = PROCESSED_DIR):
        self.dir = processed_dir
        self.df: pd.DataFrame = pd.DataFrame()
        self.files: dict[str, float] = {}
        self.loaded_at: float = 0
        self.years: list[int] = []
        self.rows_total = 0
        self.rows_decided = 0
        self._lock = threading.Lock()

    def _candidate_files(self) -> dict[str, float]:
        files = {p.name: p.stat().st_mtime for p in self.dir.glob("applications_enriched_*.parquet")}
        if not files and (self.dir / "applications_enriched.parquet").exists():
            p = self.dir / "applications_enriched.parquet"
            files[p.name] = p.stat().st_mtime
        return files

    def refresh(self) -> None:
        """Reload when the set of per-year files (or their mtimes) changes - cheap stat() otherwise."""
        files = self._candidate_files()
        if files == self.files:
            return
        with self._lock:
            if files == self.files:  # another thread just reloaded
                return
            self._load(files)

    def _load(self, files: dict[str, float]) -> None:
        t0 = time.perf_counter()
        frames = []
        for name in sorted(files):
            path = self.dir / name
            try:
                f = pd.read_parquet(path)
                frames.append(f[[c for c in COLUMNS if c in f.columns]])
                log.info("loaded %s: %d rows", name, len(f))
            except Exception as exc:  # noqa: BLE001 - file may be mid-write
                log.warning("could not load %s (%s) - skipping this time", name, exc)
                files.pop(name, None)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        self.rows_total = len(df)
        df["year"] = pd.to_datetime(df["Valid date"], errors="coerce").dt.year.astype("Int64")
        df["approved"] = df["Approved?"].astype("boolean")
        df = df[df["approved"].notna()].copy()
        df["approved"] = df["approved"].astype(bool)
        df["month"] = pd.to_numeric(df["Month"], errors="coerce").astype("Int64")
        df["flood"] = df["Flood risk?"].astype("boolean")
        df["conservation"] = df["Conservation Area?"].astype("boolean")
        df["app_type"] = df["Application type"].astype("string")
        df["Lat"] = pd.to_numeric(df["Lat"], errors="coerce")
        df["Lon"] = pd.to_numeric(df["Lon"], errors="coerce")
        df["density"] = pd.to_numeric(df["Population Density"], errors="coerce")
        df["park_m"] = pd.to_numeric(df["Distance to Park (m)"], errors="coerce")
        # local metric coordinates for nearest-neighbour searches
        df["x_m"] = df["Lon"] * 111_320 * math.cos(math.radians(51.5))
        df["y_m"] = df["Lat"] * 110_574
        self.df, self.files, self.loaded_at = df, files, time.time()
        self.rows_decided = len(df)
        self.years = sorted(int(y) for y in df["year"].dropna().unique())
        log.info("dataset ready: %d rows (%d decided) from %d file(s), years %s, in %.1fs",
                 self.rows_total, self.rows_decided, len(files), self.years, time.perf_counter() - t0)

    def info(self) -> dict:
        return {"files": sorted(self.files), "rows_total": int(self.rows_total), "rows_decided": int(self.rows_decided),
                "years": self.years, "loaded_at": self.loaded_at}

    def apply_filters(self, args) -> pd.DataFrame:
        df = self.df
        mask = pd.Series(True, index=df.index)
        tri = {"yes": True, "no": False}
        if args.get("flood") in tri:
            mask &= (df["flood"] == tri[args["flood"]]).fillna(False)
        if args.get("conservation") in tri:
            mask &= (df["conservation"] == tri[args["conservation"]]).fillna(False)
        if args.get("months"):
            months = {int(m) for m in args["months"].split(",") if m.strip().isdigit()}
            if months:
                mask &= df["month"].isin(months).fillna(False)
        if args.get("days"):
            days = {d.strip() for d in args["days"].split(",") if d.strip()}
            if days:
                mask &= df["Day of the Week"].isin(days).fillna(False)
        if args.get("app_types"):
            types = {t.strip() for t in args["app_types"].split(",") if t.strip()}
            if types:
                mask &= df["app_type"].isin(types).fillna(False)
        if args.get("year_min"):
            mask &= (df["year"] >= int(args["year_min"])).fillna(False)
        if args.get("year_max"):
            mask &= (df["year"] <= int(args["year_max"])).fillna(False)
        out = df[mask]
        log.info("filters %s -> %d/%d rows", {k: v for k, v in args.items() if v}, len(out), len(df))
        return out

    @staticmethod
    def _rate(g: pd.DataFrame) -> dict:
        n = int(len(g))
        return {"n": n, "approved": int(g["approved"].sum()) if n else 0, "rate": (float(g["approved"].mean()) if n else None)}

    def borough_rates(self, df: pd.DataFrame) -> dict:
        out = {}
        for b, g in df.groupby("Borough"):
            r = self._rate(g)
            if r["n"] < MIN_N_BOROUGH:
                r["rate"] = None
            out[str(b)] = r
        return out

    def overall(self, df: pd.DataFrame) -> dict:
        return self._rate(df)

    def grid(self, df: pd.DataFrame, borough: str, cell_m: float = 300.0) -> dict:
        g = df[(df["Borough"] == borough) & df["Lat"].notna() & df["Lon"].notna()]
        if g.empty:
            return {"type": "FeatureCollection", "features": [], "stats": self._rate(g), "cell_m": cell_m}
        dlat = cell_m / 110_574
        dlon = cell_m / (111_320 * math.cos(math.radians(51.5)))
        iy = np.floor(g["Lat"].to_numpy() / dlat).astype(int)
        ix = np.floor(g["Lon"].to_numpy() / dlon).astype(int)
        cells = pd.DataFrame({"ix": ix, "iy": iy, "approved": g["approved"].to_numpy()})
        agg = cells.groupby(["ix", "iy"])["approved"].agg(["count", "sum", "mean"]).reset_index()
        feats = []
        for row in agg.itertuples(index=False):
            if row.count < MIN_N_CELL:
                continue
            x0, y0 = row.ix * dlon, row.iy * dlat
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + dlon, y0], [x0 + dlon, y0 + dlat], [x0, y0 + dlat], [x0, y0]]]},
                "properties": {"n": int(row.count), "approved": int(row.sum), "rate": float(row.mean)},
            })
        log.info("grid %s: %d rows -> %d cells (>=%d apps each)", borough, len(g), len(feats), MIN_N_CELL)
        return {"type": "FeatureCollection", "features": feats, "stats": self._rate(g), "cell_m": cell_m}

    def options(self) -> dict:
        df = self.df
        types = df["app_type"].value_counts().head(12)
        return {
            "months": list(range(1, 13)),
            "days": DAYS,
            "app_types": [{"value": str(k), "n": int(v)} for k, v in types.items()],
            "year_min": self.years[0] if self.years else None,
            "year_max": self.years[-1] if self.years else None,
        }

    def point_estimate(self, df: pd.DataFrame, lat: float, lon: float, geo_features: dict) -> dict:
        """Likelihood at a point = approval rate of the nearest decided applications (filters already applied).

        Radius grows (250 m -> 2 km) until at least POINT_MIN_N applications are found. Also reports the rate
        among neighbours sharing the point's own conservation/flood status, and the point's features.
        """
        x = lon * 111_320 * math.cos(math.radians(51.5))
        y = lat * 110_574
        has = df["x_m"].notna() & df["y_m"].notna()
        d = np.sqrt((df.loc[has, "x_m"].to_numpy() - x) ** 2 + (df.loc[has, "y_m"].to_numpy() - y) ** 2)
        sub = df.loc[has]
        radius, near = None, None
        for r in POINT_RADII_M:
            m = d <= r
            if m.sum() >= POINT_MIN_N:
                radius, near = r, sub[m]
                break
        if near is None:
            idx = np.argsort(d)[:POINT_MIN_N]
            near, radius = sub.iloc[idx], float(d[idx].max()) if len(idx) else None
        est = self._rate(near)
        est["radius_m"] = radius
        # neighbours with the same conservation / flood status as the point
        same = near
        if geo_features.get("conservation_area") is not None:
            same = same[same["conservation"].fillna(False) == bool(geo_features["conservation_area"])]
        if geo_features.get("flood_zone") is not None:
            same = same[same["flood"].fillna(False) == bool(geo_features["flood_zone"])]
        similar = self._rate(same)
        # features observed at the nearest applications (density / ward / park are per-location attributes)
        order = np.argsort(d)[:10]
        nearest = sub.iloc[order]
        by_day = {k: self._rate(g) for k, g in near.groupby("Day of the Week")}
        by_month = {int(k): self._rate(g) for k, g in near.groupby("month") if pd.notna(k)}
        return {
            "estimate": est,
            "similar": similar,
            "by_day": by_day,
            "by_month": by_month,
            "nearby_features": {
                "population_density": (float(nearest["density"].median()) if nearest["density"].notna().any() else None),
                "ward": (str(nearest["ward_name"].dropna().mode().iloc[0]) if nearest["ward_name"].notna().any() else None),
                "distance_to_park_m": (float(nearest["park_m"].median()) if nearest["park_m"].notna().any() else None),
                "nearest_app_m": float(d[order[0]]) if len(order) else None,
            },
        }
