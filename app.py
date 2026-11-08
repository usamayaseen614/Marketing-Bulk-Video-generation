"""
app.py — Streamlit UI for the bulk marketing video generator.

Run with:  streamlit run app.py
"""

import io
import logging
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st

from i18n import t, localize_format, language_header, option_labels, stage_label
from openpyxl import load_workbook

import batching
# Aliased: this module already binds `config` to the per-batch RenderConfig.
import config as settings
import text_grids
import ui_common
from speech import synth as speech_synth
from captions import naming as caption_naming
import presets
import presets_ui
import parameter_presets_ui
import generation_defaults as defaults
import upload_progress
from jobs import store
from preview_editor import preview_editor
from presets_ui import keep
from ui_common import get_session_id
from workspace import (MAX_PROMO_VIDEOS, Workspace, build_workspace,
                       stage_uploads, staged_bytes)
from video_generator import (
    ALL_COLUMNS,
    CANVAS_H,
    CANVAS_W,
    DEFAULT_CTA_VIDEO_SLOTS,
    FPS_CHOICES,
    MAX_CTA_VIDEO_SLOTS,
    MUSIC_MIN_SECONDS,
    PROMO_ALT_SECONDS,
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
        promo_alt_paths=ws.promo_alt_paths,
        # The preview and the single-row render are interactive: one row, driven
        # by a click, so this one is allowed to synthesize on the spot rather
        # than read a cache some earlier stage filled. The batch does the
        # opposite (a pre-render stage, lookup only) because sixteen parallel
        # renders must not each load a PyTorch model.
        #
        # The cache lives outside any job folder so a second preview of the same
        # row is instant, and so it survives the job purge. It is
        # content-addressed, so sharing it across jobs is safe.
        voice_cache_dir=settings.JOBS_ROOT / "_voice",
        voice_allow_synthesis=True,
    )
    # Bad uploads caught at construction (e.g. an audio-only "video" clip that
    # would crash FFmpeg mid-render) — show them wherever a generator is built.
    for message in generator.input_warnings:
        st.warning(t(message))
    return generator


def sample_script(prompt: str) -> str:
    """One Gemini script from `prompt`, kept until the prompt changes.

    Session state, not a fresh call per click: every drag in the preview editor
    is a rerun, and Preview and Render row should narrate the same words. A
    failure is shown and not cached, so the next click tries again."""
    cached = st.session_state.get("sample_script") or {}
    if cached.get("prompt") == prompt and cached.get("script"):
        return cached["script"]
    from captions.pool import generate_scripts
    try:
        with st.spinner(t("Writing a sample script with Gemini…")):
            script = (generate_scripts(prompt, 1) or [""])[0]
    except Exception as exc:  # noqa: BLE001
        st.warning(t("Couldn't write a sample script ({0}) — this row has no narration here.", exc))
        return ""
    st.session_state["sample_script"] = {"prompt": prompt, "script": script}
    return script


def apply_script_pool(frame, row_no: int, script_file, script_prompt: str,
                      config: RenderConfig):
    """Deal scripts into the frame the way the batch's voice stage will.

    The preview exists to show what the batch would produce, so a row whose
    Voiceover comes from the pool has to be narrated here too — otherwise
    previewing it shows an empty caption band and the batch shows a full one.
    Idempotent and non-destructive (a cell the sheet already filled always
    wins), so running it here and again in the stage changes nothing.

    Generated scripts do not exist until the job runs, so a sample from the
    same prompt stands in — and only when row `row_no` would take one, so
    previewing a row with its own Voiceover costs no Gemini call."""
    if not config.voice_enabled:
        return frame
    from speech.pool import (SCREEN_TEXT_COLUMN, VOICEOVER_COLUMN, _blank,
                             apply_to_frame, parse_scripts)
    scripts = []
    if script_file is not None:
        scripts = parse_scripts(script_file.getvalue(), script_file.name)
    elif script_prompt.strip():
        row = frame.iloc[row_no - 1]
        if _blank(row.get(VOICEOVER_COLUMN)) and _blank(row.get(SCREEN_TEXT_COLUMN)):
            scripts = [s for s in [sample_script(script_prompt.strip())] if s]
    filled, _ = apply_to_frame(frame, scripts, config.voice_set)
    return filled


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

st.set_page_config(page_title=t("Bulk Video Generator"), page_icon="🎬", layout="wide")
language_header()

st.title(t("🎬 Bulk Marketing Video Generator"))
st.caption(
    t("Upload an Excel sheet, a promo video, background images, and an optional CTA "
    "image — get one 1080x1920 (9:16) MP4 per row, ready for Reels, TikTok, and Shorts.")
)
# Before any widget: keep() registers this run's fields into a fresh list, and
# the panel's Load/Clear callbacks have already rewritten session state.
presets_ui.start_run()
upload_progress.install()
presets_ui.panel()

