"""FFmpeg thread caps: decoders per input, encoder on the output.

Two separate knobs that are easy to confuse, because they are the SAME flag and
only their POSITION says which one they configure. `-threads` before a `-i` caps
that input's decoder; `-threads` in the output options caps x264. Getting the
position wrong is silent — FFmpeg exits 0 and renders either way.

Measured on one row, 22-25 inputs, idle box (wall / peak RSS):

                           no subliminal   subliminal, 3 layers
    as-is (auto)           20.1s 2934 MB   17.8s 3120 MB
    decoders capped to 1   23.0s 1944 MB   15.8s 2189 MB
    ENCODER capped to 1    72.5s 2578 MB   40.5s 3130 MB   <-- 2-4x SLOWER

Which is why the decoder cap defaults ON and the encoder cap defaults OFF.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="thrtest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from video_generator import RenderConfig, RowSpec, VideoGenerator, find_ffmpeg

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="ffthreads_"))

bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (20, 20, 20)).save(bg_dir / "b.png")
promo = TMP / "promo.mp4"
subprocess.run(
    [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
     "-i", "color=c=0x203040:s=720x1280:r=30", "-t", "1.0",
     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(promo)],
    check=True, capture_output=True)

base_png, overlay_png = TMP / "base.png", TMP / "ov.png"
Image.new("RGB", (1080, 1920), (20, 20, 20)).save(base_png)
Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(overlay_png)
row = pd.Series({"BG_Image": "b.png", "Headline": "Thread cap"})

n = 0


def build(**kw) -> list:
    global n
    n += 1
    gen = VideoGenerator(RenderConfig(**kw), bg_dir, promo, None,
                         TMP / f"work{n}", TMP / f"out{n}")
    spec = RowSpec.from_row(row, 1)
    gen._resolve_positions(spec)
    return gen.build_ffmpeg_command(spec, base_png, overlay_png, None,
                                    TMP / f"x{n}.mp4")


def split_at_last_input(cmd: list) -> int:
    """Index of the last -i. Everything after it is the OUTPUT half."""
    return max(j for j, a in enumerate(cmd) if a == "-i")


def threads_flags(cmd: list) -> tuple[list, list]:
    """(input-side positions, output-side positions) of every -threads."""
    cut = split_at_last_input(cmd)
    pos = [j for j, a in enumerate(cmd) if a == "-threads"]
    return [j for j in pos if j < cut], [j for j in pos if j > cut]


# ---------- 1. both zero: the historical command, no caps at all ----------
cmd = build(ffmpeg_threads=0, decode_threads=0)
assert "-threads" not in cmd, "with both knobs off nothing may be emitted"
print("both 0: no -threads anywhere (FFmpeg auto-detects, as before)")

# ---------- 2. decode_threads caps EVERY input, and only inputs ----------
cmd = build(ffmpeg_threads=0, decode_threads=1)
ins, outs = threads_flags(cmd)
n_inputs = sum(1 for a in cmd if a == "-i")
assert len(ins) == n_inputs, f"{len(ins)} caps for {n_inputs} inputs — every input needs one"
assert not outs, "decode_threads must NOT touch the encoder — that is the slow knob"
for rank, j in enumerate(ins):
    assert cmd[j + 1] == "1", cmd[j:j + 2]
    # Cap number k must fall after input k-1 and before input k. An input's
    # options run from the previous -i to its own, so a cap that drifts past
    # its -i silently configures the NEXT input's decoder instead.
    seen = sum(1 for a in cmd[:j] if a == "-i")
    assert seen == rank, f"cap {rank} sits after {seen} input(s) — wrong input"
print(f"decode_threads=1: {len(ins)} input-side cap(s) for {n_inputs} input(s), "
      "none on the encoder")

# ---------- 3. ffmpeg_threads caps the ENCODER, and only the encoder ----------
cmd = build(ffmpeg_threads=3, decode_threads=0)
ins, outs = threads_flags(cmd)
assert not ins, "ffmpeg_threads must not land among the inputs"
assert len(outs) == 1, outs
assert cmd[outs[0] + 1] == "3"
assert outs[0] > cmd.index("-c:v"), "-threads must follow the codec it configures"
assert outs[0] < len(cmd) - 1, "-threads must precede the output path"
print("ffmpeg_threads=3: one -threads 3 in the output options, none on the inputs")

# ---------- 4. both together stay in their own halves ----------
cmd = build(ffmpeg_threads=2, decode_threads=1)
ins, outs = threads_flags(cmd)
assert len(ins) == sum(1 for a in cmd if a == "-i") and len(outs) == 1
assert {cmd[j + 1] for j in ins} == {"1"} and cmd[outs[0] + 1] == "2"
print("both set: decoders at 1, encoder at 2, neither leaks into the other half")

# ---------- 5. FFmpeg accepts it — a misplaced flag is a hard error ----------
gen = VideoGenerator(RenderConfig(decode_threads=1, ffmpeg_threads=2), bg_dir,
                     promo, None, TMP / "workR", TMP / "outR")
res = gen.render_row(1, row, "capped.mp4")
assert res.ok, res.error
out = res.output_path
assert out.is_file() and out.stat().st_size > 0, "capped render produced nothing"
print(f"real render with both caps: {out.name}, {out.stat().st_size} bytes")

print("\nALL FFMPEG THREAD-CAP TESTS PASSED")
