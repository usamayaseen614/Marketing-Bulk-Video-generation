"""Multi-batch layout and the mix that follows it."""
import os
import sys
from collections import Counter
from pathlib import Path

# Never let a developer's real .env under test — batching reads config for its
# default batch size, and a local override would change what these assert.
os.environ["BVG_IGNORE_DOTENV"] = "1"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import batching
from batching import Slot

# ---------- the real shape: 10 batches x 1000 rows ----------
N_BATCHES, N_ROWS = 10, 1000
slots = batching.plan_render(N_BATCHES, N_ROWS)
assert len(slots) == 10_000, len(slots)
assert len(set(slots)) == 10_000, "duplicate (batch,row) pairs"
print(f"render plan: {N_BATCHES} batches x {N_ROWS} rows = {len(slots):,} videos")

# ---------- index round-trip (job_items keys) ----------
for s in (Slot(1, 1), Slot(1, 1000), Slot(2, 1), Slot(10, 1000)):
    idx = batching.item_index(s.batch, s.row, N_ROWS)
    assert batching.split_index(idx, N_ROWS) == (s.batch, s.row), (s, idx)
idxs = [batching.item_index(s.batch, s.row, N_ROWS) for s in slots]
assert sorted(idxs) == list(range(1, 10_001)), "idx must be a dense 1..N range"
print("item_index <-> split_index round-trips over all 10,000")

# ---------- the mix ----------
placement = batching.mix_into_folders(slots, N_BATCHES)
assert len(placement) == 10_000

counts = Counter(placement.values())
assert set(counts) == set(range(1, 11)), sorted(counts)
assert all(v == 1000 for v in counts.values()), counts
print(f"\nevery folder holds exactly {counts[1]:,} videos")

# Each folder must contain an EVEN share of every source batch — that is the
# difference between stratified and a plain shuffle.
summary = batching.summarize(placement, N_BATCHES)
for folder in range(1, 11):
    spread = summary[folder]["from_batch"]
    assert set(spread) == set(range(1, 11)), (folder, spread)
    assert all(v == 100 for v in spread.values()), (folder, spread)
print("every folder draws exactly 100 videos from each of the 10 source batches")
print("  folder_01 spread:", dict(sorted(summary[1]["from_batch"].items())))

# ---------- a folder must NOT be a whole source batch ----------
for folder in range(1, 11):
    members = [s for s, f in placement.items() if f == folder]
    batches_present = {s.batch for s in members}
    assert len(batches_present) == 10, (folder, batches_present)
print("no folder is just one source batch (all 10 promo videos represented)")

# ---------- determinism: a resumed job must re-derive the SAME layout ----------
again = batching.mix_into_folders(batching.plan_render(N_BATCHES, N_ROWS), N_BATCHES)
assert again == placement, "mix is not reproducible — a resume would scatter files"
print("mix is reproducible across runs (safe to resume mid-upload)")

# adding a batch must not reshuffle the earlier ones
more = batching.mix_into_folders(batching.plan_render(N_BATCHES + 1, N_ROWS), N_BATCHES)
same = [s for s in slots if more.get(s) == placement.get(s)]
assert len(same) == len(slots), f"only {len(same)}/{len(slots)} kept their folder"
print("adding an 11th batch leaves the first 10 batches' placement untouched")

# ---------- awkward shapes ----------
print("\n--- edge cases ---")
# more folders than videos
p = batching.mix_into_folders(batching.plan_render(1, 3), 10)
assert len(p) == 3 and len(set(p.values())) == 3
print("  3 videos into 10 folders: used", sorted(set(p.values())))

# a single batch still spreads across folders
p = batching.mix_into_folders(batching.plan_render(1, 100), 10)
c = Counter(p.values())
assert all(v == 10 for v in c.values()), c
print("  1 batch x 100 rows into 10 folders: 10 each")

