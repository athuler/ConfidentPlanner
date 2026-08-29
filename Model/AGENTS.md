# Confident Planner — standalone predictor

Predicts the probability that a **London planning application** is approved.
This folder is self-contained: the trained model and text embedder ship alongside
the script. It runs on macOS (or Linux/Windows) — no GPU, no internet, no other
project files needed.

## Files

| File | What it is |
|---|---|
| `predict.py` | The only file you run. CLI entry point. |
| `approval_model.pkl` | Trained logistic regression: preprocessing pipeline, one-hot encoder, SVD, model weights. |
| `embedder.pkl` | Fitted TF-IDF embedder that turns the `--description` text into the vector the model expects. |

## Setup (once)

Requires Python 3.10+.

```bash
cd ready
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux (Windows: .venv\Scripts\activate)
pip install numpy pandas "scikit-learn>=1.9"
```

Use `scikit-learn>=1.9` — the model was pickled with scikit-learn 1.9, and older
majors may fail to unpickle. Nothing else is required (the GPU/torch parts of
training were baked down to plain numpy weights).

## Running a prediction

```bash
python predict.py --description "Two storey rear extension to terraced house" \
    --borough Camden --application-type Householder --month 6 \
    --lat 51.545 --lon -0.158 --site-area 42
# -> Approval probability: 50.2%

python predict.py --description "Change of use from public house to late night bar" --json
# -> {"approval_probability": 0.4035, "approval_probability_pct": 40.4}
```

## Input contract (all flags optional except none are required)

| Flag | Meaning | Example |
|---|---|---|
| `--description` | Free-text application description (drives the text signal) | `"Demolition and erection of 4 flats"` |
| `--borough` | London borough | `Camden` |
| `--ward` | Ward name | `Kentish Town` |
| `--application-type` | Short type | `Householder`, `Full Planning Permission` |
| `--application-type-full` | Full type string | `Full Planning Permission - Householder` |
| `--month` | 1-12 (defaults to current month) | `6` |
| `--day-of-week` | Defaults to today | `Monday` |
| `--lat` / `--lon` | Site coordinates (WGS84) | `51.545` / `-0.158` |
| `--site-area` | Site area, m² | `42` |
| `--gia-gained` | Gross internal area gained, m² | `65` |
| `--population-density` | Persons per km² | `11000` |
| `--population-density-oa` | Output-area density | `12000` |
| `--distance-to-park` | Metres to nearest park | `150` |
| `--conservation-area` | `true` / `false` | `true` |
| `--decision-process` | e.g. `Delegated`, `Committee` | `Delegated` |
| `--cil-liability` | Community Infrastructure Levy status | `Yes` |
| `--json` | Emit JSON instead of text | flag |

Missing inputs are fine: numerics fall back to the training-set median,
categoricals to a "missing" category. Values never seen during training (a
misspelled borough, a new ward) are ignored — they contribute no evidence and
never raise an error.

## Output contract

- Default: one line on stdout — `Approval probability: 50.2%`
- With `--json`: `{"approval_probability": 0.502, "approval_probability_pct": 50.2}`
- Exit code 0 on success; non-zero with a message on stderr-level failures
  (e.g. missing `approval_model.pkl`).

## Model facts (for context, not required to run)

- Trained on ~466k London Planning Datahub applications, **2022-2026 only**,
  with recency weighting (2026 weighted highest).
- Logistic regression (L2, C=0.1) over: location, month/weekday, application
  type, borough, ward, site metrics, population density, park distance,
  conservation-area flag, and 32 SVD components of a TF-IDF description embedding.
- Test-set performance: ROC-AUC ≈ 0.73, accuracy ≈ 75.3% (base approval rate
  ≈ 71.7%). Treat the output as a calibrated indication, not a verdict.
- If `embedder.pkl` is missing the script still works — the description is just
  ignored (zero vector) and a warning is printed.
