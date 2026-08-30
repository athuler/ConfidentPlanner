# Confident Planner web app. The processed data and reference GeoJSONs are baked into the image
# (they are git-ignored; cloudbuild.yaml fetches them from the data bucket before building).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# code (Processing/ is imported by frontend/geo.py)
COPY Processing/ Processing/
COPY frontend/ frontend/
COPY Model/ Model/
COPY run.py .

# data assets - explicit paths only: never `COPY Data` (1.3 GB of CSVs + Datahub dumps)
COPY Data/processed/*.parquet Data/processed/
COPY Data/reference/london_boroughs.geojson Data/reference/flood_risk_zones_london.geojson Data/reference/
COPY Data/*.geojson Data/

EXPOSE 8080
# one worker (the in-memory DataStore is ~1-2 GB; do not duplicate it), threads for concurrency,
# no request timeout (the first request after a cold start may take a while)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 --access-logfile - frontend.wsgi:app
