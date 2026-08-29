#!/usr/bin/env python3
"""
Confident Planner - approval-probability model (Model/main.py).

Pipeline:
  1. Load the enriched applications dataset (Data/processed/data.csv)
  2. Recency-weighted 80/20 train/test split          <-- see WEIGHT_FOR_YEAR
  3. Train a classifier that outputs P(application approved)
  4. Evaluate (ROC-AUC, PR-AUC, Brier) and optionally save the model

WHY A WEIGHTED SPLIT (and not a plain random or pure time-based split)
----------------------------------------------------------------------
* The planning regime drifts over time (permitted-development rights, policy
  updates), so a 2026 application is a better proxy for "will a NEW application
  be approved?" than a 2016 one. We therefore weight BOTH:
    (a) the split - recent rows are more likely to land in train and test;
    (b) the fit  - the same weights are passed as `sample_weight`, so the
        model is penalised more for mispredicting recent years.
* 2026 gets the top weight: freshest data, closest to what the tool scores.

Trade-off, stated plainly: a weighted random split mixes years, so test
metrics are optimistic about true "next-year" performance. For an honest
future-performance estimate, swap in a pure time split (train <= 2024,
test >= 2025) - only `weighted_split()` needs to change.

Usage:
  python Model/main.py                                  # structured features only
  python Model/main.py --embeddings Data/processed/description_vectors.csv
  python Model/main.py --no-weights                     # ablation: unweighted split + fit
  python Model/main.py --save-model                     # pickle to Data/processed/approval_model.pkl
"""
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.decomposition import TruncatedSVD
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss,
                             log_loss, roc_auc_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO_ROOT / "Data" / "processed" / "data.csv"
DEFAULT_EMBEDDINGS = REPO_ROOT / "Data" / "processed" / "description_vectors.csv"
DEFAULT_MODEL_OUT = REPO_ROOT / "Data" / "processed" / "approval_model.pkl"

# --------------------------------------------------------------------------
# 1. Recency weights
# --------------------------------------------------------------------------
# Base weight 1.0 for everything up to and including 2021, then a ramp.
# Chosen to be simple, monotone and easy to tweak - the ordering matters far
# more than the exact numbers. 2026 is boosted hardest (x10) per the brief:
# most recent AND most policy-relevant year.
WEIGHT_FOR_YEAR = {
    2022: 2.0,
    2023: 3.0,
    2024: 4.0,
    2025: 6.0,
    2026: 10.0,
}
DEFAULT_YEAR_WEIGHT = 1.0
TEST_FRACTION = 0.20          # requested 80/20 split


def extract_years(df: pd.DataFrame) -> pd.Series:
    """Application year: 'valid_year' if present (CSV source), else parse the valid date."""
    if "valid_year" in df.columns:
        return pd.to_numeric(df["valid_year"], errors="coerce").astype("Int64")
    date_col = "Valid date" if "Valid date" in df.columns else "valid_date"
    if date_col not in df.columns:
        raise KeyError("no valid_year / 'Valid date' column found to weight and split on")
    return pd.to_datetime(df[date_col], errors="coerce", format="mixed").dt.year


def recency_weights(years: pd.Series) -> np.ndarray:
    """Map each row's year to its importance weight (see WEIGHT_FOR_YEAR)."""
    def w(y):
        return WEIGHT_FOR_YEAR.get(int(y), DEFAULT_YEAR_WEIGHT) if pd.notna(y) else DEFAULT_YEAR_WEIGHT
    return years.map(w).to_numpy(dtype=float)


def weighted_split(n: int, weights: np.ndarray, test_fraction: float, seed: int):
    """
    Weighted 80/20 split where BOTH sets skew recent.

    Each row is assigned to the test set independently with
    p_i = test_fraction * n * w_i / sum(w), i.e. probabilities proportional to
    the recency weight, normalised so the expected test size is test_fraction*n
    (actual size varies slightly - ~+-sqrt(n*p*(1-p)) - which is harmless).

    Why not priority sampling (u ** (1/w), take the top 80%)? Because that puts
    high-weight rows in TRAIN and leaves the test set skewed OLD - the opposite
    of what we want to evaluate. And why not np.random.choice(p=...)? It is
    O(n*k) and far too slow for ~1M rows.
    """
    rng = np.random.default_rng(seed)
    p_test = np.clip(test_fraction * n * weights / weights.sum(), 0.0, 1.0)
    is_test = rng.random(n) < p_test
    return np.flatnonzero(~is_test), np.flatnonzero(is_test)


