"""The GIF layer: dwell floor, contain-fit, and the input-index guard.

Three things here are worth more than the rest, because each covers a failure
that does NOT announce itself:

  * the dwell floor is computed from the VIDEO stream's duration, not the
    container header — an mp4 whose audio outlasts its video reports the audio
    length and would silently under-repeat;
  * contain-fit must fill the box in BOTH directions (a small gif is enlarged),
    while the editor payload alone stays capped at the gif's natural size — one
    helper keeping two rules apart, and no existing preview helper does either;
  * an off-by-one in the FFmpeg input indices renders a DIFFERENT video with
    exit code 0 and empty stderr, so only an explicit invariant catches it.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="giftest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

import video_generator as vg
from video_generator import (GIF_MAX_TOTAL_CLIPS, MAX_TOTAL_FFMPEG_INPUTS,
                             RenderConfig, RowSpec, VideoGenerator,
                             _probe_video_duration, find_ffmpeg, gif_repeats)
from workspace import stage_uploads, workspace_from_dir

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="giflayer_"))


def mk(path: Path, expr: str, dur: float, audio: float = 0.0) -> Path:
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", expr]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={audio}",
                "-c:a", "aac"]
    cmd += ["-t", str(max(dur, audio)), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


# ---------- 1. the dwell floor arithmetic ----------
# The user's own example is the second row: a 3-second gif plays TWICE, for six
# seconds. It is a floor, never a cut.
CASES = [(1.0, 5, 5.0), (3.0, 2, 6.0), (5.0, 1, 5.0), (7.0, 1, 7.0),
         (0.5, 10, 5.0), (2.5, 2, 5.0)]
for dur, want_reps, want_screen in CASES:
    got = gif_repeats(dur, 5.0)
    assert got == want_reps, f"{dur}s -> {got} reps, expected {want_reps}"
    assert abs(dur * got - want_screen) < 1e-9
    assert dur * got >= 5.0 - 1e-9, f"{dur}s misses the floor"
print("dwell floor: 3s gif plays twice (6s); 5.0s exactly plays once; "
      "longer gifs play once in full")

# Unmeasurable must degrade to a single play, NOT to an infinite -stream_loop:
# an unbounded input with no -t was measured still growing past 30s of CPU for a
# 20-second output.
assert gif_repeats(None, 5.0) == 1
assert gif_repeats(0.0, 5.0) == 1, "a zero duration must not reach ceil(x/0)"
assert gif_repeats(-1.0, 5.0) == 1
print("unmeasurable / zero duration degrades to one play (no divide by zero)")

# ---------- 2. duration comes from the video stream, not the container ----------
av = mk(TMP / "av_mismatch.mp4", "color=c=red:s=320x240:r=30:d=3", 3.0, audio=9.0)
container = VideoGenerator._DURATION_RE.search(
    subprocess.run([FF, "-hide_banner", "-i", str(av)],
                   capture_output=True, text=True).stderr)
h, m, s = container.groups()
container_dur = int(h) * 3600 + int(m) * 60 + float(s)
stream_dur = _probe_video_duration(FF, av)
assert abs(container_dur - 9.0) < 0.2, container_dur
assert abs(stream_dur - 3.0) < 0.2, stream_dur
assert gif_repeats(container_dur, 5.0) == 1, "container duration under-repeats"
assert gif_repeats(stream_dur, 5.0) == 2, "video duration must give 2 repeats"
print(f"A/V mismatch: header says {container_dur:.1f}s, video is "
      f"{stream_dur:.1f}s -> 2 repeats, not 1")

# ---------- 3. contain-fit fills the box, up as well as down ----------
bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(bg_dir / "b.png")
promo = mk(TMP / "promo.mp4", "color=c=0x101010:s=720x1280:r=30", 20.0)

gif_dir = TMP / "gifs"
gif_dir.mkdir()
POOL = [
    ("tiny",  "color=c=0x00FF00:s=100x80:r=30",   3.0),   # smaller than the box
    ("huge",  "color=c=0xFF0000:s=1280x720:r=30", 1.0),   # much larger
    ("tall",  "color=c=0x0000FF:s=200x900:r=30",  5.0),
    ("wide",  "color=c=0xFFAA00:s=1200x120:r=30", 7.0),
]
gif_paths = [mk(gif_dir / f"{n}.mp4", e, d) for n, e, d in POOL]

cfg = RenderConfig(
    bg_color="#FF00FF",
    video_x=90, video_y=100, video_w=900, video_h=400,
    gif_x=300, gif_y=900, gif_w=400, gif_h=400, gif_min_seconds=5.0,
    include_audio=False, preset="ultrafast", crf=30,
)
gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "work", TMP / "out",
                     gif_paths=gif_paths)
assert not gen.input_warnings, gen.input_warnings

BOX = (400, 400)
for path, (name, _, _) in zip(gif_paths, POOL):
    natural = gen._first_video_frame(path)
    fitted = gen._contain_content(*BOX, path)
    assert fitted.width <= BOX[0] and fitted.height <= BOX[1], \
        f"{name}: {fitted.size} escapes the box"
    # One side has to TOUCH the box. Falling short on both axes is the old
    # no-upscale clamp coming back, and it fails silently — the gif just looks
    # small in a box the operator sized deliberately.
    assert fitted.width >= BOX[0] - 1 or fitted.height >= BOX[1] - 1, \
        f"{name}: {fitted.size} stops short of the box on both axes"
    ar_in, ar_out = natural.width / natural.height, fitted.width / fitted.height
    assert abs(ar_in - ar_out) / ar_in < 0.03, f"{name}: aspect ratio distorted"
    print(f"   contain-fit {name:5s} {natural.size} -> {fitted.size}")
# the specific rule the user asked for: a gif SMALLER than the box grows into it
tiny_fit = gen._contain_content(*BOX, gif_dir / "tiny.mp4")
assert tiny_fit.size == (400, 320), tiny_fit.size
print("contain-fit: a 100x80 gif is enlarged to 400x320 in a 400x400 box")

# The editor payload is the ONE caller that still caps at natural size: the
# browser enlarges in CSS, so a pre-enlarged raster would be base64 weight with
# no extra detail. This is a bandwidth rule, not a fit rule.
capped = gen._contain_content(*BOX, gif_dir / "tiny.mp4", allow_upscale=False)
assert capped.size == (100, 80), capped.size
print("editor payload raster stays at the gif's natural size (CSS does the fit)")

# the padded tile must be genuinely transparent around the content
tile = gen._contain_frame(*BOX, gif_dir / "tiny.mp4")
assert tile.size == BOX and tile.mode == "RGBA"
assert tile.getpixel((2, 2))[3] == 0, "padding is not transparent"
assert tile.getpixel((200, 200))[3] == 255, "content is not opaque"
print("contain-fit tile: padding alpha=0, content alpha=255")

# ---------- 4. the sequence: derived length, floor honoured, every gif used ----------
row = pd.Series({"BG_Image": "b.png", "Headline": "", "Subheading": "", "Footer": ""})
spec = RowSpec.from_row(row, 1)
gen._resolve_positions(spec)
assert spec.gif_clips and len(spec.gif_clips) == len(spec.gif_clip_repeats)
covered = 0.0
for path, reps in zip(spec.gif_clips, spec.gif_clip_repeats):
    dur = _probe_video_duration(FF, path)
    assert dur * reps >= 5.0 - 1e-6, f"{path.name} holds only {dur * reps:.2f}s"
    covered += dur * reps
assert covered >= 20.0, f"sequence covers {covered:.1f}s of a 20s promo"
assert len(spec.gif_clips) <= GIF_MAX_TOTAL_CLIPS
print(f"sequence: {len(spec.gif_clips)} gifs covering {covered:.1f}s of a 20s "
      f"promo, each >= 5s")

# Dealt from a shuffled deck, so all four are used before any repeats — with
# independent random picks this produced A,B,A,B and never showed two uploads.
first_pass = [p.stem for p in spec.gif_clips[:len(POOL)]]
assert len(set(first_pass)) == len(POOL), f"deck repeated within a pass: {first_pass}"
print(f"deck: every gif used once before any repeat -> {first_pass}")

# Reproducible: the preview must match the render, and a resumed job must match
# what it did before the restart.
spec2 = RowSpec.from_row(row, 1)
gen._resolve_positions(spec2)
assert [p.name for p in spec2.gif_clips] == [p.name for p in spec.gif_clips]
print("sequence is reproducible for the same row")

# ...and NOT correlated with the CTA layer's picks (different RNG salt).
rng_gif = vg.random.Random(spec.placement_seed() ^ 0x91F).random()
rng_cta = vg.random.Random(spec.placement_seed() ^ 0xC7A).random()
assert rng_gif != rng_cta, "gif and CTA pickers share a seed"
print("gif picker is seeded apart from the CTA picker")

# ---------- 5. the input-index invariant ----------
cmd = gen.build_ffmpeg_command(spec, TMP / "b.png", TMP / "o.png", None,
                               TMP / "x.mp4")
n_inputs = sum(1 for a in cmd if a == "-i")
fc = cmd[cmd.index("-filter_complex") + 1]
VideoGenerator._check_filter_inputs(fc, n_inputs)          # must not raise
print(f"real command: {n_inputs} inputs, every one referenced exactly once")

# The filter has to fit the box in both directions too — a bare box-sized scale
# with force_original_aspect_ratio=decrease. The old 'min(GW,iw)' form capped it
# at the source size and would leave a small gif small in the OUTPUT only.
assert (f"scale={spec.gif_w}:{spec.gif_h}:force_original_aspect_ratio=decrease"
        in fc), "the gif chain no longer scales to the box"
assert f"min({spec.gif_w},iw)" not in fc, "the no-upscale clamp is back in the filter"
print(f"gif filter scales to the box in both directions "
      f"({spec.gif_w}x{spec.gif_h}, aspect preserved, padded transparent)")

# -stream_loop must accompany the gifs, and its count must be repeats-1
loops = [int(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-stream_loop"]
assert loops == [r - 1 for r in spec.gif_clip_repeats], (loops, spec.gif_clip_repeats)
print(f"-stream_loop counts match the repeat plan: {loops}")

# An index that drifts by one must be caught. This is the case that otherwise
# renders successfully and silently wrong.
for delta in (+1, -1):
    broken = vg.re.sub(
        r"\[(\d+):v\]",
        lambda m: f"[{max(0, int(m.group(1)) + delta)}:v]", fc)
    try:
        VideoGenerator._check_filter_inputs(broken, n_inputs)
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"an off-by-{delta:+d} index slipped through")
print("off-by-one input indices are rejected in both directions")

# The combined ceiling has to bite before Windows' command-line limit does.
assert MAX_TOTAL_FFMPEG_INPUTS < 66
print(f"combined input ceiling is {MAX_TOTAL_FFMPEG_INPUTS}, under the "
      "measured Windows limit")

# ---------- 6. a real render ----------
result = gen.render_row(1, row, "gif_row.mp4")
assert result.ok, result.error
out = TMP / "out" / result.filename
info = subprocess.run([FF, "-hide_banner", "-i", str(out)],
                      capture_output=True, text=True).stderr
m = VideoGenerator._DURATION_RE.search(info)
h, mi, s = m.groups()
out_dur = int(h) * 3600 + int(mi) * 60 + float(s)
assert abs(out_dur - 20.0) < 0.3, f"output is {out_dur}s, promo is 20s"
print(f"render: {result.filename}, {out_dur:.2f}s — ends with the promo, "
      "mid-gif if need be")

# the padding must let the layer underneath through, in the real output
lead = spec.gif_clips[0]
lead_secs = _probe_video_duration(FF, lead) * spec.gif_clip_repeats[0]
frame_png = TMP / "frame.png"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{min(lead_secs / 2, 19.0):.2f}", "-i", str(out),
                "-frames:v", "1", "-update", "1", str(frame_png)],
               check=True, capture_output=True)
frame = Image.open(frame_png).convert("RGB")
# The gif box (300,900 400x400) sits below the promo box (90,100 900x400), so
# anything showing through the padding is the magenta background.
corner, plain = frame.getpixel((303, 1290)), frame.getpixel((40, 1700))
assert all(abs(a - b) < 14 for a, b in zip(corner, plain)), \
    f"gif padding is opaque: corner {corner} vs background {plain}"
print(f"padding is see-through in the render: box corner {corner} == "
      f"background {plain}")

# ---------- 6b. every layer at once ----------
# The case the index arithmetic actually has to survive: THREE variable-length
# runs of inputs (CTA clips, gifs, subliminal stills) in one command, with the
# optional CTA image shifting everything after it. This is where an off-by-one
# would render a different video and still exit 0.
clip_dir = TMP / "clips"
clip_dir.mkdir()
cta_slots = [[mk(clip_dir / f"c{i}.mp4", f"color=c=0xFF{i}000:s=400x400:r=30", 2.0)]
             for i in range(2)]
cta_png = TMP / "cta.png"
Image.new("RGBA", (400, 160), (0, 255, 0, 255)).save(cta_png)
combo_cfg = RenderConfig(
    bg_color="#FF00FF", gif_min_seconds=5.0, include_audio=False,
    preset="ultrafast", crf=30, subliminal_targets=["Headline"], subliminal_k=3,
)
combo = VideoGenerator(combo_cfg, bg_dir, promo, cta_png, TMP / "cw", TMP / "co",
                       cta_video_slots=cta_slots, gif_paths=gif_paths)
combo_row = pd.Series({"BG_Image": "b.png", "Headline": "Secret Code XYZW",
                       "Subheading": "Sub", "Footer": "Foot"})
combo_spec = RowSpec.from_row(combo_row, 1)
combo._resolve_positions(combo_spec)
# A subliminal text ships as ONE raw rgba clip: K cropped frames concatenated,
# with the crop box carried alongside so the overlay lands at the right offset.
sub_raw = TMP / "sub0.raw"
with open(sub_raw, "wb") as fh:
    for j in range(combo_cfg.subliminal_k):
        fh.write(Image.new("RGBA", (200, 100), (255, 255, 255, 255)).tobytes())
subs = [{"raw": sub_raw, "w": 200, "h": 100, "x": 100, "y": 800,
         "k": combo_cfg.subliminal_k}]
combo_cmd = combo.build_ffmpeg_command(combo_spec, TMP / "b.png", TMP / "o.png",
                                       cta_png, TMP / "combo.mp4", subs)
combo_n = sum(1 for a in combo_cmd if a == "-i")
combo_fc = combo_cmd[combo_cmd.index("-filter_complex") + 1]
refs = sorted(int(x) for x in vg.re.findall(r"\[(\d+):v\]", combo_fc))
assert refs == list(range(combo_n)), (refs, combo_n)
VideoGenerator._check_filter_inputs(combo_fc, combo_n)
assert "[cseq]" in combo_fc and "[gseq]" in combo_fc, "a concat chain is missing"
# The subliminal clip is overlaid at its crop offset, cycle baked into the
# frames — no per-frame enable gating in the graph any more.
assert "overlay=100:800" in combo_fc, combo_fc
assert "enable=" not in combo_fc, "stray enable gate in the graph"
# -stream_loop belongs only to gifs and subliminal clips; attaching it to a CTA
# clip would silently repeat that clip instead. (Music/beds aren't in this test.)
looped = set()
for i, a in enumerate(combo_cmd):
    if a == "-stream_loop":
        looped.add(Path(combo_cmd[combo_cmd.index("-i", i) + 1]).name)
assert looped <= {p.name for p in gif_paths} | {sub_raw.name}, looped
# -stream_loop on a rawvideo input is the load-bearing primitive: if it
# quietly failed, overlay's repeatlast would freeze the LAST partial and the
# whole effect would vanish with exit 0. Prove a 3-frame raw clip cycles:
# output frame 4 must show frame 4 % 3 = 1 (green), not a held frame 2 (blue).
lraw = TMP / "loopcheck.raw"
for c in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]:
    with open(lraw, "ab") as fh:
        fh.write(Image.new("RGBA", (64, 64), c).tobytes())
lpng = TMP / "loopcheck.png"
subprocess.run(
    [FF, "-y", "-hide_banner", "-loglevel", "error",
     "-f", "rawvideo", "-pixel_format", "rgba", "-video_size", "64x64",
     "-framerate", "30", "-stream_loop", "3", "-t", "1", "-i", str(lraw),
     "-vf", "select=eq(n\\,4)", "-frames:v", "1", "-update", "1", str(lpng)],
    check=True, capture_output=True)
px = Image.open(lpng).convert("RGB").getpixel((32, 32))
assert px[1] > 200 and px[0] < 60 and px[2] < 60, \
    f"raw clip does not cycle under -stream_loop: frame 4 is {px}, not green"
print("raw rgba clip cycles under -stream_loop: frame 4 wrapped to frame 1")

# render_row builds the raw clip itself (real partials, real crop box) — the
# end-to-end proof that the cropped looping clip renders.
assert combo.render_row(1, combo_row, "combo.mp4").ok
print(f"all five layers at once: {combo_n} inputs (CTA image + "
      f"{len(combo_spec.cta_video_clips)} clips + {len(combo_spec.gif_clips)} gifs "
      f"+ {len(subs)} subliminal clip), indices intact, renders")

# ---------- 7. no pool = the layer vanishes ----------
plain_gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "work2", TMP / "out2")
assert not plain_gen._has_gifs
plain_spec = RowSpec.from_row(row, 1)
plain_gen._resolve_positions(plain_spec)
assert plain_spec.gif_clips is None
plain_cmd = plain_gen.build_ffmpeg_command(plain_spec, TMP / "b.png",
                                           TMP / "o.png", None, TMP / "y.mp4")
assert "-stream_loop" not in plain_cmd
assert "[gseq]" not in plain_cmd[plain_cmd.index("-filter_complex") + 1]
# the box is still resolved, so the editor always has numbers to draw
assert plain_spec.gif_w == 400 and plain_spec.gif_x == 300
print("no gif pool: no inputs, no filter, but the box still resolves for the editor")

# ---------- 8. staging and discovery ----------
class Fake:
    def __init__(self, path):
        self.name = Path(path).name
        self._data = Path(path).read_bytes()

    def getvalue(self):
        return self._data


staged = TMP / "assets"
# The case that would silently drop every gif: CTA clips NOT uploaded (they
# come from Drive), gifs uploaded. The gif argument must not be gated on the
# clip source.
stage_uploads(staged, Fake(promo), None, None, None,
              None, [Fake(p) for p in gif_paths])
ws = workspace_from_dir(staged, TMP / "wswork")
assert len(ws.gif_paths) == len(POOL), ws.gif_paths
assert not ws.cta_video_slots
assert [p.name for p in ws.gif_paths] == sorted(p.name for p in gif_paths), \
    "pool order must be sorted, or a resumed job picks a different sequence"
print(f"staging: {len(ws.gif_paths)} gifs survive with no CTA clips uploaded, "
      "in sorted order")

# junk in the folder is ignored rather than handed to FFmpeg
(staged / "gifs" / "Thumbs.db").write_bytes(b"junk")
ws2 = workspace_from_dir(staged, TMP / "wswork")
assert len(ws2.gif_paths) == len(POOL), ws2.gif_paths
print("staging: non-video junk in the gif folder is ignored")

# no gifs at all is a clean empty pool, not a crash
bare = TMP / "assets_bare"
stage_uploads(bare, Fake(promo), None, None, None, None, None)
assert workspace_from_dir(bare, TMP / "wswork2").gif_paths == []
print("staging: no gifs uploaded gives an empty pool")

print()
print("ALL GIF LAYER CHECKS PASSED")
