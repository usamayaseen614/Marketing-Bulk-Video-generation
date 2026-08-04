"""
jobs/runners/render.py — the batch render job.

This is the logic that used to live in app.py's run_batch(), moved out from
under the Streamlit script run so the browser tab can be closed the moment
Generate is clicked.

## What a render produces

One sheet of N rows becomes `batches x N` videos. The same rows are rendered
once per batch, each pass using a different promo video and a different
`variant_salt`, so every pass picks different ASMR clips. The finished videos
are then **mixed** across the output folders (see batching.py) so no folder is
just one promo video, and each is uploaded to Drive **twice** — once under a
short name capped at 100 characters with a single hashtag, once under a longer
name carrying every hashtag.

The second copy is made with Drive's server-side `files.copy`, so the bytes
cross the network once. At 10,000 videos that is the difference between ~80 GB
and ~160 GB of upload.

## Resume

Rendering is driven by the per-item rows in the job store rather than by
iterating the DataFrame:

  * an item still marked `pending` is rendered
  * an item marked `done` is skipped — its MP4 is already on disk
  * an item marked `failed` is left alone; render failures are deterministic
    (a background missing from the ZIP won't appear on a retry)

So a worker that dies at video 6,000 of 10,000 resumes at 6,000. Background
assignment, the caption draw, the mix and both filenames are all derived
deterministically, so a resumed run reproduces exactly the layout it started
with instead of scattering files that were already uploaded.
"""

from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

import batching
import config
import results
from batching import Slot
from jobs import store
from video_generator import RenderConfig, VideoGenerator
from workspace import workspace_from_dir

logger = logging.getLogger(__name__)

# At 10,000 items a heartbeat per completed row would mean 10,000 extra SQLite
# connections for no benefit — the staleness window is minutes, not seconds.
_HEARTBEAT_EVERY = 5.0


def _load_dataframe(assets: Path) -> pd.DataFrame:
    """Read the sheet staged at submit time — already carrying any edits saved
    from the preview editor, so the worker renders exactly what the UI showed."""
    sheet = assets / "input.xlsx"
    if not sheet.is_file():
        # Staged assets are deleted once a job finishes, so this means someone
        # requeued a completed job rather than a genuine crash-resume (where
        # cleanup never ran).
        raise FileNotFoundError(
            f"The uploaded sheet is gone from {assets}. A finished job's "
            "assets are cleaned up, so this batch cannot be re-run — submit it "
            "again from the Generate page. Its rendered videos are unaffected."
        )
    df = pd.read_excel(sheet, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    return df


# --------------------------------------------------------------------------- captions

def _assign_names(job_id: str, slots: list[Slot], n_rows: int,
                  df: pd.DataFrame) -> dict:
    """Give every (batch, row) its own caption, hashtags and two filenames.

    A caption identifies one *video*, not one sheet row — ten batches of the
    same row are ten separate posts and must not share a caption. So the draw
    is per item, and the pool's no-repeat guarantee covers all
    `batches x rows` of them.

    Precedence, highest first:
      1. a `Caption` the user typed into the sheet — hand-written always wins,
         and it is reused across batches because the user asked for that text
      2. a pair drawn from the active caption pool
      3. the row's `Headline`, which is how files were named before captions
         existed — without this fallback a batch run with no pool would name
         every single video `video.mp4`
    """
    from captions import naming
    from video_generator import _clean_str

    existing = {i["idx"]: i for i in store.list_items(job_id)}
    needed = [s for s in slots
              if not (existing.get(batching.item_index(s.batch, s.row, n_rows), {})
                      .get("meta", {}).get("short_name"))]
    if not needed:
        return {"applied": 0, "reason": "Names already assigned (resumed job)."}

    def _cell(row_no: int, column: str) -> str:
        if column not in df.columns:
            return ""
        return _clean_str(df.iloc[row_no - 1].get(column))

    # Only rows without a hand-written caption consume pool combinations —
    # drawing for all of them would burn the pool on captions never used.
    from_sheet = {s: _cell(s.row, "Caption") for s in needed}
    need_pool = [s for s in needed if not from_sheet[s]]

    pool = store.active_pool()
    drawn: dict = {}
    if pool and need_pool:
        for slot, pair in zip(need_pool, store.take_combinations(pool["id"], len(need_pool))):
            drawn[slot] = pair

    rows = []
    used_sheet = used_pool = used_headline = 0
    for slot in needed:
        caption = from_sheet[slot]
        if caption:
            hashtags = _cell(slot.row, "Hashtags")
            used_sheet += 1
        elif slot in drawn:
            caption, hashtags = drawn[slot]
            used_pool += 1
        else:
            # No pool: fall back to the pre-caption naming source.
            caption, hashtags = _cell(slot.row, "Headline"), ""
            used_headline += 1
        rows.append((slot, caption, hashtags))

    # De-duplicate names across the WHOLE render, not per batch — two batches
    # drawing similar captions would otherwise collide inside a mixed folder.
    name_pairs = naming.names_for_rows([(c, h) for _s, c, h in rows])

    for (slot, caption, hashtags), (short, long_name) in zip(rows, name_pairs):
        idx = batching.item_index(slot.batch, slot.row, n_rows)
        meta = dict((existing.get(idx) or {}).get("meta") or {})
        meta.update({
            "batch": slot.batch, "row": slot.row,
            "caption": caption, "hashtags": hashtags,
            "short_name": short, "long_name": long_name,
        })
        store.update_item(job_id, idx, name=short, meta=meta)

    return {
        "applied": len(needed),
        "from_sheet": used_sheet,
        "from_pool": used_pool,
        "from_headline": used_headline,
        "pool_id": pool["id"] if pool else None,
        "theme": pool.get("theme") if pool else None,
        "reason": None if pool else
                  "No caption pool — names fall back to the sheet's Headline.",
    }


# --------------------------------------------------------------------------- render

def _render_batches(job: dict, df: pd.DataFrame, ws, n_batches: int,
                    n_rows: int, workers: int) -> list[str]:
    """Render every outstanding item, one batch at a time."""
    job_id = job["id"]
    params = job.get("params") or {}
    base_config = params.get("render_config") or {}
    batch_warnings: list[str] = []

    pending = store.pending_render_items(job_id)
    if not pending:
        logger.info("Job %s: nothing left to render", job_id)
        return batch_warnings

    by_batch: dict[int, list[dict]] = {}
    for item in pending:
        batch, _row = batching.split_index(item["idx"], n_rows)
        by_batch.setdefault(batch, []).append(item)

    total_pending = len(pending)
    done = 0
    last_beat = 0.0

    for batch in sorted(by_batch):
        items = by_batch[batch]
        promo = ws.promo_for_batch(batch - 1)

        # variant_salt is what makes this pass differ from the others: the same
        # row picks different sample clips in each batch.
        cfg = RenderConfig(**base_config)
        cfg.variant_salt = batch
        cfg.font_path = str(ws.font_path) if ws.font_path else None

        generator = VideoGenerator(
            config=cfg,
            bg_dir=ws.bg_dir,
            video_path=promo,
            cta_path=ws.cta_path,
            work_dir=ws.work_dir / f"b{batch:02d}",
            output_dir=store.videos_dir(job_id) / batching.source_folder_name(batch),
            cta_video_slots=ws.cta_video_slots,
        )
        for message in generator.input_warnings:
            if message not in batch_warnings:
                batch_warnings.append(message)

        df_run, bg_warnings = generator.assign_backgrounds(df)
        for message in bg_warnings:
            if message not in batch_warnings:
                batch_warnings.append(message)

        logger.info("Job %s: batch %d/%d — %d row(s) using promo %s",
                    job_id, batch, n_batches, len(items), promo.name)
        store.set_stage(job_id, f"rendering batch {batch}/{n_batches}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for item in items:
                _b, row_no = batching.split_index(item["idx"], n_rows)
                row = df_run.iloc[row_no - 1]
                name = (item.get("meta") or {}).get("short_name") or None
                futures[pool.submit(generator.render_row, row_no, row, name)] = item

            for future in as_completed(futures):
                item = futures[future]
                try:
                    res = future.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Job %s item %s raised", job_id, item["idx"])
                    store.update_item(job_id, item["idx"],
                                      render_status=store.ITEM_FAILED,
                                      render_error=str(exc), render_attempts=1)
                else:
                    store.update_item(
                        job_id, item["idx"],
                        name=res.filename or "",
                        render_status=store.ITEM_DONE if res.ok else store.ITEM_FAILED,
                        render_error=res.error,
                        render_attempts=1,
                        warnings=list(res.warnings or []),
                        **({} if res.ok else {"upload_status": store.ITEM_SKIPPED}),
                    )
                done += 1
                now = time.time()
                if now - last_beat > _HEARTBEAT_EVERY:
                    last_beat = now
                    store.heartbeat(
                        job_id,
                        stage=f"rendering batch {batch}/{n_batches} "
                              f"({done}/{total_pending})")

    return batch_warnings


# --------------------------------------------------------------------------- upload

def _upload(job: dict, n_rows: int, n_folders: int, placement: dict) -> dict:
    """Upload each video twice — short name uploaded, long name server-copied."""
    job_id = job["id"]
    if not config.drive_configured():
        logger.info("Job %s: Drive not configured — videos stay on the VM", job_id)
        return {}

    from integrations import drive

    try:
        label = job.get("label") or job_id
        root = drive.ensure_path(["renders", time.strftime("%Y-%m-%d"), label])
        link = drive.folder_link(root)
        folder_ids = {
            f: drive.ensure_path([batching.folder_name(f)], parent_id=root)
            for f in range(1, n_folders + 1)
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s: could not prepare Drive folders", job_id)
        return {"drive_error": str(exc)}

    store.set_stage(job_id, "uploading")
    videos_root = store.videos_dir(job_id)
    pending = store.pending_upload_items(job_id)
    logger.info("Job %s: %d file(s) to upload", job_id, len(pending))

    def _one(item: dict) -> bool:
        """Upload one video and record the outcome **from inside the worker
        thread**.

        Persisting here rather than in the consuming loop is the whole point.
        Drive state has to be written the instant it becomes true: if it were
        collected and written by the caller, a single slow upload would hold
        back the database rows for every file that finished behind it, and a
        crash at that moment would leave dozens of videos already in Drive but
        still marked pending — so the resumed run would upload them all again,
        silently doubling them. store opens its own connection per call, so
        writing from a worker thread is safe."""
        meta = dict(item.get("meta") or {})
        batch, row_no = batching.split_index(item["idx"], n_rows)
        src = videos_root / batching.source_folder_name(batch) / (item.get("name") or "")

        store.bump_item_attempts(job_id, item["idx"], "upload_attempts")
        if not src.is_file():
            store.update_item(job_id, item["idx"], upload_status=store.ITEM_FAILED,
                              upload_error="Rendered file missing on disk")
            return False

        folder = placement.get(Slot(batch=batch, row=row_no), 1)
        parent = folder_ids.get(folder) or root
        short = meta.get("short_name") or src.name
        long_name = meta.get("long_name") or short

        try:
            file_id = meta.get("drive_file_id")
            if not file_id:
                uploaded = drive.upload_file(src, parent, short)
                file_id = uploaded["id"]
                # Recorded BEFORE the copy: if the copy fails or the process
                # dies between the two calls, the resume knows the bytes are
                # already in Drive and only needs to make the copy. This is
                # what makes the `drive_file_id and not copy_file_id` branch
                # reachable instead of dead.
                meta["drive_file_id"] = file_id
                store.update_item(job_id, item["idx"], drive_file_id=file_id,
                                  meta=meta)

            copied = drive.copy_file(file_id, long_name, parent)
            meta["copy_file_id"] = copied.get("id")
            store.update_item(
                job_id, item["idx"], upload_status=store.ITEM_DONE,
                upload_error=None, drive_file_id=file_id,
                drive_link=copied.get("webViewLink"), meta=meta)
            return True
        except Exception as exc:  # noqa: BLE001 — reported per file
            logger.warning("Job %s: upload failed for item %s: %s",
                           job_id, item["idx"], exc)
            store.update_item(job_id, item["idx"],
                              upload_status=store.ITEM_FAILED,
                              upload_error=str(exc)[:500], meta=meta)
            return False

    uploaded_ok = failed = 0
    last_beat = 0.0
    with ThreadPoolExecutor(max_workers=config.DRIVE_UPLOAD_CONCURRENCY) as pool:
        # as_completed, not map: map yields in submission order, so one slow
        # upload would stall progress reporting behind it.
        futures = [pool.submit(_one, item) for item in pending]
        for future in as_completed(futures):
            if future.result():
                uploaded_ok += 1
            else:
                failed += 1
            now = time.time()
            if now - last_beat > _HEARTBEAT_EVERY:
                last_beat = now
                store.heartbeat(
                    job_id,
                    stage=f"uploading {uploaded_ok + failed}/{len(pending)}")

    counts = store.item_counts(job_id)
    upload_failures = [
        f"Item {i['idx']} ({i['name']}): {i['upload_error']}"
        for i in store.list_items(job_id)
        if i["upload_status"] == store.ITEM_FAILED
    ][:25]

    logger.info("Job %s: uploaded %d, failed %d (each as 2 Drive files)",
                job_id, uploaded_ok, failed)
    return {
        "drive_link": link,
        "drive_folder_id": root,
        "uploaded": counts["uploaded"],
        "drive_files": counts["uploaded"] * 2,
        "upload_failed": counts["upload_failed"],
        "upload_failures": upload_failures,
    }


# --------------------------------------------------------------------------- manifests

def _write_manifests(job_id: str, n_rows: int, n_folders: int,
                     placement: dict) -> Path:
    """One sheet per output folder listing what actually landed in it.

    After mixing, the input sheet no longer describes any single folder, so the
    useful artifact is a per-folder manifest: what to post, with which caption,
    under which filename."""
    root = store.job_dir(job_id)
    items = {i["idx"]: i for i in store.list_items(job_id)}

    rows_by_folder: dict[int, list[dict]] = {}
    for idx, item in items.items():
        if item["render_status"] != store.ITEM_DONE:
            continue
        batch, row_no = batching.split_index(idx, n_rows)
        folder = placement.get(Slot(batch=batch, row=row_no), 1)
        meta = item.get("meta") or {}
        rows_by_folder.setdefault(folder, []).append({
            "Folder": batching.folder_name(folder),
            "Source_Batch": batch,
            "Sheet_Row": row_no,
            "Caption": meta.get("caption", ""),
            "Hashtags": meta.get("hashtags", ""),
            "Short_Filename": meta.get("short_name", item.get("name", "")),
            "Long_Filename": meta.get("long_name", ""),
            "Drive_Uploaded": item.get("upload_status") == store.ITEM_DONE,
        })

    combined: list[dict] = []
    for folder in sorted(rows_by_folder):
        entries = sorted(rows_by_folder[folder],
                         key=lambda r: (r["Source_Batch"], r["Sheet_Row"]))
        combined.extend(entries)
        out = root / f"{batching.folder_name(folder)}_manifest.xlsx"
        pd.DataFrame(entries).to_excel(out, index=False, engine="openpyxl")

    manifest = root / "render_manifest.xlsx"
    pd.DataFrame(combined).to_excel(manifest, index=False, engine="openpyxl")
    return manifest


# --------------------------------------------------------------------------- entry

def run(job: dict) -> dict:
    """Render, mix and publish a submitted batch."""
    job_id = job["id"]
    params = job.get("params") or {}
    started = time.time()

    assets = store.assets_dir(job_id)
    work = store.work_dir(job_id)
    videos = store.videos_dir(job_id)
    videos.mkdir(parents=True, exist_ok=True)

    store.set_stage(job_id, "preparing")

    df = _load_dataframe(assets)
    n_rows = len(df)
    n_batches = max(1, int(params.get("batches") or 1))
    n_folders = max(1, int(params.get("folders") or n_batches))
    workers = max(1, int(params.get("workers") or 2))

    ws = workspace_from_dir(assets, work)
    slots = batching.plan_render(n_batches, n_rows)

    # Register one item per (batch, row) — this is what makes resume work.
    store.add_items(job_id, [
        {"idx": batching.item_index(s.batch, s.row, n_rows), "name": "",
         "meta": {"batch": s.batch, "row": s.row}}
        for s in slots
    ])

    caption_info = _assign_names(job_id, slots, n_rows, df)
    logger.info("Job %s: %d rows x %d batches = %d videos; names assigned: %s",
                job_id, n_rows, n_batches, len(slots), caption_info.get("applied"))

    # The sheet keeps its Caption/Hashtags columns for the first batch, so it
    # still opens as a recognisable version of what was submitted.
    batch_warnings = _render_batches(job, df, ws, n_batches, n_rows, workers)

    placement = batching.mix_into_folders(slots, n_folders)

    # Upload BEFORE writing the manifests and the log. Both report per-file
    # Drive state (`Drive_Uploaded`, "drive upload failed: …"), and that state
    # only exists once _upload has run — writing them first made every row say
    # "not uploaded" no matter what actually happened.
    sheet_out = store.job_dir(job_id) / "batch_sheet.xlsx"
    src_sheet = assets / "input.xlsx"
    if src_sheet.is_file() and not sheet_out.is_file():
        shutil.copy2(src_sheet, sheet_out)

    drive_result = _upload(job, n_rows, n_folders, placement)

    store.set_stage(job_id, "building manifests")
    manifest = _write_manifests(job_id, n_rows, n_folders, placement)

    items = store.list_items(job_id)
    records = [results.record_from_item(i) for i in items]
    log_path = store.job_dir(job_id) / "render_log.txt"
    results.write_render_log(log_path, records, elapsed=time.time() - started,
                             extra_warnings=batch_warnings)

    counts = store.item_counts(job_id)
    rendered, failed = counts["rendered"], counts["render_failed"]

    zip_path = None
    if params.get("make_zip", True) and rendered:
        store.set_stage(job_id, "packaging")
        zip_path = results.package_zip(
            store.job_dir(job_id) / "marketing_videos.zip",
            videos, records, log_path=log_path,
            extra_files=[p for p in (manifest, sheet_out) if p.is_file()],
        )

    if drive_result.get("drive_link"):
        try:
            from integrations import drive as drive_mod
            for extra in (manifest, log_path, sheet_out):
                if Path(extra).is_file():
                    drive_mod.upload_file(Path(extra), drive_result["drive_folder_id"])
        except Exception:  # noqa: BLE001
            logger.warning("Could not upload manifests to Drive", exc_info=True)

    elapsed = time.time() - started
    logger.info("Job %s finished: %d rendered, %d failed, %d uploaded in %.0fs",
                job_id, rendered, failed, drive_result.get("uploaded", 0), elapsed)

    result = {
        "total": counts["total"],
        "rendered": rendered,
        "failed": failed,
        "batches": n_batches,
        "folders": n_folders,
        "rows": n_rows,
        "promo_videos": len(ws.video_paths),
        "elapsed": elapsed,
        "batch_warnings": batch_warnings,
        "failures": results.failure_summary(records),
        "videos_dir": str(videos),
        "zip_path": str(zip_path) if zip_path else None,
        "sheet_path": str(manifest) if manifest.is_file() else None,
        "log_path": str(log_path),
        "captions": caption_info,
        "mix": batching.summarize(placement, n_folders),
    }
    result.update(drive_result)
    return result
