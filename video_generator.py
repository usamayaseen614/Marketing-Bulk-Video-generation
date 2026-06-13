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
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger("video_generator")

# --------------------------------------------------------------------------- constants

CANVAS_W = 1080   # vertical 9:16 canvas for Reels / TikTok / Shorts
CANVAS_H = 1920
FPS = 30

# The CTA is invisible until CTA_FADE_START seconds, then fades in (alpha
# only) and is fully visible at CTA_FADE_START + CTA_FADE_DURATION.
CTA_FADE_START = 1.0
CTA_FADE_DURATION = 0.5

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
    "Headline", "Headline_Size", "Headline_Color", "Headline_X", "Headline_Y",
    "Subheading", "Subheading_Size", "Subheading_Color", "Subheading_X", "Subheading_Y",
    "Footer", "Footer_Size", "Footer_Color", "Footer_X", "Footer_Y",
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


def _parse_color(value, warnings: list[str], label: str) -> Optional[tuple]:
    """Accept '#RRGGBB', '#RGB', 'rgb(...)' or CSS color names ('yellow',
    'blue', 'Light Yellow'...). Blank => None (a random palette color is
    chosen later); unrecognized => warn + None."""
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
            warnings.append(f"{label}: unrecognized color '{raw}', using a random color")
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
    crf: int = 18                 # 16-28; lower = higher quality / bigger files
    preset: str = "medium"        # x264 speed/size tradeoff
    # When True, video_x/video_y are ignored and each row's video box gets a
    # seeded-random position avoiding the CTA and explicitly-placed texts.
    randomize_video_pos: bool = False
    audio_bitrate: str = "192k"
    include_audio: bool = True
    font_path: Optional[str] = None
    ffmpeg_timeout: int = 600     # seconds per row before a render is killed


