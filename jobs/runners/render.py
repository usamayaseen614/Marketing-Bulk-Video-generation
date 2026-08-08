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
just one promo video, and each is published **twice** — once under a short name
carrying a single hashtag, once under a longer name carrying up to five. Both
are capped at 90 characters of name, with ".mp4" outside that count.

## How they are published

`upload_mode: zip` (the default) puts each output folder into two archives —
`batch_NN/yt.zip` holding the short names, `batch_NN/tk.zip` the long ones. A
16,000-video night is then ~32 files in Drive instead of ~32,000, which is the
difference between a folder you can hand to someone and one you cannot.

`upload_mode: files` is the older behaviour: every video as its own Drive file
under `batch_NN/yt/` and `batch_NN/tk/`, the second made with a server-side
`files.copy` so the bytes cross the network once.

The archives cost that copy — a ZIP is opaque to files.copy, so the long-named
archive cannot be cloned from the short-named one and the bytes go up twice.
Packing happens a folder at a time and each folder's MP4s are deleted once both
of its archives are verified in Drive, so the disk high-water mark is the
rendered videos plus two archives, not two copies of everything.

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
import text_grids
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
                  df: pd.DataFrame, params: Optional[dict] = None,
                  overrides: Optional[dict] = None,
                  promo_for: Optional[dict] = None) -> dict:
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

    When a Headline grid was uploaded, step 3 reads the text that promo will
    actually render rather than the main sheet's. Both would work — names are
    de-duplicated either way — but a file called `001_main_two.mp4` whose video
    says something else is a trap for whoever posts it.
    """
    from captions import naming
    from video_generator import _clean_str

    overrides = overrides or {}
    promo_for = promo_for or {}
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

    def _headline(slot: Slot) -> str:
        """The Headline this particular video will show — the grid's when one
        covers its promo, the sheet's otherwise."""
        return text_grids.override_text(
            overrides, "Headline", promo_for.get(slot, 0), slot.row
        ) or _cell(slot.row, "Headline")

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
            caption, hashtags = _headline(slot), ""
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
                    n_rows: int, workers: int,
                    overrides: Optional[dict] = None,
                    promo_for: Optional[dict] = None) -> list[str]:
    """Render every outstanding item, one batch at a time."""
    job_id = job["id"]
    overrides = overrides or {}
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
    # Passed in by run(), which needs the same mapping to name the files.
    if promo_for is None:
        promo_for = batching.assign_promos(
            batching.plan_render(n_batches, n_rows),
            len(ws.video_paths) or 1, seed=f"promo-{job_id}")

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
                gif_paths=ws.gif_paths,
            )
            for message in generator.input_warnings:
                if message not in batch_warnings:
                    batch_warnings.append(message)

            # The one place per-promo text enters the render. Applied before
            # assign_backgrounds so the frame handed to render_row is a normal
            # sheet — RowSpec.from_row reads row['Headline'] and has no idea a
            # grid was involved, which is why nothing downstream changed.
            df_promo = text_grids.apply_overrides(df, overrides, promo_idx)
            df_run, bg_warnings = generator.assign_backgrounds(df_promo)
            for message in bg_warnings:
                if message not in batch_warnings:
                    batch_warnings.append(message)

            logger.info("Job %s: batch %d/%d — %d row(s) on promo %s%s",
                        job_id, batch, n_batches, len(group), promo.name,
                        " (per-promo text)" if df_promo is not df else "")

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
    """Publish the finished videos to Drive, in whichever shape was asked for.

    `zip` (the default) publishes each output folder as two archives —
    `batch_NN/yt.zip` and `batch_NN/tk.zip`. `files` publishes every video as
    its own Drive file under `batch_NN/yt/` and `batch_NN/tk/`, which is what
    every render before this did.

    Both are kept, because they fail differently. Archives are far quicker to
    move and to hand to someone, and they are what a 16,000-video night should
    produce; but one video inside an archive cannot be replaced without
    rebuilding it, and the second archive costs a second trip over the network
    where a second *file* costs only a server-side copy.
    """
    job_id = job["id"]
    params = job.get("params") or {}
    job["params"] = params
    if not config.drive_configured(params.get("drive_folder") or ""):
        logger.info("Job %s: Drive not configured — videos stay on the VM", job_id)
        return {}

    mode = str(params.get("upload_mode") or config.UPLOAD_MODE).strip().lower()
    if mode not in {"zip", "files"}:
        logger.warning("Job %s: unknown upload mode %r — using zip", job_id, mode)
        mode = "zip"
    if mode == "files":
        return _upload_files(job, n_rows, n_folders, placement)
    return _upload_zips(job, n_rows, n_folders, placement)


