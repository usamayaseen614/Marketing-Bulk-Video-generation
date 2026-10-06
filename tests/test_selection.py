"""Clip selection: which clips get used, and which slot each lands in."""
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="seltest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import batching

TMP = Path(tempfile.mkdtemp(prefix="clips_"))


def make_clips(n):
    """n clips, clip i having (n - i) views — so clip 0 is the strongest."""
    out = []
    for i in range(n):
        p = TMP / f"v{i:03d}.mp4"
        p.write_bytes(b"x" * (100 + i))
        out.append({"video_id": f"v{i:03d}", "path": str(p), "views": (n - i) * 1000})
    return out


clips = make_clips(80)

# ---------- top_views: curation as a rule ----------
sel = batching.select_clips(clips, slots=5, per_slot=10, strategy="top_views")
picked = [c["video_id"] for s in sel.values() for c in s]
assert len(picked) == 50, len(picked)
assert len(set(picked)) == 50, "a clip was used twice"
# it must have taken the BEST 50, not just any 50
assert set(picked) == {f"v{i:03d}" for i in range(50)}, sorted(picked)[:5]
print(f"top_views took the best {len(picked)} of {len(clips)} by view count, no repeats")

counts = Counter(s for s, v in sel.items() for _ in v)
assert all(c == 10 for c in counts.values()), counts
print("slots evenly filled:", dict(sorted(counts.items())))

# ---------- assignment is random, not positional ----------
# With positional dealing the strongest clip always lands in slot 1.
best_slot = next(s for s, v in sel.items() if any(c["video_id"] == "v000" for c in v))
ranks_by_slot = {s: [int(c["video_id"][1:]) for c in v] for s, v in sel.items()}
means = {s: sum(r) / len(r) for s, r in ranks_by_slot.items()}
print(f"strongest clip landed in slot {best_slot}; slot mean ranks "
      f"{ {s: round(m,1) for s,m in sorted(means.items())} }")
assert len(set(round(m) for m in means.values())) > 1, "means identical — suspicious"

# ---------- reproducible ----------
again = batching.select_clips(clips, slots=5, per_slot=10, strategy="top_views")
assert {s: [c["video_id"] for c in v] for s, v in again.items()} == \
       {s: [c["video_id"] for c in v] for s, v in sel.items()}
print("selection is reproducible (a resumed job rebuilds the same layout)")

# ---------- manual ----------
wanted = ["v070", "v005", "v033"]
sel_m = batching.select_clips(clips, slots=5, per_slot=10,
                              strategy="manual", picked_ids=wanted)
got = [c["video_id"] for s in sel_m.values() for c in s]
assert sorted(got) == sorted(wanted), got
print("manual picks exactly what was asked for, ignoring view count:", sorted(got))

# unknown ids are dropped rather than crashing
sel_u = batching.select_clips(clips, 5, 10, "manual", ["v001", "nope", "v002"])
got = [c["video_id"] for s in sel_u.values() for c in s]
assert sorted(got) == ["v001", "v002"], got
print("unknown ids ignored:", sorted(got))

# ---------- all ----------
sel_a = batching.select_clips(clips, slots=5, per_slot=10, strategy="all")
got = [c["video_id"] for s in sel_a.values() for c in s]
assert len(got) == 80 and len(set(got)) == 80
print(f"'all' used every one of the {len(got)} clips, still no repeats")

# ---------- fewer clips than slots need ----------
few = make_clips(3)
sel_f = batching.select_clips(few, slots=5, per_slot=10, strategy="top_views")
got = [c["video_id"] for s in sel_f.values() for c in s]
assert len(got) == 3 and len(set(got)) == 3, got
used_slots = [s for s, v in sel_f.items() if v]
print(f"3 clips into 5 slots: {len(used_slots)} slots filled, no clip duplicated to pad")

# ---------- nothing ----------
assert batching.select_clips([], 5, 10) == {}
assert batching.select_clips([{"video_id": "x"}], 5, 10) == {}, "clip with no path"
print("empty / pathless input returns nothing rather than crashing")

# ---------- materialise onto disk ----------
assets = Path(tempfile.mkdtemp(prefix="assets_")) / "assets"
n = batching.materialize_slots(sel, assets)
assert n == 50, n
dirs = sorted(d.name for d in assets.iterdir() if d.is_dir())
assert dirs == [f"cta_slot_{i}" for i in range(1, 6)], dirs
per = {d.name: len(list(d.glob("*.mp4"))) for d in sorted(assets.iterdir()) if d.is_dir()}
assert all(v == 10 for v in per.values()), per
print(f"materialised {n} clips into {dirs}")

# the render side must read them back in the same shape as an upload
from workspace import workspace_from_dir
(assets / "input.mp4").write_bytes(b"promo")
ws = workspace_from_dir(assets, assets.parent / "work")
assert len(ws.cta_video_slots) == 5, ws.cta_video_slots
assert all(len(s) == 10 for s in ws.cta_video_slots), [len(s) for s in ws.cta_video_slots]
print("workspace_from_dir sees them exactly as it would uploaded clips")

# empty slots keep their position, or per-slot speeds shift onto wrong clips
sel_gap = {1: sel[1], 2: [], 3: sel[3], 4: [], 5: sel[5]}
assets2 = Path(tempfile.mkdtemp(prefix="gap_")) / "assets"
batching.materialize_slots(sel_gap, assets2)
(assets2 / "input.mp4").write_bytes(b"promo")
ws2 = workspace_from_dir(assets2, assets2.parent / "work")
assert len(ws2.cta_video_slots) == 5
assert ws2.cta_video_slots[1] == [] and ws2.cta_video_slots[3] == []
print("empty slots keep their position:", [len(s) for s in ws2.cta_video_slots])

# re-running must not duplicate files (resume safety)
batching.materialize_slots(sel, assets)
per2 = {d.name: len(list(d.glob("*.mp4"))) for d in sorted(assets.iterdir())
        if d.is_dir() and d.name.startswith("cta_slot_")}
assert per2 == per, (per, per2)
print("re-materialising is idempotent — resume doesn't double the clips")

print("\nALL SELECTION TESTS PASSED")
