"""Exercise jobs/store.py: claim, per-item state, resume, stale recovery."""
import os, sys, tempfile, time
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="storetest_"))
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = str(SCRATCH)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from jobs import store

assert config.JOBS_ROOT == SCRATCH.resolve(), config.JOBS_ROOT
store.init_db()

# --- create a 5-item render job
job_id = store.create_job(
    kind=store.KIND_RENDER, params={"crf": 18, "workers": 10},
    label="test batch", notify_email="a@b.com", submitted_by="sess1",
    items=[{"idx": i, "name": f"{i:03d}_x.mp4"} for i in range(1, 6)],
)
store.make_job_dirs(job_id)
print("created", job_id, "counts:", store.item_counts(job_id))
assert store.item_counts(job_id)["total"] == 5

# --- claim it
claimed = store.claim_next_job()
assert claimed and claimed["id"] == job_id, claimed
assert claimed["status"] == store.STATUS_RUNNING
assert claimed["params"]["workers"] == 10, "params must round-trip through JSON"
print("claimed, attempts =", claimed["attempts"])

# nothing else queued
assert store.claim_next_job() is None, "claimed a job twice!"

# --- simulate: 3 render ok, 1 failed, 1 never touched (worker died)
for i in (1, 2, 3):
    store.update_item(job_id, i, render_status=store.ITEM_DONE,
                      warnings=[f"w{i}"] if i == 2 else [])
store.update_item(job_id, 4, render_status=store.ITEM_FAILED,
                  render_error="Background image 'x.jpg' not found in the ZIP")
counts = store.item_counts(job_id)
print("after renders:", counts)
assert counts == {"total": 5, "rendered": 3, "render_failed": 1,
                  "render_pending": 1, "uploaded": 0, "upload_failed": 0}, counts

# --- resume semantics: the untouched item is pending, and so is the failed one
# while it is still under the attempts cap. Renders used to be treated as
# deterministic and never retried, which stranded 832 rows of a 12,000-row
# batch that had died to the OOM killer — a failure that says nothing about
# the row. See config.RENDER_ATTEMPTS.
pending = store.pending_render_items(job_id)
assert [p["idx"] for p in pending] == [4, 5], pending
print("resume would re-render:", [p["idx"] for p in pending])

# At the cap it is left alone, so a row that fails every time cannot loop.
store.update_item(job_id, 4, render_attempts=config.RENDER_ATTEMPTS)
assert [p["idx"] for p in store.pending_render_items(job_id)] == [5]
# An explicit cap still overrides, the same way pending_upload_items allows.
assert [p["idx"] for p in
        store.pending_render_items(job_id, max_attempts=99)] == [4, 5]
print("failed row retried under the cap, dropped at it")

# --- uploads: only rendered items are candidates
up = store.pending_upload_items(job_id)
assert [u["idx"] for u in up] == [1, 2, 3], up
store.update_item(job_id, 1, upload_status=store.ITEM_DONE,
                  drive_file_id="fid1", drive_link="https://drive/1")
store.update_item(job_id, 2, upload_status=store.ITEM_FAILED,
                  upload_error="503", upload_attempts=1)
# a failed upload under the attempt cap IS retried
up = store.pending_upload_items(job_id, max_attempts=3)
assert [u["idx"] for u in up] == [2, 3], up
# at the cap it is not
up = store.pending_upload_items(job_id, max_attempts=1)
assert [u["idx"] for u in up] == [3], up
print("upload retry honours the attempt cap")

# --- warnings/meta round-trip
items = {i["idx"]: i for i in store.list_items(job_id)}
assert items[2]["warnings"] == ["w2"], items[2]
assert items[4]["render_error"].startswith("Background image"), items[4]

# --- bad column is rejected rather than interpolated into SQL
try:
    store.update_item(job_id, 1, **{"name=1; DROP TABLE jobs; --": "x"})
    raise AssertionError("should have rejected unknown field")
except ValueError as exc:
    print("rejected bad column:", str(exc)[:50])