@dataclass
class TextSpec:
    text: str
    role: str                # 'Headline' / 'Subheading' / 'Footer'
    size: Optional[int]      # None = randomize within the role's size range
    color: Optional[tuple]   # None = random palette color
    x: Optional[int]         # None = auto-place (X/Y cell left blank in the Excel)
    y: Optional[int]


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
    ):
        self.config = config
        self.video_path = Path(video_path)
        self.work_dir = Path(work_dir)
        self.output_dir = Path(output_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.ffmpeg = find_ffmpeg()
        logger.info("Using FFmpeg binary: %s", self.ffmpeg)

        self._bg_index, self._bg_names = self._build_bg_index(Path(bg_dir))
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self._font_lock = threading.Lock()
        self._font_path = config.font_path or find_default_font()
        if self._font_path is None:
            logger.warning("No TrueType font found; falling back to PIL default font")

        # One CTA image is shared by every row, but rows may override its box
        # via CTA_Width/CTA_Height — cache one resized copy per distinct size.
        self._cta_src = Image.open(cta_path).convert("RGBA")
        self._cta_cache: dict[tuple[int, int], Image.Image] = {}
        self._cta_lock = threading.Lock()

        self._preview_frame_lock = threading.Lock()
        self._preview_frame: Optional[Image.Image] = None

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

    def _get_font(self, size: int) -> ImageFont.ImageFont:
        with self._font_lock:
            font = self._fonts.get(size)
            if font is None:
                if self._font_path:
                    font = ImageFont.truetype(self._font_path, size)
                else:
                    font = ImageFont.load_default(size=size)
                self._fonts[size] = font
            return font

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

        font = self._get_font(element.size)
        while element.size > 18 and max(font.getlength(w) for w in words) > max_w:
            element.size -= 2
            font = self._get_font(element.size)

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
            (0, 0), element.text, font=self._get_font(element.size),
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
        cta_rect = (spec.cta_x, spec.cta_y,
                    spec.cta_x + spec.cta_w, spec.cta_y + spec.cta_h)

        color_deck = list(RANDOM_TEXT_COLORS)
        for element in spec.text_elements:
            if not element.text:
                continue
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
        for element, w, h in pending:
            x, y, clean = self._find_spot(element.x, element.y, w, h, occupied, rng)
            element.x, element.y = x, y
            if not clean:
                spec.warnings.append(
                    f"'{element.text[:40]}': no overlap-free spot found, "
                    f"placed at least-crowded position ({x}, {y})"
                )
            occupied.append(_rect_from_center(x, y, w, h))

    def build_overlay_image(self, spec: RowSpec, include_cta: bool = True) -> Image.Image:
        """Text layer: transparent canvas with the three texts — plus the CTA
        image for static composites (previews). The video render passes
        include_cta=False because the CTA is a separate FFmpeg input there,
        faded in over CTA_FADE_START..+DURATION (z-order preserved: the CTA
        still ends up above the texts).
        Text X/Y are interpreted as the CENTER of the text block (anchor='mm');
        blank coordinates are auto-placed (see _resolve_positions)."""
        self._resolve_positions(spec)
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        for element in spec.text_elements:
            if element.text:
                draw.text(
                    (element.x, element.y),
                    element.text,
                    font=self._get_font(element.size),
                    fill=element.color,
                    anchor="mm",
                    align="center",
                )
        if include_cta:
            cta = self._get_cta(spec.cta_w, spec.cta_h)
            canvas.paste(cta, (spec.cta_x, spec.cta_y), cta)
        return canvas

    # ------------------------------------------------------------- FFmpeg

    def build_ffmpeg_command(self, spec: RowSpec, base_png: Path,
                             overlay_png: Path, cta_png: Path,
                             out_path: Path) -> list[str]:
        """
        Single-pass composite. Filter graph explained:

          [1:v]scale=W:H:force_original_aspect_ratio=decrease
              Scale the uploaded video to fit INSIDE the configured box while
              preserving its aspect ratio (never distorts; upscales small sources,
              downscales large ones). force_divisible_by=2 keeps yuv chroma happy.

          [0:v][vid]overlay=x='X+(W-w)/2':y='Y+(H-h)/2':shortest=1
              Place the scaled video centered within its box on the background.
              'w'/'h' are the scaled video's runtime dimensions, so any leftover
              box area becomes implicit padding where the background shows through
              (nicer than black bars). shortest=1 is critical: the looped base
              image is an infinite stream, so the overlay must terminate when the
              video input ends or the render would never finish.

          [bgvid][2:v]overlay=0:0
              Stamp the full-canvas text layer on top.

          [3:v]format=rgba,fade=t=in:st=1:d=0.5:alpha=1
              The CTA image as its own stream: force an alpha-capable format,
              then fade ONLY the alpha channel — fully transparent until
              CTA_FADE_START, fully visible CTA_FADE_DURATION later.

          [txt][cta]overlay=CTA_X:CTA_Y,format=yuv420p
              Place the fading CTA at its box (above the texts, matching the
              static preview's z-order), then convert to yuv420p — required
              for maximum player/social-platform compatibility.

        Inputs use -loop 1 -framerate 30 so the still images behave as 30fps
        streams aligned with the output rate. -shortest at the muxer trims audio
        to the video length; -movflags +faststart relocates the moov atom for
        instant playback start after upload.
        """
        cfg = self.config
        # spec.video_* are the per-row resolved box (Excel Video_* overrides,
        # the configured values, or a randomized spot when randomize_video_pos
        # is enabled).
        filter_complex = (
            f"[1:v]scale={spec.video_w}:{spec.video_h}"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2[vid];"
            f"[0:v][vid]overlay="
            f"x='{spec.video_x}+({spec.video_w}-w)/2'"
            f":y='{spec.video_y}+({spec.video_h}-h)/2':shortest=1[bgvid];"
            f"[bgvid][2:v]overlay=0:0[txt];"
            f"[3:v]format=rgba,"
            f"fade=t=in:st={CTA_FADE_START}:d={CTA_FADE_DURATION}:alpha=1[cta];"
            f"[txt][cta]overlay={spec.cta_x}:{spec.cta_y},format=yuv420p[out]"
        )
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(FPS), "-i", str(base_png),
            "-i", str(self.video_path),
            "-loop", "1", "-framerate", str(FPS), "-i", str(overlay_png),
            "-loop", "1", "-framerate", str(FPS), "-i", str(cta_png),
            "-filter_complex", filter_complex,
            "-map", "[out]",
        ]
        if cfg.include_audio:
            # '1:a?' = take audio from the video if it exists; never fail without it.
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

    def _first_video_frame(self) -> Image.Image:
        """Extract (and cache) the uploaded video's first frame for previews."""
        with self._preview_frame_lock:
            if self._preview_frame is None:
                frame_png = self.work_dir / "preview_frame.png"
                cmd = [
                    self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(self.video_path),
                    "-frames:v", "1", "-update", "1", str(frame_png),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode != 0 or not frame_png.is_file():
                    raise RuntimeError(f"Could not extract preview frame:\n{proc.stderr[-2000:]}")
                with Image.open(frame_png) as img:
                    self._preview_frame = img.convert("RGB").copy()
                frame_png.unlink(missing_ok=True)
            return self._preview_frame

    def _fit_frame(self, box_w: int, box_h: int) -> Image.Image:
        """Scale the preview frame into a box exactly like FFmpeg's
        scale=W:H:force_original_aspect_ratio=decrease — including upscaling
        small sources (Image.thumbnail only ever shrinks)."""
        frame = self._first_video_frame()
        ratio = min(box_w / frame.width, box_h / frame.height)
        size = (max(2, round(frame.width * ratio)), max(2, round(frame.height * ratio)))
        return frame.resize(size, Image.LANCZOS)

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
        server round-trip: the background, the video's first frame scaled into
        its box, the CTA, and one white alpha-mask PNG per text (recolored
        client-side via CSS mask-image, preserving PIL's exact glyphs).

        Texts carry their CENTER point (the Excel convention for *_X/*_Y);
        the video and CTA boxes carry their top-left corner."""
        spec = RowSpec.from_row(row)
        self._resolve_positions(spec)

        frame = self._fit_frame(spec.video_w, spec.video_h)

        texts = []
        for element in spec.text_elements:
            if not element.text:
                continue
            w, h = self._measure_text(element)
            iw, ih = int(w) + 6, int(h) + 6  # small pad so antialiasing isn't clipped
            mask = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
            ImageDraw.Draw(mask).text(
                (iw / 2, ih / 2), element.text, font=self._get_font(element.size),
                fill=(255, 255, 255, 255), anchor="mm", align="center",
            )
            texts.append({
                "role": element.role,
                "cx": element.x, "cy": element.y, "w": iw, "h": ih,
                "size": element.size,
                "color": "#%02X%02X%02X" % element.color[:3],
                "mask": _img_to_data_uri(mask),
            })

        return {
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
        }
