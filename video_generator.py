"""
video_generator.py — Core rendering engine for the bulk marketing video generator.

Design overview
---------------
Each output video is a four-layer sandwich, composited by FFmpeg in a single pass:

    layer 0 (bottom): base.png    — the row's background image, cover-cropped to 1080x1920
    layer 1 (middle): video.mp4   — the uploaded promo video, scaled into a configurable box
    layer 2:          overlay.png — transparent PNG with the three text elements
    layer 3 (top):    cta.png     — the CTA image, faded in over CTA_FADE_START..+DURATION

The static layers are pre-rendered with Pillow because:
  * Text is rasterized once per row instead of once per frame (far faster than drawtext).
  * It avoids FFmpeg drawtext fontfile path-escaping issues on Windows.
  * The FFmpeg filter graph stays identical for every row, which keeps it simple to debug.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import random
import re
import shutil
import subprocess
import threading
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

import config

logger = logging.getLogger("video_generator")

# --------------------------------------------------------------------------- constants

CANVAS_W = 1080   # vertical 9:16 canvas for Reels / TikTok / Shorts
CANVAS_H = 1920
FPS = 30                  # default output frame rate (RenderConfig.fps overrides)
# Output frame rates offered in the UI. 60 halves the subliminal cycle length
# (K/fps seconds), so the effect blends more smoothly; both survive upload to
# every major platform. Higher rates are pointless — platforms re-encode down to
# 30/60 and most phone screens top out at 60 Hz.
FPS_CHOICES = (30, 60)

# Hardcoded subliminal rule (applies to whichever text has the effect): of the
# last SUBLIMINAL_TAIL_CHARS characters, at most SUBLIMINAL_TAIL_MAX_SHOWN may be
# visible in ANY single frame — the rest are force-hidden (rotated/balanced so
# all of them still surface across the cycle). This strengthens the "never whole"
# guarantee for the tail of the text (e.g. a code or domain) regardless of the
# word/char granularity or the hide percentage chosen elsewhere.
SUBLIMINAL_TAIL_CHARS = 4
SUBLIMINAL_TAIL_MAX_SHOWN = 2
# Ceiling on the subliminal cycle length. Each frame of the cycle is a separate
# full-canvas FFmpeg input, so K is a memory knob as much as a visual one. The
# sidebar's own slider stops at 8; this bounds the paths that DERIVE K instead
# of reading it (CUSTOM_SUBLIMINAL_SCHEDULES takes an lcm), so no schedule can
# quietly claim thirty-odd inputs.
SUBLIMINAL_MAX_K = 8

# Hand-authored subliminal schedules for specific known texts. When a subliminal
# text matches a key (lowercased, whitespace collapsed, curly quotes
# straightened), this schedule REPLACES the generic machinery entirely — the
# K / pattern / style settings and the last-4-chars rule don't apply. Semantics:
#   * body_frames:    frame j SHOWS ONLY the pieces of body_frames[j % len]
#   * overlay_frames: an independent rule running on top (like the tail rule) —
#                     frame j ALSO shows overlay_frames[j % len]
# Pieces are (start, end) index ranges over the text's NON-SPACE characters in
# reading order (end exclusive). Cycle length = lcm(len(body), len(overlay)),
# so no frame ever shows the whole text and every character surfaces each cycle.
CUSTOM_SUBLIMINAL_SCHEDULES = {
    # search "Plushie: Your Childhood Friend" in App Store
    # non-space chars: search(0-5) "(6) plushie(7-13) :(14) your(15-18)
    # childhood(19-27) friend(28-33) "(34) in(35-36) app(37-39) store(40-44)
    'search "plushie: your childhood friend" in app store': {
        "body_frames": [
            [(0, 6), (35, 37)],    # search + in
            [(6, 15)],             # "Plushie:  (the colon shows with Plushie)
            [(15, 35)],            # Your Childhood + Friend"
        ],
        "overlay_frames": [
            [(37, 38), (40, 42)],  # A + St    ("App Store" is never whole:
            [(38, 40), (42, 45)],  # pp + ore   its halves alternate every frame)
        ],
    },
}


def _norm_sub_text(text: str) -> str:
    """Normalize a text for CUSTOM_SUBLIMINAL_SCHEDULES lookup."""
    t = (text.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))
    return " ".join(t.split()).lower()

# The CTA is invisible until CTA_FADE_START seconds, then fades in (alpha
# only) and is fully visible at CTA_FADE_START + CTA_FADE_DURATION. These are
# only defaults now — RenderConfig.cta_fade_* / cta_video_fade_* (set from the
# sidebar, overridable per row) carry the live values.
CTA_FADE_START = 1.0
CTA_FADE_DURATION = 0.5

# The CTA video is a sequence of fixed positions ("clips") that always play in
# order 1..N. Each position is backed by a pool of sample videos; one sample is
# chosen per output video (pinned by a CTA_Clip_<i> cell, else picked at random).
# The number of ACTIVE slots is chosen in the UI (defaults to
# DEFAULT_CTA_VIDEO_SLOTS); the Excel column set is always generated off the MAX
# so existing sheets keep parsing and users can grow up to MAX_CTA_VIDEO_SLOTS
# without a code change.
DEFAULT_CTA_VIDEO_SLOTS = 5
MAX_CTA_VIDEO_SLOTS = 10
CTA_CLIP_COLUMNS = [f"CTA_Clip_{i}" for i in range(1, MAX_CTA_VIDEO_SLOTS + 1)]
# Per-clip playback-speed overrides, one column per slot. Blank falls back to
# the row-wide CTA_Video_Speed cell, then to the sidebar's per-clip default.
CTA_SPEED_COLUMNS = [f"CTA_Video_Speed_{i}" for i in range(1, MAX_CTA_VIDEO_SLOTS + 1)]
# Hard ceiling on the total clips concatenated into the side sequence (the
# fixed opening picks plus the fill clips that pad out the main video's
# duration). Guards against a pool of tiny clips exploding the FFmpeg input
# count / command-line length. See _resolve_cta_sequence.
CTA_MAX_TOTAL_CLIPS = 40

# The GIF layer: a SECOND, independent clip layer, deliberately unlike the CTA
# video one in the two ways the feature is actually about.
#
#   * Dwell time. Every gif holds its box for at least gif_min_seconds by
#     repeating ITSELF a whole number of times — a 3s gif plays twice (6s), it
#     is never cut at 5s. See _resolve_gif_sequence.
#   * Fit. Each gif is CONTAIN-fitted: scaled to sit entirely inside its box,
#     never cropped and never distorted, but always as large as the box allows
#     — a gif smaller than the box is enlarged until one of its sides touches
#     the edge, a bigger one is shrunk. The leftover box area (on the axis the
#     aspect ratio leaves short) is transparent, so the box is a fit guide
#     rather than a visible plate.
#
# The pool is FLAT, not slotted: gifs have no position semantics, and with a
# dwell floor the sequence length is derived from the promo's duration rather
# than chosen, so a fixed slot count would be wrong for every promo but one.
GIF_MIN_SECONDS = 5.0
# Ceiling on gifs concatenated into one sequence — same rationale as
# CTA_MAX_TOTAL_CLIPS. At the 5s floor this already covers a ~3.3-minute promo.
GIF_MAX_TOTAL_CLIPS = 40

# The background-video layer sequences its pool exactly like the gifs: clips are
# drawn until their combined on-screen time covers the promo, each holding the
# frame for at least BG_VIDEO_MIN_SECONDS by repeating ITSELF a whole number of
# times. The floor is longer than the gifs' because this layer is ambience
# rather than punctuation — a bed that changes every 5 seconds reads as flicker.
BG_VIDEO_MIN_SECONDS = 10.0
BG_VIDEO_MAX_TOTAL_CLIPS = 40

# The music bed: a flat pool of AUDIO files sequenced exactly like the gifs and
# the background videos — tracks are drawn until their combined length covers
# the promo, each held for at least MUSIC_MIN_SECONDS by repeating ITSELF a
# whole number of times. The floor is longer again than the background videos'
# because a track that swaps every ten seconds reads as a fault, not a bed.
#
# Mixed against the promo's own audio by music_volume: at 0.10 the bed is at
# 10% and the promo at 90%, a straight complementary split. amix runs with
# normalize=0, so those numbers are the linear gains they claim to be rather
# than something amix re-scales behind them.
MUSIC_MIN_SECONDS = 30.0
MUSIC_MAX_TOTAL_CLIPS = 20
# Sample rate / layout every track is forced to before concat. Concat refuses
# inputs that disagree, and a pool of user-supplied files disagrees as a matter
# of course — a 44.1kHz stereo MP3 next to a 48kHz mono WAV is the normal case,
# not the edge one.
MUSIC_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

# Split-audio: the promo's OWN audio track is cut into equal chunks and each is
# replayed at its own random tempo, so the sound runs fast in places and slow in
# others while still ending exactly with the picture.
#
# Exactness is arithmetic rather than luck. Chunk i is played at tempo t_i, so it
# occupies L/t_i seconds. Writing r_i = 1/t_i, the warped track runs L*sum(r_i)
# — so normalising the r_i to mean exactly 1 makes that N*L, the source length,
# to the precision of the float. atempo itself rounds to its internal WSOLA
# frames (measured 10-40ms short over a 20s clip, growing with the chunk count),
# which apad=whole_dur then makes up.
#
# atempo is a PITCH-PRESERVING time stretch: the pace changes, voices do not go
# chipmunk. It accepts 0.5-100, and the spread cap below keeps every tempo far
# inside that — the real limit is quality, since WSOLA warbles on speech well
# before the filter complains.
SPLIT_AUDIO_CHUNKS = 8
SPLIT_AUDIO_SPREAD = 0.35
# Past ~0.6 the slow chunks smear badly and the fast ones gabble. Also keeps
# 1/(1-spread) bounded well under atempo's ceiling.
SPLIT_AUDIO_MAX_SPREAD = 0.6
# Each chunk adds ~65 characters to the filter graph, which shares Windows'
# 32767-character command line with everything else — see MAX_TOTAL_FFMPEG_INPUTS.
SPLIT_AUDIO_MAX_CHUNKS = 24
# Ceiling on FFmpeg inputs for ONE row across BOTH clip layers plus the fixed
# inputs and the subliminal stills. Two independent caps are not enough: they
# add up in a single command line, and Windows stops at 32767 characters with a
# [WinError 206] that names nothing. See _check_input_budget.
MAX_TOTAL_FFMPEG_INPUTS = 60

# Every column is optional: absent/blank BG_Image cells get an image randomly
# assigned from the uploaded ZIP (no repeats until the pool is exhausted),
# absent Video_*/CTA_* cells fall back to the sidebar's boxes (or a randomized
# video position), absent texts are skipped, absent sizes/colors get defaults,
# and absent X/Y coordinates trigger auto-placement. The row count alone
# drives the batch.
# Optional fixed-size fit box for a text. When a text's <Role>_Width and
# <Role>_Height are both set, the box drives the type instead of the other way
# round: the text re-wraps to the box's width and its font size is searched for
# the largest value whose PAINTED block still fits — so line breaks are added
# and removed as the box changes, and the size follows.
#
# X/Y stay the block CENTRE, as they always have been for texts (the video, CTA
# and gif boxes are top-left instead). The box is centred on them, so adding one
# to an existing sheet never moves the text.
TEXT_ROLES = ["Headline", "Subheading", "Footer"]
TEXT_BOX_COLUMNS = [f"{role}_{dim}"
                    for role in TEXT_ROLES for dim in ("Width", "Height")]
# Bounds of the fit search. The floor is the readability guard: below it the
# text is left AT the floor, allowed to overflow, and the row is warned. That is
# a deliberate trade — clipping reads as a rendering fault, and shrinking
# without a floor silently produces text nobody can read (a 28-character word in
# a 200x200 box measured down to 12px before this bound existed).
TEXT_FIT_MIN_SIZE = 20
TEXT_FIT_MAX_SIZE = 200

REQUIRED_COLUMNS: list[str] = []
OPTIONAL_COLUMNS = [
    "BG_Image",
    "Video_X", "Video_Y", "Video_Width", "Video_Height",
    "CTA_X", "CTA_Y", "CTA_Width", "CTA_Height",
    "CTA_Fade_Start", "CTA_Fade_Duration",
    "CTA_Video_X", "CTA_Video_Y", "CTA_Video_Width", "CTA_Video_Height",
    "CTA_Video_Fade_Start", "CTA_Video_Fade_Duration", "CTA_Video_Speed",
    *CTA_SPEED_COLUMNS,
    *CTA_CLIP_COLUMNS,
    # The gif box + its fade. Geometry and this-box timing are per-row across
    # the whole sheet format; the dwell floor is not (it is a batch-wide pacing
    # decision, so it lives in the sidebar only — see RenderConfig).
    "GIF_X", "GIF_Y", "GIF_Width", "GIF_Height",
    "GIF_Fade_Start", "GIF_Fade_Duration",
    # The translucent background-video box (full canvas unless overridden).
    "BG_Video_X", "BG_Video_Y", "BG_Video_Width", "BG_Video_Height",
    *TEXT_BOX_COLUMNS,
    "Headline", "Headline_Size", "Headline_Color", "Headline_Opacity",
    "Headline_X", "Headline_Y", "Headline_Font",
    "Headline_BgColor", "Headline_BgOpacity", "Headline_Style", "Headline_Subliminal",
    "Subheading", "Subheading_Size", "Subheading_Color", "Subheading_Opacity",
    "Subheading_X", "Subheading_Y", "Subheading_Font",
    "Subheading_BgColor", "Subheading_BgOpacity", "Subheading_Style", "Subheading_Subliminal",
    "Footer", "Footer_Size", "Footer_Color", "Footer_Opacity",
    "Footer_X", "Footer_Y", "Footer_Font",
    "Footer_BgColor", "Footer_BgOpacity", "Footer_Style", "Footer_Subliminal",
]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# Font fallback chain, first hit wins (Windows first since that's the primary target).
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# Bundled font library (run `python fetch_fonts.py` to populate ./fonts). Maps
# the display name shown in the UI / typed into a *_Font cell to (filename,
# optional named variation). The two variable fonts are pinned to their Bold
# instance; the rest are single-weight display faces. Picking from this library
# (instead of system fonts) makes the look identical on Windows and Docker.
FONTS_DIR = Path(__file__).parent / "fonts"
FONT_LIBRARY: dict[str, tuple[str, Optional[str]]] = {
    "Impact (Bebas Neue)": ("BebasNeue-Regular.ttf", None),
    "Heavy (Anton)": ("Anton-Regular.ttf", None),
    "Clean (Montserrat)": ("Montserrat-Variable.ttf", "Bold"),
    "Elegant (Playfair)": ("PlayfairDisplay-Variable.ttf", "Bold"),
    "Script (Pacifico)": ("Pacifico-Regular.ttf", None),
    "Marker (Permanent Marker)": ("PermanentMarker-Regular.ttf", None),
    "Typewriter (Special Elite)": ("SpecialElite-Regular.ttf", None),
    "Bold Script (Lobster)": ("Lobster-Regular.ttf", None),
    "Retro (Press Start 2P)": ("PressStart2P-Regular.ttf", None),
    "Urban (Bungee)": ("Bungee-Regular.ttf", None),
}

# Font choices that aren't bundled families: the system font, or the user's
# uploaded TTF/OTF. These plus FONT_LIBRARY keys are what the UI offers.
FONT_SYSTEM = "System default"
FONT_CUSTOM = "Custom upload"
FONT_CHOICES = [FONT_SYSTEM, *FONT_LIBRARY, FONT_CUSTOM]


def _font_norm(name: str) -> str:
    """Loose key for font matching: lowercase, strip everything but a-z0-9."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


# Accept the full library name, the leading vibe word ("Impact"), or the family
# in parentheses ("Bebas Neue") — case/space/punctuation-insensitive — so a
# *_Font cell is forgiving to type.
_FONT_ALIASES: dict[str, str] = {}
for _key in FONT_LIBRARY:
    _vibe, _, _fam = _key.partition(" (")
    _FONT_ALIASES[_font_norm(_key)] = _key
    _FONT_ALIASES[_font_norm(_vibe)] = _key
    if _fam:
        _FONT_ALIASES[_font_norm(_fam.rstrip(")"))] = _key
for _alias in ("system", "systemdefault", "default", ""):
    _FONT_ALIASES[_alias] = FONT_SYSTEM
for _alias in ("custom", "customupload", "upload"):
    _FONT_ALIASES[_alias] = FONT_CUSTOM

# Artistic text treatments (TikTok-style), applied on top of the fill color.
TEXT_STYLES = ["classic", "outline", "shadow", "neon"]

# Random fallbacks for blank size/color cells. Size ranges are per element
# role so an auto-sized footer never dwarfs an auto-sized headline; colors are
# a curated vivid palette (pure random RGB too easily lands on unreadable
# dark-on-dark combinations).
RANDOM_SIZE_RANGES = {
    "Headline": (56, 88),
    "Subheading": (34, 52),
    "Footer": (24, 36),
}
RANDOM_TEXT_COLORS = [
    "#FFFFFF", "#FFD700", "#FFE066", "#FF6B6B", "#FF9F43", "#4ECDC4",
    "#7BED9F", "#74B9FF", "#A29BFE", "#FF7AA2", "#F8F32B", "#00F5D4",
]

# Auto-placement (used when a text's X/Y cells are blank): texts are dropped
# at random spots that keep clear of the video box, the CTA, and each other.
PLACEMENT_MARGIN = 30      # min distance from canvas edges, px
PLACEMENT_GAP = 24         # min gap between elements, px
PLACEMENT_ATTEMPTS = 400   # random tries before settling for least overlap


# --------------------------------------------------------------------------- helpers

