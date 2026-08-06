"""Publishing an output folder as yt.zip + tk.zip.

The properties that matter, and what breaks if each is lost:

  * the SHORT names go in yt.zip and the LONG ones in tk.zip — get this
    backwards and every TikTok caption loses four of its five hashtags
  * an archive is only published if the bytes that landed match the bytes sent
  * the MP4s are freed only after BOTH archives are verified in Drive — free
    them a moment earlier and a failed upload has nothing left to re-pack
  * a folder that failed keeps its videos and is re-packed next run; a folder
    that succeeded is never packed or uploaded twice
"""
import os, sys, tempfile, zipfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="upzip_")
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

N_ROWS = 2
N_FOLDERS = 2
# 2 batches x 2 rows. Folder 1 takes row 1 from both batches, folder 2 row 2 —
# so each archive is a genuine mix of source batches, as a real one is.
PLACEMENT = {Slot(batch=1, row=1): 1, Slot(batch=2, row=1): 1,
             Slot(batch=1, row=2): 2, Slot(batch=2, row=2): 2}


def make_job(label: str, params: dict = None) -> str:
    job_id = store.create_job(
        kind=store.KIND_RENDER, params=params or {}, label=label,
        items=[{"idx": i} for i in range(1, 5)])
    store.make_job_dirs(job_id)
    for idx in range(1, 5):
        batch, row = batching.split_index(idx, N_ROWS)
        short = f"caption {idx} #one.mp4"
        folder = store.videos_dir(job_id) / batching.source_folder_name(batch)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / short).write_bytes(f"video-{idx}".encode() * 64)
        store.update_item(
            job_id, idx, name=short, render_status=store.ITEM_DONE,
            meta={"batch": batch, "row": row, "short_name": short,
                  "long_name": f"caption {idx} #one #two #three #four #five.mp4"})
    return job_id


def sources(job_id: str) -> list[Path]:
    out = []
    for idx in range(1, 5):
        batch, _row = batching.split_index(idx, N_ROWS)
        out.append(store.videos_dir(job_id) / batching.source_folder_name(batch)
                   / f"caption {idx} #one.mp4")
    return out


# ---- a stub Drive that records every archive it is handed -------------------
uploads: list[dict] = []
fail_on: dict = {"name": None, "exc": None}
truncate: dict = {"name": None}

drive.set_target = lambda value: None
drive.folder_link = lambda fid: f"https://drive/{fid}"
drive.resolve_target = lambda: ("0AFakeSharedDrive", "0AFakeSharedDrive")
drive.ensure_path = lambda parts, parent_id=None: (
    (parent_id + "/" if parent_id else "") + "/".join(parts))


def _upload_or_replace(path, parent_id, name, drive_id=None):
    """Stands in for the Drive write. upload_verified() — the real one — runs
    on top of this, so its size check is exercised rather than stubbed out."""
    path = Path(path)
    assert path.is_file(), f"asked to upload a file that isn't there: {path}"
    assert drive_id == "0AFakeSharedDrive", "the runner-thread drive id was lost"
    if fail_on["name"] == f"{parent_id}/{name}":
        raise fail_on["exc"] or RuntimeError("upload exploded")
    size = path.stat().st_size
    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
    uploads.append({"parent": parent_id, "name": name, "size": size,
                    "entries": entries})
    if truncate["name"] == f"{parent_id}/{name}":
        size -= 1                      # Drive says fewer bytes landed than we sent
    # The real API returns size as a string; upload_verified int()s it.
    return {"id": f"zip-{len(uploads)}", "name": name, "size": str(size),
            "webViewLink": f"https://file/{name}"}


drive.upload_or_replace = _upload_or_replace


# ---- the happy path ---------------------------------------------------------
job = make_job("zip-happy")
result = render._upload(store.get_job(job), N_ROWS, N_FOLDERS, PLACEMENT)

assert result["upload_mode"] == "zip", result
assert len(uploads) == 4, [u["name"] for u in uploads]
assert {u["name"] for u in uploads} == {"yt.zip", "tk.zip"}
parents = {u["parent"] for u in uploads}
assert parents == {"renders/" + store.get_job(job)["params"]["drive_stamp"]
                   + "/zip-happy/batch_01",
                   "renders/" + store.get_job(job)["params"]["drive_stamp"]
                   + "/zip-happy/batch_02"}, parents