# rows that don't divide evenly
p = batching.mix_into_folders(batching.plan_render(3, 7), 3)
c = Counter(p.values())
assert sum(c.values()) == 21
assert max(c.values()) - min(c.values()) <= 1, c
print("  3x7=21 into 3 folders:", dict(sorted(c.items())), "(within 1 of even)")

# one folder
p = batching.mix_into_folders(batching.plan_render(2, 5), 1)
assert set(p.values()) == {1} and len(p) == 10
print("  everything into 1 folder: ok")

# nothing to place
assert batching.mix_into_folders([], 5) == {}
assert batching.plan_render(0, 100) == []
assert batching.plan_render(10, 0) == []
print("  empty plans: ok")

# n_folders = 0 must not divide by zero
p = batching.mix_into_folders(batching.plan_render(1, 4), 0)
assert set(p.values()) == {1}, p
print("  n_folders=0 clamps to 1 rather than crashing")

assert batching.folder_name(3) == "batch_03"
assert batching.folder_name(12) == "batch_12"
print("\nfolder naming:", batching.folder_name(1), batching.folder_name(10))

# ---------- assign_promos: complete row x promo coverage ----------
# Had zero coverage anywhere, and the property its docstring is built around
# (every row rendered once with every promo) was never asserted.
sl = batching.plan_render(5, 5)
pm = batching.assign_promos(sl, 5)
assert len({(x.row, pm[x]) for x in sl}) == 25, "every row x promo pairing, once"
assert set(pm.values()) == set(range(5)), pm
assert batching.assign_promos(batching.plan_render(3, 2), 5)[Slot(3, 1)] == 2
assert set(batching.assign_promos(sl, 0).values()) == {0}, "n_promos=0 clamps to 1"
print()
print("assign_promos: 5 rows x 5 promos gives all 25 pairings exactly once")

# ---------- the predicted bound must hold against the REAL mixer ----------
# max_copies_per_promo is arithmetic app.py shows the user BEFORE a run. It is
# worth something only while it tracks what mix_into_folders actually does.
SHAPES = [(20, 30, 1, 20), (20, 30, 3, 20), (60, 10, 1, 60), (100, 6, 1, 100),
          (100, 60, 10, 100), (100, 60, 6, 100), (200, 60, 10, 100),
          (100, 37, 7, 100), (5, 7, 3, 2), (7, 13, 4, 3), (1, 100, 10, 1)]
for nb, nr, nf, n_promos in SHAPES:
    sl = batching.plan_render(nb, nr)
    place = batching.mix_into_folders(sl, nf)
    promo = batching.assign_promos(sl, n_promos)
    real = max(Counter((place[x], promo[x]) for x in sl).values())
    bound = batching.max_copies_per_promo(nb, nr, nf, n_promos)
    assert real <= bound, (nb, nr, nf, n_promos, real, bound)
    # Exact, not merely safe, whenever folders divide rows or each promo owns
    # one batch — the two shapes a real render is normally set up as.
    if nr % nf == 0 or nb <= n_promos:
        assert real == bound, (nb, nr, nf, n_promos, real, bound)
    # summarize must measure after the fact what the formula predicted before.
    assert max(v["max_per_promo"]
               for v in batching.summarize(place, nf, promo).values()) == real
print(f"max_copies_per_promo holds over {len(SHAPES)} shapes, exact when folders "
      "divide rows or each promo owns one batch")

# The 600-videos-per-folder case the copies limit exists for: 20 promos cannot
# do it at any folder size, 60 can.
assert batching.max_copies_per_promo(20, 30, 1, 20) == 30
assert batching.max_copies_per_promo(60, 10, 1, 60) == 10
assert batching.max_copies_per_promo(100, 60, 10, 100) == 6
print("600-video folder: 20 promos gives 30 copies each, 60 gives 10, 100 gives 6")

# Old callers pass no promo map and must see exactly what they always saw.
assert "max_per_promo" not in batching.summarize(
    batching.mix_into_folders(batching.plan_render(2, 4), 2), 2)[1]
print("summarize stays backwards compatible without promo_for")
print()
print("\nALL BATCHING TESTS PASSED")
