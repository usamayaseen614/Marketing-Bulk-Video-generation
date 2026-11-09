"""
scrapers/youtube.py — pulling clips from a YouTube channel.

yt-dlp does all of it, so this is TikTok's YtDlpSource with YouTube's links and
YouTube's format choice. Measured live 2026-10-07 (residential IP, no cookies):

  * **Shorts by default.** The clip bank wants 9:16, and a channel's /shorts tab
    is exactly that. A pasted /videos, /streams or playlist link is used as-is.

  * **Flat enumeration is cheap but thin.** 30 Shorts in 1-2 s, each with a
    title and a view count parsed from the "46M views" label — no duration, no
    likes, no date. /videos entries do carry a duration, which is what the
    long-video skip (BVG_SCRAPE_YT_MAX_DURATION) reads.

  * **Video and audio are separate streams, so ffmpeg merges them.** The best
    H.264 Short is 1080x1920 + m4a (137+140). Without ffmpeg_location yt-dlp
    quietly settles for the 360p progressive file instead.

  * **deno.** yt-dlp has deprecated JS-less YouTube extraction; the `deno`
    extra in requirements.txt puts the binary beside Python's scripts, where
    yt-dlp finds it on its own. Today every usable format came from a client
    that needs no JS at all, so this is insurance rather than a requirement.

  * **Datacenter IPs** are sometimes told "Sign in to confirm you're not a
    bot". If the VM sees that, add `youtube` to BVG_PROXY_PLATFORMS.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import config
from scrapers import tiktok
from scrapers.proxy import url_for as proxy_url_for
from scrapers.tiktok import ClipInfo, ScrapeError, SingleFetch

logger = logging.getLogger(__name__)

_TABS = ("shorts", "videos", "streams")
_ID = r"[A-Za-z0-9_-]{11}"
_VIDEO_RE = re.compile(rf"(?:/shorts/|/live/|/embed/|youtu\.be/|[?&]v=)({_ID})(?![\w-])")
_BARE_ID_RE = re.compile(rf"^{_ID}$")
_CHANNEL_RE = re.compile(r"youtube\.com/(@[^/?#]+|(?:channel|c|user)/[^/?#]+)", re.I)
_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def account_name(raw: str) -> str:
    """`youtube.com/@Some.One/shorts` -> `some.one`; a /channel/ link gives its
    UC… id, a playlist link its list id.

    Handles are lowercased because YouTube ignores their case and the dedup
    history is keyed on this — @MrBeast and @mrbeast are one channel. Channel
    and playlist ids are case-sensitive, so they are left alone."""
    text = unquote((raw or "").strip())
    match = _CHANNEL_RE.search(text)
    if match:
        name = match.group(1).split("/")[-1]
        return name[1:].lower() if name.startswith("@") else name
    match = _LIST_RE.search(text)
    if match:
        return match.group(1)
    tail = text.strip("/").split("/")[-1]
    if tail.startswith("UC") and len(tail) == 24:
        return tail
    return tail.lstrip("@").lower() or "unknown"


def profile_url(raw: str) -> str:
    """The listing to walk. A channel without a tab gets /shorts."""
    text = (raw or "").strip()
    if _LIST_RE.search(text):              # a playlist is its own source
        return text if text.startswith("http") else "https://" + text
    match = _CHANNEL_RE.search(text)
    if match:
        rest = text[match.end():].split("?")[0].split("#")[0].strip("/").split("/")
        tab = rest[0].lower() if rest[0].lower() in _TABS else "shorts"
        return f"https://www.youtube.com/{match.group(1)}/{tab}"
    handle = account_name(text)
    if handle.startswith("UC") and len(handle) == 24:
        return f"https://www.youtube.com/channel/{handle}/shorts"
    return f"https://www.youtube.com/@{handle}/shorts"


def video_id_from_url(raw: str) -> str:
    text = (raw or "").strip()
    if _BARE_ID_RE.match(text):
        return text
    match = _VIDEO_RE.search(text)
    return match.group(1) if match else ""


def video_url(raw: str) -> str:
    """One video's canonical link. Everything but the id goes — `&list=` in
    particular, which would turn a single fetch into a playlist walk."""
    video_id = video_id_from_url(raw)
    if not video_id:
        return (raw or "").strip()
    if "/shorts/" in (raw or ""):
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def is_video_url(raw: str) -> bool:
    """One video, not a channel — the guard that keeps a pasted channel link
    from being walked inside a page render."""
    return bool(video_id_from_url(raw))


def _refuse_live(info: dict, *, incomplete: bool = False) -> None:
    """yt-dlp match_filter: a stream that is on air has no end, so recording
    it would never finish — inside a page render, for a single fetch. Raising
    (rather than returning a skip reason) is what makes yt-dlp fail with this
    message instead of quietly producing no file."""
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ScrapeError(f"{info.get('id') or 'That video'} is a live stream — "
                          "there is no file to download until it has ended.")


class YouTubeSource(tiktok.YtDlpSource):
    PLATFORM = "youtube"
    NAME = "YouTube"
    video_url = staticmethod(video_url)
    account_name = staticmethod(account_name)

    def _video_opts(self) -> dict:
        return {"format": config.SCRAPE_YT_FORMAT, "merge_output_format": "mp4",
                "ffmpeg_location": tiktok.find_ffmpeg(),
                "match_filter": _refuse_live}

    def _recover_audio(self, url: str, path: Path, video_id: str) -> Path:
        """TikTok's mute-H.265 problem has no YouTube equivalent: the audio is
        its own stream, merged in. A silent file here is a silent upload, and
        TikTok's fallback format would only re-fetch it at 360p."""
        return path

    def enumerate_clips(self, account_url: str, limit: int) -> list[ClipInfo]:
        url = profile_url(account_url)
        logger.info("Enumerating %s (cap %d)", url, limit)
        with self._ydl({"extract_flat": True, "playlistend": int(limit)}) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ScrapeError(
                f"Could not read {url}. Check the channel link — and if the "
                "channel has no Shorts, paste its /videos link instead. From a "
                "cloud server YouTube may also be asking to confirm you're not "
                "a bot: add youtube to BVG_PROXY_PLATFORMS."
            )

        handle = account_name(url)
        clips, too_long = [], 0
        for entry in info.get("entries") or []:
            video_id = str((entry or {}).get("id") or "").strip()
            if not video_id:
                continue
            # A stream on air lists with NO duration, so the length test alone
            # would wave it through to a download that never ends.
            if ((entry.get("duration") or 0) > config.SCRAPE_YT_MAX_DURATION
                    or entry.get("live_status") in ("is_live", "is_upcoming")):
                too_long += 1
                continue
            clips.append(tiktok._clip_from_info(
                entry, url=entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                uploader_fallback=handle))
        if too_long:
            logger.info("Skipped %d live or over-%gs video(s) on %s",
                        too_long, config.SCRAPE_YT_MAX_DURATION, url)
        logger.info("Enumerated %d entries from %s", len(clips), url)
        return clips


def make_source(account: str = "") -> YouTubeSource:
    return YouTubeSource(proxy=proxy_url_for("youtube", account))


def fetch_single_video(url: str, dest_dir: Path,
                       trim_start: Optional[float] = None,
                       trim_duration: Optional[float] = None,
                       source: Optional[YouTubeSource] = None,
                       attempts: Optional[int] = None,
                       retry_delay: Optional[float] = None) -> SingleFetch:
    """One video, whole or split — see tiktok.fetch_single_video."""
    if not is_video_url(url):
        raise ScrapeError(
            "That does not look like a link to a single video. Paste one "
            "video's link (…/shorts/…, watch?v=…, youtu.be/…) — a channel link "
            "belongs in the account scraper."
        )
    source = source or YouTubeSource(proxy=proxy_url_for("youtube", url))
    return tiktok.fetch_and_trim(url, dest_dir, source, trim_start, trim_duration,
                                 attempts, retry_delay, who="YouTube")