def _upload_files(job: dict, n_rows: int, n_folders: int, placement: dict) -> dict:
    """Upload each video twice — short name uploaded, long name server-copied."""
    job_id = job["id"]
    params = job.get("params") or {}
    job["params"] = params
    destination = params.get("drive_folder") or ""
    # Normally both. One of them halves what this run sends to Drive — see
    # _selected_platforms. The FIRST is uploaded and the rest are server-side
    # copies of it, so with one platform selected nothing is copied at all.
    platforms = _selected_platforms(params)

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
                platform: drive.ensure_path([platform], parent_id=batch_root)
                for platform in platforms
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
        targets = folder_ids.get(folder) or {p: root for p in platforms}
        # The platform is the FOLDER, not the filename: yt/ takes the short
        # name, tk/ the long one. Tagging the name too would put "yt " in front
        # of a caption that is meant to be pasted as-is.
        short = meta.get("short_name") or src.name
        names = {"yt": short, "tk": meta.get("long_name") or short}
        primary, secondaries = platforms[0], platforms[1:]

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
                uploaded = drive.upload_file(src, targets[primary],
                                             names[primary])
                file_id = uploaded["id"]
                meta["drive_file_id"] = file_id
                meta["upload_link"] = uploaded.get("webViewLink")
                store.update_item(job_id, item["idx"], drive_file_id=file_id,
                                  meta=meta)

            # Nothing to copy when only one platform is being published — the
            # bytes went up under that platform's own name.
            for secondary in secondaries:
                if meta.get("copy_file_id"):
                    break
                copied = None
                if (item.get("upload_attempts") or 0) >= 1:
                    # A previous attempt may have made the copy without us
                    # learning its id — a response lost after Drive committed,
                    # or a crash before the persist below. files.copy is not
                    # idempotent and Drive stores two same-named files in one
                    # folder without complaint, so on a RE-attempt, look
                    # before copying. First attempts skip the extra call.
                    copied = drive.find_file(names[secondary], targets[secondary],
                                             drive_id=shared_drive_id)
                if not copied:
                    copied = drive.copy_file(file_id, names[secondary],
                                             targets[secondary])
                meta["copy_file_id"] = copied.get("id")
                meta["copy_link"] = copied.get("webViewLink")
                store.update_item(job_id, item["idx"], meta=meta)

            store.update_item(
                job_id, item["idx"], upload_status=store.ITEM_DONE,
                upload_error=None, drive_file_id=file_id,
                drive_link=meta.get("copy_link") or meta.get("upload_link"),
                meta=meta)
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

    logger.info("Job %s: uploaded %d, failed %d (each as %d Drive file(s))",
                job_id, uploaded_ok, failed, len(platforms))
    return {
        "drive_link": link,
        "drive_folder_id": root,
        "upload_mode": "files",
        "upload_platforms": list(platforms),
        "uploaded": counts["uploaded"],
        "drive_files": counts["uploaded"] * len(platforms),
        "upload_failed": counts["upload_failed"],
        "upload_failures": upload_failures,
    }


# --------------------------------------------------------------------------- upload (zip)

# One archive per platform, per output folder. The platform is the ARCHIVE,
# never the filename — neither name carries a platform tag, because a filename
# here is a caption meant to be pasted straight into the post.
PLATFORMS = ("yt", "tk")


def _selected_platforms(params: dict) -> tuple[str, ...]:
    """Which of the two names this run publishes, in publishing order.

    Both, normally. One of them when a night is too big for Drive's 750 GB per
    rolling 24 hours: publishing `tk` today and `yt` tomorrow halves each day's
    traffic, and because each platform's state is recorded separately, the
    second run skips what the first already sent.

    Unknown names are dropped rather than trusted — `upload_platforms: ["tok"]`
    should publish nothing new, not silently publish everything."""
    raw = params.get("upload_platforms")
    if raw is None:
        raw = config.UPLOAD_PLATFORMS
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    chosen = tuple(p for p in PLATFORMS if p in {str(x).strip().lower() for x in raw})
    if not chosen:
        logger.warning("No recognised upload platform in %r — publishing both", raw)
        return PLATFORMS
    return chosen


