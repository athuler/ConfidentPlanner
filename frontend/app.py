"""Flask app: London approval-likelihood map."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .data import DataStore
from .geo import GeoLookups

log = logging.getLogger("frontend.app")
REPO_ROOT = Path(__file__).resolve().parent.parent
BOROUGHS_GEOJSON = REPO_ROOT / "Data" / "reference" / "london_boroughs.geojson"
BOROUGHS_URL = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
                "Local_Authority_Districts_December_2023_Boundaries_UK_BGC/FeatureServer/0/query"
                "?where=LAD23CD%20LIKE%20%27E09%25%27&outFields=LAD23CD,LAD23NM&outSR=4326&f=geojson")


def load_boroughs() -> dict:
    if not BOROUGHS_GEOJSON.exists():
        import requests
        log.info("downloading borough boundaries")
        BOROUGHS_GEOJSON.parent.mkdir(parents=True, exist_ok=True)
        BOROUGHS_GEOJSON.write_bytes(requests.get(BOROUGHS_URL, timeout=120).content)
    data = json.loads(BOROUGHS_GEOJSON.read_text())
    for f in data["features"]:
        f["properties"]["name"] = f["properties"].get("LAD23NM")
    log.info("borough boundaries: %d features", len(data["features"]))
    return data


def london_mask(boroughs: dict) -> dict:
    """World-ish rectangle minus the union of the boroughs: drawn white on top of the tiles to crop to London."""
    from shapely.geometry import box, mapping, shape
    from shapely.ops import unary_union

    union = unary_union([shape(f["geometry"]).buffer(0) for f in boroughs["features"]])
    union = union.buffer(0.002).buffer(-0.002).simplify(0.0002)  # close sliver gaps at borough seams (~200 m), keep the outline
    mask = box(-1.5, 50.8, 1.5, 52.2).difference(union)
    if mask.geom_type == "MultiPolygon":  # drop tiny leftover fragments (unclosed gaps inside London)
        parts = sorted(mask.geoms, key=lambda g: g.area, reverse=True)
        log.info("london mask: dropping %d small fragment(s), areas %s", len(parts) - 1, [round(g.area, 6) for g in parts[1:]])
        mask = parts[0]
    log.info("london mask: %s with %d part(s)", mask.geom_type, len(getattr(mask, "geoms", [mask])))
    return {"type": "Feature", "geometry": mapping(mask), "properties": {}}


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    store = DataStore()
    store.refresh()
    boroughs = load_boroughs()
    geo = GeoLookups(boroughs)
    mask = london_mask(boroughs)

    @app.before_request
    def _refresh():
        store.refresh()  # picks up newly finished years from the background pipeline

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/boroughs.geojson")
    def boroughs_geojson():
        return jsonify(boroughs)

    @app.route("/api/london_mask.geojson")
    def london_mask_geojson():
        return jsonify(mask)

    @app.route("/api/options")
    def options():
        return jsonify({"options": store.options(), "dataset": store.info()})

    @app.route("/api/rates")
    def rates():
        t0 = time.perf_counter()
        df = store.apply_filters(request.args)
        payload = {"overall": store.overall(df), "boroughs": store.borough_rates(df), "dataset": store.info()}
        log.info("/api/rates in %.0f ms", (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    @app.route("/api/heatmap/<borough>")
    def heatmap(borough: str):
        t0 = time.perf_counter()
        df = store.apply_filters(request.args)
        payload = store.grid(df, borough, float(request.args.get("cell_m", 500)))
        log.info("/api/heatmap/%s in %.0f ms", borough, (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    @app.route("/api/borough/<borough>")
    def borough(borough: str):
        t0 = time.perf_counter()
        df = store.apply_filters(request.args)
        payload = store.borough_stats(df, borough)
        log.info("/api/borough/%s in %.0f ms", borough, (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    @app.route("/api/point")
    def point():
        t0 = time.perf_counter()
        lat, lon = float(request.args["lat"]), float(request.args["lon"])
        feats = geo.at(lat, lon)
        df = store.apply_filters(request.args)
        if feats["borough"]:
            borough = store.borough_rates(df[df["Borough"] == feats["borough"]]).get(feats["borough"])
        else:
            borough = None
        payload = {"lat": lat, "lon": lon, "features": feats, "borough_rate": borough, **store.point_estimate(df, lat, lon, feats)}
        log.info("/api/point %.5f,%.5f -> %s in %.0f ms", lat, lon, feats, (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    return app
