"""tools/zip_drive_tk.py against a faked Drive tree.

This is the tool that runs over a night's finished output, so the assertions
are about the promises made to whoever runs it: it finds the right folders,
each archive holds exactly that folder's files, a run can be repeated without
re-doing work or duplicating archives, a download failure publishes nothing —
and it never, under any path, deletes anything in Drive."""
import os, sys, tempfile, zipfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="ziptool_")
os.environ["BVG_DRIVE_SHARED_DRIVE_ID"] = "0AFakeSharedDrive"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integrations import drive
from tools import zip_drive_tk as tool

WORK = Path(tempfile.mkdtemp(prefix="ziptoolwork_"))

# ---- a Drive that exists only in this dict ---------------------------------
# renders/2026-08-05/night-run/batch_NN/{tk,yt}/*.mp4
FOLDERS = {
    "R":     {"name": "night-run",  "children": ["B1", "B2"]},
    "B1":    {"name": "batch_01",   "children": ["B1TK", "B1YT"]},
    "B2":    {"name": "batch_02",   "children": ["B2TK", "B2YT"]},
    "B1TK":  {"name": "tk",         "children": []},
    "B1YT":  {"name": "yt",         "children": []},
    "B2TK":  {"name": "tk",         "children": []},
    "B2YT":  {"name": "yt",         "children": []},
}
FILES: dict[str, list[dict]] = {
    "B1TK": [{"id": f"f{n}", "name": f"caption {n} #a #b #c #d #e.mp4",
              "size": 1024 * n, "md5": "", "folder": False} for n in (1, 2, 3)],
    "B1YT": [{"id": f"s{n}", "name": f"caption {n} #a.mp4",
              "size": 1024 * n, "md5": "", "folder": False} for n in (1, 2, 3)],
    "B2TK": [{"id": f"g{n}", "name": f"other {n} #a #b #c #d #e.mp4",
              "size": 2048 * n, "md5": "", "folder": False} for n in (1, 2)],
    "B2YT": [],
}
# What is sitting in each folder besides its sub-folders — i.e. published zips.
PUBLISHED: dict[str, dict[str, dict]] = {}

deleted: list = []          # must stay empty for the whole run
downloads: list[str] = []
fail_download: dict = {"id": None}


def _list_files(folder_id, include_folders=False):
    out = list(FILES.get(folder_id, []))
    out += [dict(v, folder=False) for v in PUBLISHED.get(folder_id, {}).values()]
    if include_folders:
        out += [{"id": c, "name": FOLDERS[c]["name"], "size": 0, "md5": "",
                 "folder": True} for c in FOLDERS.get(folder_id, {}).get("children", [])]
    return sorted(out, key=lambda f: (f["name"], f["id"]))


def _find_file(name, parent_id, drive_id=None):
    assert drive_id == "0AFakeSharedDrive", "the shared-drive id was not threaded through"
    return PUBLISHED.get(parent_id, {}).get(name)


