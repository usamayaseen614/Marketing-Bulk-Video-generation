"""Caption beat grouping — the rules that decide where one on-screen caption
ends and the next begins.

No model, no FFmpeg, no I/O: speech/beats.py is pure, which is the whole reason
the splitting rules live there instead of inside the synthesis call."""
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from collections import Counter

import pandas as pd

from speech.beats import group_text, group_words
from speech.pool import apply_to_frame, parse_scripts


def timed(sentence, per=0.4, start=0.0):
    """Turn a sentence into evenly-timed words, `per` seconds each."""
    out, t = [], start
    for word in sentence.split():
        out.append({"text": word, "start": t, "end": t + per})
        t += per
    return out


# ---------- bounds are real bounds, not one-word overshoots ----------
words = timed("one two three four five six seven eight nine ten eleven twelve")
beats = group_words(words, max_words=4, max_chars=100, min_duration=0.0)
assert all(len(b[2].split()) <= 4 for b in beats), beats
assert len(beats) == 3, beats
print(f"word cap: 12 words -> {len(beats)} beats of <=4")

beats = group_words(timed("alpha bravo charlie delta echo foxtrot"),
                    max_words=99, max_chars=14, min_duration=0.0)
assert all(len(b[2]) <= 14 for b in beats), beats
print(f"char cap: {[b[2] for b in beats]}")

# ---------- punctuation ----------
beats = group_words(timed("Stop. Go now please"), max_words=4, max_chars=100,
                    min_duration=0.0)
assert beats[0][2] == "Stop.", beats
print(f"hard break after '.': {[b[2] for b in beats]}")

# A comma only breaks a beat that is already nearly full, so a short clause is
# not flashed on its own.
beats = group_words(timed("Hi, there"), max_words=4, max_chars=100, min_duration=0.0)
assert len(beats) == 1, beats
print(f"soft break held: {[b[2] for b in beats]}")

# ---------- nothing strobes ----------
words = [{"text": "long", "start": 0.0, "end": 2.0},
         {"text": "x", "start": 2.0, "end": 2.1}]
beats = group_words(words, max_words=1, max_chars=100, min_duration=0.45)
assert len(beats) == 1 and beats[0] == (0.0, 2.1, "long x"), beats
print(f"short beat merged: {beats}")

# ---------- the beat cap ----------
beats = group_words(timed(" ".join(str(i) for i in range(60))),
                    max_words=1, max_chars=100, min_duration=0.0, max_beats=10)
assert len(beats) == 10, len(beats)
assert beats[0][0] == 0.0 and abs(beats[-1][1] - 24.0) < 1e-6, beats[-1]
print(f"cap: 60 words -> {len(beats)} beats, span intact")

# ---------- timeline sanity, which the concat list depends on ----------
for start, end, text in beats:
    assert end > start, (start, end, text)
for a, b in zip(beats, beats[1:]):
    assert b[0] >= a[1] - 1e-6, (a, b)
print("monotonic, no zero-length beats")

# ---------- untimed text spread over a duration ----------
beats = group_text("Compare forty providers in seconds and switch today",
                   total_duration=8.0, max_words=3, max_chars=100,
                   min_duration=0.0)
assert beats, beats
assert abs(beats[-1][1] - 8.0) < 0.2, beats[-1]
assert beats[0][0] == 0.0, beats[0]
print(f"untimed spread over 8s -> {len(beats)} beats ending {beats[-1][1]:.2f}s")

# ---------- degenerate input never raises ----------
assert group_words([]) == []
assert group_words([{"text": "  ", "start": 0, "end": 1}]) == []
assert group_text("", 5.0) == []
assert group_text("hello", 0.0) == []
print("empty / whitespace / zero-duration input handled")

# ---------- the script + voice pool ----------
#
# Voice rotation has to stay even in BOTH directions, and the two failure modes
# pull opposite ways: keying on the row index alone correlates with the script
# deal whenever the two pool sizes share a factor, and keying on the per-script
# count alone puts an all-distinct sheet in a single voice. A four-row sheet
# narrated entirely by af_heart is how the second one was actually found.
frame, info = apply_to_frame(
    pd.DataFrame([{"Voiceover": f"line {i}"} for i in range(8)]), [], ["a", "b"])
assert Counter(frame["Voiceover_Voice"]) == {"a": 4, "b": 4}, frame["Voiceover_Voice"]
print("distinct scripts spread evenly across voices")

frame, info = apply_to_frame(pd.DataFrame([{} for _ in range(180)]),
                             [f"script {i}" for i in range(60)], ["a", "b", "c"])
assert Counter(frame["Voiceover_Voice"]) == {"a": 60, "b": 60, "c": 60}
assert set(frame[frame["Voiceover"] == "script 0"]["Voiceover_Voice"]) == {"a", "b", "c"}
assert info["applied"] == 180 and info["scripts"] == 60
print("60 scripts x 3 voices over 180 rows: even, and every script sees every voice")

# A cell the user filled in always wins.
frame, _ = apply_to_frame(
    pd.DataFrame([{"Voiceover": "mine", "Voiceover_Voice": "pinned"}, {}]),
    ["pooled"], ["a", "b"])
assert frame.at[0, "Voiceover"] == "mine" and frame.at[0, "Voiceover_Voice"] == "pinned"
assert frame.at[1, "Voiceover"] == "pooled"
print("sheet values are never overwritten by the pool")

# No pool is not an error — those rows just stay silent.
frame, info = apply_to_frame(pd.DataFrame([{}, {}]), [], [])
assert info["applied"] == 0 and "silent" in info["reason"]
assert all(str(v).strip() == "" for v in frame["Voiceover"])
print("an empty pool leaves rows silent rather than failing")

# ---------- reading an uploaded pool ----------
assert parse_scripts(b"one two\n\nthree four\n", "p.txt") == ["one two", "three four"]
assert parse_scripts(b"a\nb\nc\n", "p.txt") == ["a", "b", "c"]
print("script files parse by paragraph, then by line")

print("\nOK - speech/beats.py + speech/pool.py")
