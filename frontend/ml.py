"""Vectorised wrapper around the friend's approval model (Model/approval_model.pkl + embedder.pkl).

Replicates the preprocessing in Model/predict.py for whole DataFrames so ~1M applications can be scored
once at startup; predict_one() scores a single point for the modal.
"""
from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("frontend.ml")
MODEL_DIR = Path(__file__).resolve().parent.parent / "Model"

NUMERIC_FEATURES = ["Lat", "Lon", "dh_site_area", "dh_total_gia_gained", "Population Density", "Population Density (OA)", "Distance to Park (m)"]
LOG1P_FEATURES = {"dh_site_area", "dh_total_gia_gained", "Population Density", "Population Density (OA)", "Distance to Park (m)"}
# model feature name -> column in the enriched parquet
SOURCE_COLUMNS = {
    "dh_site_area": "dh_application_details.site_area",
    "dh_total_gia_gained": "dh_application_details.total_gia_gained",
}
CHUNK = 50_000


def _flag_str(v):
    """bool / 'true' / 'yes' / 1 -> 'True'; 'false' / 'no' / 0 -> 'False'; missing -> NaN (training vocab)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or (not isinstance(v, str) and pd.isna(v)):
        return np.nan
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "t", "y"):
            return "True"
        if s in ("false", "no", "0", "f", "n"):
            return "False"
        return np.nan
    return "True" if bool(v) else "False"


class ApprovalModel:
    def __init__(self, model_dir: Path = MODEL_DIR):
        t0 = time.perf_counter()
        with open(model_dir / "approval_model.pkl", "rb") as fh:
            self.bundle = pickle.load(fh)
        with open(model_dir / "embedder.pkl", "rb") as fh:
            self.embedder = pickle.load(fh)
        import sklearn
        self.features: list[str] = self.bundle["features"]
        self.numeric = [c for c in self.features if c.startswith("emb_") or c in NUMERIC_FEATURES]
        self.categorical = [c for c in self.features if c not in self.numeric]
        self.svd = self.bundle.get("svd")
        self.kind = self.bundle["kind"]
        log.info("approval model loaded (%s, %d features, svd %s comps, sklearn %s) in %.1fs",
                 self.kind, len(self.features), getattr(self.svd, "n_components", None), sklearn.__version__, time.perf_counter() - t0)

    # -- preprocessing --------------------------------------------------------
    def embed(self, texts: list) -> np.ndarray:
        from sklearn.feature_extraction.text import HashingVectorizer
        from sklearn.preprocessing import normalize

        dim = self.svd.n_features_in_
        texts = ["" if not isinstance(t, str) else t for t in texts]
        out = np.zeros((len(texts), self.svd.n_components), dtype=float)
        nonempty = [i for i, t in enumerate(texts) if t]
        if not nonempty:
            return out
        hv = HashingVectorizer(**self.embedder["vectorizer_params"])
        for s in range(0, len(nonempty), CHUNK):
            idx = nonempty[s : s + CHUNK]
            hashed = hv.transform([texts[i] for i in idx])
            vec = normalize(self.embedder["tfidf"].transform(hashed))
            out[idx] = self.svd.transform(vec)
        return out

    def build_frame(self, df: pd.DataFrame, descriptions=None) -> pd.DataFrame:
        X = pd.DataFrame(index=df.index)
        for c in self.features:
            if c.startswith("emb_"):
                continue
            src = SOURCE_COLUMNS.get(c, c)
            X[c] = df[c] if c in df.columns else (df[src] if src in df.columns else np.nan)
        for c in self.numeric:
            if c.startswith("emb_"):
                continue
            X[c] = pd.to_numeric(X[c], errors="coerce")
            if c in LOG1P_FEATURES:
                X[c] = np.log1p(X[c].clip(lower=0))
        for c in self.categorical:
            col = X[c]
            if c == "Month":
                col = pd.to_numeric(col, errors="coerce").map(lambda v: str(int(v)) if pd.notna(v) else np.nan)
            elif c == "Conservation Area?":
                col = col.map(_flag_str)
            elif c == "dh_cil_liability":
                col = col.map(lambda v: np.nan if pd.isna(v) else ("true" if _flag_str(v) == "True" else "false"))
            else:
                col = col.map(lambda v: str(v) if not pd.isna(v) else np.nan)
            X[c] = col.astype(object)
        if self.svd is not None:
            texts = descriptions if descriptions is not None else (df["Description"] if "Description" in df.columns else [None] * len(df))
            emb = self.embed(list(texts))
            for i in range(self.svd.n_components):
                X[f"emb_{i}"] = emb[:, i]
        return X[self.features]

    # -- scoring --------------------------------------------------------------
    def predict_frame(self, X: pd.DataFrame) -> np.ndarray:
        probs = np.empty(len(X), dtype=float)
        for s in range(0, len(X), CHUNK):
            part = X.iloc[s : s + CHUNK]
            if self.kind == "gpu-logistic-torch":
                M = self.bundle["prep"].transform(part)
                W = np.asarray(self.bundle["torch_state"]["W"], dtype=float)
                z = np.asarray(M @ W).ravel() + float(self.bundle["torch_state"]["b"])
                probs[s : s + len(part)] = 1.0 / (1.0 + np.exp(-z))
            else:
                probs[s : s + len(part)] = self.bundle["pipeline"].predict_proba(part)[:, 1]
        return probs

    def score(self, df: pd.DataFrame) -> np.ndarray:
        t0 = time.perf_counter()
        p = self.predict_frame(self.build_frame(df))
        log.info("scored %d applications in %.1fs (mean p=%.3f)", len(df), time.perf_counter() - t0, float(np.nanmean(p)) if len(p) else float("nan"))
        return p

    def predict_one(self, features: dict, description: str | None = None) -> float:
        df = pd.DataFrame([features])
        return float(self.predict_frame(self.build_frame(df, descriptions=[description]))[0])
