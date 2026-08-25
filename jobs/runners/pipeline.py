"""
jobs/runners/pipeline.py — one submit, the whole chain.

Scrape (optional) → choose clips → generate captions (optional) → render →
upload. Submit it and walk away.

## Why this exists

Every stage already worked; what didn't was the join between them. Scraped
clips landed on the VM, and the only way to use them was to download a ZIP to a
laptop, unzip it, and upload the same bytes back to the same machine through a
browser. This removes that round trip: clips are selected in place.

The same argument covers a clip library kept in Google Drive. Uploading it
through the browser means the bytes leave Google, crawl up a home connection,
and go straight back to Google. `_drive_clips_stage` has the VM fetch them
directly instead — a link, not an upload.

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
import workspace
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

    Sources: a scrape this job just ran, a previous scrape job whose clips are
    still on disk, or a Drive folder downloaded into the same place. All end up
    as the same list of dicts — clips with no scrape behind them simply have no
    view counts, which only the `top_views` strategy would have used."""
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
    for path in sorted(p for p in clips_dir.iterdir()
                       if p.is_file() and workspace.is_video(p)):
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
            "No clips available. The scrape produced nothing usable, or its "
            "clips have been removed from this machine — that happens as soon "
            "as they are safely in Drive. To reuse them without scraping "
            "again, switch the clip source to Google Drive folder and paste "
            "that scrape's Drive folder link."
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
        advice = (
            "Point at a Drive folder with more clips in it, or lower the number "
            "of clip slots." if params.get("clip_source") == "drive_folder" else
            "Scrape a different account, raise the video cap, or untick “skip "
            "clips already scraped”."
        )
        raise RuntimeError(
            f"Only {chosen} usable clip(s) available but at least {minimum} are "
            f"needed to fill {slots} slots. Nothing was rendered. {advice}"
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


def _download_drive_folder(job_id: str, link: str, dest: Path, label: str,
                           kind: str = "video") -> dict:
    """Pull one Drive folder's clips onto this machine.

    The point of the whole mode: the clips already live in Drive, and the VM
    talks to Google far faster than a laptop on a home connection does. Sending
    them down a browser upload and back up again is the slow path, and this
    removes it."""
    from integrations import drive

    folder_id = drive.extract_id(link or "")
    if not folder_id:
        raise RuntimeError(f"{label}: no Google Drive folder link was given.")

    def progress(done: int, total: int) -> None:
        store.heartbeat(job_id, stage=f"downloading {label} {done}/{total}")

    try:
        report = drive.download_folder(folder_id, dest, on_progress=progress,
                                       kind=kind)
    except drive.DriveError as exc:
        # These already say exactly what is wrong and what to do about it.
        raise RuntimeError(f"{label}: {exc}") from exc

    have = report["downloaded"] + report["skipped"]
    if not have:
        raise RuntimeError(
            f"{label}: nothing could be downloaded from “{report['folder']}”. "
            + (f"First error: {report['errors'][0]}" if report["errors"]
               else f"The folder has no {kind} files in it.")
        )
    if report["failed"]:
        # Not fatal — a batch built from 98 of 100 clips is still the batch you
        # asked for — but it must not pass unnoticed either.
        logger.warning("Job %s: %s — %d of %d clips failed to download (%s)",
                       job_id, label, report["failed"], report["files"],
                       report["errors"][0])
    logger.info("Job %s: %s — %d clips from Drive (%d new, %d already here)",
                job_id, label, have, report["downloaded"], report["skipped"])
    return report


def _drive_clips_stage(job: dict, params: dict) -> dict:
    """Clips from a Google Drive folder, in one of two layouts.

    *per_slot* mirrors the sidebar uploaders exactly — one folder per clip
    position, so which clip plays where stays yours to decide. *pooled* takes a
    single folder and deals it across the slots, which is the unattended
    version of the same thing."""
    job_id = job["id"]
    assets = store.assets_dir(job_id)
    slots = int(params.get("slots") or config.SCRAPE_SLOTS)
    store.set_stage(job_id, "downloading clips from Drive")

    if params.get("drive_clip_layout") == "per_slot":
        links = [str(v or "").strip() for v in (params.get("clips_drive_folders") or [])]
        if not any(links):
            raise RuntimeError(
                "No Drive folder links were given for the clip slots. Paste at "
                "least one, or choose a different clip source.")

        if len(links) > slots:
            # Shouldn't happen — the UI builds one box per slot — but a dropped
            # link is exactly the kind of thing that must never be silent.
            logger.warning("Job %s: %d Drive links given for %d clip slots — "
                           "links %d and up are ignored",
                           job_id, len(links), slots, slots + 1)

        # Every slot gets its folder even when its link is blank: the slot count
        # has to survive the round trip through the folders, or the per-slot
        # playback speeds shift onto the wrong clips.
        for i in range(1, slots + 1):
            (assets / f"cta_slot_{i}").mkdir(parents=True, exist_ok=True)

        per_slot: dict[int, int] = {}
        reports = []
        for i, link in enumerate(links[:slots], start=1):
            if not link:
                continue
            report = _download_drive_folder(
                job_id, link, assets / f"cta_slot_{i}", f"clip {i}")
            per_slot[i] = report["downloaded"] + report["skipped"]
            reports.append(report)
        return {
            "source": "drive_folder",
            "layout": "per_slot",
            "slots": slots,
            "clips_used": sum(per_slot.values()),
            "downloaded": sum(r["downloaded"] for r in reports),
            "already_present": sum(r["skipped"] for r in reports),
            "download_failed": sum(r["failed"] for r in reports),
            "per_slot": {str(s): n for s, n in sorted(per_slot.items())},
        }

    # Pooled: one folder, dealt across the slots by the existing selection code.
    report = _download_drive_folder(
        job_id, params.get("clips_drive_folder"),
        store.job_dir(job_id) / "clips", "clips")
    # `all`, not the scrape default: a Drive folder is a set someone already
    # curated, and there are no view counts here to rank by — so a top-N cut
    # would drop clips for no reason anyone could see.
    stage = _clips_stage(job, {**params, "clip_strategy": "all"})
    return {
        "source": "drive_folder",
        "layout": "pooled",
        "drive_folder": report["folder"],
        "downloaded": report["downloaded"],
        "already_present": report["skipped"],
        "download_failed": report["failed"],
        **stage,
    }


def _drive_pool_stage(job: dict, params: dict, param_key: str,
                      subdir: str, label: str, kind: str = "video") -> dict:
    """A FLAT clip pool from a Google Drive folder (the gifs, the background
    videos — anything without slots).

    Simpler than _drive_clips_stage because there are no slots to deal across:
    the folder is downloaded straight into `assets/<subdir>/`, which is exactly
    where workspace_from_dir looks for it. No selection step either — every
    clip in the folder is in the pool, and which ones a given video uses is
    decided per row at render time."""
    job_id = job["id"]
    dest = store.assets_dir(job_id) / subdir
    dest.mkdir(parents=True, exist_ok=True)
    store.set_stage(job_id, f"downloading {label} from Drive")
    report = _download_drive_folder(job_id, params.get(param_key), dest, label,
                                   kind=kind)
    return {
        "source": "drive_folder",
        "drive_folder": report["folder"],
        "downloaded": report["downloaded"],
        "already_present": report["skipped"],
        "download_failed": report["failed"],
    }


def _captions_stage(job: dict, params: dict) -> dict:
    """Generate a pool first, when asked. Reuses the existing pool builder."""
    job_id = job["id"]
    theme = (params.get("caption_theme") or "").strip()
    if not theme:
        # Silently skipping here meant "Generate a fresh pool" appeared to do
        # nothing and the old pool was used instead — with no way to tell.
        raise RuntimeError(
            "Caption generation was requested but no theme was given. The theme "
            "is what the captions are about, so there is nothing to generate "
            "from. Either set one or choose “Use the active caption pool”.")

    existing = store.active_pool()
    if existing and not params.get("force_new_pool"):
        return {"skipped": "reused the active pool", "pool_id": existing["id"]}

    from captions import pool as pool_module

    store.set_stage(job_id, "generating captions")

    def progress(done: int, total: int) -> None:
        store.heartbeat(job_id, stage=f"captions {done}/{total}")

    # Only generate hashtag sets if the pool is actually the hashtag source —
    # a fixed CTA line replaces them, so generating any is money for nothing.
    wants_pool_hashtags = (params.get("hashtag_source", "pool") == "pool"
                           and not params.get("fixed_tail"))
    return pool_module.build_pool(
        theme=theme,
        caption_count=int(params.get("caption_count") or config.CAPTION_POOL_SIZE),
        hashtag_count=(int(params.get("hashtag_count") or config.HASHTAG_POOL_SIZE)
                       if wants_pool_hashtags else 0),
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
    elif params.get("clip_source") == "drive_folder":
        result["clips"] = _drive_clips_stage(job, params)

    # ---- 1b. gifs (independent of where the CTA clips came from)
    if params.get("gif_source") == "drive_folder":
        result["gifs"] = _drive_pool_stage(
            job, params, "gifs_drive_folder", "gifs", "GIFs")

    # ---- 1c. background videos (independent of both pools above)
    if params.get("bg_video_source") == "drive_folder":
        result["bg_videos"] = _drive_pool_stage(
            job, params, "bg_videos_drive_folder", "bg_videos",
            "background videos")

    # ---- 1d. music (independent of every pool above)
    if params.get("music_source") == "drive_folder":
        result["music"] = _drive_pool_stage(
            job, params, "music_drive_folder", "music", "music", kind="audio")

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
