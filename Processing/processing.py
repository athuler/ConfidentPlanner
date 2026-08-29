#!/usr/bin/env python3
"""
Confident Planner - data processing pipeline.

Source (default): the London Planning Datahub (Elasticsearch, api-guest), bulk
downloaded per year for a configurable range (--years 2016-2026).  Alternative:
--source csv reads the export CSVs in Data/ and matches them to the Datahub.

Every application is then enriched with:

    Lat, Lon                     - Datahub site centroid (bbox-checked), else postcode centroid
    Borough                      - canonical London borough
    Month, Day of the Week       - from "Valid date"
    Conservation Area?           - point-in-polygon against the local conservation-area GeoJSON
    Flood risk?  / flood_zone    - point-in-polygon against planning.data.gov.uk flood-risk-zones
    Population Density           - ONS Census 2021 TS006 persons/km2 at Output Area (LSOA + borough too)
    Distance to Park (m)         - nearest OSM leisure=park (optional, --skip-parks)
    dh_*                         - every other Datahub field, flattened

All network results are cached (Data/datahub, Data/cache, Data/reference); a repeat
run makes no API calls unless --refresh-cache is given.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import logging
import math
import os
import re
import sys
import time
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

log = logging.getLogger("processing")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "Data"

POSTCODES_IO_URL = "https://api.postcodes.io/postcodes"
PLANNING_DATA_GEOJSON_URL = "https://www.planning.data.gov.uk/entity.geojson"
TS006_ZIP_URL = "https://www.nomisweb.co.uk/output/census/2021/census2021-ts006.zip"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

DATAHUB_URL = "https://planningdata.london.gov.uk/api-guest"
DATAHUB_KEY = os.environ.get("LONDON_DATAHUB_KEY", "be2rmRnt&")
DATAHUB_PAGE_SIZE = 10_000
DATAHUB_EXCLUDE_ALWAYS = ["polygon"]  # British National Grid copy of wgs84_polygon - never useful
DATAHUB_COLUMN_SETS: dict[str, dict] = {
    # everything except the big geometry / free-text-list fields  (~34 MB, ~7 s per 10k page)
    "default": {"excludes": ["polygon", "wgs84_polygon", "decision_conditions"]},
    # everything including site polygons and decision conditions   (~42 MB, ~12 s per page)
    "full": {"excludes": ["polygon"]},
    # ~30 modelling columns                                          (~11 MB, ~3.5 s per page)
    "slim": {
        "includes": [
            "id", "lpa_app_no", "lpa_name", "borough", "valid_date", "status", "decision", "decision_date",
            "decision_target_date", "application_type", "application_type_full", "development_type",
            "decision_process", "decision_agency", "description", "site_name", "site_number", "street_name",
            "locality", "postcode", "ward", "uprn", "centroid", "cil_liability", "is_pre_app",
            "appeal_decision", "appeal_decision_date", "appeal_status", "url_planning_app", "last_updated",
            "application_details.site_area", "application_details.total_gia_gained",
            "application_details.total_gia_lost", "application_details.total_gia_existing",
            "application_details.residential_details.total_no_proposed_residential_units",
            "application_details.residential_details.total_no_existing_residential_units",
        ]
    },
}

# Datahub field -> the column name the export CSVs use (so both sources look alike downstream)
DATAHUB_RENAMES = {
    "lpa_app_no": "LPA Number",
    "borough": "Borough",
    "valid_date": "Valid date",
    "status": "Status",
    "decision": "Decision",
    "decision_date": "Decision date",
    "application_type": "Application type",
    "description": "Description",
    "site_name": "Site name",
    "site_number": "Site number",
    "street_name": "Street name",
    "locality": "Locality",
    "postcode": "Postcode",
    "decision_target_date": "Decision target date",
    "url_planning_app": "URL planning application",
    "appeal_decision": "Appeal decision",
    "appeal_decision_date": "Appeal decision date",
    "application_details.residential_details.total_no_proposed_residential_units": "Total number of proposed residential units",
    "application_details.residential_details.total_no_existing_residential_units": "Total number of existing residential units",
}

# Greater London bounding box (south, west, north, east)
LONDON_BBOX = (51.28, -0.51, 51.70, 0.33)
LONDON_BBOX_WKT = "POLYGON((-0.51 51.28,0.33 51.28,0.33 51.70,-0.51 51.70,-0.51 51.28))"

USER_AGENT = "ConfidentPlanner/0.1 (https://github.com/athuler/ConfidentPlanner)"
UK_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$")

# Normalisation of the messy "Borough" column (used when a row has no usable postcode).
BOROUGH_ALIASES = {
    "hammersmith & fulham": "Hammersmith and Fulham",
    "kingston": "Kingston upon Thames",
    "kingston upon thames": "Kingston upon Thames",
    "royal borough of kingston (la code)": "Kingston upon Thames",
    "richmond": "Richmond upon Thames",
    "richmond upon thames": "Richmond upon Thames",
    "enfield council": "Enfield",
    "city of westminster": "Westminster",
    "city of london": "City of London",
    "barking & dagenham": "Barking and Dagenham",
    "kensington & chelsea": "Kensington and Chelsea",
    "royal borough of kensington and chelsea": "Kensington and Chelsea",
    "royal borough of greenwich": "Greenwich",
    "bromley custodian code": "Bromley",
    "nine elms": "Wandsworth",
    "stratford": "Newham",
    "canning town": "Newham",
    "seven kings": "Redbridge",
    "hampton": "Richmond upon Thames",
    "st margarets": "Richmond upon Thames",
    "stanmore": "Harrow",
    "colindale": "Barnet",
    "chiswick": "Hounslow",
    "crayford": "Bexley",
    "lldc": None,
    "opdc": None,
}

# Datahub lpa_name spellings (from a terms aggregation on lpa_name.raw)
DATAHUB_LPA_NAMES = {
    "hammersmith and fulham": "Hammersmith & Fulham",
    "kensington and chelsea": "Kensington & Chelsea",
    "barking and dagenham": "Barking & Dagenham",
    "kingston upon thames": "Kingston",
    "richmond upon thames": "Richmond",
}


# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #
def setup_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    root.handlers[:] = [console, fh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log.info("Logging to %s", log_file)


@contextmanager
def timed(name: str):
    log.info("=== %s: start", name)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log.info("=== %s: done in %.1fs", name, time.perf_counter() - t0)


def is_tty() -> bool:
    return sys.stderr.isatty()


# --------------------------------------------------------------------------- #
# Cache / HTTP helpers
# --------------------------------------------------------------------------- #
class JsonCache:
    """Write-through JSON key/value cache, one file per source. Negative results are cached too."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self.hits = 0
        self.misses = 0
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                log.debug("Loaded cache %s (%d entries)", self.path.name, len(self.data))
            except json.JSONDecodeError:
                log.warning("Cache %s is corrupt - starting fresh", self.path)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str):
        return self.data[key]["value"]

    def set(self, key: str, value) -> None:
        self.data[key] = {"value": value, "fetched_at": datetime.now(timezone.utc).isoformat()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def clear(self) -> None:
        self.data = {}


def make_session(extra_headers: dict | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def request_with_retry(session: requests.Session, method: str, url: str, retries: int = 5, timeout: int = 120, **kwargs):
    delay = 1.0
    for attempt in range(retries):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code < 400:
                return resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                log.error("%s %s -> %s: %s", method, url, resp.status_code, resp.text[:300])
                resp.raise_for_status()
            log.warning("%s %s -> %s (attempt %d/%d)", method, url, resp.status_code, attempt + 1, retries)
        except requests.RequestException as exc:
            log.warning("%s %s failed: %s (attempt %d/%d)", method, url, exc, attempt + 1, retries)
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError(f"Giving up on {method} {url} after {retries} attempts")


# --------------------------------------------------------------------------- #
# 1a. Source: London Planning Datahub (Elasticsearch)
# --------------------------------------------------------------------------- #
def datahub_session() -> requests.Session:
    return make_session({"X-API-AllowRequest": DATAHUB_KEY, "Content-Type": "application/json"})


def year_query(year: int) -> dict:
    return {"range": {"valid_date": {"gte": f"01/01/{year}", "lte": f"31/12/{year}", "format": "dd/MM/yyyy"}}}


def datahub_count(session: requests.Session, year: int) -> int:
    resp = request_with_retry(session, "POST", f"{DATAHUB_URL}/applications/_count", json={"query": year_query(year)})
    return int(resp.json()["count"])


def flatten_doc(src: dict) -> dict:
    """Flatten one Datahub document into a single row of scalars.

    Top-level scalars keep their name; nested objects are flattened with '.'
    (e.g. application_details.site_area); lists are summarised as n_<field>
    plus a joined text for small keyword lists.  centroid -> centroid_lat/centroid_lon.
    """
    row: dict = {}

    def walk(obj: dict, prefix: str):
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                if k == "centroid":
                    row[f"{key}_lat"] = v.get("lat")
                    row[f"{key}_lon"] = v.get("lon")
                elif k in ("wgs84_polygon", "polygon"):
                    row[key] = json.dumps(v)  # keep as GeoJSON text; area computed later if wanted
                else:
                    walk(v, key + ".")
            elif isinstance(v, list):
                row[f"n_{key}"] = len(v)
                if k == "constraints_details":
                    row["constraints_text"] = " | ".join(
                        f"{d.get('constraints_layer_description')}={d.get('result')}" for d in v if isinstance(d, dict)
                    ) or None
                elif v and all(isinstance(x, str) for x in v) and k != "decision_conditions":
                    row[key] = " | ".join(v)
            else:
                row[key] = v

    walk(src, "")
    return row


def verify_jsonl_gz(path: Path):
    """Return (n_complete_lines, last_line, intact) for a jsonl.gz, tolerating truncation/corruption."""
    n, last, intact = 0, None, True
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.endswith("\n"):
                    break
                n += 1
                last = line
    except (EOFError, zlib.error, OSError) as exc:
        log.warning("%s: unreadable after %d docs (%s)", path.name, n, exc)
        intact = False
    return n, last, intact


def rewrite_prefix(path: Path, n: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(path, "rt", encoding="utf-8") as src, gzip.open(tmp, "wt", encoding="utf-8") as dst:
        try:
            for i, line in enumerate(src):
                if i >= n or not line.endswith("\n"):
                    break
                dst.write(line)
        except (EOFError, zlib.error, OSError):
            pass
    tmp.replace(path)


def sort_key_for(doc: dict):
    vd, _id = doc.get("valid_date"), doc.get("id")
    if not vd or not _id:
        return None
    ms = int(datetime.strptime(vd, "%d/%m/%Y").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return [ms, _id]


def download_datahub(
    years,
    out_dir: Path,
    columns: str = "default",
    refresh: bool = False,
    session: requests.Session | None = None,
) -> list[Path]:
    """Bulk-download applications per valid_date year into <out_dir>/applications_<year>.<columns>.jsonl.gz.

    Resumable (meta sidecar keeps the last sort key) and idempotent (a complete year whose
    doc count still matches is skipped).  Returns the list of jsonl.gz paths.
    """
    session = session or datahub_session()
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = DATAHUB_COLUMN_SETS[columns] if isinstance(columns, str) else {"includes": list(columns)}
    tag = columns if isinstance(columns, str) else "custom"
    params: dict = {}
    if "includes" in spec:
        params["_source_includes"] = ",".join(spec["includes"])
    params["_source_excludes"] = ",".join(sorted(set(spec.get("excludes", [])) | set(DATAHUB_EXCLUDE_ALWAYS)))
    this_year = datetime.now().year
    paths: list[Path] = []

    for year in years:
        raw = out_dir / f"applications_{year}.{tag}.jsonl.gz"
        meta_path = out_dir / f"applications_{year}.{tag}.meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() and not refresh else {}
        paths.append(raw)

        expected = datahub_count(session, year)
        log.info("[datahub %d] %d applications on the server", year, expected)
        if meta.get("complete") and meta.get("expected") == expected:
            log.info("[datahub %d] already downloaded (%d docs, %s) - skipping", year, meta["downloaded"], meta["fetched_at"])
            continue
        if meta.get("complete") and year < this_year:
            log.info("[datahub %d] count changed %d -> %d; re-downloading", year, meta["expected"], expected)
            meta = {}
        if meta.get("complete"):
            log.info("[datahub %d] count changed %d -> %d (current year); re-downloading", year, meta["expected"], expected)
            meta = {}
        if expected == 0:
            log.warning("[datahub %d] nothing to download", year)
            gzip.open(raw, "wt").close()
            meta_path.write_text(json.dumps({"complete": True, "expected": 0, "downloaded": 0, "fetched_at": datetime.now(timezone.utc).isoformat()}))
            continue

        resume, downloaded = None, 0
        if raw.exists() and meta.get("downloaded") and not refresh:
            n_ok, last_line, intact = verify_jsonl_gz(raw)
            key = sort_key_for(json.loads(last_line)) if last_line else None
            if not intact or n_ok != meta.get("downloaded"):
                log.warning("[datahub %d] file truncated/corrupt at %d docs (meta said %d) - repairing", year, n_ok, meta.get("downloaded"))
                if key:
                    rewrite_prefix(raw, n_ok)
                else:
                    raw.unlink(missing_ok=True)
            if key:
                resume, downloaded = key, n_ok
                log.info("[datahub %d] resuming after %d verified docs", year, downloaded)
        mode = "at" if resume else "wt"
        pages_total = math.ceil(expected / DATAHUB_PAGE_SIZE)
        t0 = time.perf_counter()
        bytes_written = 0

        pit = request_with_retry(session, "POST", f"{DATAHUB_URL}/applications/_pit", params={"keep_alive": "5m"}).json()["id"]
        try:
            with gzip.open(raw, mode, encoding="utf-8") as fh:
                page = downloaded // DATAHUB_PAGE_SIZE
                search_after = resume
                while True:
                    body = {
                        "size": DATAHUB_PAGE_SIZE,
                        "sort": [{"valid_date": "asc"}, {"id": "asc"}],
                        "query": year_query(year),
                        "pit": {"id": pit, "keep_alive": "5m"},
                        "track_total_hits": False,
                    }
                    if search_after:
                        body["search_after"] = search_after
                    t_page = time.perf_counter()
                    resp = request_with_retry(session, "POST", f"{DATAHUB_URL}/_search", params=params, json=body, timeout=300)
                    data = resp.json()
                    pit = data.get("pit_id", pit)
                    hits = data["hits"]["hits"]
                    if not hits:
                        break
                    for h in hits:
                        line = json.dumps(h["_source"], separators=(",", ":"))
                        fh.write(line + "\n")
                        bytes_written += len(line) + 1
                    downloaded += len(hits)
                    page += 1
                    search_after = hits[-1]["sort"]
                    elapsed = time.perf_counter() - t0
                    rate = downloaded / elapsed if elapsed else 0
                    eta = (expected - downloaded) / rate if rate else float("nan")
                    log.info(
                        "[datahub %d] page %d/%d  %d/%d docs  %.1f MB raw  page %.1fs  elapsed %.0fs  ETA %.0fs",
                        year, page, pages_total, downloaded, expected, bytes_written / 1e6,
                        time.perf_counter() - t_page, elapsed, eta,
                    )
                    meta = {
                        "complete": False, "expected": expected, "downloaded": downloaded,
                        "last_sort": search_after, "columns": tag, "params": params,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                    meta_path.write_text(json.dumps(meta))
                    if len(hits) < DATAHUB_PAGE_SIZE:
                        break
        finally:
            try:
                session.delete(f"{DATAHUB_URL}/_pit", json={"id": pit}, timeout=30)
            except requests.RequestException:
                pass

        meta.update({"complete": True, "fetched_at": datetime.now(timezone.utc).isoformat()})
        meta_path.write_text(json.dumps(meta))
        log.info("[datahub %d] complete: %d docs in %.0fs -> %s (%.1f MB gz)", year, downloaded, time.perf_counter() - t0, raw.name, raw.stat().st_size / 1e6)
        if downloaded != expected:
            log.warning("[datahub %d] downloaded %d but server reported %d", year, downloaded, expected)
        cached = out_dir / f"applications_{year}.{tag}.parquet"
        if cached.exists():
            cached.unlink()
    return paths


def load_datahub(years, out_dir: Path, columns: str = "default", refresh: bool = False, session=None) -> pd.DataFrame:
    paths = download_datahub(years, out_dir, columns, refresh, session)
    frames = []
    for path in paths:
        cached = path.with_name(path.name.replace(".jsonl.gz", ".parquet"))
        if cached.exists() and cached.stat().st_mtime >= path.stat().st_mtime:
            df = pd.read_parquet(cached)
            log.info("Loaded %s (%d rows, %d cols) from parquet cache", cached.name, len(df), df.shape[1])
        else:
            t0 = time.perf_counter()
            rows = []
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        rows.append(flatten_doc(json.loads(line)))
            except (EOFError, zlib.error, OSError, json.JSONDecodeError) as exc:
                year = int(re.search(r"applications_(\d{4})", path.name).group(1))
                log.error("%s is corrupt (%s) - deleting and re-downloading %d", path.name, exc, year)
                for junk in (path, path.with_name(path.name.replace(".jsonl.gz", ".meta.json")), cached):
                    junk.unlink(missing_ok=True)
                download_datahub([year], out_dir, columns, True, session)
                rows = []
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        rows.append(flatten_doc(json.loads(line)))
            df = pd.DataFrame(rows)
            log.info("Flattened %s: %d rows, %d cols in %.1fs", path.name, len(df), df.shape[1], time.perf_counter() - t0)
            try:
                df.to_parquet(cached, index=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not write parquet cache %s: %s", cached.name, exc)
        frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    before = len(df)
    df = df.drop_duplicates(subset=["id"]).reset_index(drop=True)
    if len(df) != before:
        log.info("Dropped %d duplicate Datahub ids", before - len(df))

    # Rename core fields to the CSV export names; prefix everything else with dh_
    rename = {k: v for k, v in DATAHUB_RENAMES.items() if k in df.columns}
    rename.update({c: f"dh_{c}" for c in df.columns if c not in DATAHUB_RENAMES and c != "id"})
    rename["id"] = "dh_id"
    df = df.rename(columns=rename)
    empty = [c for c in df.columns if df[c].isna().all()]
    if empty:
        log.info("Dropping %d Datahub columns that are entirely empty: %s", len(empty), ", ".join(sorted(empty)))
        df = df.drop(columns=empty)
    df["source_file"] = "datahub"
    log.info("Datahub frame: %d rows x %d columns", len(df), df.shape[1])
    return df


# --------------------------------------------------------------------------- #
# 1b. Source: export CSVs in Data/
# --------------------------------------------------------------------------- #
DESCRIPTION_INDEX = 7


def read_csv_tolerant(path: Path) -> pd.DataFrame:
    """Read an export CSV, repairing rows with unescaped quotes or a truncated last record."""
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    consumed: list[str] = []

    def tracked():
        for line in lines:
            consumed.append(line)
            yield line

    reader = csv.reader(tracked())
    header = next(reader)
    n = len(header)
    tail = n - DESCRIPTION_INDEX - 1
    rows, repaired, padded = [], 0, 0
    consumed.clear()
    for row in reader:
        raw = "".join(consumed).rstrip("\r\n")
        consumed.clear()
        if len(row) == n:
            rows.append(row)
            continue
        parts = raw.split(",")
        if len(parts) >= n:
            desc = ",".join(parts[DESCRIPTION_INDEX : len(parts) - tail]).strip()
            if desc.startswith('"') and desc.endswith('"'):
                desc = desc[1:-1]
            row = parts[:DESCRIPTION_INDEX] + [desc.replace('""', '"')] + parts[-tail:]
            repaired += 1
            log.warning("%s line %d: repaired row with unescaped quotes (%s)", path.name, reader.line_num, row[0])
        else:
            padded += 1
            log.warning("%s line %d: short/truncated row padded (%s)", path.name, reader.line_num, row[0] if row else "?")
            row = (row + [""] * n)[:n]
        rows.append(row)
    log.info("%s: %d rows (%d repaired, %d padded)", path.name, len(rows), repaired, padded)
    return pd.DataFrame(rows, columns=header, dtype=str)


def load_csvs(data_dir: Path) -> pd.DataFrame:
    files = sorted(p for p in data_dir.glob("*.csv") if p.is_file())
    if not files:
        raise SystemExit(f"No CSV files found in {data_dir}")
    frames = []
    for path in files:
        df = read_csv_tolerant(path)
        df["source_file"] = path.name
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset=["LPA Number", "Borough"], keep="last").reset_index(drop=True)
    log.info("CSV frame: %d rows from %d files (%d duplicates dropped)", len(df), len(files), before - len(df))
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].str.strip()
    df = df.replace({"": pd.NA})
    df["Description"] = df["Description"].map(lambda s: html.unescape(s) if isinstance(s, str) else s)
    return df


def to_datahub_lpa_name(borough) -> str | None:
    b = normalise_borough(borough)
    return DATAHUB_LPA_NAMES.get(b.lower(), b) if b else None


def enrich_csv_from_datahub(df: pd.DataFrame, cache: JsonCache, session: requests.Session) -> pd.DataFrame:
    """Match CSV rows to Datahub docs by id ({lpa_name}-{ref with / -> _}) and add dh_* columns."""
    ids = {}
    for i, (ref, borough) in enumerate(zip(df["LPA Number"], df["Borough"])):
        lpa = to_datahub_lpa_name(borough)
        if isinstance(ref, str) and lpa:
            ids[i] = f"{lpa}-{ref.replace('/', '_')}"
    missing = sorted({v for v in ids.values() if v not in cache})
    cache.hits += len(set(ids.values())) - len(missing)
    cache.misses += len(missing)
    log.info("Datahub match: %d rows, %d unique ids, %d to fetch", len(df), len(set(ids.values())), len(missing))
    for i in tqdm(range(0, len(missing), 100), desc="datahub ids", disable=not (missing and is_tty())):
        batch = missing[i : i + 100]
        resp = request_with_retry(
            session, "POST", f"{DATAHUB_URL}/applications/_search",
            params={"_source_excludes": "polygon,wgs84_polygon,decision_conditions"},
            json={"size": len(batch), "query": {"ids": {"values": batch}}},
        )
        found = {h["_id"]: h["_source"] for h in resp.json()["hits"]["hits"]}
        for _id in batch:
            cache.set(_id, found.get(_id))
        cache.save()
    rows = []
    matched = 0
    for i in range(len(df)):
        src = cache.get(ids[i]) if i in ids and ids[i] in cache else None
        if src:
            matched += 1
            rows.append({("dh_" + k): v for k, v in flatten_doc(src).items()})
        else:
            rows.append({})
    log.info("Datahub match: %d/%d rows matched (%.0f%%)", matched, len(df), 100 * matched / max(len(df), 1))
    unmatched = [ids.get(i, "<no id>") for i in range(len(df)) if not rows[i]]
    log.debug("Unmatched ids (first 20): %s", unmatched[:20])
    extra = pd.DataFrame(rows, index=df.index)
    return pd.concat([df, extra], axis=1)


# --------------------------------------------------------------------------- #
# 2. Dates, postcodes, boroughs
# --------------------------------------------------------------------------- #
def clean_postcode(value) -> str | None:
    if not isinstance(value, str):
        return None
    pc = re.sub(r"\s+", "", value.upper())
    if len(pc) < 5:
        return None
    pc = f"{pc[:-3]} {pc[-3:]}"
    return pc if UK_POSTCODE_RE.match(pc) else None


def normalise_borough(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().rstrip(",").lower()
    key = re.sub(r"^(london borough of|royal borough of)\s+", "", key)
    key = re.sub(r"\s+(council|custodian code)$", "", key)
    key = re.sub(r"\s+", " ", key)
    if key in BOROUGH_ALIASES:
        return BOROUGH_ALIASES[key]
    return " ".join(w if w in ("and", "upon", "of") else w.capitalize() for w in key.split(" "))


def add_dates(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("Valid date", "Decision date", "Decision target date", "Appeal decision date"):
        if col in df:
            parsed = pd.to_datetime(df[col], dayfirst=True, format="%d/%m/%Y", errors="coerce")
            bad = df[col].notna() & parsed.isna()
            if bad.any():
                log.warning("%s: %d unparseable values (e.g. %s)", col, int(bad.sum()), df.loc[bad, col].iloc[0])
            df[col] = parsed
    valid = df["Valid date"]
    df["Month"] = valid.dt.month.astype("Int64")
    df["Day of the Week"] = valid.dt.day_name()
    df["day_of_week_num"] = valid.dt.dayofweek.astype("Int64")
    log.info("Dates: Valid date %s .. %s; Month/Day of the Week populated for %d rows", valid.min(), valid.max(), int(valid.notna().sum()))
    return df


def decision_to_bool(value):
    """Approved -> True, any other decision -> False, blank (no decision yet) -> <NA>."""
    if not isinstance(value, str) or not value.strip():
        return pd.NA
    return value.strip().lower().startswith("approv")


def add_decision_flag(df: pd.DataFrame) -> pd.DataFrame:
    df["Approved?"] = df["Decision"].map(decision_to_bool).astype("boolean")
    counts = df["Approved?"].value_counts(dropna=False).to_dict()
    other = df.loc[df["Approved?"] == False, "Decision"].value_counts().head(8).to_dict()  # noqa: E712
    log.info("Approved?: %s; most common non-approved decisions: %s", counts, other)
    return df


# --------------------------------------------------------------------------- #
# 3. Geocoding: Datahub centroid first, postcodes.io second
# --------------------------------------------------------------------------- #
def geocode(postcodes: list[str], cache: JsonCache, session: requests.Session) -> dict[str, dict | None]:
    wanted = sorted({pc for pc in postcodes if pc})
    missing = [pc for pc in wanted if pc not in cache or (cache.get(pc) and "ward_code" not in cache.get(pc))]
    cache.hits += len(wanted) - len(missing)
    cache.misses += len(missing)
    log.info("postcodes.io: %d unique postcodes, %d cached, %d to fetch (%d batches)", len(wanted), len(wanted) - len(missing), len(missing), math.ceil(len(missing) / 100))
    t0 = time.perf_counter()
    for n, i in enumerate(range(0, len(missing), 100), 1):
        batch = missing[i : i + 100]
        resp = request_with_retry(session, "POST", POSTCODES_IO_URL, json={"postcodes": batch})
        not_found = 0
        for item in resp.json()["result"]:
            res = item.get("result")
            if res is None:
                cache.set(item["query"], None)
                not_found += 1
                continue
            codes = res.get("codes") or {}
            cache.set(item["query"], {
                "lat": res.get("latitude"), "lon": res.get("longitude"),
                "admin_district": res.get("admin_district"), "admin_district_code": codes.get("admin_district"),
                "oa21": codes.get("oa21"), "lsoa21": codes.get("lsoa21"), "msoa21": codes.get("msoa21"),
                "ward": res.get("admin_ward"), "ward_code": codes.get("admin_ward"),
            })
        cache.save()
        if n % 10 == 0 or i + 100 >= len(missing):
            log.info("postcodes.io: batch %d/%d (%d not found in this batch) %.0fs", n, math.ceil(len(missing) / 100), not_found, time.perf_counter() - t0)
    return {pc: cache.get(pc) for pc in wanted}


def in_london(lat, lon) -> bool:
    s, w, n, e = LONDON_BBOX
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and s <= lat <= n and w <= lon <= e


POSTCODE_IN_TEXT_RE = re.compile(r"([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")
ADDRESS_FIELDS = ["Site name", "Site number", "Street name", "Locality", "dh_secondary_street_name", "Description"]


def postcode_from_text(*parts) -> str | None:
    """Find a UK postcode inside free-text address fields (handles 'CircusLondonNW4 3LA' and 'NW43LA')."""
    for part in parts:
        if not isinstance(part, str) or not part:
            continue
        for m in POSTCODE_IN_TEXT_RE.finditer(part.upper()):
            pc = clean_postcode(f"{m.group(1)} {m.group(2)}")
            if pc:
                return pc
    return None


def add_geocoding(df: pd.DataFrame, cache: JsonCache, session: requests.Session) -> pd.DataFrame:
    df["postcode_clean"] = df["Postcode"].map(clean_postcode)
    df["postcode_source"] = pd.Series(pd.NA, index=df.index, dtype="string")
    df.loc[df["postcode_clean"].notna(), "postcode_source"] = "postcode_column"
    n_pc = int(df["Postcode"].notna().sum())
    n_ok = int(df["postcode_clean"].notna().sum())
    bad = df.loc[df["Postcode"].notna() & df["postcode_clean"].isna(), "Postcode"]
    log.info("Postcodes: %d/%d rows have one, %d valid after cleaning, %d rejected (e.g. %s)", n_pc, len(df), n_ok, len(bad), list(bad.unique()[:8]))

    # Fallback: pull a postcode out of the address / description text
    fields = [c for c in ADDRESS_FIELDS if c in df.columns]
    need = df["postcode_clean"].isna()
    if need.any() and fields:
        found = df.loc[need, fields].apply(lambda r: postcode_from_text(*r.values), axis=1)
        hit = found.notna()
        df.loc[found.index[hit], "postcode_clean"] = found[hit]
        df.loc[found.index[hit], "postcode_source"] = "address_text"
        log.info("Postcodes from address text: %d/%d rows without a postcode recovered (e.g. %s)", int(hit.sum()), int(need.sum()), list(found[hit].head(5)))
        log.info("Postcodes now: %d/%d rows (%s)", int(df["postcode_clean"].notna().sum()), len(df), df["postcode_source"].value_counts(dropna=False).to_dict())
    results = geocode(df["postcode_clean"].dropna().tolist(), cache, session)

    def pick(pc, field):
        r = results.get(pc) if isinstance(pc, str) else None
        return r.get(field) if r else None

    pc_lat = df["postcode_clean"].map(lambda pc: pick(pc, "lat"))
    pc_lon = df["postcode_clean"].map(lambda pc: pick(pc, "lon"))
    df["borough_raw"] = df["Borough"]
    df["borough_code"] = df["postcode_clean"].map(lambda pc: pick(pc, "admin_district_code"))
    df["oa21"] = df["postcode_clean"].map(lambda pc: pick(pc, "oa21"))
    df["lsoa21"] = df["postcode_clean"].map(lambda pc: pick(pc, "lsoa21"))
    df["msoa21"] = df["postcode_clean"].map(lambda pc: pick(pc, "msoa21"))
    df["ward_code"] = df["postcode_clean"].map(lambda pc: pick(pc, "ward_code"))
    df["ward_name"] = df["postcode_clean"].map(lambda pc: pick(pc, "ward"))

    # Lat/Lon: Datahub site centroid when sane, else postcode centroid
    dh_lat = df["dh_centroid_lat"] if "dh_centroid_lat" in df else pd.Series([None] * len(df), index=df.index)
    dh_lon = df["dh_centroid_lon"] if "dh_centroid_lon" in df else pd.Series([None] * len(df), index=df.index)
    dh_ok = pd.Series([in_london(a, b) for a, b in zip(dh_lat, dh_lon)], index=df.index)
    n_dh_present = int(pd.Series([isinstance(a, (int, float)) and not pd.isna(a) for a in dh_lat], index=df.index).sum())
    log.info("Datahub centroids: %d present, %d inside London bbox (%d rejected as junk)", n_dh_present, int(dh_ok.sum()), n_dh_present - int(dh_ok.sum()))
    df["Lat"] = pd.Series(dh_lat.where(dh_ok, pc_lat), dtype="Float64")
    df["Lon"] = pd.Series(dh_lon.where(dh_ok, pc_lon), dtype="Float64")
    df["latlon_source"] = pd.Series(pd.NA, index=df.index, dtype="string")
    df.loc[dh_ok, "latlon_source"] = "datahub"
    df.loc[~dh_ok & pc_lat.notna(), "latlon_source"] = "postcode"
    log.info("Lat/Lon: %s", df["latlon_source"].value_counts(dropna=False).to_dict())

    from_postcode = df["postcode_clean"].map(lambda pc: pick(pc, "admin_district"))
    from_dh = df["dh_lpa_name"].map(normalise_borough) if "dh_lpa_name" in df else pd.Series([None] * len(df), index=df.index)
    from_raw = df["borough_raw"].map(normalise_borough)
    df["Borough"] = from_postcode.where(from_postcode.notna(), from_dh.where(from_dh.notna(), from_raw))
    df["borough_source"] = pd.Series(pd.NA, index=df.index, dtype="string")
    df.loc[from_raw.notna(), "borough_source"] = "raw_column"
    df.loc[from_dh.notna(), "borough_source"] = "datahub_lpa"
    df.loc[from_postcode.notna(), "borough_source"] = "postcode"
    log.info("Borough: %d distinct values; sources %s", df["Borough"].nunique(), df["borough_source"].value_counts(dropna=False).to_dict())
    return df


# --------------------------------------------------------------------------- #
# 4. Point-in-polygon lookups (conservation areas, flood zones)
# --------------------------------------------------------------------------- #
class PolygonIndex:
    """STRtree over GeoJSON polygons restricted to the London bbox."""

    def __init__(self, name: str, features: list[dict]):
        from shapely.geometry import box, shape
        from shapely.strtree import STRtree

        s, w, n, e = LONDON_BBOX
        london = box(w, s, e, n)
        self.name = name
        self.geoms, self.props = [], []
        skipped = 0
        t0 = time.perf_counter()
        for f in features:
            g = f.get("geometry")
            if not g:
                continue
            try:
                geom = shape(g)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if not geom.envelope.intersects(london):
                continue
            if not geom.is_valid:
                geom = geom.buffer(0)
            self.geoms.append(geom)
            self.props.append(f.get("properties") or {})
        self.tree = STRtree(self.geoms) if self.geoms else None
        log.info("%s: %d features in file, %d in London bbox, %d unreadable; index built in %.1fs", name, len(features), len(self.geoms), skipped, time.perf_counter() - t0)

    def lookup(self, lats, lons) -> list[list[dict]]:
        """For each (lat, lon) return the list of property dicts of containing polygons."""
        from shapely.geometry import Point

        out: list[list[dict]] = [[] for _ in lats]
        if not self.tree:
            return out
        pts = [Point(lon, lat) if in_london(lat, lon) else Point(0, 0) for lat, lon in zip(lats, lons)]
        q_idx, t_idx = self.tree.query(pts, predicate="within")
        for qi, ti in zip(q_idx, t_idx):
            out[qi].append(self.props[ti])
        return out


def load_geojson_features(path: Path) -> list[dict]:
    t0 = time.perf_counter()
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    feats = data["features"] if isinstance(data, dict) else data
    log.info("Loaded %s: %d features in %.1fs", path.name, len(feats), time.perf_counter() - t0)
    return feats


def find_conservation_geojson(data_dir: Path) -> Path | None:
    for p in sorted(data_dir.glob("*.geojson")) + sorted((data_dir / "reference").glob("*.geojson")):
        if "conservation" in p.name.lower():
            return p
    return None


def unique_points(df: pd.DataFrame):
    pts = df[["Lat", "Lon"]].dropna().drop_duplicates()
    return [float(v) for v in pts["Lat"]], [float(v) for v in pts["Lon"]], pts.index


def apply_point_lookup(df: pd.DataFrame, index: PolygonIndex, fn) -> pd.Series:
    """Run index.lookup on unique points and broadcast fn(props_list) back to all rows."""
    lats, lons, _ = unique_points(df)
    results = index.lookup(lats, lons)
    mapping = {(a, b): fn(r) for a, b, r in zip(lats, lons, results)}
    keys = list(zip(df["Lat"].astype(float, errors="ignore"), df["Lon"].astype(float, errors="ignore")))
    return pd.Series([mapping.get((float(a), float(b))) if not (pd.isna(a) or pd.isna(b)) else None for a, b in keys], index=df.index)


def add_conservation(df: pd.DataFrame, geojson_path: Path) -> pd.DataFrame:
    idx = PolygonIndex("conservation areas", load_geojson_features(geojson_path))
    hits = apply_point_lookup(df, idx, lambda r: r)
    df["Conservation Area?"] = hits.map(lambda r: bool(r) if r is not None else None).astype("boolean")
    df["conservation_area_name"] = hits.map(lambda r: r[0].get("NAME") or r[0].get("name") if r else None)
    df["conservation_area_lpa"] = hits.map(lambda r: r[0].get("LPA") if r else None)
    log.info("Conservation Area?: %d True, %d False, %d unknown (no point)", int((df["Conservation Area?"] == True).sum()), int((df["Conservation Area?"] == False).sum()), int(df["Conservation Area?"].isna().sum()))  # noqa: E712
    return df


def download_flood_zones(reference_dir: Path, session: requests.Session) -> Path:
    """One-off download of every flood-risk-zone polygon intersecting London (planning.data.gov.uk)."""
    out = reference_dir / "flood_risk_zones_london.geojson"
    if out.exists():
        log.info("Flood zones: using cached %s (%.0f MB)", out.name, out.stat().st_size / 1e6)
        return out
    pages_dir = reference_dir / "flood_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    features: list[dict] = []
    offset, limit = 0, 500
    t0 = time.perf_counter()
    while True:
        page_file = pages_dir / f"page_{offset}.geojson"
        if page_file.exists() and page_file.stat().st_size > 0:
            data = json.loads(page_file.read_text())
        else:
            resp = request_with_retry(session, "GET", PLANNING_DATA_GEOJSON_URL, params={
                "dataset": "flood-risk-zone", "geometry": LONDON_BBOX_WKT, "geometry_relation": "intersects",
                "limit": limit, "offset": offset,
            }, timeout=180)
            data = resp.json()
            page_file.write_text(json.dumps(data))
        feats = data.get("features", [])
        features.extend(feats)
        log.info("Flood zones: offset %d -> %d features (total %d) %.0fs", offset, len(feats), len(features), time.perf_counter() - t0)
        if len(feats) < limit or not (data.get("links") or {}).get("next"):
            break
        offset += limit
    slim = [{"type": "Feature", "geometry": f["geometry"], "properties": {k: f["properties"].get(k) for k in ("flood-risk-level", "flood-risk-type", "reference")}} for f in features]
    out.write_text(json.dumps({"type": "FeatureCollection", "features": slim}))
    log.info("Flood zones: wrote %s with %d features", out.name, len(slim))
    return out


def add_flood(df: pd.DataFrame, geojson_path: Path) -> pd.DataFrame:
    idx = PolygonIndex("flood risk zones", load_geojson_features(geojson_path))

    def level(r):
        lv = [int(p["flood-risk-level"]) for p in r if str(p.get("flood-risk-level", "")).isdigit()]
        return max(lv) if lv else None

    zones = apply_point_lookup(df, idx, level)
    has_pt = df["Lat"].notna() & df["Lon"].notna()
    df["flood_zone"] = pd.Series(zones, dtype="Int64")
    df["Flood risk?"] = pd.Series(zones.notna(), dtype="boolean").where(has_pt, pd.NA)
    log.info("Flood risk?: %s", df["flood_zone"].value_counts(dropna=False).to_dict())
    return df


# --------------------------------------------------------------------------- #
# 5. Population density (ONS Census 2021 TS006)
# --------------------------------------------------------------------------- #
def load_ts006(reference_dir: Path, session: requests.Session) -> dict[str, dict[str, float]]:
    zip_path = reference_dir / "census2021-ts006.zip"
    if not zip_path.exists():
        log.info("Downloading %s", TS006_ZIP_URL)
        reference_dir.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(request_with_retry(session, "GET", TS006_ZIP_URL).content)
    out: dict[str, dict[str, float]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for level in ("oa", "lsoa", "ltla"):
            with zf.open(f"census2021-ts006-{level}.csv") as fh:
                tbl = pd.read_csv(io.TextIOWrapper(fh, encoding="utf-8"))
            value_col = next(c for c in tbl.columns if c.startswith("Population Density"))
            out[level] = dict(zip(tbl["geography code"], tbl[value_col].astype(float)))
            log.debug("TS006 %s: %d areas", level, len(out[level]))
    return out


def find_ward_density_file(data_dir: Path) -> Path | None:
    for p in sorted(data_dir.glob("*.xlsx")) + sorted(data_dir.glob("*.csv")):
        if "density" in p.name.lower():
            return p
    return None


def load_ward_density(path: Path) -> dict[str, float]:
    """ONS TS006 ward-level workbook (Data/TS006-Population-Density-2021-wd-ONS.xlsx): code -> persons/km2."""
    tbl = pd.read_excel(path, sheet_name=0) if path.suffix == ".xlsx" else pd.read_csv(path)
    code_col = next(c for c in tbl.columns if "code" in str(c).lower())
    val_col = next(c for c in tbl.columns if str(c).lower().startswith(("observation", "population")))
    out = dict(zip(tbl[code_col].astype(str), pd.to_numeric(tbl[val_col], errors="coerce")))
    log.info("Ward density: %s -> %d wards (%s / %s)", path.name, len(out), code_col, val_col)
    return out


def add_population_density(df: pd.DataFrame, data_dir: Path, reference_dir: Path, session: requests.Session) -> pd.DataFrame:
    ward_file = find_ward_density_file(data_dir)
    if ward_file:
        wd = load_ward_density(ward_file)
        df["Population Density (Ward)"] = df["ward_code"].map(wd).astype("Float64")
        log.info("Ward density: %d/%d rows matched on ward code", int(df["Population Density (Ward)"].notna().sum()), len(df))
    else:
        log.warning("No ward-level density file (TS006 *density*.xlsx) in %s", data_dir)
        df["Population Density (Ward)"] = pd.array([None] * len(df), dtype="Float64")
    dens = load_ts006(reference_dir, session)
    df["Population Density (OA)"] = df["oa21"].map(dens["oa"]).astype("Float64")
    df["Population Density (LSOA)"] = df["lsoa21"].map(dens["lsoa"]).astype("Float64")
    df["Population Density (Borough)"] = df["borough_code"].map(dens["ltla"]).astype("Float64")
    df["Population Density"] = df["Population Density (Ward)"]
    level = pd.Series(pd.NA, index=df.index, dtype="string")
    level[df["Population Density"].notna()] = "ward"
    for col, tag in (("Population Density (OA)", "oa"), ("Population Density (LSOA)", "lsoa"), ("Population Density (Borough)", "borough")):
        fill = df["Population Density"].isna() & df[col].notna()
        df.loc[fill, "Population Density"] = df.loc[fill, col]
        level[fill] = tag
    df["population_density_level"] = level
    log.info("Population density: %s", level.value_counts(dropna=False).to_dict())
    return df


# --------------------------------------------------------------------------- #
# 6. Distance to nearest park (OSM via Overpass)
# --------------------------------------------------------------------------- #
def fetch_parks(cache: JsonCache, session: requests.Session) -> list[dict]:
    key = "leisure=park@" + ",".join(str(v) for v in LONDON_BBOX)
    if key in cache:
        cache.hits += 1
        parks = cache.get(key)
        log.info("Parks: %d from cache", len(parks))
        return parks
    cache.misses += 1
    s, w, n, e = LONDON_BBOX
    query = f'[out:json][timeout:300];(way["leisure"="park"]({s},{w},{n},{e});relation["leisure"="park"]({s},{w},{n},{e}););out geom;'
    log.info("Parks: fetching from Overpass (one-off; can take a few minutes)")
    resp = request_with_retry(session, "POST", OVERPASS_URL, data={"data": query}, retries=3, timeout=400)
    parks: list[dict] = []
    for el in resp.json().get("elements", []):
        name = (el.get("tags") or {}).get("name")
        if el["type"] == "way" and el.get("geometry"):
            parks.append({"id": el["id"], "name": name, "rings": [[(p["lon"], p["lat"]) for p in el["geometry"]]]})
        elif el["type"] == "relation":
            rings = [[(p["lon"], p["lat"]) for p in m["geometry"]] for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry") and m.get("role") in ("outer", "")]
            if rings:
                parks.append({"id": el["id"], "name": name, "rings": rings})
    log.info("Parks: %d fetched", len(parks))
    cache.set(key, parks)
    cache.save()
    return parks


def _to_metres(lon: float, lat: float, lat0: float = 51.5) -> tuple[float, float]:
    return lon * 111_320 * math.cos(math.radians(lat0)), lat * 110_574


def add_distance_to_park(df: pd.DataFrame, cache: JsonCache, session: requests.Session) -> pd.DataFrame:
    from shapely.geometry import LineString, Point, Polygon
    from shapely.strtree import STRtree

    geoms = []
    for park in fetch_parks(cache, session):
        for ring in park["rings"]:
            pts = [_to_metres(lon, lat) for lon, lat in ring]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                poly = Polygon(pts)
                geoms.append(poly if poly.is_valid else poly.buffer(0))
            elif len(pts) >= 2:
                geoms.append(LineString(pts))
    if not geoms:
        log.warning("No park geometries - Distance to Park left empty")
        df["Distance to Park (m)"] = pd.array([None] * len(df), dtype="Float64")
        return df
    tree = STRtree(geoms)
    lats, lons, _ = unique_points(df)
    pts = [Point(_to_metres(lon, lat)) for lat, lon in zip(lats, lons)]
    nearest = tree.nearest(pts)
    dist = {(a, b): round(geoms[i].distance(p), 1) for a, b, p, i in zip(lats, lons, pts, nearest)}
    df["Distance to Park (m)"] = pd.Series([dist.get((float(a), float(b))) if not (pd.isna(a) or pd.isna(b)) else None for a, b in zip(df["Lat"], df["Lon"])], index=df.index, dtype="Float64")
    log.info("Distance to Park: %d park geometries; median %.0f m", len(geoms), df["Distance to Park (m)"].median())
    return df


# --------------------------------------------------------------------------- #
# 7. Orchestration
# --------------------------------------------------------------------------- #
NEW_COLUMNS = ["Approved?", "Lat", "Lon", "Borough", "Month", "Day of the Week", "Conservation Area?", "Population Density", "Distance to Park (m)", "Flood risk?"]
SAMPLE_COLUMNS = ["LPA Number", "Borough", "Postcode", "Valid date", "Decision", "Approved?", "Lat", "Lon", "latlon_source", "Month", "Day of the Week",
                  "Conservation Area?", "conservation_area_name", "Flood risk?", "flood_zone", "Population Density", "population_density_level", "Population Density (OA)", "ward_name", "postcode_source",
                  "Distance to Park (m)", "dh_ward", "dh_application_type_full", "dh_application_details.site_area"]
STEPS = ["geocode", "conservation", "flood", "density", "parks"]


def parse_years(spec: str) -> list[int]:
    years: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            years.update(range(int(a), int(b) + 1))
        elif part:
            years.add(int(part))
    return sorted(years, reverse=True)  # newest first


def summarise(df: pd.DataFrame, caches: dict[str, JsonCache], rows_in: int, show_sample: int) -> None:
    lines = ["", "=== Processing summary ===", f"rows in: {rows_in}   rows out: {len(df)}   columns: {df.shape[1]}"]
    if "Lat" in df:
        lines.append(f"with Lat/Lon: {int(df['Lat'].notna().sum())} / {len(df)} ({df['Lat'].notna().mean():.1%})")
    if "Borough" in df:
        lines.append(f"distinct Borough values: {df['Borough'].nunique()}")
    if "Conservation Area?" in df:
        lines.append(f"in conservation area: {int((df['Conservation Area?'] == True).sum())}")  # noqa: E712
    if "Flood risk?" in df:
        lines.append(f"in flood zone: {int((df['Flood risk?'] == True).sum())}")  # noqa: E712
    lines.append("nulls per new column:")
    for col in NEW_COLUMNS:
        if col in df:
            lines.append(f"  {col:24s} {int(df[col].isna().sum()):7d}")
    lines.append("cache usage (hits / API calls):")
    for name, c in caches.items():
        lines.append(f"  {name:12s} {c.hits:7d} / {c.misses:5d}")
    dh_cols = [c for c in df.columns if c.startswith("dh_")]
    lines.append(f"Datahub columns ({len(dh_cols)}): {', '.join(dh_cols)}")
    for line in lines:
        log.info(line)
    if show_sample:
        cols = [c for c in SAMPLE_COLUMNS if c in df.columns]
        with pd.option_context("display.max_columns", None, "display.width", 250, "display.max_colwidth", 40):
            log.info("Sample rows:\n%s", df[cols].head(show_sample).T.to_string())


def run_pipeline(df: pd.DataFrame, args, out: Path, caches, session, data_dir: Path, reference_dir: Path, steps: set) -> pd.DataFrame:
    rows_in = len(df)
    if args.lpa_numbers:
        keep = {s.strip() for s in args.lpa_numbers.split(",")}
        df = df[df["LPA Number"].isin(keep)].reset_index(drop=True)
        log.warning("DEV: kept %d rows matching --lpa-numbers", len(df))
    if args.limit:
        df = df.head(args.limit).reset_index(drop=True)
        log.warning("DEV: --limit %d -> %d rows", args.limit, len(df))

    with timed("dates"):
        df = add_dates(df)
        df = add_decision_flag(df)
    if "geocode" in steps:
        with timed("geocode"):
            df = add_geocoding(df, caches["postcodes"], session)
    if "conservation" in steps and "Lat" in df:
        geo = args.conservation_geojson or find_conservation_geojson(data_dir)
        if geo:
            with timed("conservation"):
                df = add_conservation(df, geo)
        else:
            log.error("No conservation-area GeoJSON found in %s - skipping", data_dir)
    if "flood" in steps and "Lat" in df:
        with timed("flood"):
            df = add_flood(df, download_flood_zones(reference_dir, session))
    if "density" in steps and "ward_code" in df:
        with timed("density"):
            df = add_population_density(df, data_dir, reference_dir, session)
    if "parks" in steps and "Lat" in df:
        with timed("parks"):
            df = add_distance_to_park(df, caches["parks"], session)
    elif "Lat" in df:
        df["Distance to Park (m)"] = pd.array([None] * len(df), dtype="Float64")

    with timed("write"):
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, date_format="%Y-%m-%d")
        log.info("Wrote %s (%.1f MB)", out, out.stat().st_size / 1e6)
        try:
            pq = df.copy()
            for c in pq.columns[pq.dtypes == object]:
                pq[c] = pq[c].map(lambda v: v if v is None or isinstance(v, str) or (isinstance(v, float) and pd.isna(v)) else json.dumps(v) if isinstance(v, (list, dict)) else str(v))
            tmp = out.with_suffix(".parquet.tmp")
            pq.to_parquet(tmp, index=False)
            tmp.replace(out.with_suffix(".parquet"))
            log.info("Wrote %s", out.with_suffix(".parquet"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Parquet output skipped: %s", exc)
    summarise(df, caches, rows_in, args.show_sample)
    return df


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["datahub", "csv"], default="datahub")
    ap.add_argument("--years", default=str(datetime.now().year), help="Datahub valid_date years, e.g. 2016-2026, 2024, 2019,2021 (default: current year only)")
    ap.add_argument("--per-year", action="store_true", help="process each year separately, newest first, writing applications_enriched_<year>.*")
    ap.add_argument("--columns", default="default", help="Datahub column set: default | full | slim | comma list")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--out", type=Path, default=None, help="default: <data-dir>/processed/applications_enriched.csv")
    ap.add_argument("--conservation-geojson", type=Path, default=None)
    ap.add_argument("--limit", type=int, default=None, help="keep only the first N rows (dev loop)")
    ap.add_argument("--lpa-numbers", default=None, help="comma-separated LPA Numbers to keep (dev loop)")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma list of steps to run; default {','.join(STEPS)}")
    ap.add_argument("--skip-parks", action="store_true")
    ap.add_argument("--show-sample", type=int, default=0, help="print N enriched rows at the end")
    ap.add_argument("--refresh-cache", nargs="?", const="all", choices=["all", "datahub", "postcodes", "parks", "flood"])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    data_dir: Path = args.data_dir
    out: Path = args.out or data_dir / "processed" / "applications_enriched.csv"
    cache_dir, reference_dir, datahub_dir = data_dir / "cache", data_dir / "reference", data_dir / "datahub"
    setup_logging(out.parent / "processing.log", args.verbose)
    log.info("args: %s", vars(args))
    steps = set(s.strip() for s in args.steps.split(","))
    if args.skip_parks:
        steps.discard("parks")
    refresh = lambda name: args.refresh_cache in ("all", name)  # noqa: E731

    caches = {"postcodes": JsonCache(cache_dir / "postcodes.json"), "datahub_ids": JsonCache(cache_dir / "datahub_ids.json"), "parks": JsonCache(cache_dir / "parks_osm.json")}
    for name, c in caches.items():
        if refresh(name) or (name == "datahub_ids" and refresh("datahub")):
            log.info("Refreshing cache: %s", name)
            c.clear()
    if refresh("flood"):
        for p in [reference_dir / "flood_risk_zones_london.geojson", *(reference_dir / "flood_pages").glob("*.geojson")]:
            p.unlink(missing_ok=True)

    session = make_session()
    dh_session = datahub_session()
    columns = args.columns if args.columns in DATAHUB_COLUMN_SETS else [c.strip() for c in args.columns.split(",")]

    if args.source == "csv":
        with timed("load"):
            df = load_csvs(data_dir)
            df = enrich_csv_from_datahub(df, caches["datahub_ids"], dh_session)
        run_pipeline(df, args, out, caches, session, data_dir, reference_dir, steps)
        return 0

    years = parse_years(args.years)
    if not args.per_year:
        with timed("load"):
            df = load_datahub(years, datahub_dir, columns, refresh("datahub"), dh_session)
        run_pipeline(df, args, out, caches, session, data_dir, reference_dir, steps)
        return 0

    log.info("Per-year mode: %s", years)
    for year in years:
        year_out = out.with_name(f"{out.stem}_{year}{out.suffix}")
        if year_out.with_suffix(".parquet").exists() and not args.refresh_cache and not args.limit:
            log.info("[%d] %s already exists - skipping (use --refresh-cache to redo)", year, year_out.with_suffix(".parquet").name)
            continue
        with timed(f"year {year}"):
            with timed(f"load {year}"):
                df = load_datahub([year], datahub_dir, columns, refresh("datahub"), dh_session)
            run_pipeline(df, args, year_out, caches, session, data_dir, reference_dir, steps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
