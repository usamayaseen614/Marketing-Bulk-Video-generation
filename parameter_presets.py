"""Named settings without assets, shared by the UI and editable Excel files."""

from __future__ import annotations

import io
import json
import math
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

import config
import generation_defaults as defaults
import presets
from speech import synth
from video_generator import (
    CANVAS_W, CANVAS_H, DEFAULT_CTA_VIDEO_SLOTS, MAX_CTA_VIDEO_SLOTS,
    FONT_CHOICES, TEXT_STYLES, FPS_CHOICES, PROMO_ALT_SECONDS,
    SPLIT_AUDIO_MAX_CHUNKS, SPLIT_AUDIO_MAX_SPREAD,
)
from workspace import MAX_PROMO_VIDEOS

VERSION = 1
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADERS = ("key", "group", "label", "value", "unit", "allowed_values")


@dataclass(frozen=True)
class Parameter:
    label: str
    group: str
    default: object
    minimum: float | None = None
    maximum: float | None = None
    options: tuple = ()
    unit: str = ""
    max_selections: int | None = None

    @property
    def allowed_values(self) -> str:
        if self.options:
            text = ", ".join(str(v) for v in self.options)
            return text + (f" (up to {self.max_selections})" if self.max_selections else "")
        if isinstance(self.default, bool):
            return "TRUE, FALSE"
        if self.minimum is not None:
            return f"{self.minimum:g} to {self.maximum:g}"
        return "Text"


