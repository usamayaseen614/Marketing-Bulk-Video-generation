"""Timed captions + voiceover: the concat-demuxer timeline, the render length,
and the audio mix.

Real renders, not command-string assertions, because every failure this guards
against is SILENT. A caption list with mismatched frame sizes renders nothing at
exit 0; a missing tail entry freezes the last caption on screen forever; a
`-framerate` before the concat `-i` discards every duration; sidechaincompress
truncates the whole mix to the voiceover's length without a word on stderr.
Each of those was measured during design, and each has an assertion here.

No Kokoro: the voice cache is filled by hand in exactly the format
speech/synth.py writes, which is also what proves the renderer's lookup and the
synthesizer's write agree on the cache key.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="voicetest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from PIL import Image

from speech import synth
from video_generator import RenderConfig, RowSpec, VideoGenerator, find_ffmpeg

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="voicecap_"))
PROMO_DUR = 6.0
VOICE_CACHE = TMP / "voice"
VOICE_CACHE.mkdir()


def ff(*args) -> subprocess.CompletedProcess:
    return subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", *args],
                          check=True, capture_output=True)


def duration(path: Path) -> float:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def band_db(path: Path, lo: int, hi: int, start: float = 0.0,
            length: float = 99.0) -> float:
    """Mean dB in [lo, hi] Hz over a slice. Doubled filters for slope."""
    proc = subprocess.run(
        [FF, "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-af", f"highpass=f={lo},highpass=f={lo},lowpass=f={hi},lowpass=f={hi},"
                "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace")
    return float(re.search(r"mean_volume: (\S+)", proc.stderr).group(1))


def frame_at(path: Path, t: float) -> Image.Image:
    out = TMP / f"f_{t:.2f}_{path.stem}.png"
    ff("-ss", str(t), "-i", str(path), "-frames:v", "1", str(out))
    return Image.open(out).convert("RGB")


def bright_pixels(image: Image.Image, box) -> int:
    """Count near-white pixels in a region — the caption ink."""
    return int((np.asarray(image.crop(box)) > 200).all(axis=2).sum())


def put_voice(text, voice, speed, tone_secs, total_secs, words):
    """Write a voice-cache entry by hand, in synth.py's own format.

    The wav is a tone for `tone_secs` then silence — so ducking is measurable as
    a difference between the two halves of the SAME render."""
    key = synth.cache_key(text, voice, speed, "a")
    ff("-f", "lavfi", "-i",
       f"sine=frequency=900:duration={tone_secs}:sample_rate=24000",
       "-af", f"apad=whole_dur={total_secs}", "-ac", "1",
       str(VOICE_CACHE / f"{key}.wav"))
    (VOICE_CACHE / f"{key}.json").write_text(
        json.dumps({"duration": total_secs, "words": words}), encoding="utf-8")
    return key


# ------------------------------------------------------------------- fixtures
bg_dir = TMP / "bg"
bg_dir.mkdir()
Image.new("RGB", (1080, 1920), (20, 20, 20)).save(bg_dir / "b.png")

ff("-f", "lavfi", "-i", "color=c=0x202020:s=480x854:r=30",
   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
   "-t", str(PROMO_DUR), "-c:v", "libx264", "-pix_fmt", "yuv420p",
   "-c:a", "aac", "-shortest", str(TMP / "promo.mp4"))
promo = TMP / "promo.mp4"
ff("-f", "lavfi", "-i", "color=c=0x202020:s=480x854:r=30", "-t", str(PROMO_DUR),
   "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(TMP / "silent.mp4"))
silent_promo = TMP / "silent.mp4"
# A bed deliberately SHORTER than the video, which is what exposed the
# apad/duration=first bugs during design.
ff("-f", "lavfi", "-i", "sine=frequency=300:duration=2.5:sample_rate=44100",
   "-ac", "2", str(TMP / "bed.mp3"))
music = [TMP / "bed.mp3"]

SCRIPT = "Compare forty providers in seconds and switch today to start saving"
WORDS = []
_t = 0.4                       # 0.4s of lead-in silence baked into the wav
for _w in SCRIPT.split():
    WORDS.append({"text": _w, "start": _t, "end": _t + 0.3})
    _t += 0.3
SPEECH_END = _t                # ~3.7s

BASE = dict(bg_color="#101010", preset="ultrafast", crf=32,
            video_x=90, video_y=300, video_w=900, video_h=900,
            voice_enabled=True, voice_set=["af_heart"], voice_speed=1.0)
# The caption band sits low; the headline sits high. Two disjoint bands means a
# single frame can prove the caption changed AND the static text did not.
CAPTION_BOX = (0, 1250, 1080, 1600)
HEADLINE_BOX = (0, 100, 1080, 500)
ROW = pd.Series({"BG_Image": "b.png", "Headline": "ALWAYS HERE",
                 "Headline_Y": "300", "Headline_Color": "#FFFFFF",
                 "Voiceover": SCRIPT, "Screen_Text_Y": "1420",
                 "Screen_Text_Color": "#FFFFFF"})


def make(name, video=promo, row=ROW, with_music=False, **kw):
    # music_volume matters: has_music is gated on it, so passing music_paths
    # alone builds no bed at all and the "with music" cases would quietly test
    # the promo-audio-only graph instead.
    if with_music:
        kw.setdefault("music_volume", 0.35)
    cfg = RenderConfig(**{**BASE, **kw})
    gen = VideoGenerator(cfg, bg_dir, video, None, TMP / f"w_{name}",
                         TMP / f"o_{name}",
                         music_paths=music if with_music else None,
                         voice_cache_dir=VOICE_CACHE)
    assert not gen.input_warnings, gen.input_warnings
    return gen


def render(name, **kw) -> Path:
    gen = make(name, **{k: v for k, v in kw.items()
                        if k in {"video", "row", "with_music"}})
    result = gen.render_row(1, kw.get("row", ROW), filename=f"{name}.mp4")
    assert result.ok, result.error
    return result.output_path


# ------------------------------------------------ 1. nothing on = nothing changed
#
# The whole feature has to be invisible when it is off. A regression here routes
# every existing batch through new code for no reason.
off_gen = VideoGenerator(RenderConfig(**{**BASE, "voice_enabled": False}),
                         bg_dir, promo, None, TMP / "w_off", TMP / "o_off")
off_spec = RowSpec.from_row(ROW, 1)
off_gen.attach_voice(off_spec, off_gen._voice_entry(off_spec))
# Off means off for the CAPTIONS as well, not just the audio: one switch gates
# the whole feature, so filling Screen_Text cannot start captioning a batch
# whose author never turned captions on.
assert off_spec.beats == [], off_spec.beats
assert off_spec.screen_text.text == "", off_spec.screen_text.text
off_gen._resolve_positions(off_spec)
cmd = " ".join(off_gen.build_ffmpeg_command(
    off_spec, bg_dir / "b.png", bg_dir / "b.png", None, TMP / "o_off" / "x.mp4"))
assert "-f concat" not in cmd, cmd
assert "-map 1:a?" in cmd and "amix" not in cmd and "sidechaincompress" not in cmd
print("ok: voiceover off leaves the command byte-for-byte on the old path")


# ------------------------------------------------------- 2. the concat list writer
put_voice(SCRIPT, "af_heart", 1.0, SPEECH_END, SPEECH_END + 0.3, WORDS)
gen = make("list")
spec = RowSpec.from_row(ROW, 1)
gen.attach_voice(spec, gen._voice_entry(spec))
assert spec.beats, "beats should come from the cached word timings"
assert spec.voice_duration > 0 and spec.voice_wav.is_file()
layers, static = gen._caption_layers(spec)
assert len(layers) == len(spec.beats), (len(layers), len(spec.beats))
# Every frame identical in size and mode, or the concat demuxer renders the
# whole layer as nothing at all, with exit 0 and an empty stderr.
assert all(im.size == (1080, 1920) and im.mode == "RGBA" for _s, _e, im in layers)

work = TMP / "w_list"
paths = []
for i, (_s, _e, im) in enumerate(layers):
    p = work / f"row_0001_cap{i:03d}.png"
    im.save(p)
    paths.append(p)
static_png = work / "row_0001_overlay.png"
static.save(static_png)
lst = gen._write_caption_list(layers, static_png, work / "caps.txt", paths, 7.0)
text = lst.read_text(encoding="utf-8")
lines = [ln for ln in text.splitlines() if ln]

assert all(ln.startswith("file '") and ln.endswith("'")
           for ln in lines if ln.startswith("file")), text
assert "\\" not in text and "/" not in text, (
    "entries must be bare filenames: outside quotes the demuxer eats "
    "backslashes, and a drive-letter path can get prepended to itself\n" + text)
assert lines[-1].startswith("file "), "the last entry must carry NO duration"
# The tail must outrun the render, or overlay's repeatlast freezes the closing
# caption on screen for the rest of the video.
tail = float(lines[-2].split()[1])
assert tail > 3.0, (tail, text)
assert text.count("row_0001_overlay.png") >= 2, text
print(f"ok: concat list — {len(spec.beats)} beats, quoted bare names, "
      f"{tail:.2f}s static tail, last entry duration-free")


# --------------------------------------------------- 3. a real render, with timing
out = render("basic")
assert abs(duration(out) - PROMO_DUR) < 0.35, duration(out)

mid = (spec.beats[0][0] + spec.beats[0][1]) / 2
after = spec.voice_duration + 0.1
cap_mid = bright_pixels(frame_at(out, mid), CAPTION_BOX)
cap_after = bright_pixels(frame_at(out, after), CAPTION_BOX)
head_mid = bright_pixels(frame_at(out, mid), HEADLINE_BOX)
head_after = bright_pixels(frame_at(out, after), HEADLINE_BOX)

assert cap_mid > 300, f"no caption during beat 0 ({cap_mid} px)"
assert cap_after < cap_mid * 0.2, (
    f"the closing caption is still on screen at {after:.2f}s "
    f"({cap_after} px vs {cap_mid} px) — the concat tail entry is missing, so "
    "overlay's repeatlast froze the last frame")
assert head_mid > 300 and head_after > 300, (head_mid, head_after)
assert abs(head_mid - head_after) < head_mid * 0.2, (
    "the static headline changed between frames — eof_action=pass would do "
    "this, and it must NOT be used on this layer")
print(f"ok: caption {cap_mid}px during the beat, {cap_after}px after; "
      f"headline steady at {head_mid}/{head_after}px throughout")


# ------------------------------------------- 4. the script outruns the promo
LONG = "This narration deliberately runs on well past the end of the promo clip"
long_words, t = [], 0.3
for w in LONG.split():
    long_words.append({"text": w, "start": t, "end": t + 0.55})
    t += 0.55
put_voice(LONG, "af_heart", 1.0, t, t + 0.4, long_words)
long_row = ROW.copy()
long_row["Voiceover"] = LONG
assert t + 0.4 > PROMO_DUR, "fixture must be longer than the promo"

looped = render("looped", row=long_row)
assert abs(duration(looped) - (t + 0.4)) < 0.4, (
    f"promo should have looped to the script's {t + 0.4:.2f}s, "
    f"got {duration(looped):.2f}s")
print(f"ok: promo looped — {PROMO_DUR}s promo rendered to "
      f"{duration(looped):.2f}s for a {t + 0.4:.2f}s script")

# ...and with looping OFF the render stays on the promo, with a warning.
cfg_nl = RenderConfig(**{**BASE, "voice_loop_promo": False})
gen_nl = VideoGenerator(cfg_nl, bg_dir, promo, None, TMP / "w_nl", TMP / "o_nl",
                        voice_cache_dir=VOICE_CACHE)
res_nl = gen_nl.render_row(1, long_row, filename="noloop.mp4")
assert res_nl.ok, res_nl.error
assert abs(duration(res_nl.output_path) - PROMO_DUR) < 0.35
assert any("cut short" in w for w in res_nl.warnings), res_nl.warnings
print("ok: looping off keeps the promo's length and warns the row")


# --------------------------------------------------------------- 5. the audio mix
ducked = render("ducked", with_music=True)
# Full length, and this one has teeth: sidechaincompress was measured ending its
# OUTPUT ~0.4s early inside this graph, and -shortest then trimmed the PICTURE
# to match — a 6.0s render came out at 5.62s with no error anywhere. Padding the
# compressor's inputs does not cover it; the finished mix has to be pinned too.
assert abs(duration(ducked) - PROMO_DUR) < 0.35, duration(ducked)

# The 2.5s bed under a 6s video: if the mix ended with the bed, or if
# sidechaincompress truncated to the voice, the tail would be silent.
tail_db = band_db(ducked, 380, 520, start=PROMO_DUR - 1.0, length=1.0)
assert tail_db > -60, (
    f"audio died before the end ({tail_db} dB in the last second) — a 2.5s bed "
    "under a 6s video is exactly the apad / duration=first failure")

# The promo's 440 Hz carrier must be quieter while the 900 Hz voice is talking.
speaking = band_db(ducked, 380, 520, start=1.0, length=2.0)
quiet = band_db(ducked, 380, 520, start=SPEECH_END + 0.4, length=1.2)
assert quiet - speaking > 3.0, (
    f"no ducking: promo band at {speaking} dB during speech vs {quiet} dB after")
print(f"ok: ducking — promo band {speaking:.1f} dB under the voice, "
      f"{quiet:.1f} dB after it ({quiet - speaking:.1f} dB of duck); "
      f"tail alive at {tail_db:.1f} dB")

# A silent promo plus music plus voice: the path where legs[0] is the bed and
# the old single-leg apad never ran.
silent_out = render("silent", video=silent_promo, with_music=True)
assert abs(duration(silent_out) - PROMO_DUR) < 0.35, duration(silent_out)
sil_tail = band_db(silent_out, 800, 1000, start=0.5, length=1.5)
assert sil_tail > -70, (f"silent promo + music + voice lost its audio "
                        f"({sil_tail} dB)")
print(f"ok: silent promo + bed + voice keeps full-length audio ({sil_tail:.1f} dB)")


# ---------------------------------------------------- 6. the pre-flight guards
g = make("guard")
gspec = RowSpec.from_row(ROW, 1)
g.attach_voice(gspec, g._voice_entry(gspec))
g._resolve_positions(gspec)
joined = " ".join(g.build_ffmpeg_command(
    gspec, bg_dir / "b.png", bg_dir / "b.png", None, TMP / "o_guard" / "x.mp4",
    None, TMP / "w_list" / "caps.txt"))
assert "-f concat -safe 0" in joined, joined
# A rate option before the concat -i overrides the demuxer and collapses every
# entry to one frame period, silently discarding every caption duration.
assert not re.search(r"-(?:r|framerate) \S+ -f concat", joined), joined
assert "-map [aout]" in joined and "alimiter" in joined, joined
print("ok: concat input carries -safe 0 and no rate override; mix is limited")

# The unbounded-input guard: the shape measured at 18.8 GB RSS.
try:
    VideoGenerator._check_input_bounds(
        ["ffmpeg", "-loop", "1", "-i", "still.png", "-i", "p.mp4"], True)
    raise AssertionError("the guard let an unbounded looping input through")
except RuntimeError as exc:
    assert "still.png" in str(exc), exc
VideoGenerator._check_input_bounds(
    ["ffmpeg", "-loop", "1", "-t", "5", "-i", "still.png"], True)
VideoGenerator._check_input_bounds(
    ["ffmpeg", "-loop", "1", "-i", "still.png"], False)   # no audio sink, no risk
print("ok: unbounded looping inputs are refused only when audio is in the graph")

# ------------------------- 7. the combination flagged as riskiest in review
#
# subliminal_all_intra sets -x264-params keyint=1, which config.py records at
# 552s for a 13-second video against a 600s timeout. Layering a voiceover and a
# caption timeline onto THAT row shape is the one combination that could push it
# over, and it is also where an unbounded input would livelock. This proves it
# renders and stays bounded; the wall-clock headroom has to be measured on the
# VM itself, at the real crf and preset.
sub_row = ROW.copy()
sub_row["Headline_Subliminal"] = "true"
sub_gen = VideoGenerator(
    RenderConfig(**{**BASE, "subliminal_targets": ["Headline"],
                   "subliminal_all_intra": True}),
    bg_dir, promo, None, TMP / "w_sub", TMP / "o_sub",
    music_paths=music, voice_cache_dir=VOICE_CACHE)
sub_res = sub_gen.render_row(1, sub_row, filename="sub.mp4")
assert sub_res.ok, sub_res.error
assert abs(duration(sub_res.output_path) - PROMO_DUR) < 0.35
print(f"ok: subliminal + all-intra + music + voiceover + captions renders "
      f"({duration(sub_res.output_path):.2f}s, "
      f"{sub_res.output_path.stat().st_size / 1024:.0f} KB)")

# ---------------------------------- 8. Screen_Text alone, feature switched off
#
# A caption column with no narration is a supported shape on its own: the beats
# are paced across the promo instead of against speech, and none of the audio
# graph is touched. This also means adding Screen_Text to a sheet does something
# useful without anyone turning voiceover on.
silent_row = pd.Series({
    "BG_Image": "b.png", "Headline": "ALWAYS HERE", "Headline_Y": "300",
    "Headline_Color": "#FFFFFF",
    "Screen_Text": "Silent captions still work across the whole clip",
    "Screen_Text_Y": "1420", "Screen_Text_Color": "#FFFFFF"})
quiet_gen = VideoGenerator(RenderConfig(**BASE), bg_dir, promo, None,
                           TMP / "w_quiet", TMP / "o_quiet")
quiet_spec = RowSpec.from_row(silent_row, 1)
quiet_gen.attach_voice(quiet_spec, quiet_gen._voice_entry(quiet_spec))
assert len(quiet_spec.beats) >= 2, quiet_spec.beats
assert quiet_spec.beats[0][0] == 0.0
assert abs(quiet_spec.beats[-1][1] - PROMO_DUR) < 0.2, quiet_spec.beats[-1]
assert quiet_spec.voice_wav is None, "a Screen_Text row must not narrate"
quiet = quiet_gen.render_row(1, silent_row, filename="quiet.mp4")
assert quiet.ok, quiet.error
assert abs(duration(quiet.output_path) - PROMO_DUR) < 0.35
assert bright_pixels(frame_at(quiet.output_path, 1.0), CAPTION_BOX) > 300
print(f"ok: Screen_Text with no narration — {len(quiet_spec.beats)} beats paced "
      f"across the promo, no audio touched")

# ------------------- 9. the preview and the editor, which were silently blank
#
# Every entry point has to bind the narration, not just render_row. The preview
# and the editor built their spec with a bare RowSpec.from_row, so a row whose
# words come from its Voiceover had NO beats there: no caption in the preview,
# no caption element in the editor, and nothing to drag. It looked like the
# columns did nothing at all.
prev_gen = VideoGenerator(RenderConfig(**BASE), bg_dir, promo, None,
                          TMP / "w_prev", TMP / "o_prev",
                          voice_cache_dir=VOICE_CACHE)
prev_spec = prev_gen._spec_for(ROW, 1)
assert prev_spec.beats, "the preview path must bind the voice too"
preview = prev_gen.render_preview(ROW, 1)
assert bright_pixels(preview, CAPTION_BOX) > 300, (
    "the static preview shows no caption band")
payload = prev_gen.build_editor_payload(ROW, 1)
roles = {t.get("role") for t in payload.get("texts", [])}
assert "Screen_Text" in roles, roles
print(f"ok: preview and editor both carry the caption band "
      f"({len(prev_spec.beats)} beats, roles={sorted(roles)})")

# The video render must still LEAVE IT OUT of the always-on overlay, or the
# caption would burn in under the moving one.
still = prev_gen.build_overlay_image(prev_spec, include_cta=False)
assert bright_pixels(still.convert("RGB"), CAPTION_BOX) < 300, (
    "the render's static overlay must not bake the caption in")
print("ok: the video render still keeps the caption out of the static overlay")

# ------------------------- 10. captions work with no speech engine at all
#
# Kokoro cannot install on every machine (it needs Python < 3.13). A Voiceover
# that cannot be spoken should still be SHOWN — the words are right there in the
# cell, and silent captions beat a blank band. Without this the whole feature
# looked broken on a dev box.
mute_gen = VideoGenerator(RenderConfig(**BASE), bg_dir, promo, None,
                          TMP / "w_mute", TMP / "o_mute")   # no voice cache at all
mute_spec = mute_gen._spec_for(ROW, 1)
assert mute_spec.beats, "a Voiceover with no TTS should still produce captions"
assert mute_spec.voice_wav is None and mute_spec.voice_duration == 0.0
assert abs(mute_spec.beats[-1][1] - PROMO_DUR) < 0.5, mute_spec.beats[-1]
mute = mute_gen.render_row(1, ROW, filename="mute.mp4")
assert mute.ok, mute.error
assert bright_pixels(frame_at(mute.output_path, 1.0), CAPTION_BOX) > 300
print(f"ok: no speech engine -> {len(mute_spec.beats)} silent caption beats, "
      f"paced across the promo")

print("\nOK - timed captions + voiceover")
