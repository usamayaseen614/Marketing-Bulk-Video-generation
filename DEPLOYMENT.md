# Deploying to Google Cloud

This guide deploys the app on a single Compute Engine VM. That is the right
shape for this workload: long CPU-bound FFmpeg batches (30–80 min for hundreds
of videos), large temp files, and a long-lived Streamlit process. Serverless
platforms (Cloud Run, App Engine) fight all three.

Two access models are covered — pick one:

| | Option A: IAP tunnel | Option B: Domain + Caddy |
|---|---|---|
| Public exposure | None (firewall allows only Google's IAP range) | Ports 80/443 open, HTTPS + basic auth |
| Team needs | `gcloud` CLI installed, Google account on the project | Just a browser + shared password |
| Extra infra | None | A DNS A record you control |
| Best for | Technical teams, max security | Mixed/marketing teams, nicest UX |

Prerequisites: a GCP project with billing, and the [gcloud CLI](https://cloud.google.com/sdk/docs/install)
authenticated (`gcloud auth login`, `gcloud config set project YOUR_PROJECT`).

```bash
# Used throughout — adjust to taste
export REGION=us-central1
export ZONE=us-central1-a
```

## 1. Build the image with Cloud Build (no local Docker needed)

```bash
gcloud services enable artifactregistry.googleapis.com cloudbuild.googleapis.com compute.googleapis.com

gcloud artifacts repositories create video-tools \
  --repository-format=docker --location=$REGION

# From the project directory (uploads source, builds remotely, pushes):
gcloud builds submit \
  --tag $REGION-docker.pkg.dev/$(gcloud config get-value project)/video-tools/bulk-video-generator:latest
```

## 2A. Option A — private VM reached through an IAP tunnel

```bash
# VM running the container (Container-Optimized OS)
gcloud compute instances create-with-container video-generator \
  --zone=$ZONE \
  --machine-type=c2d-standard-8 \
  --boot-disk-size=100GB \
  --tags=video-gen \
  --container-image=$REGION-docker.pkg.dev/$(gcloud config get-value project)/video-tools/bulk-video-generator:latest

# Firewall: ONLY Google's IAP range may reach the app/SSH. Nothing else can,
# so the VM is effectively private even though it has an external IP.
gcloud compute firewall-rules create video-gen-iap-only \
  --direction=INGRESS --action=ALLOW \
  --rules=tcp:22,tcp:8501 \
  --source-ranges=35.235.240.0/20 \
  --target-tags=video-gen
```

Grant each team member access (once per person):

```bash
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member=user:teammate@yourcompany.com \
  --role=roles/iap.tunnelResourceAccessor
```

Each team member then runs this and opens <http://localhost:8501>:

```bash
gcloud compute start-iap-tunnel video-generator 8501 \
  --local-host-port=localhost:8501 --zone=us-central1-a
```

## 2B. Option B — public VM with HTTPS and basic auth (Caddy)

```bash
# Plain Debian VM with Docker via startup script
gcloud compute instances create video-generator \
  --zone=$ZONE \
  --machine-type=c2d-standard-8 \
  --boot-disk-size=100GB \
  --image-family=debian-12 --image-project=debian-cloud \
  --tags=https-server \
  --metadata=startup-script='#!/bin/bash
    apt-get update && apt-get install -y docker.io docker-compose-v2 git'

gcloud compute firewall-rules create video-gen-https \
  --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443 \
  --target-tags=https-server
```

Then:

1. Point a DNS **A record** (e.g. `videos.yourcompany.com`) at the VM's external IP
   (`gcloud compute instances describe video-generator --zone=$ZONE --format='get(networkInterfaces[0].accessConfigs[0].natIP)'`).
2. Copy the project to the VM (`gcloud compute scp --recurse . video-generator:~/app --zone=$ZONE`)
   or `git clone` it there.
3. On the VM (`gcloud compute ssh video-generator --zone=$ZONE`):
   - Edit `Caddyfile`: set your domain and a password hash
     (`sudo docker run --rm caddy:2 caddy hash-password --plaintext 'YOUR_PASSWORD'`).
   - In `docker-compose.yml`, delete the `ports:` mapping on the `app` service
     so the app is reachable only through Caddy.
   - `cd ~/app && sudo docker compose --profile caddy up -d --build`

Team opens `https://videos.yourcompany.com` and logs in with the shared credentials.

## 3. Updating the app

```bash
# Rebuild and push (from the project directory)
gcloud builds submit --tag $REGION-docker.pkg.dev/$(gcloud config get-value project)/video-tools/bulk-video-generator:latest

# Option A — roll the container:
gcloud compute instances update-container video-generator --zone=$ZONE \
  --container-image=$REGION-docker.pkg.dev/$(gcloud config get-value project)/video-tools/bulk-video-generator:latest

# Option B — on the VM:
#   cd ~/app && git pull && sudo docker compose --profile caddy up -d --build
```

## 4. Controlling cost

A `c2d-standard-8` is ~$300/month if left running 24/7 — and this app only
needs to be up when someone is rendering.

```bash
# Stop when idle / start when needed (state & disk persist):
gcloud compute instances stop  video-generator --zone=$ZONE
gcloud compute instances start video-generator --zone=$ZONE

# Or attach an automatic schedule (example: up 9:00–19:00 Mon–Fri, PKT):
gcloud compute resource-policies create instance-schedule office-hours \
  --region=$REGION --timezone=Asia/Karachi \
  --vm-start-schedule='0 9 * * MON-FRI' \
  --vm-stop-schedule='0 19 * * MON-FRI'
gcloud compute instances add-resource-policies video-generator \
  --zone=$ZONE --resource-policies=office-hours
```

Rough sizing guide: with 2–3 parallel renders on 8 cores, expect roughly
1.5–3x realtime per video (a 30 s video renders in ~10–20 s), i.e. a 200-video
batch in ~20–40 minutes.

## Notes & gotchas

- **Upload limit** is set to 2 GB in the Dockerfile (`--server.maxUploadSize=2000`);
  raise it there if background ZIPs are bigger.
- **Result ZIPs over 500 MB** are served via Streamlit's static file serving
  (streamed from disk) instead of the in-memory download button — already wired
  up in the image, nothing to configure.
- **Concurrent users** are isolated: each browser session renders into its own
  output folder, and folders idle >24 h are cleaned automatically.
- **Disk:** 100 GB covers temp + outputs comfortably; batches clean up after
  themselves. If you render multi-thousand-video batches, size up.
- Outputs live in the container's `/tmp` (a Docker volume) — they do not
  survive image updates, so download the ZIP before rolling a new version.