# --- stale recovery: heartbeat goes quiet -> requeued, resumes
store.heartbeat(job_id, stage="rendering")
requeued = store.requeue_stale_jobs(stale_seconds=10_000)
assert requeued == [], "fresh heartbeat must not be requeued"
requeued = store.requeue_stale_jobs(stale_seconds=-1)
assert requeued == [job_id], requeued
assert store.get_job(job_id)["status"] == store.STATUS_QUEUED
print("stale job requeued ->", store.get_job(job_id)["status"])

# --- attempt cap turns a repeatedly-crashing job into a failure
for _ in range(5):
    if store.claim_next_job() is None:
        break
    store.requeue_stale_jobs(stale_seconds=-1, max_attempts=3)
final = store.get_job(job_id)
assert final["status"] == store.STATUS_FAILED, final["status"]
assert "Abandoned after" in (final["error"] or ""), final["error"]
print("attempt cap -> failed:", final["error"][:60])

# --- finish + result round-trip
store.finish_job(job_id, store.STATUS_SUCCEEDED,
                 result={"drive_link": "https://drive/folder", "rendered": 4})
got = store.get_job(job_id)
assert got["result"]["rendered"] == 4, got["result"]

# --- cancel only applies to queued jobs
j2 = store.create_job(kind=store.KIND_SCRAPE, label="scrape")
assert store.cancel_job(j2) is True
assert store.cancel_job(job_id) is False, "must not cancel a finished job"
print("cancel guard ok")

# --- listing / filtering
assert len(store.list_jobs(limit=10)) == 2
assert [j["id"] for j in store.list_jobs(kinds=[store.KIND_SCRAPE])] == [j2]
print("list_jobs ok")

# --- reaper
store.make_job_dirs(j2)
assert store.job_dir(j2).is_dir()
removed = store.reap_old_jobs(retention_days=-1)
assert j2 in removed and not store.job_dir(j2).is_dir(), removed
assert store.get_job(j2) is not None, "history row must survive the reaper"
print("reaper removed folders, kept history:", removed)

# --- merge_job_params: the Drive stamp must survive a resume ----------------
j3 = store.create_job(kind=store.KIND_RENDER, params={"folders": 3},
                      label="stamped")
merged = store.merge_job_params(j3, drive_stamp="2026-08-05_14-23-45.123")
assert merged["drive_stamp"] == "2026-08-05_14-23-45.123", merged
assert merged["folders"] == 3, "merge must not drop existing params"
assert store.get_job(j3)["params"] == merged, "params must round-trip"

# merging again leaves earlier keys alone
store.merge_job_params(j3, drive_folder="https://drive/x")
again = store.get_job(j3)["params"]
assert again["drive_stamp"] == "2026-08-05_14-23-45.123", again
assert again["folders"] == 3 and again["drive_folder"] == "https://drive/x", again
assert store.merge_job_params("no-such-job", k="v") == {"k": "v"}
print("merge_job_params ok:", again)

# the pinning itself: decided once, identical on every later run
from jobs.runners import render as render_runner

j4 = store.create_job(kind=store.KIND_RENDER, params={}, label="pin")
p4: dict = store.get_job(j4)["params"]
first = render_runner._drive_root_stamp(j4, p4)
assert p4["drive_stamp"] == first, p4
# a "resumed" run re-reads params from the database and must not re-stamp
p4_reload = store.get_job(j4)["params"]
assert p4_reload["drive_stamp"] == first, p4_reload
time.sleep(0.02)
assert render_runner._drive_root_stamp(j4, p4_reload) == first, "re-stamped on resume!"
# and a fresh stamp really does move
assert render_runner._drive_stamp() != first
assert len(first) == len("2026-08-05_14-23-45.123"), first
print("drive stamp pinned across resume:", first)


