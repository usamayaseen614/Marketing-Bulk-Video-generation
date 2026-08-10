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
cp -n .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

**Verify both processes**, because a container where only Streamlit survived
will accept batches and never run them:

```bash
docker exec $(docker compose ps -q app) supervisorctl status
```

### Finding the external IP

**From the VM**, ask the metadata server — this needs no IAM permissions:

```bash
curl -s -H "Metadata-Flavor: Google"   http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip; echo
```

`gcloud compute instances describe` does NOT work from the VM: there you are
the instance's service account, which has the Drive and cloud-platform scopes
but not `compute.instances.get`. Run that form in Cloud Shell instead.

If that permission error appears, treat it as a signal: the service account may
have no IAM roles at all (newer projects disable automatic role grants to
default service accounts). Scopes only cap what a token *may* do — IAM decides
what it *can*. Vertex AI would then 403 in a way that reads like an application
bug, so grant the role explicitly from Cloud Shell:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID   --member=serviceAccount:YOUR_COMPUTE_SA_EMAIL   --role=roles/aiplatform.user
```

Drive needs no IAM role — only the folder shared with that same address.

### Updating

```bash
cd ~/app && git pull && docker compose up -d --build
```

That is the whole update. **Never re-run `cp .env.example .env`** — `.env` is
gitignored so `git pull` cannot touch it, but copying the template over it
wipes your configuration and every integration silently reverts to "not
configured". If the Setup page shows the project as `YOUR_PROJECT_ID`, that is
exactly what happened. (`cp -n` above refuses to overwrite, which is why it is
written that way.)

Changing settings alone needs no rebuild:

```bash
nano .env && docker compose up -d
```

Confirm the container actually picked them up:

```bash
docker compose exec app env | grep BVG_
```

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
   email from above → role **Content Manager** (Viewer or Commenter is not
   enough) → Send. If you instead share an individual *folder*, that
   dialog offers only Viewer / Commenter / **Editor** — pick Editor; it is the
   top role there and does everything this app needs.
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

### 5e. Disk sizing

Rendering needs room for the MP4s, and publishing them as ZIPs needs room for
two archives on top of that. Packing runs **one output folder at a time** and
each folder's MP4s are deleted as soon as both of its archives are verified in
Drive, so the high-water mark is:

```
all rendered MP4s  +  two archives of ONE output folder
```

At ~31 MB a video that is roughly **35 GB per 1,000 videos**, plus about 65 GB
of headroom for a 1,000-video folder. A 16,000-video night therefore wants a
**600 GB** data disk. A `pd-balanced` disk is $0.10/GB-month, so 600 GB for the
twelve hours a render actually takes is about **$1** — size it generously and
delete it afterwards rather than fighting for space at 3am.

Set `BVG_UPLOAD_FREE_LOCAL=false` to keep the MP4s on the VM after publishing
(the local ZIP fallback then still works, and the disk must hold everything).

### 5b-bis. A second service account, for a second 750 GB allowance

Drive's 750 GB per rolling 24 hours is charged **per identity**, so a second
service account has its own untouched allowance — and a brand-new one is
unspent, which is how you publish tonight instead of tomorrow.

Impersonation rather than a second key file: the VM mints short-lived tokens
for the new account, so no long-lived secret is written to disk and access is
revoked by deleting one IAM binding.

**Run these in Cloud Shell, NOT over SSH on the VM.** They are project-admin
operations and need *your* credentials. `gcloud` on the VM authenticates as the
VM's own service account, which has no permission to create service accounts or
edit IAM policy — there it fails with `PERMISSION_DENIED`. `gcloud auth list`
shows which identity you are about to use.

```bash
PROJECT=companion-app-26947
VM_SA=$(gcloud compute instances describe video-generator --zone=us-central1-a \
  --format='value(serviceAccounts[0].email)')
echo "the VM runs as: $VM_SA"

# 1. The API that mints the tokens.
gcloud services enable iamcredentials.googleapis.com --project=$PROJECT

# 2. The new identity.
gcloud iam service-accounts create video-uploads-2 \
  --project=$PROJECT --display-name="Drive uploads (second allowance)"

