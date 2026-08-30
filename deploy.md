# Deploying to Google Cloud Run

The app runs as one container (`Dockerfile`) with the processed data **baked into the image**; it scales to zero, so an
idle deployment costs ≈ $0 (only ~1.7 GB of image/bucket storage, a few cents/month). `cloudbuild.yaml` builds, pushes
and deploys; a Cloud Build trigger runs it on every push to `main`.

Because the data files are git-ignored, the build fetches them from a private bucket (`gs://<project>-data`) — this
bucket is only the hand-off from your machine to Cloud Build; the running service never reads it.

## One-time setup

Replace `PROJECT` with the project id (currently `confident-planner`):

```bash
gcloud projects create PROJECT --name="Confident Planner"
gcloud billing projects link PROJECT --billing-account=<your billing account id>
gcloud config set project PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    containerregistry.googleapis.com storage.googleapis.com

# data bucket + first upload (re-run the rsync whenever the pipeline produces new parquet files)
gsutil mb -l europe-west1 -b on gs://PROJECT-data
python Processing/processing.py --geo-bundle          # Data/reference/app_geo.pkl: pre-parsed polygon layers (~70 MB)
gsutil -m rsync -d -x '.*\.csv$|.*\.log$|.*\.tmp$|^full/.*' Data/processed gs://PROJECT-data/Data/processed
gsutil cp Data/reference/app_geo.pkl Data/reference/london_boroughs.geojson gs://PROJECT-data/Data/reference/

# build service account (Cloud Build triggers need an explicit one)
gcloud iam service-accounts create confident-planner-build
SA=confident-planner-build@PROJECT.iam.gserviceaccount.com
for role in roles/run.admin roles/iam.serviceAccountUser roles/storage.objectViewer roles/storage.admin \
            roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:$SA --role=$role --condition=None
done

# first deploy from this machine (validates the whole path; ~5 min)
gcloud builds submit --config cloudbuild.yaml --service-account=projects/PROJECT/serviceAccounts/$SA
gcloud run services describe confident-planner --region europe-west1 --format='value(status.url)'
```

## Continuous deployment

Connect the GitHub repo in the console (Cloud Build → Repositories → 2nd gen → Link repository) and create a
trigger: event *push to branch* `^main$`, configuration *Cloud Build configuration file* `cloudbuild.yaml`,
service account `confident-planner-build`. From then on every merge to `main` redeploys.

## Service settings and cost

Service settings live in `cloudbuild.yaml` substitutions: region `europe-west1` (Belgium — London's `europe-west2`
does not support the free custom-domain mapping below), 1 vCPU, 4 GiB (the process holds
~1.2 GB steady plus ~0.4 GB per request; 2 GiB runs at 85 %), `min-instances 0`, `max-instances 2`, CPU boost for
faster cold starts (~20 s: the parquet files and two GeoJSONs are parsed at start). While serving, the instance
costs ≈ $0.15 per hour of use; 2 M requests/month are free. If cold starts become annoying, `--min-instances 1`
keeps one warm for ≈ $25/month.

## Custom domain

`confidentplanner.andreithuler.com` — uses Cloud Run's built-in domain mapping (free, managed TLS):

```bash
# once: prove you own the parent domain (skip if `gcloud domains list-user-verified` already lists it)
gcloud domains verify andreithuler.com
gcloud beta run domain-mappings create --service confident-planner --domain confidentplanner.andreithuler.com --region europe-west1
gcloud beta run domain-mappings describe --domain confidentplanner.andreithuler.com --region europe-west1
```

Then add the DNS record the last command prints — a `CNAME confidentplanner → ghs.googlehosted.com.` — at the
provider that hosts `andreithuler.com`. The certificate is issued automatically once the record propagates
(usually within an hour; up to 24 h) and https://confidentplanner.andreithuler.com serves the app.

The service's default `*.run.app` URL is disabled (`--no-default-url` in `cloudbuild.yaml`; it returns 404), so the
custom domain is the only public entry point. Re-enable temporarily with
`gcloud run services update confident-planner --region europe-west1 --default-url` if you need it for debugging.

## Local check of the image

`docker build -t confident-planner . && docker run -p 8080:8080 confident-planner`.

## Updating the data

Re-run the pipeline locally (`python Processing/processing.py --years 2016-2026 --per-year --refresh-cache datahub`;
the per-year files contain only the ~21 columns the app and model read, ~110 MB for all years), rebuild the geo
bundle if the conservation/flood layers changed (`--geo-bundle`), re-run the `gsutil` lines above, then push any
commit to `main` (or `gcloud builds submit --config cloudbuild.yaml`) to bake the new files into a fresh image.

Cold start is dominated by pulling the data assets out of the image and parsing them; keep them small. What the
container loads at boot: slim parquet (~1 s) + `app_geo.pkl` (WKB polygons, ~1 s) + ML model.