def _download_file(file_id, dest, expected_size=0):
    if fail_download["id"] == file_id:
        raise RuntimeError("503 from Drive")
    downloads.append(file_id)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_id.encode() * (expected_size // len(file_id)))
    return True


def _upload_or_replace(path, parent_id, name, drive_id=None):
    path = Path(path)
    size = path.stat().st_size
    with zipfile.ZipFile(path) as zf:
        entries = zf.namelist()
    slot = PUBLISHED.setdefault(parent_id, {})
    slot[name] = {"id": f"zip-{parent_id}-{name}", "name": name,
                  "size": size, "entries": entries}
    return dict(slot[name], size=str(size), webViewLink=f"https://file/{name}")


drive.set_target = lambda value: None
drive.resolve_target = lambda: ("0AFakeSharedDrive", "0AFakeSharedDrive")
drive.folder_link = lambda fid: f"https://drive/{fid}"
drive.folder_info = lambda fid: {"id": fid, "name": FOLDERS[fid]["name"]}
drive.list_files = _list_files
drive.find_file = _find_file
drive.download_file = _download_file
drive.upload_or_replace = _upload_or_replace
drive.service = lambda: (_ for _ in ()).throw(
    AssertionError("the tool reached for a raw Drive service — it must not"))


def run(*args) -> int:
    return tool.main(["--link", "R", "--work-dir", str(WORK), *args])


# ---- dry run changes nothing ------------------------------------------------
assert run("--dry-run") == 0
assert not PUBLISHED and not downloads, "a dry run touched Drive"
print("dry run: reports the plan, downloads nothing, publishes nothing")


# ---- the dry run must report what is LEFT, not what was asked for -----------
# After a partial run this is the question being asked: which folders still
# need doing? Reporting all 16 when 15 are finished is worse than useless.
import io, contextlib

PUBLISHED.setdefault("B1", {})["tk.zip"] = {
    "id": "zip-B1-tk.zip", "name": "tk.zip",
    "size": sum(f["size"] for f in FILES["B1TK"]), "entries": []}
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    run("--dry-run")
report = buffer.getvalue()
PUBLISHED.pop("B1")

assert "1 of 2 folder(s) already done" in report, report
assert "already there" in report, report
assert "-> to do" in report, report
# The remaining total must exclude the finished folder, or the quota estimate
# it feeds is wrong in exactly the direction that matters.
b2_bytes = sum(f["size"] for f in FILES["B2TK"])
assert f"Still to move: {tool.human(b2_bytes)}" in report, report
print("dry run: counts only the folders still outstanding")

# A half-uploaded archive is reported as work still to do, not as finished.
PUBLISHED.setdefault("B1", {})["tk.zip"] = {
    "id": "zip-B1-tk.zip", "name": "tk.zip", "size": 10, "entries": []}
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    run("--dry-run")
report = buffer.getvalue()
PUBLISHED.pop("B1")
assert "a partial upload" in report, report
assert "0 of 2 folder(s) already done" in report, report
print("dry run: an undersized archive is flagged as a rebuild, not a skip")


# ---- the real thing ---------------------------------------------------------
assert run("--concurrency", "2") == 0
assert set(PUBLISHED) == {"B1", "B2"}, PUBLISHED
assert list(PUBLISHED["B1"]) == ["tk.zip"] and list(PUBLISHED["B2"]) == ["tk.zip"]
assert PUBLISHED["B1"]["tk.zip"]["entries"] == [
    "caption 1 #a #b #c #d #e.mp4", "caption 2 #a #b #c #d #e.mp4",
    "caption 3 #a #b #c #d #e.mp4"], PUBLISHED["B1"]["tk.zip"]["entries"]
assert PUBLISHED["B2"]["tk.zip"]["entries"] == [
    "other 1 #a #b #c #d #e.mp4", "other 2 #a #b #c #d #e.mp4"]
# The archive goes NEXT TO the tk/ folder, not inside it.
assert "tk.zip" not in PUBLISHED.get("B1TK", {})
assert sorted(downloads) == ["f1", "f2", "f3", "g1", "g2"]
print("run: one tk.zip per batch folder, holding exactly that folder's files")

assert not deleted, "something was deleted from Drive"
# Every downloaded video is deleted the moment it is inside the archive, and
# the archive itself once it is uploaded — nothing is left occupying the disk.
leftovers = [p for p in WORK.rglob("*") if p.is_file() and p.name != "state.json"]
assert not leftovers, f"local files left behind: {leftovers}"
print("run: local scratch fully reclaimed, nothing deleted in Drive")


# ---- a second run is a no-op ------------------------------------------------
downloads.clear()
sizes_before = {k: dict(v) for k, v in PUBLISHED.items()}
assert run() == 0
assert not downloads, "a repeat run downloaded again"
assert PUBLISHED == sizes_before, "a repeat run rewrote the archives"
print("repeat run: recorded folders skipped, archives untouched")


# ---- state lost, archive present: still skipped, and never duplicated -------
(WORK / "state.json").unlink()
downloads.clear()
assert run() == 0
assert not downloads, "a full-size archive already in Drive was rebuilt"
assert list(PUBLISHED["B1"]) == ["tk.zip"], "a second tk.zip appeared"
print("state lost: a full-size archive in Drive is proof enough to skip")


# ---- a half-uploaded archive IS rebuilt -------------------------------------
(WORK / "state.json").unlink()
PUBLISHED["B1"]["tk.zip"] = dict(PUBLISHED["B1"]["tk.zip"], size=10)
downloads.clear()
assert run("--only", "batch_01") == 0
assert sorted(downloads) == ["f1", "f2", "f3"], downloads
assert PUBLISHED["B1"]["tk.zip"]["size"] > 6 * 1024
assert list(PUBLISHED["B1"]) == ["tk.zip"], "the rebuild left two archives"
print("partial upload: an undersized archive is rebuilt in place, not doubled")


# ---- a download failure publishes nothing -----------------------------------
(WORK / "state.json").unlink()
PUBLISHED.pop("B2")
fail_download["id"] = "g1"
try:
    run("--only", "batch_02")
    raise AssertionError("a failed download was published anyway")
except SystemExit as exc:
    assert "could not be downloaded" in str(exc), exc
assert "B2" not in PUBLISHED, "an incomplete archive was published"
leftovers = [p for p in WORK.rglob("*") if p.is_file() and p.name != "state.json"]
assert not leftovers, f"a failed folder left files on disk: {leftovers}"
print("download failure: nothing published, scratch cleaned, Drive untouched")

# ...unless you say you want it anyway.
assert run("--only", "batch_02", "--allow-partial") == 0
assert PUBLISHED["B2"]["tk.zip"]["entries"] == ["other 2 #a #b #c #d #e.mp4"]
fail_download["id"] = None
print("--allow-partial: publishes what did arrive, and only then")


# ---- --platform yt reaches the other half -----------------------------------
(WORK / "state.json").unlink()
assert run("--platform", "yt", "--only", "batch_01") == 0
assert sorted(PUBLISHED["B1"]) == ["tk.zip", "yt.zip"]
assert PUBLISHED["B1"]["yt.zip"]["entries"] == [
    "caption 1 #a.mp4", "caption 2 #a.mp4", "caption 3 #a.mp4"]
print("--platform yt: same walk, short-named archive alongside the long one")

# An empty source folder is skipped rather than published as an empty archive.
(WORK / "state.json").unlink()
assert run("--platform", "yt", "--only", "batch_02") == 0
assert "yt.zip" not in PUBLISHED["B2"], "an empty folder produced an archive"
print("empty folder: skipped, not published as an empty archive")

assert not deleted, "something was deleted from Drive"
print("\nALL ZIP TOOL TESTS PASSED")
