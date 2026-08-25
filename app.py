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
import text_grids
import ui_common
from captions import naming as caption_naming
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
    MUSIC_MIN_SECONDS,
    SPLIT_AUDIO_CHUNKS,
    SPLIT_AUDIO_MAX_CHUNKS,
    SPLIT_AUDIO_MAX_SPREAD,
    SPLIT_AUDIO_SPREAD,
    FONT_CHOICES,
    FONT_CUSTOM,
    REQUIRED_COLUMNS,
    TEXT_FIT_MIN_SIZE,
    TEXT_ROLES,
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
        gif_paths=ws.gif_paths,
        bg_video_paths=ws.bg_video_paths,
        music_paths=ws.music_paths,
    )
    # Bad uploads caught at construction (e.g. an audio-only "video" clip that
    # would crash FFmpeg mid-render) — show them wherever a generator is built.
    for message in generator.input_warnings:
        st.warning(message)
    return generator


def hashtag_template_bytes() -> bytes:
    """A ready-to-edit hashtag workbook.

    Generated rather than shipped as a file: sample_assets/ is excluded from
    the container image, so a committed sample would exist in the repo and be
    missing on the VM — exactly where it is needed."""
    rows = [
        "#asmr #satisfying #fyp #viral #foryou",
        "#plushie #cozy #asmr #softtoy #foryou",
        "#relaxing #calm #sleep #asmr #unwind",
        "#oddlysatisfying #aesthetic #viral #fyp",
        "#tiktokmademebuyit #musthave #trending #fyp",
        "#selfcare #cozyvibes #comfort #foryou",
        "#asmrsounds #tingles #relax #fypage",
        "#giftideas #cute #plushies #trending",
    ]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"Hashtags": rows}).to_excel(
            writer, sheet_name="Hashtags", index=False)
    return buf.getvalue()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data(show_spinner=False)
