"""The 100-character rule is strict, so it gets tested adversarially."""
import os, sys
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from captions import naming

CAP = naming.MAX_SHORT

# ---------- the headline case ----------
short, long = naming.build_names(
    "Your skin will thank you for this one",
    "#skincare #asmr #fyp #glowup #selfcare")
print("short:", short)
print("long :", long)
assert short == "Your skin will thank you for this one #skincare.mp4", short
assert long == ("Your skin will thank you for this one "
                "#skincare #asmr #fyp #glowup #selfcare.mp4"), long
assert len(short) <= CAP

# ---------- the 100-char rule, hammered ----------
print("\n--- 100-char cap (including .mp4) ---")
cases = [
    ("short caption", "#one"),
    ("A" * 300, "#tag"),
    ("word " * 80, "#verylonghashtagname_that_goes_on"),
    ("x" * 95, "#a"),
    ("Supercalifragilisticexpialidocious" * 5, "#b"),   # no spaces to break on
    ("", "#only"),
    ("caption with no tags at all", ""),
    ("", ""),
    ("Ünïcödé çãptïön with áccents everywhere in it", "#ünïcödé"),
    ("emoji 🔥🎉 caption", "#fire"),
    ("a", "#" + "z" * 120),                              # hashtag alone > cap
]
for caption, tags in cases:
    s, l = naming.build_names(caption, tags)
    assert len(s) <= CAP, f"SHORT {len(s)} > {CAP}: {s!r}"
    assert len(l) <= naming.MAX_LONG, f"LONG {len(l)} > {naming.MAX_LONG}"
    assert s.endswith(".mp4") and l.endswith(".mp4"), (s, l)
    stem = s[:-4]
    assert stem == stem.strip(), f"leading/trailing space survived: {s!r}"
    assert not stem.endswith("."), f"trailing dot: {s!r}"
    assert not set(s) & set('<>:"/\\|?*'), f"illegal char: {s!r}"
    assert s.count("#") <= 1, f"short name has >1 hashtag: {s!r}"
    print(f"  len={len(s):3d}  {s}")

# exactly-at-the-boundary caption
s, _ = naming.build_names("y" * 200, "#tag")
assert len(s) == CAP or len(s) < CAP
print(f"\n  worst case length: {len(s)} (cap {CAP})")

# ---------- short keeps its hashtag; long keeps them all ----------
s, l = naming.build_names("word " * 60, "#alpha #beta #gamma")
assert s.count("#") == 1 and s.endswith("#alpha.mp4"), s
assert l.count("#") == 3, l
print("\nshort keeps exactly 1 hashtag, long keeps all 3")
print("  short:", s)
print("  long :", l)

# ---------- word-boundary truncation, not mid-word ----------
s, _ = naming.build_names("The quick brown fox jumps over the lazy dog and keeps running forever onwards", "#tag")
assert not s[:-4].rstrip("#tag").rstrip().endswith(("-", "_")), s
print("\nword-boundary truncation:", s)

# a single unbroken word must still be cut (no space to fall back to)
s, _ = naming.build_names("Z" * 200, "#tag")
assert len(s) <= CAP and "Z" in s
print("unbroken word still truncated:", len(s), "chars")

# ---------- paste-readiness: spaces and # survive ----------
s, _ = naming.build_names("Stop scrolling this actually works", "#fyp")
assert " " in s and "#" in s and "_" not in s
print("\npaste-ready (spaces + # kept, no underscores):", s)

# ---------- illegal characters ----------
s, _ = naming.build_names('bad/name\\with:illegal*chars?"<>|', "#ok")
assert not set(s) & set('<>:"/\\|?*'), s
print("illegal chars stripped:", s)

# ---------- windows reserved names ----------
s, _ = naming.build_names("CON", "")
assert s.upper() != "CON.MP4", s
print("reserved name guarded:", s)

# ---------- a caption containing its own hashtags ----------
s, l = naming.build_names("Buy now #sale today #urgent", "#skincare #asmr")
assert s.count("#") == 1, s
assert l.count("#") == 2, l
assert "sale" not in s and "urgent" not in s, s
print("\ncaption's own hashtags stripped so the 1-# rule holds:")
print("  short:", s)
print("  long :", l)

# a bare '#' in the caption must not count either
s, _ = naming.build_names("grade # 1 product", "#tag")
assert s.count("#") == 1, s
print("  bare '#' handled:", s)

