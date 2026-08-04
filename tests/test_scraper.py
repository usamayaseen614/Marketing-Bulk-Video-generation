"""Scraper planning, dedup, trimming and account parsing."""
import os, sys, tempfile
from collections import Counter
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="scrapetest_")
sys.path.insert(0, str(PROJ))

import config
from jobs import store
from scrapers import tiktok

# ---------- account parsing ----------
for raw, want in [
    ("https://www.tiktok.com/@someaccount", "someaccount"),
    ("https://www.tiktok.com/@some.account?lang=en", "some.account"),
    ("@handle_1", "handle_1"),
    ("handle_1", "handle_1"),
]:
    got = tiktok.account_name(raw)
    assert got == want, f"{raw!r} -> {got!r}, wanted {want!r}"
assert tiktok.profile_url("@bob") == "https://www.tiktok.com/@bob"
assert tiktok.profile_url("https://www.tiktok.com/@bob?x=1") == "https://www.tiktok.com/@bob"
print("account parsing ok")

# ---------- round-robin: 50 clips, 5 slots -> exactly 10 per slot ----------
ids = [f"v{i}" for i in range(200)]
plan = tiktok.plan_batches(ids, n_batches=4, batch_size=50, slots=5)
assert len(plan) == 200, len(plan)

for b in range(1, 5):
    batch = [p for p in plan if p.batch == b]
    assert len(batch) == 50, f"batch {b} has {len(batch)}"
    per_slot = Counter(p.slot for p in batch)
    assert set(per_slot) == {1, 2, 3, 4, 5}, per_slot
    assert all(v == 10 for v in per_slot.values()), per_slot
print("round-robin: 4 batches x 50 clips -> 10 per slot in every batch")

# the documented mapping: 1->1, 2->2 ... 5->5, 6->1
first = sorted([p for p in plan if p.batch == 1], key=lambda p: p.position)
assert [p.slot for p in first[:7]] == [1, 2, 3, 4, 5, 1, 2], [p.slot for p in first[:7]]
print("slot mapping 1,2,3,4,5,1,2 confirmed")

# with 200 unique ids and 200 needed, no clip repeats
assert len({p.video_id for p in plan}) == 200

# ---------- wrap-around must reshuffle, not clone ----------
small = [f"v{i}" for i in range(50)]          # exactly one batch worth
wrapped = tiktok.plan_batches(small, n_batches=3, batch_size=50, slots=5)
b1 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 1], key=lambda p: p.position)]
b2 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 2], key=lambda p: p.position)]
b3 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 3], key=lambda p: p.position)]
assert set(b1) == set(b2) == set(b3), "same pool expected"
assert b1 != b2, "batch 2 is a clone of batch 1 — wrap did not reshuffle"
assert b2 != b3, "batch 3 is a clone of batch 2"
print("wrap-around reshuffles: batch2 != batch1 != batch3 (same pool, new order)")

# and the slot groupings genuinely differ
slots_b1 = {p.video_id: p.slot for p in wrapped if p.batch == 1}
slots_b2 = {p.video_id: p.slot for p in wrapped if p.batch == 2}
moved = sum(1 for v in slots_b1 if slots_b1[v] != slots_b2[v])
assert moved > 20, f"only {moved}/50 clips changed slot on wrap"
print(f"  {moved}/50 clips landed in a different slot on the wrap")

# ---------- determinism: same inputs -> same plan ----------
again = tiktok.plan_batches(small, n_batches=3, batch_size=50, slots=5)
assert [(p.video_id, p.batch, p.slot) for p in wrapped] == \
       [(p.video_id, p.batch, p.slot) for p in again], "plan is not reproducible"
print("planning is deterministic across runs")

# ---------- edge cases ----------
assert tiktok.plan_batches([], 3) == []
assert tiktok.plan_batches(ids, 0) == []
dump = tiktok.plan_dump(["a", "b", "c"])
assert [d.position for d in dump] == [1, 2, 3]
assert all(d.batch == 0 and d.slot == 0 for d in dump)
print("edge cases ok")

# ---------- dedup store ----------
store.init_db()
assert store.known_clip_ids("acct") == set()
store.remember_clips("acct", [{"video_id": "v1", "content_hash": "h1", "duration": 10.0},
                              {"video_id": "v2", "content_hash": "h2", "duration": 9.5}],
                     job_id="j1")
assert store.known_clip_ids("acct") == {"v1", "v2"}
assert store.known_content_hashes("acct") == {"h1", "h2"}
# other accounts are isolated
assert store.known_clip_ids("other") == set()
# re-remembering is harmless
store.remember_clips("acct", [{"video_id": "v1", "content_hash": "h1"}], job_id="j2")
assert len(store.known_clip_ids("acct")) == 2
accounts = store.scraped_accounts()
assert accounts[0]["account"] == "acct" and accounts[0]["clips"] == 2, accounts
assert store.forget_account("acct") == 2
assert store.known_clip_ids("acct") == set()
print("dedup store ok (isolated per account, idempotent, forgettable)")

# ---------- trimming ----------
import subprocess
SA = PROJ / "sample_assets"
TMP = Path(os.environ["BVG_JOBS_ROOT"])
ffmpeg = tiktok.find_ffmpeg()
short_src = SA / "cta_video_1.mp4"
print(f"short sample duration: {tiktok.probe_duration(short_src)}s")

# A real TikTok clip is 20-60s; the samples are 2s, so build a 20s stand-in.
long_src = TMP / "long.mp4"
subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-stream_loop", "20",
                "-i", str(short_src), "-t", "20", "-c:v", "libx264",
                "-preset", "veryfast", "-pix_fmt", "yuv420p", str(long_src)],
               check=True, timeout=180)
print(f"built a {tiktok.probe_duration(long_src)}s source to trim")

# The whole point of re-encoding: an EXACT window, not keyframe-snapped.
out = TMP / "trimmed.mp4"
tiktok.trim_clip(long_src, out, start=1.0, duration=10.0)
got = tiktok.probe_duration(out)
assert out.is_file() and out.stat().st_size > 0
assert abs(got - 10.0) < 0.25, f"expected ~10.0s, got {got}s"
print(f"trim 1s->11s produced {got}s (exact, not the 8-12s a stream copy gives)")

# Trim, not filter: a clip shorter than the window is kept at its own length.
out2 = TMP / "shorter.mp4"
tiktok.trim_clip(short_src, out2, start=1.0, duration=10.0)
got2 = tiktok.probe_duration(out2)
assert out2.is_file() and got2 > 0, "short clip must still be kept"
assert got2 < 2.0, got2
print(f"2s clip with a 1s->11s window kept {got2}s — trimmed, not dropped")

# A clip shorter than the START offset would otherwise produce an empty file.
tiny = TMP / "tiny.mp4"
tiktok.trim_clip(short_src, tiny, start=5.0, duration=10.0)   # start past the end
assert tiny.is_file() and tiny.stat().st_size > 0, "start past the end made an empty file"
assert tiktok.probe_duration(tiny) > 0
print(f"start past clip end slid back to 0 -> {tiktok.probe_duration(tiny)}s, not empty")

# ---------- content hash ----------
h1 = tiktok.content_hash(short_src)
h2 = tiktok.content_hash(short_src)
assert h1 == h2 and len(h1) == 64
assert tiktok.content_hash(out) != h1
print("content hash stable and discriminating")

print("\nALL SCRAPER TESTS PASSED")