def read_text_grid(data: bytes, role: str) -> text_grids.Grid:
    """Parse one per-promo text grid, cached on the uploaded bytes.

    Streamlit re-runs this whole script on every widget touch, and openpyxl
    costs tens of milliseconds per workbook — three of them on every slider
    drag is a page that feels broken."""
    return text_grids.read_grid(data, role)


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
    bg_video_files = st.file_uploader(
        "Background videos (MP4, optional)", type=["mp4"], key="bg_video_pool",
        accept_multiple_files=True,
        help="A pool of translucent overlay videos. They play one after "
             "another for the length of each video — the same treatment as the "
             "GIFs, with its own dwell floor below — dealt from a shuffled "
             "deck so every clip is used before any repeats. Drawn directly "
             "beneath the texts and over every layer at or below the texts' "
             "z-number, full-canvas unless the box below says otherwise. Can "
             "also come from a Drive folder — see 'Background video source' in "
             "the main panel. Note: it is composited on every frame, so expect "
             "roughly 1.5–2.5× the render time per video.",
    )
    # Not gated on the uploader: the pool may arrive from Drive instead.
    bg_video_opacity = st.slider(
        "Background video opacity (%)", 1, 50, 8,
        help="How visible the background videos are. Keep it low (≈8–15) so "
             "texts stay legible.",
    )
    bg_video_min_seconds = st.number_input(
        "Minimum seconds per background video", 1.0, 120.0, 10.0, 0.5,
        help="The dwell floor. Clips play one after another for the length of "
             "the video; one shorter than this repeats itself until it clears "
             "the floor, one already longer plays once, in full. Longer than "
             "the GIF floor by default — this layer is ambience, and a bed "
             "that changes every few seconds reads as flicker.",
    )
    st.caption(
        f"At ~{bg_video_min_seconds:g}s each, a 20-second video shows about "
        f"{max(1, int(20 // bg_video_min_seconds))} and a 60-second video about "
        f"{max(1, int(60 // bg_video_min_seconds))} of them, dealt from a "
        "shuffled deck so every clip is used once before any repeats."
    )
    with st.expander("Background video box (default: full canvas)"):
        bgv_c1, bgv_c2 = st.columns(2)
        bg_video_x = bgv_c1.number_input("BG video X", 0, CANVAS_W, 0, key="bgv_x")
        bg_video_y = bgv_c2.number_input("BG video Y", 0, CANVAS_H, 0, key="bgv_y")
        bg_video_w = bgv_c1.number_input("BG video width", 50, CANVAS_W,
                                         CANVAS_W, key="bgv_w")
        bg_video_h = bgv_c2.number_input("BG video height", 50, CANVAS_H,
                                         CANVAS_H, key="bgv_h")

    st.subheader("Audio")
    music_files = st.file_uploader(
        "Music tracks (optional)",
        type=["mp3", "wav", "m4a", "aac", "ogg", "opus", "flac"],
        key="music_pool", accept_multiple_files=True,
        help="A pool of music laid under the promo video's own sound. Tracks "
             "play one after another for the length of each video — the same "
             "treatment as the background videos, with its own dwell floor "
             "below — dealt from a shuffled deck so every track is used before "
             "any repeats. Can also come from a Drive folder: see 'Music "
             "source' in the main panel.",
    )
    # Not gated on the uploader: the pool may arrive from Drive instead.
    music_volume = st.slider(
        "Music volume (%)", 0, 100, 0,
        help="The music's share of the mix. The promo video's original audio "
             "takes the rest — 10% music leaves the original at 90%. 0 turns "
             "the layer off completely, and the audio comes out exactly as it "
             "did before this setting existed.",
    )
    if music_volume:
        st.caption(
            f"Music at **{music_volume}%**, the promo's own audio at "
            f"**{100 - music_volume}%**. A promo with no audio track of its own "
            "plays the music at full volume instead — there is nothing to mix "
            "it against.")
    music_min_seconds = st.number_input(
        "Minimum seconds per track", 1.0, 600.0, float(MUSIC_MIN_SECONDS), 5.0,
        help="The dwell floor. Tracks play one after another for the length of "
             "the video; one shorter than this repeats itself until it clears "
             "the floor, one already longer plays once. Long by default — a bed "
             "that swaps every few seconds reads as a fault rather than a mix.",
    )
    split_audio = st.checkbox(
        "Split the audio track (random speed)", value=False,
        help="Cuts the promo video's OWN audio into equal chunks and replays "
             "each at its own random speed, so it runs fast in places and slow "
             "in others — and still ends exactly with the picture. Sound and "
             "vision drift apart in the middle by design. Pitch is preserved, "
             "so voices do not go chipmunk. Every output video in the batch "
             "warps differently.",
    )
    if split_audio:
        split_audio_chunks = st.slider(
            "Chunks", 2, SPLIT_AUDIO_MAX_CHUNKS, int(SPLIT_AUDIO_CHUNKS),
            help="How many pieces the audio is cut into. More chunks = the "
                 "speed changes more often; the change is audible at each seam.",
        )
        split_audio_spread = st.slider(
            "Speed variation (%)", 5, int(SPLIT_AUDIO_MAX_SPREAD * 100),
            int(SPLIT_AUDIO_SPREAD * 100),
            help="How far each chunk strays from normal speed. 35% gives "
                 "roughly 0.74x-1.54x. Past about 50% the slow parts smear and "
                 "the fast parts gabble.",
        ) / 100.0
        st.caption(
            f"~{20 / max(1, split_audio_chunks):.1f}s per chunk on a 20-second "
            "video. The chunks always add back up to the full length, so the "
            "audio never runs short or long.")
    else:
        split_audio_chunks = SPLIT_AUDIO_CHUNKS
        split_audio_spread = SPLIT_AUDIO_SPREAD

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

    st.subheader("GIFs (optional)")
    st.caption(
        "Short looping clips (MP4) shown one after another in their own box — "
        "separate from the CTA clips above, with their own pool, box and "
        "layer order. Each gif holds the box for at least the dwell time "
        "below, repeating **itself** a whole number of times to get there: a "
        "3-second gif plays twice (6s), it is never cut short. The sequence "
        "keeps drawing fresh gifs until the promo video ends, so it never "
        "freezes on a stopped animation."
    )
    gif_files = st.file_uploader(
        "GIF clips (MP4)", type=["mp4"], accept_multiple_files=True,
        key="gif_pool",
        help="One flat pool, not slots — a random selection plays in each "
             "output video, every gif used once before any repeats. Ignored "
             "when the GIFs come from Drive instead (set below the sheet).",
    )
    gif_min_seconds = st.number_input(
        "Minimum seconds per gif", 1.0, 30.0, 5.0, 0.5,
        help="The dwell floor. A gif shorter than this repeats itself until it "
             "clears the floor; a gif already longer plays once, in full.",
    )
    st.caption(
        f"At ~{gif_min_seconds:g}s each, a 20-second promo shows about "
        f"{max(1, int(20 // gif_min_seconds))} gifs and a 60-second promo about "
        f"{max(1, int(60 // gif_min_seconds))} — however many you upload. The "
        "pool is the variety across videos, not within one."
    )
    st.caption(
        "The box is an invisible fit guide, not a visible panel. Every gif is "
        "scaled to the largest size that still sits inside it — bigger gifs "
        "shrink, smaller ones are enlarged — never cropped and never stretched, "
        "so the box sets the size whatever the source resolution. Whatever is "
        "behind shows through the space left over."
    )
    gif_x = st.number_input("GIF box X", 0, CANVAS_W, 60)
    gif_y = st.number_input("GIF box Y", 0, CANVAS_H, 560)
    gif_w = st.number_input("GIF box width", 50, CANVAS_W, 360)
    gif_h = st.number_input("GIF box height", 50, CANVAS_H, 360)
    gif_fade_start = st.number_input(
        "GIF fade-in start (s)", 0.0, 30.0, 0.0, 0.1,
        help="Per-row override: `GIF_Fade_Start`. 0 = visible from the first "
             "frame, which is also what the static preview shows.",
    )
    gif_fade_duration = st.number_input(
        "GIF fade-in duration (s)", 0.0, 30.0, 0.0, 0.1,
        help="Per-row override: `GIF_Fade_Duration`. Applies to the first gif "
             "only — the rest of the sequence cuts straight in.",
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
    gif_z = st.number_input(
        "GIFs", 1, 99, 2, 1, key="z_gif",
        help="Stacking order of the GIF layer. Below the CTA layers by "
             "default — it is decoration, so it yields to anything carrying a "
             "message. Raise it above the promo video's number to float a gif "
             "over the video instead of behind it.",
    )
    cta_video_z = st.number_input(
        "CTA video", 1, 99, 3, 1, key="z_cta_video",
        help="Stacking order of the CTA video layer.",
    )
    cta_image_z = st.number_input(
        "CTA image", 1, 99, 4, 1, key="z_cta_image",
        help="Stacking order of the CTA image (button) layer.",
    )
    text_z = st.number_input(
        "Texts", 1, 99, 5, 1, key="z_text",
        help="Stacking order of the headline / subheading / footer texts.",
    )
    st.caption(
        "The background videos have no number: they always sit directly "
        "beneath the texts, over any layer whose number is at or below the "
        "texts'. (A layer raised ABOVE the texts also rises above the veil.)"
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

    st.subheader("Text fit boxes (optional)")
    st.caption(
        "Give a text a fixed box and the box drives the type: the text re-wraps "
        "to the box's width and its font size is picked so the whole block — "
        "including any outline or glow — fills the box without spilling out. "
        "Line breaks are added **and removed** as the box changes, so long and "
        "short texts come out optically the same size across a batch."
    )
    st.caption(
        f"Both width and height are needed; **0 = off**, which leaves that text "
        f"behaving exactly as before (its `*_Size` cell, wrapped to the canvas). "
        f"The box is centred on the text's X/Y, so switching it on never moves "
        f"anything. Per-row overrides: `Headline_Width` / `Headline_Height`, and "
        f"the same for Subheading and Footer. Text that cannot fit even at "
        f"{TEXT_FIT_MIN_SIZE}px is drawn at {TEXT_FIT_MIN_SIZE}px and flagged on "
        "the row rather than shrunk into illegibility."
    )
    text_box_dims = {}
    for _role in TEXT_ROLES:
        _cw, _ch = st.columns(2)
        text_box_dims[_role] = (
            _cw.number_input(f"{_role} box width", 0, CANVAS_W, 0, 10,
                             key=f"tbox_w_{_role}"),
            _ch.number_input(f"{_role} box height", 0, CANVAS_H, 0, 10,
                             key=f"tbox_h_{_role}"),
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
    bg_video_opacity=float(bg_video_opacity) / 100.0,
    bg_video_x=int(bg_video_x), bg_video_y=int(bg_video_y),
    bg_video_w=int(bg_video_w), bg_video_h=int(bg_video_h),
    bg_video_min_seconds=float(bg_video_min_seconds),
    music_volume=float(music_volume) / 100.0,
    music_min_seconds=float(music_min_seconds),
    split_audio=bool(split_audio),
    split_audio_chunks=int(split_audio_chunks),
    split_audio_spread=float(split_audio_spread),
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
    gif_x=int(gif_x), gif_y=int(gif_y),
    gif_w=int(gif_w), gif_h=int(gif_h),
    gif_min_seconds=float(gif_min_seconds),
    gif_fade_start=float(gif_fade_start),
    gif_fade_duration=float(gif_fade_duration),
    video_z=int(video_z), gif_z=int(gif_z), cta_video_z=int(cta_video_z),
    cta_image_z=int(cta_image_z), text_z=int(text_z),
    default_font=default_font, default_style=default_style,
    headline_box_w=int(text_box_dims["Headline"][0]),
    headline_box_h=int(text_box_dims["Headline"][1]),
    subheading_box_w=int(text_box_dims["Subheading"][0]),
    subheading_box_h=int(text_box_dims["Subheading"][1]),
    footer_box_w=int(text_box_dims["Footer"][0]),
    footer_box_h=int(text_box_dims["Footer"][1]),
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

# ---- optional per-promo text
#
# One grid workbook per role, each column headed with a promo video's filename.
# Uploading one makes that role's text come from the grid instead of the sheet;
# everything else about the row — size, font, colour, position, background box
# — is untouched, which is the whole point. See text_grids.py.
promo_names = [f.name for f in (promo_files or [])]
sheet_rows = len(df) if df is not None else 0
grid_keys = {role: f"text_grid_{role}" for role in TEXT_ROLES}
grid_uploads: dict[str, object] = {}

with st.expander(
    "Per-promo heading, subheading and footer text (optional)",
    # Opened once something is in it, so a blocking error is never hidden
    # behind a collapsed panel.
    expanded=any(st.session_state.get(k) is not None for k in grid_keys.values()),
):
    st.caption(
        "Upload a sheet whose **column headers are your promo video filenames** "
        "and whose rows are the text for each row of the main Excel — so the "
        "same row can say something different on every promo. Row 1 here is "
        "row 1 there. A sheet you upload replaces **only** that text: size, "
        "font, colour, position and background box still come from the main "
        "Excel and the sidebar. Leave a cell blank to keep the main Excel's "
        "text for that one."
    )
    for column, role in zip(st.columns(len(TEXT_ROLES)), TEXT_ROLES):
        grid_uploads[role] = column.file_uploader(
            f"{role} by promo (.xlsx)", type=["xlsx"], key=grid_keys[role],
            help=f"One column per promo video, one row per row of the main "
                 f"Excel. Overrides the main sheet's `{role}` column.",
        )
    if promo_names and sheet_rows:
        st.caption("Templates below already carry the right headers and row "
                   "count — fill one in and upload it back.")
        for column, role in zip(st.columns(len(TEXT_ROLES)), TEXT_ROLES):
            column.download_button(
                f"⬇️ {role} template",
                data=text_grids.template_bytes(role, promo_names, sheet_rows),
                file_name=f"{role.lower()}_by_promo.xlsx", mime=XLSX_MIME,
                key=f"text_grid_template_{role}", width="stretch",
            )
    else:
        st.caption("Upload the main Excel and your promo video(s) first to get "
                   "templates with the headers already filled in.")

    grid_reports, grid_overrides, grid_errors = text_grids.check_uploads(
        grid_uploads, promo_names, sheet_rows, reader=read_text_grid)

    if any(u is not None for u in grid_uploads.values()):
        if not promo_names:
            st.info("Upload your promo video(s) — the column headers are "
                    "checked against their filenames.")
        checked = st.button(
            "🔍 Check promo names against these sheets", width="stretch",
            help="Shows which column of each sheet feeds which promo video.",
        )
        # Shown on demand, but forced whenever something is wrong: an error
        # here refuses the batch, so it cannot be behind a button nobody pressed.
        if (checked or grid_errors) and promo_names:
            matched = {role: {i: column for i, _n, column in report.pairs}
                       for role, report in grid_reports.items()}
            pairing = []
            for index, name in enumerate(promo_names):
                entry = {"#": index + 1, "Promo video": name}
                for role in TEXT_ROLES:
                    if role not in grid_reports:
                        entry[role] = "— not uploaded"
                        continue
                    column = matched[role].get(index)
                    entry[role] = (f"✅ column “{column}”" if column
                                   else "⚠️ falls back to the main Excel")
                pairing.append(entry)
            st.dataframe(pd.DataFrame(pairing), hide_index=True, width="stretch")
            if grid_reports:
                st.caption(" · ".join(
                    f"**{role}**: {r.grid_rows} row(s) vs the main Excel's "
                    f"{r.sheet_rows}, {r.matched}/{len(promo_names)} promo(s) "
                    "matched" for role, r in grid_reports.items()))
        for message in grid_errors:
            st.error(message)
        if checked or grid_errors:
            for report in grid_reports.values():
                for message in report.warnings:
                    st.warning(message)
        if checked and grid_overrides and not grid_errors:
            st.success(
                "Every column matches a promo video — "
                + ", ".join(f"{role} ✓" for role in sorted(grid_overrides))
                + ". These sheets will be used instead of the main Excel's "
                  "text.")

# A grid swap changes what every row says, so a preview rendered from the
# previous one is a picture of something that will never be generated. Saved
# editor edits are deliberately kept: they are geometry and colour, which a
# text change does not invalidate.
grid_key = "|".join(
    f"{role}:{u.name}:{u.size}" if u is not None else f"{role}:-"
    for role, u in sorted(grid_uploads.items())
)
if st.session_state.get("text_grid_key", grid_key) != grid_key:
    for stale in ("preview_payload", "preview_nonce", "preview_baseline_edits",
                  "row_render"):
        st.session_state.pop(stale, None)
st.session_state["text_grid_key"] = grid_key

ready = df is not None and video_file is not None
if not ready:
    st.info(
        "Upload the Excel sheet and a promo video to enable preview and "
        "generation. The background ZIP and CTA image are optional — without "
        "backgrounds, videos render on the sidebar's background color."
    )

# ---- actions
st.subheader("2. Preview a row")

# With several promos uploaded, Preview and Render Row always used the first
# one — so there was no way to see how a particular row looks against a
# particular promo. In the real batch every row is paired automatically and
# spread across all of them; this is for checking one pairing by eye.
if promo_files and len(promo_files) > 1:
    col_row, col_promo, col_preview, col_render = st.columns(
        [1, 1.6, 1.4, 1.4], vertical_alignment="bottom")
    promo_choice = col_promo.selectbox(
        "Promo video", options=list(range(len(promo_files))),
        format_func=lambda i: f"{i + 1}. {promo_files[i].name}",
        disabled=not ready,
        help="Which promo to pair with this row for the preview and the test "
             "render. The full batch pairs every row automatically, spread "
             "evenly across all of them.",
    )
    # Preview and Render Row below build their workspace from this one.
    video_file = promo_files[promo_choice]
else:
    promo_choice = 0
    col_row, col_preview, col_render = st.columns(
        [1, 1.5, 1.5], vertical_alignment="bottom")

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


# ---- clip source: the whole point of the unified flow
st.subheader("3. CTA clips")


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
    options=["upload", "drive_folder", "scrape_job", "scrape_now"],
    format_func=lambda m: {
        "upload": "Upload files in the sidebar (as before)",
        "drive_folder": "Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)",
        "scrape_job": f"Use a previous scrape already on this machine "
                      f"({len(available)} available)",
        "scrape_now": "Scrape a TikTok account now, as part of this run",
    }[m],
    horizontal=False,
)

clip_params: dict = {}
if clip_source == "drive_folder":
    st.caption(
        "The clips never touch your browser: the server fetches them straight "
        "from Drive when the batch runs, which is Google-to-Google rather than "
        "up your own connection. Share the folder with the service account "
        "first — read access is enough, and unlike the *upload* destination it "
        "can be an ordinary My Drive folder."
    )
    drive_clip_layout = st.radio(
        "Folder layout",
        options=["per_slot", "pooled"],
        format_func=lambda m: {
            "per_slot": "One folder per clip slot (same as the sidebar uploaders)",
            "pooled": "One folder for everything — deal the clips across the slots",
        }[m],
        horizontal=False,
        help="Per-slot keeps control of which clip plays where. Pooled is the "
             "unattended version: every clip in the folder is used, spread "
             "evenly across the slots and never used twice.",
    )
    clip_params["drive_clip_layout"] = drive_clip_layout

    if drive_clip_layout == "per_slot":
        clip_params["clips_drive_folders"] = [
            st.text_input(
                f"Clip {i} — Drive folder link", key=f"cta_clip_drive_{i}",
                placeholder="https://drive.google.com/drive/folders/…",
                help="The folder holding this position's sample videos. Leave "
                     "blank to skip this slot.",
            ).strip()
            for i in range(1, cta_slot_count + 1)
        ]
        _links_to_check = [(f"Clip {i}", link) for i, link
                           in enumerate(clip_params["clips_drive_folders"], start=1)
                           if link]
    else:
        clip_params["clips_drive_folder"] = st.text_input(
            "Drive folder link", key="cta_clips_drive_pooled",
            placeholder="https://drive.google.com/drive/folders/…",
            help="Every video in this folder — and its sub-folders — becomes "
                 "part of the pool.",
        ).strip()
        _links_to_check = ([("Clips", clip_params["clips_drive_folder"])]
                           if clip_params["clips_drive_folder"] else [])

    # Checking here costs one API call and saves finding out three minutes into
    # a multi-hour job that the folder was never shared.
    if st.button("🔍 Check the folder(s)", disabled=not _links_to_check):
        from integrations import drive as _drv

        with st.spinner("Reading Drive…"):
            for _label, _link in _links_to_check:
                _ok, _msg = _drv.check_source(_link)
                (st.success if _ok else st.error)(f"**{_label}** — {_msg}")
    elif not _links_to_check:
        st.caption("Paste a folder link to enable the check.")

elif clip_source == "scrape_job":
    if not available:
        st.warning(
            "No finished scrape has clips on this machine. Scrapes are "
            "removed from the server once their clips are safely in Google "
            "Drive, so this list only holds ones that have not been uploaded "
            "yet. To reuse clips that are already in Drive, choose "
            "**Google Drive folder** above and paste that scrape's folder "
            "link — no re-scraping needed."
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

if clip_source != "upload":
    st.caption(
        "The sidebar clip uploaders are ignored in this mode — clips come from "
        + ("Drive instead. The sidebar's *Number of clip slots* and the per-clip "
           "speeds still apply." if clip_source == "drive_folder"
           else "the scrape instead.")
    )
    # Both non-upload sources fill the same cta_slot_N folders, so the slot
    # count has to travel with the job.
    clip_params["slots"] = int(cta_slot_count)

if clip_source in ("scrape_job", "scrape_now"):
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

# ---- GIF source. Deliberately its OWN selector rather than reusing
# clip_source: the CTA options include "scrape a TikTok account", which has no
# meaning for a pool of gif loops, and the two layers are independent
# everywhere else too.
st.markdown("**GIFs**")
gif_source = st.radio(
    "Where do the GIFs come from?",
    options=["upload", "drive_folder"],
    format_func=lambda m: {
        "upload": "Upload them in the sidebar (as above)",
        "drive_folder": "Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)",
    }[m],
    horizontal=True,
    key="gif_source",
    help="Independent of where the CTA clips come from.",
)
if gif_source == "drive_folder":
    clip_params["gifs_drive_folder"] = st.text_input(
        "GIF folder — Drive link", key="gifs_drive",
        placeholder="https://drive.google.com/drive/folders/…",
        help="Every video in this folder — and its sub-folders — becomes part "
             "of the gif pool. Share it with the service account first.",
    ).strip()
    st.caption("The sidebar GIF uploader is ignored in this mode.")
    if st.button("🔍 Check the GIF folder",
                 disabled=not clip_params.get("gifs_drive_folder")):
        from integrations import drive as _drv

        with st.spinner("Reading Drive…"):
            _ok, _msg = _drv.check_source(clip_params["gifs_drive_folder"])
            (st.success if _ok else st.error)(f"**GIFs** — {_msg}")

# ---- Background-video source. Its own selector for the same reason the gifs
# have one: the pool is independent of both clip layers everywhere else.
st.markdown("**Background videos**")
bg_video_source = st.radio(
    "Where do the background videos come from?",
    options=["upload", "drive_folder"],
    format_func=lambda m: {
        "upload": "Upload them in the sidebar (as above)",
        "drive_folder": "Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)",
    }[m],
    horizontal=True,
    key="bg_video_source",
    help="Independent of where the CTA clips and gifs come from.",
)
if bg_video_source == "drive_folder":
    clip_params["bg_videos_drive_folder"] = st.text_input(
        "Background videos folder — Drive link", key="bg_videos_drive",
        placeholder="https://drive.google.com/drive/folders/…",
        help="Every video in this folder — and its sub-folders — joins the "
             "background pool. Share it with the service account first.",
    ).strip()
    st.caption("The sidebar background-video uploader is ignored in this mode.")
    if st.button("🔍 Check the background videos folder",
                 disabled=not clip_params.get("bg_videos_drive_folder")):
        from integrations import drive as _drv

        with st.spinner("Reading Drive…"):
            _ok, _msg = _drv.check_source(clip_params["bg_videos_drive_folder"])
            (st.success if _ok else st.error)(f"**Background videos** — {_msg}")

# ---- Music source. Its own selector again, and the only one that reads AUDIO
# files out of Drive — a folder of MP3s is invisible to the three above.
st.markdown("**Music**")
music_source = st.radio(
    "Where does the music come from?",
    options=["upload", "drive_folder"],
    format_func=lambda m: {
        "upload": "Upload it in the sidebar (as above)",
        "drive_folder": "Paste a Google Drive folder link — the server "
                        "downloads it itself (no upload)",
    }[m],
    horizontal=True,
    key="music_source",
    help="Independent of where the clips, gifs and background videos come from.",
)
if music_source == "drive_folder":
    clip_params["music_drive_folder"] = st.text_input(
        "Music folder — Drive link", key="music_drive",
        placeholder="https://drive.google.com/drive/folders/…",
        help="Every audio file in this folder — and its sub-folders — joins the "
             "music pool. Share it with the service account first.",
    ).strip()
    st.caption("The sidebar music uploader is ignored in this mode.")
    if st.button("🔍 Check the music folder",
                 disabled=not clip_params.get("music_drive_folder")):
        from integrations import drive as _drv

        with st.spinner("Reading Drive…"):
            _ok, _msg = _drv.check_source(clip_params["music_drive_folder"],
                                          kind="audio")
            (st.success if _ok else st.error)(f"**Music** — {_msg}")
if music_source == "upload" and not music_files and music_volume:
    st.caption("No tracks uploaded yet — the music layer stays off until there "
               "are some.")

# ---- captions
st.subheader("4. Captions")
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

fixed_tail = st.checkbox(
    "End every filename with a fixed call-to-action line",
    value=False, key="fixed_tail",
    help="One of the lines below is picked at random for each video and added "
         "after its caption. Hashtags are switched off while this is on — the "
         "name carries one ending, not two, and both would not fit inside the "
         "90-character cap.",
)
caption_params["fixed_tail"] = bool(fixed_tail)
if fixed_tail:
    st.caption("One of these, at random, per video:\n\n"
               + "\n".join(f"- {t}" for t in caption_naming.FIXED_TAILS))
    # A pool generated before the caption limit dropped holds captions too long
    # to sit beside the line, and they are cut at naming time. Measured against
    # the real pool rather than guessed, so the warning only appears when it
    # is actually true.
    _room = caption_naming.MAX_STEM - 1 - max(
        len(t) for t in caption_naming.FIXED_TAILS)
    _over = [c for c in ((_pool or {}).get("captions") or []) if len(c) > _room]
    if _over and caption_mode == "existing":
        st.warning(
            f"**{len(_over):,} of {len(_pool['captions']):,} captions in the "
            f"active pool are longer than {_room} characters** and will be cut "
            f"to fit beside the line (the line itself is never cut). This pool "
            f"predates the {settings.CAPTION_MAX_CHARS}-character limit — "
            f"generate a fresh one to avoid it."
        )

hashtag_source = st.radio(
    "Hashtags",
    options=["pool", "excel", "none"],
    format_func=lambda m: {
        "pool": "From the caption pool",
        "excel": "From an Excel file I upload",
        "none": "None — filenames are the caption only",
    }[m],
    horizontal=True,
    disabled=bool(fixed_tail),
    help="Hashtags are optional. Without them the short filename is just the "
         "caption, still capped at 90 characters.",
)
if fixed_tail:
    # Recorded as 'none' so nothing downstream reads a stale choice as live —
    # the fixed line has already replaced hashtags by the time naming runs.
    hashtag_source = "none"
caption_params["hashtag_source"] = hashtag_source
hashtag_file = None
if hashtag_source == "excel":
    hashtag_file = st.file_uploader(
        "Hashtag sets (.xlsx) — one set per row in the first column",
        type=["xlsx"],
        help="e.g. a column of rows like '#asmr #satisfying #fyp'. They are "
             "cycled across the batch, so the spread is even.",
    )
    col_t, col_p = st.columns([1, 2])
    with col_t:
        st.download_button(
            "⬇️ Sample file",
            data=hashtag_template_bytes(),
            file_name="hashtags_sample.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="A ready-to-edit workbook — replace the rows with your own.",
        )
    with col_p:
        if hashtag_file is None:
            st.warning(
                "**No file yet** — the caption pool's hashtags will be used "
                "instead. Upload one, or switch the setting above."
            )
        else:
            # Parsed here, not at render time, so a wrong column or an empty
            # sheet is obvious now rather than after a multi-hour job.
            try:
                from captions import naming
                preview = pd.read_excel(io.BytesIO(hashtag_file.getvalue()),
                                        engine="openpyxl")
                parsed = [" ".join(naming.parse_hashtags(v))
                          for v in preview.iloc[:, 0].tolist()]
                parsed = [p for p in parsed if p]
                if not parsed:
                    st.error(
                        "No hashtags found in the first column. Each row should "
                        "hold one set, e.g. `#asmr #satisfying #fyp`."
                    )
                else:
                    st.success(f"**{len(parsed)} hashtag set(s)** read from "
                               f"`{hashtag_file.name}`.")
                    st.caption("First few: " + " · ".join(parsed[:3]))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't read that file: {exc}")

if caption_mode == "generate":
    caption_params["generate_pool"] = True
    caption_params["force_new_pool"] = True
    caption_params["caption_theme"] = st.text_input(
        "Caption theme", value=settings.CAPTION_THEME,
        placeholder="e.g. satisfying ASMR clips promoting a skincare brand",
        help="The single biggest lever on caption quality — be specific about "
             "the product and the audience.",
    )
    # One caption per video, never reused, so this needs to reach
    # `batches x rows` — checked against the real total in section 5 below,
    # where the batch count is known.
    _cap_max = settings.CAPTION_POOL_MAX
    if hashtag_source == "pool":
        col_cc, col_hh = st.columns(2)
        caption_params["caption_count"] = col_cc.number_input(
            "Captions", 50, _cap_max, 500, 50)
        caption_params["hashtag_count"] = col_hh.number_input(
            "Hashtag sets", 25, settings.HASHTAG_POOL_MAX, 100, 25)
    else:
        # Generating hashtag sets that an uploaded sheet would immediately
        # override is money spent on output nobody sees.
        caption_params["caption_count"] = st.number_input(
            "Captions", 50, _cap_max, 500, 50)
        caption_params["hashtag_count"] = 0
        st.caption(
            "Only captions will be generated — hashtags come from "
            + ("your uploaded file." if hashtag_source == "excel"
               else "the fixed call-to-action line above." if fixed_tail
               else "nowhere, by choice.")
        )
    # Same arithmetic the Setup page shows. Worth repeating here because this
    # is where a large batch gets planned, and a big pool is neither instant
    # nor free — 16,000 captions is ~160 model calls.
    _cc = int(caption_params["caption_count"])
    st.caption(
        f"~{max(1, _cc // 100)} model call(s), run "
        f"{settings.CAPTION_CONCURRENCY} at a time — roughly "
        f"${_cc / 2000 * 2:.2f} of {settings.GEMINI_POOL_MODEL} usage."
    )
    if not caption_params["caption_theme"].strip():
        st.error(
            "A caption theme is required to generate a pool — it is what the "
            "captions are about. Enter one, or switch to “Use the active "
            "caption pool”."
        )
    if not settings.gemini_configured():
        st.warning("Vertex AI isn't configured — this stage will be skipped and "
                   "files will fall back to Headline names.")

st.subheader("5. Generate")

# One sheet becomes `batches x rows` videos: the same rows rendered once per
# batch, each pass with a different promo video and different clip picks.
#
# Never below MAX_PROMO_VIDEOS: the batch count's DEFAULT is however many
# promos were uploaded, and Streamlit raises rather than clamps when a default
# lands above max_value — so a promo cap above this limit would take the whole
# page down on the upload that crossed it, not merely refuse the extra passes.
MAX_PASSES = max(20, MAX_PROMO_VIDEOS)

col_b, col_f = st.columns(2)
# Default to one pass per promo, which is the pairing people expect: every row
# rendered once with every promo.
_n_promos = max(1, len(promo_files or []))
n_batches = col_b.number_input(
    "Batches to render", 1, MAX_PASSES, _n_promos, 1, disabled=not ready,
    help="How many times the sheet is rendered. Each pass uses the next promo "
         "video and picks different sample clips. Set this to the number of "
         "promos and every row is rendered once with every promo.",
)
if _n_promos > 1 and int(n_batches) < _n_promos:
    st.warning(
        f"Only {int(n_batches)} pass(es) but {_n_promos} promo videos — promos "
        f"{int(n_batches) + 1}–{_n_promos} would never be used. Set batches to "
        f"{_n_promos} to render every row with every promo."
    )
n_folders = col_f.number_input(
    "Output folders", 1, MAX_PASSES, int(n_batches), 1, disabled=not ready,
    help="Finished videos are mixed evenly across this many Drive folders, so "
         "no folder is just one promo video. Usually the same as the batch count.",
)
_PLATFORM_CHOICES = {
    "yt,tk": "Both — yt.zip and tk.zip",
    "tk": "TikTok only — tk.zip (long names)",
    "yt": "YouTube only — yt.zip (short names)",
}
upload_platforms = st.selectbox(
    "Publish which names?", list(_PLATFORM_CHOICES),
    format_func=lambda key: _PLATFORM_CHOICES[key], disabled=not ready,
    help="Google allows one account 750 GB per rolling 24 hours into Drive, "
         "and each video is published under two names — so a very large batch "
         "cannot send both in one day. Pick one now and run this job again "
         "tomorrow with the other: the videos stay on the VM until both have "
         "been published, and the second run skips what already landed.",
)

if ready and df is not None:
    total_videos = len(df) * int(n_batches)
    promo_count = len(promo_files or [])
    _n_names = len(upload_platforms.split(","))
    note = (f"**{len(df):,} rows x {int(n_batches)} passes = "
            f"{total_videos:,} videos**, mixed across {int(n_folders)} folders, "
            + (("each published twice under the SAME name — the fixed "
                "call-to-action leaves the two forms identical, so one "
                "platform's set is enough unless you want both folders."
                if fixed_tail else "each published under both names.")
               if _n_names == 2 else
               f"published as `{upload_platforms}.zip` only."))
    if promo_count > 1 and int(n_batches) == promo_count:
        note += (f" Every row is rendered once with each of the {promo_count} "
                 "promos — all pairings, no repeats.")
    elif promo_count and int(n_batches) > promo_count:
        note += (f" Only {promo_count} promo video(s), so they cycle across the "
                 f"{int(n_batches)} passes — each pairing occurs "
                 f"{int(n_batches) // promo_count}x.")
    st.caption(note)

    # Captions are the other quantity that has to reach `batches x rows`: one
    # per video, never reused. Without this check the shortfall only surfaces
    # inside the worker, after the assets are staged and the batch queued —
    # and the number needed depends on the batch count set right here.
    if caption_mode == "generate":
        _planned = int(caption_params.get("caption_count") or 0)
        if _planned < total_videos:
            st.warning(
                f"**{_planned:,} captions for {total_videos:,} videos.** Each "
                "video takes its own and none is ever reused, so this batch "
                "would be refused before it rendered. Raise *Captions* in "
                f"section 4 to at least {total_videos:,}."
            )
    elif _pool:
        _unused, _total_caps = store.pool_remaining(_pool["id"])
        if _unused < total_videos:
            st.warning(
                f"**The active pool has {_unused:,} captions left of "
                f"{_total_caps:,}, but this batch needs {total_videos:,}.** "
                "A pool is a consumable and never starts over — generate a "
                "fresh one, or render fewer batches."
            )

    # Google allows one account 750 GB per rolling 24 hours into Drive, and
    # server-side copies count as well as uploads. At ~31 MB a video (measured
    # over a 16,000-video night) that is ~12,000 videos a day under both names,
    # or ~24,000 under one. Worth saying HERE, where the choice is still cheap,
    # rather than as a 403 eight hours into the upload.
    _EST_GB = total_videos * _n_names * 31 / 1024
    if _EST_GB > 750:
        _fits = int(750 * 1024 / (31 * _n_names))
        st.warning(
            f"**About {_EST_GB:,.0f} GB into Drive — over the 750 GB that one "
            f"account may upload per rolling 24 hours.** The last "
            f"{total_videos - _fits:,} video(s) would fail with a rate-limit "
            "error and need requeueing tomorrow."
            + ("  \nPublishing **one** set of names instead would fit "
               f"({_EST_GB / 2:,.0f} GB) — run this job again tomorrow for the "
               "other." if _n_names == 2 else
               "  \nRender fewer batches, or split this across two days.")
        )

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
drive_folder = st.text_input(
    "Google Drive folder link (optional)", value="", disabled=not ready,
    placeholder="https://drive.google.com/drive/folders/…",
    help="Paste a folder link to send THIS batch somewhere specific. Leave "
         "blank to use the server's configured destination. It must sit inside "
         "a Shared Drive — a service account cannot write to a personal My Drive.",
)
if drive_folder.strip():
    from integrations import drive as _drv
    _did = _drv.extract_id(drive_folder)
    st.caption(f"→ uploading this batch to `{_did}`"
               + ("" if _drv.looks_like_shared_drive(_did)
                  else " (a folder — fine if it lives in a Shared Drive)"))

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

generate_clicked = st.button(
    "🚀 Generate All Videos", disabled=not ready, type="primary", width="stretch"
)

if preview_clicked and ready:
    with st.spinner(f"Rendering preview of row {preview_row}…"):
        with tempfile.TemporaryDirectory(prefix="bvg_preview_") as tmp:
            try:
                # The bg-video pool IS staged here: the editor payload frames
                # the row's chosen clip so the box is draggable in the preview.
                ws = build_workspace(Path(tmp), video_file, zip_file, cta_file,
                                     font_file, cta_video_slot_files, gif_files,
                                     bg_video_files, music_files)
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch,
                # so the preview shows the row's actual background — and the
                # same per-promo text, or the editor would offer no Headline at
                # all for a row whose only Headline comes from a grid.
                df_preview, _ = generator.assign_backgrounds(
                    text_grids.apply_overrides(df, grid_overrides, promo_choice))
                payload = generator.build_editor_payload(
                    df_preview.iloc[int(preview_row) - 1], int(preview_row))
                st.session_state["preview_payload"] = payload
                st.session_state["preview_nonce"] = uuid.uuid4().hex
                st.session_state["preview_row"] = int(preview_row)
                st.session_state["preview_promo"] = (
                    video_file.name if promo_files and len(promo_files) > 1 else "")
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
                                     font_file, cta_video_slot_files, gif_files,
                                     bg_video_files, music_files)
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch, so
                # this row renders with its actual background. df already carries
                # the saved editor edits (apply_saved_edits above), and the grids
                # are applied for the selected promo so this really is the
                # pairing the batch would produce.
                df_render, bg_warnings = generator.assign_backgrounds(
                    text_grids.apply_overrides(df, grid_overrides, promo_choice))
                for message in bg_warnings:
                    st.warning(message)
                res = generator.render_row(
                    int(preview_row), df_render.iloc[int(preview_row) - 1])
                if res.ok:
                    # Read the bytes before the TemporaryDirectory vanishes.
                    st.session_state["row_render"] = {
                        "row": int(preview_row),
                        "promo": (video_file.name if promo_files
                                  and len(promo_files) > 1 else ""),
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
            f"Rendered video of row {row_render['row']}"
            + (f" using promo **{row_render['promo']}**" if row_render.get("promo") else "")
            + " — exactly what the batch would produce for this pairing, "
              "including your saved edits."
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
        (f"Promo: **{st.session_state.get('preview_promo')}** · " if
         st.session_state.get("preview_promo") else "")
        + f"Row {st.session_state.get('preview_row', 1)} preview (1080x1920) — drag the "
        "video, CTA, or texts to move them, drag the corner handle to resize, recolor "
        "texts, and add a background box; click “Save to Excel” in the panel to apply the "
        "changed values to that row for previews, generation, and the Excel download below."
    )
    if grid_overrides:
        st.caption(
            "The "
            + ", ".join(f"**{role}**" for role in sorted(grid_overrides))
            + " wording above is this promo's, from your per-promo sheet. What "
              "you change here — position, size, colour, background box — is "
              "saved against the row and so applies to every promo, which is "
              "the point: one design, different words."
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

if generate_clicked and ready and caption_mode == "generate"         and not caption_params.get("caption_theme", "").strip():
    st.error("Not queued — set a caption theme first, or choose “Use the "
             "active caption pool”.")
    generate_clicked = False

if generate_clicked and ready and clip_source == "drive_folder" and not any(
    clip_params.get("clips_drive_folders")
    or [clip_params.get("clips_drive_folder")]
):
    # Queuing a job whose clips have nowhere to come from only moves the
    # failure into the worker, minutes later and on another page.
    st.error("Not queued — paste at least one Google Drive folder link for the "
             "clips, or choose a different clip source.")
    generate_clicked = False

# Same deferred-failure rule for the other two Drive-sourced pools: a blank
# link would run the (potentially long) earlier pipeline stages and then die.
if generate_clicked and ready and gif_source == "drive_folder" \
        and not clip_params.get("gifs_drive_folder"):
    st.error("Not queued — paste the Google Drive folder link for the GIFs, "
             "or switch the GIF source back to upload.")
    generate_clicked = False

if generate_clicked and ready and bg_video_source == "drive_folder" \
        and not clip_params.get("bg_videos_drive_folder"):
    st.error("Not queued — paste the Google Drive folder link for the "
             "background videos, or switch their source back to upload.")
    generate_clicked = False

if generate_clicked and ready and music_source == "drive_folder" \
        and not clip_params.get("music_drive_folder"):
    st.error("Not queued — paste the Google Drive folder link for the music, "
             "or switch its source back to upload.")
    generate_clicked = False

if generate_clicked and ready and grid_errors:
    # A grid whose headers do not resolve would render the wrong promo's words
    # onto thousands of videos and look entirely successful doing it. There is
    # no safe way to guess past it, so the batch does not start.
    st.error(
        f"Not queued — {len(grid_errors)} problem(s) with the per-promo text "
        "sheet(s). Fix them in the “Per-promo heading, subheading and footer "
        "text” panel above, or remove the sheets to use the main Excel's text."
    )
    generate_clicked = False

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
                      None if clip_source != "upload" else cta_video_slot_files,
                      # Gated on the GIF layer's OWN source, never on
                      # clip_source: a user who fetches CTA clips from Drive but
                      # uploads their gifs would otherwise lose every one of
                      # them silently — nothing downstream checks for gifs, so
                      # the batch would render clean and gif-free.
                      None if gif_source != "upload" else gif_files,
                      # Same rule as the gifs: gated on this layer's OWN
                      # source flag, never on anyone else's.
                      None if bg_video_source != "upload" else bg_video_files,
                      # And again for the music, which is the only pool read out
                      # of Drive as AUDIO — see check_source(kind="audio").
                      None if music_source != "upload" else music_files)

        # The sheet is written with any preview-editor edits baked in, so the
        # worker renders exactly what this page was showing. updated_excel_bytes
        # preserves the original workbook's formatting.
        if hashtag_file is not None:
            (assets / "hashtags.xlsx").write_bytes(hashtag_file.getvalue())

        # Resolved to promo INDICES here, not staged as sheets: stage_uploads
        # renames every promo to input_N.mp4, so the filenames the columns were
        # matched on do not exist by the time the worker runs. Writes nothing
        # when no grids were uploaded.
        text_grids.write_overrides(assets, grid_overrides)

        (assets / "input.xlsx").write_bytes(
            updated_excel_bytes(excel_file.getvalue(),
                                st.session_state.get("row_edits") or {})
        )

        # Anything that has to be fetched or generated before rendering makes
        # this a pipeline job rather than a plain render — including a gif pool
        # that still has to come down from Drive.
        chained = (clip_source != "upload" or gif_source != "upload"
                   or bg_video_source != "upload"
                   or music_source != "upload"
                   or caption_mode == "generate")
        store.create_job(
            kind=store.KIND_PIPELINE if chained else store.KIND_RENDER,
            params={
                "render_config": asdict(config),
                "workers": int(workers),
                "batches": int(n_batches),
                "folders": int(n_folders),
                "make_zip": True,
                "upload_platforms": upload_platforms.split(","),
                "excel_name": excel_file.name,
                # Recorded because nothing else keeps them: the promos are
                # staged as input.mp4 / input_N.mp4, so this list is the only
                # way to read a job's per-promo text back against real names.
                "promo_names": promo_names,
                "text_grids": sorted(grid_overrides),
                "clip_source": clip_source,
                "gif_source": gif_source,
                "bg_video_source": bg_video_source,
                "music_source": music_source,
                "drive_folder": drive_folder.strip(),
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
