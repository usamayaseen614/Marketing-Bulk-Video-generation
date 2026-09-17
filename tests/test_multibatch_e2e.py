"""End-to-end multi-batch render: several promo videos, one sheet rendered
N times, different clips each pass, mixed across folders, two names per video."""
import os, sys, tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="mb_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import batching
from batching import Slot
from jobs import store, worker
from workspace import stage_uploads, workspace_from_dir
from video_generator import RenderConfig

SA = PROJ / "sample_assets"
N_BATCHES = 3


class Fake:
    def __init__(self, p, name=None):
        self.name = name or Path(p).name
        self._d = Path(p).read_bytes()
        self.size = len(self._d)
    def getvalue(self): return self._d


store.init_db()

# A caption pool so filenames come from captions + hashtags.
captions = [f"Caption number {i} that reads like a real post" for i in range(60)]
hashtags = [f"#tag{i} #asmr #fyp #glowup" for i in range(20)]
store.save_pool("test theme", captions, hashtags, model="stub")

job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)

# THREE different promo videos — one per batch.
promos = [Fake(SA / "promo.mp4", "promo_a.mp4"),
          Fake(SA / "cta_video_1.mp4", "promo_b.mp4"),
          Fake(SA / "cta_video_5.mp4", "promo_c.mp4")]
stage_uploads(assets, promos, Fake(SA / "backgrounds.zip"), None, None,
              [[Fake(SA / "cta_video_1.mp4")], [Fake(SA / "cta_video_2.mp4")],
               [Fake(SA / "cta_video_3.mp4")]])

df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
N_ROWS = len(df)
print(f"sheet: {N_ROWS} rows | batches: {N_BATCHES} | promos: {len(promos)}")

# staged promos are discoverable
ws = workspace_from_dir(assets, assets.parent / "work")
assert len(ws.video_paths) == 3, ws.video_paths
assert [p.name for p in ws.video_paths] == ["input.mp4", "input_2.mp4", "input_3.mp4"]
assert ws.promo_for_batch(0) != ws.promo_for_batch(1)
assert ws.promo_for_batch(3) == ws.promo_for_batch(0), "should cycle"
print("promo staging:", [p.name for p in ws.video_paths], "(cycles past the end)")

store.create_job(
    kind=store.KIND_RENDER,
    params={"render_config": asdict(RenderConfig(crf=30, preset="veryfast")),
            "workers": 4, "batches": N_BATCHES, "folders": N_BATCHES,
            "make_zip": False},
    label="multibatch-test",
    job_id=job_id,
)
worker.main(["--once"])

job = store.get_job(job_id)
res = job["result"]
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
print("\nresult:", {k: res.get(k) for k in
                    ("total", "rendered", "failed", "batches", "folders",
                     "rows", "promo_videos")})

expected = N_ROWS * N_BATCHES
assert res["total"] == expected, f"{res['total']} != {expected}"
assert res["promo_videos"] == 3
assert res["rendered"] >= expected - N_BATCHES  # one row has a bad background

# ---------- each batch rendered into its own folder ----------
videos = Path(res["videos_dir"])
for b in range(1, N_BATCHES + 1):
    folder = videos / batching.source_folder_name(b)
    assert folder.is_dir(), folder
    files = list(folder.glob("*.mp4"))
    assert files, f"batch {b} produced nothing"
    print(f"  {folder.name}: {len(files)} videos")

# ---------- THE KEY CLAIM: same row differs between batches ----------
items = {i["idx"]: i for i in store.list_items(job_id)}
sizes_by_row = {}
for idx, item in items.items():
    if item["render_status"] != store.ITEM_DONE:
        continue
    b, r = batching.split_index(idx, N_ROWS)
    p = videos / batching.source_folder_name(b) / item["name"]
    sizes_by_row.setdefault(r, {})[b] = p.stat().st_size

differing = 0
for row, per_batch in sorted(sizes_by_row.items()):
    if len(per_batch) > 1 and len(set(per_batch.values())) > 1:
        differing += 1
print(f"\nrows whose output differs across batches: {differing}/{len(sizes_by_row)}")
assert differing >= 1, ("every batch produced byte-identical output — "
                        "variant_salt / promo rotation is not working")

