"""The translucent background-video layer: pool pick, z-order, alpha, plumbing.

The failure modes worth a check here do not announce themselves: a wrong input
index renders a DIFFERENT video with exit code 0; a dropped format=rgba makes
the layer fully opaque and buries everything under it; a wrong z-position
either buries the layer or veils the texts. One real render plus two pixel
probes covers the graph; the rest is command-shape and round-trip checks.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="bgvtest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from video_generator import (RenderConfig, RowSpec, VideoGenerator,
                             find_ffmpeg)
from workspace import stage_uploads, workspace_from_dir

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="bgvlayer_"))


def mk(path: Path, expr: str, dur: float) -> Path:
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", expr, "-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(path)],
        check=True, capture_output=True)
    return path


bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(bg_dir / "b.png")  # magenta
promo = mk(TMP / "promo.mp4", "color=c=0x101010:s=720x1280:r=30", 3.0)
# A pool of three. The green one is SHORTER than the promo — exercises
# -stream_loop -1; the colors only matter for the pixel probe below, which
# pins the pick to one clip via the seeded rng being deterministic.
pool = [mk(TMP / f"bed_{c}.mp4", f"color=c=0x{h}:s=640x360:r=30", d)
        for c, h, d in (("g", "00FF00", 2.0), ("r", "FF0000", 4.0),
                        ("b", "0000FF", 4.0))]

cfg = RenderConfig(
    bg_color="#FF00FF", include_audio=False, preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    bg_video_opacity=0.5,   # high enough that a pixel probe is unambiguous
    bg_video_min_seconds=1.0,  # short floor so a 3s promo needs a real sequence
)
gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "work", TMP / "out",
                     bg_video_paths=pool)
assert not gen.input_warnings, gen.input_warnings

row = pd.Series({"BG_Image": "b.png"})
spec = RowSpec.from_row(row, 1)
gen._resolve_positions(spec)
# The sequence: several clips, covering the promo, repeats aligned with them.
assert spec.bg_video_clips and all(c in pool for c in spec.bg_video_clips)
assert len(spec.bg_video_clips) == len(spec.bg_video_clip_repeats)
assert len(spec.bg_video_clips) > 1, "a 3s promo at a 1s floor needs a sequence"
assert (spec.bg_video_x, spec.bg_video_y) == (0, 0)
assert (spec.bg_video_w, spec.bg_video_h) == (1080, 1920)
print(f"sequence: {len(spec.bg_video_clips)} clip(s), "
      f"repeats {spec.bg_video_clip_repeats}")

# ---------- 1. the command: input claimed once, chain present, z right ----------
base_png = TMP / "b.png"
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(base_png)
overlay_png = TMP / "o.png"
Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(overlay_png)
cmd = gen.build_ffmpeg_command(spec, base_png, overlay_png, None, TMP / "x.mp4")
fc = cmd[cmd.index("-filter_complex") + 1]
n = sum(1 for a in cmd if a == "-i")
VideoGenerator._check_filter_inputs(fc, n)
assert "-stream_loop" in cmd, "short clips must repeat to clear the dwell floor"
assert "-1" not in cmd, "the sequence is finite — no unbounded -stream_loop -1"
for clip in spec.bg_video_clips:
    assert str(clip) in cmd, f"{clip.name} missing from the command"
# One -stream_loop per clip, and the repeat counts are the resolved ones.
loops = [int(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-stream_loop"]
assert loops == [r - 1 for r in spec.bg_video_clip_repeats], loops
if len(spec.bg_video_clips) > 1:
    assert f"concat=n={len(spec.bg_video_clips)}:v=1:a=0[bseq]" in fc, fc
assert "colorchannelmixer=aa=0.500" in fc
# Alpha: every clip carries format=rgba (concat rejects mixed pixel formats,
# and without it the alpha multiply lands on opaque yuv), and the multiply is
# applied ONCE, to the joined sequence.
for k in range(len(spec.bg_video_clips)):
    assert f"setsar=1,format=rgba[bv{k}];" in fc, fc
assert fc.count("colorchannelmixer") == 1, "one alpha multiply, after the concat"
_alpha_src = "[bseq]" if len(spec.bg_video_clips) > 1 else "[bv0]"
assert f"{_alpha_src}colorchannelmixer=aa=0.500[bgv];" in fc, fc
# Z-order: inside the sorted stack, above the promo and directly below texts.
assert fc.index("[vidB]overlay") < fc.index("[bgv]overlay"), \
    "the layer must stack ABOVE the promo video"
assert fc.index("[bgv]overlay") < fc.index("[2:v]overlay"), \
    "the layer must stack BELOW the texts (input 2 is the text overlay)"
print(f"command: {n} inputs, indices intact, layer above promo / below texts")

# ---------- 2. the render: alpha survives, veils the promo, bg blends ----------
res = gen.render_row(1, row, "bgv.mp4")
assert res.ok, res.error
frame_png = TMP / "frame.png"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", "1.0", "-i", str(TMP / "out" / "bgv.mp4"),
                "-frames:v", "1", str(frame_png)],
               check=True, capture_output=True)
px = Image.open(frame_png).convert("RGB")
# What the probes expect depends on which clip row 1's seeded rng picked.
# At t=1.0s the FIRST clip of the sequence is on screen (every clip's dwell
# clears 1s here), so that is the colour the blend must show.
bed_rgb = {"g": (0, 255, 0), "r": (255, 0, 0), "b": (0, 0, 255)}[
    spec.bg_video_clips[0].stem.split("_")[1]]
corner = px.getpixel((20, 1900))     # background area: magenta+bed at 50%
want = tuple((255 * a + b) // 2 for a, b in zip((1, 0, 1), bed_rgb))
assert all(abs(c - w) <= 28 for c, w in zip(corner, want)), \
    f"corner {corner}: expected ~{want} blend, alpha or z-order is wrong"
inside = px.getpixel((540, 750))     # inside the promo box: promo+bed at 50%
want_in = tuple((16 + b) // 2 for b in bed_rgb)
assert all(abs(c - w) <= 28 for c, w in zip(inside, want_in)), \
    f"promo box {inside}: expected ~{want_in} — the layer must VEIL the promo"
print(f"render: corner {corner} and promo box {inside} both carry the veil")

# The point of the sequence: the bed CHANGES part-way through the video. Probe
# just past the first clip's dwell and expect the second clip's colour.
import subprocess as _sp
_first_dur = 2.0 if spec.bg_video_clips[0].stem.endswith("_g") else 4.0
_switch = _first_dur * spec.bg_video_clip_repeats[0]
assert _switch < 3.0, "the first clip must end inside the 3s promo to test this"
later_png = TMP / "frame_late.png"
_sp.run([FF, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{_switch + 0.4:.2f}", "-i", str(TMP / "out" / "bgv.mp4"),
         "-frames:v", "1", str(later_png)], check=True, capture_output=True)
later = Image.open(later_png).convert("RGB").getpixel((20, 1900))
second_rgb = {"g": (0, 255, 0), "r": (255, 0, 0), "b": (0, 0, 255)}[
    spec.bg_video_clips[1].stem.split("_")[1]]
want_2 = tuple((255 * a + b) // 2 for a, b in zip((1, 0, 1), second_rgb))
assert all(abs(c - w) <= 28 for c, w in zip(later, want_2)), \
    f"at {_switch + 0.4:.1f}s expected the SECOND clip (~{want_2}), got {later}"
assert later != corner, "the bed never changed — the sequence is not cycling"
print(f"cycle: bed switched at {_switch:g}s — {corner} -> {later} "
      f"({spec.bg_video_clips[0].name} -> {spec.bg_video_clips[1].name})")

# ---------- 2b. the dwell floor and the deck ----------
# Every clip is used once before any repeats (deck deal, not independent picks).
long_cfg = RenderConfig(bg_color="#FF00FF", include_audio=False,
                        preset="ultrafast", crf=30, bg_video_opacity=0.5,
                        bg_video_min_seconds=1.0)
long_promo = mk(TMP / "promo_long.mp4", "color=c=0x101010:s=320x240:r=30", 9.0)
deck_gen = VideoGenerator(long_cfg, bg_dir, long_promo, None, TMP / "wd",
                          TMP / "od", bg_video_paths=pool)
dspec = RowSpec.from_row(row, 7)
deck_gen._resolve_positions(dspec)
first_pass = dspec.bg_video_clips[:len(pool)]
assert len(set(first_pass)) == len(pool),     f"the deck must use every clip before repeating: {[c.name for c in first_pass]}"
# The dwell floor is a minimum, never a cut: the 2s clip under a 3s floor plays
# twice, the 4s clips play once.
floor_cfg = RenderConfig(bg_color="#FF00FF", include_audio=False,
                         preset="ultrafast", crf=30, bg_video_opacity=0.5,
                         bg_video_min_seconds=3.0)
fgen = VideoGenerator(floor_cfg, bg_dir, long_promo, None, TMP / "wf2",
                      TMP / "of2", bg_video_paths=pool)
fspec = RowSpec.from_row(row, 7)
fgen._resolve_positions(fspec)
for clip, reps in zip(fspec.bg_video_clips, fspec.bg_video_clip_repeats):
    assert reps == (2 if clip.stem.endswith("_g") else 1), (clip.name, reps)
print(f"deck: {len(pool)} clips used before any repeat; 2s clip repeats twice "
      f"under a 3s floor")

# ---------- 3. per-row sequence: deterministic, varies across rows ----------
gen2 = VideoGenerator(cfg, bg_dir, promo, None, TMP / "w2", TMP / "o2",
                      bg_video_paths=list(pool))
picks, picks2 = [], []
for r in range(1, 21):
    s1 = RowSpec.from_row(row, r)
    gen._resolve_positions(s1)
    s2 = RowSpec.from_row(row, r)
    gen2._resolve_positions(s2)
    picks.append(tuple(s1.bg_video_clips))
    picks2.append(tuple(s2.bg_video_clips))
assert picks == picks2, "the sequence must be reproducible across instances"
assert len(set(picks)) > 1, "20 rows must not all get the same sequence"
print(f"sequence: reproducible, {len(set(picks))} distinct orders across 20 rows")

# ---------- 4. per-row Excel box override reaches the command ----------
orow = pd.Series({"BG_Image": "b.png", "BG_Video_X": 100, "BG_Video_Y": 200,
                  "BG_Video_Width": 400, "BG_Video_Height": 300})
ospec = RowSpec.from_row(orow, 1)
gen._resolve_positions(ospec)
ocmd = gen.build_ffmpeg_command(ospec, base_png, overlay_png, None, TMP / "y.mp4")
ofc = ocmd[ocmd.index("-filter_complex") + 1]
assert "scale=400:300" in ofc and "[bgv]overlay=100:200" in ofc
print("override: BG_Video_* cells drive the box in the filter graph")

# ---------- 5. opacity 0 or empty pool = the layer vanishes ----------
off = VideoGenerator(RenderConfig(bg_color="#FF00FF", include_audio=False,
                                  preset="ultrafast", crf=30,
                                  bg_video_opacity=0.0),
                     bg_dir, promo, None, TMP / "w3", TMP / "o3",
                     bg_video_paths=pool)
ospec0 = RowSpec.from_row(row, 1)
off._resolve_positions(ospec0)
zcmd = off.build_ffmpeg_command(ospec0, base_png, overlay_png, None, TMP / "z.mp4")
assert "[bgv]" not in zcmd[zcmd.index("-filter_complex") + 1]
none = VideoGenerator(cfg, bg_dir, promo, None, TMP / "w4", TMP / "o4")
nspec = RowSpec.from_row(row, 1)
none._resolve_positions(nspec)
ncmd = none.build_ffmpeg_command(nspec, base_png, overlay_png, None, TMP / "q.mp4")
assert "[bgv]" not in ncmd[ncmd.index("-filter_complex") + 1]
assert "-stream_loop" not in ncmd
print("opacity 0 / empty pool: no input claimed, no filter emitted")

# ---------- 6. an audio-only 'video' is dropped with a warning, not fatal ----
audio_only = TMP / "noise.mp4"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "anullsrc=r=8000:cl=mono", "-t", "1", "-c:a", "aac",
                str(audio_only)], check=True, capture_output=True)
guard = VideoGenerator(cfg, bg_dir, promo, None, TMP / "w5", TMP / "o5",
                       bg_video_paths=[pool[0], audio_only])
assert guard.bg_video_paths == [pool[0]]
assert any("noise.mp4" in w for w in guard.input_warnings)
print("guard: audio-only clip dropped with a warning, pool keeps working")

# ---------- 7. staging and discovery round-trip ----------
class Fake:
    def __init__(self, path):
        self.name = Path(path).name
        self._data = Path(path).read_bytes()

    def getvalue(self):
        return self._data


staged = TMP / "assets"
stage_uploads(staged, Fake(promo), None, None, None, None, None,
              [Fake(p) for p in pool])
ws = workspace_from_dir(staged, TMP / "wswork")
assert [p.name for p in ws.bg_video_paths] == sorted(p.name for p in pool)
assert all(p.parent.name == "bg_videos" for p in ws.bg_video_paths)
# absent pool -> empty list, so old jobs rehydrate fine
bare = TMP / "assets_bare"
stage_uploads(bare, Fake(promo), None, None, None, None, None)
assert workspace_from_dir(bare, TMP / "wswork2").bg_video_paths == []
print("staging: pool survives the round trip sorted; absent pool = []")

# ---------- 8. the editor payload carries the draggable box ----------
payload = gen.build_editor_payload(row, 1)
bv = payload["bg_video"]
assert (bv["x"], bv["y"], bv["w"], bv["h"]) == (0, 0, 1080, 1920)
assert bv["opacity"] == 0.5 and bv["count"] == len(spec.bg_video_clips)
assert bv["frame"].startswith("data:image/")
nop = none.build_editor_payload(row, 1)
assert "bg_video" not in nop
print("editor: payload ships the box, poster frame, opacity, and pool count")

print()
print("ALL BG VIDEO LAYER CHECKS PASSED")