def find_ffmpeg() -> str:
    """Locate an FFmpeg binary: prefer one on PATH, else the static build
    that ships with the imageio-ffmpeg package (no system install needed)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# Capture kwargs for every FFmpeg call. FFmpeg echoes a container's metadata
# tags (title, comment, artist, …) into its stderr verbatim, and nothing
# requires those tags to be UTF-8 — a single stock GIPHY MP4 carrying one
# Latin-1 byte in its `comment` is enough. A bare text=True decodes with the
# locale encoding, which on any Linux deploy is UTF-8 and raises
# UnicodeDecodeError from subprocess's reader thread, i.e. NOT inside the try
# below and not catchable as OSError — it kills the probe (and with it the whole
# render) before a frame is drawn. cp1252 on Windows swallows the same byte, so
# this only ever reproduces in production. Decode explicitly and replace what
# won't decode: this output is only regex-matched or pasted into an error
# message, never interpreted byte for byte.
_FF_CAPTURE = dict(capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


# Whether a media file actually contains a video stream, cached by
# (filename, size) so re-uploads of the same files across generator instances
# (every Preview/Render click rebuilds the workspace) are only probed once.
# An MP4 with no video track (audio-only, or a corrupt video stream) would
# otherwise make FFmpeg fail mid-render with the cryptic "Stream specifier ':v'
# ... matches no streams" error.
_VIDEO_STREAM_CACHE: dict[tuple, bool] = {}
_VIDEO_STREAM_LOCK = threading.Lock()
_VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video", re.IGNORECASE)


def _has_video_stream(ffmpeg: str, path: Path) -> bool:
    try:
        key = (path.name.lower(), path.stat().st_size)
    except OSError:
        return False
    with _VIDEO_STREAM_LOCK:
        if key in _VIDEO_STREAM_CACHE:
            return _VIDEO_STREAM_CACHE[key]
    ok = False
    try:
        # ffmpeg -i with no output exits non-zero by design; the stream listing
        # is on stderr regardless (same trick as _probe_duration).
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                              **_FF_CAPTURE, timeout=60)
        ok = _VIDEO_STREAM_RE.search(proc.stderr or "") is not None
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    with _VIDEO_STREAM_LOCK:
        _VIDEO_STREAM_CACHE[key] = ok
    return ok


# The mirror of _VIDEO_STREAM_CACHE for audio, and needed for the same class of
# reason: a filter graph cannot take an optional stream. `-map 1:a?` quietly
# maps nothing when the promo is silent, but `[1:a]` in -filter_complex is a
# hard "Stream specifier ':a' ... matches no streams" before a frame is drawn.
# So anything routing audio through the graph has to know first.
_AUDIO_STREAM_CACHE: dict[tuple, bool] = {}
_AUDIO_STREAM_LOCK = threading.Lock()
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio", re.IGNORECASE)


def _has_audio_stream(ffmpeg: str, path: Path) -> bool:
    try:
        key = (path.name.lower(), path.stat().st_size)
    except OSError:
        return False
    with _AUDIO_STREAM_LOCK:
        if key in _AUDIO_STREAM_CACHE:
            return _AUDIO_STREAM_CACHE[key]
    ok = False
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                              **_FF_CAPTURE, timeout=60)
        ok = _AUDIO_STREAM_RE.search(proc.stderr or "") is not None
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    with _AUDIO_STREAM_LOCK:
        _AUDIO_STREAM_CACHE[key] = ok
    return ok


def split_audio_tempos(n: int, spread: float, rng: random.Random) -> list[float]:
    """`n` random atempo factors whose playback times sum to the source length.

    Draw the time-stretch factors r_i (how much LONGER chunk i plays), normalise
    them to mean exactly 1 so the warped track is exactly as long as what went
    in, and return the tempos 1/r_i. See the SPLIT_AUDIO_* constants."""
    n = max(2, int(n))
    spread = max(0.0, min(float(spread), SPLIT_AUDIO_MAX_SPREAD))
    stretches = [rng.uniform(1.0 - spread, 1.0 + spread) for _ in range(n)]
    total = sum(stretches)
    if total <= 0:                      # only reachable at spread >= 1
        return [1.0] * n
    scale = n / total
    stretches = [r * scale for r in stretches]
    # Normalising moves EVERY draw by one factor that depends on how the sample
    # happened to land, so a chunk can finish outside the band the spread
    # advertises. Measured over 40,000 draws: at spread 0.35 the real range is
    # 0.62x-1.79x against a nominal 0.74x-1.54x, and at spread 0.6 it reaches
    # 0.44x -- under atempo's hard 0.5 floor, where FFmpeg refuses the filter and
    # fails the row. Roughly 3% of chunks land outside the band, so this is a
    # routine occurrence rather than a corner case.
    #
    # Pull every deviation toward 1 by one shared factor until the worst offender
    # is back inside. Scaling deviations about a mean of exactly 1 leaves the
    # mean at exactly 1, so the whole exact-duration property survives untouched
    # -- which is why this is a rescale and not a clamp. A clamp would fix the
    # range and silently break the length.
    excess = max(abs(r - 1.0) for r in stretches)
    if spread > 0 and excess > spread:
        pull = spread / excess
        stretches = [1.0 + pull * (r - 1.0) for r in stretches]
    return [1.0 / r for r in stretches]


# How long a file's VIDEO STREAM runs, as opposed to what its container header
# claims. The two genuinely differ: an MP4 whose audio outlasts its video
# reports the AUDIO length in `Duration:`, and a measured 3s-video/9s-audio file
# reads as 9.0s there. That is harmless for the promo (which only needs an
# output length) but wrong for a gif, where `ceil(floor / duration)` would then
# compute 1 repeat for a clip that needs 2 and silently miss the dwell floor by
# 40%. `-stream_loop` loops on the VIDEO duration, so the repeat count has to be
# computed in the same unit it will be applied in.
#
# Method: demux the video stream to the null muxer (`-c copy`, so nothing is
# decoded) and divide the reported frame count by the reported frame rate.
# Reading the trailing `time=` instead would undershoot by one frame interval —
# a 5.000s gif measures 4.93 and would needlessly double to 10s at the boundary.
# Frames/rate is exact: measured 3.000 / 4.000 / 5.000 / 6.000 on CFR sources
# and 2.000 on a real 12.5fps animated GIF.
#
# Cached like _VIDEO_STREAM_CACHE on (filename, size) and MODULE-global on
# purpose: the dwell floor makes this probe mandatory for every picked gif on
# the *preview* path, Streamlit rebuilds the generator on every interaction, and
# each probe costs ~350ms — a per-instance cache would re-pay that on every
# click. None (unprobeable) is cached too, so a bad file is not re-probed.
_VIDEO_DURATION_CACHE: dict[tuple, Optional[float]] = {}
_VIDEO_DURATION_LOCK = threading.Lock()
_FRAME_COUNT_RE = re.compile(r"frame=\s*(\d+)")
_FRAME_RATE_RE = re.compile(r",\s*([\d.]+)\s*fps\b")
_CONTAINER_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _probe_video_duration(ffmpeg: str, path: Path) -> Optional[float]:
    """Seconds of VIDEO in `path`, or None if it can't be measured.

    Falls back to the container header when the frame count or rate can't be
    read (some exotic sources report neither), which is still better than
    nothing — callers treat None as 'play once and warn'."""
    path = Path(path)
    try:
        key = (path.name.lower(), path.stat().st_size)
    except OSError:
        return None
    with _VIDEO_DURATION_LOCK:
        if key in _VIDEO_DURATION_CACHE:
            return _VIDEO_DURATION_CACHE[key]
    dur: Optional[float] = None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path),
             "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
            **_FF_CAPTURE, timeout=120,
        )
        err = proc.stderr or ""
        frames = _FRAME_COUNT_RE.findall(err)
        rate = _FRAME_RATE_RE.search(err)
        if frames and rate:
            n, fps = int(frames[-1]), float(rate.group(1))
            if n > 0 and fps > 0:
                dur = n / fps
        if dur is None:
            match = _CONTAINER_DURATION_RE.search(err)
            if match:
                h, m, s = match.groups()
                dur = int(h) * 3600 + int(m) * 60 + float(s)
        # A zero-length read is not a duration. Returning 0.0 here would reach
        # `floor / dur` in the repeat maths and raise ZeroDivisionError, which
        # surfaces through render_row's broad except as the bare string
        # "division by zero" — naming neither the file nor the cause.
        if not dur or dur <= 0:
            dur = None
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        logger.warning("Could not probe video duration for %s: %s", path, exc)
        dur = None
    with _VIDEO_DURATION_LOCK:
        _VIDEO_DURATION_CACHE[key] = dur
    return dur


def gif_repeats(duration: Optional[float], min_seconds: float) -> int:
    """How many whole times a gif must play to hold its box for at least
    `min_seconds`. A 3s gif under a 5s floor plays twice (6s) — the floor is a
    minimum, never a cut. Clips already at or over the floor play once, and the
    5.000s boundary resolves to 1 (the epsilon absorbs float error; `ceil` can
    only ever add a repeat, so error in this direction is safe).

    An unmeasurable duration yields 1: the floor is missed, but the failure is
    bounded and the caller warns. `-stream_loop -1` is NOT the fallback here —
    an infinite input with no `-t` to bound it was measured still running and
    growing past 30s and 99MB for a 20-second output."""
    if not duration or duration <= 0:
        return 1
    return max(1, math.ceil(min_seconds / duration - 1e-9))


def find_default_font() -> Optional[str]:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def validate_dataframe(df: pd.DataFrame) -> list[str]:
    """Return the list of required columns missing from the Excel sheet."""
    present = {str(c).strip() for c in df.columns}
    return [c for c in REQUIRED_COLUMNS if c not in present]


def missing_optional_columns(df: pd.DataFrame) -> list[str]:
    """Optional columns absent from the sheet (informational, not an error)."""
    present = {str(c).strip() for c in df.columns}
    return [c for c in OPTIONAL_COLUMNS if c not in present]


def safe_filename(row_number: int, label: str) -> str:
    """Build '001_Some_Caption.mp4' style names; safe on every filesystem.

    `label` is the row's Caption when it has one, falling back to the Headline.
    Naming from the caption is what removes the old rename step: the file
    arrives in Drive already carrying the text it will be posted with.

    Non-word characters (emoji, punctuation) are stripped and the text is capped
    at 60 characters, so the result stays well inside the 255-character limit
    once the row prefix and extension are added. The numeric prefix keeps the
    batch in row order and guarantees names are unique within it — Drive would
    otherwise happily store a dozen identically-named files."""
    name = re.sub(r"[^\w\- ]", "", str(label or ""), flags=re.UNICODE).strip()
    name = re.sub(r"\s+", "_", name)[:60].strip("_")
    return f"{row_number:03d}_{name}.mp4" if name else f"{row_number:03d}_row.mp4"


def _clean_str(value) -> str:
    """Normalize a pandas cell to a display string ('' for NaN/None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _parse_opt_int(value, warnings: list[str], label: str) -> Optional[int]:
    """Parse a numeric cell. Blank => None (an automatic value is chosen later);
    garbage => warn + None."""
    if _clean_str(value) == "":
        return None
    try:
        return int(round(float(value)))
    except (ValueError, TypeError):
        warnings.append(f"{label}: invalid value '{value}', an automatic one will be used")
        return None


def _parse_opt_float(value, warnings: list[str], label: str) -> Optional[float]:
    """Like _parse_opt_int but keeps fractional values (used for fade seconds)."""
    if _clean_str(value) == "":
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        warnings.append(f"{label}: invalid value '{value}', the default will be used")
        return None


def _parse_opt_bool(value, warnings: list[str], label: str) -> Optional[bool]:
    """Parse a truthy cell. Blank => None (inherit the batch default); recognized
    true/false tokens => the bool; anything else => warn + None."""
    text = _clean_str(value).strip().lower()
    if text == "":
        return None
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    warnings.append(f"{label}: expected yes/no, got '{value}'; the default will be used")
    return None


def _img_to_data_uri(img: Image.Image, fmt: str = "PNG") -> str:
    """Encode a PIL image as a data URI for embedding in the preview editor."""
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=85)
        mime = "image/jpeg"
    else:
        img.save(buf, format="PNG")
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _rect_from_center(cx: float, cy: float, w: float, h: float) -> tuple:
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _overlap_area(a: tuple, b: tuple, gap: float = 0.0) -> float:
    """Intersection area of rect a with rect b inflated by `gap` on all sides."""
    ix = min(a[2], b[2] + gap) - max(a[0], b[0] - gap)
    iy = min(a[3], b[3] + gap) - max(a[1], b[1] - gap)
    return ix * iy if (ix > 0 and iy > 0) else 0.0


def _greedy_wrap(words: list[str], max_w: float, width) -> list[str]:
    """Word-wrap so every line's rendered width stays within max_w."""
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if width(candidate) <= max_w:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _balanced_lines(words: list[str], n_lines: int, width) -> list[str]:
    """Split words into n_lines of roughly equal rendered width (used for the
    footer's fixed 3-line layout)."""
    target = width(" ".join(words)) / n_lines
    lines: list[str] = []
    current = ""
    for i, word in enumerate(words):
        candidate = (current + " " + word).strip()
        words_left = len(words) - i - 1
        lines_left = n_lines - len(lines) - 1
        # Break early enough that every remaining line still gets a word.
        if current and len(lines) < n_lines - 1 and (
            width(candidate) > target or words_left < lines_left
        ):
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


def _parse_color(value, warnings: list[str], label: str,
                 fallback_desc: str = "a random color") -> Optional[tuple]:
    """Accept '#RRGGBB', '#RGB', 'rgb(...)' or CSS color names ('yellow',
    'blue', 'Light Yellow'...). Blank => None (the caller picks a default —
    a palette color for text, no box for backgrounds); unrecognized => warn
    + None. `fallback_desc` only tailors the warning message."""
    raw = _clean_str(value)
    if not raw:
        return None
    try:
        rgb = ImageColor.getrgb(raw)
    except ValueError:
        try:
            # Be forgiving with names: 'Light Yellow' -> 'lightyellow'
            rgb = ImageColor.getrgb(re.sub(r"\s+", "", raw.lower()))
        except ValueError:
            warnings.append(f"{label}: unrecognized color '{raw}', using {fallback_desc}")
            return None
    return rgb if len(rgb) == 4 else (*rgb, 255)


def _parse_opacity(value, warnings: list[str], label: str) -> Optional[float]:
    """Translucency for a text or its highlight box, as a 0..1 factor.

    Accepts a percentage (`65`, `65%`) or a fraction (`0.65`) — all three mean
    65% opaque. A bare number in 0..1 reads as a fraction, so write `1%` (not
    `1`) for near-invisible. Blank => None (inherit the batch default);
    unparseable => warn + None. Out-of-range values are clamped."""
    raw = _clean_str(value)
    if not raw:
        return None
    as_pct = raw.endswith("%")
    try:
        num = float(raw.rstrip("%").strip())
    except ValueError:
        warnings.append(
            f"{label}: '{raw}' is not a number (use 0-100, or 0-1), "
            "using the default opacity")
        return None
    if not as_pct and 0.0 <= num <= 1.0:
        num *= 100.0          # 0.65 -> 65%
    if not 0.0 <= num <= 100.0:
        warnings.append(f"{label}: {raw} is outside 0-100, clamped")
        num = max(0.0, min(num, 100.0))
    return num / 100.0


def _apply_opacity(color: Optional[tuple], factor: Optional[float]) -> Optional[tuple]:
    """Scale a parsed RGBA color's alpha by `factor` (0..1). The color may
    already carry alpha (an '#RRGGBBAA' cell), so this MULTIPLIES rather than
    replaces — both routes to translucency compose."""
    if color is None or factor is None:
        return color
    r, g, b = color[:3]
    base = color[3] if len(color) == 4 else 255
    return (r, g, b, max(0, min(255, int(round(base * factor)))))


def _alpha_of(color: Optional[tuple]) -> int:
    return 255 if not color or len(color) < 4 else int(color[3])


# --------------------------------------------------------------------------- dataclasses

@dataclass
class RenderConfig:
    """All knobs the UI exposes. Coordinates are in 1080x1920 canvas pixels.
    The video and CTA box values are per-batch defaults — a row's Video_* /
    CTA_* Excel cells override them for that row."""
    # Layout mode. "free" = every box is placed by its own coordinates (the
    # classic behavior). "split" = a side-by-side split screen: the main video
    # fills one half and the CTA-video sequence fills the other. In split mode
    # the two panel boxes are computed automatically (see _apply_split_layout),
    # so Video_*/CTA_Video_* positions and randomize_video_pos are ignored, and
    # the side sequence always fills the main video's duration.
    layout_mode: str = "free"
    swap_sides: bool = False          # split mode: main on the right instead of left
    split_panel_h: int = 960          # split mode: panel height (centered band)
    # Split mode: crop the OUTPUT to exactly the two panels (1080 x panel
    # height) — no background at all. Texts and the CTA are then drawn on top
    # of the videos themselves; auto-placed texts are confined to the band.
    crop_to_panels: bool = False
    # Canvas color used when a row has no background image (backgrounds are
    # optional — without a ZIP every row renders on this solid color).
    bg_color: str = "#1E1B4B"
    # The translucent background-video layer: clips from the uploaded/Drive
    # pool play back-to-back for the length of each video, sequenced like the
    # gifs (see _resolve_bg_video_sequence). Sits ABOVE the promo/gif/CTA and
    # ALWAYS directly below the texts (it shares text_z with a lower tie-break,
    # so no sidebar z setting can put it over them). 0 opacity = layer off even
    # when a pool exists. The box defaults to the full canvas.
    bg_video_opacity: float = 0.08
    bg_video_x: int = 0
    bg_video_y: int = 0
    bg_video_w: int = CANVAS_W
    bg_video_h: int = CANVAS_H
    # Dwell floor for the background sequence, scoped like gif_min_seconds and
    # for the same reasons (batch-wide pacing; per-row values would make the
    # FFmpeg input count vary row to row).
    bg_video_min_seconds: float = BG_VIDEO_MIN_SECONDS
    video_x: int = 90
    video_y: int = 300
    video_w: int = 900
    video_h: int = 900
    cta_x: int = 340
    cta_y: int = 1600
    cta_w: int = 400
    cta_h: int = 160
    # CTA-image fade: invisible until cta_fade_start, fully visible
    # cta_fade_duration seconds later (per-row CTA_Fade_* cells override).
    cta_fade_start: float = CTA_FADE_START
    cta_fade_duration: float = CTA_FADE_DURATION
    # Optional CTA *video*: a separate element layered with the CTA image, in
    # its own box, with its own configurable fade-in. One or more clips may be
    # supplied to VideoGenerator; they play back-to-back as a single clip in this
    # one box (the box + fade are shared, but each clip slot has its own
    # playback speed), and the play order is shuffled per output video. Absent =>
    # the element is skipped and the output is identical to before.
    cta_video_x: int = 360
    cta_video_y: int = 1200
    cta_video_w: int = 360
    cta_video_h: int = 360
    cta_video_fade_start: float = 0.5
    cta_video_fade_duration: float = 0.5
    # Playback speed per clip slot (1..N); >1 = faster, <1 = slower. A row's
    # CTA_Video_Speed_<i> cell overrides one clip; CTA_Video_Speed overrides the
    # whole row. Empty by default — the app fills it to the active slot count.
    # The clip loop guards `i < len(...)`, so a short/empty list is always safe
    # (missing slots fall back to normal speed).
    cta_video_speeds: list[float] = field(default_factory=list)
    # When True, the side sequence keeps drawing fresh random clips (from the
    # union of all slot pools) until its combined length covers the whole main
    # video, so the side panel is never frozen on a last frame. Off preserves
    # the classic play-once/last-frame-hold behavior. Split layout turns this on.
    cta_video_fill: bool = False
    # Optional GIF layer: a flat pool of short looping clips shown one after
    # another in their own box. Independent of the CTA video in every respect —
    # own pool, own box, own fade, own z-index — because the two answer
    # different briefs (see the GIF_* constants above). Absent pool => the layer
    # is skipped entirely and the output is identical to before.
    gif_x: int = 60
    gif_y: int = 560
    gif_w: int = 360
    gif_h: int = 360
    # Minimum seconds each gif holds the box, reached by repeating the gif a
    # whole number of times. Sidebar-only, deliberately: this is batch-wide
    # pacing, so a per-row column would be blank or identical in all 200 rows —
    # and per-row values would make the FFmpeg input count vary row to row,
    # right against the command-line ceiling. Matches how cta_video_fill (the
    # other sequence-construction knob) is scoped.
    gif_min_seconds: float = GIF_MIN_SECONDS
    # Defaults to no fade (unlike the CTA video's 0.5/0.5): a mostly-transparent
    # contain-fitted box is already visually light, and a zero fade takes the
    # passthrough branch in build_ffmpeg_command, which keeps the static preview
    # — which always paints the layer fully opaque — exactly honest.
    gif_fade_start: float = 0.0
    gif_fade_duration: float = 0.0
    # Layer order (z-index) for the five overlay layers; higher = nearer the top,
    # the background is always the base. Equal values fall back to a fixed tie
    # priority (promo < gifs < CTA video < CTA image < texts) — the gif layer is
    # the most decorative and least informational, so it loses ties to anything
    # carrying a message. Applies to every video.
    #
    # These four defaults shifted up by one when the gif layer landed. In-flight
    # jobs are unaffected: RenderConfig(**base_config) restores the persisted
    # ints, so an old job keeps 1/2/3/4 and its relative order is unchanged.
    video_z: int = 1
    gif_z: int = 2
    cta_video_z: int = 3
    cta_image_z: int = 4
    text_z: int = 5
    fps: int = FPS                # output frame rate; see FPS_CHOICES
    crf: int = 18                 # 16-28; lower = higher quality / bigger files
    preset: str = "medium"        # x264 speed/size tradeoff
    # When True, video_x/video_y are ignored and each row's video box gets a
    # seeded-random position avoiding the CTA and explicitly-placed texts.
    randomize_video_pos: bool = False
    # Distinguishes repeated renders of the SAME sheet. Every random choice a
    # row makes — which sample clip fills each CTA slot, auto-placement, random
    # sizes and colors — is seeded from the row's content and number, so
    # re-rendering a sheet reproduces it byte for byte. That is what makes the
    # preview match the render, and it is also why producing ten *different*
    # batches from one sheet needs a per-batch salt folded into that seed.
    # 0 (the default) leaves the seed exactly as it has always been.
    variant_salt: int = 0
    audio_bitrate: str = "192k"
    include_audio: bool = True
    # ---- the music bed. 0 = off, and off means the audio path is byte-for-byte
    # what it always was: `-map <promo>:a?` straight into AAC, no filter graph.
    # Above 0 the promo's audio drops to (1 - music_volume) and the bed comes in
    # at music_volume, a complementary split — "music at 10%" leaves the original
    # at 90%. Linear gain, not perceptual: 0.10 is about -20dB.
    music_volume: float = 0.0
    # Dwell floor for the music sequence. Scoped batch-wide for the same reason
    # as gif_min_seconds and bg_video_min_seconds — it is pacing, and a per-row
    # column would make the FFmpeg input count vary row to row.
    music_min_seconds: float = MUSIC_MIN_SECONDS
    # ---- split audio. Cuts the PROMO's own audio into split_audio_chunks equal
    # pieces and replays each at its own random tempo, so it runs fast in places
    # and slow in others and still ends exactly with the picture. Picture and
    # sound drift apart in the middle by design. Pitch is preserved (atempo).
    # The chunk tempos are drawn from the row's seed, so every output video in a
    # batch warps differently and re-rendering a sheet reproduces it exactly.
    split_audio: bool = False
    split_audio_chunks: int = SPLIT_AUDIO_CHUNKS
    # How far tempos stray from 1.0. 0.35 gives roughly 0.74x-1.54x.
    split_audio_spread: float = SPLIT_AUDIO_SPREAD
    font_path: Optional[str] = None   # the user's uploaded TTF/OTF, if any
    # Default font + artistic style for texts whose *_Font / *_Style cells are
    # blank. default_font is a FONT_LIBRARY key, FONT_SYSTEM, or FONT_CUSTOM.
    default_font: str = FONT_SYSTEM
    default_style: str = "classic"
    # Optional fixed fit box per text role, as (width, height) in canvas pixels;
    # 0 (either dimension) = off, which is the default and leaves every text
    # behaving exactly as it did before. A row's <Role>_Width/<Role>_Height cells
    # override these. The box is CENTRED on the text's X/Y — see TEXT_BOX_COLUMNS.
    headline_box_w: int = 0
    headline_box_h: int = 0
    subheading_box_w: int = 0
    subheading_box_h: int = 0
    footer_box_w: int = 0
    footer_box_h: int = 0
    # Default translucency for every text and every highlight box, as a 0..1
    # factor (1.0 = fully opaque). A row's <Role>_Opacity / <Role>_BgOpacity
    # cell overrides it per element, and both MULTIPLY any alpha already in the
    # color cell (an '#RRGGBBAA' value).
    text_opacity: float = 1.0
    text_bg_opacity: float = 1.0
    # Experimental subliminal / persistence-of-vision text effect (see TextSpec).
    # subliminal_targets names the text roles the effect applies to by default
    # (up to two of "Headline" / "Subheading" / "Footer"; empty = off) — it is a
    # CTA treatment, so it deliberately never applies to every text at once. A
    # row's <Role>_Subliminal cell still overrides this per text. K is the number
    # of frames per cycle (each frame omits ~1/K of the tokens), phase shifts the
    # cycle, granularity splits by "word" or "char". all_intra forces every output
    # frame to be independently coded so the "no whole frame" property survives a
    # frame-by-frame scrub of OUR file — at a big file-size cost.
    subliminal_targets: list = field(default_factory=list)
    # How each frame is built from the token groups:
    #   "hide" — show the whole text MINUS one rotating group (~1/K hidden).
    #            Each token is lit (K-1)/K of the time, so it stays bright and
    #            reads as solid text. The default.
    #   "show" — show ONLY one rotating group and hide the rest (~1/K shown).
    #            Each token is lit just 1/K of the time, so it time-averages to
    #            roughly 1/K brightness — faint and ghostly. Raising fps does not
    #            change that ratio; it only shortens the cycle.
    subliminal_mode: str = "hide"
    # How the hidden tokens are chosen each frame (hide mode only):
    #   "ordered" — the fixed comb: token t is hidden in frame t % K, so the same
    #               characters blank out in the same frames every cycle.
    #   "random"  — hide a random subset each frame, balanced (least-recently-
    #               hidden first, random ties) so no token stays hidden and the
    #               pattern doesn't repeat. Seeded per row, so the preview still
    #               matches the render. At ~1/K hidden this becomes a random
    #               partition — every token hidden EXACTLY ONCE per cycle.
    subliminal_pattern: str = "random"
    # Random hide only: percent of tokens hidden per frame. ~100/K keeps the
    # "hidden exactly once per cycle" property and stays bright; higher hides more
    # per frame (fainter). Clamped so at least one token is hidden and at least
    # one shown (no frame is ever whole, none ever fully blank).
    subliminal_hide_pct: int = 33
    subliminal_k: int = 3
    subliminal_phase: int = 0
    subliminal_granularity: str = "word"   # "word" | "char"
    subliminal_all_intra: bool = True
    # Seconds per row before a render is killed. Env-backed so it can be
    # tuned without a rebuild — see config.RENDER_TIMEOUT.
    ffmpeg_timeout: int = field(default_factory=lambda: config.RENDER_TIMEOUT)
    # Encoder threads for THIS row's FFmpeg. 0 = x264's own auto-detect, which
    # is the right default: capping it was measured 2-4x SLOWER, because frame
    # threading is most of a row's speed. See config.FFMPEG_THREADS.
    ffmpeg_threads: int = field(default_factory=lambda: config.FFMPEG_THREADS)
    # Decoder threads per INPUT. This is the one that pays: a row claiming
    # dozens of inputs otherwise gives each its own core-count-sized decoder
    # pool. 0 = auto (the old behaviour). See config.FFMPEG_DECODE_THREADS.
    decode_threads: int = field(default_factory=lambda: config.FFMPEG_DECODE_THREADS)