# 3. Let the VM act as it. This binding is the ONLY thing granting access.
gcloud iam service-accounts add-iam-policy-binding \
  video-uploads-2@$PROJECT.iam.gserviceaccount.com --project=$PROJECT \
  --member="serviceAccount:$VM_SA" \
  --role="roles/iam.serviceAccountTokenCreator"

echo "now share the Shared Drive with:"
echo "  video-uploads-2@$PROJECT.iam.gserviceaccount.com"
```

**Step 4 is manual and easy to forget:** open the Shared Drive in
drive.google.com → *Manage members* → add
`video-uploads-2@PROJECT.iam.gserviceaccount.com` as a **Content Manager**. A
new service account has no Drive access at all until you do.

Then point the app at it and restart the container:

```
BVG_DRIVE_IMPERSONATE=video-uploads-2@companion-app-26947.iam.gserviceaccount.com
```

*Test Drive access* on the Setup page now reports which account it published
as, so you can confirm the switch took effect — and know whose allowance is
being spent when uploads are refused.

To go back, unset the variable. To alternate between accounts, change it
between runs; the app publishes as exactly one identity per run, which keeps it
predictable about whose allowance a batch is spending.

A note on scale: a couple of identities for a genuinely large pipeline is
ordinary infrastructure. Spinning up a fleet of accounts specifically to
sidestep the cap is the kind of thing Google's terms are aimed at, and it is
also fragile — pair this with the one-platform-per-day option rather than
treating extra accounts as unlimited headroom.

### 5e-bis. "Shared drive not found" — membership vs folder access

`404 Shared drive not found: 0A…` means the service account is **not a member
of the Shared Drive**. That is not necessarily a problem: an account given
access to a *folder inside* the drive can read and write that folder perfectly
well, it just cannot see the drive as an object. Two API calls behave
differently and everything else works:

| Call | Member | Folder access only |
|---|---|---|
| `drives.get` — the drive's name | ✅ | ❌ 404 |
| `files.list(corpora='drive', driveId=…)` | ✅ | ❌ 404 |
| create folder / upload / copy inside the folder | ✅ | ✅ |
| create folder at the **drive root** | ✅ | ❌ 403 |

The app handles both: the name is best-effort, and searches fall back to an
ordinary parent-scoped query. **Point the destination at a folder inside the
Shared Drive rather than at the drive itself** and folder-level access is
enough for everything. Aiming at the drive root is the one thing that needs
real membership.

### 5f. The 750 GB/day Drive ceiling (plan around this)

Google allows **one user 750 GB per rolling 24 hours** of data moved into Drive.
The VM's service account is that user, and **server-side copies count as well as
uploads**. Past it every write returns `403 userRateLimitExceeded`.

For a night of 16,000 videos (~500 GB of MP4s) that is ~1 TB against the
allowance in *either* publishing mode — as ZIP uploads, or as uploads plus
`files.copy`. At ~31 MB a video, **one service account publishes about 12,000
videos a day under both names.**

Options when a run is bigger than that, best first:

- **Publish one set of names per day.** *Publish which names?* on the Generate
  page — or `upload_platforms` in the job's params — takes `yt,tk`, `tk` or
  `yt`. One platform halves the traffic, so ~24,000 videos fit in a day. Run
  the job, then requeue it the next day with the other platform: state is
  recorded per platform, so nothing is re-sent.

  The MP4s are **kept on the VM** while a platform is still outstanding, since
  the second day builds its archives from them. Size the disk for that — they
  are not freed until every platform has an archive.

- **Add a second service account** and point alternate runs at it with
  `BVG_DRIVE_CREDENTIALS_FILE` — each account gets its own 750 GB.
- **Render fewer, or smaller, videos** — at ~31 MB each the allowance is the
  binding limit long before disk or CPU is.

Throttling short of the ceiling is handled automatically: every Drive call
retries with exponential backoff, and the budget resets each time a chunk
lands, so a 30 GB archive survives repeated throttling. Tune with
`BVG_DRIVE_RETRY_ATTEMPTS` and `BVG_DRIVE_RETRY_MAX_SLEEP`.

### 5g. Repacking folders already in Drive

Renders published before ZIPs existed left every video as its own Drive file
under `batch_NN/tk/` and `batch_NN/yt/`. `tools/zip_drive_tk.py` converts them
in place, and it is built for a VM far smaller than the data: it works one
folder at a time, and inside a folder it downloads a handful of videos,
appends them to the archive and deletes them again — so **peak disk is one
folder's archive**, not the whole night.

```bash
# Over SSH on the VM. Check the plan first — this touches nothing:
sudo docker exec $(sudo docker compose ps -q app) \
  python tools/zip_drive_tk.py --link 'https://drive.google.com/drive/folders/XXXX' --dry-run

