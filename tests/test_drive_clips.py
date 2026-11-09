"""CTA clips pulled from a Google Drive folder instead of uploaded.

The upload path is slow for one reason: the clips already live in Google's
network, and a browser upload drags them down a home connection and pushes them
straight back. These tests cover the replacement — the VM fetching them itself
— and specifically the parts that are easy to get quietly wrong:

  * the listing walks sub-folders, follows shortcuts, ignores non-videos, and
    is DETERMINISTIC, because local names are assigned positionally and a
    resumed job that re-ordered the list would download the same clip twice
    under a different name
  * a half-downloaded file is never visible as a clip — not to the resume
    check, and not to the renderer, which would hand it to FFmpeg
  * per-slot layout creates EVERY slot folder, including ones with no link, or
    the per-slot playback speeds shift onto the wrong clips
"""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="driveclips_")
os.environ.pop("BVG_DRIVE_SHARED_DRIVE_ID", None)

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integrations import drive
from jobs import store
from jobs.runners import pipeline
from workspace import workspace_from_dir

store.init_db()

FOLDER = drive.FOLDER_MIME
SHORTCUT = drive.SHORTCUT_MIME


# ---- a fake Drive holding a small, awkward folder tree -----------------------
# Deliberately awkward: a sub-folder, a shortcut to a file, a shortcut to a
# folder, a shortcut whose target is gone, a PDF, a Google Doc, a synced file
# with no video mimeType, and a name that is illegal on Windows.
TREE = {
    "root": [
        {"id": "v1", "name": "beach.mp4", "mimeType": "video/mp4", "size": "10"},
        {"id": "v2", "name": "a:b?c.mp4", "mimeType": "video/mp4", "size": "20"},
        {"id": "sub", "name": "more", "mimeType": FOLDER},
        {"id": "doc", "name": "notes", "mimeType": "application/vnd.google-apps.document"},
        {"id": "pdf", "name": "brief.pdf", "mimeType": "application/pdf"},
        {"id": "sc1", "name": "linked.mp4", "mimeType": SHORTCUT,
         "shortcutDetails": {"targetId": "v9", "targetMimeType": "video/mp4"},
         "size": "40"},
        {"id": "sc0", "name": "broken.mp4", "mimeType": SHORTCUT,
         "shortcutDetails": {}},
    ],
    "sub": [
        # Synced by Drive-for-desktop: no video mimeType, only the extension.
        {"id": "v3", "name": "synced.MOV", "mimeType": "application/octet-stream",
         "size": "30"},
        # A name that collides with one in the parent folder once flattened.
        {"id": "v4", "name": "beach.mp4", "mimeType": "video/mp4", "size": "50"},
        # A shortcut back to the root: a cycle that must not loop forever.
        {"id": "sc2", "name": "loop", "mimeType": SHORTCUT,
         "shortcutDetails": {"targetId": "root", "targetMimeType": FOLDER}},
    ],
    # A separate, flat folder — one clip slot's worth.
    "solo": [
        {"id": "v5", "name": "one.mp4", "mimeType": "video/mp4", "size": "5"},
        {"id": "v6", "name": "two.mp4", "mimeType": "video/mp4", "size": "6"},
    ],
}
META = {
    "root": {"id": "root", "name": "CTA clips", "mimeType": FOLDER},
    "sub": {"id": "sub", "name": "more", "mimeType": FOLDER},
    "solo": {"id": "solo", "name": "slot two", "mimeType": FOLDER},
    "v1": {"id": "v1", "name": "beach.mp4", "mimeType": "video/mp4"},
}
BYTES = {"v1": b"a" * 10, "v2": b"b" * 20, "v3": b"c" * 30, "v4": b"d" * 50,
         "v5": b"f" * 5, "v6": b"g" * 6, "v9": b"e" * 40}

list_calls = {"n": 0}


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Files:
    def get(self, fileId=None, **kw):
        if fileId not in META:
            raise RuntimeError(f"File not found: {fileId}")
        return _Exec(META[fileId])

    def list(self, q=None, pageToken=None, **kw):
        # Only the parent clause matters to the fake.
        folder = q.split("'")[1]
        list_calls["n"] += 1
        entries = TREE.get(folder, [])
        # Paginate in twos, so the pageToken loop is actually exercised.
        start = int(pageToken or 0)
        page = entries[start:start + 2]
        nxt = start + 2
        out = {"files": page}
        if nxt < len(entries):
            out["nextPageToken"] = str(nxt)
        return _Exec(out)


drive.service = lambda: type("S", (), {"files": staticmethod(_Files)})()


