"""
jobs/store.py — the job queue's persistence layer.

A single SQLite database holds two tables:

    jobs        one row per submitted batch (a render or a scrape)
    job_items   one row per *video* — the reason a failed Drive upload can be
                retried on its own instead of re-rendering 300 files

WAL journalling lets the Streamlit process (which writes on submit) and the
worker process (which writes progress) use the database concurrently without
locking each other out. Every operation opens its own short-lived connection,
which also makes the store safe to call from the worker's render threads —
sqlite3 connections are not shareable across threads.

Render failures and upload failures are treated differently on purpose:

  * a render failure is usually deterministic (a background missing from the
    ZIP), so a retry would just fail again — the item stays failed.
  * an upload failure is usually transient (network, a 5xx from Drive), so
    those are retried up to DRIVE_UPLOAD_ATTEMPTS.

An item that was never marked at all — the worker died mid-render — stays
`pending` and is picked up again when the job resumes.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import config

# ---- job kinds
KIND_RENDER = "render"
KIND_SCRAPE = "scrape"
KIND_CAPTIONS = "captions"
# One submit that chains scrape -> clip choice -> captions -> render -> upload.
KIND_PIPELINE = "pipeline"

# ---- job statuses
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED}

# ---- per-item statuses
ITEM_PENDING = "pending"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"

# Which stage of a job an item belongs to. A pipeline job holds both, and
# their idx ranges are independent.
STAGE_RENDER = "render"
STAGE_SCRAPE = "scrape"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    params_json   TEXT NOT NULL DEFAULT '{}',
    result_json   TEXT,
    error         TEXT,
    stage         TEXT NOT NULL DEFAULT '',
    notify_email  TEXT NOT NULL DEFAULT '',
    submitted_by  TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    heartbeat_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_items (
    job_id          TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    render_status   TEXT NOT NULL DEFAULT 'pending',
    render_error    TEXT,
    render_attempts INTEGER NOT NULL DEFAULT 0,
    upload_status   TEXT NOT NULL DEFAULT 'pending',
    upload_error    TEXT,
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    drive_file_id   TEXT,
    drive_link      TEXT,
    warnings_json   TEXT NOT NULL DEFAULT '[]',
    meta_json       TEXT NOT NULL DEFAULT '{}',
    stage           TEXT NOT NULL DEFAULT 'render',
    updated_at      REAL NOT NULL,
    PRIMARY KEY (job_id, idx),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_render ON job_items(job_id, render_status);
CREATE INDEX IF NOT EXISTS idx_items_upload ON job_items(job_id, upload_status);

-- Every clip ever pulled from an account, so re-scraping next month only
-- fetches what is new. Keyed by TikTok's video id; content_hash is a backstop
-- for the same clip reposted under a new id.
CREATE TABLE IF NOT EXISTS scraped_clips (
    account      TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    content_hash TEXT,
    duration     REAL,
    first_seen   REAL NOT NULL,
    job_id       TEXT,
    PRIMARY KEY (account, video_id)
);

CREATE INDEX IF NOT EXISTS idx_clips_hash ON scraped_clips(content_hash);

-- Caption/hashtag pools. `cursor` is how many caption+hashtag PAIRS have been
-- handed out; see take_combinations() for why a counter replaces a
-- million-row used-pairs ledger.
CREATE TABLE IF NOT EXISTS caption_pools (
    id            TEXT PRIMARY KEY,
    theme         TEXT NOT NULL DEFAULT '',
    captions_json TEXT NOT NULL,
    hashtags_json TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    cursor        INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);
"""

# Columns update_item() will write. An allowlist, because the SET clause is
# built by string interpolation — never let a caller name the column freely.
_ITEM_FIELDS = {
    "name", "render_status", "render_error", "render_attempts",
    "upload_status", "upload_error", "upload_attempts",
    "drive_file_id", "drive_link", "warnings_json", "meta_json",
}


# --------------------------------------------------------------------------- connection