# ---------- both filenames, and the 90-char name rule ----------
from captions import naming
shorts, longs = [], []
for item in items.values():
    meta = item.get("meta") or {}
    if item["render_status"] != store.ITEM_DONE:
        continue
    s, l = meta.get("short_name"), meta.get("long_name")
    assert s and l, meta
    assert len(s[:-4]) <= naming.MAX_STEM, f"{len(s[:-4])} > 90: {s}"
    assert s.count("#") == 1, s
    assert l.count("#") >= 1, l
    assert item["name"] == s, (item["name"], s)
    shorts.append(s); longs.append(l)

assert len(set(shorts)) == len(shorts), "duplicate short names within a render"
assert len(set(longs)) == len(longs), "duplicate long names within a render"
print(f"\n{len(shorts)} videos, all names unique, all names <= {naming.MAX_STEM} chars")
for s, l in list(zip(shorts, longs))[:3]:
    print(f"  [{len(s):3d}] {s}")
    print(f"        {l}")

# ---------- the mix ----------
placement = batching.mix_into_folders(
    batching.plan_render(N_BATCHES, N_ROWS), N_BATCHES)
spread = batching.summarize(placement, N_BATCHES)
print("\nmix (folder -> source batches):")
for f in sorted(spread):
    print(f"  {batching.folder_name(f)}: {dict(sorted(spread[f]['from_batch'].items()))}")
for f in spread:
    assert len(spread[f]["from_batch"]) > 1 or N_BATCHES == 1, \
        f"folder {f} came from a single batch"

# ---------- manifests ----------
manifest = Path(res["sheet_path"])
assert manifest.is_file(), manifest
mdf = pd.read_excel(manifest, engine="openpyxl")
for col in ("Folder", "Source_Batch", "Sheet_Row", "Caption", "Hashtags",
            "Short_Filename", "Long_Filename"):
    assert col in mdf.columns, list(mdf.columns)
assert len(mdf) == len(shorts), (len(mdf), len(shorts))
print(f"\nrender_manifest.xlsx: {len(mdf)} rows, columns {list(mdf.columns)[:6]}...")

per_folder = sorted(store.job_dir(job_id).glob("batch_*_manifest.xlsx"))
assert len(per_folder) == N_BATCHES, per_folder
print(f"per-folder manifests: {[p.name for p in per_folder]}")

# ---------- resume ----------
# Model a genuine crash-resume: the worker was killed mid-job, so cleanup never
# ran and the staged assets are still there. (After a SUCCESSFUL job the assets
# are deliberately deleted, and _load_dataframe reports that clearly.)
victim = next(i for i in items.values() if i["render_status"] == store.ITEM_DONE)
b, r = batching.split_index(victim["idx"], N_ROWS)
(videos / batching.source_folder_name(b) / victim["name"]).unlink()
store.update_item(job_id, victim["idx"], render_status=store.ITEM_PENDING)

stage_uploads(assets, promos, Fake(SA / "backgrounds.zip"), None, None,
              [[Fake(SA / "cta_video_1.mp4")], [Fake(SA / "cta_video_2.mp4")],
               [Fake(SA / "cta_video_3.mp4")]])
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
with store._conn() as c:
    c.execute("UPDATE jobs SET status=? WHERE id=?", (store.STATUS_QUEUED, job_id))

before = store.item_counts(job_id)
worker.main(["--once"])
after = store.item_counts(job_id)
assert after["rendered"] == before["rendered"] + 1, (before, after)
assert (videos / batching.source_folder_name(b) / victim["name"]).is_file()
# names must NOT have been redrawn on resume
again = {i["idx"]: (i.get("meta") or {}).get("short_name")
         for i in store.list_items(job_id)}
for idx, item in items.items():
    if (item.get("meta") or {}).get("short_name"):
        assert again[idx] == item["meta"]["short_name"], \
            f"item {idx} was renamed on resume"
print(f"\nresume: re-rendered only item {victim['idx']}, names unchanged")

print("\nALL MULTI-BATCH E2E TESTS PASSED")
