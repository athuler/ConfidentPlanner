"""End-to-end API tests through Flask's test client (no server needed; requires processed data + Model/)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if not list((ROOT / "Data" / "processed").glob("applications_enriched_*.parquet")):
    pytest.skip("processed data not present", allow_module_level=True)

from frontend.app import create_app  # noqa: E402

DESC = "Single storey rear extension and loft conversion with rear dormer"


@pytest.fixture(scope="module")
def client():
    app = create_app()
    app.testing = True
    return app.test_client()


def rates(client, **params):
    r = client.get("/api/rates", query_string=params)
    assert r.status_code == 200
    return r.get_json()


def camden(client, **params):
    q = {"model": "ml", "app_types": "Householder", "description": DESC, **params}
    return rates(client, **q)["boroughs"]["Camden"]["rate"]


def test_ml_rates_shape(client):
    d = rates(client, model="ml", description=DESC)
    assert d["model"] == "ml" and len(d["boroughs"]) == 33
    vals = [b["rate"] for b in d["boroughs"].values()]
    assert all(np.isfinite(v) and 0 <= v <= 1 for v in vals)
    assert d["dataset"]["ml"]["state"] == "ready"


def test_ml_reacts_to_inputs_that_matter(client):
    base = camden(client)
    assert abs(camden(client, app_types="Prior Approval") - base) > 0.1          # type: strong
    assert abs(rates(client, model="ml", app_types="Householder")["boroughs"]["Camden"]["rate"] - base) > 0.005  # description
    assert camden(client, conservation="yes") != camden(client, conservation="no")  # conservation override applied
    assert camden(client, months="1") != camden(client, months="7")                 # month applied


def test_ml_weak_and_unused_inputs(client):
    base = camden(client)
    assert abs(camden(client, density="low") - camden(client, density="high")) < 0.02  # documented insensitivity
    assert camden(client, flood="yes") == base and camden(client, year_min="2024") == base  # not model inputs


def test_ml_point_and_borough_payloads(client):
    p = client.get("/api/point", query_string=dict(lat=51.5074, lon=-0.1278, model="ml", app_types="Householder", description=DESC)).get_json()
    assert p["model"] == "ml" and 0 <= p["model_prediction"] <= 1
    assert len(p["by_day"]) == 7 and len(p["by_month"]) == 12 and set(p["sensitivities"]) == {"conservation", "density", "app_types"}
    assert p["settings"]["Application type"] == "Householder" and p["settings"]["description"] == DESC
    assert p["historical"]["n"] > 0
    b = client.get("/api/borough/Westminster", query_string=dict(model="ml", description=DESC)).get_json()
    assert b["model"] == "ml" and b["by_year"] == {} and len(b["by_day"]) == 7 and len(b["app_types"]) == 3
    assert set(b["density"]) == {"low", "medium", "high"} and 0 <= b["stats"]["rate"] <= 1


def test_historical_unaffected_by_ml_params(client):
    a = rates(client)
    b = rates(client, description=DESC)  # description only matters in ML mode
    assert a["overall"] == b["overall"] and a["boroughs"]["Camden"] == b["boroughs"]["Camden"]
    assert a["overall"]["n"] > 100000 and a["model"] == "historical"
