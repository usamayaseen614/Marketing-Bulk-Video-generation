"""
config.py — environment-driven settings for the automation layer.

Everything the background worker, Drive uploader, mailer, Gemini caption pool
and TikTok scraper need to be told about the outside world lives here, read
from environment variables (optionally via a `.env` file beside this module).

Nothing in the original rendering path reads this module — `app.py`,
`video_generator.py` and `preview_editor.py` keep working with no environment
set at all. Features whose settings are missing report themselves as
unconfigured (see the `*_configured()` helpers) so the worker can skip them
with a clear log line instead of crashing a batch that otherwise succeeded.
"""

from __future__ import annotations

import os
from pathlib import Path

# A .env file is a convenience, not a requirement.
#
# BVG_IGNORE_DOTENV exists for the test suite. Without it, a developer's real
# .env — with live SMTP credentials and a real Shared Drive — silently becomes
# the configuration under test, so tests could send actual email or write to
# actual Drive. Tests set this before importing config, and everything then
# reports itself as unconfigured unless the test says otherwise.
if not os.environ.get("BVG_IGNORE_DOTENV"):
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).parent / ".env")
    except ImportError:  # pragma: no cover - python-dotenv missing is survivable
        pass


# --------------------------------------------------------------------------- helpers

def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    """Comma-separated env var to a clean list (empty entries dropped)."""
    return [part.strip() for part in _str(name, default).split(",") if part.strip()]


# --------------------------------------------------------------------------- storage

# Persistent root for job records, uploaded assets and rendered output.
#
# In the container this MUST point at a host-mounted volume — the live VM is a
# Container-Optimized OS box, so anything on the container filesystem is lost
# on `gcloud compute instances update-container`. Locally it defaults to a
# gitignored folder beside the repo.
JOBS_ROOT = Path(_str("BVG_JOBS_ROOT") or (Path(__file__).parent / "_jobs")).resolve()

# The SQLite job database. WAL mode lets the Streamlit process (which writes on
# submit) and the worker process (which writes progress) share it safely.
DB_PATH = Path(_str("BVG_DB_PATH") or (JOBS_ROOT / "jobs.db"))

# Finished job folders older than this are deleted by the worker's reaper.
# Outputs are in Drive by then; this only reclaims local disk.
JOB_RETENTION_DAYS = _int("BVG_JOB_RETENTION_DAYS", 7)


# --------------------------------------------------------------------------- worker

# How often the worker looks for queued work. A few seconds is plenty at ~7
# jobs/day and keeps the submit→start latency imperceptible.
WORKER_POLL_SECONDS = _float("BVG_WORKER_POLL_SECONDS", 3.0)

# A running job writes a heartbeat while it works. If one goes quiet for longer
# than this the worker assumes the process died and requeues it — per-item state
# means it resumes rather than restarting.
JOB_HEARTBEAT_SECONDS = _float("BVG_JOB_HEARTBEAT_SECONDS", 30.0)
JOB_STALE_SECONDS = _float("BVG_JOB_STALE_SECONDS", 300.0)

# How many times a job may be requeued after a crash before it is marked failed.
# Without this a job that reliably kills the worker would loop forever.
JOB_MAX_ATTEMPTS = _int("BVG_JOB_MAX_ATTEMPTS", 3)


# --------------------------------------------------------------------------- email