# --- cleanup_job_dir: the keep branch vs the purge branch -------------------
# This is the regression test for the bug that made the whole feature
# necessary: the old cleanup's allow-list was literally ["assets", "work"], so
# every directory a later runner invented outlived the job by a week.
def _populate(jid):
    root = store.job_dir(jid)
    for rel in ("assets/x.mp4", "work/y.png", "packing/batch_01/yt.zip.part",
                "videos/source_01/a.mp4", "clips/c.mp4",
                "marketing_videos.zip", "render_manifest.xlsx",
                "batch_01_manifest.xlsx", "render_log.txt"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


j4 = store.create_job(kind=store.KIND_RENDER, label="cleanup")
root = _populate(j4)
store.cleanup_job_dir(j4, keep_outputs=True)
assert not (root / "assets").exists(), "staged uploads must go"
assert not (root / "work").exists(), "scratch must go"
assert not (root / "packing").exists(), "half-written archives must go"
assert (root / "videos/source_01/a.mp4").is_file(), "unpublished videos must stay"
assert (root / "clips/c.mp4").is_file(), "unpublished clips must stay"
assert (root / "marketing_videos.zip").is_file(), "the fallback ZIP must stay"
assert (root / "render_manifest.xlsx").is_file()
print("cleanup keep branch ok: only reproducible scratch removed")

# The purge branch takes everything but the kilobytes of reports, including a
# ZIP the Jobs page had moved outside JOBS_ROOT.
moved = config.STATIC_DOWNLOADS / j4 / "big.zip"
moved.parent.mkdir(parents=True, exist_ok=True)
moved.write_bytes(b"x")
store.cleanup_job_dir(j4)
assert root.is_dir(), "the job folder itself stays, holding the reports"
left = sorted(p.name for p in root.iterdir())
assert left == ["batch_01_manifest.xlsx", "render_log.txt",
                "render_manifest.xlsx"], left
assert not moved.parent.exists(), "the relocated ZIP must go with its job"
print("cleanup purge branch ok, kept only:", left)

# --- cancel_job cleans up the staged upload a queued job never used ---------
j5 = store.create_job(kind=store.KIND_RENDER, label="cancel-cleanup")
_populate(j5)
assert store.cancel_job(j5) is True
assert not (store.job_dir(j5) / "assets").exists(), "cancelled staging must go"

# A cancel that changes no row must not delete that job's folder.
j6 = store.create_job(kind=store.KIND_RENDER, label="already-finished")
store.finish_job(j6, store.STATUS_SUCCEEDED, result={})
_populate(j6)
assert store.cancel_job(j6) is False
assert (store.job_dir(j6) / "videos/source_01/a.mp4").is_file(), \
    "the rmtree must not escape the rowcount check"
print("cancel_job cleanup ok, and guarded by rowcount")

# --- an abandoned job loses its scratch but keeps unpublished videos --------
# Drain the queue first: claim_next_job takes the oldest queued job, so
# anything left over from the tests above would soak up the attempts below and
# j7 would never reach the cap.
while True:
    _left = store.claim_next_job()
    if _left is None:
        break
    store.finish_job(_left["id"], store.STATUS_SUCCEEDED, result={})

j7 = store.create_job(kind=store.KIND_RENDER, label="abandoned",
                      items=[{"idx": 1, "name": "a.mp4"}])
_populate(j7)
for _ in range(5):
    if store.claim_next_job() is None:
        break
    store.requeue_stale_jobs(stale_seconds=-1, max_attempts=3)
assert store.get_job(j7)["status"] == store.STATUS_FAILED
assert not (store.job_dir(j7) / "assets").exists(), "abandoned staging must go"
assert not (store.job_dir(j7) / "work").exists()
assert (store.job_dir(j7) / "videos/source_01/a.mp4").is_file(), \
    "an abandoned job's unpublished renders must survive"
print("abandoned job cleaned to scratch only")

# --- the reaper sweeps folders that never got a database row ---------------
orphan = config.JOBS_ROOT / "deadbeefdeadbeef"
(orphan / "assets").mkdir(parents=True, exist_ok=True)
(orphan / "assets" / "input.mp4").write_bytes(b"x")
repack = config.JOBS_ROOT / "_repack"
repack.mkdir(parents=True, exist_ok=True)
(repack / "scratch.zip").write_bytes(b"x")
os.utime(orphan, (0, 0))

removed = store.reap_old_jobs(retention_days=0)
assert "deadbeefdeadbeef" in removed and not orphan.exists(), removed
assert repack.is_dir(), "_-prefixed scratch is the operator's, not the reaper's"
assert config.DB_PATH.is_file(), "the reaper must not touch the database"
print("orphan sweep ok, left _repack and jobs.db alone:", removed)

print("\nALL STORE TESTS PASSED")
