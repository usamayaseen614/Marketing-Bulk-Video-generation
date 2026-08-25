"""The 90-character name rule is strict, so it gets tested adversarially."""
import os, sys
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from captions import naming

CAP = naming.MAX_SHORT          # full filename
STEM = naming.MAX_STEM          # the rule that actually matters: 90, no .mp4

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

# ---------- the 90-char name rule, hammered ----------
print(f"\n--- {STEM}-char cap on the NAME (.mp4 not counted) ---")
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
    assert len(s[:-4]) <= STEM, f"SHORT stem {len(s[:-4])} > {STEM}: {s!r}"
    assert len(l[:-4]) <= STEM, f"LONG stem {len(l[:-4])} > {STEM}: {l!r}"
    assert s.endswith(".mp4") and l.endswith(".mp4"), (s, l)
    stem = s[:-4]
    assert stem == stem.strip(), f"leading/trailing space survived: {s!r}"
    assert not stem.endswith("."), f"trailing dot: {s!r}"
    assert not set(s) & set('<>:"/\\|?*'), f"illegal char: {s!r}"
    assert s.count("#") <= 1, f"short name has >1 hashtag: {s!r}"
    print(f"  stem={len(s[:-4]):3d} file={len(s):3d}  {s}")

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

# ---------- the long form is capped at five hashtags ----------
many = " ".join(f"#t{i}" for i in range(10))
s5, l5 = naming.build_names("a readable caption", many)
assert naming.MAX_LONG_HASHTAGS == 5, naming.MAX_LONG_HASHTAGS
assert s5.count("#") == 1, s5
assert l5.count("#") == 5, l5
# the FIRST five, in the order supplied — not an arbitrary subset
assert "#t0" in l5 and "#t4" in l5 and "#t5" not in l5, l5
print(f"\n10 hashtags supplied -> short {s5.count('#')}, long {l5.count('#')}")
print("  long:", l5)

for n in (0, 1, 3, 5, 12, 30):
    a, b = naming.build_names("caption text", " ".join(f"#x{i}" for i in range(n)))
    assert a.count("#") <= 1, (n, a)
    assert b.count("#") <= 5, (n, b)
    assert n == 0 or b.count("#") >= 1, (n, b)
    assert len(a[:-4]) <= STEM and len(b[:-4]) <= STEM
print("across 0/1/3/5/12/30 supplied: short <=1, long 1..5, both inside their caps")

# ---------- the long form drops hashtags to keep the caption whole ----------
# A 50-char caption beside five long hashtags does not fit in 90. Cutting the
# caption is what used to happen, and it is what makes two different captions
# collide on one name. The hashtags give way instead.
long_tags = "#skincareroutine #asmrsounds #foryoupage #glowuptips #selfcaresunday"
caption50 = "Your evening routine deserves better than this"
_s, l_fit = naming.build_names(caption50, long_tags)
assert caption50 in l_fit, f"caption was truncated: {l_fit}"
assert 1 <= l_fit.count("#") < 5, l_fit
assert len(l_fit[:-4]) <= STEM, len(l_fit)
print(f"\ncaption kept whole, {l_fit.count('#')} of 5 hashtags carried:")
print("  ", l_fit)

# Two captions sharing a long prefix must NOT collide once the tags give way.
a1, l1 = naming.build_names("Your evening routine deserves better than A", long_tags)
a2, l2 = naming.build_names("Your evening routine deserves better than B", long_tags)
assert l1 != l2, (l1, l2)
print("  captions sharing a 40-char prefix stay distinct:", l1 != l2)

# But when the caption cannot fit whole even beside ONE hashtag, dropping tags
# buys nothing — it is truncated either way, so the full set is carried.
_s, l_big = naming.build_names("word " * 60, "#alpha #beta #gamma")
assert l_big.count("#") == 3, l_big
print("  un-fittable caption keeps all 3 hashtags rather than losing them")

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
assert all(len(n[:-4]) <= STEM for n in out)
print(" ", out)

# a colliding name already AT the cap must stay at or under it after suffixing
at_cap = naming.build_names("q" * 200, "#tag")[0]
assert len(at_cap[:-4]) == STEM
out = naming.dedupe([at_cap, at_cap, at_cap], cap=CAP)
assert len(set(o.lower() for o in out)) == 3, out
for o in out:
    assert len(o[:-4]) <= STEM, f"suffix pushed past the cap: {len(o[:-4])} {o!r}"
print(f"  at-cap collisions stay <= {STEM}: {[len(o[:-4]) for o in out]}")

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
assert all(len(s[:-4]) <= STEM for s in shorts)
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
    assert len(s[:-4]) <= STEM, f"CAP BROKEN stem={len(s[:-4])} caption={cap_txt!r}"
    assert len(l[:-4]) <= STEM, f"LONG CAP BROKEN stem={len(l[:-4])}"
    assert s.endswith(".mp4") and l.endswith(".mp4")
    assert not set(s) & set('<>:"/\\|?*')
    # The strict rule: exactly one '#', even when the CAPTION contained some.
    assert s.count("#") <= 1, f"short has {s.count('#')} hashes: {s!r}"
    worst = max(worst, len(s[:-4]))
