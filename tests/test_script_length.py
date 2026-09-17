"""The script sets the video length, in BOTH directions.

Renders real MP4s: the length rule reaches the output through six derived
layers, so an assertion on a returned number would prove nothing about the file
that lands in Drive.

  * script longer than the promo  -> the video is the script's length (the promo
    loops), which is what the option always did
  * script SHORTER than the promo -> the video ends with the script, instead of
    the promo playing on to nobody
  * the option off                -> the promo is the length either way
  * a row with no narration       -> the promo's length, option or not
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from speech import synth
from video_generator import RenderConfig, VideoGenerator, find_ffmpeg

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="scriptlen_"))
PROMO_DUR = 6.0
VOICE_CACHE = TMP / "voice"
VOICE_CACHE.mkdir()


def ff(*args):
    return subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", *args],
                          check=True, capture_output=True)


def duration(path: Path) -> float:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    h, mn, s = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr).groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def put_voice(text: str, total_secs: float) -> None:
    """A voice-cache entry in synth.py's own format: a tone for the whole take."""
    key = synth.cache_key(text, "af_heart", 1.0, "a")
    ff("-f", "lavfi", "-i",
       f"sine=frequency=900:duration={total_secs}:sample_rate=24000",
       "-ac", "1", str(VOICE_CACHE / f"{key}.wav"))
    words, t = [], 0.0
    for word in text.split():
        step = total_secs / len(text.split())
        words.append({"text": word, "start": t, "end": t + step * 0.9})
        t += step
    (VOICE_CACHE / f"{key}.json").write_text(
        json.dumps({"duration": total_secs, "words": words}), encoding="utf-8")


bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (20, 20, 20)).save(bg_dir / "b.png")
ff("-f", "lavfi", "-i", "color=c=0x202020:s=480x854:r=30",
   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
   "-t", str(PROMO_DUR), "-c:v", "libx264", "-pix_fmt", "yuv420p",
   "-c:a", "aac", "-shortest", str(TMP / "promo.mp4"))
promo = TMP / "promo.mp4"

SHORT = "Half off everything today"          # ends well before the promo
LONG = "Compare forty providers in seconds and switch today to start saving now"
put_voice(SHORT, 2.4)
put_voice(LONG, 9.0)

BASE = dict(bg_color="#101010", preset="ultrafast", crf=32,
            video_x=90, video_y=300, video_w=900, video_h=900,
            voice_enabled=True, voice_set=["af_heart"], voice_speed=1.0)


def render(name: str, script: str, **kw) -> Path:
    cfg = RenderConfig(**{**BASE, **kw})
    gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / f"w_{name}",
                         TMP / f"o_{name}", voice_cache_dir=VOICE_CACHE)
    row = pd.Series({"BG_Image": "b.png", "Headline": "ALWAYS HERE",
                     "Voiceover": script, "Screen_Text_Y": "1420"})
    result = gen.render_row(1, row, filename=f"{name}.mp4")
    assert result.ok, result.error
    return result.output_path


# ---- the script is the length, both directions -----------------------------
out = render("short_on", SHORT, voice_loop_promo=True)
assert abs(duration(out) - 2.4) < 0.4, duration(out)
print(f"script shorter than the promo: {duration(out):.2f}s (promo {PROMO_DUR}s)")

out = render("long_on", LONG, voice_loop_promo=True)
assert abs(duration(out) - 9.0) < 0.4, duration(out)
print(f"script longer than the promo:  {duration(out):.2f}s (promo loops)")

# ---- off: the promo is the length, and a long script is cut and warned ------
out = render("short_off", SHORT, voice_loop_promo=False)
assert abs(duration(out) - PROMO_DUR) < 0.4, duration(out)

cfg = RenderConfig(**{**BASE, "voice_loop_promo": False})
gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / "w_warn", TMP / "o_warn",
                     voice_cache_dir=VOICE_CACHE)
res = gen.render_row(1, pd.Series({"BG_Image": "b.png", "Voiceover": LONG}),
                     filename="warn.mp4")
assert res.ok and abs(duration(res.output_path) - PROMO_DUR) < 0.4
assert any("cut short" in w for w in res.warnings), res.warnings
print(f"option off: {duration(res.output_path):.2f}s, row warned that the "
      "script is cut")

# ---- a row with no narration is still the promo's length -------------------
for loop in (True, False):
    cfg = RenderConfig(**{**BASE, "voice_loop_promo": loop})
    gen = VideoGenerator(cfg, bg_dir, promo, None, TMP / f"w_sil{loop}",
                         TMP / f"o_sil{loop}", voice_cache_dir=VOICE_CACHE)
    res = gen.render_row(1, pd.Series({"BG_Image": "b.png", "Headline": "HI"}),
                         filename=f"silent_{loop}.mp4")
    assert res.ok, res.error
    assert abs(duration(res.output_path) - PROMO_DUR) < 0.4, duration(res.output_path)
print("a row with no narration keeps the promo's length")

# ---- a fade tuned to the promo still appears on a short row ----------------
# cta_fade_start is chosen against the promo (default 1.0s, and the sidebar
# allows up to 30). On a 2.4s row a 5s start would hold the CTA at alpha 0 for
# the whole video: exit 0, no CTA, and nothing said anywhere.
cta_png = TMP / "cta.png"
Image.new("RGBA", (400, 160), (255, 255, 255, 255)).save(cta_png)
cfg = RenderConfig(**{**BASE, "voice_loop_promo": True,
                      "cta_fade_start": 5.0, "cta_fade_duration": 0.5})
gen = VideoGenerator(cfg, bg_dir, promo, cta_png, TMP / "w_fade", TMP / "o_fade",
                     voice_cache_dir=VOICE_CACHE)
spec = gen._spec_for(pd.Series({"BG_Image": "b.png", "Voiceover": SHORT}), 1)
gen._resolve_positions(spec)   # render_row's own order: the per-row defaults
cmd = " ".join(str(c) for c in gen.build_ffmpeg_command(
    spec, bg_dir / "b.png", bg_dir / "b.png", cta_png, TMP / "fade.mp4"))
assert "fade=t=in:st=1.9:d=0.5" in cmd, [p for p in cmd.split() if "fade" in p]
assert any("never appear" in w for w in spec.warnings), spec.warnings
print("fade pulled back into a 2.4s row: st=5.0 -> 1.9, and the row is warned")

# A fade that already fits is left exactly as it was.
cfg_ok = RenderConfig(**{**BASE, "voice_loop_promo": True,
                         "cta_fade_start": 1.0, "cta_fade_duration": 0.5})
gen_ok = VideoGenerator(cfg_ok, bg_dir, promo, cta_png, TMP / "w_fade2",
                        TMP / "o_fade2", voice_cache_dir=VOICE_CACHE)
spec_ok = gen_ok._spec_for(pd.Series({"BG_Image": "b.png", "Voiceover": SHORT}), 1)
gen_ok._resolve_positions(spec_ok)
cmd_ok = " ".join(str(c) for c in gen_ok.build_ffmpeg_command(
    spec_ok, bg_dir / "b.png", bg_dir / "b.png", cta_png, TMP / "fade2.mp4"))
assert "fade=t=in:st=1.0:d=0.5" in cmd_ok, cmd_ok[:200]
assert not any("never appear" in w for w in spec_ok.warnings), spec_ok.warnings
print("a fade that already fits is untouched")

print("OK — the script sets the video length")
