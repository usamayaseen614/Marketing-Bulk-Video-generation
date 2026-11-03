"""No publishing captions: real naming, manifests, resume and pipeline render."""
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="caption_none_")

import pandas as pd

import batching
from jobs import store, worker
from jobs.runners import render
from integrations import mailer
from video_generator import RenderConfig
from workspace import stage_uploads

store.init_db()
pool_id = store.save_pool("Exhausted", ["Unused caption"], ["#unused"])
store.take_captions(pool_id, 1)
params = {"caption_mode": "none", "hashtag_source": "excel", "fixed_tail": True,
          "generate_pool": True, "force_new_pool": True}
df = pd.DataFrame({
    "Headline": ["Repeated", "Repeated", "", "x" * 120],
    "Caption": ["Ignore this caption"] * 4,
    "Hashtags": ["#ignore"] * 4,
})
slots = [batching.Slot(batch=b, row=r) for b in (1, 2) for r in range(1, 5)]
job_id = store.create_job(kind=store.KIND_RENDER, params=params,
                          items=[{"idx": i} for i in range(1, 9)])
store.make_job_dirs(job_id)
overrides = {"Headline": {"1": ["Promo headline", "", "", ""]}}
promo_for = {s: s.batch - 1 for s in slots}
info = render._assign_names(job_id, slots, 4, df, params,
                            overrides=overrides, promo_for=promo_for)
assert info["pool_id"] is None and info["from_pool"] == info["from_sheet"] == 0
assert info["hashtag_source"] == "none"
assert store.active_pool()["cursor"] == 1
items = store.list_items(job_id)
names = [item["meta"]["short_name"] for item in items]
assert names[0] == "Repeated.mp4" and names[1] == "Repeated (2).mp4", names
assert names[2] == "video.mp4" and names[4] == "Promo headline.mp4", names
assert len(set(names)) == 8 and all(len(n[:-4]) <= 90 for n in names)
for item in items:
    meta = item["meta"]
    assert meta["caption"] == meta["hashtags"] == ""
    assert meta["short_name"] == meta["long_name"]
    store.update_item(job_id, item["idx"], render_status=store.ITEM_DONE)
manifest_path = render._write_manifests(job_id, 4, 1, {s: 1 for s in slots})
manifest = pd.read_excel(manifest_path, keep_default_na=False)
assert (manifest["Caption"] == "").all() and (manifest["Hashtags"] == "").all()
assert render._assign_names(job_id, slots, 4, df, params)["applied"] == 0
assert [i["meta"]["short_name"] for i in store.list_items(job_id)] == names
assert store.active_pool()["cursor"] == 1

# A real pipeline must render and pack even with stale generation settings.
class Upload:
    def __init__(self, path):
        self.name = path.name
        self.data = path.read_bytes()
        self.size = len(self.data)

    def getvalue(self):
        return self.data


assets_source = ROOT / "sample_assets"
pipeline_id = store.new_job_id()
store.make_job_dirs(pipeline_id)
assets = store.assets_dir(pipeline_id)
stage_uploads(assets, Upload(assets_source / "promo.mp4"),
              Upload(assets_source / "backgrounds.zip"), None, None,
              [[Upload(assets_source / "cta_video_1.mp4")]])
sheet = pd.read_excel(assets_source / "sample_5_videos.xlsx").head(1)
sheet["Caption"] = "Ignore uploaded caption"
sheet["Hashtags"] = "#ignore"
sheet.to_excel(assets / "input.xlsx", index=False)
store.create_job(kind=store.KIND_PIPELINE, job_id=pipeline_id, params={
    **params, "render_config": asdict(RenderConfig(crf=30, preset="veryfast")),
    "batches": 2, "workers": 2, "make_zip": True,
})
# The naming-only fixture is not a queued render job.
store.finish_job(job_id, store.STATUS_SUCCEEDED)
worker.main(["--once"])
job = store.get_job(pipeline_id)
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
result = job["result"]
assert "caption_pool" not in result, result.get("caption_pool")
manifest = pd.read_excel(result["sheet_path"], keep_default_na=False)
assert len(manifest) == 2 and manifest["Short_Filename"].nunique() == 2
assert (manifest["Caption"] == "").all() and (manifest["Hashtags"] == "").all()
with ZipFile(result["zip_path"]) as archive:
    videos = [Path(name).name for name in archive.namelist() if name.endswith(".mp4")]
    assert sorted(videos) == sorted(manifest["Short_Filename"]), videos
    assert archive.testzip() is None
assert store.active_pool()["cursor"] == 1
body = mailer._render_body(job, store.STATUS_SUCCEEDED, {
    **result, "upload_mode": "files", "uploaded": 2,
})
assert "same Headline-based name without captions or hashtags" in body
assert "once with a single hashtag" not in body
print("CAPTION NONE TESTS PASSED")