def _zip_entries(items: list[dict], n_rows: int, videos_root: Path
                 ) -> list[tuple[dict, Path, dict[str, str]]]:
    """(item, mp4 path, {platform: its name in that platform's archive}).

    `yt.zip` takes the short name — caption plus a single hashtag — and
    `tk.zip` the long one carrying up to five. Both fall back to whatever the
    file was actually written as, so an item whose names were assigned by an
    older version still ends up in both archives.

    The item is carried alongside because the caller has to be able to say
    which *rows* went into an archive and which did not."""
    entries = []
    for item in sorted(items, key=lambda i: i["idx"]):
        batch, _row = batching.split_index(item["idx"], n_rows)
        meta = item.get("meta") or {}
        name = item.get("name") or ""
        short = meta.get("short_name") or name
        src = videos_root / batching.source_folder_name(batch) / name
        entries.append((item, src, {"yt": short,
                                    "tk": meta.get("long_name") or short}))
    return entries


def _upload_zips(job: dict, n_rows: int, n_folders: int, placement: dict) -> dict:
    """Publish each output folder as `yt.zip` + `tk.zip`.

    ## Why a folder at a time

    A 16,000-video render is ~500 GB of MP4s and the same again in archives. If
    every archive were built before any were uploaded, the VM would need room
    for both at once. So a folder is packed, uploaded, verified, and only then
    is its share of the MP4s deleted — peak disk is the rendered videos plus
    the two archives of the *one* folder currently being packed.

    ## Why the MP4s are only freed after Drive confirms

    The videos are the only thing that cannot be rebuilt without re-rendering.
    They are therefore deleted last: after both archives are uploaded AND their
    stored size matches what was sent. A failure anywhere before that leaves
    every byte where it was, and the next attempt re-packs the folder.

    ## Resume

    Which folders are finished is written to the job's params as each one
    lands, so a worker killed at folder 11 of 16 re-packs folder 11 and leaves
    the first ten alone. Uploads go through upload_verified(), which *replaces*
    a same-named archive rather than adding a second one — so even a folder
    whose recorded state was lost cannot end up with two `tk.zip`s.
    """
    job_id = job["id"]
    params = job.get("params") or {}
    job["params"] = params
    destination = params.get("drive_folder") or ""

    import packing
    from integrations import drive

    drive.set_target(destination)

    try:
        label = job.get("label") or job_id
        root = drive.ensure_path(
            ["renders", _drive_root_stamp(job_id, params), label])
        link = drive.folder_link(root)
        # Resolved on this thread, where set_target's override is visible.
        shared_drive_id = drive.resolve_target()[0]
        folder_ids = {
            f: drive.ensure_path([batching.folder_name(f)], parent_id=root)
            for f in range(1, n_folders + 1)
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s: could not prepare Drive folders", job_id)
        return {"drive_error": str(exc)}

    videos_root = store.videos_dir(job_id)
    pack_root = store.job_dir(job_id) / "packing"
    platforms = _selected_platforms(params)
    # The MP4s may only be freed once EVERY platform has an archive — not just
    # the ones this run was asked for. Publishing tk today and yt tomorrow is
    # the whole point of the option, and deleting the videos tonight would
    # leave nothing to build yt.zip from.
    free_local = bool(params.get("free_local_videos", config.UPLOAD_FREE_LOCAL))
    logger.info("Job %s: publishing %s as ZIPs", job_id, ", ".join(platforms))

    # Every rendered item, grouped by the folder it was mixed into. An archive
    # is all-or-nothing — it has to hold the folder's complete contents — so
    # this is not filtered to items still pending upload the way the per-file
    # path is.
    by_folder: dict[int, list[dict]] = {}
    for item in store.list_items(job_id):
        if item["render_status"] != store.ITEM_DONE:
            continue
        batch, row_no = batching.split_index(item["idx"], n_rows)
        by_folder.setdefault(
            placement.get(Slot(batch=batch, row=row_no), 1), []).append(item)

    state: dict = dict(params.get("zip_uploads") or {})
    archives: list[dict] = []
    errors: list[str] = []
    freed_files = freed_bytes = kept = 0
    total_folders = len(by_folder)

    for position, folder in enumerate(sorted(by_folder), start=1):
        items = by_folder[folder]
        name = batching.folder_name(folder)
        recorded = dict(state.get(str(folder)) or {})
        published: dict = dict(recorded.get("platforms") or {})
        if not published and recorded.get("complete"):
            # A folder recorded before publishing became per-platform. Its
            # archives list says which ones landed, so read that rather than
            # re-sending them.
            published = {a["platform"]: a for a in recorded.get("archives") or []
                         if a.get("platform")}

        todo = [p for p in platforms if p not in published]
        if not todo:
            logger.info("Job %s: %s already has %s — skipping", job_id, name,
                        ", ".join(f"{p}.zip" for p in platforms))
            archives.extend(published[p] for p in platforms if p in published)
            store.set_upload_status(job_id, [i["idx"] for i in items],
                                    store.ITEM_DONE,
                                    drive_link=recorded.get("link"))
            continue

        store.set_stage(job_id, f"packing {name} ({position}/{total_folders})")
        prepared = _zip_entries(items, n_rows, videos_root)

        # Checked here rather than inside the packer, because a video that is
        # not in the archive must be *reported* as not uploaded. Sweeping it
        # into a folder marked published is how a missing file stops being
        # visible to anyone.
        packed = [(item, src, names) for item, src, names in prepared
                  if src.is_file()]
        absent = [item["idx"] for item, src, _n in prepared if not src.is_file()]
        if absent:
            store.set_upload_status(job_id, absent, store.ITEM_FAILED,
                                    error="Rendered file missing on disk",
                                    count_attempt=True)
            errors.append(f"{name}: {len(absent)} rendered file(s) were missing "
                          "on disk and are not in the archives")
        if not packed:
            logger.warning("Job %s: %s has no files to pack", job_id, name)
            continue

        idxs = [item["idx"] for item, _s, _n in packed]
        entries = [(src, names) for _i, src, names in packed]
        targets = {p: pack_root / name / f"{p}.zip" for p in todo}

        try:
            built = packing.build_platform_zips(entries, targets)
            for platform, info in built["platforms"].items():
                packing.verify(info["path"], expected_files=len(entries))
                logger.info("Job %s: %s/%s.zip — %d file(s), %.1f GB",
                            job_id, name, platform, info["files"],
                            info["size"] / 1024 ** 3)

            store.set_stage(job_id,
                            f"uploading {name} ({position}/{total_folders})")
            for platform in todo:
                info = built["platforms"][platform]
                response = drive.upload_verified(
                    info["path"], folder_ids[folder], f"{platform}.zip",
                    drive_id=shared_drive_id)
                # Recorded per platform the moment it lands, so a run that dies
                # after tk.zip and before yt.zip does not re-send tk.zip.
                published[platform] = {
                    "folder": name, "platform": platform,
                    "id": response.get("id"), "files": info["files"],
                    "bytes": int(response.get("size") or info["size"]),
                    "link": response.get("webViewLink") or "",
                }
                archives.append(published[platform])
        except Exception as exc:  # noqa: BLE001 — one folder must not lose the rest
            logger.exception("Job %s: could not publish %s", job_id, name)
            errors.append(f"{name}: {exc}")
            store.set_upload_status(job_id, idxs, store.ITEM_FAILED,
                                    error=str(exc)[:500], count_attempt=True)
            # The MP4s are untouched, so the next attempt re-packs from them.
            shutil.rmtree(pack_root / name, ignore_errors=True)
            continue

        # `complete` means every platform, not just the ones asked for today —
        # it is what tells a later run there is nothing left to publish here.
        every = all(p in published for p in PLATFORMS)
        recorded = {
            "complete": every,
            "platforms": published,
            "link": (published.get("tk") or published.get("yt") or {}).get("link") or link,
            "archives": list(published.values()),
        }
        state[str(folder)] = recorded
        # Persisted BEFORE the MP4s are deleted: a crash in the tidy-up below
        # must not look like a folder that was never published.
        params = store.merge_job_params(job_id, zip_uploads=state)
        job["params"] = params
        store.set_upload_status(job_id, idxs, store.ITEM_DONE,
                                drive_link=recorded["link"])

        shutil.rmtree(pack_root / name, ignore_errors=True)
        if free_local and every:
            count, size = packing.free_files(src for src, _names in entries)
            freed_files += count
            freed_bytes += size
            logger.info("Job %s: freed %d MP4(s) from %s", job_id, count, name)
        elif free_local:
            missing = [p for p in PLATFORMS if p not in published]
            kept += len(entries)
            logger.info("Job %s: keeping %s's MP4s — %s not published yet",
                        job_id, name, ", ".join(f"{p}.zip" for p in missing))

    shutil.rmtree(pack_root, ignore_errors=True)
    counts = store.item_counts(job_id)
    logger.info("Job %s: %d archive(s) published, %d folder(s) failed; "
                "freed %.1f GB of MP4s",
                job_id, len(archives), len(errors), freed_bytes / 1024 ** 3)

    pending_platforms = [p for p in PLATFORMS if p not in platforms]
    if kept:
        logger.info(
            "Job %s: %d MP4(s) kept on the VM — %s still to publish. Requeue "
            "this job with upload_platforms=%s once Drive's 24h allowance has "
            "rolled.", job_id, kept,
            ", ".join(f"{p}.zip" for p in pending_platforms),
            pending_platforms)

    return {
        "drive_link": link,
        "drive_folder_id": root,
        "drive_folder_ids": {str(f): fid for f, fid in folder_ids.items()},
        "upload_mode": "zip",
        "upload_platforms": list(platforms),
        "platforms_pending": pending_platforms if kept else [],
        "uploaded": counts["uploaded"],
        "drive_files": len(archives),
        "drive_zips": len(archives),
        "zip_bytes": sum(a["bytes"] for a in archives),
        "upload_failed": counts["upload_failed"],
        "upload_failures": errors[:25],
        "videos_freed": bool(freed_files),
        "freed_bytes": freed_bytes,
        "videos_kept": kept,
    }


# --------------------------------------------------------------------------- manifests

def _write_manifests(job_id: str, n_rows: int, n_folders: int,
                     placement: dict, df: Optional[pd.DataFrame] = None,
                     overrides: Optional[dict] = None,
                     promo_for: Optional[dict] = None) -> Path:
    """One sheet per output folder listing what actually landed in it.

    After mixing, the input sheet no longer describes any single folder, so the
    useful artifact is a per-folder manifest: what to post, with which caption,
    under which filename.

    With per-promo text grids in play the input sheet no longer describes any
    single *video* either — one row renders three different headlines — so the
    text each one actually showed is recorded here as well. Only for the roles
    that have a grid: on a run without them the columns would just repeat the
    sheet."""
    from video_generator import _clean_str

    root = store.job_dir(job_id)
    items = {i["idx"]: i for i in store.list_items(job_id)}
    overrides = overrides or {}
    promo_for = promo_for or {}

    def _rendered_text(role: str, slot: Slot) -> str:
        """What this video actually showed — the same grid-then-sheet
        precedence apply_overrides uses, so a blank grid cell reports the main
        sheet's text rather than an empty string it never rendered."""
        text = text_grids.override_text(overrides, role,
                                        promo_for.get(slot, 0), slot.row)
        if text or df is None or role not in df.columns:
            return text
        return _clean_str(df.iloc[slot.row - 1].get(role))

    rows_by_folder: dict[int, list[dict]] = {}
    for idx, item in items.items():
        if item["render_status"] != store.ITEM_DONE:
            continue
        batch, row_no = batching.split_index(idx, n_rows)
        slot = Slot(batch=batch, row=row_no)
        folder = placement.get(slot, 1)
        meta = item.get("meta") or {}
        entry = {
            "Folder": batching.folder_name(folder),
            "Source_Batch": batch,
            "Sheet_Row": row_no,
            "Caption": meta.get("caption", ""),
            "Hashtags": meta.get("hashtags", ""),
            "Short_Filename": meta.get("short_name", item.get("name", "")),
            "Long_Filename": meta.get("long_name", ""),
            "Promo": meta.get("promo", ""),
            "Drive_Uploaded": item.get("upload_status") == store.ITEM_DONE,
        }
        for role in text_grids.ROLES:
            if overrides.get(role):
                entry[role] = _rendered_text(role, slot)
        rows_by_folder.setdefault(folder, []).append(entry)

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

    # Which promo each video uses. Derived once and shared, because naming and
    # rendering must agree about it — a file named from promo 2's headline and
    # rendered with promo 1's would be worse than having no grids at all.
    promo_for = batching.assign_promos(slots, len(ws.video_paths) or 1,
                                       seed=f"promo-{job_id}")
    # Per-promo Headline/Subheading/Footer text, resolved to promo indices at
    # submit time — the uploaded promo filenames the columns were matched on do
    # not survive staging. {} when no grids were uploaded.
    overrides = text_grids.read_overrides(assets)
    if overrides:
        logger.info("Job %s: per-promo text for %s", job_id,
                    ", ".join(f"{role} ({len(table)} promo(s))"
                              for role, table in sorted(overrides.items())))

    # Register one item per (batch, row) — this is what makes resume work.
    store.add_items(job_id, [
        {"idx": batching.item_index(s.batch, s.row, n_rows), "name": "",
         "meta": {"batch": s.batch, "row": s.row}}
        for s in slots
    ])

    caption_info = _assign_names(job_id, slots, n_rows, df, params,
                                 overrides=overrides, promo_for=promo_for)
    logger.info("Job %s: %d rows x %d batches = %d videos; names assigned: %s",
                job_id, n_rows, n_batches, len(slots), caption_info.get("applied"))

    # The sheet keeps its Caption/Hashtags columns for the first batch, so it
    # still opens as a recognisable version of what was submitted.
    batch_warnings = _render_batches(job, df, ws, n_batches, n_rows, workers,
                                     overrides=overrides, promo_for=promo_for)

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
    manifest = _write_manifests(job_id, n_rows, n_folders, placement, df=df,
                                overrides=overrides, promo_for=promo_for)

    items = store.list_items(job_id)
    records = [results.record_from_item(i) for i in items]
    log_path = store.job_dir(job_id) / "render_log.txt"
    results.write_render_log(log_path, records, elapsed=time.time() - started,
                             extra_warnings=batch_warnings)

    counts = store.item_counts(job_id)
    rendered, failed = counts["rendered"], counts["render_failed"]

    zip_path = None
    if drive_result.get("videos_freed"):
        # The MP4s are inside the per-folder archives in Drive and gone from
        # disk, so there is nothing left here to bundle. Building this anyway
        # would produce an empty ZIP that looks like the fallback and isn't.
        logger.info("Job %s: local videos freed after packing — no fallback ZIP",
                    job_id)
    elif params.get("make_zip", True) and rendered:
        store.set_stage(job_id, "packaging")
        zip_path = results.package_zip(
            store.job_dir(job_id) / "marketing_videos.zip",
            videos, records, log_path=log_path,
            extra_files=[p for p in (manifest, sheet_out) if p.is_file()],
        )

    if drive_result.get("drive_link"):
        try:
            from integrations import drive as drive_mod

            # upload_or_replace, not upload_file: a resumed job runs this block
            # again, and files.create would leave two render_manifest.xlsx in
            # one folder with no way to tell which is current.
            for extra in (manifest, log_path, sheet_out):
                if Path(extra).is_file():
                    drive_mod.upload_or_replace(
                        Path(extra), drive_result["drive_folder_id"],
                        Path(extra).name)
            # The per-folder manifest is what turns an archive back into
            # something usable: which caption and hashtags belong to which
            # filename inside it. Kept OUTSIDE the ZIP so it can be read
            # without downloading 30 GB first.
            for folder, folder_id in (drive_result.get("drive_folder_ids") or {}).items():
                sheet = (store.job_dir(job_id)
                         / f"{batching.folder_name(int(folder))}_manifest.xlsx")
                if sheet.is_file():
                    drive_mod.upload_or_replace(sheet, folder_id, sheet.name)
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
