"""
app.py — Streamlit UI for the bulk marketing video generator.

Run with:  streamlit run app.py
"""

import io
import logging
import os
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st
from openpyxl import load_workbook

# Aliased: this module already binds `config` to the per-batch RenderConfig.
import config as settings
import ui_common
from jobs import store
from preview_editor import preview_editor
from ui_common import get_session_id
from workspace import MAX_PROMO_VIDEOS, Workspace, build_workspace, stage_uploads
from video_generator import (
    ALL_COLUMNS,
    CANVAS_H,
    CANVAS_W,
    DEFAULT_CTA_VIDEO_SLOTS,
    FPS_CHOICES,
    MAX_CTA_VIDEO_SLOTS,
    FONT_CHOICES,
    FONT_CUSTOM,
    REQUIRED_COLUMNS,
    TEXT_STYLES,
    RenderConfig,
    VideoGenerator,
    missing_optional_columns,
    validate_dataframe,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

# --------------------------------------------------------------------------- helpers

def make_generator(ws: Workspace, config: RenderConfig, output_dir: Path) -> VideoGenerator:
    config.font_path = str(ws.font_path) if ws.font_path else None
    generator = VideoGenerator(
        config=config,
        bg_dir=ws.bg_dir,
        video_path=ws.video_path,
        cta_path=ws.cta_path,
        work_dir=ws.work_dir,
        output_dir=output_dir,
        cta_video_slots=ws.cta_video_slots,
    )
    # Bad uploads caught at construction (e.g. an audio-only "video" clip that
    # would crash FFmpeg mid-render) — show them wherever a generator is built.
    for message in generator.input_warnings:
        st.warning(message)
    return generator


def apply_saved_edits(df: pd.DataFrame, edits: dict[int, dict]) -> pd.DataFrame:
    """Overlay values saved from the preview editor onto the uploaded sheet.
    Keys are 1-based data row numbers (1 = first row below the header)."""
    out = df.copy()
    for row_no, cols in edits.items():
        if not 1 <= int(row_no) <= len(out):
            continue
        for col, value in cols.items():
            # Empty optional columns parse as float64; pandas refuses to put
            # a string (e.g. a color hex) into them — widen to object first.
            if (
                isinstance(value, str)
                and col in out.columns
                and out[col].dtype != object
            ):
                out[col] = out[col].astype(object)
            out.loc[out.index[int(row_no) - 1], col] = value
    return out


def updated_excel_bytes(excel_bytes: bytes, edits: dict[int, dict]) -> bytes:
    """The original workbook with the saved values written into the first
    sheet — everything else (formatting, formulas elsewhere, extra sheets)
    is preserved. Columns the sheet doesn't have yet are appended after the
    last used column."""
    wb = load_workbook(io.BytesIO(excel_bytes))
    ws = wb.worksheets[0]
    headers = {
        str(cell.value).strip(): cell.column for cell in ws[1] if cell.value is not None
    }
    next_col = (max(headers.values()) + 1) if headers else 1
    for row_no, cols in sorted(edits.items()):
        for name, value in cols.items():
            col = headers.get(name)
            if col is None:
                col = next_col
                ws.cell(row=1, column=col, value=name)
                headers[name] = col
                next_col += 1
            ws.cell(row=int(row_no) + 1, column=col, value=value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- UI

ui_common.ensure_static_dir()
store.init_db()

st.set_page_config(page_title="Bulk Video Generator", page_icon="🎬", layout="wide")
st.title("🎬 Bulk Marketing Video Generator")
st.caption(
    "Upload an Excel sheet, a promo video, background images, and an optional CTA "
    "image — get one 1080x1920 (9:16) MP4 per row, ready for Reels, TikTok, and Shorts."
)

# ---- sidebar: layout & output configuration
with st.sidebar:
    st.header("⚙️ Settings")

    st.subheader("Layout")
    layout_mode = st.radio(
        "Layout mode",
        options=["free", "split"],
        format_func=lambda m: {"free": "Free (place each box)",
                               "split": "Split-screen (side-by-side)"}[m],
        help="Free = every box is positioned by its own coordinates. "
             "Split-screen = the main video fills one half and the side clips "
             "fill the other, ending when the main video ends.",
    )
    is_split = layout_mode == "split"
    if is_split:
        swap_sides = st.checkbox(
            "Swap left / right",
            value=False,
            help="Off = main video left, side clips right. On = flip them.",
        )
        split_panel_h = st.number_input(
            "Panel height (px)", 100, CANVAS_H, 960, 20,
            help="Height of the two side-by-side panels, centered vertically. "
                 f"Full height is {CANVAS_H}; a shorter band leaves space above "
                 "and below for text.",
        )
        crop_to_panels = st.checkbox(
            "No background — output only the panels",
            value=False,
            help="The finished video is exactly the two panels (1080 × panel "
                 "height) with no background at all. Texts and the CTA are drawn "
                 "ON TOP of the videos; auto-placed texts stay inside the band. "
                 "Off = the full 1080×1920 canvas with a background.",
        )
        st.caption(
            "Split-screen needs side clips uploaded below (otherwise the other "
            "half is just background). The side keeps drawing fresh random clips "
            "until the main video ends. Note: the main video is fitted inside its "
            "panel (letterboxed); side clips are cropped to fill theirs."
        )
    else:
        swap_sides = False
        split_panel_h = 960
        crop_to_panels = False
    bg_color = st.color_picker(
        "Background color", "#1E1B4B",
        help="Used wherever a row has no background image — e.g. when no "
             "background ZIP is uploaded (the ZIP is optional).",
    )

    st.subheader("Video placement")
    if is_split:
        st.caption("Ignored in split-screen mode — the main video fills its panel.")
    else:
        st.caption(
            "Defaults for every row — the Excel columns `Video_X`, `Video_Y`, "
            "`Video_Width`, `Video_Height` override them per video."
        )
    randomize_video = st.checkbox(
        "Randomize position per video",
        value=False,
        disabled=is_split,
        help="Each video gets its own random spot (avoiding the CTA and any "
             "explicitly positioned texts). Reproducible per row, so the "
             "preview matches the final render.",
    )
    if randomize_video or is_split:
        video_x, video_y = 0, 0  # ignored; positions are computed / from the panel
    else:
        video_x = st.number_input("Video X", 0, CANVAS_W, 90)
        video_y = st.number_input("Video Y", 0, CANVAS_H, 300)
    video_w = st.number_input("Video width", 50, CANVAS_W, 900, disabled=is_split)
    video_h = st.number_input("Video height", 50, CANVAS_H, 900, disabled=is_split)

    st.subheader("CTA image placement")
    st.caption(
        "Defaults for every row — the Excel columns `CTA_X`, `CTA_Y`, "
        "`CTA_Width`, `CTA_Height` override them per video."
    )
    cta_x = st.number_input("CTA X", 0, CANVAS_W, 340)
    cta_y = st.number_input("CTA Y", 0, CANVAS_H, 1600)
    cta_w = st.number_input("CTA width", 10, CANVAS_W, 400)
    cta_h = st.number_input("CTA height", 10, CANVAS_H, 160)
    cta_fade_start = st.number_input(
        "CTA fade-in start (s)", 0.0, 30.0, 1.0, 0.1,
        help="The CTA image is invisible until this time, then fades in. "
             "Per-row override: `CTA_Fade_Start`.",
    )
    cta_fade_duration = st.number_input(
        "CTA fade-in duration (s)", 0.0, 30.0, 0.5, 0.1,
        help="How long the fade-in takes. Per-row override: `CTA_Fade_Duration`.",
    )

    st.subheader("CTA video (optional)")
    cta_slot_count = st.number_input(
        "Number of clip slots", 1, MAX_CTA_VIDEO_SLOTS, DEFAULT_CTA_VIDEO_SLOTS, 1,
        help="How many clip positions play in fixed order. Each is a pool of "
             "sample videos; one is picked per output video. In split-screen "
             "mode the side keeps drawing fresh random clips after these to fill "
             "the whole main video.",
    )
    cta_slot_count = int(cta_slot_count)
    st.caption(
        f"A sequence of {cta_slot_count} clip"
        f"{'s' if cta_slot_count != 1 else ''} that always play in order in one "
        "shared box. Each clip is a *pool* of sample videos (up to ~30): one is "
        "chosen per output video — pinned by an Excel `CTA_Clip_<n>` cell, "
        "otherwise picked at random. Leave all empty to skip."
    )
    cta_video_slot_files = [
        st.file_uploader(
            f"Clip {i} — sample videos (MP4)", type=["mp4"],
            accept_multiple_files=True, key=f"cta_clip_{i}",
            help="One of these is chosen per output video for this position.",
        )
        for i in range(1, cta_slot_count + 1)
    ]
    if is_split:
        st.caption("Box position/size is set by the split-screen panel below.")
    cta_video_x = st.number_input("CTA video X", 0, CANVAS_W, 360, disabled=is_split)
    cta_video_y = st.number_input("CTA video Y", 0, CANVAS_H, 1200, disabled=is_split)
    cta_video_w = st.number_input("CTA video width", 50, CANVAS_W, 360, disabled=is_split)
    cta_video_h = st.number_input("CTA video height", 50, CANVAS_H, 360, disabled=is_split)
    if is_split:
        st.caption(
            "No fade in split-screen — the panel is visible from the first frame "
            "and stays to the end."
        )
    cta_video_fade_start = st.number_input(
        "CTA video fade-in start (s)", 0.0, 30.0, 0.5, 0.1, disabled=is_split,
        help="Per-row override: `CTA_Video_Fade_Start`. Ignored in split-screen.",
    )
    cta_video_fade_duration = st.number_input(
        "CTA video fade-in duration (s)", 0.0, 30.0, 0.5, 0.1, disabled=is_split,
        help="Per-row override: `CTA_Video_Fade_Duration`. Ignored in split-screen.",
    )
    st.caption(
        "Playback speed per clip position — 1 = normal, 2 = twice as fast, "
        "0.5 = half. Per-row overrides: `CTA_Video_Speed_<n>` for one clip, or "
        "`CTA_Video_Speed` for the whole row."
    )
    cta_video_speeds = [
        st.number_input(
            f"Clip {i} speed (×)", 0.25, 4.0, 1.0, 0.05, key=f"cta_speed_{i}",
        )
        for i in range(1, cta_slot_count + 1)
    ]
    cta_video_fill = st.checkbox(
        "Keep clips playing to fill the whole video",
        value=False,
        help="After the fixed clips above, keep drawing fresh random clips from "
             "the same pools until the side covers the full length of the main "
             "video — so it never freezes on a last frame. Off = play once, then "
             "hold the last frame. Split-screen mode turns this on automatically.",
    )

    st.subheader("Layer order (z-index)")
    st.caption(
        "Which element sits on top when they overlap — higher number = nearer "
        "the front. The background is always at the back. Applies to every video."
    )
    video_z = st.number_input(
        "Promo video", 1, 99, 1, 1, key="z_video",
        help="Stacking order of the promo video layer.",
    )
    cta_video_z = st.number_input(
        "CTA video", 1, 99, 2, 1, key="z_cta_video",
        help="Stacking order of the CTA video layer.",
    )
    cta_image_z = st.number_input(
        "CTA image", 1, 99, 3, 1, key="z_cta_image",
        help="Stacking order of the CTA image (button) layer.",
    )
    text_z = st.number_input(
        "Texts", 1, 99, 4, 1, key="z_text",
        help="Stacking order of the headline / subheading / footer texts.",
    )

    st.subheader("Text style")
    st.caption(
        "Defaults for every text — per-row Excel columns `*_Font`, `*_Style`, "
        "`*_Opacity` and `*_BgOpacity` override them, and `*_BgColor` adds a "
        "highlight box behind any text."
    )
    default_font = st.selectbox(
        "Default font", FONT_CHOICES, index=0,
        help="A bundled font family, the system font, or your uploaded font. "
             "Override per text with `Headline_Font` etc.",
    )
    default_style = st.selectbox(
        "Default artistic style", TEXT_STYLES, index=0,
        help="classic = plain · outline = contrasting border · shadow = drop "
             "shadow · neon = glow. Override per text with `Headline_Style` etc.",
    )
    text_opacity_pct = st.slider(
        "Text opacity (%)", 0, 100, 100,
        help="How solid every text is. Below 100 the video and background show "
             "through the letters (the outline, glow and shadow fade with them). "
             "Override per text with `Headline_Opacity` etc.",
    )
    text_bg_opacity_pct = st.slider(
        "Highlight box opacity (%)", 0, 100, 100,
        help="Same, for the `*_BgColor` box behind a text — a translucent box "
             "with solid text on top is the classic caption look. Override per "
             "text with `Headline_BgOpacity` etc.",
    )

    st.subheader("Subliminal text (experimental)")
    subliminal_targets = st.multiselect(
        "Apply to",
        options=["Headline", "Subheading", "Footer"],
        default=[],
        max_selections=2,
        help="Which texts get the effect (up to two at a time; empty = off): no "
             "single frame shows the whole text, but it cycles fast enough to "
             "read as whole in motion. Override per text with a "
             "`<Role>_Subliminal` cell (yes/no).",
    )
    subliminal_enabled = bool(subliminal_targets)
    if subliminal_enabled:
        subliminal_mode = st.radio(
            "Effect style",
            options=["hide", "show"],
            format_func=lambda m: {"hide": "Hide a slice (recommended)",
                                   "show": "Show only a slice"}[m],
            help="Hide a slice = every frame shows the whole text minus ~1/K of "
                 "the words, so it stays bright and solid. Show only a slice = "
                 "every frame shows ONLY ~1/K of the words; each word is lit just "
                 "1/K of the time, so it time-averages to about 1/K brightness "
                 "and looks faint. Raising fps shortens the cycle but does not "
                 "change that brightness ratio.",
        )
        subliminal_k = st.slider(
            "Frames per cycle (K)", 2, 8, 3,
            help="The number of distinct frames before the effect repeats "
                 "(K/fps seconds). Higher K = a longer, less obviously repeating "
                 "cycle. 3 is a good default.",
        )
        # Pattern + amount apply to the hide style only (show is always the even
        # 1/K comb).
        if subliminal_mode == "hide":
            subliminal_pattern = st.radio(
                "Hidden characters",
                options=["random", "ordered"],
                format_func=lambda p: {"random": "Random each cycle",
                                       "ordered": "Fixed pattern"}[p],
                help="Random = a different, evenly-spread set is hidden each "
                     "frame (seeded, so the preview still matches) and never "
                     "repeats the same comb. Fixed = the same characters blank "
                     "out in the same frames every cycle.",
            )
            subliminal_hide_pct = st.slider(
                "Hidden per frame (%)", 15, 70, 33,
                disabled=subliminal_pattern != "random",
                help="Random pattern only. ~33% (≈100/K) keeps every character "
                     "hidden exactly once per cycle and stays bright. Higher "
                     "hides more per frame, so the text looks fainter. The fixed "
                     "pattern always hides ~1/K.",
            )
        else:
            subliminal_pattern = "ordered"
            subliminal_hide_pct = 33
        subliminal_granularity = st.radio(
            "Split by", ["word", "char"], horizontal=True,
            help="Word = omit whole words (more readable). Char = omit letters "
                 "(auto-used when there aren't enough words for K).",
        )
        subliminal_all_intra = st.checkbox(
            "Preserve frame-by-frame (larger files)",
            value=True,
            help="Encode every frame independently so a frame-scrub of THIS file "
                 "never shows the whole text. Turning this off shrinks files but "
                 "lets the codec blur frames together.",
        )
    else:
        subliminal_mode = "hide"
        subliminal_pattern = "random"
        subliminal_hide_pct = 33
        subliminal_k = 3
        subliminal_granularity = "word"
        subliminal_all_intra = True

    st.subheader("Output")
    fps = st.select_slider(
        "Frame rate (fps)", options=list(FPS_CHOICES), value=FPS_CHOICES[0],
        help="60 halves the subliminal cycle length (K/fps seconds), so the "
             "effect blends more smoothly — at the cost of roughly double the "
             "frames to encode. Both survive upload to every major platform.",
    )
    crf = st.slider("Quality (CRF — lower = better/bigger)", 16, 28, 18)
    preset = st.select_slider(
        "Encoder speed",
        options=["veryfast", "faster", "fast", "medium", "slow"],
        value="medium",
        help="Faster presets render quicker but produce slightly larger files.",
    )
    # One render only keeps ~8-10 threads busy, so many-core machines need
    # several concurrent renders to saturate. Cap at min(16, cores) — enough for
    # a 32-core VM without letting a laptop launch 16 FFmpegs.
    max_workers = min(16, max(4, os.cpu_count() or 4))
    workers = st.slider(
        "Parallel renders", 1, max_workers, min(2, max_workers),
        help="Concurrent FFmpeg processes. 2 is a good default on office "
             "machines; on a many-core VM push this to ~1 per 3 cores "
             "(e.g. 10 on 32 cores) to keep the CPU fully busy.",
    )
    font_file = st.file_uploader(
        "Custom font (TTF/OTF, optional)", type=["ttf", "otf"],
        help=f"Upload your own font, then pick “{FONT_CUSTOM}” as the default "
             "font above (or set a `*_Font` cell to it) to use it.",
    )

config = RenderConfig(
    layout_mode=layout_mode, swap_sides=bool(swap_sides),
    split_panel_h=int(split_panel_h), crop_to_panels=bool(crop_to_panels),
    bg_color=bg_color,
    video_x=int(video_x), video_y=int(video_y),
    video_w=int(video_w), video_h=int(video_h),
    cta_x=int(cta_x), cta_y=int(cta_y),
    cta_w=int(cta_w), cta_h=int(cta_h),
    cta_fade_start=float(cta_fade_start), cta_fade_duration=float(cta_fade_duration),
    cta_video_x=int(cta_video_x), cta_video_y=int(cta_video_y),
    cta_video_w=int(cta_video_w), cta_video_h=int(cta_video_h),
    cta_video_fade_start=float(cta_video_fade_start),
    cta_video_fade_duration=float(cta_video_fade_duration),
    cta_video_speeds=[float(s) for s in cta_video_speeds],
    cta_video_fill=bool(cta_video_fill),
    video_z=int(video_z), cta_video_z=int(cta_video_z),
    cta_image_z=int(cta_image_z), text_z=int(text_z),
    default_font=default_font, default_style=default_style,
    text_opacity=text_opacity_pct / 100.0,
    text_bg_opacity=text_bg_opacity_pct / 100.0,
    subliminal_targets=list(subliminal_targets),
    subliminal_mode=subliminal_mode,
    subliminal_pattern=subliminal_pattern,
    subliminal_hide_pct=int(subliminal_hide_pct),
    subliminal_k=int(subliminal_k),
    subliminal_granularity=subliminal_granularity,
    subliminal_all_intra=bool(subliminal_all_intra),
    fps=int(fps), crf=int(crf), preset=preset,
    randomize_video_pos=randomize_video,
)

# ---- uploads
st.subheader("1. Upload assets")
col1, col2 = st.columns(2)
with col1:
    excel_file = st.file_uploader("Excel file (.xlsx)", type=["xlsx"])
    promo_files = st.file_uploader(
        f"Promo video(s) (MP4) — up to {MAX_PROMO_VIDEOS}", type=["mp4"],
        accept_multiple_files=True,
        help="Upload one to use it in every video. Upload several and a "
             "multi-batch render gives each batch its own promo video, cycling "
             "if there are fewer promos than batches.",
    )
    # Preview and Render Row work off a single promo — the first one.
    video_file = promo_files[0] if promo_files else None
with col2:
    zip_file = st.file_uploader(
        "Background images (ZIP, optional)", type=["zip"],
        help="Without a ZIP, videos render on the solid background color set in "
             "the sidebar (Layout section).",
    )
    cta_file = st.file_uploader("CTA image (PNG, optional)", type=["png"])

# ---- Excel validation & preview table
df = None
if excel_file is not None:
    try:
        df = pd.read_excel(io.BytesIO(excel_file.getvalue()), engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read the Excel file: {exc}")

    if df is not None:
        missing = validate_dataframe(df)
        if missing:
            st.error(
                "The Excel file is missing required columns: "
                + ", ".join(f"`{c}`" for c in missing)
            )
            with st.expander("Supported columns"):
                st.code(
                    "Required:\n  " + "\n  ".join(REQUIRED_COLUMNS)
                    + "\n\nOptional (defaults / auto-placement when absent):\n  "
                    + "\n  ".join(c for c in ALL_COLUMNS if c not in REQUIRED_COLUMNS)
                )
            df = None
        elif df.empty:
            st.error("The Excel file has no data rows.")
            df = None
        else:
            st.success(f"Excel loaded — {len(df)} video(s) to generate.")
            # Edits saved from the preview editor belong to one specific
            # sheet — drop them (and the stale preview) on a new upload.
            file_key = f"{excel_file.name}:{excel_file.size}"
            if st.session_state.get("excel_file_key") != file_key:
                st.session_state["excel_file_key"] = file_key
                for stale in ("row_edits", "preview_payload", "preview_nonce",
                              "preview_row", "preview_baseline_edits", "row_render"):
                    st.session_state.pop(stale, None)
            row_edits = st.session_state.get("row_edits") or {}
            if row_edits:
                df = apply_saved_edits(df, row_edits)
            absent = missing_optional_columns(df)
            if absent:
                st.caption(
                    "Columns not in this sheet (defaults / auto-placement will be used): "
                    + ", ".join(f"`{c}`" for c in absent)
                )
            with st.expander("Preview spreadsheet data"):
                st.dataframe(df, hide_index=True, width="stretch")

ready = df is not None and video_file is not None
if not ready:
    st.info(
        "Upload the Excel sheet and a promo video to enable preview and "
        "generation. The background ZIP and CTA image are optional — without "
        "backgrounds, videos render on the sidebar's background color."
    )

# ---- actions
st.subheader("2. Generate")
col_row, col_preview, col_render, col_generate = st.columns(
    [1, 1.5, 1.5, 1.8], vertical_alignment="bottom")
preview_row = col_row.number_input(
    "Row to preview", 1, len(df) if df is not None else 1, 1, disabled=not ready,
    help="Excel data row number (1 = the first row below the header).",
)
preview_clicked = col_preview.button("👁️ Preview Row", disabled=not ready, width="stretch")
render_row_clicked = col_render.button(
    "🎬 Render Row", disabled=not ready, width="stretch",
    help="Render this one row to a real MP4 — with your saved edits — and play "
         "it here. Slower than the static preview, but it's the actual video.",
)
generate_clicked = col_generate.button(
    "🚀 Generate All Videos", disabled=not ready, type="primary", width="stretch"
)

# ---- clip source: the whole point of the unified flow
st.subheader("2. CTA clips")


def _scrapes_with_clips() -> list[dict]:
    """Finished scrapes whose clips are still on this machine.

    These are already on the box that will render them — using them directly
    replaces downloading a ZIP, unzipping it, and uploading the same bytes back
    through the browser."""
    out = []
    for j in store.list_jobs(limit=40, kinds=[store.KIND_SCRAPE],
                             statuses=[store.STATUS_SUCCEEDED]):
        clips = store.job_dir(j["id"]) / "clips"
        n = len(list(clips.glob("*.mp4"))) if clips.is_dir() else 0
        if n:
            out.append({**j, "n_clips": n})
    return out


available = _scrapes_with_clips()
clip_source = st.radio(
    "Where do the clips come from?",
    options=["upload", "scrape_job", "scrape_now"],
    format_func=lambda m: {
        "upload": "Upload files in the sidebar (as before)",
        "scrape_job": f"Use a previous scrape already on this machine "
                      f"({len(available)} available)",
        "scrape_now": "Scrape a TikTok account now, as part of this run",
    }[m],
    horizontal=False,
)

clip_params: dict = {}
if clip_source == "scrape_job":
    if not available:
        st.warning(
            "No finished scrape has clips on this machine. Run one from the "
            "TikTok Scraper page, or pick another option. (Job folders are "
            f"kept for {settings.JOB_RETENTION_DAYS} days.)"
        )
    else:
        pick = st.selectbox(
            "Which scrape", options=[j["id"] for j in available],
            format_func=lambda i: next(
                f"{j['label']} — {j['n_clips']} clips" for j in available if j["id"] == i),
        )
        clip_params["clips_from_job"] = pick

elif clip_source == "scrape_now":
    clip_params["account"] = st.text_input(
        "TikTok account URL or @handle",
        placeholder="https://www.tiktok.com/@someaccount",
    )
    col_l, col_s, col_d = st.columns(3)
    clip_params["scrape_limit"] = col_l.number_input(
        "Max videos to scan", 10, 5000, 200, 10,
        help="Enumeration runs at roughly 0.26s per post, then downloads are "
             "rate-limited to about one per 1-2s.")
    clip_params["trim_start"] = col_s.number_input(
        "Trim start (s)", 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5)
    clip_params["trim_duration"] = col_d.number_input(
        "Trim length (s)", 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5)

if clip_source in ("scrape_job", "scrape_now"):
    st.caption(
        "The sidebar clip uploaders are ignored in this mode — clips are taken "
        "from the scrape instead."
    )
    col_st, col_ps = st.columns(2)
    clip_params["clip_strategy"] = col_st.selectbox(
        "Which clips to use",
        options=["top_views", "all"],
        format_func=lambda s: {"top_views": "Best by view count (recommended)",
                               "all": "All of them"}[s],
        help="Curation as a rule rather than a chore: the highest-performing "
             "clips fill the slots, and the run needs no attention. Clips are "
             "then shuffled into slots, so no slot systematically gets the "
             "best ones and no clip is used twice.",
    )
    clip_params["clips_per_slot"] = col_ps.number_input(
        "Clips per slot", 1, 50, 10, 1)
    clip_params["slots"] = int(cta_slot_count)

# ---- captions
st.subheader("3. Captions")
_pool = store.active_pool()
if _pool:
    st.caption(f"Active pool: **{_pool['combinations']:,} unique pairs** left to "
               f"draw from · theme *{_pool.get('theme')}*")
caption_mode = st.radio(
    "Captions",
    options=["existing", "generate"],
    format_func=lambda m: {
        "existing": ("Use the active caption pool" if _pool
                     else "No pool — name files from the sheet's Headline"),
        "generate": "Generate a fresh pool first (Gemini)",
    }[m],
    horizontal=True,
)
caption_params: dict = {}
if caption_mode == "generate":
    caption_params["generate_pool"] = True
    caption_params["force_new_pool"] = True
    caption_params["caption_theme"] = st.text_input(
        "Caption theme", value=settings.CAPTION_THEME,
        placeholder="e.g. satisfying ASMR clips promoting a skincare brand",
        help="The single biggest lever on caption quality — be specific about "
             "the product and the audience.",
    )
    col_cc, col_hh = st.columns(2)
    caption_params["caption_count"] = col_cc.number_input(
        "Captions", 50, 5000, 500, 50)
    caption_params["hashtag_count"] = col_hh.number_input(
        "Hashtag sets", 25, 2000, 100, 25)
    if not settings.gemini_configured():
        st.warning("Vertex AI isn't configured — this stage will be skipped and "
                   "files will fall back to Headline names.")

st.subheader("4. Generate")

# One sheet becomes `batches x rows` videos: the same rows rendered once per
# batch, each pass with a different promo video and different clip picks.
col_b, col_f = st.columns(2)
n_batches = col_b.number_input(
    "Batches to render", 1, 20, 1, 1, disabled=not ready,
    help="The sheet is rendered this many times. Each pass uses the next promo "
         "video and picks different sample clips, so the batches differ.",
)
n_folders = col_f.number_input(
    "Output folders", 1, 20, int(n_batches), 1, disabled=not ready,
    help="Finished videos are mixed evenly across this many Drive folders, so "
         "no folder is just one promo video. Usually the same as the batch count.",
)
if ready and df is not None:
    total_videos = len(df) * int(n_batches)
    promo_count = len(promo_files or [])
    note = (f"**{len(df):,} rows x {int(n_batches)} batches = "
            f"{total_videos:,} videos**, mixed across {int(n_folders)} folders, "
            f"each uploaded twice ({total_videos * 2:,} Drive files).")
    if promo_count and int(n_batches) > promo_count:
        note += (f" Only {promo_count} promo video(s) uploaded, so they cycle "
                 f"across the {int(n_batches)} batches.")
    st.caption(note)
    if total_videos > 3000:
        st.warning(
            f"{total_videos:,} videos is a long run — roughly "
            f"{total_videos * 2 / 3600:.1f}–{total_videos * 3 / 3600:.1f} hours "
            "of rendering. The VM must stay up for the whole job. It resumes "
            "if interrupted, but it won't finish until it's back."
        )

# Generation runs in the background worker, so the batch needs a name to find it
# by later and an address to report back to.
col_label, col_mail = st.columns(2)
batch_label = col_label.text_input(
    "Batch name (optional)", value="", disabled=not ready,
    placeholder="e.g. asmr-week32-set1",
    help="Shown on the Jobs page and in the notification email. Defaults to the "
         "Excel file name.",
)
notify_email = col_mail.text_input(
    "Notify email (optional)", value=", ".join(settings.MAIL_TO), disabled=not ready,
    placeholder="you@yourcompany.com",
    help="Emailed when the batch finishes — on success and on failure. Leave "
         "blank to use the server default.",
)
if not settings.mail_configured():
    st.caption(
        "⚠️ Email isn't configured on the server yet, so no notification will be "
        "sent — the Jobs page still shows live progress."
    )

if preview_clicked and ready:
    with st.spinner(f"Rendering preview of row {preview_row}…"):
        with tempfile.TemporaryDirectory(prefix="bvg_preview_") as tmp:
            try:
                ws = build_workspace(Path(tmp), video_file, zip_file, cta_file,
                                     font_file, cta_video_slot_files)
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch,
                # so the preview shows the row's actual background.
                df_preview, _ = generator.assign_backgrounds(df)
                payload = generator.build_editor_payload(
                    df_preview.iloc[int(preview_row) - 1], int(preview_row))
                st.session_state["preview_payload"] = payload
                st.session_state["preview_nonce"] = uuid.uuid4().hex
                st.session_state["preview_row"] = int(preview_row)
                # The payload was built WITH this row's saved edits applied,
                # so a later save reports changes relative to them — keep the
                # snapshot to merge against (and to detect reverts).
                st.session_state["preview_baseline_edits"] = dict(
                    (st.session_state.get("row_edits") or {}).get(int(preview_row), {})
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Preview failed")
                st.session_state.pop("preview_payload", None)
                st.error(f"Preview failed: {exc}")

if render_row_clicked and ready:
    st.session_state.pop("row_render", None)
    with st.spinner(
        f"Rendering row {preview_row} for real… (a full render — slower than "
        "the static preview)"
    ):
        with tempfile.TemporaryDirectory(prefix="bvg_rowrender_") as tmp:
            try:
                ws = build_workspace(Path(tmp), video_file, zip_file, cta_file,
                                     font_file, cta_video_slot_files)
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch, so
                # this row renders with its actual background. df already carries
                # the saved editor edits (apply_saved_edits above).
                df_render, bg_warnings = generator.assign_backgrounds(df)
                for message in bg_warnings:
                    st.warning(message)
                res = generator.render_row(
                    int(preview_row), df_render.iloc[int(preview_row) - 1])
                if res.ok:
                    # Read the bytes before the TemporaryDirectory vanishes.
                    st.session_state["row_render"] = {
                        "row": int(preview_row),
                        "name": res.filename,
                        "bytes": (Path(tmp) / "out" / res.filename).read_bytes(),
                        "warnings": list(res.warnings),
                    }
                else:
                    st.error(f"Row {preview_row} render failed: {res.error}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Row render failed")
                st.error(f"Row render failed: {exc}")

row_render = st.session_state.get("row_render")
if row_render and not generate_clicked:
    # A 9:16 video at full content width is enormous — keep the player in a
    # narrow column with the caption/warnings/buttons beside it.
    col_vid, col_side = st.columns([2, 3], vertical_alignment="top")
    with col_vid:
        st.video(row_render["bytes"])
    with col_side:
        st.caption(
            f"Rendered video of row {row_render['row']} — exactly what the "
            "batch would produce for this row, including your saved edits."
        )
        for message in row_render.get("warnings") or []:
            st.warning(f"Row {row_render['row']}: {message}")
        st.download_button(
            "⬇️ Download this video", data=row_render["bytes"],
            file_name=row_render["name"], mime="video/mp4",
        )
        if st.button("✖ Close row video"):
            st.session_state.pop("row_render", None)
            st.rerun()

if "preview_payload" in st.session_state and not generate_clicked:
    st.caption(
        f"Row {st.session_state.get('preview_row', 1)} preview (1080x1920) — drag the "
        "video, CTA, or texts to move them, drag the corner handle to resize, recolor "
        "texts, and add a background box; click “Save to Excel” in the panel to apply the "
        "changed values to that row for previews, generation, and the Excel download below."
    )
    saved = preview_editor(
        st.session_state["preview_payload"], st.session_state["preview_nonce"]
    )
    # The component echoes its last value on every rerun — only apply a save
    # that belongs to the current preview (nonce) and hasn't been seen (token).
    if (
        isinstance(saved, dict)
        and saved.get("nonce") == st.session_state.get("preview_nonce")
        and saved.get("token") != st.session_state.get("editor_save_token")
    ):
        st.session_state["editor_save_token"] = saved.get("token")
        merged = dict(st.session_state.get("preview_baseline_edits", {}))
        merged.update(saved.get("values") or {})
        edits = st.session_state.setdefault("row_edits", {})
        row_no = int(st.session_state["preview_row"])
        if merged:
            edits[row_no] = merged
        else:
            edits.pop(row_no, None)
        # Rerun so everything above (data preview, generation input) reflects
        # the just-saved values in this same interaction.
        st.rerun()

row_edits = st.session_state.get("row_edits") or {}
if row_edits and excel_file is not None and not generate_clicked:
    rows_txt = ", ".join(str(r) for r in sorted(row_edits))
    st.success(
        f"Saved edits for row(s) {rows_txt} — applied to previews and generation. "
        "Download the updated sheet to keep your Excel in sync."
    )
    col_dl, col_clear = st.columns([2, 1], vertical_alignment="center")
    col_dl.download_button(
        "⬇️ Download updated Excel",
        data=updated_excel_bytes(excel_file.getvalue(), row_edits),
        file_name=excel_file.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    if col_clear.button("🗑️ Discard saved edits"):
        st.session_state.pop("row_edits", None)
        st.rerun()

if generate_clicked and ready:
    # Submitting is a two-step dance: reserve an id, stage the uploads into its
    # folder, then insert the job row. Doing it the other way round would let
    # the worker claim a batch whose promo video is still being written.
    #
    # Everything below finishes in seconds — the rendering itself happens in the
    # worker process, so this browser tab can be closed immediately.
    try:
        job_id = store.new_job_id()
        store.make_job_dirs(job_id)
        assets = store.assets_dir(job_id)

        # All promo videos are staged; the runner picks one per batch.
        stage_uploads(assets, promo_files, zip_file, cta_file, font_file,
                      # Clips come from the scrape in chained mode; the pipeline
                      # materialises them into the same cta_slot_N folders.
                      None if clip_source != "upload" else cta_video_slot_files)

        # The sheet is written with any preview-editor edits baked in, so the
        # worker renders exactly what this page was showing. updated_excel_bytes
        # preserves the original workbook's formatting.
        (assets / "input.xlsx").write_bytes(
            updated_excel_bytes(excel_file.getvalue(),
                                st.session_state.get("row_edits") or {})
        )

        chained = clip_source != "upload" or caption_mode == "generate"
        store.create_job(
            kind=store.KIND_PIPELINE if chained else store.KIND_RENDER,
            params={
                "render_config": asdict(config),
                "workers": int(workers),
                "batches": int(n_batches),
                "folders": int(n_folders),
                "make_zip": True,
                "excel_name": excel_file.name,
                "clip_source": clip_source,
                **clip_params,
                **caption_params,
            },
            label=batch_label or Path(excel_file.name).stem,
            notify_email=notify_email.strip(),
            submitted_by=get_session_id(),
            # Items are registered by the runner, which knows the batch count.
            job_id=job_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job submission failed")
        st.error(f"Could not queue the batch: {exc}")
    else:
        logger.info("Queued render job %s (%d rows)", job_id, len(df))
        st.session_state["last_job_id"] = job_id
        st.success(
            f"**Queued — {len(df) * int(n_batches):,} videos** "
            f"({len(df):,} rows x {int(n_batches)} batches). "
            "You can close this tab now; "
            "rendering carries on in the background"
            + (f" and an email goes to {notify_email.strip()} when it's done."
               if notify_email.strip() else ".")
        )
        st.page_link("pages/1_Jobs.py", label="📋 Track progress on the Jobs page",
                     icon="➡️")

if st.session_state.get("last_job_id"):
    job = store.get_job(st.session_state["last_job_id"])
    if job and job["status"] in (store.STATUS_QUEUED, store.STATUS_RUNNING):
        st.info(
            f"Batch `{job['label'] or job['id']}` is {job['status']}"
            + (f" — {job['stage']}" if job.get("stage") else "")
            + ". Details are on the Jobs page."
        )
