"""
workspace.py — uploaded assets materialized on disk.

These helpers live outside app.py because the background worker needs them too,
and importing app.py would execute the entire Streamlit UI script as a side
effect. Nothing here imports streamlit, so it is safe to use headlessly.

Two callers, one layout:

  * Preview / Render Row stage into a TemporaryDirectory that is deleted the
    moment the interaction ends.
  * A submitted job stages into its persistent `assets/` folder, which has to
    outlive the HTTP request that created it — the worker reconstructs the
    Workspace from that folder later, possibly after a restart.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# How many promo videos one render may cycle through, one per batch.
#
# Kept in step with the "Batches to render" and "Output folders" limits in
# app.py: the batch count DEFAULTS to the number of promos uploaded, so a cap
# here above those widgets' max_value makes Streamlit throw on the upload that
# crosses it — the page dies rather than the value being clamped. See
# MAX_PASSES there, which is derived from this.
MAX_PROMO_VIDEOS = 20

# What counts as a clip once it is sitting on disk. One definition, because
# three places have to agree: the Drive downloader deciding what to fetch, the
# pipeline gathering a clip pool, and this module handing slots to the
# renderer. They diverged once — Drive fetched .mov files the pool then ignored
# — and the symptom was clips that vanished between two green log lines.
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

# The same definition for the music bed, and it has to be kept in step for the
# same reason VIDEO_SUFFIXES does — the Drive downloader, the pipeline stage and
# this module all decide "is this one of mine?" from it, and a set that only two
# of the three agree on shows up as tracks vanishing between two green log lines.
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
                  ".flac", ".wma"}


def is_video(path: Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


def is_audio(path: Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_SUFFIXES


@dataclass
class Workspace:
    """Uploaded assets materialized on disk."""
    bg_dir: Path
    video_path: Path
    cta_path: Optional[Path]
    font_path: Optional[Path]
    work_dir: Path
    cta_video_slots: list = field(default_factory=list)
    # The GIF layer's pool. FLAT, not slotted: gifs have no position semantics
    # and the sequence length is derived from the promo's duration, so there is
    # nothing for slots to address. Lives in its own `gifs/` folder rather than
    # a `*_slot_N` sibling — workspace_from_dir parses a trailing integer off
    # every `cta_slot_*` match, and a colliding prefix would raise ValueError
    # for every job in the batch.
    gif_paths: list = field(default_factory=list)
    # Every promo video uploaded, in order. A multi-batch render uses a
    # different one per batch; `video_path` is the first, so single-promo
    # callers (Preview, Render Row) are unaffected.
    video_paths: list = field(default_factory=list)
    # The translucent background-video pool. Flat like the gif pool, and
    # sequenced the same way: clips play back-to-back (seeded per row) for the
    # length of each video. Drawn full-canvas by default, always above the
    # promo/gif/CTA layers and below the texts.
    bg_video_paths: list = field(default_factory=list)
    # The music pool. Flat and sequenced like the two above, but heard rather
    # than seen: tracks play back-to-back under the promo's own audio, mixed at
    # RenderConfig.music_volume.
    music_paths: list = field(default_factory=list)

    def promo_for_batch(self, batch_index: int) -> Path:
        """The promo video for batch `batch_index` (0-based).

        Cycles when fewer videos were uploaded than there are batches, so
        asking for 10 batches with 3 promos is a sensible request rather than
        an error."""
        if not self.video_paths:
            return self.video_path
        return self.video_paths[batch_index % len(self.video_paths)]


def stage_uploads(dest: Path, video_file, zip_file, cta_file, font_file,
                  cta_video_slot_files=None, gif_files=None,
                  bg_video_files=None, music_files=None) -> None:
    """Write the in-memory uploads to disk where FFmpeg/PIL can read them.

    `video_file` may be a single upload or a list of up to MAX_PROMO_VIDEOS —
    a multi-batch render uses a different promo video per batch."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    promos = video_file if isinstance(video_file, (list, tuple)) else [video_file]
    promos = [f for f in promos if f is not None][:MAX_PROMO_VIDEOS]
    if not promos:
        raise ValueError("At least one promo video is required.")
    for i, promo in enumerate(promos, start=1):
        # input.mp4 stays the first one's name so anything reading a single
        # promo keeps working unchanged.
        name = "input.mp4" if i == 1 else f"input_{i}.mp4"
        (dest / name).write_bytes(promo.getvalue())

    # The CTA image is optional — no file written when none was uploaded.
    if cta_file is not None:
        (dest / "cta.png").write_bytes(cta_file.getvalue())

    # Backgrounds are optional — without a ZIP the folder stays empty and rows
    # render on the configured solid background color.
    bg_dir = dest / "backgrounds"
    bg_dir.mkdir(exist_ok=True)
    if zip_file is not None:
        with zipfile.ZipFile(io.BytesIO(zip_file.getvalue())) as zf:
            zf.extractall(bg_dir)

    if font_file is not None:
        suffix = Path(font_file.name).suffix.lower()
        (dest / f"custom_font{suffix}").write_bytes(font_file.getvalue())

    # One sub-folder per CTA slot, keeping each sample's original filename so the
    # Excel CTA_Clip_<n> cells can pin one by name. Empty slots still get their
    # folder: the slot count has to survive the round-trip through the folder or
    # the per-slot speeds in RenderConfig would shift onto the wrong clips.
    for i, files in enumerate(cta_video_slot_files or [], start=1):
        slot_dir = dest / f"cta_slot_{i}"
        slot_dir.mkdir(parents=True, exist_ok=True)
        for f in files or []:
            (slot_dir / Path(f.name).name).write_bytes(f.getvalue())

    # One flat folder for the gif pool. Always created, so a job resumed after
    # a restart can tell "no gifs were uploaded" from "the folder is missing".
    gif_dir = dest / "gifs"
    gif_dir.mkdir(parents=True, exist_ok=True)
    for f in gif_files or []:
        if f is not None:
            (gif_dir / Path(f.name).name).write_bytes(f.getvalue())

    # The background-video pool: one flat folder, same shape as the gifs.
    # NOT inside backgrounds/ (_build_bg_index only sees images there) and not
    # matching the input_*.mp4 / cta_slot_* globs whose sort keys parse an
    # integer. Always created, so the Drive pipeline stage and rehydration
    # agree on where the pool lives even when it arrives empty.
    bgv_dir = dest / "bg_videos"
    bgv_dir.mkdir(parents=True, exist_ok=True)
    for f in bg_video_files or []:
        if f is not None:
            (bgv_dir / Path(f.name).name).write_bytes(f.getvalue())

    # The music pool: one flat folder, same shape again. Its own folder rather
    # than sharing bg_videos/ because the two are filtered by different suffix
    # sets — a stray .mp4 in here would be silently ignored, and a .mp3 there.
    music_dir = dest / "music"
    music_dir.mkdir(parents=True, exist_ok=True)
    for f in music_files or []:
        if f is not None:
            (music_dir / Path(f.name).name).write_bytes(f.getvalue())