# ---- sidebar: layout & output configuration
with st.sidebar:
    st.header(t("⚙️ Settings"))
    parameter_presets_ui.panel()

    st.subheader(t("Layout"))
    layout_mode = st.radio(
        t("Layout mode"),
        options=["free", "split"],
        format_func=option_labels(["free", "split"], lambda m: {"free": t("Free (place each box)"),
                               "split": t("Split-screen (side-by-side)")}[m]),
        help=t("Free = every box is positioned by its own coordinates. "
             "Split-screen = the main video fills one half and the side clips "
             "fill the other, ending when the main video ends."),
        key=keep("layout_mode"),
    )
    is_split = layout_mode == "split"
    if is_split:
        swap_sides = st.checkbox(
            t("Swap left / right"),
            value=False, key=keep("swap_sides"),
            help=t("Off = main video left, side clips right. On = flip them."),
        )
        split_panel_h = st.number_input(
            t("Panel height (px)"), 100, CANVAS_H, 960, 20, key=keep("split_panel_h"),
            help=t("Height of the two side-by-side panels, centered vertically. Full height is "
                   "{0}; a shorter band leaves space above and below for text.", CANVAS_H),
        )
        crop_to_panels = st.checkbox(
            t("No background — output only the panels"),
            value=False, key=keep("crop_to_panels"),
            help=t("The finished video is exactly the two panels (1080 × panel "
                 "height) with no background at all. Texts and the CTA are drawn "
                 "ON TOP of the videos; auto-placed texts stay inside the band. "
                 "Off = the full 1080×1920 canvas with a background."),
        )
        st.caption(
            t("Split-screen needs side clips uploaded below (otherwise the other "
            "half is just background). The side keeps drawing fresh random clips "
            "until the main video ends. Note: the main video is fitted inside its "
            "panel (letterboxed); side clips are cropped to fill theirs.")
        )
    else:
        swap_sides = False
        split_panel_h = 960
        crop_to_panels = False
    bg_color = st.color_picker(
        t("Background color"), "#1E1B4B", key=keep("bg_color"),
        help=t("Used wherever a row has no background image — e.g. when no "
             "background ZIP is uploaded (the ZIP is optional)."),
    )
    bg_video_files = st.file_uploader(
        t("Background videos (MP4, optional)"), type=["mp4"],
        on_change=presets_ui.upload_changed, args=("bg_video_pool",),
        key=presets_ui.uploader_key("bg_video_pool"),
        accept_multiple_files=True,
        help=t("A pool of translucent overlay videos. They play one after "
             "another for the length of each video — the same treatment as the "
             "GIFs, with its own dwell floor below — dealt from a shuffled "
             "deck so every clip is used before any repeats. Drawn directly "
             "beneath the texts and over every layer at or below the texts' "
             "z-number, full-canvas unless the box below says otherwise. Can "
             "also come from a Drive folder — see 'Background video source' in "
             "the main panel. Note: it is composited on every frame, so expect "
             "roughly 1.5–2.5× the render time per video."),
    )
    bg_video_files = presets_ui.with_saved("bg_video_pool", bg_video_files,
                                           multiple=True)
    # Not gated on the uploader: the pool may arrive from Drive instead.
    bg_video_opacity = st.slider(
        t("Background video opacity (%)"), 1, 50, defaults.BG_VIDEO_OPACITY, key=keep("bg_video_opacity"),
        help=t("How visible the background videos are. Keep it low (≈8–15) so "
             "texts stay legible."),
    )
    bg_video_min_seconds = st.number_input(
        t("Minimum seconds per background video"), 1.0, 120.0, defaults.BG_VIDEO_MIN_SECONDS, 0.5,
        key=keep("bg_video_min_seconds"),
        help=t("The dwell floor. Clips play one after another for the length of "
             "the video; one shorter than this repeats itself until it clears "
             "the floor, one already longer plays once, in full. Longer than "
             "the GIF floor by default — this layer is ambience, and a bed "
             "that changes every few seconds reads as flicker."),
    )
    st.caption(
        t("At ~{0:g}s each, a 20-second video shows about {1} and a 60-second video about {2} of"
          " them, dealt from a shuffled deck so every clip is used once before any repeats.", bg_video_min_seconds, max(1, int(20 // bg_video_min_seconds)), max(1, int(60 // bg_video_min_seconds)))
    )
    with st.expander(t("Background video box (default: full canvas)")):
        bgv_c1, bgv_c2 = st.columns(2)
        bg_video_x = bgv_c1.number_input(t("BG video X"), 0, CANVAS_W, 0, key=keep("bgv_x"))
        bg_video_y = bgv_c2.number_input(t("BG video Y"), 0, CANVAS_H, 0, key=keep("bgv_y"))
        bg_video_w = bgv_c1.number_input(t("BG video width"), 50, CANVAS_W,
                                         CANVAS_W, key=keep("bgv_w"))
        bg_video_h = bgv_c2.number_input(t("BG video height"), 50, CANVAS_H,
                                         CANVAS_H, key=keep("bgv_h"))

    st.subheader(t("Audio"))
    music_files = st.file_uploader(
        t("Music tracks (optional)"),
        type=["mp3", "wav", "m4a", "aac", "ogg", "opus", "flac"],
        on_change=presets_ui.upload_changed, args=("music_pool",),
        key=presets_ui.uploader_key("music_pool"), accept_multiple_files=True,
        help=t("A pool of music laid under the promo video's own sound. Tracks "
             "play one after another for the length of each video — the same "
             "treatment as the background videos, with its own dwell floor "
             "below — dealt from a shuffled deck so every track is used before "
             "any repeats. Can also come from a Drive folder: see 'Music "
             "source' in the main panel."),
    )
    music_files = presets_ui.with_saved("music_pool", music_files, multiple=True)
    # Not gated on the uploader: the pool may arrive from Drive instead.
    music_volume = st.slider(
        t("Music volume (%)"), 0, 100, defaults.MUSIC_VOLUME, key=keep("music_volume"),
        help=t("The music's share of the mix. The promo video's original audio "
             "takes the rest — 10% music leaves the original at 90%. 0 turns "
             "the layer off completely, and the audio comes out exactly as it "
             "did before this setting existed."),
    )
    if music_volume:
        st.caption(
            t("Music at **{0}%**, the promo's own audio at **{1}%**. A promo with no audio track"
              " of its own plays the music at full volume instead — there is nothing to mix it "
              "against.", music_volume, 100 - music_volume))
    music_min_seconds = st.number_input(
        t("Minimum seconds per track"), 1.0, 600.0, float(MUSIC_MIN_SECONDS), 5.0,
        key=keep("music_min_seconds"),
        help=t("The dwell floor. Tracks play one after another for the length of "
             "the video; one shorter than this repeats itself until it clears "
             "the floor, one already longer plays once. Long by default — a bed "
             "that swaps every few seconds reads as a fault rather than a mix."),
    )
    split_audio = st.checkbox(
        t("Split the audio track (random speed)"), value=defaults.SPLIT_AUDIO,
        key=keep("split_audio"),
        help=t("Cuts the promo video's OWN audio into equal chunks and replays "
             "each at its own random speed, so it runs fast in places and slow "
             "in others — and still ends exactly with the picture. Sound and "
             "vision drift apart in the middle by design. Pitch is preserved, "
             "so voices do not go chipmunk. Every output video in the batch "
             "warps differently."),
    )
    if split_audio:
        split_audio_chunks = st.slider(
            t("Chunks"), 2, SPLIT_AUDIO_MAX_CHUNKS, int(SPLIT_AUDIO_CHUNKS),
            key=keep("split_audio_chunks"),
            help=t("How many pieces the audio is cut into. More chunks = the "
                 "speed changes more often; the change is audible at each seam."),
        )
        split_audio_spread = st.slider(
            t("Speed variation (%)"), 5, int(SPLIT_AUDIO_MAX_SPREAD * 100),
            int(SPLIT_AUDIO_SPREAD * 100), key=keep("split_audio_spread"),
            help=t("How far each chunk strays from normal speed. 35% keeps "
                 "every chunk between 0.74x and 1.54x — the band is a "
                 "guarantee, not an average. Past about 50% the slow parts "
                 "smear and the fast parts gabble."),
        ) / 100.0
        st.caption(
            t("~{0:.1f}s per chunk on a 20-second video. The chunks always add back up to the "
              "full length, so the audio never runs short or long.", 20 / max(1, split_audio_chunks)))
    else:
        split_audio_chunks = SPLIT_AUDIO_CHUNKS
        split_audio_spread = SPLIT_AUDIO_SPREAD

    st.subheader(t("Voiceover & captions"))
    # Asked for every engine, not just the selected one, so the dropdown can say
    # which are actually installed here instead of only reporting on the one
    # already chosen. available() caches per engine, so this costs one import
    # attempt apiece for the life of the process.
    _engine_status = {key: speech_synth.available(key)
                      for key in speech_synth.ENGINES}
    _engine_keys = list(speech_synth.ENGINES)
    _default_engine = speech_synth.normalize_engine(settings.VOICE_ENGINE)
    voice_engine = st.selectbox(
        t("Speech engine"), _engine_keys,
        index=_engine_keys.index(_default_engine), key=keep("voice_engine"),
        format_func=option_labels(_engine_keys, lambda key: t(speech_synth.ENGINES[key]) + (
            "" if _engine_status[key][0] else t(" — not installed here"))),
        help=t("Which model reads the scripts. Kokoro takes a speaking speed and "
             "an English accent; Pocket TTS (Kyutai) is smaller, installs on "
             "newer Pythons, and has no speed control. The two share no voice "
             "names, so the list below changes with this — and so does the "
             "meaning of a row's `Voiceover_Voice` cell."),
    )
    _voice_ok, _voice_why = _engine_status[voice_engine]
    # Every one of these has to exist whether or not the feature is usable —
    # RenderConfig below reads them unconditionally.
    voice_set = list(settings.VOICE_SET)
    voice_speed = settings.VOICE_SPEED
    voice_lead_in = settings.VOICE_LEAD_IN
    voice_gain = settings.VOICE_GAIN
    voice_duck = settings.VOICE_DUCK
    voice_duck_ratio = settings.VOICE_DUCK_RATIO
    voice_loop_promo = settings.VOICE_LOOP_PROMO
    voice_mute_promo = settings.VOICE_MUTE_PROMO
    beat_max_words = settings.BEAT_MAX_WORDS
    screen_text_y = settings.SCREEN_TEXT_Y
    voice_enabled = False

    if _voice_ok:
        voice_enabled = st.checkbox(
            t("Speak the script and caption it on screen"), value=False,
            key=keep("voice_enabled"),
            help=t("Reads each row's `Voiceover` text aloud and puts the words on "
                 "screen as they are spoken, a few at a time. The timing comes "
                 "from the speech itself, so nothing has to be lined up by "
                 "hand. Leave a row's `Voiceover` cell blank to pull from the "
                 "script pool below; use `Screen_Text` to show different words "
                 "than the ones being said."),
        )
    else:
        # This engine is not here, but the CAPTION half needs no engine at all —
        # the words are in the sheet. Offering it anyway is what lets the
        # timed-caption layer be built and previewed on a machine that cannot
        # run the model (Kokoro needs Python < 3.13), instead of the whole
        # feature looking broken.
        st.caption(t("Not available on this machine — {0}", t(_voice_why)))
        if any(ok for ok, _ in _engine_status.values()):
            st.caption(t("Another engine in the dropdown above IS installed here."))
        voice_enabled = st.checkbox(
            t("Show timed captions (no narration here)"), value=False,
            key=keep("voice_enabled"),
            help=t("Puts each row's `Voiceover` (or `Screen_Text`) on screen a few "
                 "words at a time, paced across the video. Identical to the full "
                 "feature minus the voice — install the engine, or deploy, and "
                 "the same sheet starts speaking and syncs the captions to it."),
        )
    if voice_enabled and _voice_ok:
        _engine_voices = speech_synth.voices(voice_engine)
        # The saved VOICE_SET is one engine's names and the two lists share
        # none, so it cannot be handed to the other engine's multiselect —
        # Streamlit raises when a default is not among the options. Keep
        # whatever still fits and fall back to this engine's own default.
        _kept = [v for v in settings.VOICE_SET if v in _engine_voices]
        voice_set = st.multiselect(
            t("Voices"), _engine_voices,
            default=_kept or [speech_synth.default_voice(voice_engine)],
            # Per engine: the two share no voice names, and a key shared
            # between them would carry one engine's picks into the other's list.
            key=keep(f"voice_set_{voice_engine}"),
            help=t("Rows rotate through these, so one posting folder is not all "
                 "the same narrator. A row's `Voiceover_Voice` cell wins.")
                 + (t(" af_/am_ are American, bf_/bm_ British; *f_ female, "
                    "*m_ male.") if voice_engine == speech_synth.KOKORO else ""),
         format_func=option_labels(_engine_voices, localize_format(str))) or [speech_synth.default_voice(voice_engine)]
        _can_speed = speech_synth.supports_speed(voice_engine)
        voice_speed = st.slider(
            # Shown at 1.0 when the engine has no rate control, because that is
            # what the batch will actually use — a disabled slider still reading
            # the saved 1.15 while RenderConfig carries 1.0 is the quiet kind of
            # wrong. Switching back to Kokoro restores the saved value.
            t("Speaking speed"), 0.7, 1.4,
            float(settings.VOICE_SPEED) if _can_speed else 1.0, 0.05,
            disabled=not _can_speed,
            # Its own unsaved key while disabled, so switching back to an
            # engine with speed control brings the chosen speed back with it.
            key=keep("voice_speed") if _can_speed else "voice_speed_fixed",
            help=t("1.0 is the model's natural pace. A row's `Voiceover_Speed` "
                 "cell overrides it.") if _can_speed else
                 t("This engine has no speaking-speed control, so neither this "
                 "nor a row's `Voiceover_Speed` cell changes the pace. Rows "
                 "that set one say so in their warnings."),
        )
        if not _can_speed:
            voice_speed = 1.0
            st.caption(
                t("{0} has no speed control — narration plays at the model's own pace.", speech_synth.ENGINES[voice_engine]))
        beat_max_words = st.slider(
            t("Words per caption"), 1, 8, int(settings.BEAT_MAX_WORDS),
            key=keep("beat_max_words"),
            help=t("How many words sit on screen at once. Three or four is the "
                 "usual short-form look; higher reads more like a subtitle."),
        )
        screen_text_y = st.slider(
            t("Caption height (px from top)"), 200, CANVAS_H - 200,
            int(settings.SCREEN_TEXT_Y), 10, key=keep("screen_text_y"),
            help=t("Where the caption band sits. Fixed rather than randomised "
                 "per row — a caption that moved between videos in a folder "
                 "would read as a fault. `Screen_Text_X`/`Screen_Text_Y` cells "
                 "override it."),
        )
        voice_lead_in = st.number_input(
            t("Silence before the first word (s)"), 0.0, 5.0,
            float(settings.VOICE_LEAD_IN), 0.1, key=keep("voice_lead_in"),
            help=t("A moment of promo before the narration starts. Reads far "
                 "better than a voice that begins on frame one."),
        )
        voice_duck = st.checkbox(
            t("Duck the other audio under the voice"), value=bool(settings.VOICE_DUCK),
            key=keep("voice_duck"),
            help=t("Drops the promo's own sound and the music bed while the voice "
                 "is talking, and lets them back up between phrases. Off mixes "
                 "everything at fixed levels instead."),
        )
        if voice_duck:
            voice_duck_ratio = st.slider(
                t("Ducking strength"), 2.0, 20.0,
                float(settings.VOICE_DUCK_RATIO), 0.5, key=keep("voice_duck_ratio"),
                help=t("How hard the bed is pushed down under the voice. 8 is a "
                     "normal voiceover duck; higher makes the narration the "
                     "only thing you hear."),
            )
        voice_gain = st.slider(
            t("Voice level"), 0.3, 1.0, float(settings.VOICE_GAIN), 0.05,
            key=keep("voice_gain"),
            help=t("Kept below 1.0 on purpose: the promo and music already fill "
                 "the mix between them, so a voice at full scale would clip on "
                 "loud material."),
        )
        voice_mute_promo = st.checkbox(
            t("Mute the promo's own audio while narrating"),
            value=bool(settings.VOICE_MUTE_PROMO), key=keep("voice_mute_promo"),
            help=t("The script is the message once there is one, so the promo's "
                 "own soundtrack is dropped on any row that narrates. Music "
                 "then becomes the whole background and plays at full volume "
                 "(the Music volume slider is a promo/music split, and there "
                 "is nothing left to split against) — ducking still pulls it "
                 "down under the voice. A row with no narration keeps the "
                 "promo's audio either way."),
        )
        voice_loop_promo = st.checkbox(
            t("The script sets the video length"), value=bool(settings.VOICE_LOOP_PROMO),
            key=keep("voice_loop_promo"),
            help=t("A narrated row lasts exactly as long as its script: a longer "
                 "script loops the promo and the CTA clips until it finishes, "
                 "a shorter one ends the video on its last word. Off instead "
                 "makes the promo the length — a long script is cut where the "
                 "promo ends, and the row is warned. A row with no narration "
                 "is the promo's length either way."),
        )
        if bool(presets_ui.value("promo_alt")):
            st.caption(t("Promo Alternate clips have no length of their own, so "
                       "the script always sets it in that mode."))
    elif voice_enabled:
        # Captions without a voice: only the controls that still mean something.
        beat_max_words = st.slider(
            t("Words per caption"), 1, 8, int(settings.BEAT_MAX_WORDS),
            key=keep("beat_max_words"),
            help=t("How many words sit on screen at once. Three or four is the "
                 "usual short-form look; higher reads more like a subtitle."),
        )
        screen_text_y = st.slider(
            t("Caption height (px from top)"), 200, CANVAS_H - 200,
            int(settings.SCREEN_TEXT_Y), 10, key=keep("screen_text_y"),
            help=t("Where the caption band sits. Fixed rather than randomised per "
                 "row. `Screen_Text_X`/`Screen_Text_Y` cells override it."),
        )

    st.subheader(t("Video placement"))
    if is_split:
        st.caption(t("Ignored in split-screen mode — the main video fills its panel."))
    else:
        st.caption(
            t("Defaults for every row — the Excel columns `Video_X`, `Video_Y`, "
            "`Video_Width`, `Video_Height` override them per video.")
        )
    randomize_video = st.checkbox(
        t("Randomize position per video"),
        value=False, key=keep("randomize_video"),
        disabled=is_split,
        help=t("Each video gets its own random spot (avoiding the CTA and any "
             "explicitly positioned texts). Reproducible per row, so the "
             "preview matches the final render."),
    )
    if randomize_video or is_split:
        video_x, video_y = 0, 0  # ignored; positions are computed / from the panel
    else:
        video_x = st.number_input(t("Video X"), 0, CANVAS_W, 90, key=keep("video_x"))
        video_y = st.number_input(t("Video Y"), 0, CANVAS_H, 300, key=keep("video_y"))
    video_w = st.number_input(t("Video width"), 50, CANVAS_W, 900, disabled=is_split,
                              key=keep("video_w"))
    video_h = st.number_input(t("Video height"), 50, CANVAS_H, 900, disabled=is_split,
                              key=keep("video_h"))

    st.subheader(t("CTA image placement"))
    st.caption(
        t("Defaults for every row — the Excel columns `CTA_X`, `CTA_Y`, "
        "`CTA_Width`, `CTA_Height` override them per video.")
    )
    cta_x = st.number_input(t("CTA X"), 0, CANVAS_W, 340, key=keep("cta_x"))
    cta_y = st.number_input(t("CTA Y"), 0, CANVAS_H, 1600, key=keep("cta_y"))
    cta_w = st.number_input(t("CTA width"), 10, CANVAS_W, 400, key=keep("cta_w"))
    cta_h = st.number_input(t("CTA height"), 10, CANVAS_H, 160, key=keep("cta_h"))
    cta_fade_start = st.number_input(
        t("CTA fade-in start (s)"), 0.0, 30.0, 1.0, 0.1, key=keep("cta_fade_start"),
        help=t("The CTA image is invisible until this time, then fades in. "
             "Per-row override: `CTA_Fade_Start`."),
    )
    cta_fade_duration = st.number_input(
        t("CTA fade-in duration (s)"), 0.0, 30.0, 0.5, 0.1,
        key=keep("cta_fade_duration"),
        help=t("How long the fade-in takes. Per-row override: `CTA_Fade_Duration`."),
    )

    st.subheader(t("CTA video (optional)"))
    cta_slot_count = st.number_input(
        t("Number of clip slots"), 1, MAX_CTA_VIDEO_SLOTS, DEFAULT_CTA_VIDEO_SLOTS, 1,
        key=keep("cta_slot_count"),
        help=t("How many clip positions play in fixed order. Each is a pool of "
             "sample videos; one is picked per output video. In split-screen "
             "mode the side keeps drawing fresh random clips after these to fill "
             "the whole main video."),
    )
    cta_slot_count = int(cta_slot_count)
    st.caption(
        t("A sequence of {0} clip{1} that always play in order in one shared box. Each clip is a"
          " *pool* of sample videos (up to ~30): one is chosen per output video — pinned by an "
          "Excel `CTA_Clip_<n>` cell, otherwise picked at random. Leave all empty to skip.", cta_slot_count, t('s' if cta_slot_count != 1 else ''))
    )
    cta_video_slot_files = [
        presets_ui.with_saved(f"cta_clip_{i}", st.file_uploader(
            t("Clip {0} — sample videos (MP4)", i), type=["mp4"],
            accept_multiple_files=True,
            on_change=presets_ui.upload_changed, args=(f"cta_clip_{i}",),
            key=presets_ui.uploader_key(f"cta_clip_{i}"),
            help=t("One of these is chosen per output video for this position."),
        ), multiple=True)
        for i in range(1, cta_slot_count + 1)
    ]
    if is_split:
        st.caption(t("Box position/size is set by the split-screen panel below."))
    cta_video_x = st.number_input(t("CTA video X"), 0, CANVAS_W, 360, disabled=is_split,
                                  key=keep("cta_video_x"))
    cta_video_y = st.number_input(t("CTA video Y"), 0, CANVAS_H, 1200, disabled=is_split,
                                  key=keep("cta_video_y"))
    cta_video_w = st.number_input(t("CTA video width"), 50, CANVAS_W, 360,
                                  disabled=is_split, key=keep("cta_video_w"))
    cta_video_h = st.number_input(t("CTA video height"), 50, CANVAS_H, 360,
                                  disabled=is_split, key=keep("cta_video_h"))
    if is_split:
        st.caption(
            t("No fade in split-screen — the panel is visible from the first frame "
            "and stays to the end.")
        )
    cta_video_fade_start = st.number_input(
        t("CTA video fade-in start (s)"), 0.0, 30.0, 0.5, 0.1, disabled=is_split,
        key=keep("cta_video_fade_start"),
        help=t("Per-row override: `CTA_Video_Fade_Start`. Ignored in split-screen."),
    )
    cta_video_fade_duration = st.number_input(
        t("CTA video fade-in duration (s)"), 0.0, 30.0, 0.5, 0.1, disabled=is_split,
        key=keep("cta_video_fade_duration"),
        help=t("Per-row override: `CTA_Video_Fade_Duration`. Ignored in split-screen."),
    )
    st.caption(
        t("Playback speed per clip position — 1 = normal, 2 = twice as fast, "
        "0.5 = half. Per-row overrides: `CTA_Video_Speed_<n>` for one clip, or "
        "`CTA_Video_Speed` for the whole row.")
    )
    cta_video_speeds = [
        st.number_input(
            t("Clip {0} speed (×)", i), 0.25, 4.0, 1.0, 0.05, key=keep(f"cta_speed_{i}"),
        )
        for i in range(1, cta_slot_count + 1)
    ]
    cta_video_fill = st.checkbox(
        t("Keep clips playing to fill the whole video"),
        value=False, key=keep("cta_video_fill"),
        help=t("After the fixed clips above, keep drawing fresh random clips from "
             "the same pools until the side covers the full length of the main "
             "video — so it never freezes on a last frame. Off = play once, then "
             "hold the last frame. Split-screen mode turns this on automatically."),
    )
    cta_after_promo = st.checkbox(
        t("Play CTA clips after the promo"),
        value=False, key=keep("cta_after_promo"), disabled=is_split,
        help=t("The promo plays first with the CTA box empty. When it ends, the "
               "clips above play once each, in order, in the CTA box, and the "
               "video runs until the last one finishes — each with its own sound. "
               "The fade-in and 'Keep clips playing' settings are ignored. Not "
               "available in split-screen."),
    )
    # An expander rather than `if cta_after_promo:` so the caption upload is
    # not thrown away when the box is unticked (see Promo Alternate below).
    with st.expander(t("CTA clips after the promo — captions"),
                     expanded=bool(cta_after_promo) and not is_split):
        cta_after_show_texts = st.checkbox(
            t("Show Headline / Subheading / Footer during the CTA clips"),
            value=True, key=keep("cta_after_show_texts"),
            help=t("Off: only the CTA caption is on screen once the promo ends."),
        )
        cta_caption_file = st.file_uploader(
            t("Caption pool (.xlsx / .csv / .txt)"), type=["xlsx", "csv", "txt"],
            key=presets_ui.uploader_key("cta_captions"),
            help=t("Excel/CSV: captions in the first column, under a header row. "
                   "Text: one caption per line. Each CTA clip shows one caption "
                   "picked at random from this pool (repeats allowed). Put a `|` "
                   "in a caption to break the line there."),
        )
        cta_caption_file = presets_ui.with_saved("cta_captions", cta_caption_file,
                                                 multiple=False)
        cta_captions: list[str] = []
        if cta_caption_file is not None:
            from speech.pool import parse_scripts as _parse_captions
            cta_captions = _parse_captions(cta_caption_file.getvalue(),
                                           cta_caption_file.name)
            if cta_captions:
                st.caption(t("**{0}** caption(s) — first one: “{1}”",
                             len(cta_captions), cta_captions[0][:60]))
            else:
                st.warning(t("No captions found in `{0}`.", cta_caption_file.name))
        st.caption(t(
            "Caption position — 0 = centred on the CTA box, near its top. Drag "
            "it in the preview to set one row; `CTA_Caption_X`/`CTA_Caption_Y` "
            "(and `_Size`, `_Color`, `_Font`, `_Style`, …) override per row."
        ))
        cta_caption_x = st.number_input(t("Caption X (centre)"), 0, CANVAS_W, 0,
                                        key=keep("cta_caption_x"))
        cta_caption_y = st.number_input(t("Caption Y (centre)"), 0, CANVAS_H, 0,
                                        key=keep("cta_caption_y"))

    # ---- Promo Alternate
    # The switch itself lives beside the promo uploader in the main area (that
    # is where someone looks when deciding not to use a promo), but the sidebar
    # runs FIRST in Streamlit's top-to-bottom script order, so it cannot read
    # the widget's return value. It reads session_state instead, which already
    # holds the NEW value at the start of the rerun a toggle triggers — so this
    # section appears and disappears on the same rerun as the tick, with no lag.
    # Nothing here may be keyed "promo_alt": that key belongs to the checkbox.
    # Read through presets_ui.value(), which knows the checkbox's current key.
    promo_alt = bool(presets_ui.value("promo_alt"))
    # In an EXPANDER rather than behind `if promo_alt:` — the widgets have to be
    # instantiated on every run whether the mode is on or not. Streamlit drops
    # the state of a keyed widget the moment a run stops rendering it, so a
    # section that only exists while the box is ticked would throw away a
    # multi-gigabyte pool the first time someone unticked it to re-read the
    # promo uploader's help text. The expander just opens itself instead.
    with st.expander(t("Promo Alternate"), expanded=promo_alt):
        st.caption(
            t("The pool that REPLACES the promo video, used only while **Use "
            "Promo Alternate clips instead** is ticked beside the promo "
            "uploader. One flat pool, not slots: clips play back-to-back in the "
            "promo's own box, each one once and in full, until the video ends — "
            "and the **voiceover decides where that is**. The CTA clips above "
            "are untouched and keep cycling in their own box, so both rotate.")
        )
        promo_alt_files = st.file_uploader(
            t("Promo Alternate clips (MP4)"), type=["mp4"],
            accept_multiple_files=True,
            on_change=presets_ui.upload_changed, args=("promo_alt_pool",),
            key=presets_ui.uploader_key("promo_alt_pool"),
            help=t("A different selection plays in each output video, every clip "
                 "used once before any repeats."),
        )
        promo_alt_files = presets_ui.with_saved("promo_alt_pool", promo_alt_files,
                                                multiple=True)
        if promo_alt_files:
            _alt_gb = sum(getattr(f, "size", 0) for f in promo_alt_files) / 1024 ** 3
            st.caption(t("{0} clip(s), {1:,.1f} GB", len(promo_alt_files), _alt_gb))
        promo_alt_seconds = st.number_input(
            t("Length without a voiceover (s)"), 1.0, 600.0,
            float(PROMO_ALT_SECONDS), 0.5, key=keep("promo_alt_seconds"),
            help=t("With no promo there is nothing to measure, so a row whose "
                 "Voiceover cell is blank (or a batch with narration turned "
                 "off) is rendered for this long. Rows that DO have narration "
                 "ignore this and last exactly as long as they speak."),
        )
        promo_alt_audio = not st.checkbox(
            t("Mute the clips' own audio"), value=True, key=keep("promo_alt_mute"),
            help=t("On, the video is carried by the voiceover and the music bed "
                 "alone. Off, the clips' own sound takes the place the promo's "
                 "audio held — mixed under the music and ducked under the "
                 "voice. Every clip in the pool must have an audio track for "
                 "that; if any is silent they are all played muted and the "
                 "batch says so."),
        )
    # The pool is only ever read in the mode it belongs to: leaving clips in the
    # uploader must not add a layer to an ordinary promo render.
    if not promo_alt:
        promo_alt_files = []

    st.subheader(t("GIFs (optional)"))
    st.caption(
        t("Short looping clips (MP4) shown one after another in their own box — "
        "separate from the CTA clips above, with their own pool, box and "
        "layer order. Each gif holds the box for at least the dwell time "
        "below, repeating **itself** a whole number of times to get there: a "
        "3-second gif plays twice (6s), it is never cut short. The sequence "
        "keeps drawing fresh gifs until the promo video ends, so it never "
        "freezes on a stopped animation.")
    )
    gif_files = st.file_uploader(
        t("GIF clips (MP4)"), type=["mp4"], accept_multiple_files=True,
        on_change=presets_ui.upload_changed, args=("gif_pool",),
        key=presets_ui.uploader_key("gif_pool"),
        help=t("One flat pool, not slots — a random selection plays in each "
             "output video, every gif used once before any repeats. Ignored "
             "when the GIFs come from Drive instead (set below the sheet)."),
    )
    gif_files = presets_ui.with_saved("gif_pool", gif_files, multiple=True)
    gif_min_seconds = st.number_input(
        t("Minimum seconds per gif"), 1.0, 30.0, 5.0, 0.5, key=keep("gif_min_seconds"),
        help=t("The dwell floor. A gif shorter than this repeats itself until it "
             "clears the floor; a gif already longer plays once, in full."),
    )
    st.caption(
        t("At ~{0:g}s each, a 20-second promo shows about {1} gifs and a 60-second promo about "
          "{2} — however many you upload. The pool is the variety across videos, not within one.", gif_min_seconds, max(1, int(20 // gif_min_seconds)), max(1, int(60 // gif_min_seconds)))
    )
    st.caption(
        t("The box is an invisible fit guide, not a visible panel. Every gif is "
        "scaled to the largest size that still sits inside it — bigger gifs "
        "shrink, smaller ones are enlarged — never cropped and never stretched, "
        "so the box sets the size whatever the source resolution. Whatever is "
        "behind shows through the space left over.")
    )
    gif_x = st.number_input(t("GIF box X"), 0, CANVAS_W, 60, key=keep("gif_x"))
    gif_y = st.number_input(t("GIF box Y"), 0, CANVAS_H, 560, key=keep("gif_y"))
    gif_w = st.number_input(t("GIF box width"), 50, CANVAS_W, 360, key=keep("gif_w"))
    gif_h = st.number_input(t("GIF box height"), 50, CANVAS_H, 360, key=keep("gif_h"))
    gif_fade_start = st.number_input(
        t("GIF fade-in start (s)"), 0.0, 30.0, 0.0, 0.1, key=keep("gif_fade_start"),
        help=t("Per-row override: `GIF_Fade_Start`. 0 = visible from the first "
             "frame, which is also what the static preview shows."),
    )
    gif_fade_duration = st.number_input(
        t("GIF fade-in duration (s)"), 0.0, 30.0, 0.0, 0.1,
        key=keep("gif_fade_duration"),
        help=t("Per-row override: `GIF_Fade_Duration`. Applies to the first gif "
             "only — the rest of the sequence cuts straight in."),
    )

    st.subheader(t("Layer order (z-index)"))
    st.caption(
        t("Which element sits on top when they overlap — higher number = nearer "
        "the front. The background is always at the back. Applies to every video.")
    )
    video_z = st.number_input(
        t("Promo video"), 1, 99, 1, 1, key=keep("z_video"),
        help=t("Stacking order of the promo video layer."),
    )
    gif_z = st.number_input(
        t("GIFs"), 1, 99, 2, 1, key=keep("z_gif"),
        help=t("Stacking order of the GIF layer. Below the CTA layers by "
             "default — it is decoration, so it yields to anything carrying a "
             "message. Raise it above the promo video's number to float a gif "
             "over the video instead of behind it."),
    )
    cta_video_z = st.number_input(
        t("CTA video"), 1, 99, 3, 1, key=keep("z_cta_video"),
        help=t("Stacking order of the CTA video layer."),
    )
    cta_image_z = st.number_input(
        t("CTA image"), 1, 99, 4, 1, key=keep("z_cta_image"),
        help=t("Stacking order of the CTA image (button) layer."),
    )
    text_z = st.number_input(
        t("Texts"), 1, 99, 5, 1, key=keep("z_text"),
        help=t("Stacking order of the headline / subheading / footer texts."),
    )
    st.caption(
        t("The background videos have no number: they always sit directly "
        "beneath the texts, over any layer whose number is at or below the "
        "texts'. (A layer raised ABOVE the texts also rises above the veil.)")
    )

    st.subheader(t("Text style"))
    st.caption(
        t("Defaults for every text — per-row Excel columns `*_Font`, `*_Style`, "
        "`*_Opacity` and `*_BgOpacity` override them, and `*_BgColor` adds a "
        "highlight box behind any text.")
    )
    default_font = st.selectbox(
        t("Default font"), FONT_CHOICES, index=0, key=keep("default_font"),
        help=t("A bundled font family, the system font, or your uploaded font. "
             "Override per text with `Headline_Font` etc."),
     format_func=option_labels(FONT_CHOICES, localize_format(str)))
    default_style = st.selectbox(
        t("Default artistic style"), TEXT_STYLES, index=0, key=keep("default_style"),
        help=t("classic = plain · outline = contrasting border · shadow = drop "
             "shadow · neon = glow. Override per text with `Headline_Style` etc."),
     format_func=option_labels(TEXT_STYLES, localize_format(str)))
    text_opacity_pct = st.slider(
        t("Text opacity (%)"), 0, 100, 100, key=keep("text_opacity_pct"),
        help=t("How solid every text is. Below 100 the video and background show "
             "through the letters (the outline, glow and shadow fade with them). "
             "Override per text with `Headline_Opacity` etc."),
    )
    text_bg_opacity_pct = st.slider(
        t("Highlight box opacity (%)"), 0, 100, 100, key=keep("text_bg_opacity_pct"),
        help=t("Same, for the `*_BgColor` box behind a text — a translucent box "
             "with solid text on top is the classic caption look. Override per "
             "text with `Headline_BgOpacity` etc."),
    )

    st.subheader(t("Text fit boxes (optional)"))
    st.caption(
        t("Give a text a fixed box and the box drives the type: the text re-wraps "
        "to the box's width and its font size is picked so the whole block — "
        "including any outline or glow — fills the box without spilling out. "
        "Line breaks are added **and removed** as the box changes, so long and "
        "short texts come out optically the same size across a batch.")
    )
    st.caption(
        t("Both width and height are needed; **0 = off**, which leaves that text behaving "
          "exactly as before (its `*_Size` cell, wrapped to the canvas). The box is centred on "
          "the text's X/Y, so switching it on never moves anything. Per-row overrides: "
          "`Headline_Width` / `Headline_Height`, and the same for Subheading and Footer. Text "
          "that cannot fit even at {0}px is drawn at {1}px and flagged on the row rather than "
          "shrunk into illegibility.", TEXT_FIT_MIN_SIZE, TEXT_FIT_MIN_SIZE)
    )
    text_box_dims = {}
    for _role in TEXT_ROLES:
        _cw, _ch = st.columns(2)
        text_box_dims[_role] = (
            _cw.number_input(t("{0} box width", t(_role)), 0, CANVAS_W, 0, 10,
                             key=keep(f"tbox_w_{_role}")),
            _ch.number_input(t("{0} box height", t(_role)), 0, CANVAS_H, 0, 10,
                             key=keep(f"tbox_h_{_role}")),
        )

    st.subheader(t("Subliminal text (experimental)"))
    subliminal_targets = st.multiselect(
        t("Apply to"),
        options=["Headline", "Subheading", "Footer"],
        default=list(defaults.SUBLIMINAL_TARGETS),
        max_selections=2, key=keep("subliminal_targets"),
        help=t("Which texts get the effect (up to two at a time; empty = off): no "
             "single frame shows the whole text, but it cycles fast enough to "
             "read as whole in motion. Override per text with a "
             "`<Role>_Subliminal` cell (yes/no)."),
     format_func=option_labels(["Headline", "Subheading", "Footer"], localize_format(str)))
    subliminal_enabled = bool(subliminal_targets)
    if subliminal_enabled:
        subliminal_mode = st.radio(
            t("Effect style"),
            options=["hide", "show"],
            format_func=option_labels(["hide", "show"], lambda m: {"hide": t("Hide a slice (recommended)"),
                                   "show": t("Show only a slice")}[m]),
            key=keep("subliminal_mode"),
            help=t("Hide a slice = every frame shows the whole text minus ~1/K of "
                 "the words, so it stays bright and solid. Show only a slice = "
                 "every frame shows ONLY ~1/K of the words; each word is lit just "
                 "1/K of the time, so it time-averages to about 1/K brightness "
                 "and looks faint. Raising fps shortens the cycle but does not "
                 "change that brightness ratio."),
        )
        subliminal_k = st.slider(
            t("Frames per cycle (K)"), 2, 8, defaults.SUBLIMINAL_K, key=keep("subliminal_k"),
            help=t("The number of distinct frames before the effect repeats "
                 "(K/fps seconds). Higher K = a longer, less obviously repeating "
                 "cycle. Defaults to 7."),
        )
        # Pattern + amount apply to the hide style only (show is always the even
        # 1/K comb).
        if subliminal_mode == "hide":
            subliminal_pattern = st.radio(
                t("Hidden characters"),
                options=["random", "ordered"],
                format_func=option_labels(["random", "ordered"], lambda p: {"random": t("Random each cycle"),
                                       "ordered": t("Fixed pattern")}[p]),
                key=keep("subliminal_pattern"),
                help=t("Random = a different, evenly-spread set is hidden each "
                     "frame (seeded, so the preview still matches) and never "
                     "repeats the same comb. Fixed = the same characters blank "
                     "out in the same frames every cycle."),
            )
            subliminal_hide_pct = st.slider(
                t("Hidden per frame (%)"), 15, 70, defaults.SUBLIMINAL_HIDE_PCT, key=keep("subliminal_hide_pct"),
                disabled=subliminal_pattern != "random",
                help=t("Random pattern only. ~33% (≈100/K) keeps every character "
                     "hidden exactly once per cycle and stays bright. Higher "
                     "hides more per frame, so the text looks fainter. The fixed "
                     "pattern always hides ~1/K."),
            )
        else:
            subliminal_pattern = "ordered"
            subliminal_hide_pct = defaults.SUBLIMINAL_HIDE_PCT
        subliminal_granularity = st.radio(
            t("Split by"), ["word", "char"], horizontal=True,
            key=keep("subliminal_granularity"),
            help=t("Word = omit whole words (more readable). Char = omit letters "
                 "(auto-used when there aren't enough words for K)."),
         format_func=option_labels(["word", "char"], localize_format(str)))
        subliminal_all_intra = st.checkbox(
            t("Preserve frame-by-frame (larger files)"),
            value=defaults.SUBLIMINAL_ALL_INTRA, key=keep("subliminal_all_intra"),
            help=t("Encode every frame independently so a frame-scrub of THIS file "
                 "never shows the whole text. Turning this off shrinks files but "
                 "lets the codec blur frames together."),
        )
    else:
        subliminal_mode = "hide"
        subliminal_pattern = "random"
        subliminal_hide_pct = defaults.SUBLIMINAL_HIDE_PCT
        subliminal_k = defaults.SUBLIMINAL_K
        subliminal_granularity = "word"
        subliminal_all_intra = defaults.SUBLIMINAL_ALL_INTRA

    st.subheader(t("Output"))
    fps = st.select_slider(
        t("Frame rate (fps)"), options=list(FPS_CHOICES), value=FPS_CHOICES[0],
        key=keep("fps"),
        help=t("60 halves the subliminal cycle length (K/fps seconds), so the "
             "effect blends more smoothly — at the cost of roughly double the "
             "frames to encode. Both survive upload to every major platform."),
     format_func=option_labels(list(FPS_CHOICES), localize_format(str)))
    crf = st.slider(t("Quality (CRF — lower = better/bigger)"), 16, 28, 18,
                    key=keep("crf"))
    preset = st.select_slider(
        t("Encoder speed"),
        options=["veryfast", "faster", "fast", "medium", "slow"],
        value=defaults.ENCODER_PRESET, key=keep("encoder_preset"),
        help=t("Faster presets render quicker but produce slightly larger files."),
     format_func=option_labels(["veryfast", "faster", "fast", "medium", "slow"], localize_format(str)))
    # One render only keeps 1-2 cores busy (the compositing pipeline inside
    # FFmpeg is serial), so many-core machines need many concurrent renders to
    # saturate. The slider tops out at the core count; RAM is the real
    # ceiling — budget roughly 2-6 GB per render when subliminal texts are on.
    max_workers = defaults.max_workers()
    workers = st.slider(
        t("Parallel renders"), 1, max_workers, max_workers,
        key=keep("workers"),
        help=t("Concurrent FFmpeg processes. Defaults to the panel maximum on "
             "this machine. Each render uses roughly 2–6 GB of RAM."),
    )
    font_file = st.file_uploader(
        t("Custom font (TTF/OTF, optional)"), type=["ttf", "otf"],
        on_change=presets_ui.upload_changed, args=("font",),
        key=presets_ui.uploader_key("font"),
        help=t("Upload your own font, then pick “{0}” as the default font above (or set a "
               "`*_Font` cell to it) to use it.", t(FONT_CUSTOM)),
    )
    font_file = presets_ui.with_saved("font", font_file, multiple=False)

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
    voice_enabled=bool(voice_enabled),
    voice_engine=str(voice_engine),
    voice_set=list(voice_set),
    voice_speed=float(voice_speed),
    voice_lead_in=float(voice_lead_in),
    voice_gain=float(voice_gain),
    voice_duck=bool(voice_duck),
    voice_duck_ratio=float(voice_duck_ratio),
    voice_loop_promo=bool(voice_loop_promo),
    voice_mute_promo=bool(voice_mute_promo),
    beat_max_words=int(beat_max_words),
    screen_text_y=int(screen_text_y),
    video_x=int(video_x), video_y=int(video_y),
    video_w=int(video_w), video_h=int(video_h),
    promo_alt=bool(promo_alt),
    promo_alt_seconds=float(promo_alt_seconds),
    promo_alt_audio=bool(promo_alt_audio),
    cta_x=int(cta_x), cta_y=int(cta_y),
    cta_w=int(cta_w), cta_h=int(cta_h),
    cta_fade_start=float(cta_fade_start), cta_fade_duration=float(cta_fade_duration),
    cta_video_x=int(cta_video_x), cta_video_y=int(cta_video_y),
    cta_video_w=int(cta_video_w), cta_video_h=int(cta_video_h),
    cta_video_fade_start=float(cta_video_fade_start),
    cta_video_fade_duration=float(cta_video_fade_duration),
    cta_video_speeds=[float(s) for s in cta_video_speeds],
    cta_video_fill=bool(cta_video_fill),
    cta_after_promo=bool(cta_after_promo) and not is_split,
    cta_after_show_texts=bool(cta_after_show_texts),
    cta_captions=list(cta_captions),
    cta_caption_x=int(cta_caption_x), cta_caption_y=int(cta_caption_y),
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
st.subheader(t("1. Upload assets"))
col1, col2 = st.columns(2)
with col1:
    excel_file = presets_ui.with_saved(
        "excel", st.file_uploader(t("Excel file (.xlsx)"), type=["xlsx"],
                                  on_change=presets_ui.upload_changed, args=("excel",),
                                  key=presets_ui.uploader_key("excel")),
        multiple=False)
    # Declared BEFORE the uploader it disables: Streamlit refuses to change a
    # widget after it has been instantiated. Its key is read by the sidebar,
    # which runs earlier in the script but sees the new value straight away
    # because a tick triggers a rerun with session_state already updated.
    st.checkbox(
        t("Use Promo Alternate clips instead"), key=keep("promo_alt"),
        help=t("No promo video at all. The promo's box is filled by a pool of "
             "clips uploaded in the sidebar, playing back-to-back, and the "
             "voiceover and its captions decide how long each video runs. The "
             "CTA clips keep cycling in their own box alongside."),
    )
    promo_source = st.radio(
        t("Promo video source"), options=["upload", "drive_folder"],
        format_func=option_labels(["upload", "drive_folder"], lambda source: {
            "upload": t("Upload files"),
            "drive_folder": t("Google Drive folder"),
        }[source]),
        horizontal=True, disabled=promo_alt, key=keep("promo_source"),
    )
    if promo_alt:
        promo_source = "upload"
    promo_drive_files = []
    promos_drive_folder = ""
    if promo_source == "drive_folder":
        from integrations import drive

        promos_drive_folder = st.text_input(
            t("Promo videos Google Drive folder link"),
            key=keep("promos_drive_folder"),
            help=t("Share this folder with the service account (Viewer access). "
                   "An ordinary My Drive folder also works."),
        ).strip()
        if st.button(t("Read promo video list"), disabled=not promos_drive_folder,
                     key="read_drive_promos"):
            try:
                with st.spinner(t("Reading promo videos from Drive…")):
                    entries = drive.list_videos(
                        drive.extract_id(promos_drive_folder),
                        max_files=MAX_PROMO_VIDEOS)
                st.session_state["_drive_promos"] = {
                    "link": promos_drive_folder, "files": entries}
                if not entries:
                    st.warning(t("This Drive folder has no video files."))
            except Exception as exc:  # noqa: BLE001 — Drive API and transport errors
                st.session_state.pop("_drive_promos", None)
                st.error(t("Could not read promo videos: {0}", exc))
        listed = st.session_state.get("_drive_promos") or {}
        if listed.get("link") == promos_drive_folder:
            promo_drive_files = listed["files"]
        st.caption(t("Read the list first. Videos download directly to the server "
                     "after you submit the job; a preview downloads only the "
                     "selected video."))
        if promo_drive_files:
            st.caption(t("{0} promo video(s), {1:,.1f} GB",
                         len(promo_drive_files),
                         sum(f["size"] for f in promo_drive_files) / 1024 ** 3))
    promo_files = st.file_uploader(
        t("Promo video(s) (MP4) — up to {0}", MAX_PROMO_VIDEOS), type=["mp4"],
        accept_multiple_files=True, disabled=promo_alt or promo_source != "upload",
        on_change=presets_ui.upload_changed, args=("promos",),
        key=presets_ui.uploader_key("promos"),
        help=t("Upload one to use it in every video. Upload several and a "
             "multi-batch render gives each batch its own promo video, cycling "
             "if there are fewer promos than batches."),
    )
    promo_files = presets_ui.with_saved("promos", promo_files, multiple=True)
    if promo_source != "upload":
        promo_files = []
    # Disabling an uploader does NOT clear what it already holds — the files
    # stay in session_state and would still be staged at submit. Drop them here
    # so everything downstream (the batch default, the grids, the pairing table,
    # stage_uploads) agrees that this mode has no promos.
    if promo_alt:
        promo_files = []
        if not promo_alt_files:
            st.warning(
                t("Promo Alternate is on but its pool is empty — upload clips "
                "under **Promo Alternate** in the sidebar."))
    # Truncated HERE, not in stage_uploads, because everything below reads this
    # list: the "Batches to render" DEFAULT (Streamlit raises rather than clamps
    # when a default lands above max_value, so an over-long upload took the page
    # down), the promo names the text grids are matched against (a column
    # matched to a dropped promo resolved to an index that was never staged —
    # wrong words on real videos, reported as success), and the pairing table.
    # One guard at the source keeps all of them agreeing with what is staged.
    if len(promo_files or []) > MAX_PROMO_VIDEOS:
        st.warning(
            t("{0} promo videos uploaded — only the first {1} are used. The rest are dropped.", len(promo_files), MAX_PROMO_VIDEOS)
        )
        promo_files = promo_files[:MAX_PROMO_VIDEOS]
    if promo_files:
        # Every one of these stays resident in this Streamlit session for as
        # long as the tab is open, AND is copied into the job's assets/ folder
        # on submit. Worth seeing before waiting out a multi-gigabyte upload.
        _promo_gb = sum(getattr(f, "size", 0) for f in promo_files) / 1024 ** 3
        st.caption(t("{0} promo video(s), {1:,.1f} GB", len(promo_files), _promo_gb))
    # Preview and Render Row work off a single promo — the first one.
    video_file = promo_files[0] if promo_files else None
with col2:
    zip_file = st.file_uploader(
        t("Background images (ZIP, optional)"), type=["zip"],
        help=t("Without a ZIP, videos render on the solid background color set in "
             "the sidebar (Layout section). The images can also come from a "
             "Drive folder — see 'Background images' under 3. CTA clips."),
        on_change=presets_ui.upload_changed, args=("bg_zip",),
        key=presets_ui.uploader_key("bg_zip"),
    )
    zip_file = presets_ui.with_saved("bg_zip", zip_file, multiple=False)
    cta_file = presets_ui.with_saved(
        "cta_image", st.file_uploader(t("CTA image (PNG, optional)"), type=["png"],
                                      on_change=presets_ui.upload_changed, args=("cta_image",),
                                      key=presets_ui.uploader_key("cta_image")),
        multiple=False)

# ---- Excel validation & preview table
df = None
if excel_file is not None:
    try:
        df = pd.read_excel(io.BytesIO(excel_file.getvalue()), engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
    except Exception as exc:  # noqa: BLE001
        st.error(t("Could not read the Excel file: {0}", exc))

    if df is not None:
        missing = validate_dataframe(df)
        if missing:
            st.error(
                t("The Excel file is missing required columns: ")
                + t(", ".join(f"`{c}`" for c in missing))
            )
            with st.expander(t("Supported columns")):
                st.code(
                    t("Required:\n  ") + "\n  ".join(REQUIRED_COLUMNS)
                    + t("\n\nOptional (defaults / auto-placement when absent):\n  ")
                    + "\n  ".join(c for c in ALL_COLUMNS if c not in REQUIRED_COLUMNS)
                )
            df = None
        elif df.empty:
            st.error(t("The Excel file has no data rows."))
            df = None
        else:
            st.success(t("Excel loaded — {0} video(s) to generate.", len(df)))
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
                    t("Columns not in this sheet (defaults / auto-placement will be used): ")
                    + t(", ".join(f"`{c}`" for c in absent))
                )
            with st.expander(t("Preview spreadsheet data")):
                st.dataframe(df, hide_index=True, width="stretch")

# ---- optional per-promo text
#
# One grid workbook per role, each column headed with a promo video's filename.
# Uploading one makes that role's text come from the grid instead of the sheet;
# everything else about the row — size, font, colour, position, background box
# — is untouched, which is the whole point. See text_grids.py.
promo_names = ([f["name"] for f in promo_drive_files] if promo_source == "drive_folder"
               else [f.name for f in (promo_files or [])])
promo_key = (promo_source, [(f["id"], f["name"], f["size"]) for f in promo_drive_files],
             [(f.name, f.size) for f in (promo_files or [])])
if st.session_state.get("_promo_key", promo_key) != promo_key:
    for stale in ("preview_payload", "preview_nonce", "preview_baseline_edits",
                  "row_render"):
        st.session_state.pop(stale, None)
st.session_state["_promo_key"] = promo_key
sheet_rows = len(df) if df is not None else 0
grid_keys = {role: f"text_grid_{role}" for role in TEXT_ROLES}
grid_uploads: dict[str, object] = {}

with st.expander(
    t("Per-promo heading, subheading and footer text (optional)"),
    # Opened once something is in it, so a blocking error is never hidden
    # behind a collapsed panel.
    expanded=any(st.session_state.get(presets_ui.uploader_key(k)) is not None
                 or presets_ui.has_saved(k) for k in grid_keys.values()),
):
    st.caption(
        t("Upload a sheet whose **column headers are your promo video filenames** "
        "and whose rows are the text for each row of the main Excel — so the "
        "same row can say something different on every promo. Row 1 here is "
        "row 1 there. A sheet you upload replaces **only** that text: size, "
        "font, colour, position and background box still come from the main "
        "Excel and the sidebar. Leave a cell blank to keep the main Excel's "
        "text for that one.")
    )
    if promo_alt:
        st.info(
            t("Not used with Promo Alternate — these sheets match their columns to "
            "promo video filenames, and this mode has no promo videos. Per-row "
            "text comes from the main Excel as usual. Anything uploaded here is "
            "kept and used again as soon as the mode is turned off.")
        )
    # The uploaders are instantiated either way: Streamlit discards a keyed
    # widget's state the moment a run stops rendering it, so hiding them would
    # silently throw away workbooks the moment someone tried the mode. What
    # changes is that `grid_uploads` is left EMPTY in this mode — with no promo
    # names every column is unmatched, which text_grids treats as an error that
    # refuses the batch outright, so anyone with a grid uploaded could not
    # submit at all.
    for column, role in zip(st.columns(len(TEXT_ROLES)), TEXT_ROLES):
        upload = column.file_uploader(
            t("{0} by promo (.xlsx)", t(role)), type=["xlsx"],
            on_change=presets_ui.upload_changed, args=(grid_keys[role],),
            key=presets_ui.uploader_key(grid_keys[role]),
            disabled=promo_alt,
            help=t("One column per promo video, one row per row of the main Excel. Overrides the"
                   " main sheet's `{0}` column.", role),
        )
        upload = presets_ui.with_saved(grid_keys[role], upload, multiple=False,
                                       where=column)
        if not promo_alt:
            grid_uploads[role] = upload
    if promo_alt:
        pass
    elif promo_names and sheet_rows:
        st.caption(t("Templates below already carry the right headers and row "
                   "count — fill one in and upload it back."))
        for column, role in zip(st.columns(len(TEXT_ROLES)), TEXT_ROLES):
            column.download_button(
                t("⬇️ {0} template", t(role)),
                data=text_grids.template_bytes(role, promo_names, sheet_rows),
                file_name=f"{role.lower()}_by_promo.xlsx", mime=XLSX_MIME,
                key=f"text_grid_template_{role}", width="stretch",
            )
    else:
        st.caption(t("Upload the main Excel and your promo video(s) first to get "
                   "templates with the headers already filled in."))

    grid_reports, grid_overrides, grid_errors = text_grids.check_uploads(
        grid_uploads, promo_names, sheet_rows, reader=read_text_grid)

    if any(u is not None for u in grid_uploads.values()):
        if not promo_names:
            st.info(t("Upload your promo video(s) — the column headers are "
                    "checked against their filenames."))
        checked = st.button(
            t("🔍 Check promo names against these sheets"), width="stretch",
            help=t("Shows which column of each sheet feeds which promo video."),
         key='_ui_app_check_promo_names_against_these_sheets')
        # Shown on demand, but forced whenever something is wrong: an error
        # here refuses the batch, so it cannot be behind a button nobody pressed.
        if (checked or grid_errors) and promo_names:
            matched = {role: {i: column for i, _n, column in report.pairs}
                       for role, report in grid_reports.items()}
            pairing = []
            for index, name in enumerate(promo_names):
                entry = {"#": index + 1, t("Promo video"): name}
                for role in TEXT_ROLES:
                    if role not in grid_reports:
                        entry[t(role)] = t("— not uploaded")
                        continue
                    column = matched[role].get(index)
                    entry[t(role)] = (t("✅ column “{0}”", column) if column
                                          else t("⚠️ falls back to the main Excel"))
                pairing.append(entry)
            st.dataframe(pd.DataFrame(pairing), hide_index=True, width="stretch")
            if grid_reports:
                st.caption(t(" · ".join(
                    t("**{0}**: {1} row(s) vs the main Excel's {2}, {3}/{4} promo(s) matched",
                      t(role), r.grid_rows, r.sheet_rows, r.matched, len(promo_names)) for role, r in grid_reports.items())))
        for message in grid_errors:
            st.error(t(message))
        if checked or grid_errors:
            for report in grid_reports.values():
                for message in report.warnings:
                    st.warning(t(message))
        if checked and grid_overrides and not grid_errors:
            st.success(
                t("Every column matches a promo video — ")
                + t(", ".join(f"{t(role)} ✓" for role in sorted(grid_overrides)))
                + t(". These sheets will be used instead of the main Excel's "
                  "text."))

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

# In Promo Alternate mode the alternate pool stands in for the promo — it is
# what fills the promo's box, so it is what "have we got a picture yet" means.
ready = df is not None and (bool(promo_alt_files) if promo_alt
                            else bool(promo_names))
if not ready:
    st.info(
        t("Upload the Excel sheet and ")
        + (t("Promo Alternate clips (sidebar)") if promo_alt else
           t("a promo video (upload files or read a Google Drive folder)"))
        + t(" to enable preview and generation. The background ZIP and CTA image "
          "are optional — without backgrounds, videos render on the sidebar's "
          "background color.")
    )

# ---- actions
st.subheader(t("2. Preview a row"))

# With several promos uploaded, Preview and Render Row always used the first
# one — so there was no way to see how a particular row looks against a
# particular promo. In the real batch every row is paired automatically and
# spread across all of them; this is for checking one pairing by eye.
if len(promo_names) > 1:
    col_row, col_promo, col_preview, col_render = st.columns(
        [1, 1.6, 1.4, 1.4], vertical_alignment="bottom")
    promo_choice = col_promo.selectbox(
        t("Promo video"), options=list(range(len(promo_names))),
        format_func=option_labels(list(range(len(promo_names))), lambda i: f"{i + 1}. {promo_names[i]}"),
        disabled=not ready,
        help=t("Which promo to pair with this row for the preview and the test "
             "render. The full batch pairs every row automatically, spread "
             "evenly across all of them."),
     key='_ui_app_promo_video')
    # Preview and Render Row below build their workspace from this one.
    if promo_source == "upload":
        video_file = promo_files[promo_choice]
else:
    promo_choice = 0
    col_row, col_preview, col_render = st.columns(
        [1, 1.5, 1.5], vertical_alignment="bottom")

preview_row = col_row.number_input(
    t("Row to preview"), 1, len(df) if df is not None else 1, 1, disabled=not ready,
    help=t("Excel data row number (1 = the first row below the header)."),
 key='_ui_app_row_to_preview')
preview_clicked = col_preview.button(t("👁️ Preview Row"), disabled=not ready, width="stretch", key='_ui_app_preview_row')
render_row_clicked = col_render.button(
    t("🎬 Render Row"), disabled=not ready, width="stretch",
    help=t("Render this one row to a real MP4 — with your saved edits — and play "
         "it here. Slower than the static preview, but it's the actual video."),
 key='_ui_app_render_row')


# ---- clip source: the whole point of the unified flow
st.subheader(t("3. CTA clips"))


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
    t("Where do the clips come from?"),
    options=["upload", "drive_folder", "scrape_job", "scrape_now"],
    format_func=option_labels(["upload", "drive_folder", "scrape_job", "scrape_now"], lambda m: {
        "upload": t("Upload files in the sidebar (as before)"),
        "drive_folder": t("Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)"),
        "scrape_job": t("Use a previous scrape already on this machine ({0} available)", len(available)),
        "scrape_now": t("Scrape an account now (TikTok, Instagram or YouTube), "
                      "as part of this run"),
    }[m]),
    horizontal=False,
    key=keep("clip_source"),
)

clip_params: dict = {}
if clip_source == "drive_folder":
    st.caption(
        t("The clips never touch your browser: the server fetches them straight "
        "from Drive when the batch runs, which is Google-to-Google rather than "
        "up your own connection. Share the folder with the service account "
        "first — read access is enough, and unlike the *upload* destination it "
        "can be an ordinary My Drive folder.")
    )
    drive_clip_layout = st.radio(
        t("Folder layout"),
        options=["per_slot", "pooled"],
        format_func=option_labels(["per_slot", "pooled"], lambda m: {
            "per_slot": t("One folder per clip slot (same as the sidebar uploaders)"),
            "pooled": t("One folder for everything — deal the clips across the slots"),
        }[m]),
        horizontal=False, key=keep("drive_clip_layout"),
        help=t("Per-slot keeps control of which clip plays where. Pooled is the "
             "unattended version: every clip in the folder is used, spread "
             "evenly across the slots and never used twice."),
    )
    clip_params["drive_clip_layout"] = drive_clip_layout

    if drive_clip_layout == "per_slot":
        clip_params["clips_drive_folders"] = [
            st.text_input(
                t("Clip {0} — Drive folder link", i), key=keep(f"cta_clip_drive_{i}"),
                placeholder=t("https://drive.google.com/drive/folders/…"),
                help=t("The folder holding this position's sample videos. Leave "
                     "blank to skip this slot."),
            ).strip()
            for i in range(1, cta_slot_count + 1)
        ]
        _links_to_check = [(t("Clip {0}", i), link) for i, link
                           in enumerate(clip_params["clips_drive_folders"], start=1)
                           if link]
    else:
        clip_params["clips_drive_folder"] = st.text_input(
            t("Drive folder link"), key=keep("cta_clips_drive_pooled"),
            placeholder=t("https://drive.google.com/drive/folders/…"),
            help=t("Every video in this folder — and its sub-folders — becomes "
                 "part of the pool."),
        ).strip()
        _links_to_check = ([(t("Clips"), clip_params["clips_drive_folder"])]
                           if clip_params["clips_drive_folder"] else [])

    # Checking here costs one API call and saves finding out three minutes into
    # a multi-hour job that the folder was never shared.
    if st.button(t("🔍 Check the folder(s)"), disabled=not _links_to_check, key='_ui_app_check_the_folder_s'):
        from integrations import drive as _drv

        with st.spinner(t("Reading Drive…")):
            for _label, _link in _links_to_check:
                _ok, _msg = _drv.check_source(_link)
                (st.success if _ok else st.error)(f"**{_label}** — {_msg}")
    elif not _links_to_check:
        st.caption(t("Paste a folder link to enable the check."))

elif clip_source == "scrape_job":
    if not available:
        st.warning(
            t("No finished scrape has clips on this machine. Scrapes are "
            "removed from the server once their clips are safely in Google "
            "Drive, so this list only holds ones that have not been uploaded "
            "yet. To reuse clips that are already in Drive, choose "
            "**Google Drive folder** above and paste that scrape's folder "
            "link — no re-scraping needed.")
        )
    else:
        pick = st.selectbox(
            t("Which scrape"), options=[j["id"] for j in available],
            format_func=option_labels([j["id"] for j in available], lambda i: t(next(
                t("{0} — {1} clips", j["label"], j["n_clips"]) for j in available if j["id"] == i))),
         key='_ui_app_which_scrape')
        clip_params["clips_from_job"] = pick

elif clip_source == "scrape_now":
    clip_params["account"] = st.text_input(
        t("Account link (or a TikTok @handle)"), key=keep("scrape_account"),
        placeholder=t("https://www.tiktok.com/@someaccount"),
        help=t("A TikTok, Instagram or YouTube profile link — the link says "
             "which site to scrape. A bare @handle is taken as TikTok."),
    )
    col_l, col_s, col_d = st.columns(3)
    clip_params["scrape_limit"] = col_l.number_input(
        t("Max videos to scan"), 10, 5000, 200, 10, key=keep("scrape_limit"),
        help=t("Enumeration runs at roughly 0.26s per post, then downloads are "
             "rate-limited to about one per 1-2s."))
    clip_params["trim_start"] = col_s.number_input(
        t("Trim start (s)"), 0.0, 60.0, settings.SCRAPE_TRIM_START, 0.5,
        key=keep("trim_start"))
    clip_params["trim_duration"] = col_d.number_input(
        t("Trim length (s)"), 1.0, 60.0, settings.SCRAPE_TRIM_DURATION, 0.5,
        key=keep("trim_duration"))

if clip_source != "upload":
    st.caption(
        t("The sidebar clip uploaders are ignored in this mode — clips come from ")
        + (t("Drive instead. The sidebar's *Number of clip slots* and the per-clip "
           "speeds still apply.") if clip_source == "drive_folder"
           else t("the scrape instead."))
    )
    # Both non-upload sources fill the same cta_slot_N folders, so the slot
    # count has to travel with the job.
    clip_params["slots"] = int(cta_slot_count)

if clip_source in ("scrape_job", "scrape_now"):
    col_st, col_ps = st.columns(2)
    clip_params["clip_strategy"] = col_st.selectbox(
        t("Which clips to use"),
        options=["top_views", "all"],
        format_func=option_labels(["top_views", "all"], lambda s: {"top_views": t("Best by view count (recommended)"),
                               "all": t("All of them")}[s]),
        key=keep("clip_strategy"),
        help=t("Curation as a rule rather than a chore: the highest-performing "
             "clips fill the slots, and the run needs no attention. Clips are "
             "then shuffled into slots, so no slot systematically gets the "
             "best ones and no clip is used twice."),
    )
    clip_params["clips_per_slot"] = col_ps.number_input(
        t("Clips per slot"), 1, 50, 10, 1, key=keep("clips_per_slot"))

# ---- GIF source. Deliberately its OWN selector rather than reusing
# clip_source: the CTA options include "scrape a TikTok account", which has no
# meaning for a pool of gif loops, and the two layers are independent
# everywhere else too.
st.markdown(t("**GIFs**"))
gif_source = st.radio(
    t("Where do the GIFs come from?"),
    options=["upload", "drive_folder"],
    format_func=option_labels(["upload", "drive_folder"], lambda m: {
        "upload": t("Upload them in the sidebar (as above)"),
        "drive_folder": t("Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)"),
    }[m]),
    horizontal=True,
    key=keep("gif_source"),
    help=t("Independent of where the CTA clips come from."),
)
if gif_source == "drive_folder":
    clip_params["gifs_drive_folder"] = st.text_input(
        t("GIF folder — Drive link"), key=keep("gifs_drive"),
        placeholder=t("https://drive.google.com/drive/folders/…"),
        help=t("Every video in this folder — and its sub-folders — becomes part "
             "of the gif pool. Share it with the service account first."),
    ).strip()
    st.caption(t("The sidebar GIF uploader is ignored in this mode."))
    if st.button(t("🔍 Check the GIF folder"),
                 disabled=not clip_params.get("gifs_drive_folder"), key='_ui_app_check_the_gif_folder'):
        from integrations import drive as _drv

        with st.spinner(t("Reading Drive…")):
            _ok, _msg = _drv.check_source(clip_params["gifs_drive_folder"])
            (st.success if _ok else st.error)(t("**GIFs** — {0}", t(_msg)))

# ---- Background-image source. Its own selector, same as the pools below: the
# images are independent of every clip layer. Files land in the same folder a ZIP
# would have been extracted into, so BG_Image names and the random deal behave
# identically whichever way they arrive.
st.markdown(t("**Background images**"))
bg_image_source = st.radio(
    t("Where do the background images come from?"),
    options=["upload", "drive_folder"],
    format_func=option_labels(["upload", "drive_folder"], lambda m: {
        "upload": t("Upload a ZIP in the sidebar (as above)"),
        "drive_folder": t("Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)"),
    }[m]),
    horizontal=True,
    key=keep("bg_image_source"),
    help=t("Independent of where the clips, gifs and background videos come from."),
)
if bg_image_source == "drive_folder":
    clip_params["bg_images_drive_folder"] = st.text_input(
        t("Background images folder — Drive link"), key=keep("bg_images_drive"),
        placeholder=t("https://drive.google.com/drive/folders/…"),
        help=t("Every image (PNG/JPG/WEBP/BMP) in this folder — and its "
             "sub-folders — joins the background pool. Share it with the "
             "service account first. An Excel BG_Image cell can name any file "
             "in it; blank cells get a random one."),
    ).strip()
    st.caption(
        t("The sidebar ZIP uploader is ignored in this mode. Preview and *Render "
        "row* don't read Drive either, so they show the solid background color; "
        "the batch itself uses the folder.")
    )
    if st.button(t("🔍 Check the background images folder"),
                 disabled=not clip_params.get("bg_images_drive_folder"), key='_ui_app_check_the_background_images_folder'):
        from integrations import drive as _drv

        with st.spinner(t("Reading Drive…")):
            _ok, _msg = _drv.check_source(clip_params["bg_images_drive_folder"],
                                          kind="image")
            (st.success if _ok else st.error)(t("**Background images** — {0}", t(_msg)))


def _for_preview(frame):
    """In Drive mode the preview workspace has no images, so a BG_Image cell that
    names one would fail the preview. Blank them: the row shows the solid color."""
    return frame.assign(BG_Image="") if bg_image_source != "upload" else frame


# ---- Background-video source. Its own selector for the same reason the gifs
# have one: the pool is independent of both clip layers everywhere else.
st.markdown(t("**Background videos**"))
bg_video_source = st.radio(
    t("Where do the background videos come from?"),
    options=["upload", "drive_folder"],
    format_func=option_labels(["upload", "drive_folder"], lambda m: {
        "upload": t("Upload them in the sidebar (as above)"),
        "drive_folder": t("Paste a Google Drive folder link — the server "
                        "downloads them itself (no upload)"),
    }[m]),
    horizontal=True,
    key=keep("bg_video_source"),
    help=t("Independent of where the CTA clips and gifs come from."),
)
if bg_video_source == "drive_folder":
    clip_params["bg_videos_drive_folder"] = st.text_input(
        t("Background videos folder — Drive link"), key=keep("bg_videos_drive"),
        placeholder=t("https://drive.google.com/drive/folders/…"),
        help=t("Every video in this folder — and its sub-folders — joins the "
             "background pool. Share it with the service account first."),
    ).strip()
    st.caption(t("The sidebar background-video uploader is ignored in this mode."))
    if st.button(t("🔍 Check the background videos folder"),
                 disabled=not clip_params.get("bg_videos_drive_folder"), key='_ui_app_check_the_background_videos_folder'):
        from integrations import drive as _drv

        with st.spinner(t("Reading Drive…")):
            _ok, _msg = _drv.check_source(clip_params["bg_videos_drive_folder"])
            (st.success if _ok else st.error)(t("**Background videos** — {0}", t(_msg)))

# ---- Music source. Its own selector again, and the only one that reads AUDIO
# files out of Drive — a folder of MP3s is invisible to the three above.
st.markdown(t("**Music**"))
music_source = st.radio(
    t("Where does the music come from?"),
    options=["upload", "drive_folder"],
    format_func=option_labels(["upload", "drive_folder"], lambda m: {
        "upload": t("Upload it in the sidebar (as above)"),
        "drive_folder": t("Paste a Google Drive folder link — the server "
                        "downloads it itself (no upload)"),
    }[m]),
    horizontal=True,
    key=keep("music_source"),
    help=t("Independent of where the clips, gifs and background videos come from."),
)
if music_source == "drive_folder":
    clip_params["music_drive_folder"] = st.text_input(
        t("Music folder — Drive link"), key=keep("music_drive"),
        placeholder=t("https://drive.google.com/drive/folders/…"),
        help=t("Every audio file in this folder — and its sub-folders — joins the "
             "music pool. Share it with the service account first."),
    ).strip()
    st.caption(t("The sidebar music uploader is ignored in this mode."))
    if st.button(t("🔍 Check the music folder"),
                 disabled=not clip_params.get("music_drive_folder"), key='_ui_app_check_the_music_folder'):
        from integrations import drive as _drv

        with st.spinner(t("Reading Drive…")):
            _ok, _msg = _drv.check_source(clip_params["music_drive_folder"],
                                          kind="audio")
            (st.success if _ok else st.error)(t("**Music** — {0}", t(_msg)))
if music_source == "upload" and not music_files and music_volume:
    st.caption(t("No tracks uploaded yet — the music layer stays off until there "
               "are some."))

# ---- captions
st.subheader(t("4. Captions"))
_pool = store.active_pool()
caption_mode = st.radio(
    t("Captions"),
    options=["existing", "generate", "none"],
    format_func=option_labels(["existing", "generate", "none"], lambda m: {
        "existing": (t("Use the active caption pool") if _pool
                     else t("No pool — name files from the sheet's Headline")),
        "generate": t("Generate a fresh pool first (Gemini)"),
        "none": t("None — no captions; name files from Headline"),
    }[m]),
    horizontal=True,
    key=keep("caption_mode"),
)
caption_params: dict = {"caption_mode": caption_mode}
if _pool and caption_mode != "none":
    st.caption(t("Active pool: **{0:,} unique pairs** left to draw from · theme *{1}*", _pool['combinations'], _pool.get('theme')))
if caption_mode == "none":
    st.session_state[keep("fixed_tail")] = False
    st.session_state[keep("hashtag_source")] = "none"

fixed_tail = st.checkbox(
    t("End every filename with a fixed call-to-action line"),
    value=False, key=keep("fixed_tail"),
    disabled=caption_mode == "none",
    help=t("One of the lines below is picked at random for each video and added "
         "after its caption. Hashtags are switched off while this is on — the "
         "name carries one ending, not two, and both would not fit inside the "
         "90-character cap."),
)
caption_params["fixed_tail"] = bool(fixed_tail)
if fixed_tail:
    st.caption(t("One of these, at random, per video:\n\n")
               + t("\n".join(f"- {t}" for t in caption_naming.FIXED_TAILS)))
    # A pool generated before the caption limit dropped holds captions too long
    # to sit beside the line, and they are cut at naming time. Measured against
    # the real pool rather than guessed, so the warning only appears when it
    # is actually true.
    _room = caption_naming.MAX_STEM - 1 - max(
        len(t) for t in caption_naming.FIXED_TAILS)
    _over = [c for c in ((_pool or {}).get("captions") or []) if len(c) > _room]
    if _over and caption_mode == "existing":
        st.warning(
            t("**{0:,} of {1:,} captions in the active pool are longer than {2} characters** and"
              " will be cut to fit beside the line (the line itself is never cut). This pool "
              "predates the {3}-character limit — generate a fresh one to avoid it.", len(_over), len(_pool['captions']), _room, settings.CAPTION_MAX_CHARS)
        )

hashtag_source = st.radio(
    t("Hashtags"),
    options=["pool", "excel", "none"],
    format_func=option_labels(["pool", "excel", "none"], lambda m: {
        "pool": t("From the caption pool"),
        "excel": t("From an Excel file I upload"),
        "none": (t("None — filenames use Headline") if caption_mode == "none"
                 else t("None — filenames are the caption only")),
    }[m]),
    horizontal=True,
    disabled=bool(fixed_tail) or caption_mode == "none", key=keep("hashtag_source"),
    help=t("Hashtags are optional. Without them the short filename is just the "
         "caption, still capped at 90 characters."),
)
if fixed_tail:
    # Recorded as 'none' so nothing downstream reads a stale choice as live —
    # the fixed line has already replaced hashtags by the time naming runs.
    hashtag_source = "none"
caption_params["hashtag_source"] = hashtag_source
# The script pool. Only rows whose `Voiceover` cell is BLANK draw from it, so a
# sheet that already carries its own scripts ignores this entirely.
script_file = None
script_source = "upload"
if voice_enabled:
    script_source = st.radio(
        t("Scripts"),
        options=["upload", "generate"],
        format_func=option_labels(["upload", "generate"], lambda m: {
            "upload": t("From a file I upload"),
            "generate": t("Generate one per video (Gemini)"),
        }[m]),
        horizontal=True, key=keep("script_source"),
        help=t("Either way, a row with its own `Voiceover` cell keeps it, and a "
             "row with only `Screen_Text` stays silent."),
    )
if voice_enabled and script_source == "generate":
    caption_params["script_prompt"] = st.text_area(
        t("Script prompt"),
        height=200, key=keep("script_prompt"),
        placeholder=t("e.g. Write a 15-second TikTok voiceover for a skincare "
                    "serum. Open with a hook, name one benefit, end with a "
                    "call to action. Casual, second person."),
        help=t("Sent to Gemini as written — describe the product, tone and "
             "length here. The app only adds how many scripts to write and "
             "asks for plain spoken text. Every video gets its own script, "
             "written when the job starts, and each script is also the "
             "video's on-screen caption."),
    )
    _prompt = caption_params["script_prompt"].strip()
    if not _prompt:
        st.error(t("A script prompt is required to generate scripts — enter "
                 "one, or switch Scripts to an uploaded file."))
    _sample = st.session_state.get("sample_script") or {}
    _have_sample = _sample.get("prompt") == _prompt and bool(_sample.get("script"))
    if st.button(t("🔄 New sample script") if _have_sample else t("✍️ Write a sample script"),
                 disabled=not _prompt,
                 help=t("Preview and Render row narrate one sample from this "
                      "prompt, written on first use and kept until the prompt "
                      "changes. The batch still writes a new script for every "
                      "video."), key="_ui_app_1813_" + str("🔄 New sample script" if _have_sample else "✍️ Write a sample script")):
        st.session_state.pop("sample_script", None)
        sample_script(_prompt)
        _sample = st.session_state.get("sample_script") or {}
        _have_sample = bool(_sample.get("script"))
    if _have_sample:
        st.caption(t("Sample for Preview and Render row (click Preview again after changing it): “{0}”", _sample['script']))
    if not settings.gemini_configured():
        st.warning(t("Vertex AI isn't configured — no scripts will be written "
                   "and those videos will render silent."))
caption_params["script_source"] = script_source
if voice_enabled and script_source == "upload":
    script_file = st.file_uploader(
        t("Script pool (.txt / .xlsx) — one script per line or per paragraph"),
        type=["txt", "md", "xlsx", "csv"],
        on_change=presets_ui.upload_changed, args=("scripts",),
        key=presets_ui.uploader_key("scripts"),
        help=t("Used only for rows with an empty `Voiceover` cell. Scripts are "
             "dealt round-robin so the spread across the batch is even, and "
             "each one rotates through the chosen voices. A .txt with blank "
             "lines between entries treats each paragraph as one script; "
             "without blank lines, one per line."),
    )
    script_file = presets_ui.with_saved("scripts", script_file, multiple=False)
    if script_file is not None:
        from speech.pool import parse_scripts as _parse_scripts
        _preview = _parse_scripts(script_file.getvalue(), script_file.name)
        if _preview:
            st.caption(t("**{0}** script(s) — first one: “{1}”", len(_preview), _preview[0][:90]))
        else:
            st.warning(t("No scripts found in `{0}`. Rows with an empty Voiceover cell will render silent.", script_file.name))

hashtag_file = None
if hashtag_source == "excel":
    hashtag_file = st.file_uploader(
        t("Hashtag sets (.xlsx) — one set per row in the first column"),
        type=["xlsx"],
        on_change=presets_ui.upload_changed, args=("hashtags",),
        key=presets_ui.uploader_key("hashtags"),
        help=t("e.g. a column of rows like '#asmr #satisfying #fyp'. They are "
             "cycled across the batch, so the spread is even."),
    )
    hashtag_file = presets_ui.with_saved("hashtags", hashtag_file, multiple=False)
    col_t, col_p = st.columns([1, 2])
    with col_t:
        st.download_button(
            t("⬇️ Sample file"),
            data=hashtag_template_bytes(),
            file_name="hashtags_sample.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help=t("A ready-to-edit workbook — replace the rows with your own."),
         key='_ui_app_sample_file')
    with col_p:
        if hashtag_file is None:
            st.warning(
                t("**No file yet** — the caption pool's hashtags will be used "
                "instead. Upload one, or switch the setting above.")
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
                        t("No hashtags found in the first column. Each row should "
                        "hold one set, e.g. `#asmr #satisfying #fyp`.")
                    )
                else:
                    st.success(t("**{0} hashtag set(s)** read from `{1}`.", len(parsed), hashtag_file.name))
                    st.caption(t("First few: ") + t(" · ".join(parsed[:3])))
            except Exception as exc:  # noqa: BLE001
                st.error(t("Couldn't read that file: {0}", exc))

if caption_mode == "generate":
    caption_params["generate_pool"] = True
    caption_params["force_new_pool"] = True
    caption_params["caption_theme"] = st.text_input(
        t("Caption theme"), value=settings.CAPTION_THEME, key=keep("caption_theme"),
        placeholder=t("e.g. satisfying ASMR clips promoting a skincare brand"),
        help=t("The single biggest lever on caption quality — be specific about "
             "the product and the audience."),
    )
    # One caption per video, never reused, so this needs to reach
    # `batches x rows` — checked against the real total in section 5 below,
    # where the batch count is known.
    _cap_max = settings.CAPTION_POOL_MAX
    if hashtag_source == "pool":
        col_cc, col_hh = st.columns(2)
        caption_params["caption_count"] = col_cc.number_input(
            t("Captions"), 50, _cap_max, 500, 50, key=keep("caption_count"))
        caption_params["hashtag_count"] = col_hh.number_input(
            t("Hashtag sets"), 25, settings.HASHTAG_POOL_MAX, 100, 25,
            key=keep("hashtag_count"))
    else:
        # Generating hashtag sets that an uploaded sheet would immediately
        # override is money spent on output nobody sees.
        caption_params["caption_count"] = st.number_input(
            t("Captions"), 50, _cap_max, 500, 50, key=keep("caption_count"))
        caption_params["hashtag_count"] = 0
        st.caption(
            t("Only captions will be generated — hashtags come from ")
            + (t("your uploaded file.") if hashtag_source == "excel"
               else t("the fixed call-to-action line above.") if fixed_tail
               else t("nowhere, by choice."))
        )
    # Same arithmetic the Setup page shows. Worth repeating here because this
    # is where a large batch gets planned, and a big pool is neither instant
    # nor free — 16,000 captions is ~160 model calls.
    _cc = int(caption_params["caption_count"])
    st.caption(
        t("~{0} model call(s), run {1} at a time — roughly ${2:.2f} of {3} usage.", max(1, _cc // 100), settings.CAPTION_CONCURRENCY, _cc / 2000 * 2, settings.GEMINI_POOL_MODEL)
    )
    if not caption_params["caption_theme"].strip():
        st.error(
            t("A caption theme is required to generate a pool — it is what the "
            "captions are about. Enter one, or switch to “Use the active "
            "caption pool”.")
        )
    if not settings.gemini_configured():
        st.warning(t("Vertex AI isn't configured — this stage will be skipped and "
                   "files will fall back to Headline names."))

st.subheader(t("5. Generate"))

# One sheet becomes `batches x rows` videos: the same rows rendered once per
# batch, each pass with a different promo video and different clip picks.
#
# Never below MAX_PROMO_VIDEOS, so the batch count can always reach one pass
# per promo (that is its default). The floor of 20 keeps the widgets usable if
# the promo cap is ever lowered. Also the ceiling the copies-per-promo solver
# below searches up to, since folders and passes share this range.
MAX_PASSES = max(20, MAX_PROMO_VIDEOS)

# Promo Alternate has no promos, so the copies-per-promo lever has no subject —
# the thing that distinguishes one pass from another here is the clip draw
# (variant_salt, which the worker already sets per batch). Dropping the third
# widget rather than relabelling it: reinterpreting "copies of one promo" as
# "copies of one sequence" would be a number nothing in the pipeline actually
# enforces, which is worse than not offering it.
cols = st.columns(2 if promo_alt else 3)
col_b, col_f = cols[0], cols[1]
col_c = None if promo_alt else cols[2]
# Default to one pass per promo, which is the pairing people expect: every row
# rendered once with every promo. With no promos there is no such pairing, so
# one pass — more is a deliberate ask for more variants.
_n_promos = max(1, len(promo_names))
_batches_default = 1 if promo_alt else _n_promos
# Keyed so a saved job can set it, which means it no longer re-defaults by
# itself when the promo count changes — follow_default puts that back.
presets_ui.follow_default("n_batches", _batches_default)
n_batches = col_b.number_input(
    t("Batches to render"), 1, MAX_PASSES, _batches_default, 1,
    disabled=not ready, key=keep("n_batches"),
    help=t("How many times the sheet is rendered. ")
         + (t("Each pass draws a different Promo Alternate sequence and different "
            "CTA clips for every row, so N passes give N different videos per "
            "row.") if promo_alt else
            t("Each pass uses the next promo video and picks different sample "
            "clips. Set this to the number of promos and every row is rendered "
            "once with every promo.")),
)
if not promo_alt and _n_promos > 1 and int(n_batches) < _n_promos:
    st.warning(
        t("Only {0} pass(es) but {1} promo videos — promos {2}–{3} would never be used. Set "
          "batches to {4} to render every row with every promo.", int(n_batches), _n_promos, int(n_batches) + 1, _n_promos, _n_promos)
    )
presets_ui.follow_default("n_folders", int(n_batches))
n_folders = col_f.number_input(
    t("Output folders"), 1, MAX_PASSES, int(n_batches), 1, disabled=not ready,
    key=keep("n_folders"),
    help=t("Finished videos are mixed evenly across this many Drive folders, so "
         "no folder is just one promo video. Usually the same as the batch "
         "count. A minimum when a copies limit is set beside it."),
)
max_per_folder = 0 if col_c is None else col_c.number_input(
    t("Max copies of one promo per folder"), 0, 10_000, 0, 1, disabled=not ready,
    key=keep("max_per_folder"),
    help=t("0 = off. When set, the output-folder count is raised (never lowered) "
         "until no folder holds more videos made from any one promo than this. "
         "More folders means SMALLER folders — if you need big folders at this "
         "limit, upload more promo videos instead."),
)

# The folder count actually submitted. `n_folders` above is a floor, not the
# answer: raising it is the only lever that shrinks the per-promo count without
# more promos, so a copies limit searches upward from whatever was typed.
#
# The widget is deliberately NOT written back through session_state — Streamlit
# refuses to have a widget's value changed after it was instantiated, and the
# derived number is shown in the caption below instead.
n_folders_eff = int(n_folders)
if max_per_folder and df is not None and len(df):
    n_folders_eff = next(
        (f for f in range(int(n_folders), MAX_PASSES + 1)
         if batching.max_copies_per_promo(
             int(n_batches), len(df), f, max(1, len(promo_names)))
         <= max_per_folder),
        # Unsatisfiable at any folder count: too few promos for a folder this
        # size. Keep what was typed; the warning below says how many are needed.
        int(n_folders),
    )
_PLATFORM_CHOICES = {
    "yt,tk": t("Both — yt.zip and tk.zip"),
    "tk": t("TikTok only — tk.zip (long names)"),
    "yt": t("YouTube only — yt.zip (short names)"),
}
upload_platforms = st.selectbox(
    t("Publish which names?"), list(_PLATFORM_CHOICES),
    format_func=option_labels(list(_PLATFORM_CHOICES), lambda key: t(_PLATFORM_CHOICES[key])), disabled=not ready,
    key=keep("upload_platforms"),
    help=t("Google allows one account 750 GB per rolling 24 hours into Drive, "
         "and each video is published under two names — so a very large batch "
         "cannot send both in one day. Pick one now and run this job again "
         "tomorrow with the other: the videos stay on the VM until both have "
         "been published, and the second run skips what already landed."),
)

if ready and df is not None:
    total_videos = len(df) * int(n_batches)
    promo_count = len(promo_names)
    _n_names = len(upload_platforms.split(","))
    note = (t("**{0:,} rows x {1} passes = {2:,} videos**, mixed across {3} folders, ", len(df), int(n_batches), total_videos, n_folders_eff)
            + ((t("each published twice under the SAME name — the fixed "
                "call-to-action leaves the two forms identical, so one "
                "platform's set is enough unless you want both folders.")
                if fixed_tail else t("each published under both names."))
               if _n_names == 2 else
               t("published as `{0}.zip` only.", upload_platforms)))
    if promo_alt:
        note += (t(" No promo video: every pass draws its own Promo Alternate sequence from the "
                   "{0} clip(s) uploaded, so the passes differ by their clips rather than by a "
                   "promo.", len(promo_alt_files or [])))
    elif promo_count > 1 and int(n_batches) == promo_count:
        note += (t(" Every row is rendered once with each of the {0} promos — all pairings, no repeats.", promo_count))
    elif promo_count and int(n_batches) > promo_count:
        note += (t(" Only {0} promo video(s), so they cycle across the {1} passes — each pairing occurs {2}x.", promo_count, int(n_batches), int(n_batches) // promo_count))
    st.caption(t(note))

    # What every folder will actually hold. Derived, not chosen: mix_into_folders
    # is stratified rather than shuffled, so this number was always guaranteed —
    # it just had nowhere to be read. See batching.max_copies_per_promo.
    _worst = batching.max_copies_per_promo(int(n_batches), len(df),
                                           n_folders_eff, max(1, promo_count))
    _per_folder = total_videos // max(1, n_folders_eff)
    st.caption(t("{0} folder(s) x ~{1:,} videos", n_folders_eff, _per_folder)
               + ("." if promo_alt else
                  t(", at most {0} copies of any one promo in any one folder.", _worst)))
    if n_folders_eff != int(n_folders):
        st.info(
            t("Output folders raised {0} → {1} to keep every folder at ≤{2} copies of one promo."
              " Folders are smaller as a result (~{3:,} videos each).", int(n_folders), n_folders_eff, int(max_per_folder), _per_folder)
        )
    elif max_per_folder and _worst > max_per_folder:
        # No folder count fixes this, so say which of the TWO factors is over
        # budget rather than guessing. _worst is their product:
        #   ceil(batches / promos)  how often one promo is reused across passes
        #                           — more folders cannot touch this at all.
        #   ceil(rows / folders)    how thinly one pass spreads — already at its
        #                           floor, since the solver searched to the max.
        _reuse = -(-int(n_batches) // max(1, promo_count))
        _floor = batching.max_copies_per_promo(int(n_batches), len(df),
                                               MAX_PASSES, max(1, promo_count))
        _need = -(-int(n_batches) // int(max_per_folder))
        st.warning(
            t("**{0} copies of one promo per folder, above your limit of {1}** — and no folder "
              "count fixes it: even at the {2}-folder maximum it is {3}.  \n", _worst, int(max_per_folder), MAX_PASSES, _floor)
            + (t("{0} promo(s) across {1} passes means each one is reused {2}x, so it lands at "
                 "least {3} times in some folder however the videos are split. Upload at least "
                 "{4} promo videos and set batches to match.", promo_count, int(n_batches), _reuse, _reuse, _need)
               if _reuse > max_per_folder else
               t("{0:,} sheet rows is too many to thin out across {1} folders. Use a shorter "
                 "sheet, or raise the limit.", len(df), MAX_PASSES))
        )

    # Captions are the other quantity that has to reach `batches x rows`: one
    # per video, never reused. Without this check the shortfall only surfaces
    # inside the worker, after the assets are staged and the batch queued —
    # and the number needed depends on the batch count set right here.
    if caption_mode == "generate":
        _planned = int(caption_params.get("caption_count") or 0)
        if _planned < total_videos:
            st.warning(
                t("**{0:,} captions for {1:,} videos.** Each video takes its own and none is "
                  "ever reused, so this batch would be refused before it rendered. Raise "
                  "*Captions* in section 4 to at least {2:,}.", _planned, total_videos, total_videos)
            )
    elif caption_mode == "existing" and _pool:
        _unused, _total_caps = store.pool_remaining(_pool["id"])
        if _unused < total_videos:
            st.warning(
                t("**The active pool has {0:,} captions left of {1:,}, but this batch needs "
                  "{2:,}.** A pool is a consumable and never starts over — generate a fresh one,"
                  " or render fewer batches.", _unused, _total_caps, total_videos)
            )

    if voice_enabled and script_source == "generate":
        # "Up to": rows with their own Voiceover or Screen_Text take no script.
        from captions.pool import SCRIPT_CHUNK_SIZE
        st.caption(
            t("Up to **{0:,} scripts** to write — ~{1:,} Gemini call(s), {2} at a time — then "
              "one voiceover per video, {3} at a time, before rendering starts.", total_videos, -(-total_videos // SCRIPT_CHUNK_SIZE), settings.CAPTION_CONCURRENCY, settings.VOICE_WORKERS)
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
            t("**About {0:,.0f} GB into Drive — over the 750 GB that one account may upload per "
              "rolling 24 hours.** The last {1:,} video(s) would fail with a rate-limit error "
              "and need requeueing tomorrow.", _EST_GB, total_videos - _fits)
            + (t("  \nPublishing **one** set of names instead would fit ({0:,.0f} GB) — run this "
                 "job again tomorrow for the other.", _EST_GB / 2) if _n_names == 2 else
               t("  \nRender fewer batches, or split this across two days."))
        )

    if total_videos > 3000:
        st.warning(
            t("{0:,} videos is a long run — roughly {1:.1f}–{2:.1f} hours of rendering. The VM "
              "must stay up for the whole job. It resumes if interrupted, but it won't finish "
              "until it's back.", total_videos, total_videos * 2 / 3600, total_videos * 3 / 3600)
        )

# Generation runs in the background worker, so the batch needs a name to find it
# by later and an address to report back to.
col_label, col_mail = st.columns(2)
batch_label = col_label.text_input(
    t("Batch name (optional)"), value="", disabled=not ready, key=keep("batch_label"),
    placeholder=t("e.g. asmr-week32-set1"),
    help=t("Shown on the Jobs page and in the notification email. Defaults to the "
         "Excel file name."),
)
drive_folder = st.text_input(
    t("Google Drive folder link (optional)"), value=settings.DRIVE_ROOT_FOLDER_ID, disabled=not ready,
    key=keep("drive_folder"),
    placeholder=t("https://drive.google.com/drive/folders/…"),
    help=t("Paste a folder link to send THIS batch somewhere specific. Leave "
         "blank to use the server's configured destination. It must sit inside "
         "a Shared Drive — a service account cannot write to a personal My Drive."),
)
if drive_folder.strip():
    from integrations import drive as _drv
    _did = _drv.extract_id(drive_folder)
    st.caption(t("→ uploading this batch to `{0}`", _did)
               + ("" if _drv.looks_like_shared_drive(_did)
                  else t(" (a folder — fine if it lives in a Shared Drive)")))

notify_email = col_mail.text_input(
    t("Notify email (optional)"), value=", ".join(settings.MAIL_TO), disabled=not ready,
    key=keep("notify_email"),
    placeholder=t("you@yourcompany.com"),
    help=t("Emailed when the batch finishes — on success and on failure. Leave "
         "blank to use the server default."),
)
if not settings.mail_configured():
    st.caption(
        t("⚠️ Email isn't configured on the server yet, so no notification will be "
        "sent — the Jobs page still shows live progress.")
    )

generate_clicked = st.button(
    t("🚀 Generate All Videos"), disabled=not ready, type="primary", width="stretch"
, key='_ui_app_generate_all_videos')

def _preview_workspace(tmp: Path) -> Workspace:
    """Stage what Preview and Render Row read. Every pool is copied, so a few
    gigabytes of gifs or background videos take a while — hence the bar.

    The bg-video pool IS staged here: the editor payload frames the row's
    chosen clip so the box is draggable in the preview."""
    args = (video_file, None if bg_image_source != "upload" else zip_file,
            cta_file, font_file, cta_video_slot_files, gif_files,
            bg_video_files, music_files, promo_alt_files)
    bar = ui_common.ByteProgress(staged_bytes(*args), "Copying files for the preview")
    try:
        if promo_source == "drive_folder":
            from integrations import drive

            entry = promo_drive_files[promo_choice]
            with st.spinner(t("Downloading selected promo video from Drive…")):
                drive.download_file(entry["id"], tmp / "input.mp4", entry["size"])
        return build_workspace(tmp, *args, progress=bar,
                               defer_promos=promo_source == "drive_folder")
    finally:
        bar.clear()


if preview_clicked and ready:
    with st.spinner(t("Rendering preview of row {0}…", preview_row)):
        with tempfile.TemporaryDirectory(prefix="bvg_preview_") as tmp:
            try:
                ws = _preview_workspace(Path(tmp))
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch,
                # so the preview shows the row's actual background — and the
                # same per-promo text, or the editor would offer no Headline at
                # all for a row whose only Headline comes from a grid. Promo N
                # is first rendered in batch N, so that is the deal shown.
                df_preview, _ = generator.assign_backgrounds(_for_preview(
                    text_grids.apply_overrides(df, grid_overrides, promo_choice)),
                    batch=promo_choice + 1)
                df_preview = apply_script_pool(
                    df_preview, int(preview_row), script_file,
                    caption_params.get("script_prompt", ""), config)
                payload = generator.build_editor_payload(
                    df_preview.iloc[int(preview_row) - 1], int(preview_row))
                st.session_state["preview_payload"] = payload
                st.session_state["preview_nonce"] = uuid.uuid4().hex
                st.session_state["preview_row"] = int(preview_row)
                st.session_state["preview_promo"] = (
                    promo_names[promo_choice] if len(promo_names) > 1 else "")
                # The payload was built WITH this row's saved edits applied,
                # so a later save reports changes relative to them — keep the
                # snapshot to merge against (and to detect reverts).
                st.session_state["preview_baseline_edits"] = dict(
                    (st.session_state.get("row_edits") or {}).get(int(preview_row), {})
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Preview failed")
                st.session_state.pop("preview_payload", None)
                st.error(t("Preview failed: {0}", exc))

if render_row_clicked and ready:
    st.session_state.pop("row_render", None)
    with st.spinner(
        t("Rendering row {0} for real… (a full render — slower than the static preview)", preview_row)
    ):
        with tempfile.TemporaryDirectory(prefix="bvg_rowrender_") as tmp:
            try:
                ws = _preview_workspace(Path(tmp))
                generator = make_generator(ws, config, Path(tmp) / "out")
                # Same deterministic background assignment as the real batch, so
                # this row renders with its actual background. df already carries
                # the saved editor edits (apply_saved_edits above), and the grids
                # are applied for the selected promo so this really is the
                # pairing the batch would produce — background included, from
                # the promo's first batch.
                df_render, bg_warnings = generator.assign_backgrounds(_for_preview(
                    text_grids.apply_overrides(df, grid_overrides, promo_choice)),
                    batch=promo_choice + 1)
                df_render = apply_script_pool(
                    df_render, int(preview_row), script_file,
                    caption_params.get("script_prompt", ""), config)
                for message in bg_warnings:
                    st.warning(t(message))
                res = generator.render_row(
                    int(preview_row), df_render.iloc[int(preview_row) - 1])
                if res.ok:
                    # Read the bytes before the TemporaryDirectory vanishes.
                    st.session_state["row_render"] = {
                        "row": int(preview_row),
                        "promo": (promo_names[promo_choice]
                                  if len(promo_names) > 1 else ""),
                        "name": res.filename,
                        "bytes": (Path(tmp) / "out" / res.filename).read_bytes(),
                        "warnings": list(res.warnings),
                    }
                else:
                    st.error(t("Row {0} render failed: {1}", preview_row, res.error))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Row render failed")
                st.error(t("Row render failed: {0}", exc))

row_render = st.session_state.get("row_render")
if row_render and not generate_clicked:
    # A 9:16 video at full content width is enormous — keep the player in a
    # narrow column with the caption/warnings/buttons beside it.
    col_vid, col_side = st.columns([2, 3], vertical_alignment="top")
    with col_vid:
        st.video(row_render["bytes"])
    with col_side:
        st.caption(
            t("Rendered video of row {0}", row_render['row'])
            + (t(" using promo **{0}**", row_render['promo']) if row_render.get("promo") else "")
            + t(" — exactly what the batch would produce for this pairing, "
              "including your saved edits.")
        )
        for message in row_render.get("warnings") or []:
            st.warning(t("Row {0}: {1}", row_render['row'], message))
        st.download_button(
            t("⬇️ Download this video"), data=row_render["bytes"],
            file_name=row_render["name"], mime="video/mp4",
         key='_ui_app_download_this_video')
        if st.button(t("✖ Close row video"), key='_ui_app_close_row_video'):
            st.session_state.pop("row_render", None)
            st.rerun()

if "preview_payload" in st.session_state and not generate_clicked:
    st.caption(
        (t("Promo: **{0}** · ", st.session_state.get('preview_promo')) if
         st.session_state.get("preview_promo") else "")
        + t("Row {0} preview (1080x1920) — drag the video, CTA, or texts to move them, drag the "
            "corner handle to resize, recolor texts, and add a background box; click “Save to "
            "Excel” in the panel to apply the changed values to that row for previews, "
            "generation, and the Excel download below.", st.session_state.get('preview_row', 1))
    )
    if grid_overrides:
        st.caption(
            t("The ")
            + t(", ".join(f"**{t(role)}**" for role in sorted(grid_overrides)))
            + t(" wording above is this promo's, from your per-promo sheet. What "
              "you change here — position, size, colour, background box — is "
              "saved against the row and so applies to every promo, which is "
              "the point: one design, different words.")
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
        t("Saved edits for row(s) {0} — applied to previews and generation. Download the updated"
          " sheet to keep your Excel in sync.", rows_txt)
    )
    col_dl, col_clear = st.columns([2, 1], vertical_alignment="center")
    col_dl.download_button(
        t("⬇️ Download updated Excel"),
        data=updated_excel_bytes(excel_file.getvalue(), row_edits),
        file_name=excel_file.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     key='_ui_app_download_updated_excel')
    if col_clear.button(t("🗑️ Discard saved edits"), key='_ui_app_discard_saved_edits'):
        st.session_state.pop("row_edits", None)
        st.rerun()

if generate_clicked and ready and caption_mode == "generate"         and not caption_params.get("caption_theme", "").strip():
    st.error(t("Not queued — set a caption theme first, or choose “Use the "
             "active caption pool”."))
    generate_clicked = False

if generate_clicked and ready and voice_enabled and script_source == "generate" \
        and not caption_params.get("script_prompt", "").strip():
    st.error(t("Not queued — write a script prompt, or switch Scripts to an "
             "uploaded file."))
    generate_clicked = False

if generate_clicked and ready and clip_source == "drive_folder" and not any(
    clip_params.get("clips_drive_folders")
    or [clip_params.get("clips_drive_folder")]
):
    # Queuing a job whose clips have nowhere to come from only moves the
    # failure into the worker, minutes later and on another page.
    st.error(t("Not queued — paste at least one Google Drive folder link for the "
             "clips, or choose a different clip source."))
    generate_clicked = False

# Same deferred-failure rule for the other two Drive-sourced pools: a blank
# link would run the (potentially long) earlier pipeline stages and then die.
if generate_clicked and ready and gif_source == "drive_folder" \
        and not clip_params.get("gifs_drive_folder"):
    st.error(t("Not queued — paste the Google Drive folder link for the GIFs, "
             "or switch the GIF source back to upload."))
    generate_clicked = False

if generate_clicked and ready and bg_image_source == "drive_folder" \
        and not clip_params.get("bg_images_drive_folder"):
    st.error(t("Not queued — paste the Google Drive folder link for the "
             "background images, or switch their source back to upload."))
    generate_clicked = False

if generate_clicked and ready and bg_video_source == "drive_folder" \
        and not clip_params.get("bg_videos_drive_folder"):
    st.error(t("Not queued — paste the Google Drive folder link for the "
             "background videos, or switch their source back to upload."))
    generate_clicked = False

if generate_clicked and ready and music_source == "drive_folder" \
        and not clip_params.get("music_drive_folder"):
    st.error(t("Not queued — paste the Google Drive folder link for the music, "
             "or switch its source back to upload."))
    generate_clicked = False

if generate_clicked and ready and grid_errors:
    # A grid whose headers do not resolve would render the wrong promo's words
    # onto thousands of videos and look entirely successful doing it. There is
    # no safe way to guess past it, so the batch does not start.
    st.error(
        t("Not queued — {0} problem(s) with the per-promo text sheet(s). Fix them in the "
          "“Per-promo heading, subheading and footer text” panel above, or remove the sheets to "
          "use the main Excel's text.", len(grid_errors))
    )
    generate_clicked = False

if generate_clicked and ready:
    # Submitting is a two-step dance: reserve an id, stage the uploads into its
    # folder, then insert the job row. Doing it the other way round would let
    # the worker claim a batch whose promo video is still being written.
    #
    # Everything below finishes in seconds to minutes, depending on how many
    # gigabytes are being copied — the progress bar says which. The rendering
    # itself happens in the worker process, so this browser tab can be closed
    # once the bar reports the batch queued.
    _progress = None
    try:
        # The sheet is written with any preview-editor edits baked in, so the
        # worker renders exactly what this page was showing. updated_excel_bytes
        # preserves the original workbook's formatting. The saved copy of this
        # job (presets.py) gets the same bytes, so loading it brings the edits
        # back.
        _row_edits = st.session_state.get("row_edits") or {}
        _sheet_bytes = updated_excel_bytes(excel_file.getvalue(), _row_edits)

        # All promo videos are staged; the runner picks one per batch.
        _staged = (
            promo_files,
            # Gated on the images' OWN source flag, like every pool below: a
            # ZIP left in the uploader must not shadow Drive.
            None if bg_image_source != "upload" else zip_file,
            cta_file, font_file,
            # Clips come from the scrape in chained mode; the pipeline
            # materialises them into the same cta_slot_N folders.
            None if clip_source != "upload" else cta_video_slot_files,
            # Gated on the GIF layer's OWN source, never on clip_source: a user
            # who fetches CTA clips from Drive but uploads their gifs would
            # otherwise lose every one of them silently — nothing downstream
            # checks for gifs, so the batch would render clean and gif-free.
            None if gif_source != "upload" else gif_files,
            # Same rule as the gifs: gated on this layer's OWN source flag,
            # never on anyone else's.
            None if bg_video_source != "upload" else bg_video_files,
            # And again for the music, which is the only pool read out of Drive
            # as AUDIO — see check_source(kind="audio").
            None if music_source != "upload" else music_files,
            # The Promo Alternate pool is upload-only: it has no Drive source of
            # its own, so there is no source flag to gate it on. Empty when the
            # mode is off, which is what makes a job's mode readable straight
            # off its assets folder.
            promo_alt_files,
        )
        # Exactly the files this job uses, by upload slot — what "Reuse a
        # previous job" loads back. Files a Drive source made irrelevant are
        # left out, the same way they are left out of the staging above.
        _files_for_save = {
            "excel": [presets.MemoryFile(excel_file.name, _sheet_bytes)
                      if _row_edits else excel_file],
            "promos": list(_staged[0] or []),
            "bg_zip": [_staged[1]],
            "cta_image": [cta_file],
            "font": [font_file],
            **{f"cta_clip_{i}": list(files or [])
               for i, files in enumerate(_staged[4] or [], start=1)},
            "gif_pool": list(_staged[5] or []),
            "bg_video_pool": list(_staged[6] or []),
            "music_pool": list(_staged[7] or []),
            "promo_alt_pool": list(_staged[8] or []),
            **{grid_keys[role]: [upload] for role, upload in grid_uploads.items()},
            "scripts": [script_file],
            "hashtags": [hashtag_file],
            "cta_captions": [cta_caption_file],
        }
        # One bar for both steps. Saving is usually quick or free: a file that
        # came from a saved job is already stored, and an identical re-upload
        # is kept once.
        _progress = ui_common.ByteProgress(
            staged_bytes(*_staged) + presets.bytes_to_save(_files_for_save),
            "Copying files into the job")

        job_id = store.new_job_id()
        store.make_job_dirs(job_id)
        assets = store.assets_dir(job_id)

        stage_uploads(assets, *_staged, progress=_progress,
                      defer_promos=promo_source == "drive_folder")

        if hashtag_file is not None:
            (assets / "hashtags.xlsx").write_bytes(hashtag_file.getvalue())
        if script_file is not None:
            # Keep the real extension: the parser dispatches on it.
            suffix = Path(script_file.name).suffix.lower() or ".txt"
            (assets / f"scripts{suffix}").write_bytes(script_file.getvalue())

        # Resolved to promo INDICES here, not staged as sheets: stage_uploads
        # renames every promo to input_N.mp4, so the filenames the columns were
        # matched on do not exist by the time the worker runs. Writes nothing
        # when no grids were uploaded.
        text_grids.write_overrides(assets, grid_overrides)

        (assets / "input.xlsx").write_bytes(_sheet_bytes)

        # Anything that has to be fetched or generated before rendering makes
        # this a pipeline job rather than a plain render — including a gif pool
        # that still has to come down from Drive.
        chained = (promo_source != "upload"
                   or clip_source != "upload" or gif_source != "upload"
                   or bg_image_source != "upload"
                   or bg_video_source != "upload"
                   or music_source != "upload"
                   or caption_mode == "generate")
        store.create_job(
            kind=store.KIND_PIPELINE if chained else store.KIND_RENDER,
            params={
                "render_config": asdict(config),
                "workers": int(workers),
                "batches": int(n_batches),
                # The EFFECTIVE count, which a copies limit may have raised
                # above the widget. The worker enforces nothing; it renders
                # into however many folders it is told.
                "folders": n_folders_eff,
                "make_zip": True,
                "upload_platforms": upload_platforms.split(","),
                "excel_name": excel_file.name,
                # Recorded because nothing else keeps them: the promos are
                # staged as input.mp4 / input_N.mp4, so this list is the only
                # way to read a job's per-promo text back against real names.
                "promo_names": promo_names,
                "promo_source": promo_source,
                "promos_drive_folder": promos_drive_folder,
                "promos_drive_files": promo_drive_files,
                "text_grids": sorted(grid_overrides),
                "clip_source": clip_source,
                "gif_source": gif_source,
                "bg_image_source": bg_image_source,
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
        if _progress is not None:
            _progress.clear()
        st.error(t("Could not queue the batch: {0}", exc))
    else:
        logger.info("Queued render job %s (%d rows)", job_id, len(df))
        st.session_state["last_job_id"] = job_id
        # The job is already queued; saving it for reuse comes second and must
        # never undo that. A failure here is reported and nothing more.
        _save_name = batch_label or Path(excel_file.name).stem
        _saved_note = ""
        try:
            _progress.phase("Saving this job so it can be reused")
            _saved = presets_ui.save_job(
                _save_name, _files_for_save, job_id=job_id,
                summary={"rows": len(df), "batches": int(n_batches),
                         "videos": len(df) * int(n_batches),
                         "folders": n_folders_eff},
                progress=_progress)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Saving job %s for reuse failed", job_id)
            st.warning(t("The batch is queued, but its settings could not be saved for reuse: {0}", exc))
        else:
            _saved_note = t(" Saved as **{0}** under ♻️ *Reuse a previous job* at the top of the page.", _save_name)
            if _saved.get("missing"):
                st.warning(
                    t("{0} file(s) from the loaded job had already been removed from the server,"
                      " so the saved copy is missing them.", len(_saved['missing'])))
        # The bar has done its job; the message below says the rest.
        _progress.clear()
        st.success(
            t("**Queued — {0:,} videos** ({1:,} rows x {2} batches). You can close this tab now;"
              " rendering carries on in the background", len(df) * int(n_batches), len(df), int(n_batches))
            + (t(" and an email goes to {0} when it's done.", notify_email.strip())
               if notify_email.strip() else ".")
            + t(_saved_note)
        )
        st.page_link("pages/1_Jobs.py", label=t("📋 Track progress on the Jobs page"),
                     icon="➡️")

if st.session_state.get("last_job_id"):
    job = store.get_job(st.session_state["last_job_id"])
    if job and job["status"] in (store.STATUS_QUEUED, store.STATUS_RUNNING):
        st.info(
            t("Batch `{0}` is {1}", job['label'] or job['id'], t(job['status']))
            + (f" — {stage_label(job['stage'])}" if job.get("stage") else "")
            + t(". Details are on the Jobs page.")
        )