# --------------------------------------------------------------------------
# 2. Features
# --------------------------------------------------------------------------
# Leakage guard: anything containing "decision" or "status" is excluded - the
# target `Approved?` is derived from `Decision`, and `Status` often states the
# outcome outright. `valid_year` is also excluded: it drives the sampling
# weights instead, so the model can't just "learn the calendar".
NUMERIC_FEATURES = [
    "site_area", "total_gia_gained", "total_gia_lost",            # CSV-source names
    "proposed_residential_units", "existing_residential_units",
    "proposed_affordable_units",
    "dh_site_area", "dh_total_gia_gained",                        # Datahub-source names (data.csv)
    "Population Density",
    "Distance to Park (m)",
]
CATEGORICAL_FEATURES = [
    "application_type", "development_type",                       # CSV-source names
    "decision_process", "decision_agency",
    "Application type",                                           # Datahub-source names (data.csv)
    "dh_application_type_full", "dh_decision_process",
    "borough", "Borough",                                         # one of the two, depending on data source
    "ward", "ward_name", "dh_ward",
    "Month",                           # seasonality: committees have busy/slow months
    "Day of the Week",
    "Conservation Area?",
    "Flood risk?",
]
EMBED_SVD_COMPONENTS = 32   # description embeddings compressed to this many dims


def expand_embeddings(embedding_csv: Path, descriptions: pd.Series) -> np.ndarray:
    """
    Join the per-description vectors produced by Model/embed_descriptions.py
    back onto the dataset. Descriptions absent from the lookup (e.g. empty)
    get a zero vector - "no description" is itself a weak signal, and zero is
    the neutral point of the embedding space.
    """
    emb = pd.read_csv(embedding_csv)
    text_col = next((c for c in ("description", "Description") if c in emb.columns), None)
    if text_col is None or "vector" not in emb.columns:
        raise ValueError(f"{embedding_csv} must contain a description column and a 'vector' column")
    lookup = {str(t).strip(): v for t, v in zip(emb[text_col], emb["vector"])}
    dim = len(json.loads(emb["vector"].iloc[0]))
    blank = np.zeros(dim, dtype=float)
    return np.vstack([
        np.array(json.loads(lookup[str(d).strip()]), dtype=float)
        if str(d).strip() in lookup else blank
        for d in descriptions.fillna("")
    ])


def build_preprocessor(numeric: list[str], categorical: list[str]) -> ColumnTransformer:
    """
    LogisticRegression needs scaled numerics and one-hot categoricals.
    sparse_output=True keeps the one-hot matrix sparse (~1M rows would not fit
    densely). StandardScaler(with_mean=False) preserves sparsity; scaling is
    not strictly required for LR but hugely speeds up lbfgs convergence.
    """
    return ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler(with_mean=False)),
        ]), numeric),
        ("cat", Pipeline([
            ("impute", SimpleImputer(strategy="constant", fill_value="<missing>")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=True)),
        ]), categorical),
    ])