@dataclass
class TextSpec:
    text: str
    role: str                # 'Headline' / 'Subheading' / 'Footer'
    size: Optional[int]      # None = randomize within the role's size range
    color: Optional[tuple]   # None = random palette color
    x: Optional[int]         # None = auto-place (X/Y cell left blank in the Excel)
    y: Optional[int]
    # Optional fixed fit box (<Role>_Width/<Role>_Height), CENTRED on x/y. When
    # both are set, _fit_text_box re-wraps to box_w and derives `size` from the
    # box rather than reading it. Either one absent = the classic behaviour,
    # where the text sizes itself and wraps against the canvas.
    box_w: Optional[int] = None
    box_h: Optional[int] = None
    font: Optional[str] = None       # font choice name; None = config.default_font
    bg_color: Optional[tuple] = None # highlight box behind the text; None = no box
    style: Optional[str] = None      # classic|outline|shadow|neon; None = default_style
    # Translucency, 0..1 (None = config.text_opacity / text_bg_opacity). Folded
    # into color/bg_color's alpha during resolution — after that the alpha
    # channel is the single source of truth for both the render and the editor.
    opacity: Optional[float] = None
    bg_opacity: Optional[float] = None
    # Experimental "subliminal" / persistence-of-vision effect: the text is split
    # across frames so no single frame shows all of it, cycling fast enough to
    # read as whole in motion. `subliminal` is the requested flag (None = inherit
    # RenderConfig.subliminal_enabled); it is demoted to False during resolution
    # when the text is too short to split. `sub_k` / `sub_rects` cache the
    # resolved cycle length and per-token pixel boxes (see _resolve_subliminal).
    subliminal: Optional[bool] = None
    sub_k: int = 0
    sub_rects: Optional[list] = None
    # A matched CUSTOM_SUBLIMINAL_SCHEDULES entry (None = generic machinery).
    sub_custom: Optional[dict] = None


@dataclass
class RowSpec:
    """A validated, defaulted view of one Excel row."""
    bg_image: str
    headline: TextSpec
    subheading: TextSpec
    footer: TextSpec
    warnings: list[str] = field(default_factory=list)
    # Per-row video box, in canvas pixels; X/Y are the box's TOP-LEFT corner.
    # Filled from the row's Video_X/Video_Y/Video_Width/Video_Height cells when
    # present; blank cells are completed by VideoGenerator._resolve_positions
    # (the configured box, or a random position when randomize_video_pos is on).
    video_x: Optional[int] = None
    video_y: Optional[int] = None
    video_w: Optional[int] = None
    video_h: Optional[int] = None
    # Per-row CTA box, same scheme: parsed from the CTA_X/CTA_Y/CTA_Width/
    # CTA_Height cells; blank cells fall back to the sidebar values.
    cta_x: Optional[int] = None
    cta_y: Optional[int] = None
    cta_w: Optional[int] = None
    cta_h: Optional[int] = None
    cta_fade_start: Optional[float] = None
    cta_fade_duration: Optional[float] = None
    # Per-row CTA-video box + fade (CTA_Video_* cells); blank => sidebar values.
    cta_video_x: Optional[int] = None
    cta_video_y: Optional[int] = None
    cta_video_w: Optional[int] = None
    cta_video_h: Optional[int] = None
    cta_video_fade_start: Optional[float] = None
    cta_video_fade_duration: Optional[float] = None
    # Row-wide speed override (CTA_Video_Speed); None = use per-clip values.
    cta_video_speed: Optional[float] = None
    # Per-slot speed overrides from the CTA_Video_Speed_<i> cells (None where
    # blank). Resolved into cta_video_clip_speeds (aligned with cta_video_clips).
    cta_clip_speeds: Optional[list] = None
    cta_video_clip_speeds: Optional[list] = None
    # Per-slot pinned sample names from the CTA_Clip_<i> cells (None = pick at
    # random). Resolved to actual file paths (in play order) in cta_video_clips
    # by _resolve_positions.
    cta_clip_names: Optional[list] = None
    cta_video_clips: Optional[list] = None
    # Per-row gif box + fade (GIF_* cells); blank => sidebar values. There is no
    # per-row gif COUNT: the sequence length is derived from the promo duration.
    gif_x: Optional[int] = None
    gif_y: Optional[int] = None
    gif_w: Optional[int] = None
    gif_h: Optional[int] = None
    gif_fade_start: Optional[float] = None
    gif_fade_duration: Optional[float] = None
    # The resolved gif sequence, filled by _resolve_gif_sequence: the chosen
    # files in play order, and — index-aligned with them — how many times each
    # must repeat to satisfy the dwell floor. The two lists MUST stay the same
    # length: build_ffmpeg_command zips them into `-stream_loop N -i path`, so a
    # mismatch would emit fewer inputs than the index arithmetic accounts for.
    gif_clips: Optional[list] = None
    gif_clip_repeats: Optional[list] = None
    # Per-row background-video box (BG_Video_* cells); blank => sidebar values
    # (full canvas by default). X/Y are the box's TOP-LEFT corner.
    bg_video_x: Optional[int] = None
    bg_video_y: Optional[int] = None
    bg_video_w: Optional[int] = None
    bg_video_h: Optional[int] = None
    # The row's background-video sequence, filled by _resolve_bg_video_sequence:
    # the chosen clips in play order and, index-aligned with them, how many
    # times each repeats to satisfy the dwell floor. Same contract as
    # gif_clips/gif_clip_repeats — the two lists MUST stay the same length,
    # because build_ffmpeg_command zips them into `-stream_loop N -i path`.
    bg_video_clips: Optional[list] = None
    bg_video_clip_repeats: Optional[list] = None
    # The row's music sequence, filled by _resolve_music_sequence. Same contract
    # as gif_clips/gif_clip_repeats: index-aligned, and build_ffmpeg_command
    # zips them into `-stream_loop N -i path`, so they MUST stay equal length.
    music_clips: Optional[list] = None
    music_clip_repeats: Optional[list] = None
    # The sheet's row number (1-based), mixed into placement_seed so two rows
    # with identical text still get distinct random picks — CTA-video clips,
    # colors, sizes, auto-placement. Without it, a templated sheet where every
    # row shares the same text (common in split-screen, which has no background
    # to vary the seed) renders N identical videos. None = unknown, which keeps
    # the historical content-only seed (used by ad-hoc callers/tests).
    seed_salt: Optional[int] = None
    resolved: bool = False

    @classmethod
    def from_row(cls, row: pd.Series, row_number: Optional[int] = None) -> "RowSpec":
        warnings: list[str] = []

        def text_spec(prefix: str) -> TextSpec:
            return TextSpec(
                text=_clean_str(row.get(prefix)),
                role=prefix,
                size=_parse_opt_int(row.get(f"{prefix}_Size"), warnings, f"{prefix}_Size"),
                color=_parse_color(row.get(f"{prefix}_Color"), warnings, f"{prefix}_Color"),
                x=_parse_opt_int(row.get(f"{prefix}_X"), warnings, f"{prefix}_X"),
                y=_parse_opt_int(row.get(f"{prefix}_Y"), warnings, f"{prefix}_Y"),
                box_w=_parse_opt_int(row.get(f"{prefix}_Width"), warnings,
                                     f"{prefix}_Width"),
                box_h=_parse_opt_int(row.get(f"{prefix}_Height"), warnings,
                                     f"{prefix}_Height"),
                font=_clean_str(row.get(f"{prefix}_Font")) or None,
                bg_color=_parse_color(row.get(f"{prefix}_BgColor"), warnings,
                                      f"{prefix}_BgColor", fallback_desc="no background"),
                opacity=_parse_opacity(row.get(f"{prefix}_Opacity"), warnings,
                                       f"{prefix}_Opacity"),
                bg_opacity=_parse_opacity(row.get(f"{prefix}_BgOpacity"), warnings,
                                          f"{prefix}_BgOpacity"),
                style=(_clean_str(row.get(f"{prefix}_Style")).lower() or None),
                subliminal=_parse_opt_bool(row.get(f"{prefix}_Subliminal"), warnings,
                                           f"{prefix}_Subliminal"),
            )

        return cls(
            bg_image=_clean_str(row.get("BG_Image")),
            headline=text_spec("Headline"),
            subheading=text_spec("Subheading"),
            footer=text_spec("Footer"),
            video_x=_parse_opt_int(row.get("Video_X"), warnings, "Video_X"),
            video_y=_parse_opt_int(row.get("Video_Y"), warnings, "Video_Y"),
            video_w=_parse_opt_int(row.get("Video_Width"), warnings, "Video_Width"),
            video_h=_parse_opt_int(row.get("Video_Height"), warnings, "Video_Height"),
            cta_x=_parse_opt_int(row.get("CTA_X"), warnings, "CTA_X"),
            cta_y=_parse_opt_int(row.get("CTA_Y"), warnings, "CTA_Y"),
            cta_w=_parse_opt_int(row.get("CTA_Width"), warnings, "CTA_Width"),
            cta_h=_parse_opt_int(row.get("CTA_Height"), warnings, "CTA_Height"),
            cta_fade_start=_parse_opt_float(row.get("CTA_Fade_Start"), warnings, "CTA_Fade_Start"),
            cta_fade_duration=_parse_opt_float(row.get("CTA_Fade_Duration"), warnings, "CTA_Fade_Duration"),
            cta_video_x=_parse_opt_int(row.get("CTA_Video_X"), warnings, "CTA_Video_X"),
            cta_video_y=_parse_opt_int(row.get("CTA_Video_Y"), warnings, "CTA_Video_Y"),
            cta_video_w=_parse_opt_int(row.get("CTA_Video_Width"), warnings, "CTA_Video_Width"),
            cta_video_h=_parse_opt_int(row.get("CTA_Video_Height"), warnings, "CTA_Video_Height"),
            cta_video_fade_start=_parse_opt_float(
                row.get("CTA_Video_Fade_Start"), warnings, "CTA_Video_Fade_Start"),
            cta_video_fade_duration=_parse_opt_float(
                row.get("CTA_Video_Fade_Duration"), warnings, "CTA_Video_Fade_Duration"),
            cta_video_speed=_parse_opt_float(row.get("CTA_Video_Speed"), warnings, "CTA_Video_Speed"),
            cta_clip_speeds=[_parse_opt_float(row.get(col), warnings, col)
                             for col in CTA_SPEED_COLUMNS],
            cta_clip_names=[_clean_str(row.get(col)) or None for col in CTA_CLIP_COLUMNS],
            gif_x=_parse_opt_int(row.get("GIF_X"), warnings, "GIF_X"),
            gif_y=_parse_opt_int(row.get("GIF_Y"), warnings, "GIF_Y"),
            gif_w=_parse_opt_int(row.get("GIF_Width"), warnings, "GIF_Width"),
            gif_h=_parse_opt_int(row.get("GIF_Height"), warnings, "GIF_Height"),
            gif_fade_start=_parse_opt_float(
                row.get("GIF_Fade_Start"), warnings, "GIF_Fade_Start"),
            gif_fade_duration=_parse_opt_float(
                row.get("GIF_Fade_Duration"), warnings, "GIF_Fade_Duration"),
            bg_video_x=_parse_opt_int(row.get("BG_Video_X"), warnings, "BG_Video_X"),
            bg_video_y=_parse_opt_int(row.get("BG_Video_Y"), warnings, "BG_Video_Y"),
            bg_video_w=_parse_opt_int(row.get("BG_Video_Width"), warnings, "BG_Video_Width"),
            bg_video_h=_parse_opt_int(row.get("BG_Video_Height"), warnings, "BG_Video_Height"),
            seed_salt=row_number,
            warnings=warnings,
        )

    @property
    def text_elements(self) -> list[TextSpec]:
        return [self.headline, self.subheading, self.footer]

    def placement_seed(self) -> int:
        """Stable per-row seed so randomized styling/placement varies across
        rows but is reproducible for the same row — the preview matches the
        final render and re-running a batch yields identical layouts. (Only
        intrinsic row content goes in; sizes/colors may themselves be drawn
        from this seed.)"""
        key = "|".join([self.bg_image] + [t.text for t in self.text_elements])
        seed = zlib.crc32(key.encode("utf-8"))
        # Fold in the row's position so identical-content rows still diverge.
        # Chained through crc32 (deterministic), and only when a row number is
        # known — an unsalted spec keeps the exact historical content-only seed.
        if self.seed_salt is not None:
            seed = zlib.crc32(f"|#{int(self.seed_salt)}".encode("utf-8"), seed)
        return seed


@dataclass
class RowResult:
    row_number: int           # 1-based Excel data row
    ok: bool
    filename: Optional[str] = None
    output_path: Optional[Path] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- engine

