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
import random
import re
import shutil
import subprocess
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

logger = logging.getLogger("video_generator")

# --------------------------------------------------------------------------- constants

CANVAS_W = 1080   # vertical 9:16 canvas for Reels / TikTok / Shorts
CANVAS_H = 1920
FPS = 30

# The CTA is invisible until CTA_FADE_START seconds, then fades in (alpha
# only) and is fully visible at CTA_FADE_START + CTA_FADE_DURATION. These are
# only defaults now — RenderConfig.cta_fade_* / cta_video_fade_* (set from the
# sidebar, overridable per row) carry the live values.
CTA_FADE_START = 1.0
CTA_FADE_DURATION = 0.5

# The CTA video is a sequence of fixed positions ("clips") that always play in
# order 1..N. Each position is backed by a pool of sample videos; one sample is
# chosen per output video (pinned by a CTA_Clip_<i> cell, else picked at random).
CTA_VIDEO_SLOTS = 4
CTA_CLIP_COLUMNS = [f"CTA_Clip_{i}" for i in range(1, CTA_VIDEO_SLOTS + 1)]
# Per-clip playback-speed overrides, one column per slot. Blank falls back to
# the row-wide CTA_Video_Speed cell, then to the sidebar's per-clip default.
CTA_SPEED_COLUMNS = [f"CTA_Video_Speed_{i}" for i in range(1, CTA_VIDEO_SLOTS + 1)]

# Every column is optional: absent/blank BG_Image cells get an image randomly
# assigned from the uploaded ZIP (no repeats until the pool is exhausted),
# absent Video_*/CTA_* cells fall back to the sidebar's boxes (or a randomized
# video position), absent texts are skipped, absent sizes/colors get defaults,
# and absent X/Y coordinates trigger auto-placement. The row count alone
# drives the batch.
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
    "Headline", "Headline_Size", "Headline_Color", "Headline_X", "Headline_Y",
    "Headline_Font", "Headline_BgColor", "Headline_Style",
    "Subheading", "Subheading_Size", "Subheading_Color", "Subheading_X", "Subheading_Y",
    "Subheading_Font", "Subheading_BgColor", "Subheading_Style",
    "Footer", "Footer_Size", "Footer_Color", "Footer_X", "Footer_Y",
    "Footer_Font", "Footer_BgColor", "Footer_Style",
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


def safe_filename(row_number: int, headline: str) -> str:
    """Build '001_Some_Headline.mp4' style names; safe on every filesystem."""
    name = re.sub(r"[^\w\- ]", "", str(headline or ""), flags=re.UNICODE).strip()
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


# --------------------------------------------------------------------------- dataclasses

@dataclass
class RenderConfig:
    """All knobs the UI exposes. Coordinates are in 1080x1920 canvas pixels.
    The video and CTA box values are per-batch defaults — a row's Video_* /
    CTA_* Excel cells override them for that row."""
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
    # whole row. Defaults to normal speed for every slot.
    cta_video_speeds: list[float] = field(
        default_factory=lambda: [1.0] * CTA_VIDEO_SLOTS)
    # Layer order (z-index) for the four overlay layers; higher = nearer the top,
    # the background is always the base. Equal values fall back to a fixed tie
    # priority (promo < CTA video < CTA image < texts). Applies to every video.
    video_z: int = 1
    cta_video_z: int = 2
    cta_image_z: int = 3
    text_z: int = 4
    crf: int = 18                 # 16-28; lower = higher quality / bigger files
    preset: str = "medium"        # x264 speed/size tradeoff
    # When True, video_x/video_y are ignored and each row's video box gets a
    # seeded-random position avoiding the CTA and explicitly-placed texts.
    randomize_video_pos: bool = False
    audio_bitrate: str = "192k"
    include_audio: bool = True
    font_path: Optional[str] = None   # the user's uploaded TTF/OTF, if any
    # Default font + artistic style for texts whose *_Font / *_Style cells are
    # blank. default_font is a FONT_LIBRARY key, FONT_SYSTEM, or FONT_CUSTOM.
    default_font: str = FONT_SYSTEM
    default_style: str = "classic"
    ffmpeg_timeout: int = 600     # seconds per row before a render is killed