def evaluate(y_true: np.ndarray, proba: np.ndarray, label: str) -> None:
    pred = (proba >= 0.5).astype(int)
    print(f"\n--- {label} ---")
    print(f"  rows:              {len(y_true)}")
    print(f"  base approval rate: {y_true.mean():.3f}")
    print(f"  ROC-AUC:           {roc_auc_score(y_true, proba):.4f}")
    print(f"  PR-AUC:            {average_precision_score(y_true, proba):.4f}")
    print(f"  Brier score:       {brier_score_loss(y_true, proba):.4f}  (lower = better calibrated)")
    print(f"  log loss:          {log_loss(y_true, proba):.4f}")
    print(f"  accuracy @0.5:     {(pred == y_true).mean():.4f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="enriched applications CSV")
    ap.add_argument("--embeddings", type=Path, default=None, help="description_vectors.csv from embed_descriptions.py")
    ap.add_argument("--no-weights", action="store_true", help="ablation: plain random split + unweighted fit")
    ap.add_argument("--test-fraction", type=float, default=TEST_FRACTION)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save-model", action="store_true", help="pickle the fitted pipeline")
    args = ap.parse_args(argv)

    if not args.data.exists():
        raise SystemExit(f"{args.data} not found - run Processing/processing.py first")

    df = pd.read_csv(args.data)
    print(f"loaded {args.data.name}: {len(df):,} rows x {df.shape[1]} columns")

    # --- target + weights -------------------------------------------------
    approved = df["Approved?"]
    keep = approved.isin([True, False])
    df = df[keep].reset_index(drop=True)
    y = approved[keep].astype(int).to_numpy()
    years = extract_years(df)
    weights = np.full(len(df), 1.0) if args.no_weights else recency_weights(years)
    if years.isna().any():
        print(f"warning: {int(years.isna().sum())} rows with no valid year -> weight 1.0")

    # --- recency-weighted 80/20 split -------------------------------------
    train_idx, test_idx = weighted_split(len(df), weights, args.test_fraction, args.seed)
    print(f"split: {len(train_idx):,} train / {len(test_idx):,} test "
          f"({'unweighted' if args.no_weights else 'recency-weighted'})")
    yt = years.iloc[test_idx].value_counts(normalize=True).sort_index()
    print("test-set year mix:", {int(k): round(float(v), 3) for k, v in yt.items()})

    # --- features ----------------------------------------------------------
    numeric = [c for c in NUMERIC_FEATURES if c in df.columns]
    categorical = [c for c in CATEGORICAL_FEATURES if c in df.columns]
    missing = [c for c in NUMERIC_FEATURES + CATEGORICAL_FEATURES if c not in df.columns]
    if missing:
        print(f"note: feature columns not in data, skipped: {missing}")

    emb_components = None
    if args.embeddings:
        if not args.embeddings.exists():
            raise SystemExit(f"{args.embeddings} not found - run Model/embed_descriptions.py first")
        vecs = expand_embeddings(args.embeddings, df["description"] if "description" in df.columns
                                 else df.get("Description", pd.Series([""] * len(df))))
        # SVD is fit on TRAIN rows only: fitting on the full frame would leak
        # test-set text statistics into the features.
        svd = TruncatedSVD(n_components=EMBED_SVD_COMPONENTS, random_state=args.seed)
        svd.fit(vecs[train_idx])
        emb_components = svd.transform(vecs)
        print(f"descriptions: {vecs.shape[1]} dims -> {EMBED_SVD_COMPONENTS} SVD components "
              f"(explained variance {svd.explained_variance_ratio_.sum():.2%})")

    # --- model -------------------------------------------------------------
    # LogisticRegression chosen deliberately: it outputs well-calibrated
    # probabilities out of the box (we report Brier), trains fast on sparse
    # one-hot data, and copes with the SVD embedding components. A gradient-
    # boosted model (HistGradientBoosting / XGBoost) is the natural upgrade if
    # AUC plateaus - but it needs ordinal-encoded categoricals and gives
    # probabilities that usually require calibration on top.
    model = Pipeline([
        ("prep", build_preprocessor(numeric, categorical)),
        ("clf", LogisticRegression(max_iter=1000, C=1.0)),
    ])
    model.fit(df.iloc[train_idx], y[train_idx],
              clf__sample_weight=None if args.no_weights else weights[train_idx])

    # --- evaluate ----------------------------------------------------------
    evaluate(y[test_idx], model.predict_proba(df.iloc[test_idx])[:, 1], "test set")

    if args.save_model:
        out = DEFAULT_MODEL_OUT
        with open(out, "wb") as fh:
            pickle.dump({"pipeline": model, "features": numeric + categorical,
                         "year_weights": None if args.no_weights else WEIGHT_FOR_YEAR}, fh)
        print(f"\nsaved model -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