by_key = {(u["parent"].rsplit("/", 1)[-1], u["name"]): u for u in uploads}
# Folder 1 holds items 1 and 3 (row 1 of each batch); folder 2 holds 2 and 4.
assert by_key[("batch_01", "yt.zip")]["entries"] == [
    "caption 1 #one.mp4", "caption 3 #one.mp4"]
assert by_key[("batch_01", "tk.zip")]["entries"] == [
    "caption 1 #one #two #three #four #five.mp4",
    "caption 3 #one #two #three #four #five.mp4"]
assert by_key[("batch_02", "yt.zip")]["entries"] == [
    "caption 2 #one.mp4", "caption 4 #one.mp4"]
print("happy path: short names in yt.zip, long names in tk.zip, mixed folders")

items = {i["idx"]: i for i in store.list_items(job)}
assert all(i["upload_status"] == store.ITEM_DONE for i in items.values())
assert all(i["drive_link"] == "https://file/tk.zip" for i in items.values())
assert result["uploaded"] == 4 and result["drive_zips"] == 4
assert result["zip_bytes"] == sum(u["size"] for u in uploads)
print("happy path: every item marked uploaded and pointed at its archive")

assert all(not src.exists() for src in sources(job)), "MP4s were not freed"
assert not (store.job_dir(job) / "packing").exists(), "packing scratch left behind"
assert result["videos_freed"] and result["freed_bytes"] > 0
print("happy path: MP4s and scratch freed once both archives were verified")

# ---- resume: a finished folder is never re-packed ---------------------------
before = len(uploads)
render._upload(store.get_job(job), N_ROWS, N_FOLDERS, PLACEMENT)
assert len(uploads) == before, "a published folder was uploaded again"
assert all(i["upload_status"] == store.ITEM_DONE
           for i in store.list_items(job)), "resume unmarked finished items"
print("resume: recorded folders skipped entirely — no re-pack, no re-upload")


# ---- a failing upload keeps that folder's videos ----------------------------
job2 = make_job("zip-fails")
stamp2 = None
uploads.clear()
fail_on["name"] = None

# Learn the stamp by letting the first folder's yt.zip through, then blow up on
# folder 1's tk.zip specifically.
probe = store.get_job(job2)
render._drive_root_stamp(job2, probe["params"])
stamp2 = store.get_job(job2)["params"]["drive_stamp"]
fail_on["name"] = f"renders/{stamp2}/zip-fails/batch_01/tk.zip"
fail_on["exc"] = RuntimeError("connection reset uploading tk.zip")

result2 = render._upload(store.get_job(job2), N_ROWS, N_FOLDERS, PLACEMENT)
items2 = {i["idx"]: i for i in store.list_items(job2)}
assert items2[1]["upload_status"] == store.ITEM_FAILED
assert items2[3]["upload_status"] == store.ITEM_FAILED
assert "connection reset" in (items2[1]["upload_error"] or "")
assert items2[2]["upload_status"] == store.ITEM_DONE
assert items2[4]["upload_status"] == store.ITEM_DONE
assert result2["upload_failures"] and "batch_01" in result2["upload_failures"][0]

kept = sources(job2)
assert kept[0].is_file() and kept[2].is_file(), \
    "the failed folder's MP4s were deleted — nothing left to re-pack from"
assert not kept[1].exists() and not kept[3].exists(), \
    "the folder that succeeded should have been freed"
assert not (store.job_dir(job2) / "packing" / "batch_01").exists(), \
    "a failed folder left its half-built archives on disk"
print("upload failure: that folder's videos kept, the other folder still freed")

# ...and the retry finishes it without touching the folder that already landed.
fail_on["name"] = None
uploads.clear()
render._upload(store.get_job(job2), N_ROWS, N_FOLDERS, PLACEMENT)
assert {u["name"] for u in uploads} == {"yt.zip", "tk.zip"}
assert {u["parent"].rsplit("/", 1)[-1] for u in uploads} == {"batch_01"}, \
    "the retry re-uploaded the folder that was already published"
