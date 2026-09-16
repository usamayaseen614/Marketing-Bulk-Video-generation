"""
speech/beats.py — turning word timings into on-screen caption beats.

This module is the whole answer to "how does the code decide where one caption
ends and the next begins", and it deliberately contains no I/O and no model: it
takes the word timings Kokoro already hands back from synthesis and groups them.
That is the same thing CapCut does after its speech-recognition pass, minus the
recognition — we generated the audio, so the timings are exact rather than
inferred, and nobody has to mark up a cell.

Pure functions, so the splitting rules are testable without a TTS model.
"""

from __future__ import annotations

# A beat is (start_seconds, end_seconds, text).
Beat = tuple[float, float, str]

# Sentence enders always break; clause enders only break a beat that is already
# nearly full, so "Hello, world" does not become two one-word flashes.
_HARD_BREAK = ".!?"
_SOFT_BREAK = ",;:"


def group_words(words, *, max_words: int = 4, max_chars: int = 28,
                min_duration: float = 0.45, max_beats: int = 120) -> list[Beat]:
    """Group timed words into TikTok-style caption beats.

    `words` is a sequence of {"text", "start", "end"}. Beats are capped by word
    count and character count, broken at punctuation where that lands nearby,
    and then post-processed so nothing strobes: a beat shorter than
    `min_duration` is merged into a neighbour, and the count is folded down to
    `max_beats` (a 300-beat script would otherwise cost real Pillow time inside
    the 16-wide render pool, one PNG per beat)."""
    groups: list[list[dict]] = []
    current: list[dict] = []

    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        # Would adding this word overflow the beat? Break BEFORE appending, so
        # the limits are true bounds rather than one-word overshoots.
        width = len(" ".join([w["text"] for w in current] + [text]))
        if current and (len(current) >= max_words or width > max_chars):
            groups.append(current)
            current = []
        current.append({"text": text, "start": start, "end": end})
        last = text[-1]
        if last in _HARD_BREAK:
            groups.append(current)
            current = []
        elif last in _SOFT_BREAK and len(current) >= max(2, max_words - 1):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    beats = [(g[0]["start"], g[-1]["end"], " ".join(w["text"] for w in g))
             for g in groups if g]
    beats = _merge_short(beats, min_duration, max_words, max_chars)
    return _cap(beats, max_beats)


def group_text(text: str, total_duration: float, *, max_words: int = 4,
               max_chars: int = 28, min_duration: float = 0.45,
               max_beats: int = 120) -> list[Beat]:
    """Group UNtimed text and spread it across `total_duration`.

    Two callers: a Screen_Text that differs from the Voiceover (nothing to align
    against, so the beats are apportioned by character count), and a Screen_Text
    with no Voiceover at all (silent captions at a fixed reading pace). Both are
    approximations by nature — the caller warns the row."""
    words = [w for w in str(text or "").split() if w]
    if not words or total_duration <= 0:
        return []
    # Fake uniform-per-character timings, then reuse the real grouper so both
    # paths obey exactly the same splitting rules.
    per_char = total_duration / max(1, sum(len(w) for w in words) + len(words) - 1)
    timed, cursor = [], 0.0
    for i, word in enumerate(words):
        span = len(word) * per_char
        timed.append({"text": word, "start": cursor, "end": cursor + span})
        cursor += span + (per_char if i < len(words) - 1 else 0.0)
    return group_words(timed, max_words=max_words, max_chars=max_chars,
                       min_duration=min_duration, max_beats=max_beats)


def _merge_short(beats: list[Beat], min_duration: float, max_words: int,
                 max_chars: int) -> list[Beat]:
    """Fold a beat too brief to read into the one before it — only while the
    result still fits `max_words` and `max_chars`.

    A lone word flashed up for a third of a second reads as a glitch, and
    punctuation is the usual cause: a hard break after "?" leaves the next word
    stranded on its own. But this never outranks the caps. Ordinary speech runs
    about 0.3s a word, so at one word per caption nearly EVERY beat is "too
    short" — merging regardless of the cap chained them together and folded a
    whole sentence into a single caption, which is exactly what the "Words per
    caption" setting exists to prevent."""
    if min_duration <= 0:
        return beats
    out: list[Beat] = []
    for start, end, text in beats:
        if out and end - start < min_duration:
            p_start, _, p_text = out[-1]
            merged = f"{p_text} {text}"
            if len(merged.split()) <= max_words and len(merged) <= max_chars:
                out[-1] = (p_start, end, merged)
                continue
        # Long enough, at the head with nothing before it, or too big to fold.
        out.append((start, end, text))
    return out


def _cap(beats: list[Beat], max_beats: int) -> list[Beat]:
    """Fold the shortest adjacent pair until the count fits.

    ponytail: O(n^2) on beat count, which is bounded by max_beats iterations
    over a list that is tens of entries long. Switch to a heap if scripts ever
    run to thousands of words."""
    if max_beats <= 0:
        return beats
    while len(beats) > max_beats:
        pick = min(range(len(beats) - 1),
                   key=lambda i: (beats[i][1] - beats[i][0])
                   + (beats[i + 1][1] - beats[i + 1][0]))
        a, b = beats[pick], beats[pick + 1]
        beats[pick:pick + 2] = [(a[0], b[1], f"{a[2]} {b[2]}")]
    return beats
