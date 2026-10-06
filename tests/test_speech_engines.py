"""Two speech engines behind one interface: dispatch, cache keys, and the
Pocket TTS adapter.

Neither engine is installed here — Kokoro cannot take Python 3.14 and
pocket-tts is deliberately marked the same way in requirements.txt — so the
Pocket path is driven through a FAKE `pocket_tts_timestamped` module standing
in for the real one. That is the same trick test_voice_captions.py plays with
the voice cache, and it covers the half that actually breaks silently: the
adapter's reading of the package's result object. The fake mirrors the real
API exactly as the shipped wheel defines it —

    TTSModel.load_model() -> model
    model.sample_rate                                     int
    model.get_state_for_audio_prompt(voice)               -> state
    model.generate_audio_with_timestamps(state, text)     -> .audio, .words
    model.generate_audio(state, text)                     -> tensor
    WordTimestamp(.word, .word_index, .start_time, .end_time)   SECONDS

— so an upstream change to any of those names shows up as this file passing
against a shape the real package no longer has, which is the honest limit of a
fake and is why the Dockerfile also loads the real model at build time.
"""
import json
import os
import sys
import tempfile
import types
import wave
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="enginetest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from speech import synth
from video_generator import RenderConfig, RowSpec

TMP = Path(tempfile.mkdtemp(prefix="engines_"))
RATE = 24000

# ---------- 1. the engine registry --------------------------------------
assert list(synth.ENGINES) == [synth.KOKORO, synth.POCKET], synth.ENGINES
assert synth.DEFAULT_ENGINE == synth.KOKORO
# The two voice catalogues share NO names. That is what forces the sidebar to
# re-offer the list per engine and what makes a stale VOICE_SET unusable on the
# other one — both behaviours are asserted further down.
assert not set(synth.voices(synth.KOKORO)) & set(synth.voices(synth.POCKET))
assert synth.default_voice(synth.KOKORO) in synth.voices(synth.KOKORO)
assert synth.default_voice(synth.POCKET) in synth.voices(synth.POCKET)
# Only the English Pocket voices are offered: the package also ships giovanni /
# lola / juergen / rafael / estelle, which need load_model(language=...) and
# would otherwise give English in an Italian accent rather than Italian.
for foreign in ("giovanni", "lola", "juergen", "rafael", "estelle"):
    assert foreign not in synth.voices(synth.POCKET), foreign
# Anything unrecognised resolves to the default rather than raising: this string
# travels through a stored job's params, so a batch queued by an older build has
# to keep running.
for junk in (None, "", "nope", "KOKORO", " Pocket "):
    assert synth.normalize_engine(junk) in synth.ENGINES, junk
assert synth.normalize_engine("KOKORO") == synth.KOKORO
assert synth.normalize_engine(" Pocket ") == synth.POCKET
assert synth.supports_speed(synth.KOKORO) and not synth.supports_speed(synth.POCKET)
print(f"registry: {len(synth.voices(synth.KOKORO))} Kokoro voices, "
      f"{len(synth.voices(synth.POCKET))} Pocket voices, no overlap")

# The Dockerfile warms every offered Pocket voice at build time, and it has to
# name them itself — the app's modules are COPYed in below that layer, so it
# cannot import POCKET_VOICES. That duplication is deliberate (it protects the
# layer cache) but it can drift, and drifting is invisible: a voice missing from
# the image simply downloads mid-render, which is the 3am failure the bake
# exists to prevent. So the two are pinned to each other here.
_docker = (PROJ / "Dockerfile").read_text(encoding="utf-8")
_start = _docker.index("voices = '", _docker.index("pocket_tts_timestamped"))
_baked = _docker[_start:_docker.index("'.split()", _start)]
_baked = _baked.split("'", 1)[1].replace("\\\n", " ").split()
assert sorted(_baked) == sorted(synth.voices(synth.POCKET)), (
    "Dockerfile and POCKET_VOICES disagree:\n"
    f"  only in Dockerfile: {sorted(set(_baked) - set(synth.voices(synth.POCKET)))}\n"
    f"  only in synth.py:   {sorted(set(synth.voices(synth.POCKET)) - set(_baked))}")
print(f"dockerfile: bakes the same {len(_baked)} Pocket voices the sidebar offers")

# ---------- 2. cache keys keep the engines apart ------------------------
# The whole point: the same script, voice and speed under two engines must NOT
# resolve to one wav, or the first engine to synthesize would speak for both.
assert (synth.cache_key("hi", "alba", 1.0, "a", synth.KOKORO)
        != synth.cache_key("hi", "alba", 1.0, "a", synth.POCKET))
