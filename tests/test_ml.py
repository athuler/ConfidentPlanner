"""Sanity tests for the ML wrapper and the hypothetical-application predictor.

Run:  source venv/bin/activate && python -m pytest tests -q      (or: python tests/test_ml.py)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from frontend.ml import ApprovalModel  # noqa: E402

MODEL = ApprovalModel()


def cli(**flags) -> float:
    cmd = [sys.executable, str(ROOT / "Model" / "predict.py"), "--json"] + sum([[f"--{k.replace('_', '-')}", str(v)] for k, v in flags.items()], [])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    return json.loads(out)["approval_probability"]


def test_wrapper_matches_predict_py():
    cases = [
        dict(description="Two storey rear extension to terraced house", borough="Camden", application_type="Householder",
             month=6, day_of_week="Monday", lat=51.545, lon=-0.158, site_area=42),
        dict(description="Change of use from public house to late night bar", month=8, day_of_week="Saturday"),
        dict(description="Single storey rear extension and loft conversion", borough="Westminster", application_type="Householder",
             month=8, day_of_week="Saturday", lat=51.5074, lon=-0.1278, conservation_area="true", population_density=3288,
             distance_to_park=230, ward="St James's"),
    ]
    keymap = {"borough": "Borough", "application_type": "Application type", "month": "Month", "day_of_week": "Day of the Week",
              "lat": "Lat", "lon": "Lon", "site_area": "dh_site_area", "conservation_area": "Conservation Area?",
              "population_density": "Population Density", "distance_to_park": "Distance to Park (m)", "ward": "ward_name"}
    for c in cases:
        expected = cli(**c)
        feats = {keymap[k]: v for k, v in c.items() if k in keymap}
        got = MODEL.predict_one(feats, description=c.get("description"))
        assert abs(got - expected) < 0.002, (c, got, expected)


def test_wrapper_matches_predict_py_all_flags():
    """Every CLI flag of Model/predict.py round-trips through the wrapper identically."""
    cases = [
        dict(description="Erection of 4 flats with parking", borough="Hackney", ward="Brownswood Ward", application_type="All Other",
             application_type_full="Full planning permission", month=3, day_of_week="Wednesday", lat=51.5676, lon=-0.0964,
             site_area=850, gia_gained=420, population_density=12000, population_density_oa=15000, distance_to_park=120,
             conservation_area="false", decision_process="Committee", cil_liability="true"),
        dict(description="Loft conversion", borough="Bromley", application_type="Prior Approval",
             application_type_full="Prior Approval: Larger Home Extension", month=11, day_of_week="Friday",
             decision_process="Delegated", cil_liability="false", conservation_area="true"),
    ]
    keymap = {"borough": "Borough", "ward": "ward_name", "application_type": "Application type", "application_type_full": "dh_application_type_full",
              "month": "Month", "day_of_week": "Day of the Week", "lat": "Lat", "lon": "Lon", "site_area": "dh_site_area",
              "gia_gained": "dh_total_gia_gained", "population_density": "Population Density", "population_density_oa": "Population Density (OA)",
              "distance_to_park": "Distance to Park (m)", "conservation_area": "Conservation Area?", "decision_process": "dh_decision_process",
              "cil_liability": "dh_cil_liability"}
    for c in cases:
        expected = cli(**c)
        got = MODEL.predict_one({keymap[k]: v for k, v in c.items() if k in keymap}, description=c["description"])
        assert abs(got - expected) < 0.002, (c, got, expected)


def test_vocab_covers_app_values():
    import json
    from frontend.predictor import FULL_TYPE, TYPE_DEFAULTS
    oh = MODEL.bundle["prep"].named_transformers_["cat"].named_steps["onehot"]
    vocab = dict(zip(MODEL.categorical, (set(c) for c in oh.categories_)))
    boroughs = [f["properties"]["LAD23NM"] for f in json.load(open(ROOT / "Data" / "reference" / "london_boroughs.geojson"))["features"]]
    assert all(b in vocab["Borough"] for b in boroughs), [b for b in boroughs if b not in vocab["Borough"]]
    assert all(v in vocab["dh_application_type_full"] for v in FULL_TYPE.values())
    assert TYPE_DEFAULTS["dh_decision_process"] in vocab["dh_decision_process"]
    assert TYPE_DEFAULTS["dh_cil_liability"] in vocab["dh_cil_liability"]
    assert {"True", "False"} <= vocab["Conservation Area?"] and {str(m) for m in range(1, 13)} <= vocab["Month"]


def test_real_rows_roughly_calibrated():
    files = sorted((ROOT / "Data" / "processed").glob("applications_enriched_2025.parquet"))
    if not files:
        return  # data not present on this machine
    df = pd.read_parquet(files[0]).sample(2000, random_state=0)
    df = df[df["Approved?"].notna()]
    p = MODEL.predict_frame(MODEL.build_frame(df))
    assert np.isfinite(p).all() and (0 <= p).all() and (p <= 1).all()
    assert abs(p.mean() - df["Approved?"].astype(bool).mean()) < 0.05, (p.mean(), df["Approved?"].astype(bool).mean())


def _hypothetical(app_type: str, description: str | None, borough="Camden", lat=51.545, lon=-0.158, conservation=False) -> float:
    from frontend.predictor import FULL_TYPE, TYPE_DEFAULTS
    feats = {"Lat": lat, "Lon": lon, "Borough": borough, "Application type": app_type, "dh_application_type_full": FULL_TYPE[app_type],
             "Month": 6, "Day of the Week": "Tuesday", "Conservation Area?": conservation, "Population Density": 9000.0,
             "Distance to Park (m)": 200.0, **TYPE_DEFAULTS}
    return MODEL.predict_one(feats, description=description)


def test_hypothetical_householder_is_plausible():
    p = _hypothetical("Householder", "Single storey rear extension and loft conversion with rear dormer")
    assert 0.6 <= p <= 0.95, p  # historical Householder approval in Camden is ~0.8


def test_prior_approval_below_householder_and_description_matters():
    desc = "Single storey rear extension projecting 6 m from the rear wall"
    assert _hypothetical("Prior Approval", desc) < _hypothetical("Householder", desc)
    assert abs(_hypothetical("Householder", desc) - _hypothetical("Householder", None)) > 0.005


def test_flag_encoding():
    from frontend.ml import _flag_str
    assert _flag_str(True) == "True" and _flag_str("false") == "False" and _flag_str("True") == "True"
    assert _flag_str(None) is np.nan or pd.isna(_flag_str(None))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