def workspace_from_dir(assets: Path, work_dir: Path) -> Workspace:
    """Rebuild a Workspace by reading a folder staged by stage_uploads().

    Clip pools come back in sorted filename order rather than upload order, so a
    job resumed after a crash sees the pools exactly as it saw them the first
    time — the per-row seeded clip choice stays reproducible across restarts."""
    assets = Path(assets)
    bg_dir = assets / "backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)

    cta_path = assets / "cta.png"
    fonts = sorted(assets.glob("custom_font.*"))

    slot_dirs = sorted(
        (p for p in assets.glob("cta_slot_*") if p.is_dir()),
        key=lambda p: int(p.name.rsplit("_", 1)[1]),
    )
    # Only actual videos: a slot folder can pick up junk (a Thumbs.db, a
    # half-written download) and everything in it is handed straight to FFmpeg.
    cta_video_slots = [
        sorted(f for f in slot.iterdir() if f.is_file() and is_video(f))
        for slot in slot_dirs
    ]

    # The gif pool, in sorted filename order for the same reason the clip pools
    # are: a job resumed after a crash must see the pool exactly as it did the
    # first time, or the seeded per-row sequence stops being reproducible.
    gif_dir = assets / "gifs"
    gif_paths = sorted(
        (f for f in gif_dir.iterdir() if f.is_file() and is_video(f))
    ) if gif_dir.is_dir() else []

    # input.mp4 first, then input_2.mp4 … input_10.mp4 in numeric order.
    video_paths = [assets / "input.mp4"] if (assets / "input.mp4").is_file() else []
    video_paths += sorted(
        (p for p in assets.glob("input_*.mp4") if p.is_file()),
        key=lambda p: int(p.stem.rsplit("_", 1)[1]),
    )

    # The background-video pool, sorted for the same reproducibility reason.
    bgv_dir = assets / "bg_videos"
    bg_video_paths = sorted(
        (f for f in bgv_dir.iterdir() if f.is_file() and is_video(f))
    ) if bgv_dir.is_dir() else []

    # The music pool, sorted for the same reproducibility reason.
    music_dir = assets / "music"
    music_paths = sorted(
        (f for f in music_dir.iterdir() if f.is_file() and is_audio(f))
    ) if music_dir.is_dir() else []

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    return Workspace(
        bg_dir=bg_dir,
        video_path=video_paths[0] if video_paths else assets / "input.mp4",
        cta_path=cta_path if cta_path.is_file() else None,
        font_path=fonts[0] if fonts else None,
        work_dir=work_dir,
        cta_video_slots=cta_video_slots,
        gif_paths=gif_paths,
        video_paths=video_paths,
        bg_video_paths=bg_video_paths,
        music_paths=music_paths,
    )


def build_workspace(tmp: Path, video_file, zip_file, cta_file, font_file,
                    cta_video_slot_files=None, gif_files=None,
                    bg_video_files=None, music_files=None) -> Workspace:
    """Stage the uploads into `tmp` and return the Workspace over them."""
    stage_uploads(tmp, video_file, zip_file, cta_file, font_file,
                  cta_video_slot_files, gif_files, bg_video_files, music_files)
    return workspace_from_dir(tmp, tmp / "work")
