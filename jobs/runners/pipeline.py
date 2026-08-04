"""
jobs/runners/pipeline.py — one submit, the whole chain.

Scrape (optional) → choose clips → generate captions (optional) → render →
upload. Submit it and walk away.

## Why this exists

Every stage already worked; what didn't was the join between them. Scraped
clips landed on the VM, and the only way to use them was to download a ZIP to a
laptop, unzip it, and upload the same bytes back to the same machine through a
browser. This removes that round trip: clips are selected in place.

## How it reuses the existing runners

The chain deliberately adds almost no rendering logic. Whatever the clips came
from, this stage materialises them into `assets/cta_slot_N/` — exactly the
layout `stage_uploads()` produces from a browser upload. From there the render
runner is called unchanged and cannot tell the difference.

## Curation

Skipping curation entirely would let watermarked, off-brand and dud clips walk
straight into thousands of finished videos. But curation does not have to be
manual to exist: "take the top N by view count" is a rule, and a rule runs
unattended. Hand-picking is still available for when it matters.

## Resume

Stages are idempotent and check the state they produced rather than a stage
counter: clips already downloaded are skipped, an existing pool is reused,
materialising twice copies nothing new, and the render runner already resumes
per item. A pipeline killed anywhere picks up where it stopped.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import batching
import config
from jobs import store
from jobs.runners import render as render_runner

logger = logging.getLogger(__name__)


def _scrape_stage(job: dict, params: dict) -> dict:
    """Pull a fresh account. Reuses the scrape runner wholesale."""
    from jobs.runners import scrape as scrape_runner

    job_id = job["id"]
    store.set_stage(job_id, "scraping")
    scrape_params = {
        "account": params.get("account"),
        "mode": "dump",           # layout is irrelevant here; selection follows
        "batches": 1,
        "limit": int(params.get("scrape_limit") or config.SCRAPE_MAX_VIDEOS),
        "trim_start": float(params.get("trim_start", config.SCRAPE_TRIM_START)),
        "trim_duration": float(params.get("trim_duration", config.SCRAPE_TRIM_DURATION)),
        "skip_known": bool(params.get("skip_known", True)),
        # The pipeline uploads its own output; a second copy of raw clips in
        # Drive is just noise.
        "upload": False,
    }
    return scrape_runner.run({**job, "params": scrape_params})


def _gather_clips(job: dict, params: dict) -> list[dict]:
    """Every clip available to this pipeline, with its metadata.

    Sources: a scrape this job just ran, or a previous scrape job whose clips
    are still on disk. Both end up as the same list of dicts."""
    source_job = params.get("clips_from_job") or job["id"]
    clips_dir = store.job_dir(source_job) / "clips"
    if not clips_dir.is_dir():
        return []

    # Metadata lives on the scrape job's items; match it to the files on disk.
    # Map EVERY segment filename to its source clip, so segments 2..n inherit
    # the view count they were cut from — otherwise they would all rank 0 and
    # the top-N rule would never choose them.
    by_name: dict[str, dict] = {}
    for item in store.list_items(source_job, stage=store.STAGE_SCRAPE):
        meta = item.get("meta") or {}
        names = meta.get("segments") or ([item["name"]] if item.get("name") else [])
        for name in names:
            by_name[name] = item

    # View counts come from the scrape's metadata sheet, which is what the
    # top-N rule sorts on.
    views: dict[str, int] = {}
    sheet = store.job_dir(source_job) / "metadata.xlsx"
    if sheet.is_file():
        try:
            import pandas as pd

            frame = pd.read_excel(sheet, engine="openpyxl")
            for _, row in frame.iterrows():
                vid = str(row.get("Video_ID") or "").strip()
                if vid:
                    raw = row.get("Views")
                    views[vid] = int(raw) if raw == raw and raw is not None else 0
        except Exception:  # noqa: BLE001 — metadata is a nicety, not a gate
            logger.warning("Couldn't read %s for view counts", sheet, exc_info=True)

    clips = []
    for path in sorted(clips_dir.glob("*.mp4")):
        item = by_name.get(path.name) or {}
        video_id = (item.get("meta") or {}).get("video_id") or path.stem
        clips.append({
            "video_id": video_id,
            "path": str(path),
            "views": views.get(video_id, 0),
        })
    return clips


def _clips_stage(job: dict, params: dict) -> dict:
    """Choose clips and put them where the render runner expects them."""
    job_id = job["id"]
    assets = store.assets_dir(job_id)
    slots = int(params.get("slots") or config.SCRAPE_SLOTS)
    per_slot = int(params.get("clips_per_slot") or 10)

    clips = _gather_clips(job, params)
    if not clips:
        raise RuntimeError(
            "No clips available. The scrape produced nothing usable, or the "
            "chosen scrape job's clips have been cleaned up "
            f"(job folders are kept for {config.JOB_RETENTION_DAYS} days)."
        )

    strategy = params.get("clip_strategy", "top_views")
    selection = batching.select_clips(
        clips, slots=slots, per_slot=per_slot, strategy=strategy,
        picked_ids=params.get("picked_clip_ids"), seed=f"sel-{job_id}")

    chosen = sum(len(v) for v in selection.values())
    wanted = slots * per_slot

    # Rendering thousands of videos from a handful of clips wastes hours and
    # produces near-identical output. Stop instead, while it is cheap.
    minimum = int(params.get("min_clips") or slots)
    if chosen < minimum:
        raise RuntimeError(
            f"Only {chosen} usable clip(s) available but at least {minimum} are "
            f"needed to fill {slots} slots. Nothing was rendered. Scrape a "
            "different account, raise the video cap, or untick “skip clips "
            "already scraped”."
        )
    if chosen < wanted:
        logger.warning("Job %s: %d clips for %d slot places — slots will be "
                       "thinner than requested", job_id, chosen, wanted)

    copied = batching.materialize_slots(selection, assets)
    logger.info("Job %s: selected %d of %d clips (%s) into %d slots",
                job_id, chosen, len(clips), strategy, slots)
    return {
        "clips_available": len(clips),
        "clips_used": chosen,
        "clips_copied": copied,
        "clip_strategy": strategy,
        "slots": slots,
        "per_slot": {s: len(v) for s, v in sorted(selection.items())},
    }


def _captions_stage(job: dict, params: dict) -> dict:
    """Generate a pool first, when asked. Reuses the existing pool builder."""
    job_id = job["id"]
    theme = (params.get("caption_theme") or "").strip()
    if not theme:
        return {"skipped": "no theme given"}

    existing = store.active_pool()
    if existing and not params.get("force_new_pool"):
        return {"skipped": "reused the active pool", "pool_id": existing["id"]}

    from captions import pool as pool_module

    store.set_stage(job_id, "generating captions")

    def progress(done: int, total: int) -> None:
        store.heartbeat(job_id, stage=f"captions {done}/{total}")

    return pool_module.build_pool(
        theme=theme,
        caption_count=int(params.get("caption_count") or config.CAPTION_POOL_SIZE),
        hashtag_count=int(params.get("hashtag_count") or config.HASHTAG_POOL_SIZE),
        progress=progress,
    )


def run(job: dict) -> dict:
    """Run the whole chain."""
    job_id = job["id"]
    params = job.get("params") or {}
    started = time.time()
    result: dict = {"pipeline": True}

    # ---- 1. clips
    if params.get("clip_source") == "scrape_now":
        # Skip if a previous attempt already downloaded them.
        have = (store.job_dir(job_id) / "clips")
        if not (have.is_dir() and any(have.glob("*.mp4"))):
            result["scrape"] = _scrape_stage(job, params)
        else:
            logger.info("Job %s: clips already downloaded — skipping scrape", job_id)
            result["scrape"] = {"skipped": "clips already present"}

    if params.get("clip_source") in ("scrape_now", "scrape_job"):
        result["clips"] = _clips_stage(job, params)

    # ---- 2. captions
    if params.get("generate_pool"):
        try:
            result["caption_pool"] = _captions_stage(job, params)
        except Exception as exc:  # noqa: BLE001
            # Captions are an enhancement; falling back to Headline names is
            # far better than throwing away a finished scrape and a render.
            logger.warning("Job %s: caption generation failed (%s) — continuing",
                           job_id, exc)
            result["caption_pool"] = {"error": str(exc)}

    # ---- 3 & 4. render and upload, unchanged
    store.set_stage(job_id, "rendering")
    render_result = render_runner.run(job)
    result.update(render_result)
    result["elapsed"] = time.time() - started
    logger.info("Job %s pipeline finished in %.0fs", job_id, result["elapsed"])
    return result