# Kokoro HAS a speed, so speed is part of its key.
assert (synth.cache_key("hi", "af_heart", 1.0, "a", synth.KOKORO)
        != synth.cache_key("hi", "af_heart", 1.2, "a", synth.KOKORO))
# Pocket has none, so it is folded out — otherwise the pre-render stage would
# dedupe a 60-script pool at three speeds into 180 byte-identical syntheses.
assert (synth.cache_key("hi", "alba", 1.0, "a", synth.POCKET)
        == synth.cache_key("hi", "alba", 1.2, "a", synth.POCKET))
# Omitting the engine is Kokoro, which is what keeps every existing caller and
# the hand-written cache entries in test_voice_captions.py working unchanged.
assert (synth.cache_key("hi", "af_heart", 1.0, "a")
        == synth.cache_key("hi", "af_heart", 1.0, "a", synth.KOKORO))
print("cache keys: engine separates, Kokoro keeps speed, Pocket folds it out")


# ---------- 3. the Pocket adapter, against a fake package ---------------
class _Word:
    __slots__ = ("word", "word_index", "start_time", "end_time")

    def __init__(self, word, index, start, end):
        self.word, self.word_index = word, index
        self.start_time, self.end_time = start, end


class _Timestamped:
    def __init__(self, audio, words):
        self.audio, self.words = audio, words


class _FakeTensor:
    """Stands in for a torch tensor: .detach().cpu().numpy() and nothing else.
    If the adapter reaches for any other torch API this breaks here rather than
    on the VM."""

    def __init__(self, array):
        self._a = np.asarray(array, dtype="float32")

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self._a


class _FakeModel:
    sample_rate = RATE
    SECONDS = 2.0

    def __init__(self):
        self.calls = []

    @classmethod
    def load_model(cls, *a, **kw):
        cls.loads = getattr(cls, "loads", 0) + 1
        return cls()

    def get_state_for_audio_prompt(self, voice, truncate=False):
        if voice not in synth.voices(synth.POCKET):
            raise ValueError(f"unknown voice {voice!r}")
        return {"voice": voice}

    def _tone(self):
        n = int(self.SECONDS * RATE)
        return _FakeTensor(0.2 * np.sin(np.arange(n) * 0.05))

    def generate_audio_with_timestamps(self, state, text, **kw):
        self.calls.append(("timed", state, text))
        parts = text.split()
        per = self.SECONDS / max(1, len(parts))
        return _Timestamped(
            self._tone(),
            tuple(_Word(w, i, i * per, (i + 1) * per)
                  for i, w in enumerate(parts)))

    def generate_audio(self, state, text, **kw):
        self.calls.append(("plain", state, text))
        # Upstream returns [channels, samples] — a DIFFERENT shape from the
        # timestamped path's flat tensor. The adapter must flatten both.
        return _FakeTensor(self._tone().numpy().reshape(1, -1))


def install_fake(timestamped: bool) -> None:
    """Put a fake Pocket package on sys.modules and clear synth's caches."""
    for name in ("pocket_tts_timestamped", "pocket_tts"):
        sys.modules.pop(name, None)
    module = types.ModuleType(
        "pocket_tts_timestamped" if timestamped else "pocket_tts")
    module.TTSModel = _FakeModel
    module.WordTimestamp = _Word
    sys.modules[module.__name__] = module
    synth._unavailable.clear()
    synth._pocket_models.clear()
    _FakeModel.loads = 0


def read_wav(path: Path) -> tuple:
    with wave.open(str(path), "rb") as h:
        return h.getframerate(), h.getnchannels(), h.getnframes()


# --- the fork: real word timings
install_fake(timestamped=True)
ok, why = synth.available(synth.POCKET)
assert ok, why
cache = TMP / "c1"
TEXT = "one two three four five six"
entry = synth.synthesize(TEXT, "alba", 1.0, cache, "a", 0.0, synth.POCKET)
assert entry, "the fake engine produced nothing"
assert abs(entry["duration"] - _FakeModel.SECONDS) < 0.01, entry["duration"]
rate, channels, frames = read_wav(entry["wav"])
assert (rate, channels) == (RATE, 1), (rate, channels)
assert abs(frames / rate - _FakeModel.SECONDS) < 0.01
words = entry["words"]
assert [w["text"] for w in words] == TEXT.split(), words
# Seconds, not milliseconds. The package computes these as
# `generated_samples / sample_rate`, so the last word must END at the audio's
# own length — a 1000x unit slip would put it at 2000 and every caption would
# sit past the end of the video.
assert abs(words[-1]["end"] - _FakeModel.SECONDS) < 0.05, words[-1]
assert all(w["start"] < w["end"] for w in words), words
assert all(words[i]["start"] >= words[i - 1]["end"] - 1e-9
           for i in range(1, len(words))), words
