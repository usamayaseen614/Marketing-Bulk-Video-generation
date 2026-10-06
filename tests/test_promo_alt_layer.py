"""Promo Alternate: the pool that REPLACES the promo video.

The failure modes worth pinning here are the silent ones. A render length still
derived from a promo file produces videos that cut the narration off mid-word
and look fine in a thumbnail. A promo input left claimed but unpainted trips
_check_filter_inputs; one left claimed AND painted buries the whole sequence
under a still. An [vidA] anchor kept on the sequence truncates the output to the
clips instead of to the voice. And a duration of None makes still_args empty,
which _check_input_bounds turns into a RuntimeError on every row of the batch.

One real render plus a pixel probe covers the graph; the rest is command-shape,
duration and round-trip checks.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="paltest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from video_generator import (PROMO_ALT_SECONDS, RenderConfig, RowSpec,
                             VideoGenerator, find_ffmpeg)
from workspace import stage_uploads, workspace_from_dir

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="palt_"))


def mk(path: Path, expr: str, dur: float, audio: bool = False) -> Path:
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", expr]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
    cmd += ["-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return path


def duration(path: Path) -> float:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True)
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    assert m, proc.stderr[-800:]
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


class Upload:
    """The bytes-and-a-name shape stage_uploads takes from Streamlit."""

    def __init__(self, path: Path):
        self.name = path.name
        self._data = path.read_bytes()

    def getvalue(self) -> bytes:
        return self._data


bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(bg_dir / "b.png")   # magenta

# A promo that must NOT reach the output, in a colour nothing else uses, and
# deliberately much longer than the mode's fallback length so a render still
# anchored on it is obvious in the duration as well as in the pixels.
promo = mk(TMP / "promo.mp4", "color=c=0x101010:s=720x1280:r=30", 12.0)
# The alternate pool. Each is SHORTER than the target length, so covering it
# needs a real sequence rather than one clip.
POOL_RGB = {"g": (0, 255, 0), "r": (255, 0, 0), "b": (0, 0, 255)}
pool = [mk(TMP / f"alt_{c}.mp4", f"color=c=0x{h}:s=640x360:r=30", d)
        for c, h, d in (("g", "00FF00", 2.0), ("r", "FF0000", 3.0),
                        ("b", "0000FF", 3.0))]

cfg = RenderConfig(
    bg_color="#FF00FF", include_audio=False, preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    promo_alt=True, promo_alt_seconds=8.0,
)
gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "work", TMP / "out",
                     promo_alt_paths=pool)
assert not gen.input_warnings, gen.input_warnings
assert gen._promo_alt, "the mode should be on with a non-empty pool"

row = pd.Series({"BG_Image": "b.png"})
spec = RowSpec.from_row(row, 1)
gen._resolve_positions(spec)

# ---------- 1. the sequence: dealt against the CONFIGURED length ----------
assert spec.promo_alt_clips and all(c in pool for c in spec.promo_alt_clips)
assert len(spec.promo_alt_clips) == len(spec.promo_alt_clip_repeats)
assert len(spec.promo_alt_clips) > 1, "8s at 2-3s per clip needs a sequence"
# Every clip plays ONCE. A promo clip replayed to clear a dwell floor reads as
# a stutter, which is why this pool's floor is effectively zero.
assert set(spec.promo_alt_clip_repeats) == {1}, spec.promo_alt_clip_repeats
# Covers the target, and does not wildly overshoot it.
covered = sum(duration(c) for c in spec.promo_alt_clips)
assert covered >= 8.0, f"sequence covers only {covered:.1f}s of 8.0s"
assert covered < 8.0 + 3.0 + 0.5, f"overshot: {covered:.1f}s"
print(f"sequence: {len(spec.promo_alt_clips)} clip(s), {covered:.1f}s for 8.0s")

# With no promos to cycle, variant_salt is the ONLY thing that makes one batch
# differ from another — it is what "Batches to render" means in this mode. Check
# the mechanism directly rather than inferring it from rendered file sizes.
seqs = []
for salt in (0, 1, 2):
    cfg_s = RenderConfig(
        bg_color="#FF00FF", include_audio=False, preset="ultrafast", crf=30,
        video_x=90, video_y=300, video_w=900, video_h=900,
        promo_alt=True, promo_alt_seconds=8.0, variant_salt=salt,
    )
    g = VideoGenerator(cfg_s, bg_dir, promo, None, TMP / f"ws{salt}",
                       TMP / f"os{salt}", promo_alt_paths=pool)
    s = g._spec_for(row, 1)
    # Exactly what render_row does between _spec_for and _resolve_positions
    # (video_generator.py, "Repeated renders of one sheet must differ").
    if cfg_s.variant_salt:
        s.seed_salt = 1 * 1_000_003 + cfg_s.variant_salt
    g._resolve_positions(s)
    seqs.append([c.name for c in s.promo_alt_clips])
assert len(set(map(tuple, seqs))) > 1, f"every batch dealt the same sequence: {seqs}"
# ...and it is still reproducible: the same salt deals the same clips.
assert seqs[0] == [c.name for c in spec.promo_alt_clips], (seqs[0], spec.promo_alt_clips)
print(f"batches: salts 0/1/2 deal {len(set(map(tuple, seqs)))} distinct sequences")

# ---------- 2. the render length comes from the voice, never the promo ------
assert gen._probe_duration(promo) > 11.0, "the promo really is 12s"
assert gen._render_duration(spec) == 8.0, gen._render_duration(spec)
voiced = RowSpec.from_row(row, 1)
voiced.voice_duration = 21.5
assert gen._render_duration(voiced) == 21.5, "narration must define the length"
# Never None: still_args/clip_args go empty on None and _check_input_bounds
# then refuses every row of a batch that mixes audio with looping inputs.
assert gen._render_duration(RowSpec.from_row(row, 9)) is not None
print("length: 8.0s configured, 21.5s under narration, never None")

# ---------- 3. the command: promo dropped, sequence in its place ------------
base_png = TMP / "base.png"
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(base_png)
overlay_png = TMP / "ov.png"
Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(overlay_png)
cmd = gen.build_ffmpeg_command(spec, base_png, overlay_png, None, TMP / "x.mp4")
fc = cmd[cmd.index("-filter_complex") + 1]
n = sum(1 for a in cmd if a == "-i")
VideoGenerator._check_filter_inputs(fc, n)
VideoGenerator._check_input_bounds(cmd, False)
# The promo is DROPPED, not merely unpainted: _check_filter_inputs requires
# every declared input to be referenced exactly once, so leaving it claimed
# would raise — and leaving it painted would bury the sequence under it.
assert str(promo) not in cmd, "the promo must not be claimed as an input"
for clip in spec.promo_alt_clips:
    assert str(clip) in cmd, f"{clip.name} missing from the command"
# [vidB] — the promo's own z-ordered layer — is the joined sequence.
assert f"concat=n={len(spec.promo_alt_clips)}:v=1:a=0[vidB];" in fc, fc
# Contain-fit and padded transparent to the promo box, the gifs' treatment:
# the promo has always been fitted whole with the background showing through
# the leftover area, and concat rejects inputs of differing sizes.
for k, idx in enumerate(range(len(spec.promo_alt_clips))):
    # The whole per-clip branch, pinned to its own label — a check that does not
    # vary with k would pass on a graph that emitted one branch and N labels.
    assert (f"format=rgba,scale=900:900:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2,pad=900:900:'trunc((900-iw)/4)*2'"
            f":'trunc((900-ih)/4)*2':color=0x00000000,setsar=1[pa{k}];") in fc, fc
# No [vidA] anchor. Anchoring the composite on the sequence would cut the
# narration short every time the clips under-fill; the base still and the
# output both carry their own -t instead.
assert "[vidA]" not in fc, "the sequence must not anchor the composite"
assert "null[anchored];" in fc, fc
assert "-t" in cmd and f"{8.0:.3f}" in cmd, cmd
# Muted by default: no promo means `-map <promo>:a?` has no input, and picking
# one clip of the sequence to speak for all of them would be worse than silence.
print(f"command: {n} inputs, promo dropped, sequence is [vidB]")

# ---------- 4. the real render: the pool is on screen, the promo is not -----
res = gen.render_row(1, row, "palt.mp4")
assert res.ok, res.error
out = TMP / "out" / "palt.mp4"
got = duration(out)
assert abs(got - 8.0) < 0.35, f"rendered {got:.2f}s, expected the configured 8s"
frame_png = TMP / "frame.png"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "0.5", "-i", str(out), "-frames:v", "1", str(frame_png)],
               check=True, capture_output=True)
px = Image.open(frame_png).convert("RGB")
# At t=0.5s the FIRST clip of this row's sequence is on screen, centred in the
# 900x900 box at (90, 300). The box is 900 wide against a 640x360 clip fitted
# by height-limited contain, so the centre pixel is the clip's own colour.
centre = px.getpixel((90 + 450, 300 + 450))
want = POOL_RGB[spec.promo_alt_clips[0].stem.split("_")[1]]
# The pool colours are pure primaries and the promo is near-black, so this one
# probe distinguishes them: matching `want` here is also proof the promo (16,16,16)
# did not reach the output.
assert all(abs(a - b) < 40 for a, b in zip(centre, want)), \
    f"box centre {centre}, expected {want} from {spec.promo_alt_clips[0].name}"
# The transparent pad really is transparent — magenta background shows through
# above the fitted clip rather than a black bar.
above = px.getpixel((90 + 450, 300 + 20))
assert all(abs(a - b) < 40 for a, b in zip(above, (255, 0, 255))), \
    f"padding area is {above}, expected the magenta background to show through"
print(f"render: {got:.2f}s, box shows {spec.promo_alt_clips[0].name}, pad clear")

# An UNDER-FILLING sequence must not shorten the video. This is the whole
# reason [vidA] is gone: with the sequence anchoring the composite, a pool that
# runs out — the 40-clip cap, or a narration far longer than the clips — would
# end the render there and cut the voice off mid-word. The last frame holds
# instead (overlay's default eof_action), exactly as the CTA box's does.
short = RowSpec.from_row(row, 1)
gen._resolve_positions(short)
short.promo_alt_clips = [pool[0]]            # 2.0s against an 8.0s render
short.promo_alt_clip_repeats = [1]
cmd_s = gen.build_ffmpeg_command(short, base_png, overlay_png, None,
                                 TMP / "short.mp4")
VideoGenerator._check_filter_inputs(cmd_s[cmd_s.index("-filter_complex") + 1],
                                    sum(1 for a in cmd_s if a == "-i"))
subprocess.run(cmd_s, check=True, capture_output=True)
short_dur = duration(TMP / "short.mp4")
assert abs(short_dur - 8.0) < 0.35, \
    f"a 2s sequence produced a {short_dur:.2f}s video — the clips anchored it"
print(f"under-fill: 2.0s of clips still renders {short_dur:.2f}s, last frame holds")

# The headline claim, through the real voice path rather than a hand-set
# attribute: attach_voice binds the narration, _resolve_positions deals the
# sequence against it, and the output ends with the voice. A promo-shaped
# render length anywhere in that chain shows up here as the wrong duration.
VOICE_S = 14.0
voice_wav = TMP / "voice.wav"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "sine=frequency=330:sample_rate=24000", "-t", str(VOICE_S),
                str(voice_wav)], check=True, capture_output=True)
cfg_v = RenderConfig(
    bg_color="#FF00FF", preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    promo_alt=True, promo_alt_seconds=8.0,   # deliberately NOT the voice length
    voice_enabled=True,
)
gen_v = VideoGenerator(cfg_v, bg_dir, promo, None, TMP / "workv", TMP / "outv",
                       promo_alt_paths=pool)
row_v = pd.Series({"BG_Image": "b.png",
                   "Voiceover": "A narration far longer than the fallback."})
spec_v = RowSpec.from_row(row_v, 1)
gen_v.attach_voice(spec_v, {"wav": str(voice_wav), "duration": VOICE_S,
                            "words": ()})
assert spec_v.voice_duration == VOICE_S
gen_v._resolve_positions(spec_v)
assert gen_v._render_duration(spec_v) == VOICE_S, \
    "the narration, not the fallback, must set the length"
# The sequence was dealt against the VOICE, so it is longer than the 8s one.
assert sum(duration(c) for c in spec_v.promo_alt_clips) >= VOICE_S
assert len(spec_v.promo_alt_clips) > len(spec.promo_alt_clips), \
    "a longer narration must draw more clips"
# No 'cut short' warning: there is no promo for the script to outrun.
assert not any("cut short" in w for w in spec_v.warnings), spec_v.warnings

# ...and the mirror case. A row whose script never got synthesized has ALWAYS
# rendered silent, but with no promo the narration is also the LENGTH, so the
# same miss now cuts the video to the fallback instead of merely muting it. A
# 14s script coming out as an 8s video must not read as success.
spec_miss = RowSpec.from_row(row_v, 1)
gen_v.attach_voice(spec_miss, None)          # cache miss: no entry for this row
assert spec_miss.voice_duration == 0.0
assert gen_v._render_duration(spec_miss) == 8.0, "falls back, as specified"
assert any("fallback length" in w for w in spec_miss.warnings), spec_miss.warnings
# A row that never asked for narration is not warned — it is simply a row with
# no voiceover, which is what the fallback is for.
spec_quiet = RowSpec.from_row(pd.Series({"BG_Image": "b.png"}), 1)
gen_v.attach_voice(spec_quiet, None)
assert not any("fallback length" in w for w in spec_quiet.warnings), \
    spec_quiet.warnings
# build_ffmpeg_command runs _check_filter_inputs and _check_input_bounds itself
# (with audio_only= for the voice and music tracks, which a bare call here could
# not supply), so reaching this line already proves the indices line up.
cmd_v = gen_v.build_ffmpeg_command(spec_v, base_png, overlay_png, None,
                                   TMP / "xv.mp4")
assert str(voice_wav) in cmd_v, "the narration was not mixed in"
subprocess.run(cmd_v, check=True, capture_output=True)
voiced_dur = duration(TMP / "xv.mp4")
assert abs(voiced_dur - VOICE_S) < 0.4, \
    f"rendered {voiced_dur:.2f}s under a {VOICE_S}s narration"
print(f"voiceover: {len(spec_v.promo_alt_clips)} clips dealt to {VOICE_S}s, "
      f"rendered {voiced_dur:.2f}s")

# ---------- 5. preview + editor: the row's OWN first clip, not the pool's ---
# preview_editor.py reads DATA.video with no existence guard, so an omitted or
# unbuildable key does not hide the box — it throws and the whole editor stops
# rendering. It must survive a job that staged no promo at all.
payload = gen.build_editor_payload(row, 1)
assert payload["video"]["count"] == len(spec.promo_alt_clips), payload["video"]
assert payload["video"]["frame"].startswith("data:image"), "no poster frame"
assert (payload["video"]["w"], payload["video"]["h"]) == (900, 900)
# The poster frame is the clip this row plays FIRST, not whatever happened to
# sort first in the pool — otherwise the preview shows a video that never runs.
lead = gen._lead_promo_path(spec)
assert lead == spec.promo_alt_clips[0], lead
prev = gen.render_preview(row, 1)
assert prev.size == (1080, 1920)
assert all(abs(a - b) < 40 for a, b
           in zip(prev.getpixel((90 + 450, 300 + 450)), want)), \
    "the static preview must show the same clip the render does"
print(f"preview: poster frame is {lead.name}, count={payload['video']['count']}")

# ---------- 6. audio: muted by default, joined when asked for --------------
# NOT asserted against `cmd`: that generator has include_audio=False, so its
# -an comes from the pre-existing branch and would pass with the new one
# deleted. Build one with audio ENABLED and the clips muted — the only thing
# that can produce -an there is the alternate branch, because `-map <promo>:a?`
# has no promo input to name.
cfg_mute = RenderConfig(
    bg_color="#FF00FF", preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    promo_alt=True, promo_alt_seconds=8.0, promo_alt_audio=False,
)
gen_mute = VideoGenerator(cfg_mute, bg_dir, promo, None, TMP / "workmu",
                          TMP / "outmu", promo_alt_paths=pool)
spec_mute = RowSpec.from_row(row, 1)
gen_mute._resolve_positions(spec_mute)
cmd_mute = gen_mute.build_ffmpeg_command(spec_mute, base_png, overlay_png, None,
                                         TMP / "xm.mp4")
assert cfg_mute.include_audio, "this check is only meaningful with audio on"
assert "-an" in cmd_mute, "muted clips with no music or voice means a silent video"
assert not any(a.endswith(":a?") for a in cmd_mute), \
    "there is no promo input for `-map <promo>:a?` to name"
res_mute = gen_mute.render_row(1, row, "muted.mp4")
assert res_mute.ok, res_mute.error
probe = subprocess.run([FF, "-hide_banner", "-i",
                        str(TMP / "outmu" / "muted.mp4")],
                       capture_output=True, text=True).stderr
assert "Audio:" not in probe, "the muted render carries an audio stream"
loud = [mk(TMP / f"loud_{i}.mp4", f"color=c=0x00FF00:s=320x240:r=30", 3.0,
           audio=True) for i in (1, 2)]
cfg_a = RenderConfig(
    bg_color="#FF00FF", preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    promo_alt=True, promo_alt_seconds=8.0, promo_alt_audio=True,
)
gen_a = VideoGenerator(cfg_a, bg_dir, promo, None, TMP / "worka", TMP / "outa",
                       promo_alt_paths=loud)
assert not gen_a.input_warnings, gen_a.input_warnings
assert gen_a._promo_has_audio, "every clip has sound, so the leg is available"
spec_a = RowSpec.from_row(row, 1)
gen_a._resolve_positions(spec_a)
cmd_a = gen_a.build_ffmpeg_command(spec_a, base_png, overlay_png, None,
                                   TMP / "xa.mp4")
fc_a = cmd_a[cmd_a.index("-filter_complex") + 1]
VideoGenerator._check_filter_inputs(fc_a, sum(1 for a in cmd_a if a == "-i"))
assert "-an" not in cmd_a, "the clips' own audio was asked for"
assert ":v=0:a=1" in fc_a, "the clip audio must be joined, not sampled"
# Padded to the render length: `-shortest` would otherwise trim the PICTURE
# down to a sequence whose audio ran out early.
assert "apad=whole_dur=8.000" in fc_a, fc_a
res_a = gen_a.render_row(1, row, "palt_audio.mp4")
assert res_a.ok, res_a.error
assert abs(duration(TMP / "outa" / "palt_audio.mp4") - 8.0) < 0.35
print("audio: silent by default, joined + padded when kept")

# A pool where only SOME clips have sound cannot be concatenated with a=1 —
# FFmpeg refuses the graph. Fall back to silence and name the offender.
gen_m = VideoGenerator(cfg_a, bg_dir, promo, None, TMP / "workm", TMP / "outm",
                       promo_alt_paths=[loud[0], pool[0]])
assert not gen_m._promo_has_audio, "a mixed pool must not build the audio leg"
assert any("no audio track" in w and pool[0].name in w
           for w in gen_m.input_warnings), gen_m.input_warnings
print("audio: mixed pool falls back to silent, by name")

# ---------- 7. an empty pool is refused, not silently ignored --------------
try:
    VideoGenerator(cfg, bg_dir, promo, None, TMP / "worke", TMP / "oute",
                   promo_alt_paths=[])
except ValueError as exc:
    assert "Promo Alternate" in str(exc), exc
else:
    raise AssertionError("an empty pool with the mode on must raise")
# Off, the promo is used exactly as before — no new code path in the default.
cfg_off = RenderConfig(bg_color="#FF00FF", include_audio=False,
                       preset="ultrafast", crf=30, video_w=900, video_h=900)
gen_off = VideoGenerator(cfg_off, bg_dir, promo, None, TMP / "worko",
                         TMP / "outo", promo_alt_paths=pool)
assert not gen_off._promo_alt
spec_off = RowSpec.from_row(row, 1)
gen_off._resolve_positions(spec_off)
assert not spec_off.promo_alt_clips, "the pool must be inert with the mode off"
cmd_off = gen_off.build_ffmpeg_command(spec_off, base_png, overlay_png, None,
                                       TMP / "xo.mp4")
fc_off = cmd_off[cmd_off.index("-filter_complex") + 1]
VideoGenerator._check_filter_inputs(
    fc_off, sum(1 for a in cmd_off if a == "-i"))
assert "[vidA]" in fc_off and str(promo) in cmd_off, "the promo path is intact"
assert abs(gen_off._render_duration(spec_off) - 12.0) < 0.2
# And the editor payload carries no count, so the editor's `if (v.count)`
# checks read false exactly as they did before this existed.
assert "count" not in gen_off.build_editor_payload(row, 1)["video"]
print("guards: empty pool raises, mode off leaves the promo path untouched")

# ---------- 8. staging round-trip: a job with NO promo at all --------------
assets = TMP / "assets"
stage_uploads(assets, [], None, None, None, None, None, None, None,
              [Upload(p) for p in pool])
ws = workspace_from_dir(assets, TMP / "wsw")
assert len(ws.promo_alt_paths) == len(pool), ws.promo_alt_paths
assert not (assets / "input.mp4").exists(), "no promo should have been written"
# video_path falls back to the pool's first clip rather than to a file that was
# never written: it is what the has-a-video-stream guard and the preview poster
# frame read, and an Optional would have to be threaded through both.
assert ws.video_path in ws.promo_alt_paths, ws.video_path
assert ws.video_path.is_file()
# Sorted, not upload order — a job resumed after a crash must deal the same
# sequence it dealt the first time.
assert ws.promo_alt_paths == sorted(ws.promo_alt_paths)
# And the old contract is unchanged: no promo AND no alternate is still an error.
try:
    stage_uploads(TMP / "assets_empty", [], None, None, None)
except ValueError as exc:
    assert "promo" in str(exc).lower(), exc
else:
    raise AssertionError("staging nothing at all must still raise")
print(f"workspace: {len(ws.promo_alt_paths)} clip(s), no input.mp4, "
      f"video_path -> {ws.video_path.name}")

# ---------- 9. the two interactions most likely to break silently -----------
# Split-screen rewrites spec.video_* in _apply_split_layout, which runs BEFORE
# the sequence is dealt — so the clips must be padded to the PANEL, not to the
# sidebar box. An odd per-row Video_Width cell (ignored by split, but not by the
# free layout) would leave an odd pad size with no valid yuv420p chroma.
for crop in (False, True):
    cfg_sp = RenderConfig(
        bg_color="#FF00FF", include_audio=False, preset="ultrafast", crf=30,
        layout_mode="split", crop_to_panels=crop, split_panel_h=1200,
        promo_alt=True, promo_alt_seconds=5.0, cta_video_fill=True,
    )
    g_sp = VideoGenerator(cfg_sp, bg_dir, promo, None, TMP / f"wsp{crop}",
                          TMP / f"osp{crop}", cta_video_slots=[[pool[0]]],
                          promo_alt_paths=pool)
    row_sp = pd.Series({"BG_Image": "b.png", "Video_Width": 537})   # odd on purpose
    s_sp = g_sp._spec_for(row_sp, 1)
    g_sp._resolve_positions(s_sp)
    assert (s_sp.video_w, s_sp.video_h) == (540, 1200), (s_sp.video_w, s_sp.video_h)
    assert s_sp.video_w % 2 == 0 and s_sp.video_h % 2 == 0
    # Both boxes cycle: the panel plays the alternate pool, the other half the
    # CTA pool, each dealt to the same render length.
    assert s_sp.promo_alt_clips and len(s_sp.cta_video_clips) > 1
    r_sp = g_sp.render_row(1, row_sp, f"split{crop}.mp4")
    assert r_sp.ok, r_sp.error
    d_sp = duration(TMP / f"osp{crop}" / f"split{crop}.mp4")
    assert abs(d_sp - 5.0) < 0.4, d_sp
print("split-screen: panel box 540x1200, both halves cycle, crop on and off")

# Every layer at once, with the alternate clips' audio feeding split-audio under
# a music bed. This is what actually exercises the input-index arithmetic:
# build_ffmpeg_command runs _check_filter_inputs (every input referenced exactly
# once) and _check_input_bounds (every looping input bounded) internally, so a
# successful render here is the assertion.
loud2 = [mk(TMP / f"la{i}.mp4", "color=c=0x00FF00:s=480x270:r=30", 3.0, audio=True)
         for i in range(3)]
beds = [mk(TMP / f"bd{i}.mp4", "color=c=0x0000FF:s=480x270:r=30", 4.0)
        for i in range(2)]
tracks = []
for i in range(2):
    t = TMP / f"tr{i}.mp3"
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=220", "-t", "10", str(t)],
                   check=True, capture_output=True)
    tracks.append(t)
cfg_all = RenderConfig(
    bg_color="#FF00FF", preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    promo_alt=True, promo_alt_seconds=6.0, promo_alt_audio=True,
    music_volume=0.2, music_min_seconds=5.0, gif_min_seconds=3.0,
    bg_video_opacity=0.2, bg_video_min_seconds=4.0,
    cta_video_fill=True, split_audio=True, subliminal_targets=["Headline"],
)
g_all = VideoGenerator(cfg_all, bg_dir, promo, None, TMP / "wall", TMP / "oall",
                       cta_video_slots=[[pool[0]], [pool[1]]], gif_paths=pool,
                       bg_video_paths=beds, music_paths=tracks,
                       promo_alt_paths=loud2)
assert g_all._promo_has_audio, "every alternate clip here has sound"
row_all = pd.Series({"BG_Image": "b.png", "Headline": "Everything at once"})
s_all = g_all._spec_for(row_all, 1)
g_all._resolve_positions(s_all)
assert g_all._split_audio_tempos(s_all), \
    "split-audio must warp the joined alternate clips, not silently no-op"
r_all = g_all.render_row(1, row_all, "all.mp4")
assert r_all.ok, r_all.error
d_all = duration(TMP / "oall" / "all.mp4")
assert abs(d_all - 6.0) < 0.5, d_all
probe_all = subprocess.run([FF, "-hide_banner", "-i", str(TMP / "oall" / "all.mp4")],
                           capture_output=True, text=True).stderr
assert "Audio:" in probe_all, "the mixed track did not reach the output"
print(f"all layers: alt {len(s_all.promo_alt_clips)} + cta "
      f"{len(s_all.cta_video_clips)} + gif {len(s_all.gif_clips or [])} + bed "
      f"{len(s_all.bg_video_clips or [])} + music {len(s_all.music_clips or [])}"
      f" + subliminal, {d_all:.2f}s with audio")

print("ALL 9 TESTS PASSED")