# One folder, to see the result in Drive before committing hours:
sudo docker exec $(sudo docker compose ps -q app) \
  python tools/zip_drive_tk.py --link 'https://drive.google.com/drive/folders/XXXX' --max-folders 1

# Then the rest. Use `screen`/`tmux`, or nohup, so SSH dropping doesn't kill it:
sudo docker exec $(sudo docker compose ps -q app) \
  python tools/zip_drive_tk.py --link 'https://drive.google.com/drive/folders/XXXX'
```

**Leaving it running overnight.** A plain `docker exec` dies with the SSH
session — the tool writes a progress line every few seconds, and once stdout is
a broken pipe the next one kills it. Run it detached inside the container
instead, which needs nothing installed on the VM:

```bash
CID=$(sudo docker ps --format '{{.ID}} {{.Image}}' | grep bulk-video-generator | cut -d' ' -f1)
sudo docker exec -d $CID sh -c \
  "mkdir -p /data/jobs/_repack && \
   python tools/zip_drive_tk.py --link 'https://drive.google.com/drive/folders/XXXX' \
   >> /data/jobs/_repack/run.log 2>&1"
```

The `mkdir` matters: the redirect is evaluated by the shell *before* Python
starts, so a missing log directory means nothing runs at all — and `-d`
swallows the error.

**Always confirm it started**, because `-d` prints nothing either way. Wait a
few seconds, then:

```bash
sudo docker exec $CID tail -20 /data/jobs/_repack/run.log
```

Advancing timestamps mean it is alive. An empty or missing log means the
command never ran — re-run it without `-d` to see the error.

The log is on the mounted volume, so it also survives the container being
replaced. The VM itself must stay up — stopping it pauses the work (it resumes
on re-run, but it will not finish while the machine is off).

**Nothing in Drive is ever deleted.** The `tk/` folder is left exactly as it
was, so the videos remain their own backup until you decide otherwise. Re-run
the same command any time: finished folders are recorded in
`/data/jobs/_repack/state.json` and skipped, and an archive that *is* re-made
replaces the old one rather than becoming a second file with the same name.

Add `--platform yt` to do the other half, and `--verify md5` to check every
download against Drive's checksum instead of just its size (exact, but it
re-reads every file).

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
- **Disk: a job's folder is deleted the moment its output is confirmed in
  Drive.** Staged uploads, scratch, the rendered MP4s, the archives and the
  local ZIP all go; only a few kilobytes of manifests and the render log stay
  behind, so the disk needs room for one night rather than a week of them.
  `BVG_JOB_RETENTION_DAYS` (default 7) now governs only the jobs that could
  *not* be published — Drive unconfigured, an upload that failed, or one of
  `yt`/`tk` still outstanding — plus folders stranded by a submit that never
  created a job row. Oversized ZIPs published under `./static/downloads/` are
  removed along with their job; nothing had ever cleaned those up before.
  Set `BVG_JOB_PURGE_ON_FINISH=false` to go back to keeping everything for the
  retention window. To reclaim disk by hand right now:

  ```bash
  docker exec <container> python -c "from jobs import store; print(store.reap_old_jobs(0))"
  ```
- **Outputs now leave the container.** With Drive configured, videos are
  uploaded as they finish and the email carries a Drive link that never
  expires. The ZIP is still produced as a fallback, but it lives on the `/data`
  mount rather than in `/tmp`, so it survives an image update.
- **The worker is the thing to check when nothing happens.** If batches sit in
  the queue, the Setup page says so explicitly. `docker logs` on the container
  shows both processes; supervisord restarts either one if it dies.