# ---- listing -----------------------------------------------------------------
files = drive.list_videos("root")
names = [f["name"] for f in files]
# The root folder's files first (by name), then the sub-folder's.
assert names == ["a:b?c.mp4", "beach.mp4", "linked.mp4", "beach.mp4", "synced.MOV"], names
assert [f["id"] for f in files] == ["v2", "v1", "v9", "v4", "v3"], files
print("listed across sub-folders, shortcuts followed:", names)

# the Google Doc, the PDF and the dead shortcut are not clips
assert not {"doc", "pdf", "sc0"} & {f["id"] for f in files}
# sizes came through as ints, not the API's strings
assert [f["size"] for f in files] == [20, 10, 40, 50, 30], files
print("non-videos dropped, sizes parsed, folder cycle did not loop")

# deterministic: the same tree must produce the same order every time, or the
# positional name de-duplication below would rename clips on a resume
assert [f["id"] for f in drive.list_videos("root")] == [f["id"] for f in files]
print("listing is deterministic across calls")

# pagination really happened (7 + 3 entries in pages of 2 = 4 + 2 calls)
assert list_calls["n"] >= 6, list_calls
print(f"pagination followed: {list_calls['n']} list calls for 2 folders")

# a link, not a bare id, is what people actually paste
assert drive.extract_id(
    "https://drive.google.com/drive/folders/root?usp=sharing") == "root"

# ---- local names: illegal characters and collisions --------------------------
local = drive.local_names(files)
assert local == ["a_b_c.mp4", "beach.mp4", "linked.mp4", "beach_2.mp4",
                 "synced.MOV"], local
assert len(set(local)) == len(local)
print("local names made safe and unique:", local)
assert drive.safe_name("  ...  ") == "clip"

# ---- a file link, not a folder, is a named mistake ---------------------------
try:
    drive.list_videos("v1")
    raise AssertionError("a file id should not pass as a folder")
except drive.DriveError as exc:
    assert "not a folder" in str(exc), exc
    print("file link rejected:", str(exc)[:60], "…")

try:
    drive.folder_info("nope")
    raise AssertionError("an unknown id should raise")
except drive.DriveError as exc:
    assert "not been given access" in str(exc), exc

# ---- the file cap is an error, never a silent truncation ---------------------
try:
    drive.list_videos("root", max_files=3)
    raise AssertionError("the cap should have raised")
except drive.DriveError as exc:
    assert "more than the" in str(exc), exc
    print("over-large folder refused rather than quietly cut")

# ---- download_folder: real bytes, resume, and the .part guard ---------------
downloads = {"n": 0}
real_download_file = drive.download_file