def parameters() -> dict[str, Parameter]:
    """The allowlist uses the existing keep() names and UI units."""
    p = {
        "layout_mode": Parameter("Layout mode", "Layout", "free", options=("free", "split")),
        "swap_sides": Parameter("Swap left / right", "Layout", False),
        "split_panel_h": Parameter("Panel height", "Layout", 960, 100, CANVAS_H, unit="px"),
        "crop_to_panels": Parameter("No background — output only the panels", "Layout", False),
        "bg_color": Parameter("Background color", "Layout", "#1E1B4B"),
        "bg_video_opacity": Parameter("Background video opacity", "Background video", defaults.BG_VIDEO_OPACITY, 1, 50, unit="%"),
        "bg_video_min_seconds": Parameter("Minimum seconds per background video", "Background video", defaults.BG_VIDEO_MIN_SECONDS, 1, 120, unit="s"),
        "music_volume": Parameter("Music volume", "Audio", defaults.MUSIC_VOLUME, 0, 100, unit="%"),
        "music_min_seconds": Parameter("Minimum seconds per track", "Audio", defaults.MUSIC_MIN_SECONDS, 1, 600, unit="s"),
        "split_audio": Parameter("Split the audio track (random speed)", "Audio", defaults.SPLIT_AUDIO),
        "split_audio_chunks": Parameter("Chunks", "Audio", defaults.SPLIT_AUDIO_CHUNKS, 2, SPLIT_AUDIO_MAX_CHUNKS),
        "split_audio_spread": Parameter("Speed variation", "Audio", defaults.SPLIT_AUDIO_SPREAD, 5, int(SPLIT_AUDIO_MAX_SPREAD * 100), unit="%"),
        "voice_engine": Parameter("Speech engine", "Voiceover", synth.normalize_engine(config.VOICE_ENGINE), options=tuple(synth.ENGINES)),
        "voice_enabled": Parameter("Voiceover / timed captions", "Voiceover", False),
        "voice_speed": Parameter("Speaking speed", "Voiceover", float(config.VOICE_SPEED), .7, 1.4, unit="×"),
        "beat_max_words": Parameter("Words per caption", "Voiceover", config.BEAT_MAX_WORDS, 1, 8),
        "screen_text_y": Parameter("Caption height", "Voiceover", config.SCREEN_TEXT_Y, 200, CANVAS_H - 200, unit="px"),
        "voice_lead_in": Parameter("Silence before the first word", "Voiceover", float(config.VOICE_LEAD_IN), 0, 5, unit="s"),
        "voice_duck": Parameter("Duck the other audio under the voice", "Voiceover", config.VOICE_DUCK),
        "voice_duck_ratio": Parameter("Ducking strength", "Voiceover", float(config.VOICE_DUCK_RATIO), 2, 20),
        "voice_gain": Parameter("Voice level", "Voiceover", float(config.VOICE_GAIN), .3, 1),
        "voice_mute_promo": Parameter("Mute the promo's own audio while narrating", "Voiceover", config.VOICE_MUTE_PROMO),
        "voice_loop_promo": Parameter("The script sets the video length", "Voiceover", config.VOICE_LOOP_PROMO),
        "randomize_video": Parameter("Randomize position per video", "Promo", False),
        "promo_alt": Parameter("Use Promo Alternate clips instead", "Promo", False),
        "promo_alt_seconds": Parameter("Length without a voiceover", "Promo", float(PROMO_ALT_SECONDS), 1, 600, unit="s"),
        "promo_alt_mute": Parameter("Mute the clips' own audio", "Promo", True),
        "cta_slot_count": Parameter("Number of clip slots", "CTA video", DEFAULT_CTA_VIDEO_SLOTS, 1, MAX_CTA_VIDEO_SLOTS),
        "cta_video_fill": Parameter("Keep clips playing to fill the whole video", "CTA video", False),
        "gif_min_seconds": Parameter("Minimum seconds per gif", "GIF", 5.0, 1, 30, unit="s"),
        "default_font": Parameter("Default font", "Text", FONT_CHOICES[0], options=tuple(FONT_CHOICES)),
        "default_style": Parameter("Default artistic style", "Text", TEXT_STYLES[0], options=tuple(TEXT_STYLES)),
        "text_opacity_pct": Parameter("Text opacity", "Text", 100, 0, 100, unit="%"),
        "text_bg_opacity_pct": Parameter("Highlight box opacity", "Text", 100, 0, 100, unit="%"),
        "subliminal_targets": Parameter("Apply to", "Subliminal", list(defaults.SUBLIMINAL_TARGETS), options=("Headline", "Subheading", "Footer"), max_selections=2),
        "subliminal_mode": Parameter("Effect style", "Subliminal", "hide", options=("hide", "show")),
        "subliminal_pattern": Parameter("Hidden characters", "Subliminal", "random", options=("random", "ordered")),
        "subliminal_k": Parameter("Frames per cycle (K)", "Subliminal", defaults.SUBLIMINAL_K, 2, 8),
        "subliminal_hide_pct": Parameter("Hidden per frame", "Subliminal", defaults.SUBLIMINAL_HIDE_PCT, 15, 70, unit="%"),
        "subliminal_granularity": Parameter("Split by", "Subliminal", "word", options=("word", "char")),
        "subliminal_all_intra": Parameter("Preserve frame-by-frame (larger files)", "Subliminal", defaults.SUBLIMINAL_ALL_INTRA),
        "fps": Parameter("Frame rate", "Output", FPS_CHOICES[0], options=tuple(FPS_CHOICES), unit="fps"),
        "crf": Parameter("Quality (CRF)", "Output", 18, 16, 28),
        "encoder_preset": Parameter("Encoder speed", "Output", defaults.ENCODER_PRESET, options=("veryfast", "faster", "fast", "medium", "slow")),
        "workers": Parameter("Parallel renders", "Output", defaults.max_workers(), 1, defaults.max_workers()),
        "clip_source": Parameter("Where do the clips come from?", "Sources", "upload", options=("upload", "drive_folder", "scrape_job", "scrape_now")),
        "drive_clip_layout": Parameter("Folder layout", "Sources", "per_slot", options=("per_slot", "pooled")),
        "scrape_limit": Parameter("Max videos to scan", "Sources", 200, 10, 5000),
        "trim_start": Parameter("Trim start", "Sources", float(config.SCRAPE_TRIM_START), 0, 60, unit="s"),
        "trim_duration": Parameter("Trim length", "Sources", float(config.SCRAPE_TRIM_DURATION), 1, 60, unit="s"),
        "clip_strategy": Parameter("Which clips to use", "Sources", "top_views", options=("top_views", "all")),
        "clips_per_slot": Parameter("Clips per slot", "Sources", 10, 1, 50),
        "caption_mode": Parameter("Captions", "Captions", "existing", options=("existing", "generate", "none")),
        "fixed_tail": Parameter("End every filename with a fixed call-to-action line", "Captions", False),
        "hashtag_source": Parameter("Hashtags", "Captions", defaults.HASHTAG_SOURCE, options=("pool", "excel", "none")),
        "script_source": Parameter("Scripts", "Captions", "upload", options=("upload", "generate")),
        "script_prompt": Parameter("Script prompt", "Captions", ""),
        "caption_theme": Parameter("Caption theme", "Captions", config.CAPTION_THEME),
        "caption_count": Parameter("Captions", "Captions", 500, 50, config.CAPTION_POOL_MAX),
        "hashtag_count": Parameter("Hashtag sets", "Captions", 100, 25, config.HASHTAG_POOL_MAX),
        "n_batches": Parameter("Batches to render", "Generate", 1, 1, max(20, MAX_PROMO_VIDEOS)),
        "n_folders": Parameter("Output folders", "Generate", 1, 1, max(20, MAX_PROMO_VIDEOS)),
        "max_per_folder": Parameter("Max copies of one promo per folder", "Generate", 0, 0, 10000),
        "upload_platforms": Parameter("Publish which names?", "Generate", ",".join(p for p in ("yt", "tk") if p in config.UPLOAD_PLATFORMS) or defaults.UPLOAD_PLATFORMS, options=("yt,tk", "tk", "yt")),
    }
    for name, label, group, coords, size_floor in (
        ("bgv", "BG video", "Background video", (0, 0, CANVAS_W, CANVAS_H), 50),
        ("video", "Video", "Promo", (90, 300, 900, 900), 50),
        ("cta", "CTA", "CTA image", (340, 1600, 400, 160), 10),
        ("cta_video", "CTA video", "CTA video", (360, 1200, 360, 360), 50),
        ("gif", "GIF box", "GIF", (60, 560, 360, 360), 50),
    ):
        for axis, title, value, ceiling in zip(("x", "y", "w", "h"), ("X", "Y", "width", "height"), coords, (CANVAS_W, CANVAS_H, CANVAS_W, CANVAS_H)):
            p[f"{name}_{axis}"] = Parameter(f"{label} {title}", group, value, 0 if axis in "xy" else size_floor, ceiling, unit="px")
    for name, group, start, duration in (("cta", "CTA image", 1.0, .5), ("cta_video", "CTA video", .5, .5), ("gif", "GIF", 0.0, 0.0)):
        p[f"{name}_fade_start"] = Parameter("Fade-in start", group, start, 0, 30, unit="s")
        p[f"{name}_fade_duration"] = Parameter("Fade-in duration", group, duration, 0, 30, unit="s")
    for i, name in enumerate(("video", "gif", "cta_video", "cta_image", "text"), 1):
        p[f"z_{name}"] = Parameter(name.replace("_", " ").title(), "Layer order", i, 1, 99)
    for i in range(1, MAX_CTA_VIDEO_SLOTS + 1):
        p[f"cta_speed_{i}"] = Parameter(f"Clip {i} speed", "CTA video", 1.0, .25, 4, unit="×")
    for role in ("Headline", "Subheading", "Footer"):
        for axis, title, ceiling in (("w", "width", CANVAS_W), ("h", "height", CANVAS_H)):
            p[f"tbox_{axis}_{role}"] = Parameter(f"{role} box {title}", "Text", 0, 0, ceiling, unit="px")
    for engine in synth.ENGINES:
        voices = synth.voices(engine)
        chosen = [v for v in config.VOICE_SET if v in voices] or [synth.default_voice(engine)]
        p[f"voice_set_{engine}"] = Parameter(f"Voices ({engine})", "Voiceover", chosen, options=tuple(voices))
    for name, label in (("gif_source", "GIFs"), ("bg_image_source", "Background images"), ("bg_video_source", "Background videos"), ("music_source", "Music")):
        p[name] = Parameter(f"{label} source", "Sources", "drive_folder", options=("upload", "drive_folder"))
    return p


