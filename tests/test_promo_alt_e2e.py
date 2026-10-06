"""End-to-end Promo Alternate through the real worker: a job with NO promo
video at all, staged, queued, rendered by jobs/worker.py.

The unit test beside this one (test_promo_alt_layer.py) drives VideoGenerator
directly. This one covers what that cannot: that a job carrying zero promos
survives store -> workspace_from_dir -> assign_promos -> _render_batches ->
RenderConfig(**params) without any of the promo-shaped assumptions on that path
raising or quietly rendering the wrong thing.
"""
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="palte2e_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from jobs import store, worker
from video_generator import RenderConfig, find_ffmpeg
from workspace import stage_uploads, workspace_from_dir

SA = PROJ / "sample_assets"
FF = find_ffmpeg()
N_BATCHES = 2
TARGET = 6.0


class Fake:
    def __init__(self, p, name=None):
        self.name = name or Path(p).name
        self._d = Path(p).read_bytes()
        self.size = len(self._d)

    def getvalue(self):
        return self._d


def duration(path: Path) -> float:
    proc = subprocess.run([FF, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    assert m, proc.stderr[-800:]
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


store.init_db()
job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)

# NO promo videos — the whole point. The alternate pool stands in for them.
alt_pool = [Fake(SA / "cta_video_1.mp4", "alt_a.mp4"),
            Fake(SA / "cta_video_2.mp4", "alt_b.mp4"),
            Fake(SA / "cta_video_3.mp4", "alt_c.mp4")]
stage_uploads(assets, [], Fake(SA / "backgrounds.zip"), None, None,
              [[Fake(SA / "cta_video_5.mp4")]], None, None, None, alt_pool)

assert not (assets / "input.mp4").exists(), "a promo was staged for a job with none"
ws = workspace_from_dir(assets, assets.parent / "work")
assert ws.video_paths == [], ws.video_paths
assert len(ws.promo_alt_paths) == 3, ws.promo_alt_paths
# promo_for_batch still answers, because the render path calls it unconditionally.
assert ws.promo_for_batch(0) == ws.video_path
assert ws.video_path in ws.promo_alt_paths
print(f"staging: 0 promos, {len(ws.promo_alt_paths)} alternate clips, "
      f"video_path -> {ws.video_path.name}")

df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
N_ROWS = len(df)

cfg = RenderConfig(crf=30, preset="veryfast", promo_alt=True,
                   promo_alt_seconds=TARGET)
store.create_job(
    kind=store.KIND_RENDER,
    params={"render_config": asdict(cfg), "workers": 2,
            "batches": N_BATCHES, "folders": N_BATCHES, "make_zip": False},
    label="promo-alt-e2e",
    job_id=job_id,
)
worker.main(["--once"])

job = store.get_job(job_id)
assert job["status"] == store.STATUS_SUCCEEDED, (job["status"], job.get("error"))
videos = sorted(store.videos_dir(job_id).rglob("*.mp4"))
assert len(videos) == N_ROWS * N_BATCHES, \
    f"{len(videos)} videos, expected {N_ROWS} rows x {N_BATCHES} batches"
print(f"worker: {job['status']}, {len(videos)} videos across "
      f"{len({v.parent.name for v in videos})} folder(s)")

# Every one is the configured length — no promo could have supplied it, and a
# duration of None would have failed the row rather than producing a file.
lengths = [duration(v) for v in videos]
assert all(abs(d - TARGET) < 0.4 for d in lengths), \
    f"lengths {['%.2f' % d for d in lengths]}, expected ~{TARGET}"
assert all(v.stat().st_size > 5000 for v in videos), "a video came out empty"
print(f"lengths: all ~{TARGET}s (min {min(lengths):.2f}, max {max(lengths):.2f})")

# The passes really do differ. With no promos to cycle, variant_salt is the ONLY
# thing distinguishing one pass from another — it is what the batch count means
# in this mode — so byte-identical output across the two folders would mean the
# feature produces N copies of the same video rather than N variants.
sizes_per_batch: dict[str, list[int]] = {}
for v in videos:
    sizes_per_batch.setdefault(v.parent.name, []).append(v.stat().st_size)
assert len(sizes_per_batch) == N_BATCHES, sizes_per_batch
first, second = (sizes_per_batch[k] for k in sorted(sizes_per_batch))
assert first != second, \
    "both passes produced byte-identical videos — variant_salt did not vary the draw"
print("passes: the two batches drew different clip sequences")

# The delivered manifest must not pin every video on one clip of the pool.
# ws.video_path is only a stand-in in this mode, and each row holds its own
# shuffled sequence — naming the stand-in would make the record simply false.
manifest = pd.read_excel(store.job_dir(job_id) / "render_manifest.xlsx",
                         engine="openpyxl")
assert set(manifest["Promo"]) == {"Promo Alternate"}, set(manifest["Promo"])
assert not any(str(v).endswith(".mp4") for v in manifest["Promo"]), \
    "the manifest names a promo file this job never rendered"
print(f"manifest: {len(manifest)} rows, Promo column reads 'Promo Alternate'")

# And the completion email says what was used rather than "0 promo video(s)".
from integrations import mailer
body = mailer._render_body(  # noqa: SLF001 — the formatter, not the transport
    {"id": job_id, "label": "promo-alt-e2e"}, "succeeded",
    {"rendered": 10, "total": 10, "failed": 0, "batches": 2,
     "rows": 5, "folders": 2, "promo_videos": 0, "promo_alt_clips": 3})
assert "0 promo video(s)" not in body, body
assert "3 Promo Alternate clip(s)" in body, body
print("email: reports the alternate pool, not '0 promo video(s)'")

print("ALL PROMO ALTERNATE E2E TESTS PASSED")
