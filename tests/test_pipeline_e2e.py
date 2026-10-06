"""The chained flow end to end: clips from a previous scrape -> captions ->
render -> mix, as one submitted job."""
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="pipe_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import batching
from jobs import store, worker
from workspace import stage_uploads
from video_generator import RenderConfig

SA = PROJ / "sample_assets"
N_BATCHES = 2


class Fake:
    def __init__(self, p, name=None):
        self.name = name or Path(p).name
        self._d = Path(p).read_bytes()
        self.size = len(self._d)
    def getvalue(self): return self._d


store.init_db()

# ---------- stand in for a finished scrape sitting on this machine ----------
scrape_id = store.new_job_id()
store.make_job_dirs(scrape_id)
clips_dir = store.job_dir(scrape_id) / "clips"
clips_dir.mkdir(parents=True, exist_ok=True)

sources = [SA / "cta_video_1.mp4", SA / "cta_video_2.mp4", SA / "cta_video_3.mp4",
           SA / "cta_video_4.mp4", SA / "cta_video_5.mp4"]
items, rows = [], []
for i in range(12):
    vid = f"70000000000000{i:02d}"
    dest = clips_dir / f"{vid}.mp4"
    shutil.copy2(sources[i % len(sources)], dest)
    items.append({"idx": i + 1, "name": dest.name, "stage": store.STAGE_SCRAPE,
                  "meta": {"video_id": vid}})
    rows.append({"Video_ID": vid, "Views": (12 - i) * 1000, "Likes": i})

store.create_job(kind=store.KIND_SCRAPE, label="fake-scrape", job_id=scrape_id)
store.add_items(scrape_id, items)
for it in items:
    store.update_item(scrape_id, it["idx"], render_status=store.ITEM_DONE)
store.finish_job(scrape_id, store.STATUS_SUCCEEDED, result={"account": "fake"})
pd.DataFrame(rows).to_excel(store.job_dir(scrape_id) / "metadata.xlsx",
                            index=False, engine="openpyxl")
print(f"pretend scrape {scrape_id}: {len(items)} clips on disk with view counts")

# a caption pool, so filenames come from captions
store.save_pool("pipeline test",
                [f"Caption number {i} that reads like a real post" for i in range(40)],
                [f"#tag{i} #asmr #fyp" for i in range(10)], model="stub")

# ---------- submit ONE pipeline job ----------
job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)
# No clip uploads at all — that is the point.
stage_uploads(assets, [Fake(SA / "promo.mp4"), Fake(SA / "cta_video_1.mp4")],
              Fake(SA / "backgrounds.zip"), None, None, None)
df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")

store.create_job(
    kind=store.KIND_PIPELINE,
    params={
        "render_config": asdict(RenderConfig(crf=30, preset="veryfast")),
        "workers": 4, "batches": N_BATCHES, "folders": N_BATCHES, "make_zip": False,
        "clip_source": "scrape_job", "clips_from_job": scrape_id,
        "clip_strategy": "top_views", "slots": 3, "clips_per_slot": 2,
    },
    label="full-run-test", job_id=job_id,
)
print("submitted pipeline job", job_id)

worker.main(["--once"])

job = store.get_job(job_id)
res = job["result"] or {}
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")

# ---------- the clips stage ran and picked the best ----------
clips_info = res.get("clips") or {}
print("\nclips stage:", {k: clips_info.get(k) for k in
                         ("clips_available", "clips_used", "clip_strategy", "per_slot")})
assert clips_info["clips_available"] == 12, clips_info
assert clips_info["clips_used"] == 6, clips_info          # 3 slots x 2 per slot
assert sum(clips_info["per_slot"].values()) == 6

# The staged assets are deleted once a job finishes, so the evidence that the
# clips stage worked is its report plus the fact that rendering succeeded with
# no clip upload anywhere in the job.
assert not assets.exists(), "assets should be cleaned up after the job"
assert clips_info["clips_copied"] == 6, clips_info
assert sorted(clips_info["per_slot"]) == ["1", "2", "3"], clips_info
print("clips were materialised into cta_slot_1..3 with no upload involved")

# top_views must have chosen the strongest clips — checked directly, since the
# selection is pure and does not depend on the job having run.
avail = [{"video_id": f"70000000000000{i:02d}",
          "path": str(clips_dir / f"70000000000000{i:02d}.mp4"),
          "views": (12 - i) * 1000} for i in range(12)]
sel = batching.select_clips(avail, slots=3, per_slot=2, strategy="top_views",
                            seed=f"sel-{job_id}")
picked = [c["video_id"] for v in sel.values() for c in v]
assert len(picked) == len(set(picked)) == 6, picked
assert set(picked) == {f"70000000000000{i:02d}" for i in range(6)}, sorted(picked)
print("took the 6 highest-view clips, each used exactly once")

# ---------- rendering happened ----------
print("\nrender:", {k: res.get(k) for k in ("total", "rendered", "failed", "batches")})
assert res["total"] == len(df) * N_BATCHES, res
assert res["rendered"] >= 1

videos = Path(res["videos_dir"])
mp4s = list(videos.rglob("*.mp4"))
assert len(mp4s) == res["rendered"], (len(mp4s), res["rendered"])
print(f"{len(mp4s)} videos rendered across {N_BATCHES} source batches")

# ---------- names came from captions ----------
from captions import naming
items_r = store.list_items(job_id, stage=store.STAGE_RENDER)
shorts = [(i.get("meta") or {}).get("short_name") for i in items_r
          if i["render_status"] == store.ITEM_DONE]
assert all(s and len(s[:-4]) <= naming.MAX_STEM and s.count("#") == 1 for s in shorts), shorts
assert len(set(shorts)) == len(shorts)
print("filenames from captions, <=90 chars, one hashtag, unique:")
print("   ", shorts[0])

# ---------- the two stages did not collide ----------
scrape_items = store.list_items(job_id, stage=store.STAGE_SCRAPE)
assert scrape_items == [], "pipeline wrote scrape items into a scrape_job run"
assert store.item_counts(job_id, stage=store.STAGE_RENDER)["total"] == len(df) * N_BATCHES
print("stages isolated: render items only, counts unpolluted")

# ---------- guard: too few clips must stop before rendering ----------
job2 = store.new_job_id()
store.make_job_dirs(job2)
a2 = store.assets_dir(job2)
stage_uploads(a2, [Fake(SA / "promo.mp4")], Fake(SA / "backgrounds.zip"), None, None, None)
df.to_excel(a2 / "input.xlsx", index=False, engine="openpyxl")
store.create_job(
    kind=store.KIND_PIPELINE,
    params={"render_config": asdict(RenderConfig(crf=30, preset="veryfast")),
            "workers": 2, "batches": 1, "folders": 1, "make_zip": False,
            "clip_source": "scrape_job", "clips_from_job": "does-not-exist",
            "slots": 5, "clips_per_slot": 10},
    label="thin-pool-test", job_id=job2)
worker.main(["--once"])
j2 = store.get_job(job2)
assert j2["status"] == store.STATUS_FAILED, j2["status"]
assert "No clips available" in (j2.get("error") or ""), j2.get("error")
assert not list((store.videos_dir(job2)).rglob("*.mp4")), "rendered despite no clips"
print(f"\nthin/missing clip pool stops before rendering: {j2['error'][:70]}…")

print("\nALL PIPELINE E2E TESTS PASSED")
