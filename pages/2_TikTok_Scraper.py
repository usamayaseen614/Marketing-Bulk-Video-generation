"""
pages/2_TikTok_Scraper.py — build the clip bank.

Two ways in, and they are deliberately not the same shape:

  * **Whole account** — paste a profile, get clips downloaded, trimmed and
    organised into Drive. Like generation, this is a background job: submit and
    close the tab. A 1,500 clip scrape takes roughly an hour, most of it
    deliberate rate-limiting.

  * **Single video** — paste one link, get the file back in the browser. It
    runs inline instead of queueing because one video is a few seconds' work,
    and a job you have to go and watch on another page would be slower than
    just doing it. Nothing touches Drive on this path: the download IS the
    delivery.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import streamlit as st

import config as settings
from jobs import store
from scrapers import tiktok
from ui_common import get_session_id, offer_file_download

st.set_page_config(page_title="TikTok Scraper", page_icon="🎵", layout="wide")
store.init_db()

st.title("🎵 TikTok Scraper")

tab_account, tab_single = st.tabs(["📁 Whole account", "🔗 Single video"])

# ========================================================================= account

with tab_account:
    st.caption(
        "Pull ASMR clips from an account, trim them, and drop them into Drive "
        "pre-sorted into slot folders — ready to drag into the generator's sidebar."
    )

    account = st.text_input(
        "TikTok account URL or @handle",
        placeholder="https://www.tiktok.com/@someaccount",
        help="The profile to pull from. Clips come newest-first, TikTok's own order.",
    )

    mode = st.radio(
        "Output layout",
        options=["batch", "dump"],
        format_func=lambda m: {
            "batch": "Batches — pre-split into batch_NN/slot_N folders",
            "dump": "Dump — one flat folder for manual curation",
        }[m],
        help="Batch mode does the sorting for you: 50 clips per batch, "
             "round-robin across the slots, 10 clips per slot.",
    )

    col_a, col_b, col_c = st.columns(3)

    if mode == "batch":
        n_batches = col_a.number_input(
            "How many batches", 1, 100, 4, 1,
            help=f"Each batch is {settings.SCRAPE_BATCH_SIZE} clips across "
                 f"{settings.SCRAPE_SLOTS} slots. If you ask for more clips than "
                 "the account has, the list wraps around — but each wrap is "
                 "reshuffled, so later batches aren't copies of the first ones.",
        )
    else:
        n_batches = 0
        col_a.caption("Dump mode — every clip in one folder.")

    limit = col_b.number_input(
        "Max videos to scan", 10, 5000, settings.SCRAPE_MAX_VIDEOS, 10,
        help="Cap on how far back the profile is walked. Enumeration runs at "
             "roughly 0.26s per post, so 1,500 takes about 6-7 minutes before any "
             "downloading starts.",
    )

    skip_known = col_c.checkbox(
        "Skip clips already scraped", value=True,
        help="Deduplicates by video ID and content hash, so re-scraping an account "
             "next month only pulls what's new.",
    )

    st.subheader("Trim window")
    st.caption(
        "Every clip is cut to this window — a trim, not a filter, so nothing is "
        "dropped for being too long. Starting at 1s skips most creator intro "
        "branding and on-screen text, which would otherwise ride into your output."
    )
    col_start, col_dur = st.columns(2)
    trim_start = col_start.number_input(
        "Start at (s)", 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5)
    trim_duration = col_dur.number_input(
        "Length (s)", 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5)
    st.caption(f"→ keeping {trim_start:g}s to {trim_start + trim_duration:g}s of each clip.")

    drive_folder = st.text_input(
        "Google Drive folder link (optional)", value="",
        placeholder="https://drive.google.com/drive/folders/…",
        help="Send this scrape somewhere specific. Blank uses the server default.",
    )
    notify_email = st.text_input(
        "Notify email (optional)", value=", ".join(settings.MAIL_TO),
        placeholder="you@yourcompany.com",
    )

    if not settings.drive_configured():
        st.warning(
            "Google Drive isn't configured, so clips will stay on the VM instead of "
            "being uploaded. See the Setup page."
        )

    # ----------------------------------------------------------------- submit

    estimate_min = (int(limit) * 0.26 + (n_batches or 20) * 50 * 1.5) / 60
    st.caption(
        f"Rough estimate: ~{estimate_min:.0f} min. Downloading is rate-limited to "
        f"about one clip every {settings.SCRAPE_MIN_DELAY:g}-{settings.SCRAPE_MAX_DELAY:g}s "
        "on purpose — going faster gets the server's IP blocked."
    )

    if st.button("🚀 Start scrape", type="primary", disabled=not account.strip()):
        handle = tiktok.account_name(account)
        job_id = store.new_job_id()
        store.make_job_dirs(job_id)
        store.create_job(
            kind=store.KIND_SCRAPE,
            params={
                "account": account.strip(),
                "mode": mode,
                "batches": int(n_batches),
                "limit": int(limit),
                "trim_start": float(trim_start),
                "trim_duration": float(trim_duration),
                "skip_known": bool(skip_known),
                "drive_folder": drive_folder.strip(),
            },
            label=f"scrape-{handle}",
            notify_email=notify_email.strip(),
            submitted_by=get_session_id(),
            job_id=job_id,
        )
        st.success(
            f"**Queued a scrape of @{handle}.** You can close this tab — it runs in "
            "the background"
            + (f" and emails {notify_email.strip()} when it's done."
               if notify_email.strip() else ".")
        )
        st.page_link("pages/1_Jobs.py", label="📋 Track progress on the Jobs page",
                     icon="➡️")

    # ---------------------------------------------------------------- history

    st.divider()
    st.subheader("Accounts scraped before")

    history = store.scraped_accounts()
    if not history:
        st.caption("None yet.")
    else:
        import time as _time
        for row in history:
            cols = st.columns([3, 2, 2, 1], vertical_alignment="center")
            cols[0].markdown(f"**@{row['account']}**")
            cols[1].caption(f"{row['clips']} clips remembered")
            cols[2].caption(
                "last " + _time.strftime("%Y-%m-%d", _time.localtime(row["last_seen"]))
                if row["last_seen"] else "")
            if cols[3].button("Forget", key=f"forget_{row['account']}",
                              help="Clear the dedup history so the next scrape "
                                   "re-pulls everything from this account."):
                store.forget_account(row["account"])
                st.rerun()


# ==================================================================== single video

def _single_dir() -> Path:
    """This browser session's scratch folder. One fetch at a time — each new
    one replaces the last, so a long session cannot quietly fill the disk."""
    return settings.SINGLE_FETCH_ROOT / get_session_id()


def _count(value) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "—"


with tab_single:
    st.caption(
        "One link in, one file out. This runs right here instead of queueing a "
        "job, and nothing goes to Drive — the download button below is the "
        "delivery."
    )

    video_link = st.text_input(
        "TikTok video link",
        placeholder="https://www.tiktok.com/@someaccount/video/7231234567890123456",
        help="A link to a single post. Share links (vm.tiktok.com/…) work too, "
             "as does a bare video ID copied out of a scrape's metadata sheet. "
             "A profile link belongs in the other tab.",
    )

    want_trim = st.checkbox(
        "Trim it to a window", value=False,
        help="Off by default: a one-off grab usually wants the whole video. "
             "Tick this to cut it the same way a batch scrape would — including "
             "the split into consecutive segments, so a 40s post gives you four "
             "clips instead of one.",
    )

    if want_trim:
        col_s, col_d = st.columns(2)
        single_start = col_s.number_input(
            "Start at (s)", 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5,
            key="single_trim_start")
        single_duration = col_d.number_input(
            "Length (s)", 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5,
            key="single_trim_duration")
        st.caption(
            f"→ cutting from {single_start:g}s in, {single_duration:g}s per "
            "segment, until the video runs out."
        )
    else:
        single_start = single_duration = None

    link_ok = tiktok.is_video_url(video_link)
    if video_link.strip() and not link_ok:
        st.warning(
            "That doesn't look like a link to a single post. Use the **Whole "
            "account** tab for a profile."
        )

    if st.button("⬇️ Fetch video", type="primary", key="fetch_single",
                 disabled=not link_ok):
        st.session_state.pop("single_fetch", None)
        try:
            # Can run to ~15s: TikTok's extraction fails intermittently on
            # links that are perfectly fine, so this retries before believing it.
            with st.spinner("Asking TikTok for that video…"):
                fetched = tiktok.fetch_single_video(
                    video_link, _single_dir(),
                    trim_start=single_start,
                    trim_duration=single_duration,
                )
                # Segments are worth one click, not four.
                bundle = None
                if len(fetched.files) > 1:
                    bundle = _single_dir() / f"{tiktok.clip_stem(fetched.clip)}.zip"
                    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as zf:
                        for part in fetched.files:
                            zf.write(part, part.name)
        except tiktok.ScrapeError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001 — yt-dlp raises its own zoo
            st.error(f"That fetch failed: {tiktok.explain_failure(str(exc))}")
        else:
            st.session_state["single_fetch"] = {
                "clip": fetched.clip,
                "files": [str(p) for p in fetched.files],
                "bundle": str(bundle) if bundle else "",
                "trimmed": fetched.trimmed,
                "has_audio": tiktok.has_audio(fetched.files[0]),
            }

    # ------------------------------------------------------------------ result
    #
    # Rendered from session state rather than from the fetch above, because
    # offer_file_download reruns the page to gate a large file — on that rerun
    # the button was not pressed, and re-fetching would hit TikTok twice for
    # one click.

    saved = st.session_state.get("single_fetch")
    if saved:
        files = [Path(p) for p in saved["files"]]
        live = [p for p in files if p.is_file()]

        if not live:
            # The TTL reaper got here first, or the session folder was reused.
            st.info("Those files have been cleaned up. Fetch the link again.")
            st.session_state.pop("single_fetch", None)
        else:
            clip = saved["clip"]
            st.success(
                f"Got it — {len(live)} file{'s' if len(live) != 1 else ''}"
                + (" (trimmed)" if saved["trimmed"] else " (original, untrimmed)")
            )

            # Worth saying out loud rather than leaving to be discovered on
            # playback: the recovery re-fetch already ran and still came up
            # empty, so this post genuinely has no sound.
            if not saved.get("has_audio", True):
                st.warning(
                    "This clip has **no audio track** — the post itself has no "
                    "sound. Not much use if you're after ASMR."
                )

            if clip.title:
                st.markdown(f"**{clip.title}**")
            byline = f"@{clip.uploader}" if clip.uploader else ""
            posted = clip.posted_date()
            st.caption(" · ".join(x for x in [byline, posted, clip.url] if x))

            stats = st.columns(4)
            stats[0].metric("Views", _count(clip.view_count))
            stats[1].metric("Likes", _count(clip.like_count))
            stats[2].metric("Comments", _count(clip.comment_count))
            stats[3].metric(
                "Length",
                f"{clip.duration:g}s" if clip.duration else "—")

            st.divider()

            bundle = Path(saved["bundle"]) if saved["bundle"] else None
            if bundle and bundle.is_file():
                offer_file_download(
                    bundle, f"single_{clip.video_id}_zip",
                    f"⬇️ Download all {len(live)} segments (ZIP)",
                    mime="application/zip")
                st.caption("…or grab them one at a time:")

            for n, path in enumerate(live, start=1):
                label = ("⬇️ Download video" if len(live) == 1
                         else f"⬇️ Segment {n} — {path.name}")
                offer_file_download(path, f"single_{clip.video_id}_{n}", label,
                                    mime="video/mp4")


# The single-fetch folders are the one thing in JOBS_ROOT no job row points at,
# so no other reaper will ever take them. Swept here, at the end, so a slow
# rmtree never sits between the user's click and their download button.
tiktok.prune_fetch_dirs(settings.SINGLE_FETCH_ROOT, settings.SINGLE_FETCH_TTL_HOURS)