SMTP_HOST = _str("BVG_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _int("BVG_SMTP_PORT", 587)
SMTP_USER = _str("BVG_SMTP_USER")
# A Google Workspace *app password*, not the account password. Spaces are
# allowed in the value Google shows you; strip them so a copy-paste works.
SMTP_PASSWORD = _str("BVG_SMTP_PASSWORD").replace(" ", "")
SMTP_STARTTLS = _bool("BVG_SMTP_STARTTLS", True)

MAIL_FROM = _str("BVG_MAIL_FROM") or SMTP_USER
MAIL_TO = _list("BVG_MAIL_TO")

# Attach the updated Excel when it is under this size; link to Drive otherwise.
MAIL_MAX_ATTACHMENT_BYTES = _int("BVG_MAIL_MAX_ATTACHMENT_BYTES", 15 * 1024 * 1024)


def mail_configured() -> bool:
    """True when the transport itself is usable.

    MAIL_TO is deliberately NOT required here. A batch can carry its own
    recipient typed into the UI, and send() already refuses a message with no
    recipients — so requiring a server-wide default at this level would
    silently disable those per-job addresses."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and MAIL_FROM)


# --------------------------------------------------------------------------- drive

# The Shared Drive the service account uploads into. A Shared Drive is required:
# a service account has no personal Drive storage, so uploading into a *My Drive*
# folder shared with it fails outright.
DRIVE_SHARED_DRIVE_ID = _str("BVG_DRIVE_SHARED_DRIVE_ID")

# Optional folder inside that Shared Drive to act as the root for everything the
# app writes. Blank = the Shared Drive root.
DRIVE_ROOT_FOLDER_ID = _str("BVG_DRIVE_ROOT_FOLDER_ID")

# Path to a service-account JSON key. Leave blank on the GCP VM: Application
# Default Credentials pick up the VM's attached service account, so there is no
# key file to store, rotate or leak.
DRIVE_CREDENTIALS_FILE = _str("BVG_DRIVE_CREDENTIALS_FILE")

# How finished videos are published.
#
#   zip    one `yt.zip` and one `tk.zip` per output folder (the default). A
#          16,000-video night becomes ~32 archives instead of 32,000 files.
#   files  every video as its own Drive file under batch_NN/yt/ and
#          batch_NN/tk/, the second made with a server-side copy.
#
# `files` moves the bytes once and `zip` moves them twice: a ZIP is opaque to
# files.copy, so the archive holding the long names cannot be cloned from the
# one holding the short names. That is the price of the archives, and it is
# paid knowingly — see packing.py.
UPLOAD_MODE = _str("BVG_UPLOAD_MODE", "zip").lower()

# Which of the two names to publish: `yt`, `tk`, or both.
#
# This exists because of Drive's 750 GB per rolling 24 hours (see README). Each
# video is published under two names, so a night that renders 500 GB of MP4s
# moves a terabyte into Drive and cannot finish in one day. Publishing ONE
# platform halves that to something that fits, and the other can go the next
# day — the videos are kept on the VM until every selected platform has been
# published, and a requeued job skips what already landed.
UPLOAD_PLATFORMS = _list("BVG_UPLOAD_PLATFORMS", "yt,tk")

# In `zip` mode, delete an output folder's MP4s once BOTH of its archives are
# verified in Drive. Rendering 16,000 videos needs ~500 GB of disk; this keeps
# a long run from also needing room for the archives on top of it. Set false to
# keep the MP4s on the VM (the local ZIP fallback then still works).
UPLOAD_FREE_LOCAL = _bool("BVG_UPLOAD_FREE_LOCAL", True)

DRIVE_UPLOAD_CONCURRENCY = _int("BVG_DRIVE_UPLOAD_CONCURRENCY", 8)
DRIVE_UPLOAD_ATTEMPTS = _int("BVG_DRIVE_UPLOAD_ATTEMPTS", 3)
DRIVE_CHUNK_BYTES = _int("BVG_DRIVE_CHUNK_BYTES", 8 * 1024 * 1024)

# Uploads are sent a chunk at a time and every chunk is its own HTTP request.
# 8 MB was sized for ~8 MB videos; a 30 GB archive at that size is ~3,750
# requests, which is slow and an efficient way to get rate-limited. Must stay a
# multiple of 256 KB — Drive's resumable protocol requires it — and costs this
# much memory per upload in flight.
DRIVE_UPLOAD_CHUNK_BYTES = _int("BVG_DRIVE_UPLOAD_CHUNK_BYTES", 64 * 1024 * 1024)

# How patiently a throttled Drive call is retried before giving up.
#
# googleapiclient's own `num_retries` handles 5xx and 429 but gives up after a
# few seconds, and Drive answers a sustained transfer with 403
# userRateLimitExceeded — a *retryable* 403 that reads like a permission error
# and is not one. The budget is per chunk, so a transfer that keeps making
# progress is never abandoned for being slow. Ten attempts backing off to two
# minutes rides out roughly a quarter-hour of throttling.
DRIVE_RETRY_ATTEMPTS = _int("BVG_DRIVE_RETRY_ATTEMPTS", 10)
DRIVE_RETRY_MAX_SLEEP = _float("BVG_DRIVE_RETRY_MAX_SLEEP", 120.0)

# Pulling CTA clips from a Drive folder instead of uploading them through the
# browser. The VM's link to Google is an order of magnitude faster than a home
# connection, so this is server-to-server work the browser never touches.
DRIVE_DOWNLOAD_CONCURRENCY = _int("BVG_DRIVE_DOWNLOAD_CONCURRENCY", 8)

# A guard against pointing the app at someone's entire Drive by accident.
# Deliberately an error rather than a silent truncation — quietly using the
# "first 5,000" of a folder is how you get a batch built from the wrong clips.
DRIVE_MAX_SOURCE_FILES = _int("BVG_DRIVE_MAX_SOURCE_FILES", 5000)


def drive_configured(override: str = "") -> bool:
    """True when there is somewhere to upload to.

    `override` is a job's own folder link, which beats the env default —
    the VM's .env cannot be edited per batch."""
    return bool((override or "").strip() or DRIVE_SHARED_DRIVE_ID)


# --------------------------------------------------------------------------- gemini

# Vertex AI on the project that already runs the VM — same service account, no
# separate API key to manage. Model IDs are settings rather than constants
# because Google retires them on a schedule.
GCP_PROJECT = _str("BVG_GCP_PROJECT") or _str("GOOGLE_CLOUD_PROJECT")
VERTEX_LOCATION = _str("BVG_VERTEX_LOCATION", "us-central1")

# Pool generation is a handful of calls where quality matters — Pro tier.
# Nothing calls a model per video, so there is no bulk-tier model here.
GEMINI_POOL_MODEL = _str("BVG_GEMINI_POOL_MODEL", "gemini-2.5-pro")

# Emoji are stripped from captions and filenames. They are legal on every
# filesystem the videos touch, but they are not wanted here — so they are
# removed at the source (when a caption is generated) as well as when a
# filename is built. Set true to keep them.
FILENAME_KEEP_EMOJI = _bool("BVG_FILENAME_KEEP_EMOJI", False)

# How long a generated caption may be.
#
# This is derived from the filename budget, not picked arbitrarily. A name is
# capped at 90 characters and the long form also carries five hashtags — about
# 38 characters — leaving roughly 50 for the caption. Asking the model for
# anything longer just means cutting its sentence in half at naming time, so
# the limit is imposed at generation instead: the caption Gemini writes is the
# caption that ships.
CAPTION_MAX_CHARS = _int("BVG_CAPTION_MAX_CHARS", 50)

CAPTION_POOL_SIZE = _int("BVG_CAPTION_POOL_SIZE", 2000)
HASHTAG_POOL_SIZE = _int("BVG_HASHTAG_POOL_SIZE", 500)
CAPTION_THEME = _str("BVG_CAPTION_THEME")

# The most a pool may be asked for in one go — the upper bound on the UI's
# number inputs, not a default.
#
# A pool is a consumable: one caption per video, never reused, so a job of
# `batches x rows` videos needs that many captions. 1,000 rows across 16 promos
# is 16,000 videos and therefore 16,000 captions, which the old 5,000 ceiling
# simply refused. The limit exists to catch a typo'd 500000, so it is set well
# above any plausible real batch rather than near it.
CAPTION_POOL_MAX = _int("BVG_CAPTION_POOL_MAX", 50000)

# Hashtags repeat freely — with the caption already unique per video they carry
# no naming duty — so this rarely needs to be large.
HASHTAG_POOL_MAX = _int("BVG_HASHTAG_POOL_MAX", 5000)

# A pool is built in chunks of ~100, and the chunks are independent — each is a
# single-turn request that knows nothing of the others. Running them one after
# another made a 5,000-caption pool a 30-minute wait for a machine doing
# nothing but waiting. 8 in flight is the same total token spend, roughly an
# eighth of the wall-clock, and gentle enough not to trip Vertex's throttling.
CAPTION_CONCURRENCY = _int("BVG_CAPTION_CONCURRENCY", 8)

# Attempts per chunk before it is given up on. Without this a single 429 threw
# away the whole pool — which mattered far more once chunks run concurrently
# and there are more of them in flight to be throttled.
CAPTION_MAX_ATTEMPTS = _int("BVG_CAPTION_MAX_ATTEMPTS", 4)


def gemini_configured() -> bool:
    return bool(GCP_PROJECT)


# --------------------------------------------------------------------------- scraper

SCRAPE_MAX_VIDEOS = _int("BVG_SCRAPE_MAX_VIDEOS", 1500)
SCRAPE_BATCH_SIZE = _int("BVG_SCRAPE_BATCH_SIZE", 50)
SCRAPE_SLOTS = _int("BVG_SCRAPE_SLOTS", 5)

# Every clip is trimmed to a fixed window — a trim, not a filter, so no video is
# ever dropped for being too long. The default skips the first second, which is
# where creator intro branding and on-screen text usually sit.
SCRAPE_TRIM_START = _float("BVG_SCRAPE_TRIM_START", 1.0)
SCRAPE_TRIM_DURATION = _float("BVG_SCRAPE_TRIM_DURATION", 10.0)

# ~1 video per 1-2s. Faster than this gets the IP blocked.
SCRAPE_MIN_DELAY = _float("BVG_SCRAPE_MIN_DELAY", 1.0)
SCRAPE_MAX_DELAY = _float("BVG_SCRAPE_MAX_DELAY", 2.0)

# Optional Netscape-format cookies file. TikTok increasingly requires a logged-in
# session, especially from datacenter IPs like the VM's.
SCRAPE_COOKIES_FILE = _str("BVG_SCRAPE_COOKIES_FILE")


# --------------------------------------------------------------------------- paths

def job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def ensure_dirs() -> None:
    """Create the storage root. Safe to call repeatedly."""
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
