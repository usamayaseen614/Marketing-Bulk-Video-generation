"""
speech/synth.py — Kokoro text-to-speech, with the word timings that make the
timed caption layer possible.

The only module in the repo that imports `kokoro`, and it imports it lazily, for
two reasons: the package pulls PyTorch (so nothing else should pay for it at
import time), and it requires Python 3.10-3.12 while the dev venv here is 3.14.
A missing or broken install must leave `app.py` starting normally with the
feature switched off, not traceback on the import line.

WHY THERE IS NO SPEECH-RECOGNITION STEP: tools like CapCut transcribe finished
audio to recover word timings. We generate the audio, and Kokoro's KPipeline
returns `result.tokens` carrying start_ts/end_ts as a normal part of synthesis.
So the timings are exact rather than inferred, there is no aligner to install,
and nobody has to mark beat boundaries up by hand.

Output is cached by content — sha1(text|voice|speed|lang) — because a 600-video
folder dealt from a 60-script pool needs 60 syntheses, not 600.
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

SAMPLE_RATE = 24000          # Kokoro's native output rate
_CACHE_VERSION = "k1"        # bump to invalidate every cached wav

# The English voices shipped with Kokoro v1.0. a*/b* are American/British,
# *f_/*m_ female/male. Other languages exist but need a different lang_code and
# the matching misaki extra, so they are deliberately not offered here.
VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
DEFAULT_VOICE = "af_heart"

_pipelines: dict[str, object] = {}
_unavailable: Optional[str] = None


def configure_threads(n: int = 1) -> None:
    """Pin torch to `n` threads. The voice stage fans out over unique scripts
    with its own pool; letting each worker also spin a torch thread pool turns
    a 112-core box into 112 processes each asking for 112 threads."""
    try:
        import torch
        torch.set_num_threads(max(1, int(n)))
    except Exception:                                   # noqa: BLE001
        pass


def available() -> tuple[bool, str]:
    """(usable, reason). Never raises, so a UI can gate on it."""
    global _unavailable
    if _unavailable is not None:
        return (False, _unavailable) if _unavailable else (True, "")
    try:
        import kokoro  # noqa: F401
        _unavailable = ""
        return True, ""
    except Exception as exc:                            # noqa: BLE001
        _unavailable = (f"Kokoro is not installed in this environment ({exc}). "
                        "Voiceover is disabled; everything else renders as "
                        "normal.")
        return False, _unavailable


def _pipeline(lang_code: str):
    if lang_code not in _pipelines:
        from kokoro import KPipeline
        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _pipelines[lang_code]


def normalize(text: str) -> str:
    """The exact text the cache key is computed over. Shared so the renderer's
    lookup and this module's write can never disagree about whitespace."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def cache_key(text: str, voice: str, speed: float, lang_code: str) -> str:
    raw = _CACHE_VERSION + "|" + lang_code + "|" + voice
    raw += "|" + format(float(speed), ".3f") + "|" + normalize(text)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_cached(text: str, voice: str, speed: float, cache_dir: Path,
                lang_code: str = "a") -> Optional[dict]:
    """Read a previously synthesized narration, or None.

    This is the renderer's whole interface to speech: the pre-render stage
    fills the cache, and render_row only ever LOOKS UP. Nothing here imports
    kokoro or touches a model, which is the point — rows render 16-wide on a
    box that has already OOM-killed sixteen parallel FFmpegs."""
    if not text or not cache_dir:
        return None
    key = cache_key(text, voice, speed, lang_code)
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


def _write_wav(path: Path, samples) -> float:
    """16-bit PCM via the stdlib. No soundfile, no pydub — one more dependency
    for something `wave` already does."""
    import numpy as np
    audio = np.asarray(samples, dtype="float32").reshape(-1)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return len(audio) / float(SAMPLE_RATE)


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
    if not words:
        return []

    # Fill gaps by interpolating across the segment, in one pass, so a
    # partially-timestamped segment keeps the timings it did get.
    per = seg_len / len(words)
    for i, word in enumerate(words):
        if word["start"] is None:
            word["start"] = i * per
        if word["end"] is None:
            word["end"] = min(seg_len, float(word["start"]) + per)
        word["start"] = float(word["start"]) + offset
        word["end"] = float(word["end"]) + offset
    # Monotonic and non-zero-length: a beat with end <= start would show for no
    # frames at all, and the concat list would carry a 0.000 duration entry.
    for i, word in enumerate(words):
        if i and word["start"] < words[i - 1]["end"]:
            word["start"] = words[i - 1]["end"]
        if word["end"] <= word["start"]:
            word["end"] = word["start"] + 0.04
    return words


def synthesize(text: str, voice: str, speed: float, cache_dir: Path,
               lang_code: str = "a", lead_in: float = 0.0) -> Optional[dict]:
    """Synthesize `text`, returning {"wav", "duration", "words"} or None.

    None means "render this row silent", never an exception — see the package
    docstring. `lead_in` is baked in as leading silence rather than applied
    later with adelay, which keeps the voice track starting at t=0 and is why
    the FFmpeg audio graph needs no delay filter anywhere."""
    text = normalize(text)
    if not text:
        return None
    ok, reason = available()
    if not ok:
        logger.warning("Voiceover skipped: %s", reason)
        return None

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(text, voice, speed, lang_code)
    wav_path, meta_path = cache_dir / (key + ".wav"), cache_dir / (key + ".json")
    cached = load_cached(text, voice, speed, cache_dir, lang_code)
    if cached:
        return cached

    try:
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
            raw = audio.detach().cpu().numpy() if hasattr(audio, "detach") else audio
            audio = np.asarray(raw, dtype="float32").reshape(-1)
            seg_len = len(audio) / float(SAMPLE_RATE)
            words.extend(_words_from_tokens(
                getattr(result, "tokens", None), cursor,
                getattr(result, "graphemes", "") or text, seg_len))
            chunks.append(audio)
            cursor += seg_len
        if not chunks:
            return None
        duration = _write_wav(wav_path, np.concatenate(chunks))
    except Exception as exc:                            # noqa: BLE001
        logger.error("Voiceover synthesis failed for %r: %s", text[:60], exc)
        wav_path.unlink(missing_ok=True)
        return None

    meta_path.write_text(json.dumps({"duration": duration, "words": words}),
                         encoding="utf-8")
    return {"wav": wav_path, "duration": duration, "words": words}
