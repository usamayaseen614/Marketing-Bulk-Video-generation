"""The resume guards on Drive uploads: bytes go up once, the copy is made once.

A worker can die between any two statements, so every Drive write is recorded
the instant it succeeds and skipped when its record already exists — for BOTH
files: the yt/ upload (drive_file_id) and the tk/ copy (copy_file_id). And
because files.copy is not idempotent, a RE-attempt looks in the tk/ folder for
an ambiguous previous copy before making another one.

These tests were rebuilt after mutation testing showed the first version
passed with the instant-persist lines deleted (its crash fixtures were built
from final DB state) and with the copy handed the SHORT name. Every scenario
below fails if its guard, persist, or name is removed."""
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

N_ROWS_1 = 2
placement1 = {Slot(batch=1, row=1): 1, Slot(batch=1, row=2): 1}


def make_job(label: str, rows: list[tuple[int, str]]) -> str:
    job_id = store.create_job(
        kind=store.KIND_RENDER, params={}, label=label,
        items=[{"idx": i} for i, _ in rows],
    )
    store.make_job_dirs(job_id)
    videos = store.videos_dir(job_id) / batching.source_folder_name(1)
    videos.mkdir(parents=True, exist_ok=True)
    for i, name in rows:
        (videos / name).write_bytes(b"fake mp4 bytes")
        store.update_item(
            job_id, i, name=name, render_status=store.ITEM_DONE,
            meta={"batch": 1, "row": i, "short_name": name,
                  "long_name": name.replace(".mp4", " #x #y.mp4")})
    return job_id


# ---- a stub Drive that counts every write -----------------------------------
calls = {"upload": [], "copy": [], "find": []}
find_result: dict = {"value": None}     # what find_file "sees" in tk/
fail_next_copy: dict = {"exc": None}    # armed per scenario

drive.set_target = lambda value: None
drive.ensure_path = lambda parts, parent_id=None: "folder-" + "-".join(parts)
drive.folder_link = lambda fid: f"https://drive/{fid}"
drive.resolve_target = lambda: ("0AFakeSharedDrive", "0AFakeSharedDrive")


def _upload_file(path, parent_id, name=None):
    calls["upload"].append((Path(path).name, parent_id, name))
    return {"id": f"up-{len(calls['upload'])}", "name": name,
            "webViewLink": "https://file/up"}


def _copy_file(file_id, new_name, parent_id=None):
    if fail_next_copy["exc"]:
        exc, fail_next_copy["exc"] = fail_next_copy["exc"], None
        raise exc
    calls["copy"].append((file_id, new_name, parent_id))
    return {"id": f"cp-{len(calls['copy'])}", "name": new_name,
            "webViewLink": "https://file/cp"}


def _find_file(name, parent_id, drive_id=None):
    calls["find"].append((name, parent_id, drive_id))
    assert drive_id == "0AFakeSharedDrive", \
        "find_file must get the runner-thread drive id, not resolve its own"
    return find_result["value"]


drive.upload_file = _upload_file
drive.copy_file = _copy_file
drive.find_file = _find_file

job1 = make_job("resume-guard", [(1, "one caption #a.mp4"),
                                 (2, "two caption #b.mp4")])

# ---- first run: two uploads into yt/, two copies into tk/ -------------------
res = render._upload(store.get_job(job1), N_ROWS_1, 1, placement1)
assert len(calls["upload"]) == 2 and len(calls["copy"]) == 2, calls
assert not calls["find"], "first attempts must not spend a find_file call"
assert all(parent == "folder-yt" for _, parent, _ in calls["upload"])
assert all(parent == "folder-tk" for _, _, parent in calls["copy"])
# The upload carries the SHORT name, the copy the LONG one — exactly.
assert {n for _, _, n in calls["upload"]} == \
    {"one caption #a.mp4", "two caption #b.mp4"}, calls["upload"]
assert {n for _, n, _ in calls["copy"]} == \
    {"one caption #a #x #y.mp4", "two caption #b #x #y.mp4"}, calls["copy"]
items = {i["idx"]: i for i in store.list_items(job1)}
assert all(i["upload_status"] == store.ITEM_DONE for i in items.values())
assert res["uploaded"] == 2 and res["drive_files"] == 4, res
print("first run: short names into yt/, long names into tk/, no find calls")

# ---- crash window A: worker died between the upload and the copy ------------
meta = dict(items[1]["meta"])
meta.pop("copy_file_id", None)
meta.pop("copy_link", None)
store.update_item(job1, 1, upload_status=store.ITEM_PENDING, meta=meta)

render._upload(store.get_job(job1), N_ROWS_1, 1, placement1)
assert len(calls["upload"]) == 2, "the yt bytes were uploaded AGAIN"
assert len(calls["find"]) == 1, "a re-attempt must look in tk/ first"
assert len(calls["copy"]) == 3, "the missing tk copy was not made"
assert calls["copy"][2][1] == "one caption #a #x #y.mp4", calls["copy"][2]
items = {i["idx"]: i for i in store.list_items(job1)}
assert items[1]["meta"]["copy_file_id"] == "cp-3"
print("crash between upload and copy: upload skipped, tk/ checked, copy re-made")