assert all(i["upload_status"] == store.ITEM_DONE for i in store.list_items(job2))
assert all(not src.exists() for src in sources(job2))
print("retry: only the failed folder is re-packed, and then it is freed too")


# ---- a truncated upload is refused ------------------------------------------
job3 = make_job("zip-truncated")
uploads.clear()
render._drive_root_stamp(job3, store.get_job(job3)["params"])
stamp3 = store.get_job(job3)["params"]["drive_stamp"]
truncate["name"] = f"renders/{stamp3}/zip-truncated/batch_02/yt.zip"

result3 = render._upload(store.get_job(job3), N_ROWS, N_FOLDERS, PLACEMENT)
items3 = {i["idx"]: i for i in store.list_items(job3)}
assert items3[2]["upload_status"] == store.ITEM_FAILED, \
    "an archive Drive stored short was accepted"
assert "truncated" in (items3[2]["upload_error"] or "").lower()
assert sources(job3)[1].is_file(), "videos freed despite a truncated upload"
assert items3[1]["upload_status"] == store.ITEM_DONE
print("truncated upload: refused, that folder failed, its videos kept")


# ---- keeping the MP4s is a supported choice ---------------------------------
truncate["name"] = None
job4 = make_job("zip-keep", params={"free_local_videos": False})
uploads.clear()
result4 = render._upload(store.get_job(job4), N_ROWS, N_FOLDERS, PLACEMENT)
assert len(uploads) == 4 and not result4["videos_freed"]
assert all(src.is_file() for src in sources(job4)), \
    "free_local_videos=False still deleted the videos"
print("free_local_videos=False: archives published, MP4s left on the VM")


# ---- a video missing from disk is reported, not swept into the archive ------
job6 = make_job("zip-missing")
uploads.clear()
sources(job6)[0].unlink()          # item 1, which belongs to folder 1

result6 = render._upload(store.get_job(job6), N_ROWS, N_FOLDERS, PLACEMENT)
items6 = {i["idx"]: i for i in store.list_items(job6)}
assert items6[1]["upload_status"] == store.ITEM_FAILED, \
    "a video that never made it into the archive was marked uploaded"
assert "missing" in (items6[1]["upload_error"] or "").lower()
assert items6[3]["upload_status"] == store.ITEM_DONE, \
    "the rest of the folder should still publish"
by_key6 = {(u["parent"].rsplit("/", 1)[-1], u["name"]): u for u in uploads}
assert by_key6[("batch_01", "yt.zip")]["entries"] == ["caption 3 #one.mp4"]
assert any("missing" in line for line in result6["upload_failures"])
print("missing MP4: excluded from the archive AND reported as not uploaded")


# ---- publishing one platform at a time, a day apart -------------------------
# Drive allows one account 750 GB per rolling 24 hours, so a big night has to
# send tk today and yt tomorrow. What must hold: today sends ONLY tk, the MP4s
# survive the night (tomorrow builds yt.zip from them), tomorrow sends ONLY yt,
# and only then are the videos freed.
job7 = make_job("zip-tk-today", params={"upload_platforms": ["tk"]})
uploads.clear()

day1 = render._upload(store.get_job(job7), N_ROWS, N_FOLDERS, PLACEMENT)
assert {u["name"] for u in uploads} == {"tk.zip"}, [u["name"] for u in uploads]
assert len(uploads) == 2, "one tk.zip per output folder, and nothing else"
assert day1["upload_platforms"] == ["tk"]
assert all(src.is_file() for src in sources(job7)), \
    "the MP4s were deleted with yt.zip still unpublished — tomorrow has nothing to pack"
assert not day1["videos_freed"] and day1["videos_kept"] == 4
assert day1["platforms_pending"] == ["yt"], day1
assert all(i["upload_status"] == store.ITEM_DONE for i in store.list_items(job7))
print("day 1 (tk only): tk.zip published, MP4s deliberately kept for day 2")