@dataclass
class TextSpec:
    text: str
    role: str                # 'Headline' / 'Subheading' / 'Footer'
    size: Optional[int]      # None = randomize within the role's size range
    color: Optional[tuple]   # None = random palette color
    x: Optional[int]         # None = auto-place (X/Y cell left blank in the Excel)
    y: Optional[int]
    font: Optional[str] = None       # font choice name; None = config.default_font
    bg_color: Optional[tuple] = None # highlight box behind the text; None = no box
    style: Optional[str] = None      # classic|outline|shadow|neon; None = default_style


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
    resolved: bool = False

    @classmethod
    def from_row(cls, row: pd.Series) -> "RowSpec":
        warnings: list[str] = []

        def text_spec(prefix: str) -> TextSpec:
            return TextSpec(
                text=_clean_str(row.get(prefix)),
                role=prefix,
                size=_parse_opt_int(row.get(f"{prefix}_Size"), warnings, f"{prefix}_Size"),
                color=_parse_color(row.get(f"{prefix}_Color"), warnings, f"{prefix}_Color"),
                x=_parse_opt_int(row.get(f"{prefix}_X"), warnings, f"{prefix}_X"),
                y=_parse_opt_int(row.get(f"{prefix}_Y"), warnings, f"{prefix}_Y"),
                font=_clean_str(row.get(f"{prefix}_Font")) or None,
                bg_color=_parse_color(row.get(f"{prefix}_BgColor"), warnings,
                                      f"{prefix}_BgColor", fallback_desc="no background"),
                style=(_clean_str(row.get(f"{prefix}_Style")).lower() or None),
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
        return zlib.crc32(key.encode("utf-8"))


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
        cta_path: Path,
        work_dir: Path,
        output_dir: Path,
        cta_video_slots: Optional[list] = None,
    ):
        self.config = config
        self.video_path = Path(video_path)
        # Optional CTA video: a list of slots (positions 1..N that play in fixed
        # order); each slot is a pool of sample clips, one of which is chosen per
        # output video. Empty/all-empty => the CTA video element is skipped.
        self.cta_video_slots = [[Path(p) for p in (slot or [])]
                                for slot in (cta_video_slots or [])]
        self._has_cta_video = any(self.cta_video_slots)
        self.work_dir = Path(work_dir)
        self.output_dir = Path(output_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = find_ffmpeg()
        logger.info("Using FFmpeg binary: %s", self.ffmpeg)

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

        # One CTA image is shared by every row, but rows may override its box
        # via CTA_Width/CTA_Height — cache one resized copy per distinct size.
        self._cta_src = Image.open(cta_path).convert("RGBA")
        self._cta_cache: dict[tuple[int, int], Image.Image] = {}
        self._cta_lock = threading.Lock()

        # First frame of each video, by path (promo + optional CTA video), for
        # the static preview / editor stand-in.
        self._preview_frame_lock = threading.Lock()
        self._video_frames: dict[str, Image.Image] = {}

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
            raise ValueError("The backgrounds ZIP contains no images")

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
    def _contrast(color: tuple) -> tuple:
        """Black on light text, white on dark text — used for outline strokes."""
        r, g, b = color[:3]
        return (0, 0, 0, 255) if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else (255, 255, 255, 255)

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
        """Background layer: cover-crop (fill + center-crop) to exactly 1080x1920."""
        bg_path = self.resolve_bg(spec.bg_image)
        with Image.open(bg_path) as img:
            return ImageOps.fit(img.convert("RGB"), (CANVAS_W, CANVAS_H), Image.LANCZOS)

    def _wrap_text(self, element: TextSpec) -> None:
        """Re-flow an element's text so its rendered block fits on the canvas.

        - Footer: always balanced onto 3 lines (fewer if it has fewer words).
        - Headline/Subheading: kept on one line when it fits; greedily wrapped
          onto more lines when it would run past the canvas edges.
        - Safety valve: if a single word is wider than the canvas even alone,
          the font size is stepped down until it fits.
        Runs before measurement/placement so the auto-placer reserves space
        for the full wrapped block."""
        words = element.text.split()
        if not words:
            return
        max_w = CANVAS_W - 2 * PLACEMENT_MARGIN

        font = self._font_for(element)
        while element.size > 18 and max(font.getlength(w) for w in words) > max_w:
            element.size -= 2
            font = self._font_for(element)

        if element.role == "Footer":
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
                   occupied: list[tuple], rng: random.Random) -> tuple[int, int, bool]:
        """Pick a CENTER point for a w x h box: rejection-sample random positions
        until one clears every occupied rect (with PLACEMENT_GAP breathing room).
        If the canvas is too crowded, settle for the sampled spot with the least
        total overlap. A provided coordinate pins that axis and only the missing
        one is randomized."""
        def axis_range(fixed: Optional[int], size: float, limit: int) -> tuple[float, float]:
            if fixed is not None:
                return fixed, fixed
            lo = PLACEMENT_MARGIN + size / 2
            hi = limit - PLACEMENT_MARGIN - size / 2
            return (limit / 2, limit / 2) if lo > hi else (lo, hi)  # oversized: center it

        x_lo, x_hi = axis_range(fixed_x, w, CANVAS_W)
        y_lo, y_hi = axis_range(fixed_y, h, CANVAS_H)

        best, best_overlap = (CANVAS_W / 2, CANVAS_H / 2), float("inf")
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
            spec.cta_video_clips = chosen
            spec.cta_video_clip_speeds = chosen_speeds

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
            if element.size is None:
                lo, hi = RANDOM_SIZE_RANGES[element.role]
                element.size = rng.randint(lo, hi)
            if element.color is None:
                pick = color_deck.pop(rng.randrange(len(color_deck)))
                element.color = (*ImageColor.getrgb(pick), 255)
            # Wrap once size is final: footer to 3 lines, others to fit canvas.
            self._wrap_text(element)

        explicit_rects: list[tuple] = []
        pending: list[tuple[TextSpec, float, float]] = []
        for element in spec.text_elements:
            if not element.text:
                continue
            w, h = self._measure_text(element)
            if element.x is not None and element.y is not None:
                explicit_rects.append(_rect_from_center(element.x, element.y, w, h))
            else:
                pending.append((element, w, h))

        # A Video_X/Video_Y cell pins its axis (even when randomize is on);
        # _find_spot only randomizes the missing one.
        if cfg.randomize_video_pos and (spec.video_x is None or spec.video_y is None):
            fixed_cx = None if spec.video_x is None else int(spec.video_x + spec.video_w / 2)
            fixed_cy = None if spec.video_y is None else int(spec.video_y + spec.video_h / 2)
            cx, cy, clean = self._find_spot(
                fixed_cx, fixed_cy, spec.video_w, spec.video_h,
                [cta_rect] + explicit_rects, rng,
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
        occupied = [video_rect, cta_rect] + explicit_rects
        if self._has_cta_video:
            occupied.append(cta_video_rect)
        for element, w, h in pending:
            x, y, clean = self._find_spot(element.x, element.y, w, h, occupied, rng)
            element.x, element.y = x, y
            if not clean:
                spec.warnings.append(
                    f"'{element.text[:40]}': no overlap-free spot found, "
                    f"placed at least-crowded position ({x}, {y})"
                )
            occupied.append(_rect_from_center(x, y, w, h))

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

        Shadow/glow use temporary layers the same size as `target`, so this
        works equally on a full canvas (overlay) or a small tile (editor)."""
        font = self._font_for(element)
        style = element.style or "classic"
        stroke, sh_off, sh_blur, glow, _bg_pad, _pad = self._style_metrics(element)
        draw = ImageDraw.Draw(target)
        common = dict(font=font, anchor="mm", align="center")

        if what == "full" and element.bg_color:
            l, t, r, b = draw.multiline_textbbox((ax, ay), element.text,
                                                 stroke_width=stroke, **common)
            pad = max(6, round(element.size * 0.30))
            radius = round((b - t + 2 * pad) * 0.30)
            draw.rounded_rectangle((l - pad, t - pad, r + pad, b + pad),
                                   radius=radius, fill=element.bg_color)

        if what in ("full", "decoration"):
            if sh_off:
                sh = Image.new("RGBA", target.size, (0, 0, 0, 0))
                ImageDraw.Draw(sh).multiline_text((ax + sh_off, ay + sh_off), element.text,
                                                  fill=(0, 0, 0, 170), **common)
                if sh_blur:
                    sh = sh.filter(ImageFilter.GaussianBlur(sh_blur))
                target.alpha_composite(sh)
            if glow:
                gl = Image.new("RGBA", target.size, (0, 0, 0, 0))
                ImageDraw.Draw(gl).multiline_text((ax, ay), element.text,
                                                  fill=(*element.color[:3], 255), **common)
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
            if what == "full" and stroke:
                draw.multiline_text((ax, ay), element.text, fill=fillc,
                                    stroke_width=stroke, stroke_fill=self._contrast(element.color),
                                    **common)
            else:
                draw.multiline_text((ax, ay), element.text, fill=fillc, **common)

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
            self._paint_text(layer, element.x, element.y, element, "full")
            canvas = Image.alpha_composite(canvas, layer)
        if include_cta:
            if self._has_cta_video:
                # Cover-fill the box with the first clip in this row's order
                # (matches the render, which cover-fills each clip to the box).
                frame = self._cover_frame(spec.cta_video_w, spec.cta_video_h,
                                          self._lead_cta_path(spec))
                canvas.paste(frame.convert("RGBA"), (spec.cta_video_x, spec.cta_video_y))
            cta = self._get_cta(spec.cta_w, spec.cta_h)
            canvas.paste(cta, (spec.cta_x, spec.cta_y), cta)
        return canvas

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

    # ------------------------------------------------------------- FFmpeg

    def build_ffmpeg_command(self, spec: RowSpec, base_png: Path,
                             overlay_png: Path, cta_png: Path,
                             out_path: Path) -> list[str]:
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

          CTA videos (inputs 4..4+M-1, present only when uploaded) -> [ctav]
              One sample is chosen per slot for this row (inputs are exactly those
              picks). Each is cover-filled to the CTA box (scale=increase + crop,
              so all share one size) and sped up/slowed by its own
              `setpts=PTS/SPEED`, then joined with `concat` in the fixed slot
              order into one stream and alpha-faded in. They play once through (the
              last frame holds if the promo outlasts them); -shortest trims excess.

          [3:v]format=rgba,fade=t=in:st=CFS:d=CFD:alpha=1 -> [cta]
              The CTA image as its own stream: force an alpha-capable format, then
              fade ONLY the alpha channel — fully transparent until cta_fade_start,
              fully visible cta_fade_duration later.

          Z-order: the four overlay layers — promo video [vidB], CTA video [ctav],
          CTA image [cta], texts [2:v] — are stacked onto [anchored] in ascending
          order of their sidebar z-index (video_z / cta_video_z / cta_image_z /
          text_z; higher = on top). The background is always the base. Equal
          z-indexes fall back to a fixed priority (promo < CTA video < CTA image <
          texts) so the order is deterministic. The topmost overlay also converts
          to yuv420p — required for maximum player/social-platform compatibility.

        Inputs use -loop 1 -framerate 30 so the still images behave as 30fps
        streams aligned with the output rate. -shortest at the muxer trims audio
        to the video length; -movflags +faststart relocates the moov atom for
        instant playback start after upload.
        """
        cfg = self.config
        # spec.video_* are the per-row resolved box (Excel Video_* overrides,
        # the configured values, or a randomized spot when randomize_video_pos
        # is enabled). The promo video defines the render length: [vidA] anchors
        # the otherwise-infinite looped background to the promo's duration, and
        # [vidB] is the promo's visible layer painted in z-order below.
        video_pos = (f"x='{spec.video_x}+({spec.video_w}-w)/2'"
                     f":y='{spec.video_y}+({spec.video_h}-h)/2'")
        parts = [
            f"[1:v]scale={spec.video_w}:{spec.video_h}"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"split[vidA][vidB];",
            f"[0:v][vidA]overlay={video_pos}:shortest=1[anchored];",
        ]
        clips = spec.cta_video_clips or []
        has_ctav = bool(self._has_cta_video and clips)
        if has_ctav:
            n = len(clips)
            speeds = spec.cta_video_clip_speeds or [1.0] * n
            cw, ch = spec.cta_video_w, spec.cta_video_h
            # Cover-fill each chosen clip to the box so they share one size
            # (needed to concat); inputs 4..4+n-1 are the per-row picks in order.
            # setpts=PTS/SPEED is applied per clip BEFORE the concat so each slot
            # plays at its own speed; concat then re-stamps the joined timeline.
            labels = []
            for k in range(n):
                sp = speeds[k] if k < len(speeds) else 1.0
                parts.append(
                    f"[{4 + k}:v]fps={FPS},"
                    f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                    f"crop={cw}:{ch},setsar=1,setpts=PTS/{sp:.4f},format=rgba[cv{k}];"
                )
                labels.append(f"[cv{k}]")
            if n > 1:
                parts.append(f"{''.join(labels)}concat=n={n}:v=1:a=0[cseq];")
                seq = "[cseq]"
            else:
                seq = labels[0]
            parts.append(
                f"{seq}fade=t=in:st={spec.cta_video_fade_start}"
                f":d={spec.cta_video_fade_duration}:alpha=1[ctav];"
            )
        parts.append(
            f"[3:v]format=rgba,"
            f"fade=t=in:st={spec.cta_fade_start}:d={spec.cta_fade_duration}:alpha=1[cta];"
        )
        # Stack the overlay layers by their sidebar z-index (higher = on top; the
        # background is always the base). Ties fall back to the fixed priority in
        # the second tuple field so the order stays deterministic. Each tuple:
        # (z-index, tie-break priority, overlay input, overlay position).
        layers = [
            (cfg.video_z, 0, "[vidB]", video_pos),
            (cfg.cta_image_z, 2, "[cta]", f"{spec.cta_x}:{spec.cta_y}"),
            (cfg.text_z, 3, "[2:v]", "0:0"),
        ]
        if has_ctav:
            layers.append((cfg.cta_video_z, 1, "[ctav]",
                           f"{spec.cta_video_x}:{spec.cta_video_y}"))
        layers.sort(key=lambda layer: (layer[0], layer[1]))

        last = "anchored"
        for i, (_z, _prio, label, pos) in enumerate(layers):
            top = i == len(layers) - 1
            out = "out" if top else f"z{i}"
            fmt = ",format=yuv420p" if top else ""
            sep = "" if top else ";"  # the final [out] feeds -map, no trailing ;
            parts.append(f"[{last}]{label}overlay={pos}{fmt}[{out}]{sep}")
            last = out
        filter_complex = "".join(parts)

        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(FPS), "-i", str(base_png),
            "-i", str(self.video_path),
            "-loop", "1", "-framerate", str(FPS), "-i", str(overlay_png),
            "-loop", "1", "-framerate", str(FPS), "-i", str(cta_png),
        ]
        # CTA clips: this row's chosen sample per slot, in fixed play order; the
        # filter concats them. They play once through (no -stream_loop).
        for clip_path in clips:
            cmd += ["-i", str(clip_path)]
        cmd += ["-filter_complex", filter_complex, "-map", "[out]"]
        if cfg.include_audio:
            # '1:a?' = take audio from the promo video if it exists; never fail
            # without it. The CTA video's audio is intentionally ignored.
            cmd += ["-map", "1:a?", "-c:a", "aac", "-b:a", cfg.audio_bitrate]
        else:
            cmd += ["-an"]
        cmd += [
            "-c:v", "libx264",
            "-preset", cfg.preset,
            "-crf", str(cfg.crf),
            "-r", str(FPS),
            "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ]
        return cmd

    # ------------------------------------------------------------- per-row render

    def render_row(self, row_number: int, row: pd.Series) -> RowResult:
        """Render one Excel row to an MP4. Never raises — failures are captured
        in the returned RowResult so one bad row can't abort the batch."""
        spec = RowSpec.from_row(row)
        base_png = self.work_dir / f"row_{row_number:04d}_base.png"
        overlay_png = self.work_dir / f"row_{row_number:04d}_overlay.png"
        cta_png = self.work_dir / f"row_{row_number:04d}_cta.png"
        filename = safe_filename(row_number, spec.headline.text)
        out_path = self.output_dir / filename
        try:
            self.build_base_image(spec).save(base_png)
            # resolves positions; the CTA ships as its own input so FFmpeg
            # can fade it in (see build_ffmpeg_command)
            self.build_overlay_image(spec, include_cta=False).save(overlay_png)
            self._get_cta(spec.cta_w, spec.cta_h).save(cta_png)

            cmd = self.build_ffmpeg_command(spec, base_png, overlay_png, cta_png, out_path)
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.config.ffmpeg_timeout
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
            return RowResult(row_number, False, error=error, warnings=spec.warnings)
        except Exception as exc:  # noqa: BLE001 — per-row isolation is the point
            logger.error("Row %d FAILED: %s", row_number, exc)
            out_path.unlink(missing_ok=True)
            return RowResult(row_number, False, error=str(exc), warnings=spec.warnings)
        finally:
            base_png.unlink(missing_ok=True)
            overlay_png.unlink(missing_ok=True)
            cta_png.unlink(missing_ok=True)

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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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

    def render_preview(self, row: pd.Series) -> Image.Image:
        """Static composite of one row — same layout math as the real render,
        with the video represented by its first frame."""
        spec = RowSpec.from_row(row)
        base = self.build_base_image(spec).convert("RGBA")
        overlay = self.build_overlay_image(spec)  # resolves positions first

        frame = self._fit_frame(spec.video_w, spec.video_h)
        fx = spec.video_x + (spec.video_w - frame.width) // 2
        fy = spec.video_y + (spec.video_h - frame.height) // 2
        base.paste(frame, (fx, fy))

        return Image.alpha_composite(base, overlay).convert("RGB")

    def build_editor_payload(self, row: pd.Series) -> dict:
        """Everything the interactive preview editor needs, with each layer
        shipped separately so the browser can move/recolor elements without a
        server round-trip: the background, the promo video's first frame scaled
        into its box, the optional CTA video's first frame, the CTA image, and
        per text — a white alpha-mask PNG of the fill glyphs (recolored
        client-side via CSS mask-image) plus a baked 'decoration' PNG carrying
        the artistic style (outline/shadow/neon) behind it. The text's
        background highlight box is described geometrically (bg_w/bg_h/bg_radius)
        so the editor can draw and recolor it live in CSS.

        Texts carry their CENTER point (the Excel convention for *_X/*_Y);
        the video and CTA boxes carry their top-left corner."""
        spec = RowSpec.from_row(row)
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
            deco = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            self._paint_text(deco, ax, ay, element, "decoration")

            # Background highlight box geometry (canvas px), matching _paint_text's
            # 'full' box, so the editor draws/recolors it live in CSS.
            bgp = max(6, round(element.size * 0.30))
            bg_w, bg_h = int(round(r - l)) + 2 * bgp, int(round(b - t)) + 2 * bgp

            texts.append({
                "role": element.role,
                "cx": element.x, "cy": element.y, "w": iw, "h": ih,
                "size": element.size,
                "color": "#%02X%02X%02X" % element.color[:3],
                "font": element.font,
                "style": element.style,
                "bg": ("#%02X%02X%02X" % element.bg_color[:3]) if element.bg_color else None,
                "bg_w": bg_w, "bg_h": bg_h, "bg_radius": round(bg_h * 0.30),
                "mask": _img_to_data_uri(ink),
                "deco": _img_to_data_uri(deco),
            })

        payload = {
            "canvas_w": CANVAS_W,
            "canvas_h": CANVAS_H,
            "bg": _img_to_data_uri(self.build_base_image(spec), "JPEG"),
            "video": {
                "x": spec.video_x, "y": spec.video_y,
                "w": spec.video_w, "h": spec.video_h,
                "frame": _img_to_data_uri(frame.convert("RGB"), "JPEG"),
                "frame_w": frame.width, "frame_h": frame.height,
            },
            "cta": {
                "x": spec.cta_x, "y": spec.cta_y, "w": spec.cta_w, "h": spec.cta_h,
                "img": _img_to_data_uri(self._get_cta(spec.cta_w, spec.cta_h)),
            },
            "texts": texts,
            # Sidebar layer order — the editor applies these as CSS z-index so the
            # preview stacking matches the render (higher = on top).
            "z": {
                "video": self.config.video_z, "cta_video": self.config.cta_video_z,
                "cta_image": self.config.cta_image_z, "text": self.config.text_z,
            },
        }
        if self._has_cta_video:
            cv = self._cover_frame(spec.cta_video_w, spec.cta_video_h, self._lead_cta_path(spec))
            payload["cta_video"] = {
                "x": spec.cta_video_x, "y": spec.cta_video_y,
                "w": spec.cta_video_w, "h": spec.cta_video_h,
                "frame": _img_to_data_uri(cv.convert("RGB"), "JPEG"),
                "frame_w": cv.width, "frame_h": cv.height,
            }
        return payload
