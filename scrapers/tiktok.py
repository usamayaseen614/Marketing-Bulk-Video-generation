"""
scrapers/tiktok.py — pulling clips from a TikTok profile.

`ClipSource` is the seam. yt-dlp is the implementation today; if TikTok tightens
up, a managed API (Apify, EnsembleData, Bright Data) becomes a second
implementation and nothing in the job runner changes.

Findings from testing this against a live profile, because they shaped the
design and are not obvious:

  * **Flat enumeration already carries the metadata.** view_count, like_count,
    comment_count, duration and timestamp all come back from the single
    playlist walk, so building the metadata sheet costs zero extra requests.
    Measured ~0.26s per entry, i.e. roughly 6-7 minutes to enumerate 1,500.

  * **Photo carousels cannot be filtered reliably up front.** They do not carry
    a `/photo/` URL in flat mode, and `formats` is empty for *every* flat entry,
    video or not. The only flat-mode hint is `duration is None`, and it is not
    definitive. What actually happens is that resolving one raises
    "Unable to extract universal data for rehydration". So the download loop
    tolerates per-clip failure and counts skips, rather than pre-filtering — and
    the real video count is reported back, since a profile advertising "1000
    posts" may hold far fewer actual videos.

  * **Extraction is flaky, so failures are worth retrying.** On a re-scrape of
    the same profile, 2 of 3 posts that had failed the first time downloaded
    fine. That is why only *successful* clips are remembered for dedup: a
    failure is not recorded as "seen", so the next scrape tries it again. Do
    not "optimise" this by blacklisting failed ids — it would quietly discard
    real videos.

  * **Datacenter IPs are the real risk.** All of the above was measured from a
    residential connection. TikTok blocks cloud egress far more aggressively,
    so a cookies file may be needed on the VM (BVG_SCRAPE_COOKIES_FILE).

There is also a single-link path (`fetch_single_video`) for grabbing one video
on its own. It shares the trimming and naming code with the profile scrape but
none of its machinery: no job row, no dedup ledger, no Drive upload, no rate
limiting — one link is one request, and there is nothing to pace.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Protocol

import config

logger = logging.getLogger(__name__)

_ACCOUNT_RE = re.compile(r"@([A-Za-z0-9_.]+)")

# /video/ and /photo/ are the two post types; /v/ is the old m.tiktok.com form.
_VIDEO_PATH_RE = re.compile(r"/(?:video|photo|v)/(\d+)")
# Share links carry no id at all — only TikTok's redirect knows what they are.
_SHORT_LINK_RE = re.compile(
    r"^https?://(?:v[mt]\.tiktok\.com/[A-Za-z0-9]+"
    r"|(?:www\.)?tiktok\.com/t/[A-Za-z0-9]+)", re.I)
_BARE_ID_RE = re.compile(r"^\d{8,}$")


class ScrapeError(RuntimeError):
    """Configuration or enumeration problems worth surfacing verbatim."""


def account_name(url_or_handle: str) -> str:
    """`https://www.tiktok.com/@someone?lang=en` -> `someone`."""
    match = _ACCOUNT_RE.search(url_or_handle or "")
    if match:
        return match.group(1)
    return (url_or_handle or "").strip().strip("/").split("/")[-1] or "unknown"


def profile_url(url_or_handle: str) -> str:
    raw = (url_or_handle or "").strip()
    if raw.startswith("http"):
        return raw.split("?")[0]
    return f"https://www.tiktok.com/@{account_name(raw)}"


# --------------------------------------------------------------------- one link

def video_url(raw: str) -> str:
    """Normalise a single-post link.

    The query string goes because TikTok's share button appends a tracking
    blob (`?is_from_webapp=1&sender_device=…`) that makes two links to the same
    video look different.

    A bare numeric id is accepted because that is what comes out of the
    metadata sheet's Video_ID column. `@_` stands in for the handle the
    canonical form wants: the id is what actually resolves the post."""
    text = (raw or "").strip()
    if not text:
        return ""
    if _BARE_ID_RE.match(text):
        return f"https://www.tiktok.com/@_/video/{text}"
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    return text.split("?")[0].split("#")[0].rstrip("/")


def is_video_url(raw: str) -> bool:
    """Does this point at ONE post rather than a whole profile?

    This is a guard, not a nicety. yt-dlp handed a profile URL happily walks
    the entire account, so without it a mis-paste would download hundreds of
    videos synchronously inside a page render."""
    text = (raw or "").strip()
    if not text:
        return False
    if _BARE_ID_RE.match(text):
        return True
    url = video_url(text)
    return bool(_SHORT_LINK_RE.match(url) or _VIDEO_PATH_RE.search(url))


def is_photo_url(raw: str) -> bool:
    """Photo carousels are not videos — see the module docstring. Worth
    catching from the URL when we can, so the user gets told why instead of
    watching an extraction fail."""
    return "/photo/" in video_url(raw).lower()


def video_id_from_url(raw: str) -> str:
    """The post id, or "" for a share link that only TikTok can resolve."""
    match = _VIDEO_PATH_RE.search(video_url(raw))
    return match.group(1) if match else ""


def explain_failure(message: str) -> str:
    """Turn yt-dlp's internals into something an operator can act on.

    "Unable to extract universal data for rehydration" is what a photo
    carousel looks like from the outside — see the module docstring."""
    text = str(message or "")
    if "rehydration" in text or "Unable to extract" in text:
        return "Not a downloadable video (photo carousel or removed post)"
    return text


@dataclass
class ClipInfo:
    """One post as seen during enumeration."""
    video_id: str
    url: str
    title: str = ""
    duration: Optional[float] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    repost_count: Optional[int] = None
    timestamp: Optional[int] = None
    uploader: str = ""
    # The sound behind the post. Only ever populated by a FULL extraction --
    # flat enumeration carries no music info at all, which is why the music
    # scrape can only learn what a post's sound is after downloading it.
    track: str = ""
    artists: str = ""
    album: str = ""

    @property
    def likely_photo(self) -> bool:
        """A hint, not a verdict — see the module docstring."""
        return self.duration is None

    def posted_date(self) -> str:
        if not self.timestamp:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(self.timestamp))


def _clip_from_info(info: dict, url: str, uploader_fallback: str = "") -> ClipInfo:
    """Map one yt-dlp info dict onto a ClipInfo.

    `url` is passed in rather than read here because the field to trust
    differs by extraction mode: a flat playlist entry puts the webpage link in
    `url`, while a full extraction puts the raw CDN media link there and the
    webpage link in `webpage_url`. Reading the wrong one gives a metadata sheet
    full of expiring CDN URLs."""
    return ClipInfo(
        video_id=str(info.get("id") or "").strip(),
        url=url,
        title=info.get("title") or "",
        duration=info.get("duration"),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        comment_count=info.get("comment_count"),
        repost_count=info.get("repost_count"),
        timestamp=info.get("timestamp"),
        uploader=info.get("uploader") or uploader_fallback,
        track=str(info.get("track") or "").strip(),
        artists=", ".join(info.get("artists") or []) or str(info.get("artist") or ""),
        album=str(info.get("album") or "").strip(),
    )


# TikTok's placeholder for "this creator's own audio". yt-dlp already collapses
# the English form ("original sound - somehandle") to this, and the localised
# forms are caught by the handle suffix instead -- see sound_key.
_GENERIC_SOUND = "original sound"


def sound_key(clip: ClipInfo) -> str:
    """A stable identity for the sound behind a post, or "" when TikTok did not
    name one specific enough to trust.

    Deduping a music scrape by sound name is the whole point of it: an account
    with 500 posts routinely draws on far fewer sounds, and byte-hashing cannot
    collapse them because each post clips the same track to a different length.

    But it must not over-collapse either. A creator's own audio is titled
    "original sound - <their handle>", which is one NAME across hundreds of
    genuinely different recordings -- so those return "" and fall back to the
    byte hash."""
    track = re.sub(r"\s+", " ", (clip.track or "").strip().lower())
    if not track or track == _GENERIC_SOUND:
        return ""
    handle = (clip.uploader or "").strip().lower()
    if handle and track.endswith(f"- {handle}"):
        return ""
    artists = re.sub(r"\s+", " ", (clip.artists or "").strip().lower())
    return track + chr(0x1f) + artists


def clip_stem(clip: ClipInfo) -> str:
    """`someaccount_7231234567890123456` — a filename that says what it is.

    The profile scrape names files by bare video id because the folder they
    land in already carries the account. A single fetch lands in the user's
    Downloads next to everything else they have ever downloaded, so the handle
    has to be in the name."""
    handle = re.sub(r"[^A-Za-z0-9_.-]", "", clip.uploader or "").strip("._-")
    return f"{handle}_{clip.video_id}" if handle else (clip.video_id or "tiktok")


class ClipSource(Protocol):
    """What the scrape runner needs from a clip provider."""

    def enumerate_clips(self, account_url: str, limit: int) -> list[ClipInfo]:
        ...

    def download(self, clip: ClipInfo, dest_dir: Path) -> Path:
        ...


# --------------------------------------------------------------------------- yt-dlp

class YtDlpSource:
    """yt-dlp implementation of ClipSource."""

    def __init__(self, cookies_file: Optional[str] = None):
        self.cookies_file = cookies_file or config.SCRAPE_COOKIES_FILE

    def _base_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": True,
        }
        if self.cookies_file and Path(self.cookies_file).is_file():
            opts["cookiefile"] = self.cookies_file
        return opts

    def _ydl(self, extra: dict):
        try:
            import yt_dlp
        except ImportError as exc:  # pragma: no cover
            raise ScrapeError(
                "yt-dlp is not installed. Install it with:  pip install yt-dlp"
            ) from exc
        opts = self._base_opts()
        opts.update(extra)
        return yt_dlp.YoutubeDL(opts)

    def enumerate_clips(self, account_url: str, limit: int) -> list[ClipInfo]:
        """Walk the profile newest-first — TikTok's natural order, so no sort."""
        url = profile_url(account_url)
        logger.info("Enumerating %s (cap %d)", url, limit)
        started = time.time()

        with self._ydl({"extract_flat": True, "playlistend": int(limit)}) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ScrapeError(
                f"Could not read the profile {url}. It may be private, renamed, "
                "or TikTok may be blocking this server's IP — try supplying a "
                "cookies file (BVG_SCRAPE_COOKIES_FILE)."
            )

        clips: list[ClipInfo] = []
        for entry in info.get("entries") or []:
            if not entry:
                continue
            video_id = str(entry.get("id") or "").strip()
            if not video_id:
                continue
            clips.append(_clip_from_info(
                entry,
                url=entry.get("url") or entry.get("webpage_url") or "",
                uploader_fallback=account_name(url),
            ))

        logger.info("Enumerated %d entries from %s in %.0fs",
                    len(clips), url, time.time() - started)
        return clips

    def download(self, clip: ClipInfo, dest_dir: Path) -> Path:
        """Fetch one clip. Raises on photo posts and anything else unplayable —
        the caller counts those as skips."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ignoreerrors must be OFF here: a silent failure would look like a
        # successful download of nothing.
        with self._ydl({"outtmpl": str(dest_dir / f"{clip.video_id}.%(ext)s"),
                        "format": config.SCRAPE_FORMAT,
                        "ignoreerrors": False}) as ydl:
            ydl.download([clip.url])

        return self._recover_audio(clip.url, _produced_file(dest_dir, clip.video_id),
                                   clip.video_id)

    def _recover_audio(self, url: str, path: Path, video_id: str) -> Path:
        """Re-fetch without H.265 if what landed has no audio track.

        TikTok's bytevc1 renditions sometimes arrive mute while advertising
        AAC, and nothing in the format metadata distinguishes those from the
        real thing — see config.SCRAPE_FORMAT. So the check has to happen on
        the bytes, after the fact.

        Costs a second download only for clips that came back silent. A post
        that is *genuinely* silent keeps its original, higher-quality file:
        the fallback is for TikTok's broken renditions, not for creators who
        posted without sound."""
        if not config.SCRAPE_REQUIRE_AUDIO or has_audio(path):
            return path

        logger.info("Clip %s came back with no audio — retrying without H.265",
                    video_id)
        alt_dir = path.parent / "_alt"
        shutil.rmtree(alt_dir, ignore_errors=True)
        alt_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._ydl({"outtmpl": str(alt_dir / f"{video_id}.%(ext)s"),
                            "format": config.SCRAPE_FORMAT_WITH_AUDIO,
                            "ignoreerrors": False}) as ydl:
                ydl.download([url])
            alt = _produced_file(alt_dir, video_id)
            if not has_audio(alt):
                # The post really has no sound. Keep the better original.
                logger.info("Clip %s has no audio in any rendition", video_id)
                return path
            path.unlink(missing_ok=True)
            recovered = path.parent / alt.name
            recovered.unlink(missing_ok=True)
            shutil.move(str(alt), recovered)
            logger.info("Clip %s: audio recovered via the H.264 rendition", video_id)
            return recovered
        except Exception:  # noqa: BLE001 — a failed rescue must not lose the clip
            logger.warning("Clip %s: audio recovery failed, keeping the silent file",
                           video_id, exc_info=True)
            return path
        finally:
            shutil.rmtree(alt_dir, ignore_errors=True)

    def download_audio(self, clip: ClipInfo, dest_dir: Path) -> tuple[Path, ClipInfo]:
        """Fetch just the sound of one post, as an MP3, plus what TikTok says it is.

        There is no way to fetch the ORIGINAL music file for an ordinary post:
        TikTok exposes `music.playUrl` only for audio-only slideshows, where
        yt-dlp finds no video formats at all. For everything else the audio has
        to come out of the post itself, so this is `bestaudio/best` plus the
        extract-audio postprocessor -- yt-dlp takes an audio-only rendition
        where TikTok offers one and strips the MP4 where it does not.

        Two things follow, and both are deliberate rather than oversights:
        bandwidth is the same as a video scrape whenever no audio rendition
        exists, and what lands is the post's full MIX (the sound plus any
        voiceover over it), not an isolated stem.

        Returns the enriched ClipInfo too. The track name only exists in a full
        extraction -- enumeration is flat and carries no music info -- and the
        sound name is what a music scrape deduplicates on.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ffmpeg_location, always: the postprocessor is what turns the download
        # into an MP3, and without this it looks for `ffmpeg` on PATH -- which
        # is not where this project's binary necessarily is (see find_ffmpeg).
        opts = {
            "outtmpl": str(dest_dir / f"{clip.video_id}.%(ext)s"),
            "format": config.SCRAPE_AUDIO_FORMAT,
            "ignoreerrors": False,
            "noplaylist": True,
            "ffmpeg_location": find_ffmpeg(),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": config.SCRAPE_AUDIO_CODEC,
                "preferredquality": config.SCRAPE_AUDIO_QUALITY,
            }],
        }
        with self._ydl(opts) as ydl:
            info = ydl.extract_info(clip.url, download=True)
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if not info:
            raise ScrapeError(f"TikTok returned nothing for {clip.url}")

        enriched = _clip_from_info(
            info, url=clip.url, uploader_fallback=clip.uploader)
        # Enumeration knows the view counts; the download knows the sound. Keep
        # both -- the metadata sheet wants the first and dedup wants the second.
        for field_name in ("view_count", "like_count", "comment_count",
                           "repost_count", "timestamp", "title"):
            if not getattr(enriched, field_name):
                setattr(enriched, field_name, getattr(clip, field_name))
        return _produced_file(dest_dir, clip.video_id), enriched

    def fetch_one(self, url: str, dest_dir: Path) -> tuple[ClipInfo, Path]:
        """Resolve *and* download a single post in one extraction.

        The profile path keeps those two steps apart for a reason: enumeration
        is one request covering hundreds of posts, and the downloads that
        follow are paced over the next hour. A single link has nothing to pace
        and nothing to enumerate, so splitting it would only mean asking TikTok
        about the same video twice.

        `playlist_items` is the safety net under `is_video_url`: a share link
        is opaque until TikTok redirects it, so if one ever resolves to a
        profile this caps the damage at one video instead of the whole
        account."""
        target = video_url(url)
        if not target:
            raise ScrapeError("No link given.")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # ignoreerrors OFF for the same reason as download(): this request IS
        # the one video, so a silent None is a failure the caller must hear.
        opts = {
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "format": config.SCRAPE_FORMAT,
            "ignoreerrors": False,
            "noplaylist": True,
            "playlist_items": "1",
        }
        with self._ydl(opts) as ydl:
            info = ydl.extract_info(target, download=True)

        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if not info:
            raise ScrapeError(
                f"Could not read {target}. The post may be private or deleted, "
                "or TikTok may be blocking this server's IP — try supplying a "
                "cookies file (BVG_SCRAPE_COOKIES_FILE)."
            )

        clip = _clip_from_info(
            info,
            url=info.get("webpage_url") or info.get("original_url") or target,
            uploader_fallback=account_name(target),
        )
        if not clip.video_id:
            raise ScrapeError(f"TikTok returned no video id for {target}.")
        produced = _produced_file(dest_dir, clip.video_id)
        return clip, self._recover_audio(clip.url or target, produced, clip.video_id)


