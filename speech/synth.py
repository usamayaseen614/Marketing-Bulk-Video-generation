"""
speech/synth.py — text-to-speech, with the word timings that make the timed
caption layer possible.

TWO ENGINES, one interface. `kokoro` and `pocket` (Kyutai's Pocket TTS) are
picked per batch from the sidebar; everything above this module — the renderer,
the pre-render voice stage, the caption beats — is written against the functions
here and never learns which one ran.

The only module in the repo that imports either package, and it imports them
lazily, for two reasons: both pull PyTorch (so nothing else should pay for it at
import time), and neither is installable everywhere — Kokoro requires Python
3.10-3.12 while the dev venv here is 3.14. A missing or broken install must
leave `app.py` starting normally with that engine switched off, not traceback on
the import line, which is why `available()` is per-engine.

WHY THERE IS NO SPEECH-RECOGNITION STEP: tools like CapCut transcribe finished
audio to recover word timings. We generate the audio, and both engines hand the
timings back as a normal part of synthesis — Kokoro's KPipeline returns
`result.tokens` carrying start_ts/end_ts, and pocket-tts-timestamped returns
`result.words` carrying start_time/end_time (seconds, measured against the
decoder's own sample count). So the timings are exact rather than inferred,
there is no aligner to install, and nobody has to mark beat boundaries up by
hand. Upstream `pocket_tts` has no timestamps at all; it is accepted as a
fallback and its words are spread evenly, which the caller can see because
`words` comes back with `estimated: True`.

WHAT THE TWO DO NOT SHARE: Kokoro takes a speaking `speed` and a `lang_code`;
pocket-tts takes neither — its voice carries the language and the model has no
rate control. `supports_speed()` is what the UI and the row warnings gate on,
and `cache_key` folds the unsupported one out so two speeds cannot occupy two
cache entries holding byte-identical audio.

Output is cached by content — sha1(engine|lang|voice|speed|text) — because a
600-video folder dealt from a 60-script pool needs 60 syntheses, not 600.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000          # Kokoro's native output rate; pocket-tts reports
                             # its own at runtime (also 24k today, not assumed)
# Bumped from "k1" when the engine joined the key: a wav cached before that
# carries no engine in its name, so the two engines could otherwise serve each
# other's audio for the same script. Invalidating the old entries costs one
# re-synthesis per script and is the documented way to do this.
_CACHE_VERSION = "k2"

KOKORO = "kokoro"
POCKET = "pocket"
# What the sidebar dropdown offers, in order. Keys are what travels in
# RenderConfig.voice_engine and in the cache key, so they are short and stable;
# the values are only ever shown.
ENGINES = {
    KOKORO: "Kokoro",
    POCKET: "Pocket TTS (Kyutai)",
}
DEFAULT_ENGINE = KOKORO

# The English voices shipped with Kokoro v1.0. a*/b* are American/British,
# *f_/*m_ female/male. Other languages exist but need a different lang_code and
# the matching misaki extra, so they are deliberately not offered here.
KOKORO_VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

# Pocket TTS's predefined voices, from the package's own
# _ORIGINS_OF_PREDEFINED_VOICES catalogue. The ENGLISH ones only, on exactly the
# reasoning above: the catalogue also ships giovanni/lola/juergen/rafael/estelle
# for Italian/Spanish/German/Portuguese/French, but those need load_model() to
# be given the matching `language`, and the sidebar offers no such choice. Using
# one against the English model gives English in that accent, not that language,
# which is the kind of half-working that reads as a bug.
POCKET_VOICES = [
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette",
    "eponine", "eve", "fantine", "george", "jane", "javert", "jean", "marius",
    "mary", "michael", "paul", "peter_yearsley", "stuart_bell", "vera",
]

VOICES_BY_ENGINE = {KOKORO: KOKORO_VOICES, POCKET: POCKET_VOICES}
DEFAULT_VOICE_BY_ENGINE = {KOKORO: "af_heart", POCKET: "alba"}


def normalize_engine(engine: Optional[str]) -> str:
    """Whatever arrived, as one of ENGINES. An unknown value is the default
    rather than an error: this string travels through a job's params and a
    stored RenderConfig, so a batch queued by an older build must still run."""
    key = str(engine or "").strip().lower()
    return key if key in ENGINES else DEFAULT_ENGINE


def voices(engine: Optional[str] = None) -> list[str]:
    """The voice names this engine accepts. The two lists share no names, which
    is why the sidebar has to re-offer them when the engine changes and why a
    `Voiceover_Voice` cell is only meaningful next to its own engine."""
    return list(VOICES_BY_ENGINE[normalize_engine(engine)])


def default_voice(engine: Optional[str] = None) -> str:
    return DEFAULT_VOICE_BY_ENGINE[normalize_engine(engine)]


def supports_speed(engine: Optional[str] = None) -> bool:
    """Whether this engine has a speaking-rate control at all.

    Kokoro takes `speed` straight into the pipeline. Pocket TTS has no such
    parameter — `generate_audio` takes the text and the voice state and nothing
    else. Rather than stretch the finished wav behind the user's back, the
    sidebar disables the slider and the row says its `Voiceover_Speed` cell was
    ignored."""
    return normalize_engine(engine) == KOKORO


_pipelines: dict[str, object] = {}
_pocket_models: dict[str, object] = {}
_unavailable: dict[str, Optional[str]] = {}


def configure_threads(n: int = 1) -> None:
    """Pin torch to `n` threads. The voice stage fans out over unique scripts
    with its own pool; letting each worker also spin a torch thread pool turns
    a 112-core box into 112 processes each asking for 112 threads."""
    try:
        import torch
        torch.set_num_threads(max(1, int(n)))
    except Exception:                                   # noqa: BLE001
        pass


def _import_pocket():
    """The Pocket TTS module to use, and whether it can time words.

    Prefers `pocket_tts_timestamped`, the fork that adds
    generate_audio_with_timestamps(); falls back to Kyutai's upstream
    `pocket_tts`, which synthesizes perfectly well but hands back no timings at
    all. The fallback is worth having — a machine with only upstream installed
    should narrate rather than refuse — but the two are not equivalent, and
    which one ran is carried out of synthesize() on every word so the caller can
    say the captions are approximate."""
    try:
        import pocket_tts_timestamped as module
        return module, True
    except Exception as fork_exc:                       # noqa: BLE001
        try:
            import pocket_tts as module
        except Exception as base_exc:                   # noqa: BLE001
            # Report the FORK's failure, because that is the package to install
            # — reporting upstream's would send someone to `pip install
            # pocket-tts` and quietly cost them the word timings.
            raise ImportError(str(fork_exc)) from base_exc
        return module, False


def available(engine: Optional[str] = None) -> tuple[bool, str]:
    """(usable, reason) for one engine. Never raises, so a UI can gate on it.

    Cached per engine rather than globally: the answer differs between them
    (Kokoro cannot install above Python 3.12, Pocket TTS can), and the sidebar
    asks about whichever one is selected."""
    engine = normalize_engine(engine)
    cached = _unavailable.get(engine)
    if cached is not None:
        return (False, cached) if cached else (True, "")
    try:
        if engine == POCKET:
            _import_pocket()
        else:
            import kokoro  # noqa: F401
        _unavailable[engine] = ""
        return True, ""
    except Exception as exc:                            # noqa: BLE001
        name = "Kokoro" if engine == KOKORO else "Pocket TTS"
        hint = ("" if engine == KOKORO else
                " (pip install pocket-tts-timestamped)")
        _unavailable[engine] = (
            f"{name} is not installed in this environment ({exc}){hint}. "
            "Voiceover is disabled; everything else renders as normal.")
        return False, _unavailable[engine]


def _pipeline(lang_code: str):
    if lang_code not in _pipelines:
        from kokoro import KPipeline
        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _pipelines[lang_code]


def _pocket_model():
    """The loaded Pocket TTS model, once per process.

    load_model() is documented as slow and downloads weights on first use, and
    the voice stage synthesizes a whole batch's scripts in one process — so this
    is the same cache `_pipelines` is, for the same reason. The model is also
    documented NOT thread-safe; nothing here shares one across threads (the
    stage fans out over PROCESSES, and its fallback is serial)."""
    if "model" not in _pocket_models:
        module, timed = _import_pocket()
        _pocket_models["model"] = module.TTSModel.load_model()
        _pocket_models["timed"] = timed
    return _pocket_models["model"], _pocket_models["timed"]


def normalize(text: str) -> str:
    """The exact text the cache key is computed over. Shared so the renderer's
    lookup and this module's write can never disagree about whitespace."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def cache_key(text: str, voice: str, speed: float, lang_code: str,
              engine: Optional[str] = None) -> str:
    """The name a synthesis is stored under.

    `speed` is folded to 1.0 for an engine that has no rate control, so two rows
    asking for 0.9x and 1.2x of the same script share the one entry they would
    have produced anyway. That is not a saving for its own sake: the pre-render
    stage DEDUPES on this key, so without it a 60-script pool at three speeds
    would synthesize 180 identical wavs. Both sides compute it here, so the
    stage's key and the renderer's lookup cannot drift apart."""
    engine = normalize_engine(engine)
    if not supports_speed(engine):
        speed = 1.0
    raw = _CACHE_VERSION + "|" + engine + "|" + lang_code + "|" + voice
    raw += "|" + format(float(speed), ".3f") + "|" + normalize(text)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_cached(text: str, voice: str, speed: float, cache_dir: Path,
                lang_code: str = "a",
                engine: Optional[str] = None) -> Optional[dict]:
    """Read a previously synthesized narration, or None.

    This is the renderer's whole interface to speech: the pre-render stage
    fills the cache, and render_row only ever LOOKS UP. Nothing here imports
    kokoro or touches a model, which is the point — rows render 16-wide on a
    box that has already OOM-killed sixteen parallel FFmpegs."""
    if not text or not cache_dir:
        return None
    key = cache_key(text, voice, speed, lang_code, engine)
    wav_path = Path(cache_dir) / (key + ".wav")
    meta_path = Path(cache_dir) / (key + ".json")
    if not (wav_path.is_file() and meta_path.is_file()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {"wav": wav_path, "duration": float(meta["duration"]),
                "words": meta["words"]}
    except Exception:                                   # noqa: BLE001
        logger.warning("Voice cache entry %s is unreadable", key)
        return None


def _write_wav(path: Path, samples, sample_rate: int = SAMPLE_RATE) -> float:
    """16-bit PCM via the stdlib. No soundfile, no pydub — one more dependency
    for something `wave` already does.

    `sample_rate` is a parameter rather than the module constant because the two
    engines report their own. They agree at 24 kHz today; writing the header
    from whatever the engine actually returned is what keeps a future change on
    either side from producing a wav that plays at the wrong pitch — and the
    duration returned here is what the whole render length is derived from, so
    getting the rate wrong would mis-time every caption as well."""
    import numpy as np
    audio = np.asarray(samples, dtype="float32").reshape(-1)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return len(audio) / float(sample_rate)


def _clean_words(words: list[dict], offset: float, seg_len: float) -> list[dict]:
    """Rebase, gap-fill and make a word list monotonic. Shared by both engines.

    Everything here is about beats that would otherwise be unusable rather than
    merely imprecise: a word with no timing at all (both engines can produce
    one), and a word whose end lands at or before its start — which shows for no
    frames and writes a 0.000 duration into the caption concat list.

    A word whose timing this function INVENTED is marked `estimated`, which is
    the one thing the caller cannot work out afterwards — an interpolated span
    and a measured one look identical once they are numbers. attach_voice warns
    the row when every beat carries it, because "the captions are synced to the
    speech" is the feature's whole promise and a silently paced row breaks it."""
    if not words:
        return []
    per = seg_len / len(words) if len(words) else 0.0
    for i, word in enumerate(words):
        if word["start"] is None:
            word["start"] = i * per
            word["estimated"] = True
        if word["end"] is None:
            word["end"] = min(seg_len, float(word["start"]) + per)
            word["estimated"] = True
        word["start"] = float(word["start"]) + offset
        word["end"] = float(word["end"]) + offset
    for i, word in enumerate(words):
        if i and word["start"] < words[i - 1]["end"]:
            word["start"] = words[i - 1]["end"]
        if word["end"] <= word["start"]:
            word["end"] = word["start"] + 0.04
    return words


def _words_from_tokens(tokens, offset: float, fallback_text: str,
                       seg_len: float) -> list[dict]:
    """Rebuild word spans from Kokoro's per-token timestamps.

    A token is usually a word, but misaki can split one, so tokens are
    accumulated until a whitespace boundary. Timestamps are model output and can
    be absent (documented as weaker on some voices) — a word without them falls
    back to an even share of the segment, which keeps captions flowing at
    roughly the right pace instead of collapsing into zero-length beats."""
    words: list[dict] = []
    buf, start, end = "", None, None
    for token in tokens or ():
        text = getattr(token, "text", "") or ""
        ts, te = getattr(token, "start_ts", None), getattr(token, "end_ts", None)
        if ts is not None:
            start = ts if start is None else min(start, ts)
        if te is not None:
            end = te if end is None else max(end, te)
        buf += text
        if getattr(token, "whitespace", ""):
            if buf.strip():
                words.append({"text": buf.strip(), "start": start, "end": end})
            buf, start, end = "", None, None
    if buf.strip():
        words.append({"text": buf.strip(), "start": start, "end": end})

    if not words:
        words = [{"text": w, "start": None, "end": None}
                 for w in str(fallback_text).split() if w]
    # Gaps are filled by interpolating across the segment in one pass, so a
    # partially-timestamped segment keeps the timings it did get.
    return _clean_words(words, offset, seg_len)


def _words_from_timestamps(timed, text: str, seg_len: float) -> list[dict]:
    """Word spans from pocket-tts-timestamped's `result.words`.

    Each entry is a WordTimestamp carrying .word/.start_time/.end_time, and the
    times are SECONDS — the package computes them as
    `time_offset + generated_samples / self.sample_rate` against the decoder's
    own sample count, so they are on the same clock as the audio this call
    returns and need no scaling.

    An empty list is the upstream-`pocket_tts` case (no timestamp support at
    all) and also what a timestamp model with no alignment heads returns: fall
    back to the text spread evenly, exactly as the Kokoro path does when a
    segment comes back untimed."""
    words = [{"text": str(getattr(w, "word", "") or "").strip(),
              "start": getattr(w, "start_time", None),
              "end": getattr(w, "end_time", None)}
             for w in (timed or ())]
    words = [w for w in words if w["text"]]
    if not words:
        words = [{"text": w, "start": None, "end": None}
                 for w in str(text).split() if w]
    return _clean_words(words, 0.0, seg_len)


def _to_float32(audio):
    """A torch tensor or an array, as one flat float32 numpy array.

    Both engines return torch tensors, and pocket-tts returns a different SHAPE
    from each of its two entry points — [channels, samples] out of
    generate_audio() and a flat 1-D concatenation out of
    generate_audio_with_timestamps(). reshape(-1) is what makes that
    difference stop mattering; it is also why the mono wav writer is correct
    for both."""
    import numpy as np
    raw = audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio
    return np.asarray(raw, dtype="float32").reshape(-1)


def _synth_kokoro(text: str, voice: str, speed: float, lang_code: str,
                  lead_in: float) -> tuple:
    """(audio, words, sample_rate) from Kokoro, or (None, [], rate)."""
    import numpy as np
    pipeline = _pipeline(lang_code)
    chunks: list = []
    words: list[dict] = []
    cursor = float(max(0.0, lead_in))
    if cursor > 0:
        chunks.append(np.zeros(int(cursor * SAMPLE_RATE), dtype="float32"))
    # One continuous read. Kokoro may still segment internally; each
    # segment's timestamps are rebased by `cursor` so the words come back
    # on a single timeline.
    for result in pipeline(text, voice=voice, speed=float(speed)):
        audio = getattr(result, "audio", None)
        if audio is None and isinstance(result, (tuple, list)) and result:
            # Older kokoro yields plain (graphemes, phonemes, audio) tuples
            # rather than a Result. Worth catching: the difference between
            # the two is silent — .audio would simply be absent and every
            # row would come out with no narration and no error.
            audio = result[-1]
        if audio is None:
            continue
        audio = _to_float32(audio)
        seg_len = len(audio) / float(SAMPLE_RATE)
        words.extend(_words_from_tokens(
            getattr(result, "tokens", None), cursor,
            getattr(result, "graphemes", "") or text, seg_len))
        chunks.append(audio)
        cursor += seg_len
    if not chunks:
        return None, [], SAMPLE_RATE
    return np.concatenate(chunks), words, SAMPLE_RATE


def _synth_pocket(text: str, voice: str, lead_in: float) -> tuple:
    """(audio, words, sample_rate) from Pocket TTS, or (None, [], rate).

    No `speed` and no `lang_code` in the signature, because the engine has
    neither — see supports_speed(). The voice is a name from POCKET_VOICES, but
    get_state_for_audio_prompt also accepts a path or an hf:// URL, so a name it
    does not know fails there rather than here and the row renders silent with
    the reason logged.

    The lead-in is prepended as silence exactly as Kokoro's is, and the word
    timings are shifted by it in the same pass — the wav starts at t=0 either
    way, which is what lets the FFmpeg audio graph stay free of delay filters."""
    import numpy as np
    model, timed = _pocket_model()
    rate = int(getattr(model, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE)
    state = model.get_state_for_audio_prompt(voice)
    if timed:
        result = model.generate_audio_with_timestamps(state, text)
        audio = _to_float32(getattr(result, "audio", None))
        raw_words = getattr(result, "words", ())
    else:
        # Upstream pocket_tts: audio only. The words are then spread evenly by
        # _words_from_timestamps' own fallback, which marks every one of them
        # `estimated` — so this path needs no flag of its own, and neither does
        # the case where the FORK is installed but its model config has no
        # timestamp heads and returns nothing to align.
        audio = _to_float32(model.generate_audio(state, text))
        raw_words = ()
    if audio is None or not len(audio):
        return None, [], rate
    seg_len = len(audio) / float(rate)
    words = _words_from_timestamps(raw_words, text, seg_len)
    lead = float(max(0.0, lead_in))
    if lead > 0:
        audio = np.concatenate(
            [np.zeros(int(lead * rate), dtype="float32"), audio])
        for word in words:
            word["start"] += lead
            word["end"] += lead
    return audio, words, rate


def synthesize(text: str, voice: str, speed: float, cache_dir: Path,
               lang_code: str = "a", lead_in: float = 0.0,
               engine: Optional[str] = None) -> Optional[dict]:
    """Synthesize `text`, returning {"wav", "duration", "words"} or None.

    None means "render this row silent", never an exception — see the package
    docstring. `lead_in` is baked in as leading silence rather than applied
    later with adelay, which keeps the voice track starting at t=0 and is why
    the FFmpeg audio graph needs no delay filter anywhere.

    `speed` and `lang_code` are passed for every engine and used by the ones
    that have them; see supports_speed(). They still reach cache_key, which
    folds out the ones this engine ignores."""
    text = normalize(text)
    if not text:
        return None
    engine = normalize_engine(engine)
    ok, reason = available(engine)
    if not ok:
        logger.warning("Voiceover skipped: %s", reason)
        return None

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(text, voice, speed, lang_code, engine)
    wav_path, meta_path = cache_dir / (key + ".wav"), cache_dir / (key + ".json")
    cached = load_cached(text, voice, speed, cache_dir, lang_code, engine)
    if cached:
        return cached

    try:
        if engine == POCKET:
            audio, words, rate = _synth_pocket(text, voice, lead_in)
        else:
            audio, words, rate = _synth_kokoro(text, voice, speed, lang_code,
                                               lead_in)
        if audio is None:
            return None
        duration = _write_wav(wav_path, audio, rate)
    except Exception as exc:                            # noqa: BLE001
        logger.error("Voiceover synthesis failed for %r (%s): %s",
                     text[:60], engine, exc)
        wav_path.unlink(missing_ok=True)
        return None

    meta_path.write_text(json.dumps({"duration": duration, "words": words}),
                         encoding="utf-8")
    return {"wav": wav_path, "duration": duration, "words": words}
