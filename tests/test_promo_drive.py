"""Drive originals through the real UI, downloader, pipeline and FFmpeg."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="promo_drive_")
os.environ.pop("BVG_DRIVE_SHARED_DRIVE_ID", None)

import httplib2
import pandas as pd
from googleapiclient.http import HttpRequest
from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

import config
from integrations import drive
from jobs import store, worker
from jobs.runners import pipeline
from video_generator import find_ffmpeg
from workspace import MAX_PROMO_VIDEOS, workspace_from_dir

config.DRIVE_ROOT_FOLDER_ID = ""
scratch = Path(os.environ["BVG_JOBS_ROOT"])
media = {}
for file_id, color in (("first", "red"), ("second", "blue")):
    path = scratch / f"{file_id}.mp4"
    subprocess.run([
        find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=160x284:r=30",
        "-t", "0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True, capture_output=True)
    media[file_id] = path.read_bytes()

entries = [{"id": file_id, "name": name, "size": str(len(media[file_id])),
            "mimeType": "video/mp4"}
           for file_id, name in (("second", "b.mp4"), ("first", "a.mp4"))]
requests = []
failed = set()
list_calls = []


class Reply:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Transport:
    def request(self, uri, method="GET", **kwargs):
        file_id = uri.rsplit("/", 1)[1]
        requests.append(file_id)
        if file_id in failed:
            raise RuntimeError("Source video is unavailable")
        data = media[file_id]
        return httplib2.Response({"status": "200", "content-length": str(len(data))}), data


class Files:
    def get(self, fileId, **kwargs):
        return Reply({"id": fileId, "name": fileId, "mimeType": drive.FOLDER_MIME})

    def list(self, q, **kwargs):
        folder = q.split("'")[1]
        list_calls.append(folder)
        if folder == "unavailable":
            raise RuntimeError("Drive list is temporarily unavailable")
        if folder == "too_many":
            return Reply({"files": [{**entries[0], "id": str(i)}
                                    for i in range(MAX_PROMO_VIDEOS + 1)]})
        return Reply({"files": entries if folder == "originals" else []})

    def get_media(self, fileId, **kwargs):
        return HttpRequest(Transport(), lambda response, content: content,
                           f"https://drive.test/{fileId}")


class Service:
    def files(self):
        return Files()


drive.service = Service


def workbook(headers, row):
    wb = Workbook()
    wb.active.append(headers)
    wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def control(kind, label):
    return next(item for item in getattr(at, kind) if item.label == label)


def run():
    at.run()
    assert not at.exception, [(e.message, e.stack_trace) for e in at.exception]


at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
run()
control("file_uploader", "Excel file (.xlsx)").set_value(
    ("campaign.xlsx", workbook(["Headline"], ["Base"]), "application/octet-stream"))
control("file_uploader", "Promo video(s) (MP4) — up to 600").set_value(
    [("ignored.mp4", media["first"], "video/mp4")])
control("radio", "Promo video source").set_value("drive_folder")
run()
assert control("button", "🚀 Generate All Videos").disabled
for folder, expected in (("too_many", False), ("empty", False), ("originals", True)):
    control("text_input", "Promo videos Google Drive folder link").set_value(
        f"https://drive.google.com/drive/folders/{folder}")
    run()
    at.button(key="read_drive_promos").click()
    run()
    assert control("button", "🚀 Generate All Videos").disabled == (not expected)
    if folder == "too_many":
        assert any("600" in item.value for item in at.error)
assert requests == [], "listing must not fetch video bytes"
assert control("number_input", "Batches to render").value == 2
assert control("selectbox", "Promo video").options == ["1. a.mp4", "2. b.mp4"]
assert control("file_uploader", "Promo video(s) (MP4) — up to 600").disabled

control("text_input", "Promo videos Google Drive folder link").set_value(
    "https://drive.google.com/drive/folders/unavailable")
run()
assert control("button", "🚀 Generate All Videos").disabled
at.button(key="read_drive_promos").click()
run()
assert any("temporarily unavailable" in item.value for item in at.error)
assert control("button", "🚀 Generate All Videos").disabled
assert "_drive_promos" not in at.session_state
control("text_input", "Promo videos Google Drive folder link").set_value(
    "https://drive.google.com/drive/folders/originals")
run()
at.button(key="read_drive_promos").click()
run()

control("file_uploader", "Headline by promo (.xlsx)").set_value(
    ("headlines.xlsx", workbook(["a.mp4", "b.mp4"], ["First text", "Second text"]),
     "application/octet-stream"))
for label in ("Where do the GIFs come from?", "Where do the background images come from?",
              "Where do the background videos come from?", "Where does the music come from?"):
    control("radio", label).set_value("upload")
control("radio", "Captions").set_value("none")
control("slider", "Parallel renders").set_value(1)
run()
control("selectbox", "Promo video").set_value(1)
control("button", "👁️ Preview Row").click()
run()
assert requests == ["second"], requests
assert at.session_state["preview_promo"] == "b.mp4"

control("radio", "Promo video source").set_value("upload")
run()
assert control("number_input", "Batches to render").value == 1
assert "preview_payload" not in at.session_state
control("radio", "Promo video source").set_value("drive_folder")
run()
assert control("text_input", "Promo videos Google Drive folder link").value.endswith("originals")
assert control("number_input", "Batches to render").value == 2

at.button(key="switch_language").click()
run()
assert control("radio", "原视频来源").value == "drive_folder"
assert at.button(key="read_drive_promos").label == "读取原视频列表"
assert requests == ["second"]
at.button(key="switch_language").click()
run()

control("button", "🚀 Generate All Videos").click()
run()
jobs = store.list_jobs()
assert len(jobs) == 1, [e.value for e in at.error]
job = jobs[0]
assert job["kind"] == store.KIND_PIPELINE
assert job["params"]["promo_names"] == ["a.mp4", "b.mp4"]
assert [entry["id"] for entry in job["params"]["promos_drive_files"]] == ["first", "second"]
assert not list(store.assets_dir(job["id"]).glob("input*.mp4"))
assert requests == ["second"], "submitting must leave downloads to the worker"

# Folder edits after submission must not change batch or text assignments.
entries[:] = [{**entries[0], "id": "later", "name": "0_new.mp4"}]
before = len(list_calls)
failed.add("second")
try:
    pipeline._drive_promos_stage(job, job["params"])
    raise AssertionError("a missing original must stop before rendering")
except RuntimeError as exc:
    assert "Nothing was rendered" in str(exc), exc
assert store.item_counts(job["id"])["total"] == 0
ws = workspace_from_dir(store.assets_dir(job["id"]), store.work_dir(job["id"]))
assert len(ws.video_paths) == 1
assert ws.video_paths[0].read_bytes() == media["first"]

failed.clear()
worker.main(["--once"])
job = store.get_job(job["id"])
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
assert len(list_calls) == before, "the worker must use the submitted list"
assert job["result"]["promos"]["skipped"] == 1
assert job["result"]["promos"]["downloaded"] == 1
manifest = pd.read_excel(job["result"]["sheet_path"]).sort_values("Source_Batch")
assert manifest["Headline"].tolist() == ["First text", "Second text"], manifest
assert manifest["Promo"].tolist() == ["input.mp4", "input_2.mp4"], manifest
assert len(list(store.videos_dir(job["id"]).rglob("*.mp4"))) == 2
print("ALL DRIVE PROMO TESTS PASSED")
