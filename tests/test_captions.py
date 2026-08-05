"""Caption pool combinatorics, sanitisation, and sheet wiring.

No Gemini calls — the pool is stubbed. What matters here is that no two videos
ever get the same caption+hashtag pair, and that captions survive becoming
filenames."""
import os, sys, tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="captest_")
sys.path.insert(0, str(PROJ))

import pandas as pd
from jobs import store
from captions import assign
from video_generator import safe_filename

store.init_db()

# ---------- the uniqueness guarantee ----------
caps = [f"caption {i}" for i in range(40)]
tags = [f"#a{i} #b{i}" for i in range(25)]      # 40 x 25 = 1000 pairs
pool_id = store.save_pool("test theme", caps, tags, model="stub")
pool = store.active_pool()
assert pool["id"] == pool_id and pool["combinations"] == 1000, pool["combinations"]
print(f"pool: {len(caps)} captions x {len(tags)} hashtag sets = {pool['combinations']} pairs")

# Draw EVERY combination and prove none repeats.
seen = set()
for chunk in range(10):
    for pair in store.take_combinations(pool_id, 100):
        assert pair not in seen, f"repeat after {len(seen)} draws: {pair}"
        seen.add(pair)
assert len(seen) == 1000, len(seen)
print(f"drew all {len(seen)} pairs across 10 batches — zero repeats")

# Both dimensions are exercised evenly, not caption-1-with-every-hashtag first.
from collections import Counter
first200 = list(store.take_combinations(store.save_pool("t2", caps, tags), 200))
cap_spread = len({c for c, _ in first200})
tag_spread = len({t for _, t in first200})
print(f"first 200 draws touch {cap_spread}/40 captions and {tag_spread}/25 hashtag sets")
assert cap_spread > 20 and tag_spread > 15, "walk is not spreading across both axes"

# Cursor persists across calls, so restarts don't hand out repeats.
p3 = store.save_pool("t3", caps, tags)
a = set(store.take_combinations(p3, 500))
b = set(store.take_combinations(p3, 500))
assert not (a & b), "cursor did not persist — overlap between draws"
print("cursor persists: two 500-draws from one pool never overlap")

# ---------- one caption per video, never repeated ----------
# take_combinations only promises distinct PAIRS: caption 7 comes back once per
# hashtag set. A short filename is caption + the first hashtag, so two videos
# handed the same caption collide on their name. take_captions is the draw the
# renderer uses, and it makes the caption itself the thing that cannot repeat.
p4 = store.save_pool("t4", caps, tags)
drawn = store.take_captions(p4, 40)
assert len(drawn) == 40, len(drawn)
assert len({c for c, _ in drawn}) == 40, "a caption was handed out twice"
assert len({t for _, t in drawn}) > 15, "hashtags stopped spreading"
print(f"take_captions(40): {len({c for c, _ in drawn})} distinct captions, "
      f"{len({t for _, t in drawn})} distinct hashtag sets")

# The exact case that used to produce ' (2)': same caption, different tags.
by_caption: dict[str, set] = {}
for caption, tag in drawn:
    by_caption.setdefault(caption, set()).add(tag)
assert all(len(v) == 1 for v in by_caption.values()), \
    "same caption reappeared with a different hashtag set"

# The cursor carries, so a second job never reuses the first job's captions.
p5 = store.save_pool("t5", caps, tags)
first = {c for c, _ in store.take_captions(p5, 20)}
second = {c for c, _ in store.take_captions(p5, 20)}
assert not (first & second), sorted(first & second)
print("two 20-caption draws from one pool share no caption")

def _cursor(pool_id: str) -> int:
    return next(p["cursor"] for p in store.list_pools(limit=50)
                if p["id"] == pool_id)


# p5 has now handed out all 40 of its captions, so it is SPENT. The pool never
# laps: starting the captions over would silently reuse them across jobs, which
# is the thing "one caption, one video" is supposed to rule out.
before = _cursor(p5)
assert before == 40, before
try:
    store.take_captions(p5, 1)
    raise AssertionError("a spent pool handed out a caption again")
except store.PoolTooSmall as exc:
    assert exc.available == 0 and exc.total == 40 and exc.needed == 1, exc
    print("spent pool ->", exc)

# ...and the rejected draw must not have consumed anything.
assert _cursor(p5) == before, (_cursor(p5), before)
print("a rejected draw leaves the cursor untouched at", before)

# The shortfall is measured in UNUSED captions, not pool size: a pool with 30
# left refuses a 31-video job even though it holds 40 in total.
p6 = store.save_pool("t6", caps, tags)
store.take_captions(p6, 10)
try:
    store.take_captions(p6, 31)
    raise AssertionError("PoolTooSmall was not raised")
