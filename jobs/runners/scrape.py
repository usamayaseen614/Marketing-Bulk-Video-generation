"""
jobs/runners/scrape.py — the TikTok scrape job.

Batch mode and dump mode share one pipeline; only the Drive folder layout at
the end differs:

    batch:  <root>/scrapes/<account>/batch_01/slot_1/…  + metadata.xlsx
    dump:   <root>/scrapes/<account>/dump_<date>/…      + metadata.xlsx

Rate limiting is the binding constraint, not bandwidth: ~1 clip per 1-2s. Going
faster gets the IP blocked, which costs far more than the time it saves.

Per-clip state lives in job_items, so a scrape killed at clip 900 of 1,500
resumes rather than starting over — the same machinery the render job uses.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

import pandas as pd

import config
from jobs import store
from scrapers import tiktok

logger = logging.getLogger(__name__)


def _sleep_politely() -> None:
    time.sleep(random.uniform(config.SCRAPE_MIN_DELAY, config.SCRAPE_MAX_DELAY))


def _metadata_frame(clips: dict[str, tiktok.ClipInfo],
                    plan: list[tiktok.PlannedClip],
                    items_by_id: dict[str, dict],
                    owner_of: Optional[dict] = None) -> pd.DataFrame:
    """One row per clip.

    This exists so curating 500 clips is a matter of sorting by views and
    skimming the top 100, rather than scrubbing thumbnails for an afternoon."""
    placement: dict[str, list[str]] = {}
    for entry in plan:
        where = ("dump" if entry.batch == 0
                 else f"batch_{entry.batch:02d}/slot_{entry.slot}")
        # entry.video_id is a segment filename; roll it up to its source clip.
        owner = (owner_of or {}).get(entry.video_id, entry.video_id)
        placement.setdefault(owner, []).append(where)

    rows = []
    for video_id, clip in clips.items():
        item = items_by_id.get(video_id) or {}
        meta = item.get("meta") or {}
        rows.append({
            "Video_ID": video_id,
            "Views": clip.view_count,
            "Likes": clip.like_count,
            "Comments": clip.comment_count,
            "Reposts": clip.repost_count,
            "Original_Duration_s": clip.duration,
            "Trimmed_Duration_s": meta.get("trimmed_duration"),
            "Posted": clip.posted_date(),
            "Title": clip.title,
            "URL": clip.url,
            "File": item.get("name") or "",
            "Segments": len((item.get("meta") or {}).get("segments") or []) or 1,
            "Placement": ", ".join(placement.get(video_id, [])),
            "Status": item.get("render_status") or "pending",
            "Error": item.get("render_error") or "",
        })

    frame = pd.DataFrame(rows)
    if not frame.empty:
        # Most useful default: highest-performing clips first.
        frame = frame.sort_values("Views", ascending=False, na_position="last")
    return frame


def _package_clips(job_id: str, plan: list[tiktok.PlannedClip],
                   clips_dir: Path,
                   sheet_path: Path, mode: str) -> Optional[Path]:
    """ZIP the clips in their batch/slot layout.

    Without Drive configured this is the ONLY way to get the clips off the
    machine — they would otherwise sit in the job folder with nothing in the UI
    pointing at them. The folder structure is preserved rather than flattened
    because that structure is the point: you drag slot_1..slot_5 straight into
    the generator's sidebar instead of hand-sorting loose files."""
    import zipfile

    zip_path = store.job_dir(job_id) / "clips.zip"
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for entry in plan:
            # entry.video_id is a segment FILENAME here.
            name = entry.video_id
            src = clips_dir / name
            if not src.is_file():
                continue
            if mode == "dump":
                arc = f"{entry.position:02d}_{name}"
            else:
                arc = (f"batch_{entry.batch:02d}/slot_{entry.slot}/"
                       f"{entry.position:02d}_{name}")
            zf.write(src, arc)
            written += 1
        if sheet_path.is_file():
            zf.write(sheet_path, sheet_path.name)

    if not written:
        zip_path.unlink(missing_ok=True)
        return None
    logger.info("Job %s: packaged %d clip(s) into %s", job_id, written, zip_path.name)
    return zip_path