def _produced_file(dest_dir: Path, video_id: str) -> Path:
    """The file yt-dlp just wrote, ignoring any abandoned `.part` beside it."""
    produced = [p for p in sorted(Path(dest_dir).glob(f"{video_id}.*"))
                if p.suffix.lower() != ".part"]
    if not produced:
        raise ScrapeError(f"No file produced for {video_id}")
    return produced[0]


# --------------------------------------------------------------------------- trim

def find_ffmpeg() -> str:
    """Reuse the renderer's resolution so the scraper and the renderer always
    agree on which FFmpeg binary is in play."""
    from video_generator import find_ffmpeg as _find
    return _find()


def _ff_capture() -> dict:
    """Same reason as the renderer's: scraped MP4s carry arbitrary metadata
    bytes that FFmpeg replays into stderr, and a bare text=True turns one of
    them into an uncatchable UnicodeDecodeError on a UTF-8 locale."""
    from video_generator import _FF_CAPTURE
    return dict(_FF_CAPTURE)


def split_clip(src: Path, dest_dir: Path, stem: str, start: float,
               duration: float, ffmpeg: Optional[str] = None,
               min_fraction: float = 0.5) -> list[Path]:
    """Cut a clip into consecutive segments, not just its opening window.

    A 40-second video with a 10-second window becomes four usable clips
    (1-11, 11-21, 21-31, 31-41) instead of one, so a scrape yields roughly four
    times the material for the same download and the same rate-limit budget.

    The first segment still starts at `start`, which is what skips the creator's
    intro branding. A trailing remnant shorter than `min_fraction` of the target
    is dropped — two-second scraps are not worth a slot.

    Returns the segments in order; the caller names and hashes each one."""
    ffmpeg = ffmpeg or find_ffmpeg()
    src = Path(src)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    total = probe_duration(src, ffmpeg)
    if total is None:
        # Unknown length: fall back to a single window rather than guessing.
        out = dest_dir / f"{stem}.mp4"
        trim_clip(src, out, start, duration, ffmpeg)
        return [out]

    begin = start if total > start else 0.0
    segments: list[Path] = []
    index = 0
    cursor = begin
    while cursor < total:
        remaining = total - cursor
        if remaining < duration * min_fraction and segments:
            break            # trailing scrap, and we already have something
        index += 1
        out = dest_dir / (f"{stem}.mp4" if index == 1 else f"{stem}_{index}.mp4")
        try:
            trim_clip(src, out, cursor, min(duration, remaining), ffmpeg)
            segments.append(out)
        except ScrapeError:
            # One bad segment must not lose the ones that worked.
            logger.warning("Segment %d of %s failed", index, src.name, exc_info=True)
            out.unlink(missing_ok=True)
            if not segments:
                raise
            break
        cursor += duration

    return segments


