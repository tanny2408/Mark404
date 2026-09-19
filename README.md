# Mark404

## Deploy to Google Cloud Run

The Streamlit app is packaged in the root `Dockerfile`. It listens on the
`PORT` environment variable supplied by Cloud Run and accepts the eight input
CSV files through the web interface.

### Build and run locally

```bash
docker build -t railway-track-access-optimiser .
docker run --rm -p 8501:8080 railway-track-access-optimiser
```

Open <http://localhost:8501> after the container starts.

### Deploy with Google Cloud CLI

Enable Cloud Run and Cloud Build APIs, then run from the repository root:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud run deploy railway-track-access-optimiser \
	--source . \
	--region asia-southeast1 \
	--allow-unauthenticated \
	--memory 2Gi \
	--cpu 2 \
	--timeout 3600 \
	--max 3
```

Cloud Run builds the Dockerfile with Cloud Build and prints the service URL
when deployment completes.