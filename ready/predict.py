#!/usr/bin/env python3
"""
Confident Planner - STANDALONE approval-probability predictor.

Scores ONE London planning application and prints the probability it is
approved. Everything the model needs ships in this folder:

    predict.py          this script (the only file you run)
    approval_model.pkl  trained logistic regression + preprocessing + SVD
    embedder.pkl        TF-IDF embedder for the description text

Setup (once):  pip install numpy pandas "scikit-learn>=1.9"
Run:           python predict.py --description "..." [--borough ...] ...

Only --description feeds the text signal; every other flag is optional.
Missing values fall back to the training-set median (numerics) or a "missing"
category (categoricals); values never seen in training are ignored rather
than treated as errors.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
MODEL = HERE / "approval_model.pkl"
EMBEDDER = HERE / "embedder.pkl"

# Dataset columns the model may use. Split into numeric / categorical the same
# way the training script did; "emb_*" are the description embedding components.
NUMERIC_FEATURES = [
    "Lat", "Lon",
    "site_area", "total_gia_gained", "total_gia_lost",
    "proposed_residential_units", "existing_residential_units",
    "proposed_affordable_units",
    "dh_site_area", "dh_total_gia_gained",
    "Population Density", "Population Density (OA)",
    "Distance to Park (m)",
]
LOG1P_FEATURES = {"dh_site_area", "dh_total_gia_gained", "site_area", "total_gia_gained",
                  "Population Density", "Population Density (OA)", "Distance to Park (m)"}

# CLI flag (dest) -> dataset column. All optional.
FEATURE_ARGS = {
    "lat": "Lat", "lon": "Lon",
    "borough": "Borough", "ward": "ward_name",
    "month": "Month", "day_of_week": "Day of the Week",
    "conservation_area": "Conservation Area?",
    "population_density": "Population Density",
    "population_density_oa": "Population Density (OA)",
    "distance_to_park": "Distance to Park (m)",
    "application_type": "Application type",
    "application_type_full": "dh_application_type_full",
    "site_area": "dh_site_area", "gia_gained": "dh_total_gia_gained",
    "decision_process": "dh_decision_process", "cil_liability": "dh_cil_liability",
}


def embed_description(text: str | None, dim: int) -> np.ndarray:
    """Embed one description with the fitted TF-IDF embedder; zero vector if unavailable."""
    if not text or not EMBEDDER.exists():
        if text and not EMBEDDER.exists():
            print("warning: embedder.pkl not found - description ignored")
        return np.zeros((1, dim), dtype=float)
    from sklearn.feature_extraction.text import HashingVectorizer
    from sklearn.preprocessing import normalize

    with open(EMBEDDER, "rb") as fh:
        emb = pickle.load(fh)
    hashed = HashingVectorizer(**emb["vectorizer_params"]).transform([text])
    return normalize(emb["tfidf"].transform(hashed)).toarray()


def main(argv=None) -> int:
    today = datetime.date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--description", default=None, help="planning application description (free text)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    for flag in FEATURE_ARGS:
        ap.add_argument(f"--{flag.replace('_', '-')}", default=None)
    args = ap.parse_args(argv)

    if not MODEL.exists():
        raise SystemExit(f"{MODEL.name} not found - it must sit next to predict.py")

    with open(MODEL, "rb") as fh:
        bundle = pickle.load(fh)
    features = bundle["features"]
    numeric = [c for c in features if c.startswith("emb_") or c in NUMERIC_FEATURES]
    categorical = [c for c in features if c not in numeric]

    # --- build the single-row frame, defaulting the calendar fields ----------
    supplied = {col: getattr(args, flag) for flag, col in FEATURE_ARGS.items() if getattr(args, flag) is not None}
    if args.month is None:
        supplied["Month"] = str(today.month)
    if args.day_of_week is None:
        supplied["Day of the Week"] = today.strftime("%A")
    if args.conservation_area is not None:
        ca = str(args.conservation_area).strip()
        supplied["Conservation Area?"] = ca.capitalize() if ca.lower() in ("true", "false") else ca
    df = pd.DataFrame([{c: supplied.get(c, np.nan) for c in features}])

    # --- identical preprocessing to training (numeric/log1p, categorical str) -
    for c in numeric:
        if c.startswith("emb_"):
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
        if c in LOG1P_FEATURES:
            df[c] = np.log1p(df[c].clip(lower=0))
    for c in categorical:
        df[c] = df[c].map(lambda v: str(v) if not pd.isna(v) else np.nan)

    # --- description -> SVD components ---------------------------------------
    svd = bundle.get("svd")
    if svd is not None:
        vec = embed_description(args.description, svd.n_features_in_)
        for i, v in enumerate(svd.transform(vec)[0]):
            df[f"emb_{i}"] = v

    # --- predict --------------------------------------------------------------
    if bundle["kind"] == "gpu-logistic-torch":
        X = bundle["prep"].transform(df)
        X = X.toarray() if hasattr(X, "toarray") else X
        W = np.asarray(bundle["torch_state"]["W"], dtype=float)
        z = float(X[0] @ W + bundle["torch_state"]["b"])
        proba = 1.0 / (1.0 + np.exp(-z))          # numpy sigmoid: no torch/GPU needed
    else:  # sklearn-logistic
        proba = float(bundle["pipeline"].predict_proba(df)[0, 1])

    pct = round(100 * proba, 1)
    if args.json:
        print(json.dumps({"approval_probability": round(proba, 4), "approval_probability_pct": pct}))
    else:
        print(f"Approval probability: {pct}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