except store.PoolTooSmall as exc:
    assert (exc.needed, exc.available, exc.total) == (31, 30, 40), exc
    assert "30" in str(exc) and "40" in str(exc) and "31" in str(exc), str(exc)
    print("partially used pool ->", exc)

# Every caption in a pool is handed out exactly once over its whole life,
# however many jobs it takes to get through it.
small = store.save_pool("t7", [f"c{i}" for i in range(12)], ["#h0", "#h1"])
spent: list[tuple[str, str]] = []
for _ in range(3):
    job_draw = store.take_captions(small, 4)
    assert len({c for c, _ in job_draw}) == 4, job_draw
    spent.extend(job_draw)
assert len({c for c, _ in spent}) == 12, sorted(c for c, _ in spent)
assert store.pool_remaining(small) == (0, 12), store.pool_remaining(small)
# Hashtags repeat freely — with the caption unique, they carry no naming duty.
assert len({t for _, t in spent}) == 2, "hashtags should be reused"
print(f"12 captions across 3 jobs of 4: each used exactly once, "
      f"hashtags reused ({len({t for _, t in spent})} sets for 12 videos)")

# Exactly enough is fine; a one-caption pool can still serve a one-video job —
# and is spent the moment it does.
tiny = store.save_pool("t8", ["only one"], ["#x"])
assert store.take_captions(tiny, 1) == [("only one", "#x")]
assert store.pool_remaining(tiny) == (0, 1), store.pool_remaining(tiny)
print("edge cases ok: exact fit spends the pool")

# ---------- realistic scale ----------
big_caps = [f"c{i}" for i in range(2000)]
big_tags = [f"t{i}" for i in range(500)]
big = store.save_pool("real", big_caps, big_tags)
assert store.active_pool()["combinations"] == 1_000_000
day = store.take_combinations(big, 2000)
assert len(set(day)) == 2000
print(f"2000-caption x 500-hashtag pool = 1,000,000 pairs "
      f"(~{1_000_000 // 2000} days at 2,000 videos/day before any repeat)")

# ---------- filename sanitisation ----------
cases = [
    ("Summer sale 🔥 50% off!!", "emoji + punctuation"),
    ('bad/name\\with:illegal*chars?', "illegal path chars"),
    ("x" * 200, "over-long"),
    ("   ", "whitespace only"),
    ("", "empty"),
    ("CON", "windows reserved name"),
    ("Ünïcödé wörks", "non-ascii letters"),
]
print("\nfilenames from captions:")
for text, why in cases:
    name = safe_filename(7, text)
    assert len(name) < 100, name
    assert not set(name) & set('<>:"/\\|?*'), f"illegal char survived: {name}"
    assert name.endswith(".mp4") and name.startswith("007_")
    print(f"  {why:24s} -> {name}")

assert safe_filename(7, "   ") == "007_row.mp4"
assert safe_filename(7, "") == "007_row.mp4"
assert "🔥" not in safe_filename(1, "fire 🔥")
print("emoji stripped, illegal chars removed, length capped, blanks fall back")

# sanitize_for_filename guards the Windows reserved names too
assert assign.sanitize_for_filename("CON") == "CON_"
assert assign.sanitize_for_filename("a/b\\c") == "abc"
print("reserved-name guard ok:", assign.sanitize_for_filename("CON"))

# ---------- applying to a sheet ----------
df = pd.DataFrame({"Headline": ["A", "B", "C", "D"]})
p = store.save_pool("sheet", caps, tags)
out, info = assign.apply_to_frame(df)
assert info["applied"] == 4, info
assert list(out.columns[-2:]) == ["Caption", "Hashtags"]
assert all(str(c).strip() for c in out["Caption"]), out["Caption"].tolist()
assert len(set(out["Caption"])) == 4, "captions repeated within one batch"
print(f"\nsheet: added Caption+Hashtags to {info['applied']} rows")
print(out[["Headline", "Caption", "Hashtags"]].to_string(index=False))

# A hand-written caption must win over the pool.
df2 = pd.DataFrame({"Headline": ["A", "B"], "Caption": ["MINE", ""]})
out2, info2 = assign.apply_to_frame(df2)
assert out2.at[0, "Caption"] == "MINE", out2.at[0, "Caption"]
assert out2.at[1, "Caption"] and out2.at[1, "Caption"] != "MINE"
assert info2["applied"] == 1
print("hand-written captions are preserved; only blanks are filled")

# ---------- no pool = no failure ----------
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="captest2_")
import importlib, config as cfg
importlib.reload(cfg)
importlib.reload(store)
store.init_db()
out3, info3 = assign.apply_to_frame(pd.DataFrame({"Headline": ["A"]}))
assert info3["applied"] == 0 and "pool" in info3["reason"].lower(), info3
print("no pool ->", info3["reason"], "(frame returned unchanged, no exception)")

print("\nALL CAPTION TESTS PASSED")