class VideoGenerator:
    """Renders one MP4 per Excel row. Thread-safe: render_row() may be called
    from multiple worker threads (each row uses uniquely-named temp files)."""

    def __init__(
        self,
        config: RenderConfig,
        bg_dir: Path,
        video_path: Path,
        cta_path: Optional[Path],
        work_dir: Path,
        output_dir: Path,
        cta_video_slots: Optional[list] = None,
        gif_paths: Optional[list] = None,
        bg_video_paths: Optional[list] = None,
        music_paths: Optional[list] = None,
    ):
        self.config = config
        self.video_path = Path(video_path)
        # Optional CTA video: a list of slots (positions 1..N that play in fixed
        # order); each slot is a pool of sample clips, one of which is chosen per
        # output video. Empty/all-empty => the CTA video element is skipped.
        self.cta_video_slots = [[Path(p) for p in (slot or [])]
                                for slot in (cta_video_slots or [])]
        self._has_cta_video = any(self.cta_video_slots)
        # Optional GIF layer: one FLAT pool (no slots — gifs have no position
        # semantics and the sequence length is derived, not chosen). Empty =>
        # the layer is skipped entirely.
        self.gif_paths = [Path(p) for p in (gif_paths or [])]
        self._has_gifs = bool(self.gif_paths)
        self.work_dir = Path(work_dir)
        self.output_dir = Path(output_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = find_ffmpeg()
        logger.info("Using FFmpeg binary: %s", self.ffmpeg)

        # Guard against inputs that would make FFmpeg die mid-render with the
        # cryptic "Stream specifier ':v' ... matches no streams" error: the main
        # video must have a video track, and side clips without one (audio-only
        # or corrupt uploads) are skipped with a warning naming the file. The
        # warnings are surfaced by the app after construction.
        self.input_warnings: list[str] = []
        if not _has_video_stream(self.ffmpeg, self.video_path):
            raise ValueError(
                "The promo video has no video stream — it looks audio-only or "
                "corrupt. Re-export it as a normal MP4 video.")
        checked_slots: list[list[Path]] = []
        for i, slot in enumerate(self.cta_video_slots, start=1):
            good = []
            for p in slot:
                if _has_video_stream(self.ffmpeg, p):
                    good.append(p)
                else:
                    self.input_warnings.append(
                        f"Clip slot {i}: '{p.name}' has no video stream "
                        "(audio-only or corrupt) — skipped. Rows would otherwise "
                        "fail with an FFmpeg 'matches no streams' error.")
            checked_slots.append(good)
        self.cta_video_slots = checked_slots
        self._has_cta_video = any(self.cta_video_slots)
        good_gifs = []
        for p in self.gif_paths:
            if _has_video_stream(self.ffmpeg, p):
                good_gifs.append(p)
            else:
                self.input_warnings.append(
                    f"GIF '{p.name}' has no video stream (audio-only or "
                    "corrupt) — skipped. Rows would otherwise fail with an "
                    "FFmpeg 'matches no streams' error.")
        self.gif_paths = good_gifs
        self._has_gifs = bool(self.gif_paths)
        # The background-video pool: drop-and-warn per clip like the gifs,
        # never raise — one audio-only upload would otherwise fail every row.
        good_bgvs = []
        for p in (bg_video_paths or []):
            p = Path(p)
            if _has_video_stream(self.ffmpeg, p):
                good_bgvs.append(p)
            else:
                self.input_warnings.append(
                    f"Background video '{p.name}' has no video stream "
                    "(audio-only or corrupt) — skipped.")
        self.bg_video_paths = good_bgvs
        self._has_bg_videos = bool(self.bg_video_paths)
        # The music pool: audio files, so the check is the mirror image of the
        # clip pools' — drop anything with no AUDIO stream and warn, never
        # raise. A stray cover image dragged in with the tracks would otherwise
        # fail every row with FFmpeg's "matches no streams".
        good_music = []
        for p in (music_paths or []):
            p = Path(p)
            if _has_audio_stream(self.ffmpeg, p):
                good_music.append(p)
            else:
                self.input_warnings.append(
                    f"Music '{p.name}' has no audio stream (or isn't a media "
                    "file) — skipped.")
        self.music_paths = good_music
        self._has_music = bool(self.music_paths)
        # Whether the PROMO carries audio, probed once. `-map <promo>:a?` could
        # stay ignorant of this; a filter graph cannot (see _has_audio_stream).
        self._promo_has_audio = _has_audio_stream(self.ffmpeg, self.video_path)

        self._bg_index, self._bg_names = self._build_bg_index(Path(bg_dir))
        # Fonts are cached by (file path, named variation, size). The uploaded
        # custom font and the resolved system font back the FONT_CUSTOM /
        # FONT_SYSTEM choices; FONT_LIBRARY names resolve to ./fonts files.
        self._fonts: dict[tuple, ImageFont.FreeTypeFont] = {}
        self._font_lock = threading.Lock()
        self._custom_font = config.font_path
        self._system_font = find_default_font()
        if self._system_font is None:
            logger.warning("No TrueType font found; falling back to PIL default font")

        # The CTA image is optional. When supplied it's shared by every row, but
        # rows may override its box via CTA_Width/CTA_Height — cache one resized
        # copy per distinct size. Without one, the CTA-image layer is skipped
        # entirely (no reserved space, no FFmpeg input, no editor payload).
        self._has_cta = cta_path is not None
        self._cta_src = Image.open(cta_path).convert("RGBA") if self._has_cta else None
        self._cta_cache: dict[tuple[int, int], Image.Image] = {}
        self._cta_lock = threading.Lock()

        # First frame of each video, by path (promo + optional CTA video), for
        # the static preview / editor stand-in.
        self._preview_frame_lock = threading.Lock()
        self._video_frames: dict[str, Image.Image] = {}

        # Cached probed durations (seconds), by path. The side sequence fills the
        # main video's length by measuring durations (see _probe_duration /
        # _resolve_cta_sequence); each distinct file is probed once per batch.
        self._duration_lock = threading.Lock()
        self._durations: dict[str, Optional[float]] = {}

    # ------------------------------------------------------------- background lookup

    @staticmethod
    def _build_bg_index(bg_dir: Path) -> tuple[dict[str, Path], list[str]]:
        """Case-insensitive index of every image in the extracted ZIP, keyed both by
        bare filename and by relative path, so 'promo1.jpg' and 'summer/promo1.jpg'
        both resolve regardless of how the ZIP is structured. Also returns the
        sorted list of unique image names (relative paths) for random assignment."""
        index: dict[str, Path] = {}
        names: list[str] = []
        for path in sorted(bg_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                rel = path.relative_to(bg_dir).as_posix().lower()
                index.setdefault(rel, path)
                index.setdefault(path.name.lower(), path)
                names.append(rel)
        return index, names

    def resolve_bg(self, name: str) -> Path:
        if not name:
            raise FileNotFoundError("BG_Image cell is empty")
        key = name.replace("\\", "/").strip().lower()
        path = self._bg_index.get(key) or self._bg_index.get(Path(key).name)
        if path is None:
            raise FileNotFoundError(f"Background image '{name}' not found in the ZIP")
        return path

    def assign_backgrounds(self, df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        """Fill blank or missing BG_Image cells with images from the uploaded ZIP.

        Images are dealt like a shuffled deck: none repeats until every image
        has been used once (repeats are unavoidable when rows outnumber images,
        which adds a warning). Images explicitly referenced by other rows are
        excluded from the deal, and the shuffle is seeded from the ZIP contents
        and row count — so the preview and re-runs see the same assignment.
        """
        df = df.copy()
        if "BG_Image" not in df.columns:
            df["BG_Image"] = ""
        df["BG_Image"] = df["BG_Image"].astype("object")

        blank_mask = df["BG_Image"].map(lambda v: _clean_str(v) == "")
        needed = int(blank_mask.sum())
        if needed == 0:
            return df, []
        if not self._bg_names:
            # Backgrounds are optional: with no images uploaded, blank cells stay
            # blank and those rows render on the configured solid color.
            return df, []

        warnings: list[str] = []
        used_paths = set()
        for value in df.loc[~blank_mask, "BG_Image"]:
            try:
                used_paths.add(self.resolve_bg(_clean_str(value)))
            except FileNotFoundError:
                pass  # bad explicit name — reported per-row at render time
        pool = [n for n in self._bg_names if self.resolve_bg(n) not in used_paths]
        if not pool:
            pool = list(self._bg_names)
        if needed > len(pool):
            warnings.append(
                f"{needed} rows need a background but only {len(pool)} unused "
                f"images are in the ZIP — some backgrounds will repeat."
            )

        rng = random.Random(
            zlib.crc32(("|".join(self._bg_names) + f"|{len(df)}").encode("utf-8"))
        )
        deck: list[str] = []
        assigned: list[str] = []
        for _ in range(needed):
            if not deck:  # reshuffle a fresh deck only once the pool is exhausted
                deck = pool.copy()
                rng.shuffle(deck)
            assigned.append(deck.pop())
        df.loc[blank_mask, "BG_Image"] = assigned
        return df, warnings

    # ------------------------------------------------------------- PIL layers

    def _resolve_font(self, choice: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Map a font choice to (file path, named variation). A FONT_LIBRARY
        name/alias -> its bundled file; FONT_CUSTOM -> the uploaded font;
        FONT_SYSTEM/blank/unknown -> the system font (path may be None, meaning
        PIL's built-in default)."""
        key = _FONT_ALIASES.get(_font_norm(choice or ""), choice)
        if key in FONT_LIBRARY:
            filename, variation = FONT_LIBRARY[key]
            path = FONTS_DIR / filename
            if path.is_file():
                return str(path), variation
            # bundled file missing (fonts not fetched) — fall back gracefully
            return self._system_font, None
        if key == FONT_CUSTOM:
            return (self._custom_font or self._system_font), None
        return self._system_font, None

    def _get_font(self, path: Optional[str], variation: Optional[str],
                  size: int) -> ImageFont.ImageFont:
        cache_key = (path, variation, size)
        with self._font_lock:
            font = self._fonts.get(cache_key)
            if font is None:
                if path:
                    font = ImageFont.truetype(path, size)
                    if variation:
                        try:
                            font.set_variation_by_name(variation)
                        except Exception:  # noqa: BLE001 — not all fonts are variable
                            pass
                else:
                    font = ImageFont.load_default(size=size)
                self._fonts[cache_key] = font
            return font

    def _font_for(self, element: TextSpec) -> ImageFont.ImageFont:
        """The FreeType font for a text element at its (resolved) size, honoring
        its *_Font choice and falling back to the configured default font."""
        path, variation = self._resolve_font(element.font or self.config.default_font)
        return self._get_font(path, variation, element.size)

    @staticmethod
    def _contrast(color: tuple, alpha: Optional[int] = None) -> tuple:
        """Black on light text, white on dark text — used for outline strokes.
        The ring inherits the text's own alpha (unless overridden) so a
        translucent text doesn't get a solid outline around it."""
        r, g, b = color[:3]
        a = _alpha_of(color) if alpha is None else max(0, min(255, int(alpha)))
        return (0, 0, 0, a) if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else (255, 255, 255, a)

    @staticmethod
    def _style_metrics(element: TextSpec) -> tuple[int, int, int, int, int, int]:
        """Pixel sizes for an element's artistic style, derived from its font
        size: (outline stroke, shadow offset, shadow blur, neon glow blur,
        background padding, total tile padding). Zero where a feature is off."""
        s = max(1, int(element.size or 1))
        style = element.style or "classic"
        stroke = max(2, round(s / 12)) if style == "outline" else 0
        sh_off = max(2, round(s / 16)) if style == "shadow" else 0
        sh_blur = max(2, round(s / 18)) if style == "shadow" else 0
        glow = max(4, round(s / 6)) if style == "neon" else 0
        bg_pad = max(6, round(s * 0.30)) if element.bg_color else 0
        pad = stroke + sh_off + sh_blur * 3 + glow * 3 + bg_pad + 8
        return stroke, sh_off, sh_blur, glow, bg_pad, pad

    def _get_cta(self, w: int, h: int) -> Image.Image:
        with self._cta_lock:
            cta = self._cta_cache.get((w, h))
            if cta is None:
                cta = self._cta_src.resize((w, h), Image.LANCZOS)
                self._cta_cache[(w, h)] = cta
            return cta

    def build_base_image(self, spec: RowSpec) -> Image.Image:
        """Background layer: cover-crop (fill + center-crop) to exactly 1080x1920.
        Backgrounds are optional — a row with a blank BG_Image (no ZIP uploaded,
        or an empty pool) renders on the configured solid color instead."""
        if not spec.bg_image:
            try:
                color = ImageColor.getrgb(self.config.bg_color or "#1E1B4B")
            except ValueError:
                color = (30, 27, 75)
            return Image.new("RGB", (CANVAS_W, CANVAS_H), color)
        bg_path = self.resolve_bg(spec.bg_image)
        with Image.open(bg_path) as img:
            return ImageOps.fit(img.convert("RGB"), (CANVAS_W, CANVAS_H), Image.LANCZOS)

    def _wrap_text(self, element: TextSpec) -> None:
        """Re-flow an element's text so its rendered block fits on the canvas.

        Manual line breaks come first: a real line break inside the cell
        (Alt+Enter in Excel) or a '|' marker forces a break exactly there, for
        any of the three texts (the footer's automatic balancing is skipped).
        A manual line that is still too wide for the canvas keeps the automatic
        width-driven wrapping on top.

        Without any manual break, the automatic rules apply unchanged:
        - Footer: always balanced onto 3 lines (fewer if it has fewer words).
        - Headline/Subheading: kept on one line when it fits; greedily wrapped
          onto more lines when it would run past the canvas edges.
        - Safety valve: if a single word is wider than the canvas even alone,
          the font size is stepped down until it fits.
        Runs before measurement/placement so the auto-placer reserves space
        for the full wrapped block."""
        segments = [s for s in
                    (seg.strip() for seg in element.text.replace("|", "\n").split("\n"))
                    if s]
        words = [w for seg in segments for w in seg.split()]
        if not words:
            return
        max_w = CANVAS_W - 2 * PLACEMENT_MARGIN

        font = self._font_for(element)
        while element.size > 18 and max(font.getlength(w) for w in words) > max_w:
            element.size -= 2
            font = self._font_for(element)

        if len(segments) > 1:
            # Manual breaks: honor them verbatim; only a segment that would run
            # off the canvas is wrapped further.
            lines = []
            for seg in segments:
                seg_words = seg.split()
                if font.getlength(" ".join(seg_words)) <= max_w:
                    lines.append(" ".join(seg_words))
                else:
                    lines.extend(_greedy_wrap(seg_words, max_w, font.getlength))
        elif element.role == "Footer":
            lines = _balanced_lines(words, min(3, len(words)), font.getlength)
            # Balanced thirds can still overflow on extreme text; fall back to
            # width-driven wrapping in that case.
            if any(font.getlength(line) > max_w for line in lines):
                lines = _greedy_wrap(words, max_w, font.getlength)
        elif font.getlength(" ".join(words)) <= max_w:
            lines = [" ".join(words)]
        else:
            lines = _greedy_wrap(words, max_w, font.getlength)
        element.text = "\n".join(lines)

    def _fit_text_box(self, element: TextSpec, warnings: list[str]) -> None:
        """Re-flow and re-size a text so its painted block fills a fixed box.

        Replaces _wrap_text for any element carrying a box. The box drives the
        type: the text is wrapped to the box's width and the font size is
        searched for the largest value whose painted block still fits BOTH
        dimensions. Line breaks are recomputed from scratch each time, so a
        wider box pulls text back onto fewer lines and a narrower one adds them.

        Two things this measures that a naive fit would not:

        * The PAINTED block, not the glyphs. _style_metrics' padding scales with
          the font size — a neon glow alone is half the font size on every side —
          so measuring glyphs would let the treatment spill outside the box the
          user drew. The same text in the same box lands at 58px in `classic`
          and 45px in `neon`, and that is correct.
        * Manual line breaks. A real newline (Alt+Enter) or a '|' still forces a
          break; each segment is then wrapped further only if it is too wide.

        The size GROWS as well as shrinks, so <Role>_Size is not consulted for a
        boxed text — the box is the instruction. That is what keeps headlines
        optically consistent across a batch of wildly different lengths."""
        box_w, box_h = int(element.box_w), int(element.box_h)
        segments = [s for s in (seg.strip() for seg in
                                element.text.replace("|", "\n").split("\n")) if s]
        seg_words = [seg.split() for seg in segments if seg.split()]
        if not seg_words:
            return
        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        def layout(size: int) -> Optional[str]:
            """The wrapped text at `size`, or None if it cannot fit the box."""
            element.size = size
            font = self._font_for(element)
            pad = self._style_metrics(element)[5]
            avail_w, avail_h = box_w - 2 * pad, box_h - 2 * pad
            if avail_w <= 0 or avail_h <= 0:
                return None
            lines: list[str] = []
            for words in seg_words:
                # A single word wider than the box can't be wrapped out of
                # trouble — only a smaller size fixes it.
                if max(font.getlength(w) for w in words) > avail_w:
                    return None
                joined = " ".join(words)
                if font.getlength(joined) <= avail_w:
                    lines.append(joined)
                else:
                    lines.extend(_greedy_wrap(words, avail_w, font.getlength))
            text = "\n".join(lines)
            bbox = draw.multiline_textbbox((0, 0), text, font=font, anchor="mm",
                                           align="center")
            if bbox[2] - bbox[0] > avail_w or bbox[3] - bbox[1] > avail_h:
                return None
            return text

        # Binary search for the largest fitting size. Height grows monotonically
        # with the font size in every realistic case (a bigger face wraps onto
        # more, taller lines), and scanning the whole range linearly costs an
        # order of magnitude more for a result that differs by at most a pixel.
        lo, hi = TEXT_FIT_MIN_SIZE, TEXT_FIT_MAX_SIZE
        best_size, best_text = None, None
        while lo <= hi:
            mid = (lo + hi) // 2
            got = layout(mid)
            if got is not None:
                best_size, best_text, lo = mid, got, mid + 1
            else:
                hi = mid - 1
        if best_text is not None:
            element.size, element.text = best_size, best_text
            return

        # Nothing fits even at the floor. Draw it at the floor and let it
        # overflow, with a warning naming the text: clipping mid-word reads as a
        # rendering fault, and shrinking without a bound produces text nobody
        # can read and nothing to tell you why.
        element.size = TEXT_FIT_MIN_SIZE
        font = self._font_for(element)
        pad = self._style_metrics(element)[5]
        avail_w = max(1.0, box_w - 2 * pad)
        lines = []
        for words in seg_words:
            lines.extend(_greedy_wrap(words, avail_w, font.getlength))
        element.text = "\n".join(lines) or element.text
        warnings.append(
            f"{element.role}: the text does not fit its {box_w}x{box_h} box even "
            f"at {TEXT_FIT_MIN_SIZE}px — drawn at {TEXT_FIT_MIN_SIZE}px and it "
            "will overflow. Widen the box, shorten the text, or switch off the "
            "outline/glow style (its padding scales with the font size).")

    def _measure_text(self, element: TextSpec) -> tuple[float, float]:
        """Rendered width/height of a text block at its font size."""
        draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = draw.multiline_textbbox(
            (0, 0), element.text, font=self._font_for(element),
            anchor="mm", align="center",
        )
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    @staticmethod
    def _find_spot(fixed_x: Optional[int], fixed_y: Optional[int], w: float, h: float,
                   occupied: list[tuple], rng: random.Random,
                   y_bounds: Optional[tuple] = None) -> tuple[int, int, bool]:
        """Pick a CENTER point for a w x h box: rejection-sample random positions
        until one clears every occupied rect (with PLACEMENT_GAP breathing room).
        If the canvas is too crowded, settle for the sampled spot with the least
        total overlap. A provided coordinate pins that axis and only the missing
        one is randomized. `y_bounds` (top, bottom in canvas px) confines the
        vertical range — used when the output is cropped to the split band so
        auto-placed texts can't land in the cropped-away area."""
        def axis_range(fixed: Optional[int], size: float,
                       lo_lim: float, hi_lim: float) -> tuple[float, float]:
            if fixed is not None:
                return fixed, fixed
            lo = lo_lim + PLACEMENT_MARGIN + size / 2
            hi = hi_lim - PLACEMENT_MARGIN - size / 2
            mid = (lo_lim + hi_lim) / 2
            return (mid, mid) if lo > hi else (lo, hi)  # oversized: center it

        y_lo_lim, y_hi_lim = y_bounds if y_bounds else (0, CANVAS_H)
        x_lo, x_hi = axis_range(fixed_x, w, 0, CANVAS_W)
        y_lo, y_hi = axis_range(fixed_y, h, y_lo_lim, y_hi_lim)

        best, best_overlap = (CANVAS_W / 2, (y_lo_lim + y_hi_lim) / 2), float("inf")
        for _ in range(PLACEMENT_ATTEMPTS):
            cx, cy = rng.uniform(x_lo, x_hi), rng.uniform(y_lo, y_hi)
            rect = _rect_from_center(cx, cy, w, h)
            overlap = sum(_overlap_area(rect, occ, PLACEMENT_GAP) for occ in occupied)
            if overlap == 0:
                return int(cx), int(cy), True
            if overlap < best_overlap:
                best, best_overlap = (cx, cy), overlap
        return int(best[0]), int(best[1]), False

    def _resolve_positions(self, spec: RowSpec) -> None:
        """Fill in the row's randomized styling and layout.

        Resolution order matters:
          1. The CTA box and the video box size: the row's CTA_*/Video_Width/
             Video_Height cells win, blank cells fall back to the sidebar.
          2. Missing text sizes/colors are randomized — size affects how much
             space a text needs, so it must be fixed before placement. Colors
             are dealt from the palette without repeating within the row.
          3. Explicitly positioned texts are honored as-is (no-go zones).
          4. The video box position: the row's Video_X/Video_Y cells win;
             blank cells fall back to the configured position, or — with
             randomize_video_pos — a random spot avoiding the CTA and the
             explicit texts.
          5. Auto-placed texts then avoid the video box, CTA, explicit texts,
             and each other.
        All randomness comes from one RNG seeded per row content, so previews
        match final renders and re-runs are reproducible."""
        if spec.resolved:
            return
        spec.resolved = True

        cfg = self.config
        rng = random.Random(spec.placement_seed())

        # Split-screen: compute the two side-by-side panels up front. This sets
        # spec.video_*/cta_video_* to the panels, so the box-sizing, CTA-video
        # defaulting, and randomize/video-position code below all see concrete
        # values and leave them untouched.
        if cfg.layout_mode == "split":
            self._apply_split_layout(spec)

        def box_dim(value: Optional[int], default: int, limit: int,
                    label: str, lo: int = 50) -> int:
            if value is None:
                return default
            clamped = max(lo, min(value, limit))
            if clamped != value:
                spec.warnings.append(f"{label}: {value} is out of range, clamped to {clamped}")
            return clamped

        spec.video_w = box_dim(spec.video_w, cfg.video_w, CANVAS_W, "Video_Width")
        spec.video_h = box_dim(spec.video_h, cfg.video_h, CANVAS_H, "Video_Height")
        spec.cta_w = box_dim(spec.cta_w, cfg.cta_w, CANVAS_W, "CTA_Width", lo=10)
        spec.cta_h = box_dim(spec.cta_h, cfg.cta_h, CANVAS_H, "CTA_Height", lo=10)
        if spec.cta_x is None:
            spec.cta_x = cfg.cta_x
        if spec.cta_y is None:
            spec.cta_y = cfg.cta_y
        if spec.cta_fade_start is None:
            spec.cta_fade_start = cfg.cta_fade_start
        if spec.cta_fade_duration is None:
            spec.cta_fade_duration = cfg.cta_fade_duration
        cta_rect = (spec.cta_x, spec.cta_y,
                    spec.cta_x + spec.cta_w, spec.cta_y + spec.cta_h)
        # Only treat the CTA-image box as a no-go zone for auto-placement when an
        # image is actually supplied (its position is still resolved for the
        # editor payload, but an absent image reserves no space).
        cta_occupied = [cta_rect] if self._has_cta else []

        # CTA video box (resolved unconditionally so the FFmpeg command / preview
        # always have concrete numbers; only treated as a no-go zone for text
        # auto-placement when a CTA video is actually supplied).
        spec.cta_video_w = box_dim(spec.cta_video_w, cfg.cta_video_w, CANVAS_W, "CTA_Video_Width")
        spec.cta_video_h = box_dim(spec.cta_video_h, cfg.cta_video_h, CANVAS_H, "CTA_Video_Height")
        if spec.cta_video_x is None:
            spec.cta_video_x = cfg.cta_video_x
        if spec.cta_video_y is None:
            spec.cta_video_y = cfg.cta_video_y
        if spec.cta_video_fade_start is None:
            spec.cta_video_fade_start = cfg.cta_video_fade_start
        if spec.cta_video_fade_duration is None:
            spec.cta_video_fade_duration = cfg.cta_video_fade_duration
        # Row-wide speed override (CTA_Video_Speed): clamp if present, else leave
        # None so each clip falls back to its per-clip default below.
        if spec.cta_video_speed is not None:
            spec.cta_video_speed = max(0.25, min(float(spec.cta_video_speed), 4.0))
        cta_video_rect = (spec.cta_video_x, spec.cta_video_y,
                          spec.cta_video_x + spec.cta_video_w,
                          spec.cta_video_y + spec.cta_video_h)
        # Pick one sample per CTA slot (positions play in fixed order 1..N). A
        # CTA_Clip_<i> cell pins a sample by name; otherwise one is chosen at
        # random — seeded per row so it varies across output videos yet matches
        # the preview and re-runs. Each chosen clip also gets a resolved speed
        # (per-clip cell > row-wide cell > sidebar default), kept aligned with
        # cta_video_clips so build_ffmpeg_command can speed each clip separately.
        if self._has_cta_video:
            clip_rng = random.Random(spec.placement_seed() ^ 0xC7A)
            names = spec.cta_clip_names or []
            clip_speeds = spec.cta_clip_speeds or []
            chosen: list[Path] = []
            chosen_speeds: list[float] = []
            for i, slot in enumerate(self.cta_video_slots):
                if not slot:
                    continue
                name = names[i] if i < len(names) else None
                path = self._match_clip(slot, name) if name else None
                if name and path is None:
                    spec.warnings.append(
                        f"CTA_Clip_{i + 1}: '{name}' not found in clip {i + 1}'s "
                        f"samples, picking one at random"
                    )
                if path is None:
                    path = clip_rng.choice(slot)
                chosen.append(path)
                per_clip = clip_speeds[i] if i < len(clip_speeds) else None
                default = cfg.cta_video_speeds[i] if i < len(cfg.cta_video_speeds) else 1.0
                speed = per_clip if per_clip is not None else (
                    spec.cta_video_speed if spec.cta_video_speed is not None else default)
                chosen_speeds.append(max(0.25, min(float(speed), 4.0)))
            # Warn if the sheet pins a clip beyond the active slot count.
            for j in range(len(self.cta_video_slots), len(names)):
                if names[j]:
                    spec.warnings.append(
                        f"CTA_Clip_{j + 1}: only {len(self.cta_video_slots)} slot(s) "
                        f"are active, so this cell is ignored")
            if cfg.cta_video_fill or cfg.layout_mode == "split":
                self._fill_cta_sequence(spec, chosen, chosen_speeds, clip_rng)
            spec.cta_video_clips = chosen
            spec.cta_video_clip_speeds = chosen_speeds

        # Gif box (resolved unconditionally, same reasoning as the CTA video box
        # above: the editor needs concrete numbers whether or not a pool exists).
        spec.gif_w = box_dim(spec.gif_w, cfg.gif_w, CANVAS_W, "GIF_Width")
        spec.gif_h = box_dim(spec.gif_h, cfg.gif_h, CANVAS_H, "GIF_Height")
        if spec.gif_x is None:
            spec.gif_x = cfg.gif_x
        if spec.gif_y is None:
            spec.gif_y = cfg.gif_y
        if spec.gif_fade_start is None:
            spec.gif_fade_start = cfg.gif_fade_start
        if spec.gif_fade_duration is None:
            spec.gif_fade_duration = cfg.gif_fade_duration
        # Even origin: the gif is centered inside its box on an even offset (see
        # the pad expression in build_ffmpeg_command) to keep the yuv420p chroma
        # planes aligned. That only holds all the way through if the box itself
        # starts on an even pixel.
        spec.gif_x -= spec.gif_x % 2
        spec.gif_y -= spec.gif_y % 2
        gif_rect = (spec.gif_x, spec.gif_y,
                    spec.gif_x + spec.gif_w, spec.gif_y + spec.gif_h)
        if self._has_gifs:
            self._resolve_gif_sequence(spec)

        # Background-video box (resolved unconditionally, same reasoning as the
        # CTA video / gif boxes: the editor needs concrete numbers either way).
        # Full canvas by default. Never a no-go zone for text auto-placement —
        # at its opacity the texts are meant to sit on it.
        spec.bg_video_w = box_dim(spec.bg_video_w, cfg.bg_video_w, CANVAS_W,
                                  "BG_Video_Width")
        spec.bg_video_h = box_dim(spec.bg_video_h, cfg.bg_video_h, CANVAS_H,
                                  "BG_Video_Height")
        # Even box, for the same class of reason the gif box gets an even
        # origin: the sequence is carried through the graph as yuv420p (see
        # build_ffmpeg_command), whose chroma planes are half-resolution, and
        # scaling to an odd width or height has no valid chroma size. The
        # sidebar's own values are even; an Excel BG_Video_Width cell is what
        # can be odd, and one such cell would otherwise fail every row using it.
        spec.bg_video_w -= spec.bg_video_w % 2
        spec.bg_video_h -= spec.bg_video_h % 2
        if spec.bg_video_x is None:
            spec.bg_video_x = cfg.bg_video_x
        if spec.bg_video_y is None:
            spec.bg_video_y = cfg.bg_video_y
        # Opacity 0 turns the layer off entirely (build_ffmpeg_command gates on
        # it too), so don't probe a pool and emit warnings for a layer that
        # will never reach the output.
        if self._has_bg_videos and (cfg.bg_video_opacity or 0) > 0:
            self._resolve_bg_video_sequence(spec)

        # The music bed has no box and no z-order — it is only ever heard — so
        # unlike every layer above it there is nothing to resolve for the
        # editor. Gated on the volume for the same reason the background videos
        # are gated on opacity: a bed mixed at 0 never reaches the output, so
        # probing its pool would only produce warnings about a silent layer.
        if self._has_music and (cfg.music_volume or 0) > 0 and cfg.include_audio:
            self._resolve_music_sequence(spec)

        color_deck = list(RANDOM_TEXT_COLORS)
        for element in spec.text_elements:
            if not element.text:
                continue
            # Resolve font + artistic style defaults before sizing/wrapping,
            # since the font affects text width and the style affects padding.
            if element.font is None:
                element.font = cfg.default_font
            if not element.style:
                element.style = cfg.default_style
            if element.style not in TEXT_STYLES:
                spec.warnings.append(
                    f"{element.role}_Style: unknown style '{element.style}', using 'classic'")
                element.style = "classic"
            # The fit box: a row's cells first, else the sidebar default for
            # this role. Both dimensions are needed — a width alone has no
            # height to fit against, so it falls back to the classic behaviour.
            if element.box_w is None:
                element.box_w = getattr(cfg, f"{element.role.lower()}_box_w", 0)
            if element.box_h is None:
                element.box_h = getattr(cfg, f"{element.role.lower()}_box_h", 0)
            boxed = bool(element.box_w and element.box_h)
            if element.size is None:
                lo, hi = RANDOM_SIZE_RANGES[element.role]
                element.size = rng.randint(lo, hi)
            if element.color is None:
                pick = color_deck.pop(rng.randrange(len(color_deck)))
                element.color = (*ImageColor.getrgb(pick), 255)
            # Bake translucency into the alpha channel once, here: the cell wins
            # over the batch default, and both multiply any alpha the color cell
            # already carried. _resolve_positions runs once per spec (the
            # `resolved` guard), so this never compounds across calls.
            element.color = _apply_opacity(
                element.color,
                element.opacity if element.opacity is not None else cfg.text_opacity)
            element.bg_color = _apply_opacity(
                element.bg_color,
                element.bg_opacity if element.bg_opacity is not None else cfg.text_bg_opacity)
            # Lay the text out once its font and style are final. A boxed text
            # derives its size from the box (so the <Role>_Size above is only a
            # starting value the search discards); an unboxed one keeps the
            # classic rules — footer balanced to 3 lines, others wrapped to the
            # canvas.
            if boxed:
                self._fit_text_box(element, spec.warnings)
            else:
                self._wrap_text(element)

        explicit_rects: list[tuple] = []
        pending: list[tuple[TextSpec, float, float]] = []
        for element in spec.text_elements:
            if not element.text:
                continue
            w, h = self._measure_text(element)
            # A boxed text reserves its BOX, not the block that happened to fit
            # inside it: the box is the space the user allotted, and holding it
            # clear keeps the layout stable when the text later changes length.
            if element.box_w and element.box_h:
                w, h = float(element.box_w), float(element.box_h)
            if element.x is not None and element.y is not None:
                explicit_rects.append(_rect_from_center(element.x, element.y, w, h))
            else:
                pending.append((element, w, h))

        # A Video_X/Video_Y cell pins its axis (even when randomize is on);
        # _find_spot only randomizes the missing one. (Split mode already fixed
        # the video box to a panel, so randomization never applies there.)
        if (cfg.layout_mode != "split" and cfg.randomize_video_pos
                and (spec.video_x is None or spec.video_y is None)):
            fixed_cx = None if spec.video_x is None else int(spec.video_x + spec.video_w / 2)
            fixed_cy = None if spec.video_y is None else int(spec.video_y + spec.video_h / 2)
            cx, cy, clean = self._find_spot(
                fixed_cx, fixed_cy, spec.video_w, spec.video_h,
                cta_occupied + explicit_rects, rng,
            )
            spec.video_x = int(cx - spec.video_w / 2)
            spec.video_y = int(cy - spec.video_h / 2)
            if not clean:
                spec.warnings.append(
                    f"video box: no overlap-free spot found, "
                    f"placed at least-crowded position ({spec.video_x}, {spec.video_y})"
                )
        else:
            if spec.video_x is None:
                spec.video_x = cfg.video_x
            if spec.video_y is None:
                spec.video_y = cfg.video_y

        video_rect = (spec.video_x, spec.video_y,
                      spec.video_x + spec.video_w, spec.video_y + spec.video_h)
        # crop_to_panels (split only): the output is exactly the panel band, so
        # the video boxes are NOT no-go zones (texts overlay the videos) and
        # auto-placement is confined to the band. Anything explicitly placed
        # outside it would be cropped away — warn.
        crop_mode = cfg.layout_mode == "split" and cfg.crop_to_panels
        if crop_mode:
            band = (spec.video_y, spec.video_y + spec.video_h)
            occupied = cta_occupied + explicit_rects
            y_bounds = band
            for element in spec.text_elements:
                if element.text and element.y is not None and not (
                        band[0] <= element.y <= band[1]):
                    spec.warnings.append(
                        f"{element.role}_Y={element.y} is outside the panel band "
                        f"({band[0]}-{band[1]}) — it will be cropped out of the "
                        "output")
            if self._has_cta and not (band[0] <= spec.cta_y <= band[1]):
                spec.warnings.append(
                    f"CTA image at y={spec.cta_y} is outside the panel band "
                    f"({band[0]}-{band[1]}) — it will be cropped out of the output")
            # The gif box is not repositioned in split mode — it is a floating
            # accent over one of the panels, not a panel — but BOTH its edges
            # have to be inside the band or it is silently half-cropped. The
            # checks above test a single y because a text/CTA-image anchor is a
            # point; a box is not.
            if self._has_gifs and not (band[0] <= spec.gif_y
                                       and spec.gif_y + spec.gif_h <= band[1]):
                spec.warnings.append(
                    f"GIF box (y={spec.gif_y}..{spec.gif_y + spec.gif_h}) is not "
                    f"fully inside the panel band ({band[0]}-{band[1]}) — the "
                    "part outside will be cropped out of the output")
        else:
            occupied = [video_rect] + cta_occupied + explicit_rects
            if self._has_cta_video:
                occupied.append(cta_video_rect)
            if self._has_gifs:
                occupied.append(gif_rect)
            y_bounds = None
        for element, w, h in pending:
            x, y, clean = self._find_spot(element.x, element.y, w, h, occupied, rng,
                                          y_bounds)
            element.x, element.y = x, y
            if not clean:
                spec.warnings.append(
                    f"'{element.text[:40]}': no overlap-free spot found, "
                    f"placed at least-crowded position ({x}, {y})"
                )
            occupied.append(_rect_from_center(x, y, w, h))

        # Text positions are final now, so decide the subliminal split per text.
        self._resolve_subliminal(spec)

    def _resolve_subliminal(self, spec: RowSpec) -> None:
        """Decide the subliminal (persistence-of-vision) split for each text.
        The batch default applies to exactly ONE role (config.subliminal_target)
        — it's a CTA treatment, never something every text gets at once — and a
        row's <Role>_Subliminal cell overrides that per text. For texts that end
        up asking for it, this computes the token boxes and the effective cycle
        length K (capped at the token count). Texts too short to split (< 2
        tokens) are demoted to normal rendering with a warning. Results cache on
        the element (subliminal / sub_k / sub_rects) so the overlay build, the
        partial render, and the editor payload all agree."""
        cfg = self.config
        targets = {str(t).strip().lower() for t in (cfg.subliminal_targets or [])}
        for element in spec.text_elements:
            default_on = element.role.lower() in targets
            want = default_on if element.subliminal is None else element.subliminal
            element.subliminal = False
            element.sub_k = 0
            element.sub_rects = None
            element.sub_custom = None
            if not want or not element.text:
                continue
            # Hand-authored schedule for this exact text? It replaces the generic
            # machinery (and the last-4 rule) entirely.
            custom = CUSTOM_SUBLIMINAL_SCHEDULES.get(_norm_sub_text(element.text))
            if custom is not None:
                rects = self._token_rects(element, element.x, element.y, "char")
                need = max(e for frame in (custom["body_frames"]
                                           + custom["overlay_frames"])
                           for _, e in frame)
                if len(rects) >= need:
                    nb = len(custom["body_frames"])
                    no = len(custom["overlay_frames"])
                    element.subliminal = True
                    # lcm, capped. Every K costs one full-canvas FFmpeg input
                    # (see add_still in build_ffmpeg_command for what those cost
                    # in RAM), and an lcm grows fast enough to be a landmine:
                    # a 5-frame body against a 7-frame overlay is 35 stills, which
                    # clears MAX_TOTAL_FFMPEG_INPUTS and would take the box down.
                    # The cap truncates the cycle rather than the text — every
                    # piece still shows, just on a shorter period.
                    element.sub_k = min(nb * no // math.gcd(nb, no),
                                        SUBLIMINAL_MAX_K)
                    element.sub_rects = rects
                    element.sub_custom = custom
                    spec.warnings.append(
                        f"{element.role}: custom subliminal schedule active for "
                        "this text — the K / pattern / style settings and the "
                        "last-4-characters rule don't apply")
                    continue
            k_req = max(2, int(cfg.subliminal_k))
            gran = (cfg.subliminal_granularity or "word").lower()
            rects = self._token_rects(element, element.x, element.y, gran)
            # Fall back to character granularity when there aren't enough word
            # tokens to guarantee every frame omits at least one.
            if gran != "char" and len(rects) < k_req:
                rects_c = self._token_rects(element, element.x, element.y, "char")
                if len(rects_c) > len(rects):
                    rects = rects_c
            if len(rects) < 2:
                spec.warnings.append(
                    f"{element.role}: too short to split subliminally — shown normally")
                continue
            element.subliminal = True
            # Ordered and show mode partition tokens into groups, so there can be
            # at most one group per token (K capped at the token count). Random
            # hide instead cycles K distinct frames (the period) and hides a
            # rotating random subset, so it can use the full requested K even with
            # fewer tokens than frames.
            random_hide = ((cfg.subliminal_mode or "hide").lower() == "hide"
                           and (cfg.subliminal_pattern or "random").lower() == "random")
            element.sub_k = k_req if random_hide else min(k_req, len(rects))
            element.sub_rects = rects

    def _apply_split_layout(self, spec: RowSpec) -> None:
        """Split-screen: the main video fills one half of the canvas and the
        CTA-video sequence fills the other, as a centered vertical band whose
        height is cfg.split_panel_h. Overwrites both boxes' geometry on `spec`
        (so per-row Video_*/CTA_Video_* cells and randomize_video_pos are
        ignored). swap_sides puts the main video on the right instead of left."""
        cfg = self.config
        overridden = [label for label, val in (
            ("Video_X", spec.video_x), ("Video_Y", spec.video_y),
            ("Video_Width", spec.video_w), ("Video_Height", spec.video_h),
            ("CTA_Video_X", spec.cta_video_x), ("CTA_Video_Y", spec.cta_video_y),
            ("CTA_Video_Width", spec.cta_video_w), ("CTA_Video_Height", spec.cta_video_h),
        ) if val is not None]
        if overridden:
            spec.warnings.append(
                "split layout active — these per-row cells are ignored: "
                + ", ".join(overridden))
        half = CANVAS_W // 2
        panel_h = max(50, min(int(cfg.split_panel_h), CANVAS_H))
        panel_h -= panel_h % 2          # even sizes/offsets keep the yuv420p
        y0 = (CANVAS_H - panel_h) // 2  # crop (crop_to_panels) chroma-clean
        y0 -= y0 % 2
        main_box = (0, y0, half, panel_h)                 # (x, y, w, h)
        side_box = (half, y0, CANVAS_W - half, panel_h)
        if cfg.swap_sides:
            main_box, side_box = side_box, main_box
        spec.video_x, spec.video_y, spec.video_w, spec.video_h = main_box
        spec.cta_video_x, spec.cta_video_y, spec.cta_video_w, spec.cta_video_h = side_box
        # A split-screen panel is part of the layout, not an accent that appears
        # later: it must be solid from the first frame and stay to the end. Zero
        # the fade here (set, not None, so the sidebar defaults below don't
        # re-apply it); build_ffmpeg_command then skips the fade filter entirely.
        spec.cta_video_fade_start = 0.0
        spec.cta_video_fade_duration = 0.0

    def _fill_cta_sequence(self, spec: RowSpec, chosen: list, chosen_speeds: list,
                           clip_rng: random.Random) -> None:
        """Pad the already-picked side sequence so it covers the full main-video
        duration by appending fresh random clips drawn from the union of all
        slot pools. Mutates `chosen` / `chosen_speeds` in place. Degrades
        gracefully (leaves the sequence as-is, so the last frame holds) whenever
        a duration can't be measured; the muxer's -shortest trims the final
        overshoot clip."""
        main_dur = self._probe_duration(self.video_path)
        if main_dur is None:
            spec.warnings.append(
                "CTA video: couldn't measure the main video's duration; the side "
                "sequence isn't padded (its last frame may hold).")
            return
        # Union of every slot's pool, de-duplicated (a clip may appear in more
        # than one slot) but order-stable for reproducibility.
        seen: set[str] = set()
        pool: list[Path] = []
        for slot in self.cta_video_slots:
            for p in slot:
                if str(p) not in seen:
                    seen.add(str(p))
                    pool.append(p)
        if not pool:
            return

        # setpts=PTS/speed shortens a clip's on-screen time by its speed factor.
        def effective(path: Path, speed: float) -> Optional[float]:
            raw = self._probe_duration(path)
            return None if raw is None else raw / speed

        cumulative = 0.0
        for p, s in zip(chosen, chosen_speeds):
            eff = effective(p, s)
            if eff is None:
                spec.warnings.append(
                    "CTA video: couldn't measure a clip's duration; the side "
                    "sequence isn't padded (its last frame may hold).")
                return
            cumulative += eff
        # Fill clips have no slot index, so they take the row-wide speed if set,
        # otherwise normal speed.
        fill_speed = spec.cta_video_speed if spec.cta_video_speed is not None else 1.0
        fill_speed = max(0.25, min(float(fill_speed), 4.0))
        while cumulative < main_dur and len(chosen) < CTA_MAX_TOTAL_CLIPS:
            candidates = pool
            if len(pool) > 1 and chosen:
                prev = str(chosen[-1])
                candidates = [p for p in pool if str(p) != prev] or pool
            pick = clip_rng.choice(candidates)
            eff = effective(pick, fill_speed)
            if eff is None:
                spec.warnings.append(
                    "CTA video: couldn't measure a fill clip's duration; stopped "
                    "padding the side sequence early.")
                return
            chosen.append(pick)
            chosen_speeds.append(fill_speed)
            cumulative += eff
        if cumulative < main_dur and len(chosen) >= CTA_MAX_TOTAL_CLIPS:
            spec.warnings.append(
                f"CTA video: reached the {CTA_MAX_TOTAL_CLIPS}-clip cap before "
                "covering the full main video (clips may be very short).")

    def _deal_dwell_sequence(self, spec: RowSpec, pool: list, salt: int,
                             floor: float, cap: int, label: str,
                             item: str) -> tuple[list, list]:
        """Draw clips from a flat pool until they cover the promo, each held for
        at least `floor` seconds. Returns (clips, repeats), index-aligned.

        Shared by the gif layer and the background-video layer, which want the
        same thing: a derived-length sequence over a flat pool with a dwell
        floor. The sequence LENGTH IS DERIVED, not configured — with a dwell
        floor the count needed depends on the promo, and one sheet renders
        against up to MAX_PROMO_VIDEOS promos of different lengths, so any fixed
        count would be right for at most one of them.

        This is _fill_cta_sequence's shape but NOT its arithmetic, and those two
        must not be merged: a CTA clip contributes `duration / speed`, whereas a
        clip here contributes `duration * repeats` where `repeats` is itself
        derived from the duration. `salt` keeps each caller's draw independent —
        sharing one would lock two layers to the same picks in every row.
        """
        rng = random.Random(spec.placement_seed() ^ salt)
        target = self._probe_duration(self.video_path)
        if target is None:
            spec.warnings.append(
                f"{label}: couldn't measure the promo video's duration, so the "
                f"sequence length can't be derived — playing each {item} in the "
                "pool once. It may stop before the video ends.")

        chosen: list[Path] = []
        repeats: list[int] = []
        unmeasured: list[str] = []
        covered = 0.0
        # Deal from a shuffled deck rather than picking independently each time,
        # the same way backgrounds are assigned: every clip in the pool is used
        # once before any is used twice. Independent random picks look fine in
        # theory and bad in practice — with four gifs against a 20s promo they
        # produced green, amber, green, amber, leaving two uploads never shown.
        deck: list[Path] = []

        def draw() -> Path:
            if not deck:
                deck.extend(pool)
                rng.shuffle(deck)
                # Don't let a reshuffle put the same clip either side of the
                # seam; that is the one repeat the deck can't rule out.
                if chosen and len(deck) > 1 and str(deck[0]) == str(chosen[-1]):
                    deck.append(deck.pop(0))
            return deck.pop(0)

        # Without a target, fall back to one pass over the pool: bounded, and
        # with the floor applied it still covers `floor` seconds per clip.
        untargeted_limit = min(len(pool), cap)
        while len(chosen) < cap:
            if target is None:
                if len(chosen) >= untargeted_limit:
                    break
            elif covered >= target:
                break
            pick = draw()
            dur = _probe_video_duration(self.ffmpeg, pick)
            reps = gif_repeats(dur, floor)
            chosen.append(pick)
            repeats.append(reps)
            if dur:
                covered += dur * reps
            else:
                # Unmeasurable: it plays once and misses the floor, but the
                # failure stays bounded and named. Count the floor toward the
                # target anyway so an entirely unmeasurable pool can't spin the
                # loop all the way to the cap.
                unmeasured.append(pick.name)
                covered += floor
        if unmeasured:
            names = ", ".join(sorted(set(unmeasured))[:5])
            spec.warnings.append(
                f"{label}: couldn't measure the length of {names} — played once "
                f"instead of repeating to {floor:g}s.")
        if target is not None and covered < target:
            spec.warnings.append(
                f"{label}: reached the {cap}-{item} cap before covering the "
                f"whole video; the last {item}'s final frame will hold.")
        return chosen, repeats

    def _resolve_gif_sequence(self, spec: RowSpec) -> None:
        """Fill spec.gif_clips / spec.gif_clip_repeats for this row."""
        if not self.gif_paths:
            return
        # A different salt from the CTA picker's 0xC7A. Seeding both from the
        # same row seed with the same salt would lock the two layers together:
        # every row that drew CTA clip #3 would also draw gif #3, in every batch.
        spec.gif_clips, spec.gif_clip_repeats = self._deal_dwell_sequence(
            spec, self.gif_paths, 0x91F,
            max(0.1, float(self.config.gif_min_seconds or GIF_MIN_SECONDS)),
            GIF_MAX_TOTAL_CLIPS, "GIFs", "gif")

    def _resolve_bg_video_sequence(self, spec: RowSpec) -> None:
        """Fill spec.bg_video_clips / spec.bg_video_clip_repeats for this row.

        Same treatment as the gifs — a derived-length sequence over the flat
        pool — with its own dwell floor (longer: this layer is ambience, and a
        bed that changes every few seconds reads as flicker) and its own salt."""
        if not self.bg_video_paths:
            return
        spec.bg_video_clips, spec.bg_video_clip_repeats = self._deal_dwell_sequence(
            spec, self.bg_video_paths, 0xB6D,
            max(0.1, float(self.config.bg_video_min_seconds or BG_VIDEO_MIN_SECONDS)),
            BG_VIDEO_MAX_TOTAL_CLIPS, "Background videos", "background video")

    def _resolve_music_sequence(self, spec: RowSpec) -> None:
        """Fill spec.music_clips / spec.music_clip_repeats for this row.

        Third caller of the dwell-sequence dealer, and it wants exactly what the
        other two do: a derived-length sequence over a flat pool with a floor.
        That the items are audio rather than video changes nothing here — only
        how build_ffmpeg_command wires them up."""
        if not self.music_paths:
            return
        spec.music_clips, spec.music_clip_repeats = self._deal_dwell_sequence(
            spec, self.music_paths, 0x5D3,
            max(0.1, float(self.config.music_min_seconds or MUSIC_MIN_SECONDS)),
            MUSIC_MAX_TOTAL_CLIPS, "Music", "track")

    def _split_audio_tempos(self, spec: RowSpec) -> list[float]:
        """This row's per-chunk tempos, or [] when the effect is off/unusable.

        Seeded off the row like every other random choice, with its own salt so
        two rows that drew the same gifs do not also warp identically."""
        cfg = self.config
        if not (cfg.split_audio and cfg.include_audio and self._promo_has_audio):
            return []
        chunks = max(2, min(int(cfg.split_audio_chunks or SPLIT_AUDIO_CHUNKS),
                            SPLIT_AUDIO_MAX_CHUNKS))
        # The chunk boundaries are cut at fractions of the measured length, so
        # without a measurement there is nothing to cut. Warn rather than guess:
        # a silent no-op would read as "the checkbox does nothing".
        if self._probe_duration(self.video_path) is None:
            spec.warnings.append(
                "Split audio: couldn't measure the promo's duration, so the "
                "audio can't be cut into chunks — left at normal speed.")
            return []
        rng = random.Random(spec.placement_seed() ^ 0x5A7)
        return split_audio_tempos(chunks, cfg.split_audio_spread, rng)

    def _paint_text(self, target: Image.Image, ax: float, ay: float, element: TextSpec,
                    what: str = "full", fill: Optional[tuple] = None) -> None:
        """Draw one text element onto `target` (any RGBA image) with its block
        center at (ax, ay). Single source of truth for both the overlay render
        and the editor payload:

          what='full'       background box + artistic decoration + colored fill
          what='decoration' artistic decoration only (shadow/glow/outline ring),
                            no bg box, no fill — baked behind the editor's
                            recolorable glyph layer (the bg box is drawn live in
                            CSS, so it is intentionally excluded here)
          what='ink'        the fill glyphs only, in `fill` (white for the
                            editor's recolorable mask)
          what='bgbox'      the background highlight box only, no glyphs — the
                            always-on layer behind a subliminal text, whose
                            glyphs cycle in separate per-frame layers

        Shadow/glow use temporary layers the same size as `target`, so this
        works equally on a full canvas (overlay) or a small tile (editor).

        Translucent elements (alpha < 255, from an *_Opacity cell or an
        '#RRGGBBAA' color) are painted on their own scratch layer and
        alpha-composited: ImageDraw writes ink straight into the pixel without
        blending, which would otherwise punch a see-through hole through the
        highlight box / glow underneath instead of veiling it."""
        font = self._font_for(element)
        style = element.style or "classic"
        stroke, sh_off, sh_blur, glow, _bg_pad, _pad = self._style_metrics(element)
        draw = ImageDraw.Draw(target)
        common = dict(font=font, anchor="mm", align="center")
        text_alpha = _alpha_of(element.color)

        if what in ("full", "bgbox") and element.bg_color:
            l, t, r, b = draw.multiline_textbbox((ax, ay), element.text,
                                                 stroke_width=stroke, **common)
            pad = max(6, round(element.size * 0.30))
            radius = round((b - t + 2 * pad) * 0.30)
            box = Image.new("RGBA", target.size, (0, 0, 0, 0))
            ImageDraw.Draw(box).rounded_rectangle((l - pad, t - pad, r + pad, b + pad),
                                                  radius=radius, fill=element.bg_color)
            target.alpha_composite(box)

        if what in ("full", "decoration"):
            if sh_off:
                sh = Image.new("RGBA", target.size, (0, 0, 0, 0))
                ImageDraw.Draw(sh).multiline_text(
                    (ax + sh_off, ay + sh_off), element.text,
                    # A faint text casts a faint shadow.
                    fill=(0, 0, 0, round(170 * text_alpha / 255)), **common)
                if sh_blur:
                    sh = sh.filter(ImageFilter.GaussianBlur(sh_blur))
                target.alpha_composite(sh)
            if glow:
                gl = Image.new("RGBA", target.size, (0, 0, 0, 0))
                ImageDraw.Draw(gl).multiline_text((ax, ay), element.text,
                                                  fill=(*element.color[:3], text_alpha), **common)
                gl = gl.filter(ImageFilter.GaussianBlur(glow))
                target.alpha_composite(gl)
                target.alpha_composite(gl)  # double up for a brighter halo
            if what == "decoration" and stroke:
                # Outline ring only (transparent glyph body); the fill is the
                # editor's separate recolorable ink layer.
                draw.multiline_text((ax, ay), element.text, fill=(0, 0, 0, 0),
                                    stroke_width=stroke, stroke_fill=self._contrast(element.color),
                                    **common)

        if what in ("full", "ink"):
            fillc = fill if fill is not None else element.color
            # See the docstring: translucent glyphs go through a scratch layer so
            # they blend with the box/glow below instead of replacing it. The
            # outline ring is drawn in the same pass and inherits the same alpha,
            # so ring and body veil the background by the same amount.
            translucent = _alpha_of(fillc) < 255
            layer = Image.new("RGBA", target.size, (0, 0, 0, 0)) if translucent else target
            pen = ImageDraw.Draw(layer) if translucent else draw
            if what == "full" and stroke:
                pen.multiline_text((ax, ay), element.text, fill=fillc,
                                   stroke_width=stroke,
                                   stroke_fill=self._contrast(element.color, _alpha_of(fillc)),
                                   **common)
            else:
                pen.multiline_text((ax, ay), element.text, fill=fillc, **common)
            if translucent:
                target.alpha_composite(layer)

    def build_overlay_image(self, spec: RowSpec, include_cta: bool = True) -> Image.Image:
        """Text layer: transparent canvas with the three styled texts — plus the
        CTA video's first frame and the CTA image for static composites
        (previews). The video render passes include_cta=False because the CTA
        image and CTA video are separate FFmpeg inputs there, faded in (z-order
        preserved: CTA video over texts, CTA image on top).
        Each text is painted onto its own full-canvas layer and alpha-composited
        so styles (glow/shadow/outline/bg box) blend correctly even when texts
        overlap or run off-canvas. Text X/Y are the CENTER of the block."""
        self._resolve_positions(spec)
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        for element in spec.text_elements:
            if not element.text:
                continue
            layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
            if element.subliminal:
                # A subliminal text's GLYPHS are not baked here — they ship as
                # their own K cycled FFmpeg inputs (build_subliminal_layers), so
                # a static composite can't show the effect (it's motion-only).
                # Its highlight box, though, must stay put: it belongs to this
                # always-on layer, painted UNDER the cycling glyphs (the layer
                # sort puts the subliminal partials just above this one). Baking
                # the box into the partials instead would notch a hole in it
                # every time a character inside it is hidden.
                if not element.bg_color:
                    continue
                self._paint_text(layer, element.x, element.y, element, "bgbox")
            else:
                self._paint_text(layer, element.x, element.y, element, "full")
            canvas = Image.alpha_composite(canvas, layer)
        if include_cta:
            # Painted in the layers' DEFAULT z-order (gifs < CTA video < CTA
            # image), which is what makes overlapping boxes look right in the
            # still. Custom z-indexes are honoured by the render, not here — the
            # static preview has always been an approximation of the stacking.
            if self._has_gifs and spec.gif_clips:
                # Contain-fit the first gif of this row's sequence. Pasted WITH
                # its alpha as the mask, unlike the cover-filled CTA frame
                # below: whatever the aspect ratio leaves over is transparent,
                # and a maskless paste would punch an opaque hole through
                # everything under the box.
                tile = self._contain_frame(spec.gif_w, spec.gif_h,
                                           self._lead_gif_path(spec))
                canvas.paste(tile, (spec.gif_x, spec.gif_y), tile)
            if self._has_cta_video:
                # Cover-fill the box with the first clip in this row's order
                # (matches the render, which cover-fills each clip to the box).
                frame = self._cover_frame(spec.cta_video_w, spec.cta_video_h,
                                          self._lead_cta_path(spec))
                canvas.paste(frame.convert("RGBA"), (spec.cta_video_x, spec.cta_video_y))
            if self._has_cta:
                cta = self._get_cta(spec.cta_w, spec.cta_h)
                canvas.paste(cta, (spec.cta_x, spec.cta_y), cta)
        return canvas

    # ------------------------------------------------- subliminal text (POV)

    def _token_rects(self, element: TextSpec, ax: float, ay: float,
                     granularity: str) -> list:
        """Per-token pixel boxes for `element.text` (already wrapped into lines),
        in the same coordinate space _paint_text uses: the block is centered at
        (ax, ay), each line is horizontally centered (anchor='mm', align center).
        Returns [(token_index, (l, t, r, b)), ...]. `granularity` is 'word' or
        'char'. Left/right come from measuring the actual line substrings, so
        intra-line kerning is respected."""
        font = self._font_for(element)
        lines = element.text.split("\n")
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        spacing = 4  # PIL multiline_text default line spacing
        n = len(lines)
        block_h = n * line_h + (n - 1) * spacing
        top = ay - block_h / 2.0
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        rects: list = []
        t_idx = 0
        for li, line in enumerate(lines):
            line_top = top + li * (line_h + spacing)
            line_w = measure.textlength(line, font=font)
            x0 = ax - line_w / 2.0
            if granularity == "char":
                spans = [(i, i + 1) for i, ch in enumerate(line) if not ch.isspace()]
            else:
                spans = []
                i = 0
                while i < len(line):
                    if line[i].isspace():
                        i += 1
                        continue
                    j = i
                    while j < len(line) and not line[j].isspace():
                        j += 1
                    spans.append((i, j))
                    i = j
            for a, b in spans:
                left = x0 + measure.textlength(line[:a], font=font)
                right = x0 + measure.textlength(line[:b], font=font)
                rects.append((t_idx, (left, line_top, right, line_top + line_h)))
                t_idx += 1
        return rects

    @staticmethod
    def _balanced_hidden_sets(n: int, k: int, h: int, rng: random.Random) -> list:
        """K frames, each hiding exactly h of the n token indices, chosen so the
        per-token hide counts stay as even as possible: each frame hides the
        least-recently-hidden tokens, breaking ties randomly. Consequences —
          * every token is hidden close to k*h/n times (spread out, none stuck),
          * a token hidden this frame has a higher count, so it's unlikely to be
            picked again immediately (no obvious repeats),
          * at h == n/k it's a random partition: every token hidden EXACTLY once
            across the k frames."""
        counts = [0] * n
        sets = []
        for _ in range(k):
            order = list(range(n))
            rng.shuffle(order)                    # random tie-break
            order.sort(key=lambda c: counts[c])   # least-hidden first (stable)
            hide = order[:h]
            for c in hide:
                counts[c] += 1
            sets.append(set(hide))
        return sets

    def _subliminal_partials(self, element: TextSpec) -> list:
        """Render element.sub_k full-canvas RGBA layers, one per frame of the
        cycle. What each frame drops (or keeps) depends on the config:

          hide + "ordered" — hide token t in frame t % K (a fixed, even comb).
          hide + "random"  — hide a balanced random subset each frame (seeded per
                             row, so the preview matches the render); about
                             subliminal_hide_pct of the tokens per frame. At
                             ~100/K %% this is a random partition (each token
                             hidden exactly once per cycle); higher hides more.
          show             — show ONLY the tokens where t % K == j (~1/K shown).

        In every case no single frame carries the whole text and the union across
        the K frames is the whole text. Kept pixels are identical across frames
        because each derives from ONE correctly rendered raster."""
        if element.sub_custom:
            return self._custom_subliminal_partials(element)
        k = element.sub_k
        rects = element.sub_rects or []
        n = len(rects)
        cfg = self.config
        show_only = (cfg.subliminal_mode or "hide").lower() == "show"
        random_hide = (not show_only
                       and (cfg.subliminal_pattern or "random").lower() == "random")

        # Per-frame token-index sets: HIDDEN for hide mode, SHOWN for show mode.
        if random_hide:
            h = max(1, min(n - 1, round((cfg.subliminal_hide_pct or 33) / 100.0 * n)))
            # Guarantee every token still appears at least once per cycle: a token
            # may be hidden in at most k-1 of the k frames, so the total hides
            # (k*h) must fit within n*(k-1). Without this, a high hide % against a
            # small K (e.g. 70% at K=3) leaves some words hidden in EVERY frame —
            # they'd never be seen at all. Raise K to hide more per frame.
            h = max(1, min(h, (n * (k - 1)) // k))
            seed = zlib.crc32(("sub|" + element.role + "|" + element.text).encode("utf-8"))
            frame_sets = self._balanced_hidden_sets(n, k, h, random.Random(seed))
        else:  # ordered hide, or show — the fixed comb
            frame_sets = [{i for i in range(n) if i % k == j} for j in range(k)]

        # Render the styled text once, WITHOUT its highlight box: carving tokens
        # out of a raster containing the box would notch a hole in it every
        # frame. The box is painted once into the always-on static overlay
        # instead (build_overlay_image), directly beneath these cycling glyphs.
        saved_bg = element.bg_color
        element.bg_color = None
        try:
            full = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
            self._paint_text(full, element.x, element.y, element, "full")
        finally:
            element.bg_color = saved_bg
        stroke, sh_off, sh_blur, glow, _bg, _pad = self._style_metrics(element)
        # Enough to swallow the token's own style halo (glow/shadow/stroke are in
        # the max()), plus a small margin. Kept tight because at high hide
        # percentages a large margin would nibble the edges of the few glyphs
        # that ARE shown (their erased neighbours' boxes reach into them).
        pad = int(max(stroke, sh_off + sh_blur, glow)) + 2

        def padded(rect):
            l, t, r, b = rect
            return (max(0, int(l - pad)), max(0, int(t - pad)),
                    min(CANVAS_W, int(r + pad)), min(CANVAS_H, int(b + pad)))

        # Hardcoded tail rule: the last SUBLIMINAL_TAIL_CHARS characters follow
        # their OWN schedule, independent of the body. They alternate every frame
        # — the 1st & 4th shown together, then the 2nd & 3rd — so at most
        # SUBLIMINAL_TAIL_MAX_SHOWN of them are ever visible at once (exactly two
        # for a full 4-char tail). Measured on the CHARACTER boxes so it holds
        # whatever the body granularity is.
        char_rects = self._token_rects(element, element.x, element.y, "char")
        tail = char_rects[-SUBLIMINAL_TAIL_CHARS:]
        tail_m = len(tail)

        def tail_shown(j):
            # indices (into `tail`) to SHOW this frame; the rest stay hidden
            if tail_m >= 4:
                return (0, tail_m - 1) if j % 2 == 0 else (1, 2)
            if tail_m == 3:
                return (0, 2) if j % 2 == 0 else (1,)
            if tail_m == 2:
                return (0,) if j % 2 == 0 else (1,)
            return tuple(range(tail_m))

        # Per-character tail boxes. ERASE boxes are padded for style halos but
        # clamped so they never reach left of the first tail character —
        # otherwise the padding eats into the character just before the tail
        # (e.g. the 'd' of "…Friend.com" losing its right edge). PASTE boxes are
        # additionally clamped against both neighbours' tight boxes: cropping the
        # full raster with unclamped padding would carry a pad-wide sliver of a
        # HIDDEN neighbour's ink back in ("half a letter" artifacts).
        tail_left = int(tail[0][1][0]) if tail_m else 0

        def tail_erase_box(rect):
            b = padded(rect)
            return (max(b[0], tail_left), b[1], b[2], b[3])

        def tail_paste_box(ti):
            l, t, r, b = tail[ti][1]
            pl, pt, pr, pb = padded((l, t, r, b))
            # own territory: from the previous tail char's tight right edge to the
            # next one's tight left edge (char 0 stops at its own tight left so it
            # can't resurrect a body-hidden character before the tail).
            lo = int(tail[ti - 1][1][2]) if ti > 0 else tail_left
            hi = int(tail[ti + 1][1][0]) if ti + 1 < tail_m else pr
            return (max(pl, lo), pt, min(pr, hi), pb)

        partials = []
        for j in range(k):
            sel = frame_sets[j]
            if show_only:
                # Start empty and copy in only the shown tokens, so a neighbour's
                # padded box can't shave the kept glyphs.
                img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
                for t_idx, rect in rects:
                    if t_idx in sel:
                        box = padded(rect)
                        if box[2] > box[0] and box[3] > box[1]:
                            img.paste(full.crop(box), (box[0], box[1]))
            else:
                # Copy the full text and erase the hidden tokens. ImageDraw writes
                # pixels directly (no compositing), so this clears them to
                # transparent.
                img = full.copy()
                draw = ImageDraw.Draw(img)
                for t_idx, rect in rects:
                    if t_idx in sel:
                        draw.rectangle(padded(rect), fill=(0, 0, 0, 0))
            # Apply the tail schedule ON TOP of the body: clear the whole tail
            # region, then paste back only the scheduled characters, each via its
            # own alpha mask on its TIGHT box — so a hidden neighbour can't clip a
            # tiny glyph like '.', and a shown one isn't revealed by padding bleed.
            if tail_m:
                tdraw = ImageDraw.Draw(img)
                for _, rect in tail:
                    tdraw.rectangle(tail_erase_box(rect), fill=(0, 0, 0, 0))
                for ti in tail_shown(j):
                    box = tail_paste_box(ti)
                    if box[2] > box[0] and box[3] > box[1]:
                        glyph = full.crop(box)
                        img.paste(glyph, (box[0], box[1]), glyph)
            partials.append(img)
        return partials

    def _custom_subliminal_partials(self, element: TextSpec) -> list:
        """Frames for a hand-authored CUSTOM_SUBLIMINAL_SCHEDULES entry. Show-
        style: each frame starts empty and paints ONLY the scheduled pieces —
        body piece-set j % len(body) plus overlay piece-set j % len(overlay)
        (the overlay runs independently on top, like the last-4 rule it
        replaces). Every character is pasted from one correctly rendered raster
        via its own tight alpha-masked box, so contiguous pieces reassemble
        seamlessly and hidden neighbours can't bleed in."""
        sched = element.sub_custom
        rects = element.sub_rects or []
        body = sched["body_frames"]
        over = sched["overlay_frames"]
        saved_bg = element.bg_color
        element.bg_color = None
        try:
            full = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
            self._paint_text(full, element.x, element.y, element, "full")
        finally:
            element.bg_color = saved_bg
        partials = []
        for j in range(element.sub_k):
            shown: set[int] = set()
            for a, b in body[j % len(body)] + over[j % len(over)]:
                shown.update(range(a, b))
            img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
            for t_idx, rect in rects:
                if t_idx in shown:
                    box = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
                    if box[2] > box[0] and box[3] > box[1]:
                        glyph = full.crop(box)
                        img.paste(glyph, (box[0], box[1]), glyph)
            partials.append(img)
        return partials

    def build_subliminal_layers(self, spec: RowSpec) -> list:
        """One entry per subliminal text: {'role', 'k', 'images': [K RGBA imgs]}.
        Empty when no text uses the effect."""
        self._resolve_positions(spec)
        out = []
        for element in spec.text_elements:
            if element.subliminal and element.sub_k >= 2:
                out.append({
                    "role": element.role,
                    "k": element.sub_k,
                    "images": self._subliminal_partials(element),
                })
        return out

    @staticmethod
    def _match_clip(slot: list, name: str) -> Optional[Path]:
        """Find a sample in a slot by file name (case-insensitive, with or
        without extension). None if no match."""
        key = name.strip().lower()
        for p in slot:
            if p.name.lower() == key or p.stem.lower() == key:
                return p
        return None

    def _lead_cta_path(self, spec: RowSpec) -> Path:
        """The clip shown first in this row's CTA sequence (slot 1's pick)."""
        return spec.cta_video_clips[0]

    def _lead_gif_path(self, spec: RowSpec) -> Path:
        """The gif shown first in this row's sequence — the one frame a static
        preview can honestly show."""
        return spec.gif_clips[0]

    # ------------------------------------------------------------- FFmpeg

    # Every `[<n>:v]` reference in a filter_complex.
    _FILTER_INPUT_RE = re.compile(r"\[(\d+):v\]")
    _FILTER_AUDIO_INPUT_RE = re.compile(r"\[(\d+):a\]")

    @classmethod
    def _check_filter_inputs(cls, filter_complex: str, n_inputs: int,
                             audio_only: Iterable[int] = ()) -> None:
        """Assert that each of the n_inputs declared inputs is referenced
        exactly once in the filter graph.

        `audio_only` names the inputs carrying no video (the music bed), which
        are checked against their `[N:a]` references instead — they are a fourth
        variable-length run in the same command line and drift exactly as
        silently as the other three.

        This is a real guard, not a formality. With two clip layers plus the
        subliminal stills, the inputs form three variable-length runs in one
        command, and an index that drifts by one does not fail — an injected
        off-by-one was measured to exit 0 with empty stderr and silently render
        a different video (the gif box playing a subliminal text still, the CTA
        sequence eating a gif). By construction every input feeds exactly one
        filter chain here, so any drift shows up as one index referenced twice
        and another not at all, which this catches and a max-index check does
        not."""
        audio_only = set(audio_only)
        seen: dict[int, int] = {}
        for match in cls._FILTER_INPUT_RE.findall(filter_complex):
            index = int(match)
            seen[index] = seen.get(index, 0) + 1
        for match in cls._FILTER_AUDIO_INPUT_RE.findall(filter_complex):
            index = int(match)
            if index in audio_only:
                seen[index] = seen.get(index, 0) + 1
        missing = [i for i in range(n_inputs) if i not in seen]
        duplicated = sorted(i for i, count in seen.items() if count > 1)
        stray = sorted(i for i in seen if i >= n_inputs)
        if missing or duplicated or stray:
            raise RuntimeError(
                "Internal error building the FFmpeg command: input indices and "
                f"the filter graph disagree ({n_inputs} inputs declared; "
                f"unreferenced={missing}, referenced more than once={duplicated}, "
                f"out of range={stray}). Refusing to render rather than produce "
                "a video with the wrong clips in it.")

    def build_ffmpeg_command(self, spec: RowSpec, base_png: Path,
                             overlay_png: Path, cta_png: Optional[Path],
                             out_path: Path,
                             sub_layers: Optional[list] = None) -> list[str]:
        """
        Single-pass composite. Filter graph explained:

          [1:v]scale=W:H:force_original_aspect_ratio=decrease,split[vidA][vidB]
              Scale the uploaded video to fit INSIDE the configured box while
              preserving its aspect ratio (never distorts; upscales small sources,
              downscales large ones). force_divisible_by=2 keeps yuv chroma happy.
              `split` duplicates it: [vidA] anchors the render length, [vidB] is
              the promo's visible layer (painted in z-order below).

          [0:v][vidA]overlay=x='X+(W-w)/2':y='Y+(H-h)/2':shortest=1[anchored]
              Place the scaled video centered within its box on the background.
              'w'/'h' are the scaled video's runtime dimensions, so any leftover
              box area becomes implicit padding where the background shows through
              (nicer than black bars). shortest=1 is critical: the looped base
              image is an infinite stream, so this overlay must terminate when the
              video ends or the render would never finish. The promo is re-painted
              at the same box in z-order below, so this anchor paint is idempotent
              and never disturbs the chosen layering.

          CTA videos (present only when uploaded) -> [ctav]
              One sample is chosen per slot for this row. Each is cover-filled to
              the CTA box (scale=increase + crop, so all share one size) and sped
              up/slowed by its own `setpts=PTS/SPEED`, then joined with `concat`
              in the fixed slot order into one stream and alpha-faded in. They
              play once through (the last frame holds if the promo outlasts
              them); -shortest trims excess.

          GIFs (present only when a pool was supplied) -> [gifl]
              A different treatment from the CTA clips on both axes that matter:

              * Dwell. Each gif is claimed with `-stream_loop <repeats-1>`, an
                input-LEVEL option that replays the file before the filter graph
                sees it, so a 3s gif under a 5s floor arrives as 6s of video. The
                `loop` video filter would do the same thing by buffering decoded
                frames — measured at ~740MB peak against ~75MB for -stream_loop,
                and it silently appends a frozen tail when placed before `fps=`.
              * Fit. `scale=W:H:force_original_aspect_ratio=decrease` fits the
                gif INSIDE the box at the largest size the box allows, in both
                directions: oversized gifs shrink, undersized ones are enlarged
                until one side touches the edge. Nothing is cropped or
                distorted. `pad` then centres the result on the box at an even
                offset with a fully transparent colour, so whatever sits below
                shows through the area the aspect ratio leaves over instead of
                black bars.

              The pad is not cosmetic: concat REJECTS inputs of differing sizes
              ("Input link parameters do not match"), and contain-fitting gifs of
              assorted shapes produces exactly that. format=rgba must precede the
              pad or the transparent colour flattens to opaque black.

              No setpts here. -stream_loop already emits continuous monotonic
              PTS, concat re-stamps the joined timeline, and setpts=N/FRAME_RATE/TB
              was measured to drop one frame per segment.

          [N:v]format=rgba,fade=t=in:st=CFS:d=CFD:alpha=1 -> [cta]
              The CTA image (present only when one is uploaded) as its own
              stream: force an alpha-capable format, then fade ONLY the alpha
              channel — fully transparent until cta_fade_start, fully visible
              cta_fade_duration later.

          Z-order: the overlay layers — promo video [vidB], optional gifs [gifl],
          optional CTA video [ctav], optional CTA image [cta], the optional
          translucent background sequence [bgv], texts — stacked onto [anchored]
          in ascending order of their sidebar z-index (video_z / gif_z /
          cta_video_z / cta_image_z / text_z; higher = on top). The background
          is always the base. Equal z-indexes fall back to a fixed priority
          (promo < gifs < CTA video < CTA image < background beds < texts) so
          the order is deterministic; [bgv] has no z knob — it shares text_z
          with a lower tie-break, pinning it directly beneath the texts. The
          topmost overlay also converts to yuv420p — required for maximum
          player/social-platform compatibility.

        INPUT INDICES. Every input is claimed through add_input(), which appends
        it and returns the index it took; the filter graph is then written using
        those returned values. Nothing derives an index by summing the lengths of
        other lists. That used to be safe by construction with one run of clip
        inputs, but a second run makes it a hazard rather than a chore — an
        off-by-one was measured to produce a DIFFERENT video with exit code 0 and
        empty stderr (the gif box quietly playing a subliminal text still). It is
        not a class of bug that announces itself, so _check_filter_inputs asserts
        the real invariant afterwards: every input referenced exactly once.

        Still inputs use -loop 1 -framerate <fps> so they behave as streams
        aligned with the output rate (config.fps, 30 or 60). -shortest at the
        muxer trims audio to the video length; -movflags +faststart relocates the
        moov atom for instant playback start after upload.
        """
        cfg = self.config
        fps = int(cfg.fps or FPS)
        has_cta = self._has_cta
        clips = spec.cta_video_clips or []
        has_ctav = bool(self._has_cta_video and clips)
        gifs = list(spec.gif_clips or [])
        gif_reps = list(spec.gif_clip_repeats or [])
        has_gif = bool(self._has_gifs and gifs)
        bgvs = list(spec.bg_video_clips or [])
        bgv_reps = list(spec.bg_video_clip_repeats or [])
        has_bgv = bool(self._has_bg_videos and bgvs
                       and (cfg.bg_video_opacity or 0) > 0)
        # Same normalisation as the gifs, for the same reason: zip() truncates,
        # so a length mismatch would emit fewer -i than the filter graph
        # references and shift every later input by one.
        if len(bgv_reps) != len(bgvs):
            bgv_reps = (bgv_reps + [1] * len(bgvs))[:len(bgvs)]
        music = list(spec.music_clips or [])
        music_reps = list(spec.music_clip_repeats or [])
        has_music = bool(self._has_music and music and cfg.include_audio
                         and (cfg.music_volume or 0) > 0)
        # Same normalisation, same reason as the two above: zip() truncates, so
        # a length mismatch would emit fewer -i than the graph references.
        if len(music_reps) != len(music):
            music_reps = (music_reps + [1] * len(music))[:len(music)]
        sub_layers = sub_layers or []
        # These two are built together and stay aligned by construction, but a
        # mismatch here would be silent and expensive: zip() truncates, so the
        # command would carry fewer -i than the filter graph references and every
        # later input would shift by one. Normalise rather than trust.
        if len(gif_reps) != len(gifs):
            gif_reps = (gif_reps + [1] * len(gifs))[:len(gifs)]

        # ---- inputs: claimed in order, each handing back its own index -------
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        n_inputs = 0

        def add_input(*args: str) -> int:
            """Append one input to `cmd` and return the index it claimed.

            Each input gets its own decoder thread cap. `-threads` BEFORE a -i is
            an input option, so it sizes that input's DECODER; the one in the
            output options sizes the encoder. Every decoder otherwise
            auto-detects from the machine's core count, and this command claims
            up to MAX_TOTAL_FFMPEG_INPUTS of them — one measured row held 1,070
            threads and 5.5 GB for a fifteen-second promo, and the renderer runs
            many of these at once.

            Measured on one row, 22-25 inputs, idle box (wall / peak RSS):

                                       no subliminal   subliminal, 3 layers
                as-is (auto decoders)  20.1s 2934 MB   17.8s 3120 MB
                decoders capped to 1   23.0s 1944 MB   15.8s 2189 MB
                as-is, +audio split    31.5s 3041 MB
                capped to 1, +split    24.2s 2070 MB

            So it is ~30% less memory across the board, and on the two shapes
            this renderer actually runs — subliminal on, audio split on — it is
            also FASTER, because the decoder threads were competing with the
            encoder rather than helping. Capping the ENCODER is the opposite
            trade and was measured 2-4x SLOWER; see config.FFMPEG_THREADS."""
            nonlocal n_inputs
            index = n_inputs
            n_inputs += 1
            if cfg.decode_threads:
                cmd.extend(("-threads", str(cfg.decode_threads)))
            cmd.extend(args)
            return index

        # Every still is a `-loop 1` stream, and the OUTPUT -t / -shortest at the
        # end of this command do NOT bound the INPUT side — FFmpeg keeps reading
        # and buffering them regardless. With one sink that is survivable:
        # overlay's framesync backpressure holds the stills in step with the
        # promo. Add a SECOND sink — the [aout] audio chain, fed by a music input
        # demuxed independently of every video input — and FFmpeg 7's scheduler
        # alternates between sinks, letting the infinite stills run ahead into
        # unbounded filtergraph FIFOs.
        #
        # It is the input COUNT that tips it, because each buffered frame is
        # 1080*1920*4 = 8.3 MB and a subliminal row claims K stills on top of the
        # base and the text overlay. Measured on one 20s row, K=7, music bed on:
        #
        #     stills  peak RSS   result
        #        2     1.3 GB    ok, 42s          (no subliminal, no music)
        #        9     3.6 GB    ok, 69s          (subliminal, no music)
        #        2     1.3 GB    ok, 10s          (music, no subliminal)
        #        9    18.8 GB    LIVELOCK         (both — encoder frozen at
        #                                          frame 13, RSS still climbing)
        #        9     4.6 GB    ok, 65s          (both, with the -t below)
        #
        # Sixteen of the livelocked case in parallel is what the OOM killer
        # answered with "FFmpeg exited with code -9"; the rows that swapped
        # instead of dying hit ffmpeg_timeout. K=3 (the shipped default) survives
        # at 2.9 GB, which is why this went unnoticed until a job raised K.
        #
        # An input-side -t ends each still with the promo, so nothing can run
        # ahead. The margin is slack for container-vs-stream duration rounding:
        # the base still feeds an overlay with shortest=1, so it must OUTLAST the
        # promo or the whole render would be truncated to the still.
        still_dur = self._probe_duration(self.video_path)  # cached per path
        still_args = (["-t", f"{still_dur + 1.0:.3f}"] if still_dur else [])

        def clip_args(speed: float = 1.0) -> list:
            """The same bound for a real CLIP input (CTA sample, gif, bed,
            music), which the stills' `-t` never covered.

            The stills were bounded because an infinite `-loop 1` could run
            ahead forever. A finite clip can do the same damage without being
            infinite: a 60s CTA sample cover-filled to a 540x1920 panel and
            carried in rgba is 4.1 MB per frame, and five of them decoded ahead
            of a slow composite is tens of GB — measured on this box as ffmpeg
            processes OOM-killed at 62-81 GB RSS on rows whose promo was under
            ten seconds. Bounding each input to the promo's length caps the
            run-ahead at (promo x fps) frames per input instead of (clip x fps).

            Nothing visible can be lost: the OUTPUT ends with the promo, and
            these layers are concatenated in order, so any source past the
            promo's length would be painted after the last output frame.
            `speed` is the CTA clip's setpts multiplier — at 2x, two seconds of
            source make one second of screen time, so the source bound has to
            scale with it. Slower clips need less source, never more, hence the
            max(). No bound when the promo can't be probed, exactly as before."""
            if not still_dur:
                return []
            return ["-t", f"{(still_dur + 1.0) * max(1.0, speed):.3f}"]

        def add_still(path) -> int:
            """A still image as a stream at the output rate, bounded to the
            promo's length so it cannot run ahead of the encoder."""
            return add_input("-loop", "1", "-framerate", str(fps), *still_args,
                             "-i", str(path))

        base_i = add_still(base_png)
        promo_i = add_input("-i", str(self.video_path))
        text_i = add_still(overlay_png)
        cta_i = add_still(cta_png) if has_cta else None
        # Each CTA sample is bounded by its own speed: the filter graph below
        # replays this same per-clip speed with setpts, and the two must agree
        # or a sped-up clip would be cut short. Read exactly as there.
        cta_speeds = spec.cta_video_clip_speeds or []
        clip_ix = [add_input(*clip_args(cta_speeds[k] if k < len(cta_speeds) else 1.0),
                             "-i", str(p))
                   for k, p in enumerate(clips)]
        # -stream_loop N replays the file N extra times before decoding, which
        # is what turns a 3s gif into the 6s the dwell floor asks for. The -t
        # then caps the looped total at the promo's length.
        gif_ix = [add_input("-stream_loop", str(max(1, int(r)) - 1), *clip_args(),
                            "-i", str(p))
                  for p, r in zip(gifs, gif_reps)]
        # Each subliminal text is ONE tiny K-frame raw rgba clip (cropped to
        # the text's painted box, cycle and phase pre-baked into the frame
        # order by render_row), looped for the promo's length. This replaced K
        # full-canvas looped stills gated with per-frame enable expressions —
        # K full-frame PNG inflates per output frame plus gigabytes of
        # framesync buffering, for pixels that were mostly transparent.
        # The 3600s fallback keeps the loop finite when the promo can't be
        # probed — the same failure that silently strips still_args' -t. Only
        # the -stream_loop COUNT grows with it, never the raw file.
        loop_secs = (still_dur or 3600.0) + 1.0

        def add_sub(sl: dict) -> int:
            frames = max(1, math.ceil(loop_secs * fps))
            loops = max(0, math.ceil(frames / int(sl["k"])) - 1)
            return add_input(
                "-f", "rawvideo", "-pixel_format", "rgba",
                "-video_size", f"{sl['w']}x{sl['h']}",
                "-framerate", str(fps), "-stream_loop", str(loops),
                *still_args, "-i", str(sl["raw"]))

        sub_ix = [add_sub(sl) for sl in sub_layers]
        # Music is claimed before the background beds, and for the same reason
        # they are claimed last: the beds have a graceful degradation (a short
        # sequence holds its last frame) and the music does not. A bed that
        # under-fills is cosmetic; a track dropped for want of an input slot is
        # a gap of silence.
        music_ix = ([add_input("-stream_loop", str(max(1, int(r)) - 1), *clip_args(),
                               "-i", str(p))
                     for p, r in zip(music, music_reps)] if has_music else [])
        # Background beds, claimed like the gifs: -stream_loop N replays a clip
        # before decoding, which is what holds a short bed for the dwell floor.
        # Finite by construction — an unbounded -stream_loop -1 was measured
        # running away when no -t bounded the output.
        #
        # Claimed LAST, and trimmed to whatever budget the other layers left,
        # because this is the layer that must give way: three variable-length
        # runs (CTA clips, gifs, beds) share one 60-input ceiling, and at their
        # per-layer caps they add to 120. The bed is ambience — the CTA and the
        # gifs carry the message — so a short bed whose last frame holds beats
        # failing the row outright, which is what the raise below would do for
        # every row in the batch.
        if has_bgv:
            room = max(0, MAX_TOTAL_FFMPEG_INPUTS - n_inputs)
            if len(bgvs) > room:
                spec.warnings.append(
                    f"Background videos: only {room} of {len(bgvs)} clip(s) fit "
                    f"beside this row's other layers (the {MAX_TOTAL_FFMPEG_INPUTS}"
                    "-input ceiling), so the sequence is shorter and its last "
                    "frame holds. Raise 'Minimum seconds per background video' "
                    "to need fewer.")
                bgvs = bgvs[:room]
                bgv_reps = bgv_reps[:room]
                has_bgv = bool(bgvs)
        bgv_ix = ([add_input("-stream_loop", str(max(1, int(r)) - 1), *clip_args(),
                             "-i", str(p))
                   for p, r in zip(bgvs, bgv_reps)] if has_bgv else [])

        if n_inputs > MAX_TOTAL_FFMPEG_INPUTS:
            # Two independent per-layer caps are not enough — they land in one
            # command line together. Fail with something that names the cause,
            # because the alternative is [WinError 206] surfacing through
            # render_row's broad except as "The filename or extension is too long".
            raise RuntimeError(
                f"This row needs {n_inputs} FFmpeg inputs, over the "
                f"{MAX_TOTAL_FFMPEG_INPUTS} limit: {len(clips)} CTA clip(s), "
                f"{len(gifs)} gif(s), {len(sub_layers)} subliminal layer(s), "
                f"{len(music) if has_music else 0} music track(s), "
                f"{len(bgvs) if has_bgv else 0} background video(s). "
                "Raise the gif dwell time so fewer gifs are needed, raise the "
                "background-video or music dwell time, use fewer CTA clips, or "
                "shorten the promo video.")

        # ---- filter graph, written against the indices claimed above ---------
        # spec.video_* are the per-row resolved box (Excel Video_* overrides,
        # the configured values, or a randomized spot when randomize_video_pos
        # is enabled). The promo video defines the render length: [vidA] anchors
        # the otherwise-infinite looped background to the promo's duration, and
        # [vidB] is the promo's visible layer painted in z-order below.
        video_pos = (f"x='{spec.video_x}+({spec.video_w}-w)/2'"
                     f":y='{spec.video_y}+({spec.video_h}-h)/2'")
        parts = [
            f"[{promo_i}:v]scale={spec.video_w}:{spec.video_h}"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"split[vidA][vidB];",
            f"[{base_i}:v][vidA]overlay={video_pos}:shortest=1[anchored];",
        ]
        if has_ctav:
            n = len(clips)
            speeds = spec.cta_video_clip_speeds or [1.0] * n
            cw, ch = spec.cta_video_w, spec.cta_video_h
            # Cover-fill each chosen clip to the box so they share one size
            # (needed to concat). setpts=PTS/SPEED is applied per clip BEFORE the
            # concat so each slot plays at its own speed; concat then re-stamps
            # the joined timeline.
            labels = []
            for k, idx in enumerate(clip_ix):
                sp = speeds[k] if k < len(speeds) else 1.0
                parts.append(
                    f"[{idx}:v]fps={fps},"
                    f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                    f"crop={cw}:{ch},setsar=1,setpts=PTS/{sp:.4f},format=rgba[cv{k}];"
                )
                labels.append(f"[cv{k}]")
            if n > 1:
                parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=0[cseq];")
                seq = "[cseq]"
            else:
                seq = labels[0]
            # A zero-length fade means "visible from the first frame" (always the
            # case in split-screen, where the panel is layout, not an accent) —
            # emit a passthrough instead of a degenerate fade=d=0.
            if (spec.cta_video_fade_duration or 0) > 0:
                parts.append(
                    f"{seq}fade=t=in:st={spec.cta_video_fade_start}"
                    f":d={spec.cta_video_fade_duration}:alpha=1[ctav];"
                )
            else:
                parts.append(f"{seq}null[ctav];")
        if has_gif:
            gw, gh = spec.gif_w, spec.gif_h
            labels = []
            for k, idx in enumerate(gif_ix):
                parts.append(
                    f"[{idx}:v]fps={fps},format=rgba,"
                    f"scale={gw}:{gh}"
                    f":force_original_aspect_ratio=decrease:force_divisible_by=2,"
                    f"pad={gw}:{gh}:'trunc(({gw}-iw)/4)*2':'trunc(({gh}-ih)/4)*2'"
                    f":color=0x00000000,setsar=1[gv{k}];"
                )
                labels.append(f"[gv{k}]")
            if len(labels) > 1:
                parts.append(
                    f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[gseq];")
                gseq = "[gseq]"
            else:
                gseq = labels[0]
            if (spec.gif_fade_duration or 0) > 0:
                parts.append(
                    f"{gseq}fade=t=in:st={spec.gif_fade_start}"
                    f":d={spec.gif_fade_duration}:alpha=1[gifl];"
                )
            else:
                parts.append(f"{gseq}null[gifl];")
        if has_bgv:
            # Translucent background sequence. Each clip is crop-first
            # cover-filled to the box (crop to the box's aspect, then scale —
            # same visible region as scale-then-crop at 4-6x less CPU, which
            # matters at full canvas), which also gives every clip the identical
            # size concat demands.
            #
            # The per-clip format is yuv420p, NOT rgba, and that is a memory
            # decision rather than a cosmetic one. concat rejects inputs whose
            # pixel formats differ, so every branch has to agree on one — but
            # every branch also holds buffered frames of it, and this layer runs
            # at FULL CANVAS with as many branches open as the promo is long:
            # one per clip in the sequence, up to the input ceiling.
            #
            # rgba is 4 bytes a pixel against yuv420p's 1.5, and the difference
            # is not marginal. Measured on one row (76s promo, 1080x1920, the
            # bed at 846x1614), peak RSS with the per-clip rgba against this:
            #
            #     clips   rgba     yuv420p    the layer's own cost
            #        0   1.5 GB     -          (baseline, no bed)
            #        7   3.2 GB    2.9 GB      1.6 -> 1.4 GB
            #       14   3.7 GB    2.8 GB      2.2 -> 1.3 GB   (-41%)
            #       23   4.9 GB    3.6 GB      3.3 -> 2.0 GB   (-39%)
            #
            # It grows with the clip count either way — that is the design —
            # but the slope decides how many of these fit in the box at once,
            # and the renderer runs up to 16 in parallel. Over the top the
            # kernel kills them, which surfaces as "FFmpeg exited with code -9"
            # and an EMPTY stderr; the survivors swap and hit ffmpeg_timeout.
            # yuv444p is not the safe middle it looks like — 3.7GB at 14 clips,
            # i.e. three bytes a pixel is too close to four to be worth it.
            #
            # Nothing is lost by waiting: h264 hands these over as yuv420p
            # already, so this is a no-op per branch, and the format=rgba below
            # converts the joined timeline once instead of once per clip. The
            # one real difference is that the bed is now scaled with subsampled
            # chroma rather than in RGB — 48.9 dB PSNR against the old output,
            # on a layer composited at a few percent opacity.
            bw, bh = spec.bg_video_w, spec.bg_video_h
            labels = []
            for k, idx in enumerate(bgv_ix):
                parts.append(
                    f"[{idx}:v]fps={fps},"
                    f"crop='min(iw,ih*{bw}/{bh})':'min(ih,iw*{bh}/{bw})',"
                    f"scale={bw}:{bh},setsar=1,format=yuv420p[bv{k}];")
                labels.append(f"[bv{k}]")
            if len(labels) > 1:
                parts.append(
                    f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[bseq];")
                bseq = "[bseq]"
            else:
                bseq = labels[0]
            # One alpha multiply over the joined timeline rather than per clip:
            # cheaper, and it cannot drift between clips. format=rgba has to
            # precede it or the alpha lands on opaque yuv — see the branches
            # above for why it is here and not there.
            parts.append(
                f"{bseq}format=rgba,"
                f"colorchannelmixer=aa={cfg.bg_video_opacity:.3f}[bgv];")
        if has_cta:
            parts.append(
                f"[{cta_i}:v]format=rgba,"
                f"fade=t=in:st={spec.cta_fade_start}:d={spec.cta_fade_duration}:alpha=1[cta];"
            )
        # Stack the overlay layers by their sidebar z-index (higher = on top; the
        # background is always the base). Ties fall back to the fixed priority in
        # the second tuple field so the order stays deterministic. Each tuple:
        # (z-index, tie-break priority, overlay input, overlay position).
        layers = [
            (cfg.video_z, 0, "[vidB]", video_pos),
            (cfg.text_z, 4, f"[{text_i}:v]", "0:0"),
        ]
        if has_cta:
            layers.append((cfg.cta_image_z, 3, "[cta]", f"{spec.cta_x}:{spec.cta_y}"))
        if has_ctav:
            layers.append((cfg.cta_video_z, 2, "[ctav]",
                           f"{spec.cta_video_x}:{spec.cta_video_y}"))
        if has_gif:
            # No shortest=1 here: the gif sequence is finite, and terminating the
            # composite on it would cut the video short whenever the sequence
            # under-fills. The promo's own anchor plus -t bound the render.
            layers.append((cfg.gif_z, 1, "[gifl]",
                           f"{spec.gif_x}:{spec.gif_y}"))
        if has_bgv:
            # The translucent background sequence shares texts' z with a lower
            # tie-break, so it sits IMMEDIATELY below the texts (priority 4)
            # and subliminals (priority 5) whatever z values the sidebar holds,
            # and above every layer with z <= text_z. There is deliberately no
            # bg_video_z knob: "under the texts, over everything else" is the
            # layer's contract, not a preference.
            layers.append((cfg.text_z, 3.9, "[bgv]",
                           f"{spec.bg_video_x}:{spec.bg_video_y}"))
        # Subliminal text: one K-frame looping clip per text, overlaid at its
        # crop offset. The cycle lives in the clip's frame order, so per output
        # frame exactly one partial shows and no frame carries the whole text.
        # They sit at text_z (just above the static texts). eof_action=pass:
        # the clip is looped past the promo's length, but if the promo outruns
        # it anyway (unprobeable promo longer than the loop fallback) the
        # effect must VANISH, not freeze one readable partial on screen —
        # overlay's default repeatlast would burn in a fixed fragment of the
        # hidden text.
        for m, sl in enumerate(sub_layers):
            layers.append((cfg.text_z, 5, f"[{sub_ix[m]}:v]",
                           f"{sl['x']}:{sl['y']}:eof_action=pass"))
        layers.sort(key=lambda layer: (layer[0], layer[1]))

        # crop_to_panels (split only): the finished composite is cropped to
        # exactly the panel band — the output IS the two videos (1080 x panel
        # height), no background at all. The band is even-aligned by
        # _apply_split_layout so the yuv420p chroma stays clean.
        final = ""
        if cfg.layout_mode == "split" and cfg.crop_to_panels:
            final = f",crop={CANVAS_W}:{spec.video_h}:0:{spec.video_y}"
        final += ",format=yuv420p"

        last = "anchored"
        for i, layer in enumerate(layers):
            label, pos = layer[2], layer[3]
            top = i == len(layers) - 1
            out = "out" if top else f"z{i}"
            fmt = final if top else ""
            sep = "" if top else ";"  # the final [out] feeds -map, no trailing ;
            parts.append(f"[{last}]{label}overlay={pos}{fmt}[{out}]{sep}")
            last = out

        # ---- audio ----------------------------------------------------------
        # The DEFAULT path is untouched: with no music bed and no split, audio
        # is still `-map <promo>:a?` straight into AAC — optional, so a silent
        # promo maps nothing and renders fine. The graph is only entered when
        # one of the two features is actually on, so every sheet that renders
        # today produces the same command it did before.
        #
        # Once inside it, though, `?` is gone: a filter graph cannot take an
        # optional stream, which is why self._promo_has_audio exists.
        tempos = self._split_audio_tempos(spec)
        aparts: list[str] = []
        audio_out = ""              # non-empty => map this label instead
        if cfg.include_audio and (has_music or tempos):
            main_dur = self._probe_duration(self.video_path)
            legs: list[str] = []
            # Complementary split: music at v leaves the original at 1-v.
            vol = max(0.0, min(float(cfg.music_volume or 0.0), 1.0))
            if self._promo_has_audio:
                if tempos:
                    n = len(tempos)
                    step = main_dur / n
                    # asegment, not asplit+atrim: one decode feeding N cuts
                    # rather than N branches each discarding what it is not for,
                    # and about half the graph text per chunk — which matters,
                    # because this shares Windows' command-line ceiling with
                    # everything else here.
                    stamps = "|".join(f"{step * (i + 1):.4f}" for i in range(n - 1))
                    aparts.append(f"[{promo_i}:a]asegment=timestamps={stamps}"
                                 + "".join(f"[as{i}]" for i in range(n)) + ";")
                    for i, tempo in enumerate(tempos):
                        # asetpts rebases each cut to zero so concat can stitch
                        # them; atempo is a pitch-preserving stretch, so the
                        # pace moves and the voices do not go chipmunk.
                        aparts.append(f"[as{i}]asetpts=PTS-STARTPTS,"
                                     f"atempo={tempo:.5f}[aw{i}];")
                    # apad=whole_dur, never a bare apad. atempo rounds to its
                    # WSOLA frames, so the joined track lands a few tens of ms
                    # short and needs making up — but a bare apad pads FOREVER,
                    # and an infinite leg under amix's duration=first would run
                    # the render away on any path where -t went missing. The
                    # bounded form both fills the gap and terminates on its own
                    # (verified with -t removed entirely).
                    aparts.append("".join(f"[aw{i}]" for i in range(n))
                                 + f"concat=n={n}:v=0:a=1,"
                                 f"apad=whole_dur={main_dur:.3f}[apromo];")
                else:
                    aparts.append(f"[{promo_i}:a]anull[apromo];")
                if has_music:
                    aparts.append(f"[apromo]volume={1.0 - vol:.4f}[apromov];")
                    legs.append("[apromov]")
                else:
                    legs.append("[apromo]")
            if has_music:
                # With no original audio to balance against, the bed IS the mix:
                # honouring "10%" literally would produce a near-silent video
                # that reads as a bug rather than a setting.
                bed_vol = vol if self._promo_has_audio else 1.0
                if not self._promo_has_audio:
                    spec.warnings.append(
                        "Music: this promo video has no audio track, so the "
                        f"music plays at full volume rather than {vol:.0%} — "
                        "there is nothing to mix it against.")
                for k, idx in enumerate(music_ix):
                    # aformat before concat, always: concat refuses inputs whose
                    # rate or layout disagree, and a pool of user-supplied
                    # tracks disagreeing is the normal case (44.1kHz stereo MP3
                    # beside a 48kHz mono WAV), not the edge one.
                    aparts.append(f"[{idx}:a]{MUSIC_FORMAT},asetpts=PTS-STARTPTS[mu{k}];")
                if len(music_ix) > 1:
                    aparts.append("".join(f"[mu{k}]" for k in range(len(music_ix)))
                                 + f"concat=n={len(music_ix)}:v=0:a=1[museq];")
                    museq = "[museq]"
                else:
                    museq = "[mu0]"
                aparts.append(f"{museq}volume={bed_vol:.4f}[abed];")
                legs.append("[abed]")
            if len(legs) == 2:
                # normalize=0 or the weights stop meaning what they say — amix
                # otherwise rescales by the number of live inputs. duration=first
                # ends the mix with the promo leg, which apad has already pinned
                # to the video's exact length.
                aparts.append("".join(legs) + "amix=inputs=2:duration=first"
                                             ":dropout_transition=0:normalize=0[aout]")
                audio_out = "[aout]"
            elif legs:
                # A lone music leg still has to reach the end of the video: with
                # nothing else mapped, -shortest would otherwise cut the picture
                # at the point the bed ran out. (The promo leg is already pinned
                # by its own apad above.) Bounded, so it cannot run away.
                if legs[0] == "[abed]" and main_dur:
                    aparts.append(f"[abed]apad=whole_dur={main_dur:.3f}[aout]")
                    audio_out = "[aout]"
                else:
                    audio_out = legs[0]

        # The last VIDEO chain deliberately carries no trailing ';' (it feeds
        # -map), and the audio chains must not leave one either — FFmpeg reads a
        # dangling separator as an empty filterchain and refuses the graph.
        filter_complex = "".join(parts)
        if aparts:
            filter_complex += ";" + "".join(aparts).rstrip(";")
        # The invariant that actually catches an index slip. Checking that the
        # highest referenced index equals n_inputs-1 does NOT: an injected
        # off-by-one in either direction passes it and still renders a different
        # video with exit 0.
        self._check_filter_inputs(filter_complex, n_inputs, audio_only=music_ix)

        cmd += ["-filter_complex", filter_complex, "-map", "[out]"]
        if not cfg.include_audio:
            cmd += ["-an"]
        elif audio_out:
            cmd += ["-map", audio_out, "-c:a", "aac", "-b:a", cfg.audio_bitrate]
        else:
            # Take audio from the promo video if it exists; never fail without
            # it. The CTA video's and the gifs' audio is intentionally ignored.
            cmd += ["-map", f"{promo_i}:a?", "-c:a", "aac", "-b:a", cfg.audio_bitrate]
        cmd += [
            "-c:v", "libx264",
            "-preset", cfg.preset,
            "-crf", str(cfg.crf),
            "-r", str(fps),
        ]
        if cfg.ffmpeg_threads:
            # x264 picks min(1.5 * ncores, 128) frame threads from the MACHINE's
            # core count, so every parallel row asks for the whole box. Sixteen
            # rows on a 112-core VM is ~2,000 encoder threads; 112 rows is
            # ~14,000, and each frame thread carries its own frame buffers — the
            # memory and the context switching are both charged per thread, not
            # per row. Capping it is what makes workers * threads track the
            # cores actually available. 0 keeps x264's own auto-detect, which is
            # the right answer for a single preview render.
            cmd += ["-threads", str(cfg.ffmpeg_threads)]
        # Bound the output to the main video's exact length. The looped base /
        # overlay / subliminal stills are infinite streams; the muxer -shortest
        # only trims them when the promo has an audio track to anchor against
        # (-map 1:a?). A silent or audio-disabled promo would otherwise run away,
        # so probe the duration and cap it explicitly. This also makes the
        # "whole video ends when the main video ends" contract exact.
        main_dur = self._probe_duration(self.video_path)
        if main_dur:
            cmd += ["-t", f"{main_dur:.3f}"]
        if sub_layers and cfg.subliminal_all_intra:
            # Independently code every frame (keyint=1) so the per-frame
            # "incomplete text" property survives a frame-by-frame scrub of THIS
            # file — inter-frame prediction would otherwise smear neighbouring
            # partials into a decodable whole. Substantially larger files.
            cmd += ["-x264-params", "keyint=1:scenecut=0"]
        cmd += [
            "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ]
        return cmd

    # ------------------------------------------------------------- per-row render

    def render_row(self, row_number: int, row: pd.Series,
                   filename: Optional[str] = None) -> RowResult:
        """Render one Excel row to an MP4. Never raises — failures are captured
        in the returned RowResult so one bad row can't abort the batch.

        `filename` lets the caller name the output. Naming policy (captions,
        hashtags, length caps) lives with the caller, not in the render engine.
        Omit it for the historical Caption-or-Headline name."""
        spec = RowSpec.from_row(row, row_number)
        # Repeated renders of one sheet must differ; see RenderConfig.variant_salt.
        # Guarded so the default (0) leaves every existing seed untouched.
        if self.config.variant_salt:
            spec.seed_salt = int(row_number) * 1_000_003 + int(self.config.variant_salt)
        base_png = self.work_dir / f"row_{row_number:04d}_base.png"
        overlay_png = self.work_dir / f"row_{row_number:04d}_overlay.png"
        # The CTA image is optional — no PNG (and no FFmpeg input) without one.
        cta_png = self.work_dir / f"row_{row_number:04d}_cta.png" if self._has_cta else None
        # Name from the row's Caption when there is one, else the Headline.
        filename = filename or safe_filename(
            row_number, _clean_str(row.get("Caption")) or spec.headline.text)
        out_path = self.output_dir / filename
        sub_files: list[Path] = []
        try:
            self.build_base_image(spec).save(base_png)
            # resolves positions; the CTA ships as its own input so FFmpeg
            # can fade it in (see build_ffmpeg_command)
            self.build_overlay_image(spec, include_cta=False).save(overlay_png)
            if cta_png is not None:
                self._get_cta(spec.cta_w, spec.cta_h).save(cta_png)

            # Subliminal texts: crop each element's K partials to the union of
            # their painted pixels (measured with getbbox, so shadows and
            # outlines are covered without geometry bookkeeping) and write them
            # as ONE raw rgba clip whose frame order bakes in the cycle and the
            # phase. FFmpeg loops that tiny clip instead of decoding K
            # full-canvas PNGs per output frame — see add_sub in
            # build_ffmpeg_command. Offsets and sizes are even-aligned so the
            # chroma of the composite below stays stable.
            sub_layers = []
            phase = int(self.config.subliminal_phase)
            for e, item in enumerate(self.build_subliminal_layers(spec)):
                k = item["k"]
                images = item["images"]
                boxes = [box for box in (im.getbbox() for im in images) if box]
                if not boxes:
                    continue   # nothing painted — skip the layer entirely
                x0 = min(b[0] for b in boxes) & ~1
                y0 = min(b[1] for b in boxes) & ~1
                w = min(CANVAS_W - x0, (max(b[2] for b in boxes) - x0 + 1) & ~1)
                h = min(CANVAS_H - y0, (max(b[3] for b in boxes) - y0 + 1) & ~1)
                raw = self.work_dir / f"row_{row_number:04d}_sub{e}.raw"
                with open(raw, "wb") as fh:
                    for j in range(k):
                        fh.write(images[(j + phase) % k]
                                 .crop((x0, y0, x0 + w, y0 + h)).tobytes())
                sub_files.append(raw)
                sub_layers.append(
                    {"raw": raw, "w": w, "h": h, "x": x0, "y": y0, "k": k})

            cmd = self.build_ffmpeg_command(spec, base_png, overlay_png, cta_png,
                                            out_path, sub_layers)
            proc = subprocess.run(
                cmd, **_FF_CAPTURE, timeout=self.config.ffmpeg_timeout
            )
            if proc.returncode != 0:
                tail = "\n".join(proc.stderr.strip().splitlines()[-30:])
                raise RuntimeError(f"FFmpeg exited with code {proc.returncode}:\n{tail}")
            if not out_path.is_file() or out_path.stat().st_size == 0:
                raise RuntimeError("FFmpeg produced no output file")

            logger.info("Row %d OK -> %s", row_number, filename)
            return RowResult(row_number, True, filename, out_path, warnings=spec.warnings)
        except subprocess.TimeoutExpired:
            error = f"Render timed out after {self.config.ffmpeg_timeout}s"
            logger.error("Row %d FAILED: %s", row_number, error)
            # A killed FFmpeg leaves a truncated MP4 behind. The item is marked
            # failed, so the packer excludes it from its folder and free_files
            # — which walks item metadata, not the directory — can never see
            # it. On the path where the videos are kept, that orphan is kept
            # too. Matches the generic handler just below.
            out_path.unlink(missing_ok=True)
            return RowResult(row_number, False, error=error, warnings=spec.warnings)
        except Exception as exc:  # noqa: BLE001 — per-row isolation is the point
            logger.error("Row %d FAILED: %s", row_number, exc)
            out_path.unlink(missing_ok=True)
            return RowResult(row_number, False, error=str(exc), warnings=spec.warnings)
        finally:
            base_png.unlink(missing_ok=True)
            overlay_png.unlink(missing_ok=True)
            if cta_png is not None:
                cta_png.unlink(missing_ok=True)
            for f in sub_files:
                f.unlink(missing_ok=True)

    # ------------------------------------------------------------- duration probe

    _DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

    def _probe_duration(self, path: Optional[Path] = None) -> Optional[float]:
        """Measure a video's duration in seconds, cached per path.

        Uses FFmpeg itself (NOT ffprobe — imageio-ffmpeg bundles only ffmpeg):
        `ffmpeg -i <path>` with no output exits non-zero by design and prints
        the stream info, including a `Duration: HH:MM:SS.ss` line, to stderr.
        We parse that line and ignore the non-zero return. Returns None if the
        duration can't be determined (callers degrade gracefully)."""
        path = Path(path) if path else self.video_path
        key = str(path)
        with self._duration_lock:
            if key in self._durations:
                return self._durations[key]
            dur: Optional[float] = None
            try:
                proc = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-i", str(path)],
                    **_FF_CAPTURE, timeout=120,
                )
                # ffmpeg prints info to stderr; exit code is non-zero (no output
                # file) but that's expected here — parse regardless.
                match = self._DURATION_RE.search(proc.stderr or "")
                if match:
                    h, m, s = match.groups()
                    dur = int(h) * 3600 + int(m) * 60 + float(s)
            except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
                logger.warning("Could not probe duration for %s: %s", path, exc)
            self._durations[key] = dur
            return dur

    # ------------------------------------------------------------- preview

    def _first_video_frame(self, path: Optional[Path] = None) -> Image.Image:
        """Extract (and cache, per path) a video's first frame for previews.
        Defaults to the promo video."""
        path = Path(path) if path else self.video_path
        key = str(path)
        with self._preview_frame_lock:
            frame = self._video_frames.get(key)
            if frame is None:
                frame_png = self.work_dir / f"preview_frame_{abs(hash(key)):x}.png"
                cmd = [
                    self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(path),
                    "-frames:v", "1", "-update", "1", str(frame_png),
                ]
                proc = subprocess.run(cmd, **_FF_CAPTURE, timeout=120)
                if proc.returncode != 0 or not frame_png.is_file():
                    raise RuntimeError(f"Could not extract preview frame:\n{proc.stderr[-2000:]}")
                with Image.open(frame_png) as img:
                    frame = img.convert("RGB").copy()
                frame_png.unlink(missing_ok=True)
                self._video_frames[key] = frame
            return frame

    def _fit_frame(self, box_w: int, box_h: int, path: Optional[Path] = None) -> Image.Image:
        """Scale a video's first frame into a box exactly like FFmpeg's
        scale=W:H:force_original_aspect_ratio=decrease — including upscaling
        small sources (Image.thumbnail only ever shrinks). Defaults to the
        promo video; pass `path` for the CTA video."""
        frame = self._first_video_frame(path)
        ratio = min(box_w / frame.width, box_h / frame.height)
        size = (max(2, round(frame.width * ratio)), max(2, round(frame.height * ratio)))
        return frame.resize(size, Image.LANCZOS)

    def _cover_frame(self, box_w: int, box_h: int, path: Path) -> Image.Image:
        """Cover-fill a video's first frame to exactly box_w x box_h (center-crop)
        — matches how the CTA clips are scaled+cropped in the render so they line
        up edge to edge in the box."""
        return ImageOps.fit(self._first_video_frame(path), (box_w, box_h), Image.LANCZOS)

    def _contain_content(self, box_w: int, box_h: int, path: Path,
                         allow_upscale: bool = True) -> Image.Image:
        """A gif's first frame scaled to fill the box as far as its aspect ratio
        allows without escaping it — the visible content only, no padding. Small
        gifs are enlarged, big ones shrunk; nothing is cropped or distorted.

        This is the Pillow counterpart of the filter's
        scale=W:H:force_original_aspect_ratio=decrease. _cover_frame can't stand
        in (it crops), and _fit_frame reads the promo video by default, so the
        gif layer keeps its own helper.

        allow_upscale=False caps the result at the source's natural size. That
        is NOT a fit rule — it exists only for the editor payload, where the
        browser does the fitting in CSS and enlarging the raster before base64
        would inflate the payload without adding a pixel of detail."""
        frame = self._first_video_frame(path)
        ratio = min(box_w / frame.width, box_h / frame.height)
        if not allow_upscale:
            ratio = min(ratio, 1.0)
        size = (max(2, round(frame.width * ratio)), max(2, round(frame.height * ratio)))
        return frame.resize(size, Image.LANCZOS)

    def _contain_frame(self, box_w: int, box_h: int, path: Path) -> Image.Image:
        """_contain_content centred on a fully transparent box-sized tile — the
        counterpart of the filter's `pad ... color=0x00000000`, so the static
        preview shows the same see-through margins the render produces."""
        content = self._contain_content(box_w, box_h, path)
        tile = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
        tile.paste(content.convert("RGBA"),
                   ((box_w - content.width) // 2, (box_h - content.height) // 2))
        return tile

    def render_preview(self, row: pd.Series, row_number: Optional[int] = None) -> Image.Image:
        """Static composite of one row — same layout math as the real render,
        with the video represented by its first frame. Pass the row's 1-based
        sheet number so the preview's random picks match render_row's."""
        spec = RowSpec.from_row(row, row_number)
        base = self.build_base_image(spec).convert("RGBA")
        overlay = self.build_overlay_image(spec)  # resolves positions first

        frame = self._fit_frame(spec.video_w, spec.video_h)
        fx = spec.video_x + (spec.video_w - frame.width) // 2
        fy = spec.video_y + (spec.video_h - frame.height) // 2
        base.paste(frame, (fx, fy))

        return Image.alpha_composite(base, overlay).convert("RGB")

    def build_editor_payload(self, row: pd.Series, row_number: Optional[int] = None) -> dict:
        """Everything the interactive preview editor needs, with each layer
        shipped separately so the browser can move/recolor elements without a
        server round-trip: the background, the promo video's first frame scaled
        into its box, the optional CTA video's first frame, the optional CTA
        image, and per text — a white alpha-mask PNG of the fill glyphs (recolored
        client-side via CSS mask-image) plus a baked 'decoration' PNG carrying
        the artistic style (outline/shadow/neon) behind it. The text's
        background highlight box is described geometrically (bg_w/bg_h/bg_radius)
        so the editor can draw and recolor it live in CSS.

        Texts carry their CENTER point (the Excel convention for *_X/*_Y);
        the video and CTA boxes carry their top-left corner. Pass the row's
        1-based sheet number so the preview's random picks (CTA clips, colors,
        …) match what render_row produces for that row."""
        spec = RowSpec.from_row(row, row_number)
        self._resolve_positions(spec)

        frame = self._fit_frame(spec.video_w, spec.video_h)

        texts = []
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        for element in spec.text_elements:
            if not element.text:
                continue
            font = self._font_for(element)
            stroke, _sh_off, _sh_blur, _glow, _bg_pad, pad = self._style_metrics(element)
            l, t, r, b = measure.multiline_textbbox(
                (0, 0), element.text, font=font, anchor="mm", align="center",
                stroke_width=stroke,
            )
            iw, ih = int(round(r - l)) + 2 * pad, int(round(b - t)) + 2 * pad
            ax, ay = iw / 2 - (l + r) / 2, ih / 2 - (t + b) / 2

            ink = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            self._paint_text(ink, ax, ay, element, "ink", fill=(255, 255, 255, 255))
            # Bake the decoration at FULL alpha and let the editor apply the
            # element's translucency in CSS — otherwise dragging the opacity
            # slider would fade the glyphs but not their outline/glow/shadow.
            deco = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            self._paint_text(deco, ax, ay,
                             replace(element, color=(*element.color[:3], 255)),
                             "decoration")

            # Background highlight box geometry (canvas px), matching _paint_text's
            # 'full' box, so the editor draws/recolors it live in CSS.
            bgp = max(6, round(element.size * 0.30))
            bg_w, bg_h = int(round(r - l)) + 2 * bgp, int(round(b - t)) + 2 * bgp

            texts.append({
                "role": element.role,
                "cx": element.x, "cy": element.y, "w": iw, "h": ih,
                "size": element.size,
                # The fit box, when this text has one (0 = unboxed). The editor
                # resizes THIS instead of scaling the font, and draws it as an
                # outline so the allotted space is visible even when the fitted
                # text is much smaller than it.
                "box_w": int(element.box_w or 0), "box_h": int(element.box_h or 0),
                # Color and opacity travel separately: the editor's <input
                # type=color> only speaks 6-digit hex, and they map to two
                # different Excel columns (*_Color and *_Opacity).
                "color": "#%02X%02X%02X" % element.color[:3],
                "opacity": round(_alpha_of(element.color) / 255 * 100),
                "font": element.font,
                "style": element.style,
                "bg": ("#%02X%02X%02X" % element.bg_color[:3]) if element.bg_color else None,
                "bg_opacity": round(_alpha_of(element.bg_color) / 255 * 100),
                "bg_w": bg_w, "bg_h": bg_h, "bg_radius": round(bg_h * 0.30),
                "mask": _img_to_data_uri(ink),
                "deco": _img_to_data_uri(deco),
                # The static editor can't show the motion-only subliminal effect;
                # it shows the full text and flags it (the render splits it across
                # sub_k frames). See build_subliminal_layers.
                "subliminal": bool(element.subliminal),
                "subliminal_k": element.sub_k,
            })

        payload = {
            "canvas_w": CANVAS_W,
            "canvas_h": CANVAS_H,
            "layout": self.config.layout_mode,
            # crop_to_panels: the output is only the panel band — the editor dims
            # everything outside it so cropped-away areas are obvious.
            "crop": ({"y": spec.video_y, "h": spec.video_h}
                     if (self.config.layout_mode == "split"
                         and self.config.crop_to_panels) else None),
            "bg": _img_to_data_uri(self.build_base_image(spec), "JPEG"),
            "video": {
                "x": spec.video_x, "y": spec.video_y,
                "w": spec.video_w, "h": spec.video_h,
                "frame": _img_to_data_uri(frame.convert("RGB"), "JPEG"),
                "frame_w": frame.width, "frame_h": frame.height,
            },
            "texts": texts,
            # Sidebar layer order — the editor applies these as CSS z-index so the
            # preview stacking matches the render (higher = on top).
            "z": {
                "video": self.config.video_z, "gif": self.config.gif_z,
                "cta_video": self.config.cta_video_z,
                "cta_image": self.config.cta_image_z, "text": self.config.text_z,
                # No bg_video entry: that layer has no z knob — the editor uses
                # Z.text directly, with DOM order breaking the tie the same way
                # the render's 3.9 tie-break does (directly beneath the texts).
            },
        }
        if self._has_bg_videos and spec.bg_video_clips:
            # Cover-fit poster frame of the FIRST clip in this row's sequence,
            # shown at the render opacity via CSS so the translucency is honest.
            # Like the gif box, a caption admits what a still cannot show: the
            # sequence rotates through several beds during the video.
            bv = self._cover_frame(spec.bg_video_w, spec.bg_video_h,
                                   spec.bg_video_clips[0])
            payload["bg_video"] = {
                "x": spec.bg_video_x, "y": spec.bg_video_y,
                "w": spec.bg_video_w, "h": spec.bg_video_h,
                "frame": _img_to_data_uri(bv.convert("RGB"), "JPEG"),
                "frame_w": bv.width, "frame_h": bv.height,
                "opacity": self.config.bg_video_opacity,
                "count": len(spec.bg_video_clips),
            }
        if self._has_cta:
            payload["cta"] = {
                "x": spec.cta_x, "y": spec.cta_y, "w": spec.cta_w, "h": spec.cta_h,
                "img": _img_to_data_uri(self._get_cta(spec.cta_w, spec.cta_h)),
            }
        if self._has_cta_video:
            cv = self._cover_frame(spec.cta_video_w, spec.cta_video_h, self._lead_cta_path(spec))
            payload["cta_video"] = {
                "x": spec.cta_video_x, "y": spec.cta_video_y,
                "w": spec.cta_video_w, "h": spec.cta_video_h,
                "frame": _img_to_data_uri(cv.convert("RGB"), "JPEG"),
                "frame_w": cv.width, "frame_h": cv.height,
            }
        if self._has_gifs and spec.gif_clips:
            lead = self._lead_gif_path(spec)
            natural = self._first_video_frame(lead)
            # Ship the content WITHOUT the transparent padding, plus the gif's
            # true natural size. The editor's box is resizable, so it has to
            # re-derive the contain-fit itself on every drag — growing the box
            # grows the gif, exactly as the render does. Baking a box-sized
            # padded tile instead would stretch on resize and quietly lie.
            # allow_upscale=False keeps the shipped raster at natural size at
            # most: the CSS does the enlarging, so extra pixels here would only
            # be base64 weight (see _contain_content).
            shown = self._contain_content(spec.gif_w, spec.gif_h, lead,
                                          allow_upscale=False)
            payload["gif"] = {
                "x": spec.gif_x, "y": spec.gif_y,
                "w": spec.gif_w, "h": spec.gif_h,
                "frame": _img_to_data_uri(shown.convert("RGB"), "JPEG"),
                "nat_w": natural.width, "nat_h": natural.height,
                # What a still cannot show: the sequence rotates. The editor
                # captions the box with this so the preview is honest about
                # being one gif of several.
                "count": len(spec.gif_clips),
            }
        return payload
