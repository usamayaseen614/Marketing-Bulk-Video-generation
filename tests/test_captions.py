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
