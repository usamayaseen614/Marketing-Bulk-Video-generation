"""Background images from a Drive folder: Drive -> backgrounds/ -> the renderer.

The folder has to land where a ZIP upload would have been extracted, and Drive
must fetch exactly the extensions the renderer will index — a .gif it downloaded
would sit there unused behind a green log line.
"""
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="bgimgdrive_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import video_generator
import workspace
from integrations import drive
from jobs import store
from jobs.runners import pipeline
from workspace import workspace_from_dir

FOLDER = "application/vnd.google-apps.folder"
TMP = Path(tempfile.mkdtemp(prefix="bgimgdrive_ws_"))
store.init_db()

# One definition, shared with the renderer's own index.
assert video_generator.IMAGE_EXTENSIONS is workspace.IMAGE_SUFFIXES
assert not (workspace.IMAGE_SUFFIXES & workspace.VIDEO_SUFFIXES)
print("ok: Drive and the renderer share one image-extension set")

TREE = {
    "bgs": [
        {"id": "i1", "name": "Blue.PNG", "mimeType": "image/png", "size": "10"},
        # Drive-for-desktop sync: no image mimeType, only the extension.
        {"id": "i2", "name": "sunset.jpg", "mimeType": "application/octet-stream",
         "size": "20"},
        # Labelled an image by Drive but not something the renderer indexes.
        {"id": "g1", "name": "loop.gif", "mimeType": "image/gif", "size": "5"},
        {"id": "v1", "name": "clip.mp4", "mimeType": "video/mp4", "size": "7"},
        {"id": "sub", "name": "more", "mimeType": FOLDER},
    ],
    "sub": [{"id": "i3", "name": "deep.webp", "mimeType": "image/webp", "size": "30"}],
}
META = {"bgs": {"id": "bgs", "name": "Backgrounds", "mimeType": FOLDER}}
BYTES = {"i1": b"a" * 10, "i2": b"b" * 20, "i3": b"c" * 30}


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Files:
    def get(self, fileId=None, **kw):
        return _Exec(META[fileId])

    def list(self, q=None, pageToken=None, **kw):
        return _Exec({"files": TREE.get(q.split("'")[1], [])})


drive.service = lambda: type("S", (), {"files": staticmethod(_Files)})()

names = [f["name"] for f in drive.list_videos("bgs", kind="image")]
# Ordered by folder path first, then name — the sub-folder's file comes last.
assert names == ["Blue.PNG", "sunset.jpg", "deep.webp"], names
print(f"ok: a Drive folder reads as {len(names)} images (gif and video ignored)")

ok, message = drive.check_source("bgs", kind="image")
assert ok and "3 images" in message, message
print("ok: check_source counts images")


def _fake_download(file_id, dest, expected_size=0):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == expected_size:
        return False
    dest.write_bytes(BYTES[file_id])
    return True


drive.download_file = _fake_download

job_id = store.new_job_id()
store.make_job_dirs(job_id)
store.create_job(kind=store.KIND_PIPELINE, params={}, label="bg-img-drive",
                 job_id=job_id)
report = pipeline._drive_pool_stage(
    {"id": job_id}, {"bg_images_drive_folder": "bgs"},
    "bg_images_drive_folder", "backgrounds", "background images", kind="image")
assert report["downloaded"] == 3, report
assets = store.assets_dir(job_id)
landed = sorted(p.name for p in (assets / "backgrounds").iterdir() if p.is_file())
assert landed == ["Blue.PNG", "deep.webp", "sunset.jpg"], landed
print("ok: the pipeline stage lands the images in backgrounds/")

# The renderer's own index must resolve them by name, case-insensitively — which
# is what an Excel BG_Image cell does.
(assets / "input.mp4").write_bytes(b"x")
ws = workspace_from_dir(assets, TMP / "work")
index, idx_names = video_generator.VideoGenerator._build_bg_index(ws.bg_dir)
assert {"blue.png", "sunset.jpg", "deep.webp"} <= set(index), sorted(index)
print("ok: the downloaded folder rehydrates into a pool BG_Image can name")

print("\nall bg-image-drive checks passed")
