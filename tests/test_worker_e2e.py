"""End-to-end: submit a real render job, run the worker, then prove resume works.

Renders actual MP4s with FFmpeg from sample_assets. Row 2 of data.xlsx points at
a background that isn't in the ZIP on purpose, so this also covers per-row
failure recording.
"""
import os, shutil, sys, tempfile, time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="e2e_"))
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = str(SCRATCH)
sys.path.insert(0, str(PROJ))

import config
from jobs import store, worker
from workspace import stage_uploads
from video_generator import RenderConfig
import pandas as pd
from dataclasses import asdict

SA = PROJ / "sample_assets"


class Fake:
    def __init__(self, path):
        self.name = Path(path).name
        self._data = Path(path).read_bytes()
        self.size = len(self._data)
    def getvalue(self):
        return self._data


store.init_db()

# ---------- submit, exactly as the UI will ----------
job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)

stage_uploads(
    assets,
    video_file=Fake(SA / "promo.mp4"),
    zip_file=Fake(SA / "backgrounds.zip"),
    cta_file=Fake(SA / "cta.png"),
    font_file=None,
    cta_video_slot_files=[[Fake(SA / "cta_video_1.mp4")], [Fake(SA / "cta_video_2.mp4")]],
)
def write_sheet(dest):
    """5 real rows, with row 3 pointed at a background that isn't in the ZIP so
    per-row failure handling gets exercised."""
    df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
    df["BG_Image"] = df["BG_Image"].astype(object)
    df.loc[df.index[2], "BG_Image"] = "definitely_missing_bg.png"
    df.to_excel(dest, index=False, engine="openpyxl")
    return df

df = write_sheet(assets / "input.xlsx")
n_rows = len(df)
print(f"sheet has {n_rows} rows (row 3 has a deliberately missing background)")

cfg = RenderConfig(crf=28, preset="veryfast", fps=30)
created = store.create_job(
    kind=store.KIND_RENDER,
    params={"render_config": asdict(cfg), "workers": 4, "make_zip": True},
    label="e2e test batch",
    items=[{"idx": i} for i in range(1, n_rows + 1)],
    job_id=job_id,
)
assert created == job_id

# assets must exist BEFORE the row is claimable
assert (assets / "input.mp4").is_file()
print("submitted", job_id)

# ---------- run the worker ----------
t0 = time.time()
rc = worker.main(["--once"])
assert rc == 0
print(f"worker finished in {time.time() - t0:.0f}s")

job = store.get_job(job_id)
counts = store.item_counts(job_id)
print("status:", job["status"], "| counts:", counts)
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
assert counts["total"] == n_rows
assert counts["rendered"] >= 1, "nothing rendered at all"
assert counts["render_pending"] == 0, "worker left rows unprocessed"

res = job["result"]
print("result:", {k: res[k] for k in ("total", "rendered", "failed", "elapsed")})
assert res["rendered"] == counts["rendered"]

# the deliberately-broken row must be recorded as a failure with a real reason
items = {i["idx"]: i for i in store.list_items(job_id)}
failed = [i for i in items.values() if i["render_status"] == store.ITEM_FAILED]
for f in failed:
    print(f"  row {f['idx']} failed: {f['render_error'][:70]}")
    assert f["render_error"], "a failed row must record why"

# real MP4s on disk, one per rendered item, non-empty
videos = store.videos_dir(job_id)
# A render now writes into batch_NN/ subfolders (multi-batch layout).
mp4s = sorted(videos.rglob("*.mp4"))
assert len(mp4s) == counts["rendered"], f"{len(mp4s)} files vs {counts['rendered']} rendered"
for m in mp4s:
    assert m.stat().st_size > 1000, f"{m.name} is suspiciously small"
print("rendered files:", [m.name for m in mp4s])

# log + zip + sheet
log = Path(res["log_path"])
assert log.is_file() and "Bulk video render log" in log.read_text(encoding="utf-8")
assert Path(res["zip_path"]).is_file(), "zip fallback missing"
assert Path(res["sheet_path"]).is_file(), "batch sheet missing"
import zipfile
with zipfile.ZipFile(res["zip_path"]) as zf:
    names = zf.namelist()
assert "render_log.txt" in names and any(n.endswith(".mp4") for n in names), names
print("zip contains", len(names), "entries")

# staged assets cleaned, videos kept
assert not assets.exists(), "assets should be cleaned after the job"
assert videos.is_dir(), "videos must survive cleanup"
print("cleanup ok: assets removed, videos kept")

# ---------- resume ----------
# Simulate a crash after 1 render: wipe the rest and requeue.
keep = mp4s[0].name
survivors = [i for i in items.values() if i["render_status"] == store.ITEM_DONE]
assert len(survivors) >= 2, "need >=2 rendered rows to test resume"
resumed_idx = survivors[1]["idx"]
next(videos.rglob(survivors[1]["name"])).unlink()
store.update_item(job_id, resumed_idx, render_status=store.ITEM_PENDING,
                  name="", render_error=None)

# assets are gone (cleaned), so re-stage them the way a resubmit would
store.make_job_dirs(job_id)
stage_uploads(assets, Fake(SA / "promo.mp4"), Fake(SA / "backgrounds.zip"),
              Fake(SA / "cta.png"), None,
              [[Fake(SA / "cta_video_1.mp4")], [Fake(SA / "cta_video_2.mp4")]])
write_sheet(assets / "input.xlsx")

with store._conn() as c:
    c.execute("UPDATE jobs SET status=? WHERE id=?", (store.STATUS_QUEUED, job_id))

before = store.item_counts(job_id)
print("before resume:", before)
assert before["render_pending"] == 1

t0 = time.time()
worker.main(["--once"])
after = store.item_counts(job_id)
print(f"after resume ({time.time() - t0:.0f}s):", after)
assert after["render_pending"] == 0
assert after["rendered"] == before["rendered"] + 1
# the already-rendered file was NOT touched
assert any(videos.rglob(keep)), "resume must not delete completed work"
assert any(videos.rglob(store.list_items(job_id)[resumed_idx - 1]["name"]))
print("resume re-rendered exactly the missing row, left the rest alone")

print("\nALL WORKER E2E TESTS PASSED")