@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """A short-lived autocommit connection. isolation_level=None keeps Python
    from opening implicit transactions, so `BEGIN IMMEDIATE` below means what
    it says."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently
# does nothing on an existing table, so a new column has to be ALTERed in or
# every deployed database breaks on upgrade.
_MIGRATIONS = [
    # A pipeline job holds both scraped clips and rendered videos in job_items,
    # and their idx ranges would otherwise collide.
    ("job_items", "stage", "TEXT NOT NULL DEFAULT 'render'"),
]


def init_db() -> None:
    """Create the schema and apply any column migrations. Idempotent — both the
    app and the worker call this at startup so neither depends on the other
    having run first."""
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        for table, column, spec in _MIGRATIONS:
            existing = {row["name"] for row in
                        conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["params"] = json.loads(job.pop("params_json") or "{}")
    raw_result = job.pop("result_json", None)
    job["result"] = json.loads(raw_result) if raw_result else None
    return job


def _item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
    item["meta"] = json.loads(item.pop("meta_json") or "{}")
    return item


# --------------------------------------------------------------------------- jobs

def new_job_id() -> str:
    """Allocate an id before the job row exists.

    Submitting is a two-step dance on purpose: reserve an id, stage the uploads
    into its folder, and only then insert the row. Writing the row first would
    let the worker claim a job whose promo video is still being written."""
    return uuid.uuid4().hex[:16]


def create_job(
    kind: str,
    params: Optional[dict] = None,
    label: str = "",
    notify_email: str = "",
    submitted_by: str = "",
    items: Optional[Iterable[dict]] = None,
    job_id: Optional[str] = None,
) -> str:
    """Insert a queued job (plus its items) and return its id.

    Pass `job_id` from new_job_id() when assets were staged first — the job row
    is written last so the worker can never claim a half-built job."""
    job_id = job_id or new_job_id()
    now = time.time()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO jobs (id, kind, status, label, params_json, "
                "notify_email, submitted_by, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, STATUS_QUEUED, label,
                 json.dumps(params or {}), notify_email, submitted_by, now),
            )
            if items:
                conn.executemany(
                    "INSERT INTO job_items (job_id, idx, name, meta_json, stage, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    [(job_id, int(it["idx"]), str(it.get("name") or ""),
                      json.dumps(it.get("meta") or {}),
                      str(it.get("stage") or STAGE_RENDER), now) for it in items],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def merge_job_params(job_id: str, **values) -> dict:
    """Merge keys into a job's params and return the merged dict.

    For values a run has to *decide* and then keep deciding the same way on
    every resume. The Drive timestamp is the case this exists for: derived
    fresh each run it would send a requeued job to a brand-new folder, so it is
    written back the first time it is computed and read from here thereafter.

    Read-modify-write inside BEGIN IMMEDIATE, since the Streamlit process may
    be writing the same row."""
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT params_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return dict(values)
            params = json.loads(row["params_json"] or "{}")
            params.update(values)
            conn.execute("UPDATE jobs SET params_json=? WHERE id=?",
                         (json.dumps(params), job_id))
            conn.execute("COMMIT")
            return params
        except Exception:
            conn.execute("ROLLBACK")
            raise


def list_jobs(limit: int = 50, kinds: Optional[Iterable[str]] = None,
              statuses: Optional[Iterable[str]] = None) -> list[dict]:
    """Newest first — what the Jobs page renders."""
    sql = "SELECT * FROM jobs"
    where, params = [], []
    if kinds:
        kinds = list(kinds)
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    if statuses:
        statuses = list(statuses)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += statuses
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_job_from_row(r) for r in rows]


def claim_next_job(kinds: Optional[Iterable[str]] = None) -> Optional[dict]:
    """Atomically take the oldest queued job and mark it running.

    `BEGIN IMMEDIATE` takes the write lock up front, so two workers can never
    claim the same job. There is only one worker today, but getting this wrong
    would be a silent double-render, so it is done properly."""
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            sql = "SELECT id FROM jobs WHERE status=?"
            params: list[Any] = [STATUS_QUEUED]
            if kinds:
                kinds = list(kinds)
                sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                params += kinds
            sql += " ORDER BY created_at LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            now = time.time()
            conn.execute(
                "UPDATE jobs SET status=?, started_at=COALESCE(started_at,?), "
                "heartbeat_at=?, attempts=attempts+1, error=NULL WHERE id=?",
                (STATUS_RUNNING, now, now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return _job_from_row(claimed)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def heartbeat(job_id: str, stage: str = "") -> None:
    """Tell the world the job is still alive. A job whose heartbeat goes stale
    is assumed dead and requeued (see requeue_stale_jobs)."""
    with _conn() as conn:
        if stage:
            conn.execute("UPDATE jobs SET heartbeat_at=?, stage=? WHERE id=?",
                         (time.time(), stage, job_id))
        else:
            conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?",
                         (time.time(), job_id))


def set_stage(job_id: str, stage: str) -> None:
    heartbeat(job_id, stage)


def finish_job(job_id: str, status: str, error: Optional[str] = None,
               result: Optional[dict] = None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, error=?, result_json=?, finished_at=? "
            "WHERE id=?",
            (status, error, json.dumps(result) if result is not None else None,
             time.time(), job_id),
        )


def cancel_job(job_id: str) -> bool:
    """Cancel a job that hasn't started. A running job is left alone — killing
    a batch mid-FFmpeg would leave half-written files behind."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status=?, finished_at=? WHERE id=? AND status=?",
            (STATUS_CANCELLED, time.time(), job_id, STATUS_QUEUED),
        )
        cancelled = cur.rowcount > 0
    # The one terminal transition that never reaches worker.run_job, which is
    # why nothing has ever cleaned up after it. A queued job produced nothing
    # here — its folder is pure staged upload, a promo video plus an extracted
    # background ZIP, often gigabytes — and claim_next_job only ever takes
    # `queued`, so there is nothing left to resume.
    if cancelled:
        cleanup_job_dir(job_id)
    return cancelled