# ---- crash window B: both ids persisted, item still pending -----------------
store.update_item(job1, 1, upload_status=store.ITEM_PENDING)
render._upload(store.get_job(job1), N_ROWS_1, 1, placement1)
assert len(calls["upload"]) == 2 and len(calls["copy"]) == 3
assert len(calls["find"]) == 1, "recorded ids need no find_file call"
items = {i["idx"]: i for i in store.list_items(job1)}
assert items[1]["upload_status"] == store.ITEM_DONE
assert items[1]["drive_link"] == "https://file/cp"
print("crash after the copy: NOTHING re-sent, the item just marked done")

# ---- ambiguous retry: the previous attempt's copy IS in tk/ -----------------
# files.copy committed but the response was lost. The re-attempt must adopt
# the existing file, not make a same-named sibling.
meta = dict(items[2]["meta"])
meta.pop("copy_file_id", None)
meta.pop("copy_link", None)
store.update_item(job1, 2, upload_status=store.ITEM_PENDING, meta=meta)
find_result["value"] = {"id": "cp-found", "name": "two caption #b #x #y.mp4",
                        "webViewLink": "https://file/found"}

render._upload(store.get_job(job1), N_ROWS_1, 1, placement1)
find_result["value"] = None
assert len(calls["copy"]) == 3, "copied AGAIN despite the file existing in tk/"
assert len(calls["find"]) == 2
items = {i["idx"]: i for i in store.list_items(job1)}
assert items[2]["meta"]["copy_file_id"] == "cp-found"
assert items[2]["drive_link"] == "https://file/found"
assert items[2]["upload_status"] == store.ITEM_DONE
print("ambiguous retry: existing tk/ copy adopted, no duplicate made")

# ---- the copy raising must NOT lose the already-uploaded yt/ file -----------
# Proves drive_file_id is persisted the INSTANT the upload succeeds — not in
# the final 'done' write, which never happens here.
job2 = make_job("copy-blows-up", [(1, "three caption #c.mp4")])
fail_next_copy["exc"] = RuntimeError("socket reset during files.copy")

render._upload(store.get_job(job2), 1, 1, {Slot(batch=1, row=1): 1})
item = store.list_items(job2)[0]
assert item["upload_status"] == store.ITEM_FAILED
assert "socket reset" in (item["upload_error"] or "")
assert item["drive_file_id"] == "up-3", \
    "upload id must be durable BEFORE the copy is attempted"
uploads_before = len(calls["upload"])

store.update_item(job2, 1, upload_status=store.ITEM_PENDING)
render._upload(store.get_job(job2), 1, 1, {Slot(batch=1, row=1): 1})
assert len(calls["upload"]) == uploads_before, "re-uploaded after a copy failure"
item = store.list_items(job2)[0]
assert item["upload_status"] == store.ITEM_DONE
print("copy failure: yt/ id already durable, retry copies without re-uploading")

# ---- a HARD crash between the copy and the 'done' write ---------------------
# Proves copy_file_id is persisted in its OWN update, before 'done'. The crash
# is a BaseException so the `except Exception` handler cannot catch it — an
# ordinary exception would be saved by the handler's own meta write, which is
# exactly how the first version of this test failed to prove anything.
job3 = make_job("done-write-blows-up", [(1, "four caption #d.mp4")])
real_update = store.update_item
armed = {"on": True}


class _Crash(BaseException):
    """The process dying mid-statement, as far as `except Exception` knows."""


def _dying_update(job_id, idx, **kw):
    if armed["on"] and kw.get("upload_status") == store.ITEM_DONE:
        armed["on"] = False
        raise _Crash()
    return real_update(job_id, idx, **kw)


store.update_item = _dying_update
try:
    render._upload(store.get_job(job3), 1, 1, {Slot(batch=1, row=1): 1})
    raise AssertionError("the simulated crash never fired")
except _Crash:
    pass
finally:
    store.update_item = real_update

item = store.list_items(job3)[0]
# A hard crash leaves no FAILED marker — the item is still pending...
assert item["upload_status"] == store.ITEM_PENDING, item["upload_status"]
# ...but the copy id must already be durable, from its own earlier write.
assert item["meta"].get("copy_file_id"), \
    "copy id must be durable BEFORE the 'done' write"
copies_before = len(calls["copy"])

render._upload(store.get_job(job3), 1, 1, {Slot(batch=1, row=1): 1})
assert len(calls["copy"]) == copies_before, "re-copied after a hard crash"
assert store.list_items(job3)[0]["upload_status"] == store.ITEM_DONE
print("hard crash before 'done': tk/ id already durable, retry touches nothing")

# ---- deploy boundary: a legacy mid-upload job keeps its date root -----------
# Jobs from before stamps existed uploaded into renders/<date>/. Resumed under
# the new code they must NOT mint a millisecond stamp and split the tree.
params = store.get_job(job1)["params"]
assert params.get("drive_stamp"), "run 1 should have pinned a stamp"
store.merge_job_params(job1, drive_stamp="")     # simulate a pre-stamp job
params = store.get_job(job1)["params"]
legacy = render._drive_root_stamp(job1, params)
import re
assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", legacy), \
    f"mid-upload legacy job got a fresh stamp: {legacy}"
assert store.get_job(job1)["params"]["drive_stamp"] == legacy
print(f"legacy mid-upload job keeps its date root: renders/{legacy}/")

# ...while a job with nothing in Drive yet gets a millisecond stamp.
job4 = make_job("fresh", [(1, "five caption #e.mp4")])
fresh = render._drive_root_stamp(job4, store.get_job(job4)["params"])
assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.\d{3}", fresh), fresh
print(f"job with no uploads gets the millisecond stamp: renders/{fresh}/")

print("\nALL UPLOAD RESUME TESTS PASSED")