def _fake_media(file_id, dest, expected_size=0):
    """Stands in for MediaIoBaseDownload, keeping the staging/rename dance."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and (not expected_size or dest.stat().st_size == expected_size):
        return False
    downloads["n"] += 1
    staging = dest.parent / drive._INCOMING
    staging.mkdir(exist_ok=True)
    part = staging / (dest.name + ".part")
    part.write_bytes(BYTES[file_id])
    part.replace(dest)
    return True


drive.download_file = _fake_media

dest = Path(os.environ["BVG_JOBS_ROOT"]) / "pool"
seen = []
report = drive.download_folder("root", dest, on_progress=lambda d, t: seen.append((d, t)))
assert report["files"] == 5 and report["downloaded"] == 5, report
assert report["skipped"] == 0 and report["failed"] == 0, report
assert report["bytes"] == 150, report
assert report["folder"] == "CTA clips"
assert sorted(p.name for p in dest.iterdir() if p.is_file()) == sorted(local)
assert seen[-1] == (5, 5), seen
print(f"downloaded {report['downloaded']} clips, {report['bytes']} bytes, progress reported")

# no staging folder survives a clean run
assert not (dest / drive._INCOMING).exists(), "the .incoming folder was left behind"

# resume: everything is already here, so nothing moves a second time
again = drive.download_folder("root", dest)
assert again["downloaded"] == 0 and again["skipped"] == 5, again
assert downloads["n"] == 5, "a resumed download re-fetched bytes"
print("resumed run re-downloaded nothing: 5 skipped, 0 fetched")

# a truncated file is NOT accepted as done — it is fetched again
(dest / "beach.mp4").write_bytes(b"a" * 3)
third = drive.download_folder("root", dest)
assert third["downloaded"] == 1 and third["skipped"] == 4, third
assert (dest / "beach.mp4").read_bytes() == BYTES["v1"]
print("a truncated clip is re-fetched, not trusted")

# ---- one bad clip is reported, not fatal ------------------------------------
def _one_bad(file_id, dest_path, expected_size=0):
    if file_id == "v4":
        raise RuntimeError("403 forbidden")
    return _fake_media(file_id, dest_path, expected_size)


drive.download_file = _one_bad
partial_dir = Path(os.environ["BVG_JOBS_ROOT"]) / "partial"
partial = drive.download_folder("root", partial_dir)
assert partial["downloaded"] == 4 and partial["failed"] == 1, partial
assert "403 forbidden" in partial["errors"][0], partial["errors"]
print("one unreadable clip failed alone:", partial["errors"][0][:50], "…")
drive.download_file = _fake_media

# ---- the staging folder is invisible to the renderer ------------------------
# workspace_from_dir treats every FILE in a cta_slot_N folder as a clip. A
# leftover .part must therefore live in a directory, not beside its target.
slot_probe = Path(os.environ["BVG_JOBS_ROOT"]) / "probe"
(slot_probe / "cta_slot_1" / drive._INCOMING).mkdir(parents=True)
(slot_probe / "cta_slot_1" / drive._INCOMING / "half.mp4.part").write_bytes(b"x")
(slot_probe / "cta_slot_1" / "good.mp4").write_bytes(b"y")
ws = workspace_from_dir(slot_probe, slot_probe / "work")
assert [p.name for p in ws.cta_video_slots[0]] == ["good.mp4"], ws.cta_video_slots
print("a crashed download's .part is invisible to the renderer")


# ---- the pipeline stage: per-slot layout ------------------------------------
job_id = store.new_job_id()
store.make_job_dirs(job_id)
job = {"id": job_id}
assets = store.assets_dir(job_id)

result = pipeline._drive_clips_stage(job, {
    "clip_source": "drive_folder",
    "drive_clip_layout": "per_slot",
    "slots": 4,
    # Slot 3 is deliberately blank: a user who only has clips for some slots.
    "clips_drive_folders": ["root", "solo", "", "root"],
})
assert result["layout"] == "per_slot" and result["clips_used"] == 5 + 2 + 5, result
assert result["per_slot"] == {"1": 5, "2": 2, "4": 5}, result["per_slot"]

# EVERY slot folder exists, including the one with no link — the slot count has
# to survive or RenderConfig's per-slot speeds land on the wrong clips.
slot_dirs = sorted(p.name for p in assets.glob("cta_slot_*"))
assert slot_dirs == ["cta_slot_1", "cta_slot_2", "cta_slot_3", "cta_slot_4"], slot_dirs
ws = workspace_from_dir(assets, store.work_dir(job_id))
assert [len(s) for s in ws.cta_video_slots] == [5, 2, 0, 5], ws.cta_video_slots
print("per-slot layout:", {i + 1: len(s) for i, s in enumerate(ws.cta_video_slots)},
      "— empty slot kept its folder")

# ---- the pipeline stage: pooled layout --------------------------------------
job2 = store.new_job_id()
store.make_job_dirs(job2)
assets2 = store.assets_dir(job2)
pooled = pipeline._drive_clips_stage({"id": job2}, {
    "clip_source": "drive_folder",
    "drive_clip_layout": "pooled",
    "slots": 3,
    "clips_per_slot": 1,          # must be ignored: every clip gets used
    "clips_drive_folder": "https://drive.google.com/drive/folders/root",
})
assert pooled["layout"] == "pooled", pooled
assert pooled["clip_strategy"] == "all", pooled
assert pooled["clips_available"] == 5 and pooled["clips_used"] == 5, pooled
ws2 = workspace_from_dir(assets2, store.work_dir(job2))
assert sum(len(s) for s in ws2.cta_video_slots) == 5, ws2.cta_video_slots
assert len(ws2.cta_video_slots) == 3, "pooled layout lost a slot"
print("pooled layout dealt", pooled["clips_used"], "clips across 3 slots:",
      [len(s) for s in ws2.cta_video_slots])

# ---- a missing link fails before anything is rendered -----------------------
for bad in ({"drive_clip_layout": "per_slot", "clips_drive_folders": ["", ""]},
            {"drive_clip_layout": "pooled", "clips_drive_folder": ""}):
    job3 = store.new_job_id()
    store.make_job_dirs(job3)
    try:
        pipeline._drive_clips_stage({"id": job3}, {"slots": 2, **bad})
        raise AssertionError(f"a blank link should have stopped the job: {bad}")
    except RuntimeError as exc:
        assert "link" in str(exc).lower(), exc
print("a blank Drive link stops the job with a plain message")

drive.download_file = real_download_file
print("\nALL DRIVE CLIP TESTS PASSED")