def requeue_stale_jobs(stale_seconds: Optional[float] = None,
                       max_attempts: Optional[int] = None) -> list[str]:
    """Recover jobs whose worker died. Anything still `running` with a heartbeat
    older than `stale_seconds` goes back to `queued` — per-item state means it
    resumes where it stopped. Past `max_attempts` it is failed instead, so a job
    that reliably crashes the worker can't loop forever."""
    stale = config.JOB_STALE_SECONDS if stale_seconds is None else stale_seconds
    cap = config.JOB_MAX_ATTEMPTS if max_attempts is None else max_attempts
    cutoff = time.time() - stale
    requeued: list[str] = []
    abandoned: list[str] = []
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, attempts FROM jobs WHERE status=? "
                "AND COALESCE(heartbeat_at, started_at, created_at) < ?",
                (STATUS_RUNNING, cutoff),
            ).fetchall()
            for row in rows:
                if row["attempts"] >= cap:
                    conn.execute(
                        "UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                        (STATUS_FAILED,
                         f"Abandoned after {row['attempts']} attempts — the worker "
                         "stopped responding each time.",
                         time.time(), row["id"]),
                    )
                    abandoned.append(row["id"])
                else:
                    conn.execute("UPDATE jobs SET status=? WHERE id=?",
                                 (STATUS_QUEUED, row["id"]))
                    requeued.append(row["id"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # Done after COMMIT so a slow rmtree never holds BEGIN IMMEDIATE open.
    # This branch was pure SQL, so three attempts' worth of staged assets and
    # render scratch used to sit here until the 7-day reaper. keep_outputs is
    # deliberate: an abandoned job may hold rendered videos that were never
    # published, and those are precisely what must not be deleted.
    for job_id in abandoned:
        cleanup_job_dir(job_id, keep_outputs=True)
    return requeued


# --------------------------------------------------------------------------- items

def add_items(job_id: str, items: Iterable[dict]) -> None:
    """Register items after the job exists — the scraper only learns how many
    clips there are once it has enumerated the profile."""
    now = time.time()
    rows = [(job_id, int(it["idx"]), str(it.get("name") or ""),
             json.dumps(it.get("meta") or {}),
             str(it.get("stage") or STAGE_RENDER), now) for it in items]
    if not rows:
        return
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO job_items (job_id, idx, name, meta_json, stage, updated_at) "
            "VALUES (?,?,?,?,?,?)", rows)


def list_items(job_id: str, stage: Optional[str] = STAGE_RENDER) -> list[dict]:
    """Items for one stage. Pass stage=None for every item in the job."""
    with _conn() as conn:
        if stage is None:
            rows = conn.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM job_items WHERE job_id=? AND stage=? ORDER BY idx",
                (job_id, stage)).fetchall()
    return [_item_from_row(r) for r in rows]