assert not any(w.get("estimated") for w in words), "these timings are real"
print(f"pocket (fork): {len(words)} words, last ends at "
      f"{words[-1]['end']:.2f}s of {_FakeModel.SECONDS}s audio")

# The cache is honoured, and the model is loaded ONCE per process — load_model
# is documented slow and the voice stage runs a whole batch through one process.
before = _FakeModel.loads
again = synth.synthesize(TEXT, "alba", 1.0, cache, "a", 0.0, synth.POCKET)
assert again["wav"] == entry["wav"]
assert _FakeModel.loads == before, "a cache hit must not load the model"
synth.synthesize(TEXT + " seven", "alba", 1.0, cache, "a", 0.0, synth.POCKET)
assert _FakeModel.loads == before, "the model must be reused, not reloaded"

# Lead-in: silence is PREPENDED and the words move with it, so the wav still
# starts at t=0 and the FFmpeg audio graph needs no delay filter.
led = synth.synthesize(TEXT, "anna", 1.0, TMP / "c2", "a", 0.75, synth.POCKET)
assert abs(led["duration"] - (_FakeModel.SECONDS + 0.75)) < 0.02, led["duration"]
assert abs(led["words"][0]["start"] - 0.75) < 0.05, led["words"][0]
assert abs(led["words"][-1]["end"] - (_FakeModel.SECONDS + 0.75)) < 0.05
print(f"pocket (fork): 0.75s lead-in shifts the first word to "
      f"{led['words'][0]['start']:.2f}s")

# A voice the engine does not know fails the row, it does not crash the batch.
assert synth.synthesize(TEXT, "af_heart", 1.0, TMP / "c3", "a", 0.0,
                        synth.POCKET) is None, \
    "a Kokoro voice name must not silently work on Pocket"

# --- upstream: no timings, words spread evenly and SAID to be estimated
install_fake(timestamped=False)
ok, why = synth.available(synth.POCKET)
assert ok, why
plain = synth.synthesize(TEXT, "alba", 1.0, TMP / "c4", "a", 0.0, synth.POCKET)
assert plain, "upstream pocket_tts must still narrate"
assert abs(plain["duration"] - _FakeModel.SECONDS) < 0.01
assert [w["text"] for w in plain["words"]] == TEXT.split()
assert all(w.get("estimated") for w in plain["words"]), \
    "estimated timings must say so, or they read as synced"
# Evenly spread across the audio, and still ending with it.
assert abs(plain["words"][-1]["end"] - _FakeModel.SECONDS) < 0.05
print("pocket (upstream): narrates, words spread evenly and marked estimated")

# The FORK installed but returning no alignment at all — a model config without
# timestamp heads. Same outcome as upstream, and it must be flagged the same
# way: the flag has to follow whether timings were MEASURED, not which module
# happened to import.
class _NoAlignModel(_FakeModel):
    def generate_audio_with_timestamps(self, state, text, **kw):
        return _Timestamped(self._tone(), ())      # audio, no words


install_fake(timestamped=True)
sys.modules["pocket_tts_timestamped"].TTSModel = _NoAlignModel
blind = synth.synthesize(TEXT, "alba", 1.0, TMP / "c6", "a", 0.0, synth.POCKET)
assert blind, "no alignment must still narrate"
assert [w["text"] for w in blind["words"]] == TEXT.split()
assert all(w.get("estimated") for w in blind["words"]), \
    "the fork returning no words is just as estimated as upstream"
print("pocket (fork, no alignment): narrates, and still marked estimated")

# --- neither installed
for name in ("pocket_tts_timestamped", "pocket_tts"):
    sys.modules.pop(name, None)
sys.modules["pocket_tts_timestamped"] = None   # import raises ImportError
sys.modules["pocket_tts"] = None
synth._unavailable.clear()
synth._pocket_models.clear()
ok, why = synth.available(synth.POCKET)
assert not ok and "pocket-tts-timestamped" in why, why
assert synth.synthesize(TEXT, "alba", 1.0, TMP / "c5", "a", 0.0,
                        synth.POCKET) is None
