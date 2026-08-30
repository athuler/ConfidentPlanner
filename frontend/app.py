"""Flask app: London approval-likelihood map."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from .data import DataStore, release_memory
from .geo import GeoLookups
from .predictor import Predictor

log = logging.getLogger("frontend.app")
REPO_ROOT = Path(__file__).resolve().parent.parent


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    store = DataStore()
    store.refresh()
    geo = GeoLookups()  # pre-parsed bundle when available, else the raw GeoJSON files
    boroughs, mask = geo.boroughs_geojson, geo.mask
    release_memory()  # the GeoJSON parse leaves a lot of freed-but-retained memory behind
    predictor = None
    try:
        from .ml import ApprovalModel
        predictor = Predictor(ApprovalModel(), store, geo, boroughs)
        store.ml_status = {"state": "ready", "error": None}
    except Exception as exc:  # noqa: BLE001
        log.exception("ML model unavailable")
        store.ml_status = {"state": "error", "error": str(exc)}

    def ml_mode() -> bool:
        return request.args.get("model") == "ml" and predictor is not None

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
        if ml_mode():
            payload = {**predictor.boroughs(request.args), "dataset": store.info(), "model": "ml"}
        else:
            df = store.apply_filters(request.args)
            payload = {"overall": store.overall(df), "boroughs": store.borough_rates(df), "dataset": store.info(), "model": "historical"}
        log.info("/api/rates in %.0f ms", (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    @app.route("/api/heatmap/<borough>")
    def heatmap(borough: str):
        t0 = time.perf_counter()
        df = store.apply_filters(request.args)
        payload = store.grid(df, borough, float(request.args.get("cell_m", 500)))
        if ml_mode():
            payload = predictor.grid(payload, request.args)
        log.info("/api/heatmap/%s in %.0f ms", borough, (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    @app.route("/api/borough/<borough>")
    def borough(borough: str):
        t0 = time.perf_counter()
        df = store.apply_filters(request.args)
        payload = store.borough_stats(df, borough)
        if ml_mode():
            c = predictor.centroids.get(borough)
            if c is not None:
                sens = predictor.sensitivities(c.y, c.x, request.args)
                london = predictor.boroughs(request.args)["overall"]
                payload = {**payload, "model": "ml", "historical_stats": payload["stats"], "stats": sens["base"], "london": london,
                           "by_day": sens["by_day"], "by_month": sens["by_month"], "conservation": sens["conservation"],
                           "density": sens["density"], "app_types": sens["app_types"], "by_year": {}, "london_by_year": {},
                           "settings": sens["settings"], "flood": {}}
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
        payload["model"] = "historical"
        if ml_mode():
            sens = predictor.sensitivities(lat, lon, request.args)
            payload.update({"model": "ml", "model_prediction": sens["base"]["rate"], "historical": payload["estimate"],
                            "by_day": sens["by_day"], "by_month": sens["by_month"], "sensitivities": {
                                "conservation": sens["conservation"], "density": sens["density"], "app_types": sens["app_types"]},
                            "settings": sens["settings"]})
        log.info("/api/point %.5f,%.5f -> %s in %.0f ms", lat, lon, feats, (time.perf_counter() - t0) * 1000)
        return jsonify(payload)

    return app