print(f"\nfuzzed 4000 random caption/hashtag pairs — cap never broken "
      f"(longest short name seen: {worst})")

# ---------- fixed CTA tails: they replace hashtags and are never cut ----------
print("\n--- fixed call-to-action tails ---")
TAIL = naming.FIXED_TAILS[2]          # the longest of the three (47 chars)
assert len(TAIL) == max(len(t) for t in naming.FIXED_TAILS)
s_t, l_t = naming.build_names("Your skin will thank you for this",
                              "#skincare #asmr #fyp", tail=TAIL)
print("short:", s_t)
assert s_t == l_t, "with a tail both names are the same by design"
assert s_t.endswith(f"{TAIL}.mp4"), s_t
assert "#" not in s_t, "a fixed tail replaces hashtags entirely"
assert len(s_t[:-4]) <= STEM

# A caption at the generation cap plus the longest tail still fits whole.
import config as settings
longest_caption = "x" * settings.CAPTION_MAX_CHARS
s_f, _ = naming.build_names(longest_caption, "", tail=TAIL)
assert s_f == f"{longest_caption} {TAIL}.mp4", s_f
assert len(s_f[:-4]) <= STEM, len(s_f[:-4])
print(f"caption at the {settings.CAPTION_MAX_CHARS}-char generation cap + the "
      f"longest tail = {len(s_f[:-4])} chars, inside {STEM}")

# An over-long caption gives way; the tail survives whole.
s_o, _ = naming.build_names("word " * 60, "", tail=TAIL)
assert s_o.endswith(f"{TAIL}.mp4"), s_o
assert len(s_o[:-4]) <= STEM

# Every tail, drawn per video, and all three actually get used across a batch.
rows_t = [(f"caption number {i}", "#a #b") for i in range(60)]
pairs_t = naming.names_for_rows(rows_t, tails=naming.FIXED_TAILS)
used = {t for t in naming.FIXED_TAILS
        if any(sh[:-4].endswith(t) for sh, _ in pairs_t)}
assert used == set(naming.FIXED_TAILS), f"only {len(used)}/3 tails drawn"
assert all(len(sh[:-4]) <= STEM and "#" not in sh for sh, _ in pairs_t)
assert len({sh for sh, _ in pairs_t}) == len(pairs_t), "names must stay unique"
print(f"60 videos drew all 3 tails at random, every name unique and inside {STEM}")

# Fuzz the tail path too - the cap is the same hard rule.
for _ in range(2000):
    cap_txt = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 250)))
    t = rng.choice(naming.FIXED_TAILS)
    s_x, l_x = naming.build_names(cap_txt, "#a #b #c", tail=t)
    assert len(s_x[:-4]) <= STEM, f"TAIL CAP BROKEN {len(s_x[:-4])}"
    assert len(l_x[:-4]) <= STEM
    assert s_x[:-4].endswith(t), f"tail was cut: {s_x!r}"
    assert not set(s_x) & set('<>:"/\|?*')
print("fuzzed 2000 tailed names - cap never broken, tail never cut")

# Duplicate captions: the ' (2)' counter must come out of the CAPTION, never
# out of the call-to-action line. Cutting the end of a tailed name publishes a
# dead link (PlushieFriend.co), which is the whole payload of the feature.
dupe_rows = [("Your plushie remembers every single word you say", "")] * 12
dupe_pairs = naming.names_for_rows(dupe_rows, tails=naming.FIXED_TAILS)
for sh, lo in dupe_pairs:
    assert any(sh[:-4].endswith(t) for t in naming.FIXED_TAILS),         f"the counter ate the tail: {sh!r}"
    assert any(lo[:-4].endswith(t) for t in naming.FIXED_TAILS), lo
    assert len(sh[:-4]) <= STEM, len(sh[:-4])
assert len({sh for sh, _ in dupe_pairs}) == len(dupe_pairs), "not unique"
assert sum(" (" in sh for sh, _ in dupe_pairs) >= 8, "dedupe never fired"
print("12 identical captions deduped with every URL intact, e.g.")
print("  ", dupe_pairs[1][0])

# The same, at the exact generation cap, where the budget is tightest.
tight = [("A 40 character caption that fills it up!", "")] * 3
for sh, _ in naming.names_for_rows(tight, tails=naming.FIXED_TAILS):
    assert any(sh[:-4].endswith(t) for t in naming.FIXED_TAILS), sh
    assert len(sh[:-4]) <= STEM, len(sh[:-4])
print("40-char captions at the cap dedupe without touching the tail")

# An untailed name still trims from the end, as it always has.
plain = naming.dedupe(["same name.mp4"] * 3, cap=naming.MAX_SHORT)
assert plain[1].endswith("(2).mp4") and len(plain[1]) <= naming.MAX_SHORT
print("untailed dedupe unchanged:", plain[1])

print("\nALL NAMING TESTS PASSED")