# ---------- hashtag parsing ----------
assert naming.parse_hashtags("#a #b #c") == ["#a", "#b", "#c"]
assert naming.parse_hashtags("a, b,c") == ["#a", "#b", "#c"]
assert naming.parse_hashtags("#a #A #a") == ["#a"], "case-insensitive dedupe"
assert naming.parse_hashtags("") == []
assert naming.parse_hashtags(None) == []
print("\nhashtag parsing ok")

# ---------- emoji are stripped from captions and filenames ----------
print("\n--- emoji stripping (default) ---")
for raw, why in [
    ("fire sale 🔥 today", "mid-caption"),
    ("🔥 fire sale", "leading"),
    ("fire sale 🔥", "trailing"),
    ("a 👨‍👩‍👧‍👦 family", "compound (ZWJ) emoji"),
    ("flags 🇬🇧🇺🇸 here", "regional indicators"),
    ("check ✅ done ❌", "dingbats"),
    ("arrows ➡️ here", "arrow + variation selector"),
]:
    s_, l_ = naming.build_names(raw, "#tag")
    stem = s_[:-4]
    leftover = [c for c in stem if ord(c) > 0x2500 and c not in "—–…‘’“”"]
    assert not leftover, f"{why}: emoji survived {leftover} in {s_!r}"
    assert "  " not in s_, f"{why} left a double space: {s_!r}"
    assert "🔥" not in l_ and "✅" not in l_, l_
    print(f"  {why:26s} -> {s_}")

# typographic punctuation must survive the emoji strip
for keep in ("—", "–", "…", "’"):
    s_, _ = naming.build_names(f"stop{keep}now 🔥", "#x")
    assert keep in s_, (keep, s_)
print("  typographic punctuation (em dash, en dash, ellipsis, curly quote) kept")

# opting back in still works
s_, _ = naming.build_names("fire 🔥", "#x", keep_emoji=True)
assert "🔥" in s_, s_
print("  keep_emoji=True still keeps them:", s_)

# ---------- collisions ----------
print("\n--- collisions ---")
dupes = ["same name.mp4"] * 4 + ["other.mp4"]
out = naming.dedupe(dupes)
assert len(set(n.lower() for n in out)) == len(out), out
assert all(len(n) <= CAP for n in out)
print(" ", out)

# a colliding name already AT the cap must stay at or under it after suffixing
at_cap = naming.build_names("q" * 200, "#tag")[0]
assert len(at_cap) <= CAP
out = naming.dedupe([at_cap, at_cap, at_cap], cap=CAP)
assert len(set(o.lower() for o in out)) == 3, out
for o in out:
    assert len(o) <= CAP, f"suffix pushed past the cap: {len(o)} {o!r}"
print(f"  at-cap collisions stay <= {CAP}: {[len(o) for o in out]}")

# ---------- whole-batch naming ----------
rows = [
    ("Your skin will thank you", "#skincare #asmr"),
    ("Your skin will thank you", "#glowup #fyp"),      # same caption, diff tags
    ("Your skin will thank you", "#skincare #asmr"),   # exact duplicate
]
pairs = naming.names_for_rows(rows)
shorts = [p[0] for p in pairs]
longs = [p[1] for p in pairs]
assert len(set(shorts)) == 3, shorts
assert len(set(longs)) == 3, longs
assert all(len(s) <= CAP for s in shorts)
print("\nbatch naming de-duplicates both lists independently:")
for s, l in pairs:
    print(f"  {s}\n    {l}")

# ---------- fuzz: the cap must NEVER break ----------
import random
rng = random.Random(1234)
alphabet = "abcdefghijklmnopqrstuvwxyz ÜÑ🔥.,!?-_#/\\:*"
worst = 0
for _ in range(4000):
    cap_txt = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 250)))
    tags = " ".join(f"#{''.join(rng.choice('abcdefgh') for _ in range(rng.randint(1, 25)))}"
                    for _ in range(rng.randint(0, 12)))
    s, l = naming.build_names(cap_txt, tags)
    assert len(s) <= CAP, f"CAP BROKEN len={len(s)} caption={cap_txt!r} tags={tags!r}"
    assert len(l) <= naming.MAX_LONG, f"LONG CAP BROKEN len={len(l)}"
    assert s.endswith(".mp4") and l.endswith(".mp4")
    assert not set(s) & set('<>:"/\\|?*')
    # The strict rule: exactly one '#', even when the CAPTION contained some.
    assert s.count("#") <= 1, f"short has {s.count('#')} hashes: {s!r}"
    worst = max(worst, len(s))
print(f"\nfuzzed 4000 random caption/hashtag pairs — cap never broken "
      f"(longest short name seen: {worst})")

print("\nALL NAMING TESTS PASSED")
