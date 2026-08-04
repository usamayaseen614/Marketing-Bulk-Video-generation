# Deploying to Google Cloud

> ## ⚠️ Read this first — the container-VM path is dead
>
> `gcloud compute instances create-with-container` was deprecated in July 2025,
> and its container startup agent (**konlet**) was **shut down on 2026-07-31**.
> Google: *"any workflows that rely on the container startup agent or the
> `gce-container-declaration` instance metadata no longer work."*
>
> The failure is **silent**: the VM boots, reports RUNNING, bills at full rate,
> and never starts the container. Verify deployments with `docker ps` over SSH,
> never with instance status.
>
> **Use [section 0](#0-fresh-deployment-debian--docker-compose) below.** Any
> section here that mentions `create-with-container` or `update-container`
> describes the old path and is kept only for VMs created before that date.

## 0. Fresh deployment: Debian + Docker Compose

This replaces sections 1 and 2. It needs no Artifact Registry and no Cloud
Build — the image is built on the VM from the repo.

**Scopes must be set at creation.** They cannot be changed on a running VM, and
`cloud-platform` does **not** include Drive (Drive is a Workspace API, outside
that umbrella). There is no gcloud alias for it either, so the full URI is
required. Passing `--scopes` *replaces* the default set, which is why
`cloud-platform` is listed too — without it you silently lose Cloud Logging and
Monitoring.

```bash
gcloud compute instances create video-generator   --project=YOUR_PROJECT_ID   --zone=us-central1-a   --machine-type=e2-standard-4   --image-family=debian-13   --image-project=debian-cloud   --boot-disk-size=100GB   --boot-disk-type=pd-balanced   --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive   --tags=streamlit-8501   --metadata=startup-script='#!/bin/bash
set -eux
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
touch /var/log/startup-done
'
```

`gcloud` splits `--metadata` on commas, so a single comma in that script would
truncate it. The script above is deliberately comma-free; if you edit it, use
`--metadata-from-file=startup-script=startup.sh` instead.

Firewall — restrict to your own IP (`curl -s ifconfig.me`). Streamlit has no
authentication, so `0.0.0.0/0` puts a Drive-writing, Vertex-billing app in front
of the internet:

```bash
gcloud compute firewall-rules create allow-streamlit-8501   --project=YOUR_PROJECT_ID --network=default   --direction=INGRESS --action=ALLOW --rules=tcp:8501   --target-tags=streamlit-8501 --source-ranges=YOUR.IP.HERE/32
```

```bash
gcloud services enable drive.googleapis.com aiplatform.googleapis.com --project=YOUR_PROJECT_ID
```

Then SSH in and start it:

```bash
gcloud compute ssh video-generator --project=YOUR_PROJECT_ID --zone=us-central1-a
```

```bash
while [ ! -f /var/log/startup-done ]; do echo waiting; sleep 10; done
sudo usermod -aG docker $USER && exec newgrp docker
git clone -b feat/automation-pipeline https://github.com/YOUR_USER/YOUR_REPO.git app && cd app
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

**Verify both processes**, because a container where only Streamlit survived
will accept batches and never run them:

```bash
docker exec $(docker compose ps -q app) supervisorctl status
```

Updating later is `git pull && docker compose up -d --build`. Environment
changes are just an edit to `.env` plus `docker compose up -d` — no need to
re-specify anything, unlike the old `update-container` flow.

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

**This changed with background jobs.** The app used to do its work inside the
browser request, so stopping the VM whenever nobody was looking at it was safe.
Now a click only *queues* a job — the worker inside the container does the
rendering, scraping and uploading afterwards. **Stopping the VM while a job is
queued or running stalls it**, and an automatic stop schedule will eventually
cut a batch in half.

Nothing is lost when that happens: a killed job is requeued on the next start
and resumes from per-item state rather than re-rendering. But the batch does not
finish, and no email arrives, until the VM is back.

So the rule is now: **stop it when the Jobs page shows nothing queued or
running**, not merely when nobody is using the UI.

```bash
# Check first — this should print nothing before you stop the VM:
#   open http://EXTERNAL_IP:8501/Jobs and confirm "Active (0)"

gcloud compute instances stop  video-generator --zone=us-central1-a
gcloud compute instances start video-generator --zone=us-central1-a
```

A fixed stop schedule is now a poor fit — a 19:00 stop will kill a 18:50 batch
every time. If you want one anyway, set it well clear of when batches are
submitted:

```bash
gcloud compute resource-policies create instance-schedule office-hours \
  --region=us-central1 --timezone=Asia/Karachi \
  --vm-start-schedule='0 9 * * MON-FRI' \
  --vm-stop-schedule='0 23 * * MON-FRI'
gcloud compute instances add-resource-policies video-generator \
  --zone=us-central1-a --resource-policies=office-hours
```

Reminder on sizing: the live VM is a `c2d-standard-32` at roughly **$1.50/hour,
about $1,100–1,200/month if left running 24/7**. Stopped, you pay only for the
100 GB disk (a few dollars a month). The external IP changes on every
stop/start unless you reserve a static one.

## 5. The automation layer

Three things must be configured for the background jobs to do anything useful.
All of them are optional — anything unconfigured reports itself on the app's
**Setup** page and is skipped, rather than failing a batch.

### 5a. Persistent storage (required)

Job records, staged uploads and rendered videos live in `/data` inside the
container. **That must be a host mount.** Without it, `update-container` wipes
queued jobs and any finished videos that had not yet reached Drive.

```bash
gcloud compute instances update-container video-generator \
  --zone=us-central1-a \
  --container-image=us-central1-docker.pkg.dev/YOUR_PROJECT_ID/video-tools/bulk-video-generator:latest \
  --container-mount-host-path=host-path=/var/lib/videogen,mount-path=/data,mode=rw
```

`/var/lib/videogen` survives container updates and reboots on Container-Optimized
OS. It does not survive deleting the VM.

### 5b. Drive and Vertex AI access (needs the VM stopped)

The VM's service account only gets tokens for the scopes the instance was
created with, and **`cloud-platform` alone does not include Drive**. Changing
scopes requires the VM to be stopped.

```bash
gcloud compute instances stop video-generator --zone=us-central1-a

gcloud compute instances set-service-account video-generator \
  --zone=us-central1-a \
  --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform

gcloud compute instances start video-generator --zone=us-central1-a
```

Then enable the APIs and let the service account use Vertex AI:

```bash
gcloud services enable drive.googleapis.com aiplatform.googleapis.com \
  --project=YOUR_PROJECT_ID

# Find the service account the VM runs as:
gcloud compute instances describe video-generator --zone=us-central1-a \
  --format='get(serviceAccounts[0].email)'

# Give it Vertex AI access (paste the email from the command above):
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member=serviceAccount:PASTE_THE_EMAIL_HERE \
  --role=roles/aiplatform.user
```

**The Shared Drive is a manual step in the browser**, because a service account
has no Drive storage of its own — a folder in someone's *My Drive* shared with
it will fail on the first upload:

1. drive.google.com → **Shared drives** in the left sidebar → **New**.
   If there is no *Shared drives* entry, the account is not on Google Workspace
   and this route will not work.
2. Open the new Shared Drive → **Manage members** → paste the service account
   email from above → role **Content Manager** (Viewer or Contributor is not
   enough) → Send.
3. Copy the ID out of the URL: `drive.google.com/drive/folders/`**`<this part>`**.

### 5c. Settings

Everything else is environment variables — see `.env.example` for the full list
with explanations. On the container VM they go on the container:

```bash
gcloud compute instances update-container video-generator \
  --zone=us-central1-a \
  --container-image=us-central1-docker.pkg.dev/YOUR_PROJECT_ID/video-tools/bulk-video-generator:latest \
  --container-mount-host-path=host-path=/var/lib/videogen,mount-path=/data,mode=rw \
  --container-env=BVG_DRIVE_SHARED_DRIVE_ID=PASTE_DRIVE_ID,BVG_GCP_PROJECT=YOUR_PROJECT_ID,BVG_SMTP_USER=notifications@yourcompany.com,BVG_SMTP_PASSWORD=PASTE_APP_PASSWORD,BVG_MAIL_TO=you@yourcompany.com,BVG_CAPTION_THEME=PASTE_YOUR_THEME
```

`BVG_SMTP_PASSWORD` is a Google **app password** (16 characters), not the
account password. Create one at myaccount.google.com → Security → 2-Step
Verification → App passwords; that account needs 2-Step Verification on first.

Then open **Setup** in the app and use the three test buttons — *Send test
email*, *Test Drive access*, *Test Vertex AI connection*. Each reports the
specific cause when something is wrong, rather than a stack trace.

### 5d. Generate a caption pool

Captions are generic and themed, generated in bulk occasionally and recombined
per batch — there is no model call per video. On the **Setup** page, set the
theme and press *Generate a new pool*. 2,000 captions × 500 hashtag sets is a
million unique pairs, roughly a year and a half at 2,000 videos a day, and costs
about $2 of Gemini usage to build.

## 6. Security — worth doing before this goes further

The firewall rule `video-gen-public` currently allows `tcp:8501` from
`0.0.0.0/0`, so **the app is reachable by anyone who finds the IP, with no
login**. That was a modest risk when the worst case was a stranger rendering
videos. It is a larger one now: the app writes to your Shared Drive, sends mail
from your domain, and spends money on Vertex AI.

Two ways to close it, both already supported by this repo:

```bash
# Option 1 — lock the firewall to Google's IAP range and tunnel in (section 2A)
gcloud compute firewall-rules delete video-gen-public
gcloud compute firewall-rules create video-gen-iap-only \
  --direction=INGRESS --action=ALLOW \
  --rules=tcp:22,tcp:8501 \
  --source-ranges=35.235.240.0/20 \
  --target-tags=video-gen

# Option 2 — put Caddy in front with HTTPS + a shared password (section 2B)
```

Option 2 keeps the "just open a URL" experience for non-technical teammates.

## 7. A note on `create-with-container`

Google now marks `gcloud compute instances create-with-container` as deprecated
and points to running `docker run` on a normal VM instead. The existing VM keeps
working and everything above applies to it unchanged, so there is no urgency.

When you do migrate, it gets simpler rather than harder: a plain Debian VM with
Docker installed can use the `docker-compose.yml` in this repo directly
(`docker compose up -d`), with settings in a `.env` file instead of a very long
`--container-env` flag.

Rough sizing guide: with 2–3 parallel renders on 8 cores, expect roughly
1.5–3x realtime per video (a 30 s video renders in ~10–20 s), i.e. a 200-video
batch in ~20–40 minutes.

## Notes & gotchas

- **Upload limit** is set to 2 GB in the Dockerfile (`--server.maxUploadSize=2000`);
  raise it there if background ZIPs are bigger.
- **Result ZIPs over 500 MB** are served via Streamlit's static file serving
  (streamed from disk) instead of the in-memory download button — already wired
  up in the image, nothing to configure.
- **Jobs, not sessions.** Batches are queued and run by a worker process, so
  closing the tab, sleeping the laptop or losing the websocket no longer kills a
  render. Reopen the **Jobs** page from any browser to see live progress.
- **Resume is automatic.** If the worker dies mid-batch — OOM, `docker stop`, a
  VM restart — the job is requeued on the next start and picks up from
  per-item state. A crash at video 250 of 300 costs the one video in flight,
  not the 250 already finished.
- **Disk:** 100 GB covers temp + outputs comfortably. Finished job folders are
  reaped after `BVG_JOB_RETENTION_DAYS` (default 7). Uploaded assets are deleted
  as soon as a job finishes; only the videos linger.
- **Outputs now leave the container.** With Drive configured, videos are
  uploaded as they finish and the email carries a Drive link that never
  expires. The ZIP is still produced as a fallback, but it lives on the `/data`
  mount rather than in `/tmp`, so it survives an image update.
- **The worker is the thing to check when nothing happens.** If batches sit in
  the queue, the Setup page says so explicitly. `docker logs` on the container
  shows both processes; supervisord restarts either one if it dies.