def run(job: dict) -> dict:
    job_id = job["id"]
    params = job.get("params") or {}
    started = time.time()

    account_input = params.get("account") or ""
    account = tiktok.account_name(account_input)
    mode = params.get("mode", "batch")
    n_batches = int(params.get("batches") or 1)
    limit = int(params.get("limit") or config.SCRAPE_MAX_VIDEOS)
    trim_start = float(params.get("trim_start", config.SCRAPE_TRIM_START))
    trim_duration = float(params.get("trim_duration", config.SCRAPE_TRIM_DURATION))
    skip_known = bool(params.get("skip_known", True))

    work = store.work_dir(job_id)
    clips_dir = store.job_dir(job_id) / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = work / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    source = tiktok.YtDlpSource()

    # ---- 1. enumerate
    store.set_stage(job_id, "enumerating")
    found = source.enumerate_clips(account_input, limit)

    # A profile advertising "1000 posts" may hold far fewer real videos, so the
    # true count is reported rather than silently coming up short.
    likely_photos = [c for c in found if c.likely_photo]
    candidates = [c for c in found if not c.likely_photo]

    known = store.known_clip_ids(account) if skip_known else set()
    duplicates = [c for c in candidates if c.video_id in known]
    fresh = [c for c in candidates if c.video_id not in known]

    logger.info(
        "Job %s: %s -> %d posts, %d likely photos, %d already known, %d to fetch",
        job_id, account, len(found), len(likely_photos), len(duplicates), len(fresh))

    if not fresh:
        return {
            "account": account, "videos_found": len(candidates),
            "photos_skipped": len(likely_photos), "duplicates": len(duplicates),
            "downloaded": 0, "trimmed": 0, "batches": 0,
            "elapsed": time.time() - started,
            "note": ("Nothing new — every video on this profile has been "
                     "scraped before. Untick “Skip clips already scraped” to "
                     "pull them again."),
        }

    clip_by_id = {c.video_id: c for c in fresh}
    store.add_items(job_id, [
        {"idx": i, "name": "", "stage": store.STAGE_SCRAPE,
         "meta": {"video_id": c.video_id, "url": c.url}}
        for i, c in enumerate(fresh, start=1)
    ])
    idx_by_id = {c.video_id: i for i, c in enumerate(fresh, start=1)}

    # ---- 2. download + trim, one at a time, politely
    store.set_stage(job_id, "downloading")
    ffmpeg = tiktok.find_ffmpeg()
    seen_hashes = store.known_content_hashes(account) if skip_known else set()

    downloaded = trimmed = skipped = 0
    pending = store.pending_render_items(job_id, stage=store.STAGE_SCRAPE)
    logger.info("Job %s: %d clip(s) to fetch (%d already done)",
                job_id, len(pending), len(fresh) - len(pending))

    for n, item in enumerate(pending, start=1):
        video_id = (item.get("meta") or {}).get("video_id")
        clip = clip_by_id.get(video_id)
        if clip is None:
            store.update_item(job_id, item["idx"], render_status=store.ITEM_FAILED,
                              render_error="Clip vanished from the enumeration")
            continue

        try:
            raw = source.download(clip, raw_dir)
            downloaded += 1

            digest = tiktok.content_hash(raw)
            if digest in seen_hashes:
                # Same clip reposted under a new id.
                raw.unlink(missing_ok=True)
                store.update_item(job_id, item["idx"],
                                  render_status=store.ITEM_SKIPPED,
                                  render_error="Duplicate content (already have this clip)")
                skipped += 1
                _sleep_politely()
                continue
            seen_hashes.add(digest)

            # One download can yield several clips: a 40s video with a 10s
            # window becomes four, instead of throwing 30 seconds away.
            segments = tiktok.split_clip(
                raw, clips_dir, clip.video_id, trim_start, trim_duration, ffmpeg)
            raw.unlink(missing_ok=True)
            trimmed += len(segments)

            store.update_item(
                job_id, item["idx"], name=segments[0].name,
                render_status=store.ITEM_DONE, render_error=None,
                meta={**(item.get("meta") or {}),
                      "content_hash": digest,
                      "segments": [p.name for p in segments],
                      "trimmed_duration": tiktok.probe_duration(segments[0], ffmpeg)},
            )
            if len(segments) > 1:
                logger.info("Job %s: %s -> %d segments",
                            job_id, clip.video_id, len(segments))
        except Exception as exc:  # noqa: BLE001 — photo posts land here, by design
            skipped += 1
            message = str(exc)
            if "rehydration" in message or "Unable to extract" in message:
                message = "Not a downloadable video (photo carousel or removed post)"
            store.update_item(job_id, item["idx"], render_status=store.ITEM_FAILED,
                              render_error=message[:400])
            logger.info("Job %s: skipped %s — %s", job_id, video_id, message[:120])

        store.heartbeat(job_id, stage=f"downloading {n}/{len(pending)}")
        _sleep_politely()

    # ---- 3. remember what we have, so next month only pulls what's new
    items = store.list_items(job_id, stage=store.STAGE_SCRAPE)
    items_by_id = {(i.get("meta") or {}).get("video_id"): i for i in items}
    good = [i for i in items if i["render_status"] == store.ITEM_DONE]
    store.remember_clips(
        account,
        [{"video_id": (i.get("meta") or {}).get("video_id"),
          "content_hash": (i.get("meta") or {}).get("content_hash"),
          "duration": (i.get("meta") or {}).get("trimmed_duration")}
         for i in good],
        job_id=job_id,
    )

    # Every produced segment is its own clip. Planning over source video ids
    # would place only the first segment and silently strand the rest.
    segment_files: list[str] = []
    owner_of: dict[str, str] = {}
    for i in good:
        meta = i.get("meta") or {}
        vid = meta.get("video_id")
        for name in (meta.get("segments") or ([i["name"]] if i.get("name") else [])):
            segment_files.append(name)
            owner_of[name] = vid

    # ---- 4. plan the layout
    if mode == "dump":
        plan = tiktok.plan_dump(segment_files)
    else:
        plan = tiktok.plan_batches(segment_files, n_batches)

    # ---- 5. metadata sheet
    store.set_stage(job_id, "building metadata")
    sheet_path = store.job_dir(job_id) / "metadata.xlsx"
    frame = _metadata_frame({c.video_id: c for c in fresh}, plan,
                            items_by_id, owner_of)
    frame.to_excel(sheet_path, index=False, engine="openpyxl")

    # ---- 6. package, so the clips are reachable even without Drive
    #
    # Skipped when this scrape is a pipeline's internal stage: that path reads
    # the clips straight off disk and its result lands nested under
    # result["scrape"], while the Jobs page only ever reads the top-level
    # zip_path. So this was writing a full uncompressed second copy of clips/
    # that no code path could reach — mirrors the condition on _upload below.
    store.set_stage(job_id, "packaging")
    zip_path = (None if params.get("upload") is False
                else _package_clips(job_id, plan, clips_dir, sheet_path, mode))

    # ---- 7. upload
    drive_result = ({} if params.get("upload") is False else
                    _upload(job, account, mode, plan, items_by_id,
                            clips_dir, sheet_path))

    elapsed = time.time() - started
    batches_built = len({p.batch for p in plan if p.batch}) if mode != "dump" else 0
    failures = [f"{(i.get('meta') or {}).get('video_id')}: {i['render_error']}"
                for i in items if i["render_status"] == store.ITEM_FAILED][:25]

    result = {
        "account": account,
        "videos_found": len(candidates),
        "photos_skipped": len(likely_photos) + skipped,
        "duplicates": len(duplicates),
        "downloaded": downloaded,
        "trimmed": trimmed,
        "batches": batches_built,
        "clips_dir": str(clips_dir),
        "sheet_path": str(sheet_path),
        "zip_path": str(zip_path) if zip_path else None,
        "elapsed": elapsed,
        "failures": failures,
    }
    result.update(drive_result)
    logger.info("Job %s scrape done: %d trimmed from %d found in %.0fs",
                job_id, trimmed, len(candidates), elapsed)
    return result


