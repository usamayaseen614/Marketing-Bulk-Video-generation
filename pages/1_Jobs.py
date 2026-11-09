"""
pages/1_Jobs.py — submitted batches: queued, running, finished.

The Generate button no longer blocks on the render, so this is where a batch is
actually watched. Reopening the app at any time — from any browser, after a
laptop sleep, after the tab was closed — shows the same state, because it all
comes from the job database rather than session state.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from i18n import t, language_header, stage_label

import config as settings
import results as results_mod
import ui_common
from jobs import store

st.set_page_config(page_title=t("Jobs"), page_icon="📋", layout="wide")
ui_common.ensure_static_dir()
store.init_db()

language_header()

st.title(t("📋 Jobs"))

ACTIVE = (store.STATUS_QUEUED, store.STATUS_RUNNING)

_STATUS_ICON = {
    store.STATUS_QUEUED: "🕐",
    store.STATUS_RUNNING: "⚙️",
    store.STATUS_SUCCEEDED: "✅",
    store.STATUS_FAILED: "❌",
    store.STATUS_CANCELLED: "🚫",
}


def fmt_time(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def fmt_duration(job: dict) -> str:
    start = job.get("started_at")
    if not start:
        return "—"
    end = job.get("finished_at") or time.time()
    secs = int(end - start)
    return (t("{0}s", secs) if secs < 60
            else t("{0}m {1:02d}s", secs // 60, secs % 60))


def worker_is_alive() -> bool:
    """A running job whose heartbeat has gone quiet means the worker died.

    Worth surfacing loudly: without it, a queued batch that never starts just
    looks like it is taking a long time."""
    running = store.list_jobs(limit=5, statuses=[store.STATUS_RUNNING])
    if not running:
        return True
    newest = max((j.get("heartbeat_at") or 0) for j in running)
    return (time.time() - newest) < settings.JOB_STALE_SECONDS


def stage_for(job: dict) -> str:
    """Scrape items live in their own stage; everything else renders."""
    return (store.STAGE_SCRAPE if job["kind"] == store.KIND_SCRAPE
            else store.STAGE_RENDER)


# "downloading clip 1 40/200", "captions 3/16", "packing batch_02 (2/5)": the
# stages that count for themselves already say how far along they are.
_STAGE_COUNT = re.compile(r"(\d+)\s*/\s*(\d+)")
# Stages whose count is already drawn by the bars above it.
_STAGE_DRAWN = re.compile(r"^(rendering|uploading \d+/\d+)")


def render_progress(job: dict) -> None:
    counts = store.item_counts(job["id"], stage=stage_for(job))
    total = counts["total"]
    stage = job.get("stage") or ""
    if total:
        finished = counts["rendered"] + counts["render_failed"]
        st.progress(
            min(1.0, finished / total),
            text=t("Rendered {0}/{1}", counts['rendered'], total)
                  + (t(" · {0} failed", counts['render_failed']) if counts["render_failed"] else ""),
        )
        # Drive gets a bar of its own once uploading starts. Before, it was a
        # count tacked onto the render bar — which sits at 100% for the whole
        # upload phase, so an hour of uploading looked like nothing happening.
        if (counts["uploaded"] or counts["upload_failed"]
                or stage.startswith("uploading")):
            text = (t("Uploaded {0}/{1} to Drive", counts["uploaded"], total)
                    + (t(" · {0} failed", counts["upload_failed"])
                       if counts["upload_failed"] else ""))
            remaining = total - counts["uploaded"]
            # Speed only while it means something: during the upload, with
            # videos still to go. A finished or stalled upload has no rate.
            rate = (store.upload_rate(job["id"], stage=stage_for(job))
                    if remaining and stage.startswith("uploading") else None)
            if rate:
                per_min = rate * 60
                text += (t(" · {0:.0f} videos/min", per_min) if per_min >= 10
                         else t(" · {0:.1f} videos/min", per_min))
                text += t(" · about {0} left", ui_common.human_duration(remaining / rate))
            st.progress(min(1.0, counts["uploaded"] / total), text=t(text))
    match = _STAGE_COUNT.search(stage)
    if match and not _STAGE_DRAWN.match(stage):
        done, of = int(match.group(1)), int(match.group(2))
        if of > 0:
            st.progress(min(1.0, done / of), text=stage_label(stage))


@st.cache_data(show_spinner=False)
def _read_sheet(path: str, mtime: float):
    """Cached on (path, mtime) so a finished job's sheet is read once, not on
    every 5-second rerun of this page."""
    return pd.read_excel(path, engine="openpyxl")


def show_captions(job: dict, sheet: Path, label: str) -> None:
    """Captions on screen, not just inside a download.

    The captions ARE the deliverable for posting — you read one, paste it, and
    upload the matching file. Making that require downloading a spreadsheet
    called "batch sheet" hid the most useful thing the job produced."""
    try:
        frame = _read_sheet(str(sheet), sheet.stat().st_mtime)
    except Exception as exc:  # noqa: BLE001
        st.caption(t("Couldn't read {0}: {1}", sheet.name, exc))
        return

    is_render = job["kind"] in (store.KIND_RENDER, store.KIND_PIPELINE)
    title = t("📝 Captions & filenames") if is_render else t("📋 Clip metadata")

    with st.expander(t("{0} ({1} rows)", t(title), len(frame))):
        # Lead with what you actually post; the rest is still there to scroll.
        preferred = ["Caption", "Hashtags", "Short_Filename", "Long_Filename",
                     "Folder", "Source_Batch", "Sheet_Row"]
        cols = [c for c in preferred if c in frame.columns]
        cols += [c for c in frame.columns if c not in cols]

        if (is_render and "Caption" in frame.columns
                and (job.get("params") or {}).get("caption_mode") != "none"):
            st.caption(
                t("The **short filename** is what to upload; its caption is the "
                "text to paste. Click a cell to select it, or take the CSV.")
            )
        st.dataframe(frame[cols], hide_index=True, width="stretch", height=320)

        col_x, col_c = st.columns(2)
        with col_x:
            ui_common.offer_file_download(
                sheet, f"sheet_{job['id']}", "⬇️ Excel",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                file_name=f"{label}.xlsx",
            )
        with col_c:
            # CSV opens anywhere and pastes cleanly into a sheet — handy when
            # the Excel is going somewhere that can't open .xlsx. utf-8-sig so
            # Excel on Windows shows accented characters correctly.
            st.download_button(
                t("⬇️ CSV"),
                data=frame[cols].to_csv(index=False).encode("utf-8-sig"),
                file_name=f"{label}.csv", mime="text/csv",
                key=f"csv_{job['id']}",
            )


def pool_workbook(captions: list[str], hashtags: list[str]) -> bytes:
    """The pool as one .xlsx with a sheet each. Two lists of different lengths
    do not belong in one table."""
    import io

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"Caption": captions}).to_excel(
            writer, sheet_name="Captions", index=False)
        pd.DataFrame({"Hashtags": hashtags}).to_excel(
            writer, sheet_name="Hashtag sets", index=False)
    return buf.getvalue()


def show_caption_pool(job: dict) -> None:
    """Everything a caption-pool job produced.

    The pool lives in the database rather than a file, so without this the job
    finished with nothing to look at — no way to read what was generated, judge
    whether the theme worked, or take the text elsewhere."""
    result = job.get("result") or {}
    pool_id = result.get("pool_id")
    pool = store.get_pool(pool_id) if pool_id else None
    if not pool:
        st.caption(t("This pool is no longer in the database."))
        return

    captions, hashtags = pool["captions"], pool["hashtags"]
    active = t(" · **active**") if pool.get("active") else t(" (superseded by a newer pool)")
    st.markdown(
        t("**{0:,} captions × {1:,} hashtag sets = {2:,} unique pairs**{3}", len(captions), len(hashtags), pool['combinations'], t(active))
    )
    st.caption(t("Theme: {0} · model: {1} · {2:,} pairs used so far", pool.get('theme'), pool.get('model'), pool.get('cursor', 0)))

    tab_c, tab_h = st.tabs([t("Captions ({0:,})", len(captions)),
                            t("Hashtag sets ({0:,})", len(hashtags))])
    with tab_c:
        st.dataframe(pd.DataFrame({"Caption": captions}),
                     hide_index=False, width="stretch", height=320)
    with tab_h:
        st.dataframe(pd.DataFrame({"Hashtags": hashtags}),
                     hide_index=False, width="stretch", height=320)

    slug = f"pool_{job['id']}"
    name = (pool.get("theme") or "caption-pool")[:40].strip().replace("/", "-")
    col_x, col_c, col_h = st.columns(3)
    with col_x:
        st.download_button(
            t("⬇️ Excel (both)"),
            data=pool_workbook(captions, hashtags),
            file_name=f"{name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"x_{slug}",
        )
    with col_c:
        st.download_button(
            t("⬇️ Captions CSV"),
            data=pd.DataFrame({"Caption": captions}).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{name}-captions.csv", mime="text/csv", key=f"c_{slug}",
        )
    with col_h:
        st.download_button(
            t("⬇️ Hashtags CSV"),
            data=pd.DataFrame({"Hashtags": hashtags}).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{name}-hashtags.csv", mime="text/csv", key=f"h_{slug}",
        )


def render_job(job: dict, expanded: bool = False) -> None:
    icon = _STATUS_ICON.get(job["status"], "•")
    label = job.get("label") or job["id"]
    kind = t({store.KIND_RENDER: "Render",
            store.KIND_SCRAPE: "Scrape",
            store.KIND_CAPTIONS: "Caption pool",
            store.KIND_PIPELINE: "Full run"}.get(job["kind"], job["kind"]))
    header = f"{icon} {label} — {kind} · {t(job['status'])}"
    if job["status"] == store.STATUS_RUNNING and job.get("stage"):
        header += f" ({stage_label(job['stage'])})"

    with st.expander(t(header), expanded=expanded):
        counts = store.item_counts(job["id"], stage=stage_for(job))
        meta = st.columns(4)
        meta[0].metric(t("Submitted"), fmt_time(job["created_at"]))
        meta[1].metric(t("Duration"), fmt_duration(job))
        if job["kind"] == store.KIND_CAPTIONS:
            # A pool job has no per-video items, so "Videos —" told you nothing.
            res = job.get("result") or {}
            meta[2].metric(t("Captions"), f"{res.get('captions', 0):,}")
            meta[3].metric(t("Combinations"), f"{res.get('combinations', 0):,}")
        else:
            meta[2].metric(t("Videos"), counts["total"] or "—")
            meta[3].metric(t("Failed"), counts["render_failed"] or 0)

        if job["status"] in ACTIVE:
            render_progress(job)

        if job["status"] == store.STATUS_QUEUED:
            if st.button(t("Cancel"), key=f"cancel_{job['id']}"):
                if store.cancel_job(job["id"]):
                    st.rerun()
                else:
                    st.warning(t("Too late — it already started."))

        if job.get("error"):
            st.error(t(job["error"]))

        result = job.get("result") or {}

        drive_link = result.get("drive_link")
        if drive_link:
            st.markdown(t("### [📁 Open in Google Drive]({0})", drive_link))

        for warning in result.get("batch_warnings") or []:
            st.warning(t(warning))

        # ---- downloads (the ZIP fallback, still useful when someone wants the
        # files directly rather than through Drive)
        zip_path = result.get("zip_path")
        if zip_path and Path(zip_path).is_file():
            if job["kind"] == store.KIND_SCRAPE and result.get("asset") == "music":
                label = "⬇️ Download all tracks (ZIP)"
                st.caption(
                    t("One MP3 per distinct sound, in a single flat folder — "
                    "drop them into the generator's Music uploader, or point the "
                    "Music source at this scrape's Drive folder.")
                )
            elif job["kind"] == store.KIND_SCRAPE:
                label = "⬇️ Download all clips (ZIP)"
                st.caption(
                    t("Clips are laid out as `batch_NN/slot_N/` inside the ZIP — "
                    "unzip and drag the slot folders straight into the "
                    "generator's sidebar.")
                )
            else:
                label = "⬇️ Download all videos (ZIP)"
            ui_common.offer_zip_download(Path(zip_path), slug=job["id"], label=label)
        elif (job["kind"] == store.KIND_SCRAPE
              and job["status"] == store.STATUS_SUCCEEDED
              and result.get("clips_dir")
              # Guarded on the directory, not just on the string being set:
              # once an uploaded scrape is purged the path is gone, and this
              # would send someone SSH-ing after a folder that no longer exists.
              and Path(result["clips_dir"]).is_dir()):
            st.info(
                t("Clips are on the machine that ran the job, at `{0}`. Configure Google Drive "
                  "on the Setup page to have future scrapes uploaded automatically.", result['clips_dir'])
            )
        elif result.get("upload_mode") == "zip" and result.get("drive_zips"):
            # There is no local ZIP to offer in this mode and there never was:
            # the videos were packed straight into the per-folder archives in
            # Drive. Saying "the ZIP has been cleaned up" here would send
            # someone looking for a file that was never on this machine.
            names = t(" and ").join(f"`{p}.zip`" for p in
                                 (result.get("upload_platforms") or ["yt", "tk"]))
            freed = (t(" The videos were removed from the server once Drive confirmed them.")
                     if result.get("videos_freed") else "")
            st.caption(
                t("Published as **{0:,} ZIP(s)** — {1} inside each `batch_NN` folder in Drive.{2}", result['drive_zips'], names, freed)
            )
            pending = result.get("platforms_pending") or []
            if pending:
                st.info(
                    "**"
                    + t(", ".join(f"`{p}.zip`" for p in pending))
                    + t(" still to publish.** {0:,} video(s) are being kept on the VM for it — "
                        "they are not deleted until every set of names has been published. Once "
                        "Drive's 750 GB/24h allowance has rolled over, submit this batch again "
                        "with ", result.get('videos_kept', 0))
                    + t(" and ").join(f"*{p}*" for p in pending)
                    + t(" selected; anything already uploaded is skipped.")
                )
        elif drive_link and job["status"] == store.STATUS_SUCCEEDED:
            st.caption(
                t("The outputs are in Google Drive and the local copies have "
                "been removed from this server.")
            )
        elif job["status"] == store.STATUS_SUCCEEDED and counts["rendered"]:
            st.caption(
                t("The local ZIP has been cleaned up. Job folders whose output could not be "
                  "published are kept for {0} days.", settings.JOB_RETENTION_DAYS)
            )

        # ---- a caption-pool job's whole output lives in the database
        if job["kind"] == store.KIND_CAPTIONS and job["status"] == store.STATUS_SUCCEEDED:
            show_caption_pool(job)

        # ---- captions/metadata, readable on the page rather than hidden in a file
        sheet = result.get("sheet_path")
        if sheet and Path(sheet).is_file():
            show_captions(job, Path(sheet), label)

        # ---- per-row detail
        items = store.list_items(job["id"], stage=stage_for(job))
        failed = [i for i in items if i["render_status"] == store.ITEM_FAILED]
        if failed:
            with st.expander(t("Failed rows ({0})", len(failed)), expanded=False):
                st.dataframe(
                    pd.DataFrame([{"Row": i["idx"], "Error": i["render_error"]}
                                  for i in failed]),
                    hide_index=True, width="stretch",
                )

        upload_failed = [i for i in items if i["upload_status"] == store.ITEM_FAILED]
        if upload_failed:
            with st.expander(t("Drive uploads that failed ({0})", len(upload_failed))):
                st.dataframe(
                    pd.DataFrame([{"Row": i["idx"], "File": i["name"],
                                   "Error": i["upload_error"]}
                                  for i in upload_failed]),
                    hide_index=True, width="stretch",
                )

        warn_rows = [(i["idx"], w) for i in items for w in (i["warnings"] or [])]
        if warn_rows:
            with st.expander(t("Warnings ({0})", len(warn_rows))):
                for idx, message in warn_rows:
                    st.text(t("Row {0}: {1}", idx, message))

        st.caption(t("Job id `{0}`", job['id']))


# --------------------------------------------------------------------------- page

if not worker_is_alive():
    st.error(
        t("**The worker looks down.** A job is marked running but hasn't checked in for over "
          "{0}s. It will be requeued and resumed automatically once the worker is back — nothing"
          " already rendered is lost. On the VM, check the worker process in the container.", int(settings.JOB_STALE_SECONDS))
    )

# Active jobs refresh on their own; finished ones don't need to.
@st.fragment(run_every="5s")
def active_section() -> None:
    active = store.list_jobs(limit=25, statuses=list(ACTIVE))
    st.subheader(t("Active ({0})", len(active)))
    if not active:
        st.caption(t("Nothing queued or running."))
        return
    for job in active:
        render_job(job, expanded=True)


active_section()

st.divider()

col_head, col_refresh = st.columns([4, 1], vertical_alignment="bottom")
col_head.subheader(t("History"))
if col_refresh.button(t("🔄 Refresh"), width="stretch", key='_ui_1_Jobs_refresh'):
    st.rerun()

history = store.list_jobs(
    limit=40,
    statuses=[store.STATUS_SUCCEEDED, store.STATUS_FAILED, store.STATUS_CANCELLED],
)
if not history:
    st.caption(t("No finished jobs yet."))
for job in history:
    render_job(job)