def pending_render_items(job_id: str, stage: str = STAGE_RENDER) -> list[dict]:
    """Items still needing a render. Excludes ones that already failed — a row
    that failed for a deterministic reason would only fail again."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM job_items WHERE job_id=? AND stage=? AND render_status=? "
            "ORDER BY idx", (job_id, stage, ITEM_PENDING)).fetchall()
    return [_item_from_row(r) for r in rows]


def pending_upload_items(job_id: str, max_attempts: Optional[int] = None,
                         stage: str = STAGE_RENDER) -> list[dict]:
    """Rendered items not yet in Drive. Unlike renders, failed uploads ARE
    retried — the usual cause is a transient network error."""
    cap = config.DRIVE_UPLOAD_ATTEMPTS if max_attempts is None else max_attempts
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM job_items WHERE job_id=? AND stage=? AND render_status=? "
            "AND (upload_status=? OR (upload_status=? AND upload_attempts < ?)) "
            "ORDER BY idx",
            (job_id, stage, ITEM_DONE, ITEM_PENDING, ITEM_FAILED, cap)).fetchall()
    return [_item_from_row(r) for r in rows]


def update_item(job_id: str, idx: int, **fields: Any) -> None:
    """Write named columns on one item. `warnings` and `meta` may be passed as
    Python objects and are JSON-encoded here."""
    if "warnings" in fields:
        fields["warnings_json"] = json.dumps(fields.pop("warnings"))
    if "meta" in fields:
        fields["meta_json"] = json.dumps(fields.pop("meta"))
    unknown = set(fields) - _ITEM_FIELDS
    if unknown:
        raise ValueError(f"Unknown job_item field(s): {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name}=?" for name in fields)
    values = list(fields.values()) + [time.time(), job_id, int(idx)]
    with _conn() as conn:
        conn.execute(
            f"UPDATE job_items SET {assignments}, updated_at=? "
            "WHERE job_id=? AND idx=?", values)


def set_upload_status(job_id: str, idxs: Iterable[int], status: str, *,
                      error: Optional[str] = None,
                      drive_link: Optional[str] = None,
                      drive_file_id: Optional[str] = None,
                      count_attempt: bool = False) -> int:
    """Record ONE Drive outcome across MANY items.

    Uploading a folder as a ZIP settles every video in it with a single Drive
    write, so the thousand items inside share one fate. update_item() opens a
    connection per row, which for a 16,000-video job would mean 16,000
    connections to record 32 facts — this writes them a chunk at a time
    instead.

    Chunked at 400 because the values go into an `IN (...)` list, and SQLite's
    parameter limit is 999 on older builds."""
    idxs = [int(i) for i in idxs]
    if not idxs:
        return 0
    attempts = "upload_attempts=upload_attempts+1, " if count_attempt else ""
    changed = 0
    with _conn() as conn:
        for start in range(0, len(idxs), 400):
            chunk = idxs[start:start + 400]
            marks = ",".join("?" * len(chunk))
            cur = conn.execute(
                "UPDATE job_items SET upload_status=?, upload_error=?, "
                "drive_link=COALESCE(?, drive_link), "
                "drive_file_id=COALESCE(?, drive_file_id), "
                f"{attempts}updated_at=? "
                f"WHERE job_id=? AND idx IN ({marks})",
                [status, error, drive_link, drive_file_id, time.time(),
                 job_id, *chunk],
            )
            changed += cur.rowcount
    return changed


def bump_item_attempts(job_id: str, idx: int, column: str) -> None:
    if column not in {"render_attempts", "upload_attempts"}:
        raise ValueError(f"Not an attempts column: {column}")
    with _conn() as conn:
        conn.execute(
            f"UPDATE job_items SET {column}={column}+1, updated_at=? "
            "WHERE job_id=? AND idx=?", (time.time(), job_id, int(idx)))


def item_counts(job_id: str, stage: str = STAGE_RENDER) -> dict[str, int]:
    """Progress summary for the UI and the notification email."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(render_status='done')   AS rendered,"
            " SUM(render_status='failed') AS render_failed,"
            " SUM(render_status='pending')AS render_pending,"
            " SUM(upload_status='done')   AS uploaded,"
            " SUM(upload_status='failed') AS upload_failed"
            " FROM job_items WHERE job_id=? AND stage=?", (job_id, stage)).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