# Tomorrow: the same job, requeued asking for the other half.
store.merge_job_params(job7, upload_platforms=["yt"])
uploads.clear()
day2 = render._upload(store.get_job(job7), N_ROWS, N_FOLDERS, PLACEMENT)
assert {u["name"] for u in uploads} == {"yt.zip"}, [u["name"] for u in uploads]
assert len(uploads) == 2, "tk.zip was re-sent — that is 500 GB of wasted quota"
by_key7 = {(u["parent"].rsplit("/", 1)[-1], u["name"]): u for u in uploads}
assert by_key7[("batch_01", "yt.zip")]["entries"] == [
    "caption 1 #one.mp4", "caption 3 #one.mp4"], "yt.zip has the wrong names"
assert day2["videos_freed"] and not day2["platforms_pending"]
assert all(not src.exists() for src in sources(job7)), \
    "both platforms are published — the MP4s should finally be freed"
print("day 2 (yt only): only yt.zip sent, tk.zip untouched, MP4s then freed")

# A third run has nothing left to do at all.
uploads.clear()
store.merge_job_params(job7, upload_platforms=["yt", "tk"])
render._upload(store.get_job(job7), N_ROWS, N_FOLDERS, PLACEMENT)
assert not uploads, "a fully published job re-sent something"
print("day 3 (both): everything already recorded — nothing re-sent")

# An unrecognised platform must not silently mean "publish everything".
assert render._selected_platforms({"upload_platforms": ["tok"]}) == ("yt", "tk")
assert render._selected_platforms({"upload_platforms": "tk"}) == ("tk",)
assert render._selected_platforms({"upload_platforms": ["TK", "yt"]}) == ("yt", "tk"), \
    "platforms must come back in publishing order, not the order given"
assert render._selected_platforms({}) == ("yt", "tk")
print("platform parsing: case-insensitive, ordered, and safe when nonsense")


# ---- upload_mode=files still routes to the per-video path -------------------
calls = []
drive.upload_file = lambda path, parent, name=None: (
    calls.append(("upload", parent, name)) or
    {"id": f"f{len(calls)}", "name": name, "webViewLink": "https://f"})
drive.copy_file = lambda fid, name, parent=None: (
    calls.append(("copy", parent, name)) or
    {"id": f"c{len(calls)}", "name": name, "webViewLink": "https://c"})
drive.find_file = lambda name, parent, drive_id=None: None

job5 = make_job("files-mode", params={"upload_mode": "files"})
uploads.clear()
result5 = render._upload(store.get_job(job5), N_ROWS, N_FOLDERS, PLACEMENT)
assert not uploads, "files mode published an archive"
assert result5["upload_mode"] == "files"
assert len([c for c in calls if c[0] == "upload"]) == 4
assert len([c for c in calls if c[0] == "copy"]) == 4
assert all(src.is_file() for src in sources(job5)), \
    "files mode must not delete the videos"
print("upload_mode=files: unchanged per-video path, nothing zipped or freed")

# One platform in files mode uploads that platform's OWN name and makes no
# copy — the copy is what doubles the bytes, so skipping it is the point.
calls.clear()
job8 = make_job("files-tk-only",
                params={"upload_mode": "files", "upload_platforms": ["tk"]})
result8 = render._upload(store.get_job(job8), N_ROWS, N_FOLDERS, PLACEMENT)
assert not [c for c in calls if c[0] == "copy"], \
    "a copy was made for a single platform — that is the quota being wasted"
uploaded8 = [c for c in calls if c[0] == "upload"]
assert len(uploaded8) == 4
assert all(name.endswith("#one #two #three #four #five.mp4")
           for _kind, _parent, name in uploaded8), \
    "tk-only must upload the LONG name, not the short one"
assert all(parent.endswith("/tk") for _kind, parent, _name in uploaded8)
assert result8["drive_files"] == 4, result8      # not doubled
assert all(i["drive_link"] == "https://f" for i in store.list_items(job8)), \
    "with no copy, the item must link to the uploaded file"
print("files mode, tk only: long name uploaded straight to tk/, no copy made")

print("\nALL ZIP UPLOAD TESTS PASSED")
