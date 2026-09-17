"""Publishing a job that died mid-render, without re-rendering it.

The scenario: 4 videos were asked for, 3 rendered, then the disk filled and
the worker's cleanup took `assets/` on the way out — so the job can never be
requeued, but its MP4s are still there. What must hold:

  * the tool reconstructs the SAME output-folder placement the render used,
    from the job's params alone — get this wrong and videos land in the wrong
    archive under a name drawn from a different row
  * the item that never rendered is simply absent; it must not fail the run
  * re-running publishes nothing a second time
"""
import os, sys, tempfile, zipfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="uprend_")
os.environ["BVG_DRIVE_SHARED_DRIVE_ID"] = "0AFakeSharedDrive"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import batching
from jobs import store
from integrations import drive

store.init_db()

N_ROWS, N_BATCHES, N_FOLDERS = 2, 2, 2
RENDERED = (1, 2, 3)          # idx 4 never got its turn — the disk filled first

# ---- a stub Drive that records every archive it is handed -------------------
uploads: list[dict] = []
drive.set_target = lambda value: None
drive.begin_run = lambda: None
drive.identities_spent = lambda: False
drive.folder_link = lambda fid: f"https://drive/{fid}"
drive.resolve_target = lambda: ("0AFakeSharedDrive", "0AFakeSharedDrive")
drive.ensure_path = lambda parts, parent_id=None: (
    (parent_id or "root") + "/" + "/".join(parts))
drive.upload_or_replace = lambda path, folder_id, name, **kw: {"id": "x"}


def _upload_verified(path, folder_id, name, drive_id=None):
    uploads.append({"folder": folder_id, "name": name,
                    "names": sorted(zipfile.ZipFile(path).namelist())})
    return {"id": f"id-{len(uploads)}", "size": str(path.stat().st_size),
            "webViewLink": f"https://drive/file/{len(uploads)}"}


drive.upload_verified = _upload_verified

from tools import upload_rendered  # noqa: E402  — after the stubs are in place

job_id = store.create_job(
    kind=store.KIND_RENDER, label="died at 3 of 4",
    params={"batches": N_BATCHES, "folders": N_FOLDERS,
            "upload_mode": "zip", "free_local_videos": True},
    items=[{"idx": i} for i in range(1, 5)])
store.make_job_dirs(job_id)
for idx in RENDERED:
    batch, row = batching.split_index(idx, N_ROWS)
    short = f"caption {idx} #one.mp4"
    folder = store.videos_dir(job_id) / batching.source_folder_name(batch)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / short).write_bytes(f"video-{idx}".encode() * 64)
    store.update_item(job_id, idx, name=short, render_status=store.ITEM_DONE,
                      meta={"batch": batch, "row": row, "short_name": short,
                            "long_name": f"caption {idx} #one #two #three.mp4"})
# The state a dead render leaves behind: the job is failed and assets/ is gone.
store.finish_job(job_id, store.STATUS_FAILED, error="No space left on device")
store.cleanup_job_dir(job_id, keep_outputs=True)
assert not store.assets_dir(job_id).exists(), "the scenario needs assets/ deleted"

assert upload_rendered.main([job_id]) == 0, "publishing 3 of 4 should succeed"

# The placement the tool rebuilt must be the one the render would have used.
expected = batching.mix_into_folders(
    batching.plan_render(N_BATCHES, N_ROWS), N_FOLDERS)
for idx in RENDERED:
    batch, row = batching.split_index(idx, N_ROWS)
    want = batching.folder_name(expected[batching.Slot(batch=batch, row=row)])
    got = [u for u in uploads if f"caption {idx} #one.mp4" in u["names"]]
    assert got, f"idx {idx} reached no archive"
    assert all(want in u["folder"] for u in got), \
        f"idx {idx} went to {got[0]['folder']}, expected {want}"

packed = sorted(n for u in uploads if u["name"] == "yt.zip" for n in u["names"])
assert packed == [f"caption {i} #one.mp4" for i in RENDERED], \
    f"yt.zip should hold exactly the rendered videos, got {packed}"
assert not any("caption 4" in n for u in uploads for n in u["names"]), \
    "the item that never rendered must not appear in any archive"
assert {u["name"] for u in uploads} == {"yt.zip", "tk.zip"}
assert all("#one #two #three.mp4" in n
           for u in uploads if u["name"] == "tk.zip" for n in u["names"]), \
    "tk.zip must carry the long names"

# Re-run: everything is recorded as published, so nothing goes up twice.
before = len(uploads)
assert upload_rendered.main([job_id]) == 0
assert len(uploads) == before, f"re-run re-sent {len(uploads) - before} archive(s)"

print("OK — tools/upload_rendered.py publishes a dead render's videos")
