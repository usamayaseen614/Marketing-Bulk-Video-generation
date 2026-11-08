"""CTA clips AFTER the promo: the promo plays, then the slot clips play once
each in the CTA box, each with a random caption from the uploaded pool.

One real render, then pixels, audio levels and the length are read back at
fixed times, because every way this goes wrong renders "successfully": the
promo still anchoring the length cuts the clips off, a missing setpts shift
plays the clips over the promo, the promo freezing on its last frame instead of
emptying, captions on the wrong clip, or `-shortest` trimming the picture to
the promo's audio.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from video_generator import (CTA_CAPTION_ROLE, RenderConfig, VideoGenerator,
                             find_ffmpeg)

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="ctaafter_"))


def mk(path: Path, hexcolor: str, dur: float, tone: int = 0) -> Path:
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", f"color=c=0x{hexcolor}:s=640x360:r=30"]
    if tone:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=44100"]
    cmd += ["-t", str(dur), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if tone:
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return path


def duration(path: Path) -> float:
    err = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def frame(path: Path, t: float) -> Image.Image:
    png = TMP / f"f_{t:.2f}.png"
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t),
                    "-i", str(path), "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("RGB")


def near(px, rgb, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px, rgb))


def count(img: Image.Image, box, test) -> int:
    c = img.crop(box)
    return sum(1 for x in range(c.width) for y in range(c.height)
               if test(c.getpixel((x, y))))


def peak_db(path: Path, start: float, length: float) -> float:
    err = subprocess.run([FF, "-hide_banner", "-ss", str(start), "-t", str(length),
                          "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    return -200.0 if (not m or m.group(1) == "-inf") else float(m.group(1))


bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (255, 0, 255)).save(bg_dir / "b.png")   # magenta

promo = mk(TMP / "promo.mp4", "202020", 2.0, tone=440)
red = mk(TMP / "red.mp4", "FF0000", 1.0, tone=880)
green = mk(TMP / "green.mp4", "00FF00", 1.5)           # no audio track at all
blue = mk(TMP / "blue.mp4", "0000FF", 1.0, tone=660)   # played at 2x -> 0.5s

cfg = RenderConfig(
    bg_color="#FF00FF", preset="ultrafast", crf=30,
    video_x=90, video_y=300, video_w=900, video_h=900,
    cta_video_x=360, cta_video_y=1200, cta_video_w=360, cta_video_h=360,
    cta_video_speeds=[1.0, 1.0, 2.0],
    cta_after_promo=True, cta_after_show_texts=False,
    cta_captions=["BUY", "", "  "] + ["BUY"],   # blanks are dropped from the pool
)
gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "work", TMP / "out",
                     cta_video_slots=[[red], [green], [blue]])
assert not gen.input_warnings, gen.input_warnings

row = pd.Series({"BG_Image": "b.png", "Headline": "HEAD", "Headline_X": 540,
                 "Headline_Y": 150, "Headline_Size": 90, "Headline_Color": "#00FFFF",
                 f"{CTA_CAPTION_ROLE}_Color": "#FFFF00",
                 f"{CTA_CAPTION_ROLE}_Size": 60})

# ---------- 1. resolution: three clips, their screen time, their captions ----
spec = gen._spec_for(row, 1)
gen._resolve_positions(spec)
assert spec.cta_video_clips == [red, green, blue], spec.cta_video_clips
assert [round(d, 2) for d in spec.cta_after_durations] == [1.0, 1.5, 0.5], \
    spec.cta_after_durations
assert spec.cta_caption_texts == ["BUY"] * 3, spec.cta_caption_texts
assert abs(gen._render_duration(spec) - 5.0) < 0.05, gen._render_duration(spec)
cap = spec.cta_caption
assert 360 <= cap.x <= 720 and 1200 < cap.y < 1360, (cap.x, cap.y)

# ---------- 2. the editor gets the caption as a draggable text --------------
roles = [t["role"] for t in gen.build_editor_payload(row, 1)["texts"]]
assert CTA_CAPTION_ROLE in roles, roles

# ---------- 3. one real render -------------------------------------------------
res = gen.render_row(1, row, "out.mp4")
assert res.ok, res.error
out = res.output_path
total = duration(out)
assert abs(total - 5.0) < 0.15, f"expected ~5.0s (2s promo + 3s of clips), got {total:.2f}"

promo_c, cta_c = (540, 750), (540, 1450)
cap_box = (360, cap.y - 40, 720, cap.y + 40)
head_box = (300, 100, 780, 200)
yellow = lambda p: p[0] > 200 and p[1] > 200 and p[2] < 90
cyan = lambda p: p[0] < 90 and p[1] > 200 and p[2] > 200

f = frame(out, 1.0)        # promo phase
assert near(f.getpixel(promo_c), (32, 32, 32)), f.getpixel(promo_c)
assert near(f.getpixel(cta_c), (255, 0, 255)), f"CTA box not empty: {f.getpixel(cta_c)}"
assert count(f, cap_box, yellow) == 0, "caption showing during the promo"
assert count(f, head_box, cyan) > 200, "headline missing during the promo"

for t, rgb, name in ((2.5, (255, 0, 0), "red"), (3.6, (0, 255, 0), "green"),
                     (4.75, (0, 0, 255), "blue")):
    f = frame(out, t)
    assert near(f.getpixel(cta_c), rgb), f"{name} clip at {t}s: {f.getpixel(cta_c)}"
    assert near(f.getpixel(promo_c), (255, 0, 255)), \
        f"promo box should be empty at {t}s: {f.getpixel(promo_c)}"
    assert count(f, cap_box, yellow) > 100, f"no caption over the {name} clip"
    assert count(f, head_box, cyan) == 0, f"headline not hidden at {t}s"

# ---------- 4. sound: promo, then each clip's own, silence where it has none --
assert peak_db(out, 0.3, 1.2) > -30, "promo audio missing"
assert peak_db(out, 2.2, 0.6) > -30, "red clip's own audio missing"
assert peak_db(out, 3.2, 0.8) < -60, "green clip has no audio, should be silent"
assert peak_db(out, 4.55, 0.35) > -30, "blue clip's own audio missing"

# ---------- 5. texts kept on (the default): each clip's frame carries them ----
cfg.cta_after_show_texts = True
spec3 = gen._spec_for(row, 1)
static = gen.build_overlay_image(spec3, include_cta=False)
frames = gen._cta_caption_layers(spec3, static)
assert [(round(s, 2), round(e, 2)) for s, e, _ in frames] == \
    [(2.0, 3.0), (3.0, 4.5), (4.5, 5.0)], [(s, e) for s, e, _ in frames]
for _s, _e, im in frames:
    rgb = im.convert("RGB")
    assert count(rgb, head_box, cyan) > 200 and count(rgb, cap_box, yellow) > 100

# ---------- 6. mode off: the classic command is untouched ----------------------
off = VideoGenerator(RenderConfig(bg_color="#FF00FF", preset="ultrafast"),
                     bg_dir, promo, None, TMP / "work2", TMP / "out2",
                     cta_video_slots=[[red], [green], [blue]])
s2 = off._spec_for(row, 1)
off.build_overlay_image(s2, include_cta=False)
cmd = " ".join(off.build_ffmpeg_command(s2, TMP / "b.png", TMP / "o.png", None,
                                        TMP / "x.mp4"))
assert "shortest=1" in cmd and "setpts=PTS+" not in cmd and "anullsrc" not in cmd
assert s2.cta_after_durations is None and abs(off._render_duration(s2) - 2.0) < 0.1

print(f"OK: {total:.2f}s, captions {spec.cta_caption_texts}, "
      f"warnings {res.warnings}")