# --------------------------------------------------------------------------- scrape dedup

def known_clip_ids(account: str) -> set[str]:
    """Video ids already pulled from this account, so a re-scrape next month
    only downloads what's new."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT video_id FROM scraped_clips WHERE account=?", (account,)).fetchall()
    return {r["video_id"] for r in rows}


def known_content_hashes(account: str) -> set[str]:
    """Backstop for the same clip reposted under a new video id."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT content_hash FROM scraped_clips "
            "WHERE account=? AND content_hash IS NOT NULL", (account,)).fetchall()
    return {r["content_hash"] for r in rows}


def remember_clips(account: str, clips: Iterable[dict], job_id: str = "") -> None:
    """Record clips as seen. INSERT OR IGNORE so a resumed scrape is harmless."""
    now = time.time()
    rows = [(account, str(c["video_id"]), c.get("content_hash"),
             c.get("duration"), now, job_id) for c in clips]
    if not rows:
        return
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO scraped_clips "
            "(account, video_id, content_hash, duration, first_seen, job_id) "
            "VALUES (?,?,?,?,?,?)", rows)


def forget_account(account: str) -> int:
    """Clear an account's dedup history so the next scrape re-pulls everything."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM scraped_clips WHERE account=?", (account,))
        return cur.rowcount


def scraped_accounts() -> list[dict]:
    """Accounts scraped before, with how many clips each has — shown on the
    scraper page so a re-scrape is an informed choice."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account, COUNT(*) AS clips, MAX(first_seen) AS last_seen "
            "FROM scraped_clips GROUP BY account ORDER BY last_seen DESC").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- caption pools

def _coprime_stride(total: int) -> int:
    """A stride coprime to `total`, so repeatedly adding it visits every value
    in 0..total-1 exactly once before repeating."""
    if total <= 2:
        return 1
    # Start near the golden-ratio fraction for a well-spread walk, then step up
    # until it shares no factor with total.
    candidate = max(2, int(total * 0.6180339887) | 1)
    for offset in range(total):
        stride = candidate + offset
        if math.gcd(stride, total) == 1:
            return stride % total or 1
    return 1


