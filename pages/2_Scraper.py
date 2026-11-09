"""
pages/2_Scraper.py — build the clip bank from TikTok, Instagram or YouTube.

The platform is picked once at the top and both tabs follow it. Underneath,
each one is a module in scrapers/ with the same handful of functions, so
everything below is written once.

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

from i18n import t, localize_format, language_header, option_labels

import config as settings
import scrapers
from jobs import store
from scrapers import tiktok
from ui_common import get_session_id, offer_file_download

st.set_page_config(page_title=t("Scraper"), page_icon="🎵", layout="wide")
store.init_db()

language_header()

st.title(t("🎵 Scraper"))

platform = st.radio(
    t("Platform"), options=list(scrapers.PLATFORMS),
    format_func=option_labels(list(scrapers.PLATFORMS), localize_format(scrapers.PLATFORMS.get)), horizontal=True, key="scrape_platform",
)
NAME = scrapers.PLATFORMS[platform]
pmod = scrapers.module(platform)

# (profile example, single-video example)
EXAMPLES = {
    "tiktok": ("https://www.tiktok.com/@someaccount",
               "https://www.tiktok.com/@someaccount/video/7231234567890123456"),
    "instagram": ("https://www.instagram.com/someaccount/",
                  "https://www.instagram.com/reel/DZ-dSUgSRiy/"),
    "youtube": ("https://www.youtube.com/@somechannel",
                "https://www.youtube.com/shorts/BfEVQW9Uc7I"),
}

if platform == "instagram":
    st.caption(
        t("Reels only — what Instagram shows a logged-out visitor on the "
        "profile's Reels tab, pinned reels first. Each reel lands as a "
        "720×1280 MP4.")
    )
elif platform == "youtube":
    st.caption(
        t("The channel's **Shorts** tab — or paste a link to its /videos or /streams tab, or a "
          "playlist, to pull that instead. Videos longer than {0:g}s are skipped: every clip is "
          "cut into 10s segments, and one hour-long upload would be 360 of them.", settings.SCRAPE_YT_MAX_DURATION)
    )

if platform in settings.PROXY_PLATFORMS and not settings.proxy_configured():
    st.warning(
        t("**No proxy configured for {0}.** Fine from a home connection, but from a cloud server"
          " {1} serves its pages with the data stripped out, so a scrape from the VM will fail. "
          "Set `BVG_DECODO_USER` and `BVG_DECODO_PASS` (the social-media dashboard's Decodo "
          "account) in the VM's `.env` — see the Setup page.", NAME, NAME)
    )

tab_account, tab_single = st.tabs([t("📁 Whole account"), t("🔗 Single video")])

# ========================================================================= account

with tab_account:
    st.caption(
        t("Pull ASMR clips from an account on {0}, trim them, and drop them into Drive "
          "pre-sorted into slot folders — ready to drag into the generator's sidebar.", NAME)
    )

    account = st.text_input(
        t("{0} account URL or @handle", NAME),
        placeholder=t(EXAMPLES[platform][0]),
        help=t("The profile to pull from, newest posts first."),
     key="_ui_2_Scraper_90_" + str(f"{NAME} account URL or @handle"))
    # A bare @handle says nothing about where it lives; a pasted link does,
    # and scraping @bob on the wrong platform would quietly pull a stranger.
    linked = scrapers.detect(account, default=platform)
    if linked != platform:
        st.warning(t("That's a {0} link — switch **Platform** above to {1}.", scrapers.PLATFORMS[linked], scrapers.PLATFORMS[linked]))
    account_problem = "" if linked != platform else scrapers.not_an_account(platform, account)
    if account_problem:
        st.warning(t(account_problem))

    asset = st.radio(
        t("What to pull"),
        options=["video", "music"],
        format_func=option_labels(["video", "music"], lambda a: {
            "video": t("Videos — clips, trimmed and sorted into slot folders"),
            "music": t("Overlay music — just the sound behind each post, as MP3"),
        }[a]),
        horizontal=True,
        help=t("Music mode gives you a folder of tracks to drop under your own "
             "videos, rather than a clip bank."),
     key='_ui_2_Scraper_what_to_pull')
    is_music = asset == "music"
    if is_music:
        st.info(
            t("**What actually lands.** {0} does not publish the original music file for a "
              "normal post, so each track is pulled out of the post itself — which means it is "
              "that post's full audio (the sound plus any voiceover over it), not an isolated "
              "stem, and it costs the same bandwidth as downloading the video would.\n\nPosts "
              "sharing a sound are collapsed to one file where the platform identifies the sound"
              " (TikTok names it, Instagram gives it an id), so an account with 500 posts "
              "usually yields far fewer tracks. Where it does not (TikTok's “original sound”, "
              "most YouTube Shorts) tracks are deduplicated by content instead, and "
              "near-identical ones can slip through.\n\nMusic scrapes keep their own history, so "
              "this will not skip anything just because the account was scraped for videos "
              "before.", NAME)
        )

    mode = st.radio(
        t("Output layout"),
        options=["batch", "dump"],
        format_func=option_labels(["batch", "dump"], lambda m: {
            "batch": t("Batches — pre-split into batch_NN/slot_N folders"),
            "dump": t("Dump — one flat folder for manual curation"),
        }[m]),
        help=t("Batch mode does the sorting for you: 50 clips per batch, "
             "round-robin across the slots, 10 clips per slot."),
        disabled=is_music,
        captions=([t("Not used for music — tracks land in one folder."), ""]
                  if is_music else t(None)),
     key='_ui_2_Scraper_output_layout')
    if is_music:
        mode = "dump"

    col_a, col_b, col_c = st.columns(3)

    if mode == "batch":
        n_batches = col_a.number_input(
            t("How many batches"), 1, 100, 4, 1,
            help=t("Each batch is {0} clips across {1} slots. If you ask for more clips than the"
                   " account has, the list wraps around — but each wrap is reshuffled, so later "
                   "batches aren't copies of the first ones.", settings.SCRAPE_BATCH_SIZE, settings.SCRAPE_SLOTS),
         key='_ui_2_Scraper_how_many_batches')
    else:
        n_batches = 0
        col_a.caption(t("Dump mode — every clip in one folder."))

    limit = col_b.number_input(
        t("Max videos to scan"), 10, 5000, settings.SCRAPE_MAX_VIDEOS, 10,
        help=t("Cap on how far back the profile is walked. Listing runs at "
             "roughly 0.26s per post on TikTok and 0.4s on Instagram (YouTube "
             "is faster), so 1,500 takes 6-10 minutes before any downloading "
             "starts."),
     key='_ui_2_Scraper_max_videos_to_scan')

    skip_known = col_c.checkbox(
        t("Skip clips already scraped"), value=True,
        help=t("Deduplicates by video ID and content hash, so re-scraping an account "
             "next month only pulls what's new."),
     key='_ui_2_Scraper_skip_clips_already_scraped')

    st.subheader(t("Trim window"))
    if is_music:
        # Opt-in where the video trim is mandatory: the default deliverable is
        # the whole track, and a window is for cutting intros or capping length
        # when you want it -- not something a default should do to every song.
        trim_music = st.checkbox(
            t("Trim each track"), value=False,
            help=t("Off = every track lands whole (the default). On = each track "
                 "is cut to the window below — a trim, not a filter, so a "
                 "track shorter than the window is kept at whatever length it "
                 "has, and one shorter than the start offset is kept whole."),
         key='_ui_2_Scraper_trim_each_track')
        if trim_music:
            mcol_start, mcol_dur = st.columns(2)
            trim_start = mcol_start.number_input(
                t("Start at (s)"), 0.0, 600.0, 0.0, 0.5,
                help=t("Skip this much of each track's opening — e.g. a spoken intro before the beat."),
                key="music_trim_start")
            trim_duration = mcol_dur.number_input(
                t("Length (s)"), 1.0, 600.0, 60.0, 1.0,
                help=t("How much of each track to keep, from the start offset."),
                key="music_trim_duration")
            st.caption(t("→ keeping {0:g}s to {1:g}s of each track.", trim_start, trim_start + trim_duration))
        else:
            trim_start, trim_duration = 0.0, 0.0
    else:
        st.caption(
            t("Every clip is cut to this window — a trim, not a filter, so nothing "
            "is dropped for being too long. Starting at 1s skips most creator "
            "intro branding and on-screen text, which would otherwise ride into "
            "your output.")
        )
        col_start, col_dur = st.columns(2)
        trim_start = col_start.number_input(
            t("Start at (s)"), 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5, key='_ui_2_Scraper_start_at_s')
        trim_duration = col_dur.number_input(
            t("Length (s)"), 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5, key='_ui_2_Scraper_length_s')
        st.caption(t("→ keeping {0:g}s to {1:g}s of each clip.", trim_start, trim_start + trim_duration))

    drive_folder = st.text_input(
        t("Google Drive folder link (optional)"), value="",
        placeholder=t("https://drive.google.com/drive/folders/…"),
        help=t("Send this scrape somewhere specific. Blank uses the server default."),
     key='_ui_2_Scraper_google_drive_folder_link_optional')
    notify_email = st.text_input(
        t("Notify email (optional)"), value=", ".join(settings.MAIL_TO),
        placeholder=t("you@yourcompany.com"),
     key='_ui_2_Scraper_notify_email_optional')

    if not settings.drive_configured():
        st.warning(
            t("Google Drive isn't configured, so clips will stay on the VM instead of "
            "being uploaded. See the Setup page.")
        )

    # ----------------------------------------------------------------- submit

    estimate_min = (int(limit) * 0.26 + (n_batches or 20) * 50 * 1.5) / 60
    st.caption(
        t("Rough estimate: ~{0:.0f} min. Downloading is rate-limited to about one clip every "
          "{1:g}-{2:g}s on purpose — going faster gets the server's IP blocked.", estimate_min, settings.SCRAPE_MIN_DELAY, settings.SCRAPE_MAX_DELAY)
    )

    if st.button(t("🚀 Start scrape"), type="primary",
                 disabled=not account.strip() or linked != platform
                 or bool(account_problem), key='_ui_2_Scraper_start_scrape'):
        handle = pmod.account_name(account)
        job_id = store.new_job_id()
        store.make_job_dirs(job_id)
        store.create_job(
            kind=store.KIND_SCRAPE,
            params={
                "account": account.strip(),
                "platform": platform,
                "asset": asset,
                "mode": mode,
                "batches": int(n_batches),
                "limit": int(limit),
                "trim_start": float(trim_start),
                "trim_duration": float(trim_duration),
                "skip_known": bool(skip_known),
                "drive_folder": drive_folder.strip(),
            },
            label=("" if platform == "tiktok" else f"{platform}-")
                  + f"{'music' if is_music else 'scrape'}-{handle}",
            notify_email=notify_email.strip(),
            submitted_by=get_session_id(),
            job_id=job_id,
        )
        st.success(
            t("**Queued a {0} {1} of @{2}.** You can close this tab — it runs in the background", NAME, t('music scrape' if is_music else 'scrape'), handle)
            + (t(" and emails {0} when it's done.", notify_email.strip())
               if notify_email.strip() else ".")
        )
        st.page_link("pages/1_Jobs.py", label=t("📋 Track progress on the Jobs page"),
                     icon="➡️")

    # ---------------------------------------------------------------- history

    st.divider()
    st.subheader(t("Accounts scraped before"))

    history = store.scraped_accounts()
    if not history:
        st.caption(t("None yet."))
    else:
        import time as _time
        for row in history:
            cols = st.columns([3, 2, 2, 1], vertical_alignment="center")
            # Ledger keys are "instagram:bob" / "youtube:bob"; TikTok's are bare.
            where, _, name = row["account"].rpartition(":")
            cols[0].markdown(f"**{scrapers.PLATFORMS.get(where, 'TikTok')} · @{name}**")
            cols[1].caption(t("{0} clips remembered", row['clips']))
            cols[2].caption(
                t("last ") + t(_time.strftime("%Y-%m-%d", _time.localtime(row["last_seen"])))
                if row["last_seen"] else "")
            if cols[3].button(t("Forget"), key=f"forget_{row['account']}",
                              help=t("Clear the dedup history so the next scrape "
                                   "re-pulls everything from this account.")):
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
        t("One link in, one file out. This runs right here instead of queueing a "
        "job, and nothing goes to Drive — the download button below is the "
        "delivery.")
    )

    video_link = st.text_input(
        t("{0} video link", NAME),
        placeholder=t(EXAMPLES[platform][1]),
        help=t("A link to a single post")
             + (t(" — share links (vm.tiktok.com/…) work too") if platform == "tiktok" else "")
             + t(". A bare video ID copied out of a scrape's metadata sheet works "
               "as well. A profile link belongs in the other tab."),
     key="_ui_2_Scraper_323_" + str(f"{NAME} video link"))

    want_trim = st.checkbox(
        t("Trim it to a window"), value=False,
        help=t("Off by default: a one-off grab usually wants the whole video. "
             "Tick this to cut it the same way a batch scrape would — including "
             "the split into consecutive segments, so a 40s post gives you four "
             "clips instead of one."),
     key='_ui_2_Scraper_trim_it_to_a_window')

    if want_trim:
        col_s, col_d = st.columns(2)
        single_start = col_s.number_input(
            t("Start at (s)"), 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5,
            key="single_trim_start")
        single_duration = col_d.number_input(
            t("Length (s)"), 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5,
            key="single_trim_duration")
        st.caption(
            t("→ cutting from {0:g}s in, {1:g}s per segment, until the video runs out.", single_start, single_duration)
        )
    else:
        single_start = single_duration = None

    link_ok = (pmod.is_video_url(video_link)
               and scrapers.detect(video_link, default=platform) == platform)
    if video_link.strip() and not link_ok:
        st.warning(
            t("That doesn't look like a link to a single {0} post. Use the **Whole account** tab"
              " for a profile, or switch **Platform** above for another site's link.", NAME)
        )

    if st.button(t("⬇️ Fetch video"), type="primary", key="fetch_single",
                 disabled=not link_ok):
        st.session_state.pop("single_fetch", None)
        try:
            # Can run to ~15s: extraction fails intermittently on links that
            # are perfectly fine, so this retries before believing it.
            with st.spinner(t("Asking {0} for that video…", NAME)):
                fetched = pmod.fetch_single_video(
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
            st.error(t(str(exc)))
        except Exception as exc:  # noqa: BLE001 — yt-dlp raises its own zoo
            # explain_failure decodes TikTok's errors; elsewhere it would
            # call a perfectly ordinary YouTube failure a photo carousel.
            st.error(t("That fetch failed: ") + (t(tiktok.explain_failure(str(exc)))
                                              if platform == "tiktok" else t(str(exc))))
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
    # the button was not pressed, and re-fetching would hit the site twice for
    # one click.

    saved = st.session_state.get("single_fetch")
    if saved:
        files = [Path(p) for p in saved["files"]]
        live = [p for p in files if p.is_file()]

        if not live:
            # The TTL reaper got here first, or the session folder was reused.
            st.info(t("Those files have been cleaned up. Fetch the link again."))
            st.session_state.pop("single_fetch", None)
        else:
            clip = saved["clip"]
            st.success(
                t("Got it — {0} file{1}", len(live), t('s' if len(live) != 1 else ''))
                + (t(" (trimmed)") if saved["trimmed"] else t(" (original, untrimmed)"))
            )

            # Worth saying out loud rather than leaving to be discovered on
            # playback: the recovery re-fetch already ran and still came up
            # empty, so this post genuinely has no sound.
            if not saved.get("has_audio", True):
                st.warning(
                    t("This clip has **no audio track** — the post itself has no "
                    "sound. Not much use if you're after ASMR.")
                )

            if clip.title:
                st.markdown(f"**{clip.title}**")
            byline = f"@{clip.uploader}" if clip.uploader else ""
            posted = clip.posted_date()
            st.caption(t(" · ".join(x for x in [byline, posted, clip.url] if x)))

            stats = st.columns(4)
            stats[0].metric(t("Views"), _count(clip.view_count))
            stats[1].metric(t("Likes"), _count(clip.like_count))
            stats[2].metric(t("Comments"), _count(clip.comment_count))
            stats[3].metric(
                t("Length"),
                f"{clip.duration:g}s" if clip.duration else "—")

            st.divider()

            bundle = Path(saved["bundle"]) if saved["bundle"] else None
            if bundle and bundle.is_file():
                offer_file_download(
                    bundle, f"single_{clip.video_id}_zip",
                    t("⬇️ Download all {0} segments (ZIP)", len(live)),
                    mime="application/zip")
                st.caption(t("…or grab them one at a time:"))

            for n, path in enumerate(live, start=1):
                label = (t("⬇️ Download video") if len(live) == 1
                         else t("⬇️ Segment {0} — {1}", n, path.name))
                offer_file_download(path, f"single_{clip.video_id}_{n}", label,
                                    mime="video/mp4")


# The single-fetch folders are the one thing in JOBS_ROOT no job row points at,
# so no other reaper will ever take them. Swept here, at the end, so a slow
# rmtree never sits between the user's click and their download button.
tiktok.prune_fetch_dirs(settings.SINGLE_FETCH_ROOT, settings.SINGLE_FETCH_TTL_HOURS)