def trim_clip(src: Path, dest: Path, start: float, duration: float,
              ffmpeg: Optional[str] = None) -> Path:
    """Cut a clip to `duration` seconds starting at `start`, re-encoding.

    Re-encode rather than `-c copy` on purpose: stream copy cuts on keyframes,
    which would produce 8-12s clips instead of exactly 10s. These are short 9:16
    clips, so re-encoding is quick, and it drops a 1,500-clip scrape from ~5-7GB
    to ~2-3GB.

    This is a trim, not a filter — a clip shorter than the window is kept at
    whatever length it has. A clip shorter than `start` would otherwise produce
    an empty file, so the window slides back to 0 for those."""
    ffmpeg = ffmpeg or find_ffmpeg()
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    probed = probe_duration(src, ffmpeg)
    effective_start = start
    if probed is not None and probed <= start:
        # e.g. a 0.8s clip with a 1s start offset — keep the whole thing.
        effective_start = 0.0

    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-ss", f"{effective_start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    proc = subprocess.run(cmd, **_ff_capture(), timeout=180)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise ScrapeError(f"Trim failed for {src.name}: {tail}")
    return dest


def trim_audio(src: Path, start: float, duration: float,
               ffmpeg: Optional[str] = None) -> Path:
    """Cut an audio file to `duration` seconds starting at `start`, in place.

    NOT trim_clip: that re-encodes with libx264 into the source container, and
    an MP3 container refuses both a video stream and AAC. Stream copy instead of
    re-encoding, because unlike the video trim there is no keyframe problem to
    dodge -- MP3 frames are ~26ms apart, so `-ss` with `-c copy` cuts to within
    a frame, costs no quality, and is effectively free.

    Same trim-not-filter contract as trim_clip: a track shorter than `start`
    keeps its whole length (the window slides back to 0), and one shorter than
    the window is kept at whatever length it has."""
    ffmpeg = ffmpeg or find_ffmpeg()
    src = Path(src)

    probed = probe_duration(src, ffmpeg)
    effective_start = start
    if probed is not None and probed <= start:
        effective_start = 0.0

    dest = src.with_name(src.stem + ".trim" + src.suffix)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-ss", f"{effective_start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        str(dest),
    ]
    proc = subprocess.run(cmd, **_ff_capture(), timeout=180)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise ScrapeError(f"Trim failed for {src.name}: {tail}")
    dest.replace(src)
    return src


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(r"^\s*Stream #.*: Audio:", re.M)


def _probe_text(path: Path, ffmpeg: Optional[str] = None) -> str:
    """FFmpeg's description of a file. `ffmpeg -i` with no output is an error
    by design — everything interesting is on stderr either way."""
    ffmpeg = ffmpeg or find_ffmpeg()
    try:
        proc = subprocess.run([ffmpeg, "-i", str(path)],
                              **_ff_capture(), timeout=60)
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stderr or ""


def probe_duration(path: Path, ffmpeg: Optional[str] = None) -> Optional[float]:
    match = _DURATION_RE.search(_probe_text(path, ffmpeg))
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def has_audio(path: Path, ffmpeg: Optional[str] = None) -> bool:
    """Does this file carry an actual audio stream?

    The only trustworthy answer about a TikTok download. The format metadata
    says `acodec: aac` on renditions that arrive with no audio stream at all,
    so believing it is how a clip bank ends up full of silent ASMR."""
    return bool(_AUDIO_STREAM_RE.search(_probe_text(path, ffmpeg)))


def content_hash(path: Path, chunk: int = 1 << 20) -> str:
    """Hash of the first megabyte plus the size — enough to spot the same clip
    reposted under a new video id, without reading gigabytes."""
    path = Path(path)
    digest = hashlib.sha256()
    digest.update(str(path.stat().st_size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(chunk))
    return digest.hexdigest()


# --------------------------------------------------------------------- one video

@dataclass
class SingleFetch:
    """The result of grabbing one link: metadata plus files on disk."""
    clip: ClipInfo
    files: list[Path] = field(default_factory=list)
    trimmed: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files if p.is_file())


def fetch_single_video(url: str, dest_dir: Path,
                       trim_start: Optional[float] = None,
                       trim_duration: Optional[float] = None,
                       source: Optional[YtDlpSource] = None,
                       attempts: Optional[int] = None,
                       retry_delay: Optional[float] = None) -> SingleFetch:
    """Download one TikTok post into `dest_dir`, optionally trimmed.

    Deliberately none of the profile scrape's machinery: no job row, no dedup
    ledger (grabbing the same video twice on purpose is the whole point of a
    one-off fetch), no Drive upload, no rate limiting.

    `trim_duration=None` keeps the original untouched — the common case is
    wanting the video, not a slot-sized clip. When a window IS given the same
    `split_clip` the scrape uses runs, so a 40s post yields four clips here
    exactly as it would in a batch.

    `dest_dir` is emptied first: each fetch replaces the last, which is what
    keeps a session from accumulating videos nobody asked to keep.

    Retries where the profile scrape does not, and the asymmetry is the point.
    Extraction really is flaky: measured live, a link downloaded fine and then
    failed "Unable to extract universal data for rehydration" seconds later on
    the very next request. A scrape shrugs that off because failures are never
    remembered, so the next run over the account picks the clip up. One link
    gets exactly one chance, and the failure it reports — "photo carousel or
    removed post" — is a confident lie about a video that plainly exists."""
    if not is_video_url(url):
        raise ScrapeError(
            "That does not look like a link to a single video. Paste the URL "
            "of one post (…/@handle/video/1234…, or a vm.tiktok.com share "
            "link) — a profile link belongs in the account scraper."
        )
    if is_photo_url(url):
        raise ScrapeError(
            "That is a photo carousel, not a video — there is no video file "
            "on it to download."
        )

    dest_dir = Path(dest_dir)
    shutil.rmtree(dest_dir, ignore_errors=True)
    raw_dir = dest_dir / "raw"

    source = source or YtDlpSource()
    tries = max(1, int(config.SINGLE_FETCH_ATTEMPTS if attempts is None else attempts))
    pause = (config.SINGLE_FETCH_RETRY_DELAY if retry_delay is None
             else float(retry_delay))
    failure: Optional[Exception] = None

    for attempt in range(1, tries + 1):
        # Cleared each time so a half-written file from the previous attempt
        # cannot be mistaken for this one's download.
        shutil.rmtree(raw_dir, ignore_errors=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            clip, raw = source.fetch_one(url, raw_dir)
            break
        except Exception as exc:  # noqa: BLE001 — photo posts and dead links too
            failure = exc
            logger.info("Single fetch %s attempt %d/%d failed: %s",
                        url, attempt, tries, str(exc)[:160])
            if attempt < tries and pause > 0:
                time.sleep(pause * attempt)
    else:
        reason = (explain_failure(str(failure)) or "TikTok refused the link").rstrip(". ")
        if tries > 1:
            reason += (f". Gave up after {tries} tries — TikTok's extraction is "
                       "flaky, so the same link may well work in a minute")
        raise ScrapeError(reason + ".") from failure

    stem = clip_stem(clip)

    if trim_duration is None:
        # Keep the original bytes, just under a name worth having in Downloads.
        final = dest_dir / f"{stem}{raw.suffix or '.mp4'}"
        shutil.move(str(raw), final)
        files = [final]
        trimmed = False
    else:
        files = split_clip(raw, dest_dir, stem,
                           float(trim_start or 0.0), float(trim_duration))
        raw.unlink(missing_ok=True)
        trimmed = True

    shutil.rmtree(raw_dir, ignore_errors=True)
    logger.info("Single fetch %s -> %d file(s)%s",
                clip.video_id, len(files), " (trimmed)" if trimmed else "")
    return SingleFetch(clip=clip, files=files, trimmed=trimmed)


def prune_fetch_dirs(root: Path, max_age_hours: float) -> int:
    """Delete single-fetch folders nobody has touched in a while.

    These live under a `_`-prefixed path so store.reap_old_jobs() leaves them
    alone (it deletes any folder in JOBS_ROOT without a matching job row, which
    would otherwise yank a video out from under someone mid-download). That
    exemption is also why they need their own reaper: no other one looks here."""
    root = Path(root)
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0.0, float(max_age_hours)) * 3600
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


# --------------------------------------------------------------------------- planning

@dataclass
class PlannedClip:
    video_id: str
    batch: int          # 1-based; 0 in dump mode
    slot: int           # 1-based; 0 in dump mode
    position: int       # position within the batch


def plan_batches(video_ids: list[str], n_batches: int,
                 batch_size: Optional[int] = None,
                 slots: Optional[int] = None) -> list[PlannedClip]:
    """Lay clips out as batch_NN / slot_N.

    Round-robin within a batch: video 1 to slot 1, 2 to slot 2 ... 6 back to
    slot 1, so 50 clips across 5 slots give 10 per slot.

    When batches x batch_size exceeds the number of clips available the list
    wraps — but each wrap is **reshuffled**, so batch 21 is not a copy of batch
    1. The shuffle is seeded by pass number, so re-running the same scrape
    produces the same plan."""
    batch_size = batch_size or config.SCRAPE_BATCH_SIZE
    slots = slots or config.SCRAPE_SLOTS
    pool = list(dict.fromkeys(video_ids))   # de-dupe, keep newest-first order
    if not pool or n_batches <= 0:
        return []

    deck: list[str] = []
    pass_no = 0

    def refill() -> None:
        """Start a fresh pass over the pool, shuffled every time.

        Shuffling the FIRST pass too is the point. Clips arrive newest-first
        (and are sorted by view count for curation), so dealing them
        positionally gave slot 1 the strongest clips and slot 5 the weakest,
        every single batch. Shuffling first makes the slot a clip lands in
        genuinely random; dealing without replacement keeps any clip from
        appearing twice and keeps the slots evenly filled.

        Seeded by pass number, so a resumed job re-derives the same layout."""
        nonlocal deck, pass_no
        chunk = list(pool)
        random.Random(f"deal-{pass_no}-{len(pool)}").shuffle(chunk)
        pass_no += 1
        deck = chunk

    # A clip must not appear twice in the same batch while there are unused
    # clips left to pick — otherwise a small pool produces a batch holding the
    # same video a dozen times, which is useless as a clip bank. Repetition is
    # only accepted when the pool genuinely has fewer clips than a batch holds.
    picky = len(pool) >= batch_size

    planned: list[PlannedClip] = []
    for batch_index in range(n_batches):
        chosen: list[str] = []
        used: set[str] = set()
        while len(chosen) < batch_size:
            if not deck:
                refill()
            index = 0
            if picky:
                index = next((i for i, v in enumerate(deck) if v not in used), -1)
                if index < 0:
                    # This pass is exhausted of clips this batch hasn't used.
                    refill()
                    index = next((i for i, v in enumerate(deck) if v not in used), 0)
            video_id = deck.pop(index)
            chosen.append(video_id)
            used.add(video_id)

        for position, video_id in enumerate(chosen):
            planned.append(PlannedClip(
                video_id=video_id,
                batch=batch_index + 1,
                slot=(position % slots) + 1,
                position=position + 1,
            ))
    return planned


def plan_dump(video_ids: list[str]) -> list[PlannedClip]:
    """Dump mode: one flat folder for manual curation."""
    return [PlannedClip(video_id=v, batch=0, slot=0, position=i + 1)
            for i, v in enumerate(video_ids)]