def _upload(job: dict, account: str, mode: str, plan: list[tiktok.PlannedClip],
            items_by_id: dict, clips_dir: Path, sheet_path: Path) -> dict:
    """Push clips into Drive, pre-split into the slot folders the sidebar
    uploaders expect — the user drags 5 folders in instead of hand-sorting 50
    loose files."""
    job_id = job["id"]
    destination = (job.get("params") or {}).get("drive_folder") or ""
    if not config.drive_configured(destination):
        logger.info("Job %s: Drive not configured — clips stay on the VM", job_id)
        return {}

    from integrations import drive

    # A job may carry its own destination; blank falls back to the env default.
    drive.set_target(destination)

    try:
        root_id = drive.ensure_path(["scrapes", account])
        link = drive.folder_link(root_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s: could not prepare the Drive folder", job_id)
        return {"drive_error": str(exc)}

    store.set_stage(job_id, "uploading")

    # Group by destination so each folder is created once, then upload each
    # group in parallel.
    groups: dict[tuple, list[tiktok.PlannedClip]] = {}
    if mode == "dump":
        folder = f"dump_{time.strftime('%Y-%m-%d')}"
        for entry in plan:
            groups.setdefault((folder,), []).append(entry)
    else:
        for entry in plan:
            groups.setdefault(
                (f"batch_{entry.batch:02d}", f"slot_{entry.slot}"), []).append(entry)

    uploaded = failed = 0
    for parts, entries in sorted(groups.items()):
        try:
            folder_id = drive.ensure_path(list(parts), parent_id=root_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create %s: %s", "/".join(parts), exc)
            failed += len(entries)
            continue

        queue = []
        for entry in entries:
            # entry.video_id is a segment FILENAME here.
            name = entry.video_id
            src = clips_dir / name
            if src.is_file():
                # Position prefix keeps slot folders in play order, and makes
                # the same clip appearing in two batches distinguishable.
                queue.append((src, f"{entry.position:02d}_{name}"))

        ok, bad = drive.upload_many(queue, folder_id)
        uploaded += ok
        failed += bad
        store.heartbeat(job_id, stage=f"uploading {uploaded}/{len(plan)}")

    try:
        drive.upload_file(sheet_path, root_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not upload the metadata sheet", exc_info=True)

    logger.info("Job %s: uploaded %d clip file(s), %d failed", job_id, uploaded, failed)
    return {"drive_link": link, "uploaded": uploaded, "upload_failed": failed}
