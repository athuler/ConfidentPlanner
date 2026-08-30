#!/usr/bin/env python3
"""
Confident Planner - description embeddings (Model/embed_descriptions.py).

Reads the enriched dataset, embeds every DISTINCT application description
into a fixed-size vector, and writes a two-column CSV:

    description,vector
    "Demolition of existing garage ...","[0.012, -0.044, ...]"

The model (Model/main.py) joins this file back onto the dataset, compresses
the vectors with SVD and feeds them as features - that is how descriptions
inform the approval prediction.

Embedder choice
---------------
Default: TF-IDF over hashed word 1-2 grams (scikit-learn only, offline, no
downloads, fixed 512 dims). It captures WHICH words appear ("demolition",
"change of use", "listed building") but not their meaning - "loft conversion"
and "attic conversion" look unrelated to it.

Upgrade path: install sentence-transformers and pass a model name, e.g.
    pip install sentence-transformers
    python Model/embed_descriptions.py --model sentence-transformers/all-MiniLM-L6-v2
That gives dense semantic vectors (~384 dims) where similar proposals land
close together - usually worth a few AUC points on text-heavy tabular data.

Usage:
  python Model/embed_descriptions.py                          # from the enriched CSV, TF-IDF
  python Model/embed_descriptions.py --model sentence-transformers/all-MiniLM-L6-v2
  python Model/embed_descriptions.py --data Data/processed/data.csv \
      --out Data/processed/description_vectors.csv --limit 1000   # quick dev run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO_ROOT / "Data" / "processed" / "data.csv"
DEFAULT_OUT = REPO_ROOT / "Data" / "processed" / "description_vectors.csv"


def embed_tfidf(texts: list[str]):
    """
    HashingVectorizer (never stores a vocabulary -> constant memory, and the
    SAME 512 dims regardless of corpus size) + TF-IDF weighting + L2 norm.
    alternate_sign=False keeps hashed counts additive so the output stays
    interpretable as a (sparse) tf-idf vector.
    """
    from sklearn.feature_extraction.text import HashingVectorizer, TfidfTransformer
    from sklearn.preprocessing import normalize

    hashed = HashingVectorizer(n_features=512, ngram_range=(1, 2),
                               alternate_sign=False, norm=None).transform(texts)
    return normalize(TfidfTransformer().fit_transform(hashed))


def embed_sentence_transformer(texts: list[str], model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise SystemExit("sentence-transformers is not installed: pip install sentence-transformers "
                         "(or drop --model to use the default TF-IDF embedder)")
    model = SentenceTransformer(model_name)
    return model.encode(texts, batch_size=64, show_progress_bar=True, normalize_embeddings=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="enriched applications CSV")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output CSV: description,vector")
    ap.add_argument("--model", default="tfidf",
                    help="'tfidf' (default, offline) or a sentence-transformers model name")
    ap.add_argument("--text-column", default=None,
                    help="description column (default: first of description / Description / dh_description)")
    ap.add_argument("--limit", type=int, default=None, help="dev: embed only the first N rows")
    args = ap.parse_args(argv)

    if not args.data.exists():
        raise SystemExit(f"{args.data} not found - run Processing/processing.py first")

    df = pd.read_csv(args.data, usecols=None)
    text_col = args.text_column or next(
        (c for c in ("description", "Description", "dh_description") if c in df.columns), None)
    if text_col is None:
        raise SystemExit(f"no description column in {args.data} (columns: {list(df.columns)})")

    texts = df[text_col].dropna().astype(str).str.strip()
    texts = texts[texts != ""]
    if args.limit:
        texts = texts.head(args.limit)
    unique = list(dict.fromkeys(texts))          # dedupe: identical descriptions share one vector
    print(f"{len(texts):,} descriptions -> {len(unique):,} unique")

    if args.model == "tfidf":
        vectors = embed_tfidf(unique)
    else:
        vectors = embed_sentence_transformer(unique, args.model)

    # TF-IDF vectors are sparse (csr) - densify before serialising; 512 dims
    # dense is trivial, and sentence-transformer output is dense already.
    dense = vectors.toarray() if hasattr(vectors, "toarray") else vectors
    out = pd.DataFrame({
        "description": unique,
        # JSON array string so the CSV stays a plain two-column file; main.py
        # parses it back with json.loads. Rounded to keep the file small.
        "vector": [json.dumps([round(float(v), 4) for v in row]) for row in dense],
    })
    out.to_csv(args.out, index=False)
    print(f"wrote {args.out} ({len(out):,} rows, dim {vectors.shape[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
