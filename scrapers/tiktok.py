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
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Protocol

import config

logger = logging.getLogger(__name__)

_ACCOUNT_RE = re.compile(r"@([A-Za-z0-9_.]+)")


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

    @property
    def likely_photo(self) -> bool:
        """A hint, not a verdict — see the module docstring."""
        return self.duration is None

    def posted_date(self) -> str:
        if not self.timestamp:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(self.timestamp))


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
            clips.append(ClipInfo(
                video_id=video_id,
                url=entry.get("url") or entry.get("webpage_url") or "",
                title=entry.get("title") or "",
                duration=entry.get("duration"),
                view_count=entry.get("view_count"),
                like_count=entry.get("like_count"),
                comment_count=entry.get("comment_count"),
                repost_count=entry.get("repost_count"),
                timestamp=entry.get("timestamp"),
                uploader=entry.get("uploader") or account_name(url),
            ))

        logger.info("Enumerated %d entries from %s in %.0fs",
                    len(clips), url, time.time() - started)
        return clips

    def download(self, clip: ClipInfo, dest_dir: Path) -> Path:
        """Fetch one clip. Raises on photo posts and anything else unplayable —
        the caller counts those as skips."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        template = str(dest_dir / f"{clip.video_id}.%(ext)s")

        # ignoreerrors must be OFF here: a silent failure would look like a
        # successful download of nothing.
        with self._ydl({"outtmpl": template, "format": "mp4/best",
                        "ignoreerrors": False}) as ydl:
            ydl.download([clip.url])

        produced = sorted(dest_dir.glob(f"{clip.video_id}.*"))
        produced = [p for p in produced if p.suffix.lower() != ".part"]
        if not produced:
            raise ScrapeError(f"No file produced for {clip.video_id}")
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


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(path: Path, ffmpeg: Optional[str] = None) -> Optional[float]:
    ffmpeg = ffmpeg or find_ffmpeg()
    try:
        proc = subprocess.run([ffmpeg, "-i", str(path)],
                              **_ff_capture(), timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def content_hash(path: Path, chunk: int = 1 << 20) -> str:
    """Hash of the first megabyte plus the size — enough to spot the same clip
    reposted under a new video id, without reading gigabytes."""
    path = Path(path)
    digest = hashlib.sha256()
    digest.update(str(path.stat().st_size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(chunk))
    return digest.hexdigest()


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