def save_pool(theme: str, captions: list[str], hashtags: list[str],
              model: str = "") -> str:
    """Store a new pool and make it the active one."""
    if not captions:
        raise ValueError("A pool needs at least one caption.")
    # A single empty set is legitimate: hashtags may come from an uploaded
    # sheet, or be switched off entirely.
    hashtags = list(hashtags) or [""]
    pool_id = uuid.uuid4().hex[:12]
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE caption_pools SET active=0")
            conn.execute(
                "INSERT INTO caption_pools (id, theme, captions_json, "
                "hashtags_json, model, cursor, active, created_at) "
                "VALUES (?,?,?,?,?,0,1,?)",
                (pool_id, theme, json.dumps(captions), json.dumps(hashtags),
                 model, time.time()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return pool_id


def get_pool(pool_id: str) -> Optional[dict]:
    """One pool by id — what a finished caption job needs to show its output."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM caption_pools WHERE id=?", (pool_id,)).fetchone()
    if not row:
        return None
    pool = dict(row)
    pool["captions"] = json.loads(pool.pop("captions_json"))
    pool["hashtags"] = json.loads(pool.pop("hashtags_json"))
    pool["combinations"] = len(pool["captions"]) * len(pool["hashtags"])
    return pool


def active_pool() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM caption_pools WHERE active=1 "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    pool = dict(row)
    pool["captions"] = json.loads(pool.pop("captions_json"))
    pool["hashtags"] = json.loads(pool.pop("hashtags_json"))
    pool["combinations"] = len(pool["captions"]) * len(pool["hashtags"])
    return pool


def list_pools(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, theme, model, cursor, active, created_at, "
            "LENGTH(captions_json) AS csize FROM caption_pools "
            "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def take_combinations(pool_id: str, count: int) -> list[tuple[str, str]]:
    """Hand out `count` caption+hashtag pairs that have never been used before.

    Rather than storing a row per used pair — 2,000 videos a day would be ~730k
    rows a year purely to answer "have we used this one?" — the pool is walked
    with a stride coprime to the number of combinations. Adding that stride
    repeatedly visits all N combinations exactly once before any repeat, so a
    single integer cursor gives the same guarantee as a full ledger, and the
    walk is reproducible.

    Advancing the cursor is atomic, so two jobs running back to back can never
    be handed the same pair."""
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT captions_json, hashtags_json, cursor FROM caption_pools "
                "WHERE id=?", (pool_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"No caption pool {pool_id!r}")
            captions = json.loads(row["captions_json"])
            hashtags = json.loads(row["hashtags_json"])
            start = int(row["cursor"])
            conn.execute("UPDATE caption_pools SET cursor=? WHERE id=?",
                         (start + int(count), pool_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    total = len(captions) * len(hashtags)
    stride = _coprime_stride(total)
    out: list[tuple[str, str]] = []
    for n in range(start, start + int(count)):
        combo = (n * stride) % total
        out.append((captions[combo // len(hashtags)], hashtags[combo % len(hashtags)]))
    return out


class PoolTooSmall(RuntimeError):
    """The pool cannot give every video a caption of its own."""

    def __init__(self, needed: int, available: int, total: int):
        self.needed = int(needed)
        self.available = int(available)      # never used before
        self.total = int(total)              # in the pool at all
        super().__init__(
            f"The active caption pool has {self.available:,} unused caption(s) "
            f"left of {self.total:,}, but this job needs {self.needed:,} — one "
            f"per video, and a caption is never used twice."
        )


def take_captions(pool_id: str, count: int) -> list[tuple[str, str]]:
    """Hand out `count` pairs whose **captions** are all different.

    take_combinations guarantees distinct caption+hashtag *pairings*, which is
    a weaker promise than it looks: one caption appears once per hashtag set,
    so two videos in the same job can be handed the same caption with different
    tags. A short filename is caption + the *first* hashtag only, so that is a
    duplicate name — and the old answer was to bolt ` (2)` onto it, which turns
    two identical captions into two files pretending to be versions of each
    other.

    Walking the caption list instead makes the caption the unit that cannot
    repeat: the same coprime-stride trick, but over `len(captions)`.

    **The walk never laps.** A pool is a consumable of exactly `len(captions)`
    videos — once the cursor reaches the end, the pool is spent and the next
    job is refused rather than quietly starting the captions over. That is what
    makes "one caption, one video" true across jobs and not merely inside one:
    since the cursor only ever moves forward through a single lap, and
    multiplying by a coprime stride is a bijection, every caption in the pool is
    handed out exactly once in its lifetime.

    Raises PoolTooSmall rather than repeating a caption — a job that cannot
    name its videos uniquely should stop before it renders anything, not after.
    """
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT captions_json, hashtags_json, cursor FROM caption_pools "
                "WHERE id=?", (pool_id,)).fetchone()
            if row is None:
                raise ValueError(f"No caption pool {pool_id!r}")
            captions = json.loads(row["captions_json"])
            # save_pool already normalises an empty list to [""]; belt and
            # braces, since a pool row can predate that.
            hashtags = json.loads(row["hashtags_json"]) or [""]
            start = int(row["cursor"])
            unused = max(0, len(captions) - start)
            if int(count) > unused:
                raise PoolTooSmall(count, unused, len(captions))
            conn.execute("UPDATE caption_pools SET cursor=? WHERE id=?",
                         (start + int(count), pool_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # start + count <= len(captions) is guaranteed above, so every index below
    # is a first visit. Hashtags DO repeat — deliberately: with the caption
    # already unique per video, the tags carry no naming duty at all.
    c_n, h_n = len(captions), len(hashtags)
    stride, hstride = _coprime_stride(c_n), _coprime_stride(h_n)
    return [
        (captions[(n * stride) % c_n], hashtags[(n * hstride) % h_n])
        for n in range(start, start + int(count))
    ]


def pool_remaining(pool_id: Optional[str] = None) -> tuple[int, int]:
    """(unused, total) captions for a pool — what a job may still draw.

    A pool is spent once `unused` hits zero, so this is the number worth
    showing next to “Batches to render” rather than the pool's size."""
    pool = active_pool() if pool_id is None else get_pool(pool_id)
    if not pool:
        return 0, 0
    total = len(pool["captions"])
    return max(0, total - int(pool.get("cursor") or 0)), total


# --------------------------------------------------------------------------- folders

def job_dir(job_id: str) -> Path:
    return config.job_dir(job_id)


def assets_dir(job_id: str) -> Path:
    return job_dir(job_id) / "assets"


def work_dir(job_id: str) -> Path:
    return job_dir(job_id) / "work"


def videos_dir(job_id: str) -> Path:
    return job_dir(job_id) / "videos"


def make_job_dirs(job_id: str) -> Path:
    root = job_dir(job_id)
    for sub in ("assets", "work", "videos"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def outputs_published(job: dict, result: Optional[dict]) -> tuple[bool, str]:
    """Is every byte this job produced also sitting somewhere off this VM?

    Returns (ok, reason) — the reason is logged either way, so an operator
    looking at a full disk can see exactly why a folder was spared.

    The asymmetry here is the whole design. Being wrong towards "keep" costs
    disk that the retention reaper reclaims in a week. Being wrong towards
    "delete" destroys a night of rendering that exists nowhere else. So every
    check below is a reason to KEEP, and the only way out is the final
    `return True`: anything unrecognised falls through to keeping the files.
    """
    if not config.JOB_PURGE_ON_FINISH:
        return False, "BVG_JOB_PURGE_ON_FINISH is off"

    # A caption job writes no files at all — the pool it produces is rows in
    # this database. There is nothing to prove and nothing worth keeping.
    if job.get("kind") == KIND_CAPTIONS:
        return True, "caption pools live in the database"

    # The runner raised, so worker.run_job never got a result dict. Whatever is
    # on disk is all there is.
    if not result:
        return False, "the job did not report a result"

    # One check covering three separate ways nothing reached Drive: Drive not
    # configured at all, Drive folder preparation failing, and the pipeline's
    # internal scrape stage (which is deliberately run with upload disabled
    # because the render stage consumes the clips locally).
    if not result.get("drive_link"):
        return False, "nothing was published to Drive"
    if result.get("upload_failed") or result.get("upload_failures"):
        return False, "some files did not reach Drive"

    # Publishing tk today and yt tomorrow is a supported way to stay under
    # Drive's 750 GB per 24 hours, and tomorrow's yt.zip is built out of
    # tonight's MP4s. Deleting them now would leave nothing to build it from.
    if result.get("videos_kept") or result.get("platforms_pending"):
        return False, "another set of names is still to be published"
    selected = result.get("upload_platforms")
    if selected is not None and set(selected) != {"yt", "tk"}:
        # The belt to that braces: `platforms_pending` is only populated when
        # the MP4s were actually kept, which itself requires free_local — so a
        # single-platform run with free_local off reports nothing pending.
        return False, "only one set of names was published"

    # Read with the identical expression the packer uses, so an operator who
    # asked for the MP4s to stay does not get them deleted by another route.
    params = job.get("params") or {}
    if not bool(params.get("free_local_videos", config.UPLOAD_FREE_LOCAL)):
        return False, "BVG_UPLOAD_FREE_LOCAL asks for the videos to stay here"

    rendered = result.get("rendered")
    if rendered is not None:
        if int(rendered) > int(result.get("uploaded") or 0):
            return False, "not every rendered video has a Drive record"
    elif int(result.get("trimmed") or 0) and not int(result.get("uploaded") or 0):
        return False, "the clips were not uploaded"

    return True, "published to Drive"


# The few kilobytes worth keeping when a job folder is purged: they are the
# only local record of which caption went with which filename, the Jobs page
# reads render_manifest.xlsx back as result["sheet_path"], and they weigh
# nothing against the hundreds of GB the purge reclaims. The retention reaper
# takes them in the end anyway.
_KEEP_AFTER_PURGE = {"render_manifest.xlsx", "render_log.txt",
                     "batch_sheet.xlsx", "metadata.xlsx"}
_KEEP_SUFFIX = "_manifest.xlsx"          # batch_NN_manifest.xlsx, one per folder
# So that a pathological file cannot defeat the purge by being named like a
# manifest.
_KEEP_MAX_BYTES = 64 * 1024 * 1024


def cleanup_job_dir(job_id: str, keep_outputs: bool = False) -> None:
    """Remove a finished job's files from this machine.

    keep_outputs=False — the output was confirmed off this VM, so everything
    goes except the handful of report files named above.
    keep_outputs=True — the output could NOT be confirmed anywhere else, so
    only reproducible scratch goes; videos/, clips/ and the local ZIP fallback
    stay behind for the retention reaper.
    """
    root = job_dir(job_id)

    if keep_outputs:
        if root.is_dir():
            # `packing` is in this list even though nothing was published: a
            # worker killed mid-pack leaves batch_NN/*.zip.part behind, and an
            # archive is rebuildable from the MP4s by definition.
            for sub in ("assets", "work", "packing"):
                shutil.rmtree(root / sub, ignore_errors=True)
        return

    # A KEEP-list rather than a delete-list, on purpose. The allow-list this
    # replaces was literally ["assets", "work"], which is exactly how videos/,
    # clips/, packing/, marketing_videos.zip, clips.zip and four manifests all
    # came to outlive every job by a week: each was added by a later runner and
    # nobody pointed the cleanup at it. Anything a future runner invents is now
    # deleted by default.
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                continue
            keep = (child.name in _KEEP_AFTER_PURGE
                    or child.name.endswith(_KEEP_SUFFIX))
            try:
                if keep and child.stat().st_size <= _KEEP_MAX_BYTES:
                    continue
            except OSError:
                pass
            child.unlink(missing_ok=True)

    # The Jobs page MOVES a result ZIP over 5 GB out of the job folder so
    # Tornado can stream it. That copy lives outside JOBS_ROOT where no reaper
    # has ever looked, so it has to travel with the job it belongs to.
    shutil.rmtree(config.STATIC_DOWNLOADS / job_id, ignore_errors=True)


def reap_old_jobs(retention_days: Optional[int] = None) -> list[str]:
    """Delete finished jobs' folders once they age out, so a long-lived VM
    never fills its disk. The database rows are kept — they are tiny and are
    the only history of what ran."""
    days = config.JOB_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = time.time() - days * 86400
    removed: list[str] = []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN (?,?,?) AND COALESCE(finished_at, created_at) < ?",
            (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_CANCELLED, cutoff)).fetchall()
        known = {r["id"] for r in conn.execute("SELECT id FROM jobs").fetchall()}
    for row in rows:
        root = job_dir(row["id"])
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)
            removed.append(row["id"])
        # Same reason cleanup_job_dir does this: an oversized ZIP the Jobs page
        # moved under ./static is outside JOBS_ROOT, and nothing else can see it.
        shutil.rmtree(config.STATIC_DOWNLOADS / row["id"], ignore_errors=True)

    # Folders with no database row at all. The query above is row-driven and
    # never lists JOBS_ROOT — but make_job_dirs runs BEFORE create_job on all
    # three submit paths, with the entire multi-GB upload staged inside that
    # window, so a bad ZIP or a Streamlit rerun mid-submit strands gigabytes
    # behind a folder this function could otherwise never see.
    #
    # `_`-prefixed names are skipped because tools/zip_drive_tk.py puts its
    # scratch in JOBS_ROOT/_repack, and a repack in progress must not be pulled
    # out from under the operator. jobs.db and its -wal/-shm are files, so
    # is_dir() already excludes them.
    if config.JOBS_ROOT.is_dir():
        for entry in config.JOBS_ROOT.iterdir():
            if (not entry.is_dir() or entry.name in known
                    or entry.name.startswith("_")):
                continue
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed
