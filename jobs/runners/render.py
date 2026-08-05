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
short name carrying a single hashtag, once under a longer name carrying up to
five. Both are capped at 90 characters of name, with ".mp4" outside that count.

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
from datetime import datetime
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

def _hashtag_override(job_id: str, params: dict) -> Optional[list[str]]:
    """Hashtag sets from somewhere other than the caption pool.

    Three sources: the pool (default), an uploaded Excel whose first column is
    one hashtag set per row, or none at all. With none, a filename is just its
    caption — the naming code already treats the hashtag as optional, so the
    "exactly one #" rule simply becomes "at most one"."""
    source = params.get("hashtag_source", "pool")
    if source == "none":
        return []
    if source != "excel":
        return None                      # None means "use the pool's own"

    sheet = store.assets_dir(job_id) / "hashtags.xlsx"
    if not sheet.is_file():
        logger.warning("Job %s: hashtag Excel missing — falling back to the pool",
                       job_id)
        return None
    try:
        frame = pd.read_excel(sheet, engine="openpyxl")
    except Exception:  # noqa: BLE001
        logger.warning("Job %s: could not read %s", job_id, sheet, exc_info=True)
        return None

    from captions import naming

    values: list[str] = []
    for raw in frame.iloc[:, 0].tolist():
        clean = " ".join(naming.parse_hashtags(raw))
        if clean:
            values.append(clean)
    logger.info("Job %s: %d hashtag set(s) from the uploaded sheet",
                job_id, len(values))
    return values or []


def _assign_names(job_id: str, slots: list[Slot], n_rows: int,
                  df: pd.DataFrame, params: Optional[dict] = None) -> dict:
    """Give every (batch, row) its own caption, hashtags and two filenames.

    A caption identifies one *video*, not one sheet row — ten batches of the
    same row are ten separate posts and must not share a caption. So the draw
    is per item, and every pool caption in a job is different from every other:
    take_captions makes the caption the unit that cannot repeat, because a
    short filename is caption + one hashtag, so two videos handed the same
    caption collide on their name no matter how their hashtags differ.

    Precedence, highest first:
      1. a `Caption` the user typed into the sheet — hand-written always wins,
         and it is reused across batches because the user asked for that text.
         This is the one source that can still collide, and therefore the only
         place a ` (2)` suffix can now come from
      2. a caption drawn from the active caption pool — never repeated
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
        try:
            pairs = store.take_captions(pool["id"], len(need_pool))
        except store.PoolTooSmall as exc:
            # Before a single frame is rendered: a job that cannot name its
            # videos apart is not worth the hours of FFmpeg it would cost.
            raise RuntimeError(
                f"{exc}\n\nEvery video needs a caption of its own — that is "
                f"what stops two files in one Drive folder sharing a name. "
                f"Generate a fresh pool on the Setup page (a pool is spent "
                f"once its captions are used), or lower “Batches to render”."
            ) from exc
        drawn = dict(zip(need_pool, pairs))

    # Hashtags may come from somewhere other than the pool entirely.
    override = _hashtag_override(job_id, params or {})

    rows = []
    used_sheet = used_pool = used_headline = 0
    for n, slot in enumerate(needed):
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
        if override is not None and not from_sheet[slot]:
            # Cycled rather than random so the spread is even and reproducible.
            hashtags = override[n % len(override)] if override else ""
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
        "hashtag_source": (params or {}).get("hashtag_source", "pool"),
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

    # Every video gets its own promo, spread evenly inside each batch — so a
    # batch is a mix of all of them rather than 1,000 variations of one.
    all_slots = batching.plan_render(n_batches, n_rows)
    promo_for = batching.assign_promos(all_slots, len(ws.video_paths) or 1,
                                       seed=f"promo-{job_id}")

    for batch in sorted(by_batch):
        items = by_batch[batch]
        store.set_stage(job_id, f"rendering batch {batch}/{n_batches}")

        # variant_salt is what makes this pass differ from the others: the same
        # row picks different sample clips in each batch.
        cfg = RenderConfig(**base_config)
        cfg.variant_salt = batch
        cfg.font_path = str(ws.font_path) if ws.font_path else None

        # Grouped by promo so one generator is built per promo rather than per
        # row — constructing one probes the video with FFmpeg, which is far too
        # expensive to repeat thousands of times.
        by_promo: dict[int, list[dict]] = {}
        for item in items:
            _b, row_no = batching.split_index(item["idx"], n_rows)
            by_promo.setdefault(
                promo_for.get(Slot(batch=batch, row=row_no), 0), []).append(item)

        for promo_idx in sorted(by_promo):
            group = by_promo[promo_idx]
            promo = ws.promo_for_batch(promo_idx)

            generator = VideoGenerator(
                config=cfg,
                bg_dir=ws.bg_dir,
                video_path=promo,
                cta_path=ws.cta_path,
                work_dir=ws.work_dir / f"b{batch:02d}_p{promo_idx:02d}",
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

            logger.info("Job %s: batch %d/%d — %d row(s) on promo %s",
                        job_id, batch, n_batches, len(group), promo.name)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for item in group:
                    _b, row_no = batching.split_index(item["idx"], n_rows)
                    row = df_run.iloc[row_no - 1]
                    name = (item.get("meta") or {}).get("short_name") or None
                    futures[pool.submit(generator.render_row, row_no, row, name)] = item

                for future in as_completed(futures):
                    item = futures[future]
                    meta = dict(item.get("meta") or {})
                    meta["promo"] = promo.name
                    try:
                        res = future.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Job %s item %s raised", job_id, item["idx"])
                        store.update_item(job_id, item["idx"],
                                          render_status=store.ITEM_FAILED,
                                          render_error=str(exc), render_attempts=1,
                                          meta=meta)
                    else:
                        store.update_item(
                            job_id, item["idx"],
                            name=res.filename or "",
                            render_status=store.ITEM_DONE if res.ok else store.ITEM_FAILED,
                            render_error=res.error,
                            render_attempts=1,
                            warnings=list(res.warnings or []),
                            meta=meta,
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

def _drive_stamp() -> str:
    """The name of a job's Drive folder: sortable, and unique to the run.

    Milliseconds because a plain date collided in two ways — two jobs sharing a
    label on the same day merged into one folder (ensure_folder deliberately
    reuses), and a job that ran past midnight split across two of them."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S.") + f"{now.microsecond // 1000:03d}"


