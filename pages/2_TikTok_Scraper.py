"""
pages/2_TikTok_Scraper.py — build the clip bank.

Paste a TikTok account, get clips downloaded, trimmed and organised into Drive.
Like generation, this is a background job: submit and close the tab. A 1,500
clip scrape takes roughly an hour, most of it deliberate rate-limiting.
"""

from __future__ import annotations

import streamlit as st

import config as settings
from jobs import store
from scrapers import tiktok
from ui_common import get_session_id

st.set_page_config(page_title="TikTok Scraper", page_icon="🎵", layout="wide")
store.init_db()

st.title("🎵 TikTok Scraper")
st.caption(
    "Pull ASMR clips from an account, trim them, and drop them into Drive "
    "pre-sorted into slot folders — ready to drag into the generator's sidebar."
)

# --------------------------------------------------------------------------- form

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

# --------------------------------------------------------------------------- submit

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

# --------------------------------------------------------------------------- history

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
