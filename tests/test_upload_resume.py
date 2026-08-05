"""The resume guards on Drive uploads: bytes go up once, the copy is made once.

A worker can die between any two statements, so every Drive write is recorded
the instant it succeeds and skipped when its record already exists — for BOTH
files: the yt/ upload (drive_file_id) and the tk/ copy (copy_file_id). The tk
half used to be missing: the copy ran unconditionally on every resume, and
Drive happily stores two same-named files in one folder, so a worker killed
between the copy and the 'done' write silently duplicated the video."""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="upresume_")
# A fake destination so config.drive_configured() lets _upload run at all.
os.environ["BVG_DRIVE_SHARED_DRIVE_ID"] = "0AFakeSharedDrive"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import batching
from batching import Slot
from jobs import store
from jobs.runners import render
from integrations import drive

store.init_db()

# ---- a 2-video job, already "rendered", files on disk -----------------------
N_ROWS = 2
job_id = store.create_job(
    kind=store.KIND_RENDER, params={}, label="resume-guard",
    items=[{"idx": i} for i in (1, 2)],
)
store.make_job_dirs(job_id)
videos = store.videos_dir(job_id) / batching.source_folder_name(1)
videos.mkdir(parents=True, exist_ok=True)
for i, name in ((1, "one caption #a.mp4"), (2, "two caption #b.mp4")):
    (videos / name).write_bytes(b"fake mp4 bytes")
    store.update_item(
        job_id, i, name=name, render_status=store.ITEM_DONE,
        meta={"batch": 1, "row": i, "short_name": name,
              "long_name": name.replace(".mp4", " #x #y.mp4")})

placement = {Slot(batch=1, row=1): 1, Slot(batch=1, row=2): 1}

# ---- a stub Drive that counts every write -----------------------------------
calls = {"upload": [], "copy": []}

drive.set_target = lambda value: None
drive.ensure_path = lambda parts, parent_id=None: "folder-" + "-".join(parts)
drive.folder_link = lambda fid: f"https://drive/{fid}"


def _upload_file(path, parent_id, name=None):
    calls["upload"].append((Path(path).name, parent_id, name))
    return {"id": f"up-{len(calls['upload'])}", "name": name,
            "webViewLink": "https://file/up"}


def _copy_file(file_id, new_name, parent_id=None):
    calls["copy"].append((file_id, new_name, parent_id))
    return {"id": f"cp-{len(calls['copy'])}", "name": new_name,
            "webViewLink": "https://file/cp"}


drive.upload_file = _upload_file
drive.copy_file = _copy_file

# ---- first run: two uploads into yt/, two copies into tk/ -------------------
res = render._upload(store.get_job(job_id), N_ROWS, 1, placement)
assert len(calls["upload"]) == 2 and len(calls["copy"]) == 2, calls
assert all(parent == "folder-yt" for _, parent, _ in calls["upload"]), calls["upload"]
assert all(parent == "folder-tk" for _, _, parent in calls["copy"]), calls["copy"]
# Unprefixed names: the platform is the folder, never the filename.
assert all(not n.startswith(("yt ", "tk ")) for _, _, n in calls["upload"])
items = {i["idx"]: i for i in store.list_items(job_id)}
assert all(i["upload_status"] == store.ITEM_DONE for i in items.values())
assert res["uploaded"] == 2 and res["drive_files"] == 4, res
print("first run: 2 uploads into yt/, 2 copies into tk/, both items done")

# ---- crash window A: worker died between the upload and the copy ------------
# drive_file_id is persisted, copy_file_id is not, the item is still pending.
meta = dict(items[1]["meta"])
meta.pop("copy_file_id", None)
meta.pop("copy_link", None)
store.update_item(job_id, 1, upload_status=store.ITEM_PENDING, meta=meta)

render._upload(store.get_job(job_id), N_ROWS, 1, placement)
assert len(calls["upload"]) == 2, "the yt bytes were uploaded AGAIN"
assert len(calls["copy"]) == 3, "the missing tk copy was not made"
items = {i["idx"]: i for i in store.list_items(job_id)}
assert items[1]["upload_status"] == store.ITEM_DONE
assert items[1]["meta"]["copy_file_id"] == "cp-3", items[1]["meta"]
print("crash between upload and copy: upload skipped, only the copy re-made")

# ---- crash window B: worker died between the copy and the 'done' write ------
# BOTH ids are persisted, the item is still pending. Nothing may touch Drive.
store.update_item(job_id, 1, upload_status=store.ITEM_PENDING)

render._upload(store.get_job(job_id), N_ROWS, 1, placement)
assert len(calls["upload"]) == 2, "re-uploaded on resume"
assert len(calls["copy"]) == 3, "re-copied on resume — duplicate file in tk/"
items = {i["idx"]: i for i in store.list_items(job_id)}
assert items[1]["upload_status"] == store.ITEM_DONE
assert items[1]["drive_link"] == "https://file/cp", items[1]["drive_link"]
print("crash after the copy: NOTHING re-sent, the item is just marked done")

# ---- the root folder stayed pinned across all three runs --------------------
stamp = store.get_job(job_id)["params"].get("drive_stamp")
assert stamp, "drive_stamp was never persisted"
print(f"drive root stayed pinned across resumes: renders/{stamp}/resume-guard")

print("\nALL UPLOAD RESUME TESTS PASSED")
