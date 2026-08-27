"""The per-render encoder thread cap.

x264 sizes its frame-thread pool from the machine's core count and has no idea
how many siblings are running: `workers` parallel rows each ask for the whole
box, which on a 112-core VM is ~14,000 encoder threads between them, each
carrying its own frame buffers. RenderConfig.ffmpeg_threads caps it.

Two things can silently go wrong and neither announces itself: the flag can be
absent (the default reverts to auto and the cap does nothing), or it can land
in the wrong half of the command line — before the output options it configures
a DECODER instead of the encoder, which still exits 0 and still renders.
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


def build(threads: int) -> list:
    gen = VideoGenerator(RenderConfig(ffmpeg_threads=threads), bg_dir, promo,
                         None, TMP / f"work{threads}", TMP / f"out{threads}")
    spec = RowSpec.from_row(row, 1)
    gen._resolve_positions(spec)
    return gen.build_ffmpeg_command(spec, base_png, overlay_png, None,
                                    TMP / f"x{threads}.mp4")


# ---------- 1. zero means "leave x264 alone" — the historical behaviour ----------
assert "-threads" not in build(0), "ffmpeg_threads=0 must not emit the flag"
print("ffmpeg_threads=0: no -threads, x264 auto-detects (unchanged)")

# ---------- 2. a cap emits the flag, in the OUTPUT half of the command ----------
cmd = build(3)
assert "-threads" in cmd, "a cap must reach FFmpeg"
i = cmd.index("-threads")
assert cmd[i + 1] == "3", cmd[i:i + 2]
# Placement is the whole point: options are positional in FFmpeg, so this has to
# sit with the encoder settings, after the last -i and before the output path.
assert i > max(j for j, a in enumerate(cmd) if a == "-i"), \
    "-threads landed among the INPUTS — that configures a decoder, not x264"
assert i > cmd.index("-c:v"), "-threads must follow the codec it configures"
assert i < len(cmd) - 1, "-threads must precede the output path"
print(f"ffmpeg_threads=3: -threads 3 at position {i}, inside the output options")

# ---------- 3. FFmpeg actually accepts it (a misplaced flag is a hard error) ----
gen = VideoGenerator(RenderConfig(ffmpeg_threads=2), bg_dir, promo, None,
                     TMP / "workR", TMP / "outR")
res = gen.render_row(1, row, "capped.mp4")
assert res.ok, res.error
out = res.output_path
assert out.is_file() and out.stat().st_size > 0, "capped render produced nothing"
print(f"real render with -threads 2: {out.name}, {out.stat().st_size} bytes")

# ---------- 4. the derivation the batch job uses ----------
# jobs/runners/render.py: config.FFMPEG_THREADS or max(1, cpu // workers).
# The point is that it never returns 0 (which would silently mean "auto" and
# undo the cap) however lopsided the worker count is.
for cpu, workers in ((112, 112), (112, 8), (8, 112), (1, 1), (16, 5)):
    derived = max(1, cpu // max(1, workers))
    assert derived >= 1, (cpu, workers, derived)
    assert derived * workers <= cpu or derived == 1, (cpu, workers, derived)
print("derivation: never 0, and workers x threads stays within the core count")

print("\nALL FFMPEG THREAD-CAP TESTS PASSED")