def default_values() -> dict:
    return {key: deepcopy(p.default) for key, p in parameters().items()}


def validate(values: dict) -> dict:
    """Validate an external workbook/record once, filling omitted fields."""
    if not isinstance(values, dict):
        raise ValueError("Parameters must be a key/value mapping.")
    specs = parameters()
    unknown = values.keys() - specs.keys()
    if unknown:
        raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
    result = default_values()
    errors = []
    for key, raw in values.items():
        try:
            result[key] = _parse_value(key, raw, specs[key])
        except ValueError as exc:
            errors.append(f"{key}: {exc}")
    if errors:
        raise ValueError("\n".join(errors))
    return result


def _parse_value(key: str, raw, spec: Parameter):
    kind = type(spec.default)
    if kind is bool:
        if isinstance(raw, bool):
            value = raw
        elif isinstance(raw, str) and raw.strip().upper() in ("TRUE", "FALSE"):
            value = raw.strip().upper() == "TRUE"
        else:
            raise ValueError("use TRUE or FALSE")
    elif kind in (int, float):
        if isinstance(raw, bool) or raw is None or raw == "":
            raise ValueError("a number is required")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError("a number is required") from None
        if not math.isfinite(number) or (kind is int and not number.is_integer()):
            raise ValueError("use a finite " + ("integer" if kind is int else "number"))
        value = kind(number)
    elif kind is list:
        if raw is None or raw == "":
            value = []
        elif isinstance(raw, str):
            value = [v.strip() for v in raw.split(",") if v.strip()]
        elif isinstance(raw, list) and all(isinstance(v, str) for v in raw):
            value = list(raw)
        else:
            raise ValueError("use a comma-separated list of choices")
        if len(set(value)) != len(value) or any(v not in spec.options for v in value):
            raise ValueError("use distinct choices from: " + spec.allowed_values)
        if spec.max_selections and len(value) > spec.max_selections:
            raise ValueError(spec.allowed_values)
    else:
        if raw is not None and not isinstance(raw, str):
            raise ValueError("text is required")
        value = raw or ""
    if spec.minimum is not None and not spec.minimum <= value <= spec.maximum:
        raise ValueError("allowed range: " + spec.allowed_values)
    if spec.options and kind is not list and value not in spec.options:
        raise ValueError("choose from: " + spec.allowed_values)
    if key == "bg_color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("use a color such as #1E1B4B")
    return value


