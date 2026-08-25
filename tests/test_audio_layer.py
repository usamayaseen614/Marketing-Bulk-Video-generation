"""The music bed and the split-audio warp: mixing, exact length, plumbing.

Both features fail quietly rather than loudly, which is why these are real
renders and not command-string assertions. A dropped `normalize=0` leaves the
mix audible but at the wrong ratio; a bare `apad` in place of `apad=whole_dur`
renders fine until the day `-t` is absent and then never terminates; a missing
`aformat` only breaks on a pool whose files disagree, which a tidy fixture never
does — so the fixture here deliberately disagrees.

The invariant the whole split-audio feature rests on is that the warped track is
EXACTLY as long as what went in, so most of this is duration arithmetic.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="audiotest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import random

import pandas as pd
from PIL import Image

from video_generator import (SPLIT_AUDIO_MAX_SPREAD, RenderConfig, RowSpec,
                             VideoGenerator, find_ffmpeg, split_audio_tempos)

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="audiolayer_"))
PROMO_DUR = 6.0


def ff(*args) -> subprocess.CompletedProcess:
    return subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", *args],
                          check=True, capture_output=True)


def duration(path: Path) -> float:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr)
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def streams(path: Path) -> str:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    return proc.stderr


# ---------------------------------------------------------------- the arithmetic
#
# This is the whole feature in one property: however the tempos come out, the
# playback times must add back up to the source length. Checked directly here
# because a render can only ever show it to a few milliseconds.
for n in (2, 3, 8, 24):
    for spread in (0.0, 0.1, 0.35, SPLIT_AUDIO_MAX_SPREAD):
        tempos = split_audio_tempos(n, spread, random.Random(n * 100 + int(spread * 10)))
        assert len(tempos) == n
        chunk = PROMO_DUR / n
        played = sum(chunk / t for t in tempos)
        assert abs(played - PROMO_DUR) < 1e-9, (n, spread, played)
        # atempo accepts 0.5-100; the spread cap has to keep every tempo inside
        # it with room to spare, or a row fails at render time instead of here.
        assert all(0.5 < t < 3.0 for t in tempos), (n, spread, tempos)
# An over-wide spread is clamped, not obeyed.
wild = split_audio_tempos(6, 5.0, random.Random(1))
assert all(0.5 < t < 3.0 for t in wild), wild
print("ok: tempo arithmetic is exact and bounded")


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

# A pool that DISAGREES with itself: different rates, channel counts, codecs
# and lengths. That is what a real folder of tracks looks like, and it is the
# only fixture that can catch a missing aformat before concat.
ff("-f", "lavfi", "-i", "sine=frequency=300:duration=2.5:sample_rate=44100",
   "-ac", "2", str(TMP / "t1.mp3"))
ff("-f", "lavfi", "-i", "sine=frequency=520:duration=1.5:sample_rate=48000",
   "-ac", "1", str(TMP / "t2.wav"))
ff("-f", "lavfi", "-i", "sine=frequency=760:duration=4.0:sample_rate=22050",
   "-ac", "2", str(TMP / "t3.m4a"))
music = [TMP / "t1.mp3", TMP / "t2.wav", TMP / "t3.m4a"]
row = pd.Series({"BG_Image": "b.png"})

BASE = dict(bg_color="#101010", preset="ultrafast", crf=32,
            video_x=90, video_y=300, video_w=900, video_h=900)


def render(name: str, video=promo, **kw) -> Path:
    cfg = RenderConfig(**BASE, **kw)
    gen = VideoGenerator(cfg, bg_dir, video, None, TMP / f"work_{name}",
                         TMP / f"out_{name}", music_paths=music)
    assert not gen.input_warnings, gen.input_warnings
    result = gen.render_row(1, row, filename=f"{name}.mp4")
    assert result.ok, result.error
    return result.output_path


# -------------------------------------------------- 1. nothing on = nothing changed
#
# The default audio path must keep the SHAPE it always had: a plain
# `-map <promo>:a?` and no filter graph. A regression here silently routes every
# existing user's batches through new code for no reason.
plain_cfg = RenderConfig(**BASE)
plain_gen = VideoGenerator(plain_cfg, bg_dir, promo, None, TMP / "w0", TMP / "o0")
plain_spec = RowSpec.from_row(row, 1)
plain_gen._resolve_positions(plain_spec)
cmd = plain_gen.build_ffmpeg_command(plain_spec, bg_dir / "b.png", bg_dir / "b.png",
                                     None, TMP / "o0" / "x.mp4")
joined = " ".join(cmd)
assert "-map 1:a?" in joined, joined
assert "atempo" not in joined and "amix" not in joined
assert plain_spec.music_clips is None
print("ok: with both features off the audio path is untouched")


# ------------------------------------------------------- 2. split audio, no music
out = render("split", split_audio=True, split_audio_chunks=6, split_audio_spread=0.4)
assert abs(duration(out) - PROMO_DUR) < 0.05, duration(out)
assert "Audio:" in streams(out)
print(f"ok: split audio alone -> {duration(out)}s (want {PROMO_DUR})")

# The warp must differ per row but reproduce for the same row, exactly like
# every other seeded choice in the renderer.
g = VideoGenerator(RenderConfig(**BASE, split_audio=True, split_audio_chunks=6),
                   bg_dir, promo, None, TMP / "ws", TMP / "os")
s1 = RowSpec.from_row(row, 1)
s2 = RowSpec.from_row(row, 2)
s1b = RowSpec.from_row(row, 1)
assert g._split_audio_tempos(s1) == g._split_audio_tempos(s1b)
assert g._split_audio_tempos(s1) != g._split_audio_tempos(s2)
print("ok: the warp is per-row and reproducible")


# ------------------------------------------------------- 3. music bed, no split
out = render("music", music_volume=0.25, music_min_seconds=1.0)
assert abs(duration(out) - PROMO_DUR) < 0.05, duration(out)
print(f"ok: music bed alone -> {duration(out)}s")

# The bed really is a SEQUENCE: a 6s promo at a 1s floor cannot be one track.
gm = VideoGenerator(RenderConfig(**BASE, music_volume=0.25, music_min_seconds=1.0),
                    bg_dir, promo, None, TMP / "wm", TMP / "om", music_paths=music)
sm = RowSpec.from_row(row, 1)
gm._resolve_positions(sm)
assert sm.music_clips and len(sm.music_clips) == len(sm.music_clip_repeats)
assert all(c in music for c in sm.music_clips)
cmd = " ".join(gm.build_ffmpeg_command(sm, bg_dir / "b.png", bg_dir / "b.png",
                                       None, TMP / "om" / "x.mp4"))
assert "normalize=0" in cmd, "amix must not rescale the weights"
assert "aformat" in cmd, "a mixed-rate pool needs aformat before concat"
assert "volume=0.7500" in cmd, "the promo leg must drop to 1 - music_volume"
print(f"ok: bed is a {len(sm.music_clips)}-track sequence, mixed complementarily")


# ------------------------------------------------------------ 4. both together
out = render("both", split_audio=True, split_audio_chunks=8, split_audio_spread=0.35,
             music_volume=0.15, music_min_seconds=1.0)
assert abs(duration(out) - PROMO_DUR) < 0.05, duration(out)
print(f"ok: split + music -> {duration(out)}s")


# ------------------------------------------- 5. a silent promo still gets music
out = render("silentpromo", video=silent_promo, music_volume=0.15,
             music_min_seconds=1.0)
assert "Audio:" in streams(out), "music must survive a promo with no audio"
assert abs(duration(out) - PROMO_DUR) < 0.05, duration(out)
print(f"ok: silent promo + music -> {duration(out)}s, audio present")

# ...and split audio on a silent promo is a no-op rather than a crash.
out = render("silentsplit", video=silent_promo, split_audio=True)
assert abs(duration(out) - PROMO_DUR) < 0.05, duration(out)
print("ok: split audio on a silent promo degrades to a no-op")


# ---------------------------------------------------- 6. include_audio still wins
gq = VideoGenerator(RenderConfig(**BASE, include_audio=False, split_audio=True,
                                 music_volume=0.5, music_min_seconds=1.0),
                    bg_dir, promo, None, TMP / "wq", TMP / "oq", music_paths=music)
sq = RowSpec.from_row(row, 1)
gq._resolve_positions(sq)
cmd = " ".join(gq.build_ffmpeg_command(sq, bg_dir / "b.png", bg_dir / "b.png",
                                       None, TMP / "oq" / "x.mp4"))
assert "-an" in cmd and "amix" not in cmd and "atempo" not in cmd, cmd
assert not sq.music_clips
print("ok: 'no audio' overrides both features")


# --------------------------------- 7. the bed is really in the mix, at the right level
#
# Every check above would pass just as happily if the music leg were built,
# mapped, and silent. So: mix a 2kHz track under a 440Hz promo and band-pass the
# output. A layer that failed to attach reads the same as one mixed at zero, and
# a dropped `normalize=0` reads as the wrong ratio rather than as an error.
ff("-f", "lavfi", "-i", "sine=frequency=2000:duration=8:sample_rate=44100",
   str(TMP / "hi.mp3"))
HI = [TMP / "hi.mp3"]


def band_db(path: Path, lo: int, hi: int) -> float:
    """Mean dB left after keeping only [lo, hi] Hz. Doubled filters for slope."""
    proc = subprocess.run(
        [FF, "-hide_banner", "-i", str(path), "-af",
         f"highpass=f={lo},highpass=f={lo},lowpass=f={hi},lowpass=f={hi},volumedetect",
         "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    return float(re.search(r"mean_volume: (\S+)", proc.stderr).group(1))


def render_hi(name: str, **kw) -> Path:
    gen = VideoGenerator(RenderConfig(**BASE, music_min_seconds=1.0, **kw),
                         bg_dir, promo, None, TMP / f"wk_{name}",
                         TMP / f"ot_{name}", music_paths=HI)
    result = gen.render_row(1, row, filename=f"{name}.mp4")
    assert result.ok, result.error
    return result.output_path


quiet = band_db(render_hi("vol0", music_volume=0.0), 1500, 2600)
low = band_db(render_hi("vol10", music_volume=0.10), 1500, 2600)
loud = band_db(render_hi("vol60", music_volume=0.60), 1500, 2600)
assert low > quiet + 6, ("the bed never reached the mix", quiet, low)
# 0.10 -> 0.60 is a factor of 6, i.e. 20*log10(6) = 15.6dB. Landing near that is
# what proves the gains are applied as given rather than renormalised by amix.
assert 12 < loud - low < 19, ("amix is rescaling the weights", low, loud)
print(f"ok: bed off {quiet:.1f}dB, at 10% {low:.1f}dB, at 60% {loud:.1f}dB "
      f"(+{loud - low:.1f}dB for a 6x gain, expected +15.6)")

# Three rows must warp three different ways, or the batch is 200 copies of one
# effect. Compared on the decoded samples, not the file size.
warped = []
gw = VideoGenerator(RenderConfig(**BASE, split_audio=True, split_audio_chunks=6,
                                 split_audio_spread=0.45),
                    bg_dir, promo, None, TMP / "wvar", TMP / "ovar")
for n in (1, 2, 3):
    out = gw.render_row(n, row, filename=f"var{n}.mp4").output_path
    wav = out.with_suffix(".wav")
    ff("-i", str(out), "-vn", "-ac", "1", str(wav))
    warped.append(wav.read_bytes())
assert len({w[:200_000] for w in warped}) == 3, "every row warped identically"
print("ok: three rows produce three distinct warped tracks")


# ------------------------------------------- 8. a non-audio file is dropped, not fatal
gb = VideoGenerator(RenderConfig(**BASE, music_volume=0.2), bg_dir, promo, None,
                    TMP / "wb", TMP / "ob",
                    music_paths=music + [bg_dir / "b.png"])
assert any("b.png" in w for w in gb.input_warnings), gb.input_warnings
assert len(gb.music_paths) == len(music)
print("ok: a stray non-audio file is warned about and skipped")

print("\nall audio-layer checks passed")
