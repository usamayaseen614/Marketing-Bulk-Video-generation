"""The music pool's route from an upload or a Drive folder to the renderer.

workspace.py already warns that the Drive downloader, the pipeline stage and
the workspace module all have to agree on "is this one of mine?", and that when
they diverged the symptom was clips vanishing between two green log lines. The
music pool adds a FOURTH place to that list and answers with a different suffix
set, so an MP3 has four chances to be silently dropped and none of them raises.

That is what this pins down: the same folder must yield the videos to the video
pools and the tracks to the music pool, and neither must ever see the other's.
"""
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="musicplumb_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import io

import workspace
from integrations import drive
from jobs import store
from jobs.runners import pipeline
from workspace import stage_uploads, workspace_from_dir

FOLDER = "application/vnd.google-apps.folder"
TMP = Path(tempfile.mkdtemp(prefix="musicplumbing_"))
store.init_db()


# --------------------------------------------------------------- the suffix sets
#
# They must not overlap: a file that answers "yes" to both would be dealt into
# two pools and handed to FFmpeg twice under different assumptions.
assert not (workspace.VIDEO_SUFFIXES & workspace.AUDIO_SUFFIXES)
for name in ("a.mp3", "A.WAV", "b.m4a", "c.flac", "d.ogg", "e.opus", "f.aac"):
    assert workspace.is_audio(name), name
    assert not workspace.is_video(name), name
for name in ("a.mp4", "b.MOV", "c.webm"):
    assert workspace.is_video(name) and not workspace.is_audio(name), name
# .m4a and .m4v differ by one character and are genuinely different things.
assert workspace.is_audio("x.m4a") and workspace.is_video("x.m4v")
print("ok: the audio and video suffix sets are disjoint and complete")


# ------------------------------------------------------------ staging an upload
class Upload:
    def __init__(self, name: str, data: bytes = b"x"):
        self.name = name
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


promo = Upload("promo.mp4", b"video-bytes")
assets = TMP / "assets"
stage_uploads(assets, promo, None, None, None,
              music_files=[Upload("b_track.mp3"), Upload("a_track.wav"),
                           Upload("c_track.m4a")])
ws = workspace_from_dir(assets, TMP / "work")
# Sorted, not upload order: a job resumed after a crash has to see the pool
# exactly as it did the first time, or the seeded per-row sequence stops being
# reproducible. Same contract as the gif and background pools.
assert [p.name for p in ws.music_paths] == ["a_track.wav", "b_track.mp3",
                                            "c_track.m4a"], ws.music_paths
# The music folder is its own: a stray video in it is not a track, and a stray
# track in bg_videos/ is not a background.
(assets / "music" / "junk.mp4").write_bytes(b"x")
(assets / "music" / "Thumbs.db").write_bytes(b"x")
(assets / "bg_videos" / "stray.mp3").write_bytes(b"x")
ws = workspace_from_dir(assets, TMP / "work")
assert [p.name for p in ws.music_paths] == ["a_track.wav", "b_track.mp3",
                                            "c_track.m4a"], ws.music_paths
assert ws.bg_video_paths == [], ws.bg_video_paths
print("ok: music stages to its own folder, sorted, with junk filtered out")

# No music uploaded at all is an empty pool, never a missing folder — the
# renderer distinguishes "none supplied" from "folder gone".
bare = TMP / "bare"
stage_uploads(bare, promo, None, None, None)
assert (bare / "music").is_dir()
assert workspace_from_dir(bare, TMP / "work2").music_paths == []
print("ok: no music uploaded gives an empty pool, not a missing folder")


# ------------------------------------------------------- reading Drive as audio
# One folder holding both kinds, plus the cases that actually break extension
# matching: a synced file with no audio mimeType, and an image alongside.
TREE = {
    "mixed": [
        {"id": "v1", "name": "clip.mp4", "mimeType": "video/mp4", "size": "10"},
        {"id": "a1", "name": "beat.mp3", "mimeType": "audio/mpeg", "size": "20"},
        # Drive-for-desktop sync: no audio mimeType, only the extension.
        {"id": "a2", "name": "synced.WAV", "mimeType": "application/octet-stream",
         "size": "30"},
        {"id": "img", "name": "cover.jpg", "mimeType": "image/jpeg", "size": "5"},
        {"id": "a3", "name": "loop.m4a", "mimeType": "audio/mp4", "size": "40"},
    ],
}
META = {"mixed": {"id": "mixed", "name": "Sounds", "mimeType": FOLDER}}
BYTES = {"a1": b"a" * 20, "a2": b"b" * 30, "a3": b"c" * 40, "v1": b"d" * 10}


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

audio = [f["name"] for f in drive.list_videos("mixed", kind="audio")]
assert audio == ["beat.mp3", "loop.m4a", "synced.WAV"], audio
video = [f["name"] for f in drive.list_videos("mixed")]
assert video == ["clip.mp4"], video
print(f"ok: one Drive folder reads as {len(audio)} tracks or {len(video)} clip")

ok, message = drive.check_source("mixed", kind="audio")
assert ok and "3 tracks" in message, message
ok, message = drive.check_source("mixed")
assert ok and "1 clips" in message, message
print("ok: check_source counts tracks or clips depending on what is asked")


def _fake_download(file_id, dest, expected_size=0):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size == expected_size:
        return False
    dest.write_bytes(BYTES[file_id])
    return True


drive.download_file = _fake_download

# The stage the pipeline actually runs. If it forgets kind="audio" the folder
# reads as one video file and the music pool comes back empty, with a green log
# line either way.
job_id = store.new_job_id()
store.make_job_dirs(job_id)
store.create_job(kind=store.KIND_PIPELINE, params={}, label="music-plumb",
                 job_id=job_id)
report = pipeline._drive_pool_stage(
    {"id": job_id}, {"music_drive_folder": "mixed"},
    "music_drive_folder", "music", "music", kind="audio")
assert report["downloaded"] == 3, report
landed = sorted(p.name for p in (store.assets_dir(job_id) / "music").iterdir()
                if p.is_file())
assert landed == ["beat.mp3", "loop.m4a", "synced.WAV"], landed
print("ok: the pipeline stage pulls the tracks, not the clips, out of Drive")

# ...and that folder rehydrates into a Workspace the renderer can use.
(store.assets_dir(job_id) / "input.mp4").write_bytes(b"x")
ws = workspace_from_dir(store.assets_dir(job_id), TMP / "work3")
assert [p.name for p in ws.music_paths] == landed, ws.music_paths
print("ok: the downloaded folder rehydrates into Workspace.music_paths")

print("\nall music-plumbing checks passed")