def _drive_root_stamp(job_id: str, params: dict) -> str:
    """The stamp for this job, decided once and reused by every resume.

    Derived fresh each run it would be a *different* folder every time, so a
    requeued job would scatter one batch across as many folders as it took
    attempts. Persisted on first use instead."""
    stamp = (params.get("drive_stamp") or "").strip()
    if stamp:
        return stamp
    # A job that already has files in Drive but no recorded stamp was mid-
    # upload when stamps were introduced: its files sit in the legacy
    # renders/<date>/ tree. Minting a fresh stamp now would split one job
    # across two roots, so reconstruct the legacy name — the date the job
    # first started — and let ensure_folder find the existing tree.
    if any(i.get("drive_file_id") or (i.get("meta") or {}).get("drive_file_id")
           for i in store.list_items(job_id)):
        job = store.get_job(job_id) or {}
        started = job.get("started_at") or job.get("created_at") or time.time()
        stamp = time.strftime("%Y-%m-%d", time.localtime(started))
    else:
        stamp = _drive_stamp()
    params["drive_stamp"] = stamp
    store.merge_job_params(job_id, drive_stamp=stamp)
    return stamp


def _upload(job: dict, n_rows: int, n_folders: int, placement: dict) -> dict:
    """Upload each video twice — short name uploaded, long name server-copied."""
    job_id = job["id"]
    params = job.get("params") or {}
    job["params"] = params
    destination = params.get("drive_folder") or ""
    if not config.drive_configured(destination):
        logger.info("Job %s: Drive not configured — videos stay on the VM", job_id)
        return {}

    from integrations import drive

    # A job may carry its own destination, since the VM's .env cannot be edited
    # per batch. Blank falls back to the configured default.
    drive.set_target(destination)

    try:
        label = job.get("label") or job_id
        root = drive.ensure_path(
            ["renders", _drive_root_stamp(job_id, params), label])
        link = drive.folder_link(root)
        # Two folders per batch: the short single-hashtag name under yt/, the
        # long all-hashtags name under tk/. Keeping both in one folder meant
        # every video appeared twice in the same listing, which made picking
        # what to post needlessly confusing.
        folder_ids = {}
        for f in range(1, n_folders + 1):
            batch_root = drive.ensure_path([batching.folder_name(f)], parent_id=root)
            folder_ids[f] = {
                "yt": drive.ensure_path(["yt"], parent_id=batch_root),
                "tk": drive.ensure_path(["tk"], parent_id=batch_root),
            }
        # Resolved HERE, on the runner thread, where set_target's override is
        # visible — resolve_target is thread-local, so the pool workers below
        # must never resolve it themselves.
        shared_drive_id = drive.resolve_target()[0]
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
        targets = folder_ids.get(folder) or {"yt": root, "tk": root}
        # The platform is the FOLDER, not the filename: yt/ takes the short
        # name, tk/ the long one. Tagging the name too would put "yt " in front
        # of a caption that is meant to be pasted as-is.
        short = meta.get("short_name") or src.name
        long_name = meta.get("long_name") or short

        try:
            # Both Drive writes follow the same rule: record the id the INSTANT
            # the write succeeds, in its own DB update, and skip the write when
            # its id is already recorded. The worker can die between any two
            # statements here; what makes that survivable is that each write's
            # record and the write itself are never more than one statement
            # apart. (The copy used to run unguarded — a worker killed between
            # the copy and the 'done' write re-copied on resume, and Drive
            # happily stores two same-named files in one folder.)
            file_id = meta.get("drive_file_id")
            if not file_id:
                uploaded = drive.upload_file(src, targets["yt"], short)
                file_id = uploaded["id"]
                meta["drive_file_id"] = file_id
                store.update_item(job_id, item["idx"], drive_file_id=file_id,
                                  meta=meta)

            if not meta.get("copy_file_id"):
                copied = None
                if (item.get("upload_attempts") or 0) >= 1:
                    # A previous attempt may have made the copy without us
                    # learning its id — a response lost after Drive committed,
                    # or a crash before the persist below. files.copy is not
                    # idempotent and Drive stores two same-named files in one
                    # folder without complaint, so on a RE-attempt, look
                    # before copying. First attempts skip the extra call.
                    copied = drive.find_file(long_name, targets["tk"],
                                             drive_id=shared_drive_id)
                if not copied:
                    copied = drive.copy_file(file_id, long_name, targets["tk"])
                meta["copy_file_id"] = copied.get("id")
                meta["copy_link"] = copied.get("webViewLink")
                store.update_item(job_id, item["idx"], meta=meta)

            store.update_item(
                job_id, item["idx"], upload_status=store.ITEM_DONE,
                upload_error=None, drive_file_id=file_id,
                drive_link=meta.get("copy_link"), meta=meta)
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
            "Promo": meta.get("promo", ""),
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

    caption_info = _assign_names(job_id, slots, n_rows, df, params)
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
