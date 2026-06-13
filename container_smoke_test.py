"""One-shot engine test executed INSIDE the Docker container (docker exec).
Verifies Linux ffmpeg, DejaVu fonts, and a full row render. Deleted after use."""

import subprocess
import tempfile
import zipfile
from pathlib import Path

import pandas as pd

from video_generator import RenderConfig, VideoGenerator, find_default_font, find_ffmpeg

assets = Path("/tmp/assets")
df = pd.read_excel(assets / "data.xlsx")

tmp = Path(tempfile.mkdtemp())
bg_dir, work, out = tmp / "bgs", tmp / "work", tmp / "out"
with zipfile.ZipFile(assets / "backgrounds.zip") as zf:
    zf.extractall(bg_dir)

print("ffmpeg:", find_ffmpeg())
print("font:", find_default_font())

gen = VideoGenerator(RenderConfig(), bg_dir, assets / "promo.mp4", assets / "cta.png", work, out)
result = gen.render_row(1, df.iloc[0])
print("render ok:", result.ok, "| file:", result.filename, "| error:", result.error)
assert result.ok, result.error

probe = subprocess.run(
    [find_ffmpeg(), "-i", str(result.output_path)], capture_output=True, text=True
).stderr
line = next(l for l in probe.splitlines() if "Video:" in l)
print(line.strip())
assert "1080x1920" in line and "h264" in line
print("CONTAINER SMOKE TEST PASSED")