del sys.modules["pocket_tts_timestamped"], sys.modules["pocket_tts"]
synth._unavailable.clear()
print("pocket (absent): reports the pip name and renders silent, never raises")


# ---------- 4. the renderer side: engine travels, speed warns -----------
# RenderConfig carries the engine and DEFAULTS, so a job queued before this
# feature rehydrates through RenderConfig(**stored) and runs on Kokoro.
assert RenderConfig().voice_engine == synth.KOKORO
stored = {"voice_enabled": True, "voice_set": ["af_heart"]}     # no voice_engine
assert RenderConfig(**stored).voice_engine == synth.KOKORO

# A Voiceover_Speed cell an engine cannot honour is named on the row, because
# the same sheet DOES change pace on the other engine.
class _Gen:
    """attach_voice without a model, an FFmpeg or a promo — the warning path is
    pure config, and building a VideoGenerator needs a real video file."""
    from video_generator import VideoGenerator as _V
    attach_voice = _V.attach_voice
    _promo_alt = False
    _render_duration = staticmethod(lambda spec: 10.0)
    _probe_duration = staticmethod(lambda path=None: 10.0)
    video_path = None

    def __init__(self, cfg):
        self.config = cfg


def warnings_for(engine, speed, voice=None, entry=None, grep="speaking-speed"):
    cfg = RenderConfig(voice_enabled=True, voice_engine=engine)
    spec = RowSpec.from_row(pd.Series(
        {"Voiceover": "some narration", "Voiceover_Speed": speed,
         "Voiceover_Voice": voice}), 1)
    _Gen(cfg).attach_voice(spec, entry)
    return [w for w in spec.warnings if grep in w]


assert warnings_for(synth.POCKET, 1.4), "an ignored speed cell must be named"
assert "Pocket TTS" in warnings_for(synth.POCKET, 1.4)[0]
assert not warnings_for(synth.POCKET, 1.0), "1.0x changes nothing, so no noise"
assert not warnings_for(synth.KOKORO, 1.4), "Kokoro honours it"
assert not warnings_for(synth.POCKET, None), "no cell, no warning"

# The fatal one: a Voiceover_Voice cell from the OTHER engine's catalogue. The
# two share no names, so a sheet built for Kokoro goes entirely silent the
# moment the dropdown says Pocket — and finishes as a success. Named on the row.
V = "Voiceover_Voice"
assert "silent" in warnings_for(synth.POCKET, None, voice="af_heart", grep=V)[0]
assert warnings_for(synth.KOKORO, None, voice="alba", grep=V), "and the reverse"
assert not warnings_for(synth.POCKET, None, voice="alba", grep=V), "own voice: fine"
assert not warnings_for(synth.KOKORO, None, voice="af_heart", grep=V)
# A row that DID synthesize is never shouted about, whatever the cell says —
# both engines also accept a path or an hf:// URL that is not in the list.
ok_entry = {"wav": str(TMP / "x.wav"), "duration": 2.0,
            "words": [{"text": "some", "start": 0.0, "end": 1.0},
                      {"text": "narration", "start": 1.0, "end": 2.0}]}
assert not warnings_for(synth.POCKET, None, voice="hf://kyutai/tts-voices/x.wav",
                        entry=ok_entry, grep=V)

# Estimated captions are named on the row. Without this the flag is dead data:
# paced and synced captions look identical in the output and only diverge as
# drift on a long script.
def caption_warnings(words):
    cfg = RenderConfig(voice_enabled=True, voice_engine=synth.POCKET)
    spec = RowSpec.from_row(pd.Series({"Voiceover": "one two three"}), 1)
    _Gen(cfg).attach_voice(spec, {"wav": str(TMP / "x.wav"), "duration": 3.0,
                                  "words": words})
    return [w for w in spec.warnings if "no word timings" in w]


real = [{"text": "one", "start": 0.0, "end": 1.0},
        {"text": "two", "start": 1.0, "end": 2.0}]
guessed = [dict(w, estimated=True) for w in real]
assert caption_warnings(guessed), "paced captions must say so"
assert not caption_warnings(real), "measured timings must not be warned about"
assert not caption_warnings([]), "no words at all is a different case"
# One measured word among estimated ones is still partly real — don't cry wolf.
assert not caption_warnings([real[0], guessed[1]]), "only ALL-estimated warns"
print("renderer: engine defaults to Kokoro on old jobs; ignored speed and "
      "estimated captions are both named")

print("ALL 4 SPEECH ENGINE TESTS PASSED")
