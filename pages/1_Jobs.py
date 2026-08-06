"""
pages/1_Jobs.py — submitted batches: queued, running, finished.

The Generate button no longer blocks on the render, so this is where a batch is
actually watched. Reopening the app at any time — from any browser, after a
laptop sleep, after the tab was closed — shows the same state, because it all
comes from the job database rather than session state.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import streamlit as st

import config as settings
import results as results_mod
import ui_common
from jobs import store

st.set_page_config(page_title="Jobs", page_icon="📋", layout="wide")
ui_common.ensure_static_dir()
store.init_db()

st.title("📋 Jobs")

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
    return f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60:02d}s"


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


def render_progress(job: dict) -> None:
    counts = store.item_counts(job["id"], stage=stage_for(job))
    total = counts["total"]
    if not total:
        return
    finished = counts["rendered"] + counts["render_failed"]
    st.progress(
        min(1.0, finished / total),
        text=f"Rendered {counts['rendered']}/{total}"
              + (f" · {counts['render_failed']} failed" if counts["render_failed"] else "")
              + (f" · {counts['uploaded']} uploaded" if counts["uploaded"] else ""),
    )


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
        st.caption(f"Couldn't read {sheet.name}: {exc}")
        return

    is_render = job["kind"] in (store.KIND_RENDER, store.KIND_PIPELINE)
    title = "📝 Captions & filenames" if is_render else "📋 Clip metadata"

    with st.expander(f"{title} ({len(frame)} rows)"):
        # Lead with what you actually post; the rest is still there to scroll.
        preferred = ["Caption", "Hashtags", "Short_Filename", "Long_Filename",
                     "Folder", "Source_Batch", "Sheet_Row"]
        cols = [c for c in preferred if c in frame.columns]
        cols += [c for c in frame.columns if c not in cols]

        if is_render and "Caption" in frame.columns:
            st.caption(
                "The **short filename** is what to upload; its caption is the "
                "text to paste. Click a cell to select it, or take the CSV."
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
                "⬇️ CSV",
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
        st.caption("This pool is no longer in the database.")
        return

    captions, hashtags = pool["captions"], pool["hashtags"]
    active = " · **active**" if pool.get("active") else " (superseded by a newer pool)"
    st.markdown(
        f"**{len(captions):,} captions × {len(hashtags):,} hashtag sets = "
        f"{pool['combinations']:,} unique pairs**{active}"
    )
    st.caption(f"Theme: {pool.get('theme')} · model: {pool.get('model')} · "
               f"{pool.get('cursor', 0):,} pairs used so far")

    tab_c, tab_h = st.tabs([f"Captions ({len(captions):,})",
                            f"Hashtag sets ({len(hashtags):,})"])
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
            "⬇️ Excel (both)",
            data=pool_workbook(captions, hashtags),
            file_name=f"{name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"x_{slug}",
        )
    with col_c:
        st.download_button(
            "⬇️ Captions CSV",
            data=pd.DataFrame({"Caption": captions}).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{name}-captions.csv", mime="text/csv", key=f"c_{slug}",
        )
    with col_h:
        st.download_button(
            "⬇️ Hashtags CSV",
            data=pd.DataFrame({"Hashtags": hashtags}).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"{name}-hashtags.csv", mime="text/csv", key=f"h_{slug}",
        )


def render_job(job: dict, expanded: bool = False) -> None:
    icon = _STATUS_ICON.get(job["status"], "•")
    label = job.get("label") or job["id"]
    kind = {store.KIND_RENDER: "Render",
            store.KIND_SCRAPE: "Scrape",
            store.KIND_CAPTIONS: "Caption pool",
            store.KIND_PIPELINE: "Full run"}.get(job["kind"], job["kind"])
    header = f"{icon} {label} — {kind} · {job['status']}"
    if job["status"] == store.STATUS_RUNNING and job.get("stage"):
        header += f" ({job['stage']})"

    with st.expander(header, expanded=expanded):
        counts = store.item_counts(job["id"], stage=stage_for(job))
        meta = st.columns(4)
        meta[0].metric("Submitted", fmt_time(job["created_at"]))
        meta[1].metric("Duration", fmt_duration(job))
        if job["kind"] == store.KIND_CAPTIONS:
            # A pool job has no per-video items, so "Videos —" told you nothing.
            res = job.get("result") or {}
            meta[2].metric("Captions", f"{res.get('captions', 0):,}")
            meta[3].metric("Combinations", f"{res.get('combinations', 0):,}")
        else:
            meta[2].metric("Videos", counts["total"] or "—")
            meta[3].metric("Failed", counts["render_failed"] or 0)

        if job["status"] in ACTIVE:
            render_progress(job)

        if job["status"] == store.STATUS_QUEUED:
            if st.button("Cancel", key=f"cancel_{job['id']}"):
                if store.cancel_job(job["id"]):
                    st.rerun()
                else:
                    st.warning("Too late — it already started.")

        if job.get("error"):
            st.error(job["error"])

        result = job.get("result") or {}

        drive_link = result.get("drive_link")
        if drive_link:
            st.markdown(f"### [📁 Open in Google Drive]({drive_link})")

        for warning in result.get("batch_warnings") or []:
            st.warning(warning)

        # ---- downloads (the ZIP fallback, still useful when someone wants the
        # files directly rather than through Drive)
        zip_path = result.get("zip_path")
        if zip_path and Path(zip_path).is_file():
            if job["kind"] == store.KIND_SCRAPE:
                label = "⬇️ Download all clips (ZIP)"
                st.caption(
                    "Clips are laid out as `batch_NN/slot_N/` inside the ZIP — "
                    "unzip and drag the slot folders straight into the "
                    "generator's sidebar."
                )
            else:
                label = "⬇️ Download all videos (ZIP)"
            ui_common.offer_zip_download(Path(zip_path), slug=job["id"], label=label)
        elif (job["kind"] == store.KIND_SCRAPE
              and job["status"] == store.STATUS_SUCCEEDED
              and result.get("clips_dir")):
            st.info(
                "Clips are on the machine that ran the job, at "
                f"`{result['clips_dir']}`. Configure Google Drive on the Setup "
                "page to have future scrapes uploaded automatically."
            )
        elif job["status"] == store.STATUS_SUCCEEDED and counts["rendered"]:
            st.caption(
                "The ZIP has been cleaned up (job folders are kept for "
                f"{settings.JOB_RETENTION_DAYS} days)."
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
            with st.expander(f"Failed rows ({len(failed)})", expanded=False):
                st.dataframe(
                    pd.DataFrame([{"Row": i["idx"], "Error": i["render_error"]}
                                  for i in failed]),
                    hide_index=True, width="stretch",
                )

        upload_failed = [i for i in items if i["upload_status"] == store.ITEM_FAILED]
        if upload_failed:
            with st.expander(f"Drive uploads that failed ({len(upload_failed)})"):
                st.dataframe(
                    pd.DataFrame([{"Row": i["idx"], "File": i["name"],
                                   "Error": i["upload_error"]}
                                  for i in upload_failed]),
                    hide_index=True, width="stretch",
                )

        warn_rows = [(i["idx"], w) for i in items for w in (i["warnings"] or [])]
        if warn_rows:
            with st.expander(f"Warnings ({len(warn_rows)})"):
                for idx, message in warn_rows:
                    st.text(f"Row {idx}: {message}")

        st.caption(f"Job id `{job['id']}`")


# --------------------------------------------------------------------------- page

if not worker_is_alive():
    st.error(
        "**The worker looks down.** A job is marked running but hasn't checked "
        f"in for over {int(settings.JOB_STALE_SECONDS)}s. It will be requeued "
        "and resumed automatically once the worker is back — nothing already "
        "rendered is lost. On the VM, check the worker process in the container."
    )

# Active jobs refresh on their own; finished ones don't need to.
@st.fragment(run_every="5s")
def active_section() -> None:
    active = store.list_jobs(limit=25, statuses=list(ACTIVE))
    st.subheader(f"Active ({len(active)})")
    if not active:
        st.caption("Nothing queued or running.")
        return
    for job in active:
        render_job(job, expanded=True)


active_section()

st.divider()

col_head, col_refresh = st.columns([4, 1], vertical_alignment="bottom")
col_head.subheader("History")
if col_refresh.button("🔄 Refresh", width="stretch"):
    st.rerun()

history = store.list_jobs(
    limit=40,
    statuses=[store.STATUS_SUCCEEDED, store.STATUS_FAILED, store.STATUS_CANCELLED],
)
if not history:
    st.caption("No finished jobs yet.")
for job in history:
    render_job(job)