def save(name: str, values: dict, *, replace_id: str | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Enter a preset name.")
    records = list_presets()
    if replace_id is not None and not any(r["id"] == replace_id for r in records):
        raise ValueError("That parameter preset no longer exists.")
    if any(r["name"] == name and r["id"] != replace_id for r in records):
        raise ValueError("That name already exists. Select it and use Overwrite selected.")
    previous = next((r for r in records if r["id"] == replace_id), {})
    record = dict(version=VERSION, id=replace_id or uuid.uuid4().hex, name=name,
                  values=deepcopy(values), created_at=previous.get("created_at", time.time()),
                  updated_at=time.time())
    presets._write_json(_path(record["id"]), record)
    return record


def list_presets() -> list[dict]:
    folder = config.PRESETS_ROOT / "parameter_presets"
    records = []
    for path in folder.glob("*.json"):
        if path.name.startswith(".part-"):
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["id"] != path.stem:
            raise ValueError(f"Parameter preset ID does not match {path.name}.")
        records.append(record)
    return sorted(records, key=lambda r: r["name"].casefold())


def delete(preset_id: str) -> None:
    _path(preset_id).unlink(missing_ok=True)


def _path(preset_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", preset_id):
        raise ValueError("Invalid parameter preset ID.")
    return config.PRESETS_ROOT / "parameter_presets" / f"{preset_id}.json"


def record_values(record: dict) -> dict:
    if record.get("version") != VERSION:
        raise ValueError("Unsupported parameter preset version.")
    return validate(record.get("values"))


def to_excel(record: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Parameters"
    ws.append(["format_version", VERSION])
    ws.append(["preset_name", record["name"]])
    ws.append(["Edit value cells. Missing rows use current defaults. Lists use commas; booleans use TRUE/FALSE."])
    ws.append(HEADERS)
    for key, spec in parameters().items():
        value = record["values"].get(key, spec.default)
        if isinstance(value, list):
            value = ", ".join(value)
        ws.append([key, spec.group, spec.label, value, spec.unit, spec.allowed_values])
    # Prompts and names are literal text, including text beginning with '='.
    for row in ws:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in ws[4]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="234E70")
    for row in ws.iter_rows(min_row=5):
        row[3].fill = PatternFill("solid", fgColor="E8F3FF")
    for col, width in zip("ABCDEF", (28, 20, 48, 48, 10, 48)):
        ws.column_dimensions[col].width = width
    ws.merge_cells("A3:F3")
    ws.freeze_panes = "D5"
    ws.auto_filter.ref = f"A4:F{ws.max_row}"
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def from_excel(data: bytes) -> tuple[str, dict]:
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:
        raise ValueError("Cannot read this .xlsx file.") from exc
    try:
        if "Parameters" not in wb.sheetnames:
            raise ValueError("Missing Parameters worksheet; upload a parameter preset export.")
        ws = wb["Parameters"]
        rows = iter(ws.iter_rows())
        version, name, _, header = (next(rows, ()) for _ in range(4))
        if len(version) < 2 or version[0].value != "format_version" or version[1].value != VERSION:
            raise ValueError("Unsupported or missing parameter preset version.")
        if len(name) < 2 or name[0].value != "preset_name" or not isinstance(name[1].value, str) or not name[1].value.strip():
            raise ValueError("A preset_name is required in B2.")
        if tuple(c.value for c in header[:6]) != HEADERS:
            raise ValueError("Parameter table headers must match the exported template.")
        values = {}
        for row_number, row in enumerate(rows, start=5):
            if all(c.value is None for c in row):
                continue
            key, cell = row[0].value, row[3]
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"Row {row_number}: missing parameter key.")
            key = key.strip()
            if key in values:
                raise ValueError(f"Duplicate parameter: {key}")
            if cell.data_type == "f":
                raise ValueError(f"{key}: enter a value, not a formula.")
            values[key] = cell.value
        return name[1].value.strip(), validate(values)
    finally:
        wb.close()
